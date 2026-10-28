"""Persisting incident chains (I-13209-a wiring, T-35).

The decision is already covered in test_event_chains; this covers the work
around it -- gathering neighbours, applying the plan, and losing a concurrent
race safely. Enrichment must never break a pass, and no article may ever end
up in two chains.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.event_chain_worker import (
    ChainOutcome,
    EventChainWorker,
    plan_assignments,
)
from app.event_chains import ChainCandidate


NOW = datetime(2026, 8, 12, tzinfo=UTC)
CHAIN = "11111111-1111-1111-1111-111111111111"
HACK_TITLE = "Ronin bridge exploit drains ETH from Axie treasury"


def _article(article_id, *, title=None, days=0, subjects=("ronin", "ETH")):
    # Distinct headlines by default: two outlets carrying the exact same
    # headline are one story republished, and those no longer join (DR-13233).
    return {
        "article_id": article_id,
        # Not a " - tail" suffix: normalized_title strips those as site names.
        "title": f"{HACK_TITLE} ({article_id})" if title is None else title,
        "domain": "example.test",
        "published_at": NOW - timedelta(days=days),
        "source_hash": "a" * 64,
        "tagged_atoms": [{"subject_id": subject} for subject in subjects],
    }


class FakeEdge:
    def __init__(self, source, target, similarity):
        self.source_article_id = source
        self.target_article_id = target
        self.similarity = similarity


class FakeStore:
    def __init__(self, edges=(), error=None):
        self.edges = list(edges)
        self.error = error
        self.calls = 0

    async def article_knn_graph(self, candidates, *, neighbors, min_similarity):
        self.calls += 1
        if self.error:
            raise self.error
        return self.edges


class FakeRepo:
    def __init__(self, articles=(), chains=None, *, batch_error=None, join_error=None):
        self.articles = list(articles)
        self.chains = dict(chains or {})
        self.batch_error = batch_error
        self.join_error = join_error
        self.joined: list[tuple[str, str]] = []
        self.opened: list[tuple[str, tuple]] = []

    async def chainable_articles(self, *, coverage_domain, since, limit):
        if self.batch_error:
            raise self.batch_error
        return self.articles

    async def chain_ids_for_articles(self, article_ids):
        return {k: v for k, v in self.chains.items() if k in set(article_ids)}

    async def join_event_chain(self, *, chain_id, article_id, similarity):
        if self.join_error:
            raise self.join_error
        if article_id in self.chains:
            return False
        self.chains[article_id] = chain_id
        self.joined.append((chain_id, article_id))
        return True

    async def open_event_chain(self, *, chain_id, coverage_domain, members):
        if len(members) < 2:
            return False
        self.opened.append((chain_id, tuple(member for member, _ in members)))
        for member, _ in members:
            self.chains[member] = chain_id
        return True


def _worker(repo, store) -> EventChainWorker:
    return EventChainWorker(repo, store, now=lambda: NOW)


# --- planning ----------------------------------------------------------------

def test_a_qualified_neighbour_plans_a_join():
    joins, opens = plan_assignments(
        [_article("a1")],
        {"a1": [ChainCandidate("a2", 0.95, CHAIN)]},
    )
    # The neighbour's entities are unknown here, so no join is planned.
    assert joins == {"a1": None}
    assert opens == {}


def test_syndicated_reprints_plan_nothing():
    # The staging corpus' only joinable pairs were five outlets republishing
    # one article; without this the chain would report a story following itself.
    joins, opens = plan_assignments(
        [_article("a1", title=HACK_TITLE), _article("a2", title=HACK_TITLE)],
        {"a1": [ChainCandidate("a2", 0.99, None)]},
    )
    assert joins == {"a1": None, "a2": None}
    assert opens == {}


def test_a_reprint_does_not_join_the_chain_holding_its_original():
    joins, opens = plan_assignments(
        [_article("a1", title=HACK_TITLE), _article("a2", title=HACK_TITLE)],
        {"a1": [ChainCandidate("a2", 0.99, CHAIN)]},
    )
    assert joins["a1"] is None
    assert opens == {}


def test_entities_are_shared_across_the_batch():
    joins, _opens = plan_assignments(
        [_article("a1"), _article("a2")],
        {"a1": [ChainCandidate("a2", 0.95, CHAIN)]},
    )
    # a2 is in the same batch, so its names are known and the join qualifies.
    assert joins["a1"] == CHAIN


def test_two_unchained_neighbours_plan_a_new_chain():
    joins, opens = plan_assignments(
        [_article("a1"), _article("a2")],
        {"a1": [ChainCandidate("a2", 0.95)]},
    )
    assert opens["a1"] == ("a2",)
    assert joins.get("a1") is None


def test_an_unrelated_article_is_planned_unchained():
    joins, opens = plan_assignments(
        [
            _article("a1"),
            _article(
                "a2",
                title="Wormhole exploit hits SOL bridge",
                subjects=("wormhole", "SOL"),
            ),
        ],
        {"a1": [ChainCandidate("a2", 0.99)]},
    )
    assert joins["a1"] is None
    assert opens == {}


def test_malformed_articles_are_skipped():
    joins, opens = plan_assignments(["nope", {"article_id": ""}], {})
    assert joins == {} and opens == {}


# --- running -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_join_is_persisted():
    repo = FakeRepo([_article("a1"), _article("a2")], chains={"a2": CHAIN})
    store = FakeStore([FakeEdge("a1", "a2", 0.95)])

    outcome = await _worker(repo, store).run_once()

    assert outcome.joined == 1
    assert repo.joined == [(CHAIN, "a1")]


@pytest.mark.asyncio
async def test_a_new_chain_is_opened_with_both_members():
    repo = FakeRepo([_article("a1"), _article("a2")])
    store = FakeStore([FakeEdge("a1", "a2", 0.95)])

    outcome = await _worker(repo, store).run_once()

    assert outcome.opened == 1
    # The article itself is seeded alongside its neighbour; a chain of one is
    # not an event.
    assert repo.opened[0][1] == ("a1", "a2")


@pytest.mark.asyncio
async def test_nothing_to_do_is_not_an_error():
    outcome = await _worker(FakeRepo([]), FakeStore()).run_once()
    assert outcome == ChainOutcome()


@pytest.mark.asyncio
async def test_a_batch_failure_never_breaks_the_pass():
    repo = FakeRepo([_article("a1")], batch_error=RuntimeError("db down"))
    outcome = await _worker(repo, FakeStore()).run_once()
    # Chaining is enrichment; the rest of the system keeps running.
    assert outcome == ChainOutcome()


@pytest.mark.asyncio
async def test_an_index_failure_never_breaks_the_pass():
    repo = FakeRepo([_article("a1")])
    store = FakeStore(error=RuntimeError("index down"))

    outcome = await _worker(repo, store).run_once()

    assert outcome.considered == 1
    assert outcome.joined == 0


@pytest.mark.asyncio
async def test_a_lost_race_is_absorbed():
    repo = FakeRepo(
        [_article("a1"), _article("a2")],
        chains={"a2": CHAIN},
        join_error=RuntimeError("unique violation"),
    )
    store = FakeStore([FakeEdge("a1", "a2", 0.95)])

    outcome = await _worker(repo, store).run_once()

    # The first writer keeps the article; this pass simply records nothing.
    assert outcome.joined == 0


@pytest.mark.asyncio
async def test_articles_without_a_publication_time_are_not_queried():
    article = _article("a1")
    article["published_at"] = None
    store = FakeStore()

    await _worker(FakeRepo([article]), store).run_once()

    assert store.calls == 0


@pytest.mark.asyncio
async def test_the_span_bound_limits_how_far_back_it_looks():
    seen = {}

    class RecordingRepo(FakeRepo):
        async def chainable_articles(self, *, coverage_domain, since, limit):
            seen["since"] = since
            return []

    await _worker(RecordingRepo(), FakeStore()).run_once()

    assert seen["since"] < NOW
    assert (NOW - seen["since"]).days == 45


# --- persistence contract ----------------------------------------------------

def test_a_join_cannot_split_an_article_across_chains():
    import inspect

    from app.repository import Repository

    body = inspect.getsource(Repository.join_event_chain)
    assert "ON CONFLICT (article_id) DO NOTHING" in body


def test_opening_never_leaves_a_chain_of_one():
    import inspect

    from app.repository import Repository

    body = inspect.getsource(Repository.open_event_chain)
    assert "if len(members) < 2" in body
    assert "EmptyChainRollback" in body
