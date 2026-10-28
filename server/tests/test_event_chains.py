"""Deciding that two reports are the same incident (I-13209-a, PP-4).

A false join is worse than no join: it makes one article appear to correct
another it has nothing to do with. So similarity never joins on its own, and
almost every test here asserts a refusal.
"""
from __future__ import annotations

import pytest

from app.event_chains import (
    JOIN_SIMILARITY,
    MAX_NEIGHBOURS,
    MIN_SHARED_ENTITIES,
    ChainCandidate,
    article_entities,
    chain_decision,
    deduplicate_neighbours,
    shared_entity_count,
    should_open_chain,
    story_key,
)


CHAIN = "11111111-1111-1111-1111-111111111111"
OTHER_CHAIN = "22222222-2222-2222-2222-222222222222"
HACK = frozenset({"RONIN", "AXIE", "ETH"})


def _neighbour(article_id="a-1", similarity=0.95, chain_id=None) -> ChainCandidate:
    return ChainCandidate(
        article_id=article_id, similarity=similarity, chain_id=chain_id
    )


def _decide(neighbours, *, entities=HACK, neighbour_entities=None):
    return chain_decision(
        neighbours,
        entities=entities,
        neighbour_entities=neighbour_entities
        if neighbour_entities is not None
        else {n.article_id: HACK for n in neighbours},
    )


# --- what identifies an incident ---------------------------------------------

def test_entities_come_from_verified_subjects_and_title_names():
    entities = article_entities(
        title="Ronin bridge exploit drains ETH from Axie treasury",
        tagged_atoms=[{"subject_id": "ronin"}, {"subject_id": "ETH"}],
    )
    assert {"RONIN", "ETH", "AXIE"} <= entities


def test_generic_capitalised_words_identify_nothing():
    entities = article_entities(title="The Exchange Said Monday That Crypto Fell")
    # Title Case capitalises everything, so capitalisation says nothing about
    # which words are names; counting them would inflate the overlap the join
    # gate depends on.
    assert entities == frozenset()


def test_title_case_still_yields_acronyms_and_tagged_subjects():
    entities = article_entities(
        title="SEC Files Against Ripple Over XRP Sales",
        tagged_atoms=[{"subject_id": "ripple"}],
    )
    # These are unambiguous regardless of house style.
    assert entities == frozenset({"SEC", "XRP", "RIPPLE"})


def test_an_article_with_no_names_has_no_entities():
    assert article_entities(title="prices moved again") == frozenset()
    assert article_entities() == frozenset()


def test_shared_entity_count_is_an_intersection():
    assert shared_entity_count(HACK, frozenset({"RONIN", "ETH"})) == 2
    assert shared_entity_count(HACK, frozenset({"SOLANA"})) == 0


# --- joining an existing chain -----------------------------------------------

def test_a_qualified_neighbour_joins_its_chain():
    assert _decide([_neighbour(chain_id=CHAIN)]) == CHAIN


def test_similarity_below_the_bar_never_joins():
    assert _decide([_neighbour(similarity=JOIN_SIMILARITY - 0.01, chain_id=CHAIN)]) is None


def test_the_similarity_boundary_joins():
    assert _decide([_neighbour(similarity=JOIN_SIMILARITY, chain_id=CHAIN)]) == CHAIN


def test_similarity_alone_is_not_enough():
    # Embeddings rate "another bridge exploit" as near-identical to a
    # different bridge exploit; the names are what separate them.
    assert _decide(
        [_neighbour(similarity=0.99, chain_id=CHAIN)],
        neighbour_entities={"a-1": frozenset({"WORMHOLE", "SOL"})},
    ) is None


def test_one_shared_name_is_not_enough():
    assert MIN_SHARED_ENTITIES == 2
    assert _decide(
        [_neighbour(chain_id=CHAIN)],
        neighbour_entities={"a-1": frozenset({"ETH"})},
    ) is None


def test_an_article_without_entities_never_joins():
    assert _decide([_neighbour(chain_id=CHAIN)], entities=frozenset()) is None


def test_two_candidate_chains_join_neither():
    # At least one of them is wrong, and merging them is the failure this
    # unit exists to avoid.
    neighbours = [
        _neighbour("a-1", chain_id=CHAIN),
        _neighbour("a-2", chain_id=OTHER_CHAIN),
    ]
    assert _decide(neighbours) is None


def test_several_neighbours_in_one_chain_still_join_it():
    neighbours = [_neighbour("a-1", chain_id=CHAIN), _neighbour("a-2", chain_id=CHAIN)]
    assert _decide(neighbours) == CHAIN


def test_only_the_nearest_neighbours_are_considered():
    filler = [
        _neighbour(f"f-{index}", similarity=0.99) for index in range(MAX_NEIGHBOURS)
    ]
    late = _neighbour("late", chain_id=CHAIN)
    assert _decide([*filler, late]) is None


def test_no_neighbours_means_no_chain():
    assert _decide([]) is None


def test_unchained_neighbours_alone_do_not_pick_a_chain():
    # There is nothing to join yet; opening one is a separate decision.
    assert _decide([_neighbour(), _neighbour("a-2")]) is None


# --- opening a new chain -----------------------------------------------------

def test_a_new_chain_opens_around_qualified_unchained_neighbours():
    members = should_open_chain(
        [_neighbour("a-1"), _neighbour("a-2")],
        entities=HACK,
        neighbour_entities={"a-1": HACK, "a-2": HACK},
    )
    assert members == ("a-1", "a-2")


def test_a_lone_report_opens_nothing():
    # One article is not an event.
    assert should_open_chain([], entities=HACK, neighbour_entities={}) == ()


def test_unqualified_neighbours_do_not_open_a_chain():
    assert should_open_chain(
        [_neighbour("a-1", similarity=0.5)],
        entities=HACK,
        neighbour_entities={"a-1": HACK},
    ) == ()
    assert should_open_chain(
        [_neighbour("a-1")],
        entities=HACK,
        neighbour_entities={"a-1": frozenset({"ETH"})},
    ) == ()


def test_an_existing_chain_in_range_blocks_opening_a_competing_one():
    members = should_open_chain(
        [_neighbour("a-1", chain_id=CHAIN), _neighbour("a-2")],
        entities=HACK,
        neighbour_entities={"a-1": HACK, "a-2": HACK},
    )
    assert members == ()


def test_opening_requires_entities():
    assert should_open_chain(
        [_neighbour("a-1")], entities=frozenset(), neighbour_entities={"a-1": HACK}
    ) == ()


def test_members_are_deduplicated():
    members = should_open_chain(
        [_neighbour("a-1"), _neighbour("a-1")],
        entities=HACK,
        neighbour_entities={"a-1": HACK},
    )
    assert members == ("a-1",)


# --- syndicated reprints (DR-13233) -------------------------------------------

def _reprints(count: int, *, chain_id=None) -> tuple[list[ChainCandidate], dict]:
    """`count` neighbours that are all the same story republished."""
    neighbours = [
        _neighbour(f"copy-{index}", chain_id=chain_id) for index in range(count)
    ]
    keys = {candidate.article_id: "japan bitcoin" for candidate in neighbours}
    return neighbours, keys


def test_story_key_folds_case_and_punctuation():
    # The same fold triage uses for its duplicate penalty, so the two can never
    # disagree about what "the same headline" means.
    assert story_key("Diese Entwicklung in Japan könnte Bitcoin belasten") == story_key(
        "  diese entwicklung in japan könnte bitcoin belasten !  "
    )


def test_syndicated_reprints_never_form_a_chain():
    neighbours, keys = _reprints(4)
    kept, reprint_chains = deduplicate_neighbours(
        neighbours, story_key="japan bitcoin", neighbour_story_keys=keys
    )
    # Four outlets republishing one article is one report, not five.
    assert kept == ()
    assert reprint_chains == frozenset()
    assert _decide(kept) is None
    assert should_open_chain(kept, entities=HACK, neighbour_entities={}) == ()


def test_reprint_does_not_join_its_originals_chain():
    neighbours, keys = _reprints(1, chain_id=CHAIN)
    kept, reprint_chains = deduplicate_neighbours(
        neighbours, story_key="japan bitcoin", neighbour_story_keys=keys
    )
    # Joining would put one story into the same chain twice.
    assert kept == ()
    assert reprint_chains == frozenset({CHAIN})


def test_follow_up_still_joins_past_a_reprint():
    reprint = _neighbour("copy-0", chain_id=CHAIN)
    follow_up = _neighbour("follow-up", chain_id=CHAIN)
    kept, reprint_chains = deduplicate_neighbours(
        [reprint, follow_up],
        story_key="japan bitcoin",
        neighbour_story_keys={
            "copy-0": "japan bitcoin",
            "follow-up": "japan bitcoin fallout widens",
        },
    )
    assert [candidate.article_id for candidate in kept] == ["follow-up"]
    # The reprint is in the chain, so this story is already there.
    assert reprint_chains == frozenset({CHAIN})


def test_opened_chain_keeps_one_copy_per_headline():
    neighbours = [_neighbour(f"copy-{index}") for index in range(3)]
    keys = {candidate.article_id: "japan bitcoin" for candidate in neighbours}
    kept, _ = deduplicate_neighbours(
        neighbours, story_key="fallout widens", neighbour_story_keys=keys
    )
    members = should_open_chain(
        kept, entities=HACK, neighbour_entities={n.article_id: HACK for n in kept}
    )
    # Three copies of one story contribute one member, not three.
    assert members == ("copy-0",)


def test_untitled_article_is_not_deduplicated():
    neighbour = _neighbour("a-1")
    kept, reprint_chains = deduplicate_neighbours(
        [neighbour], story_key="", neighbour_story_keys={"a-1": ""}
    )
    # Nothing to compare, so the neighbour is judged on its merits.
    assert kept == (neighbour,)
    assert reprint_chains == frozenset()


# --- schema ------------------------------------------------------------------

def test_an_article_can_belong_to_only_one_chain():
    from app.db import SCHEMA_SQL

    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_chain_member_article" in SCHEMA_SQL
    assert "ON event_chain_members (article_id)" in SCHEMA_SQL


def test_chain_tables_are_required():
    from app.db_bootstrap import REQUIRED_TABLES

    assert {"event_chains", "event_chain_members"} <= set(REQUIRED_TABLES)


@pytest.mark.parametrize("constant", [JOIN_SIMILARITY, MIN_SHARED_ENTITIES])
def test_thresholds_are_stated_not_implied(constant):
    assert constant
