from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repository import Repository, repaired_timeline_nodes
from app.db import SCHEMA_SQL


CATALOG_ID = UUID("00000000-0000-4000-8000-000000000100")
TIMELINE_ID = UUID("00000000-0000-4000-8000-000000000200")
NODE_IDS = [
    UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(1, 5)
]
ARTICLE_IDS = [f"article-{index}" for index in range(1, 5)]


def test_timeline_schema_has_unique_candidate_slot_cost_ceiling():
    normalized = " ".join(SCHEMA_SQL.split())
    assert "candidate_slot TEXT NOT NULL UNIQUE" in normalized


class RecordingPool:
    def __init__(self, *, rows=None, row=None):
        self.rows = list(rows or [])
        self.row = row
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


def _repo(pool) -> Repository:
    return Repository(SimpleNamespace(pool=pool))


@pytest.mark.asyncio
async def test_timeline_candidates_are_strict_recent_public_and_bounded():
    pool = RecordingPool()

    assert await _repo(pool).timeline_cluster_candidates(
        since_days=30,
        limit=500,
        day_span=14,
    ) == []

    query, args = pool.calls[0]
    assert "m.status='ok'" in query
    assert "format_version'='3'" in query
    assert "critic_verdict'='pass'" in query
    assert "m.data->>'headline' ~ '[가-힣]'" in query
    assert "p.draft_id IS NULL OR p.state='published'" in query
    assert (
        "ORDER BY kst_day ASC,article_id ASC"
    ) in " ".join(query.split())
    assert "LIMIT $4" in query
    # Each of the day_span most recent KST days contributes an equal quota, so
    # the pool spans dates instead of truncating to the oldest one.
    assert "PARTITION BY kst_day" in query
    assert "day_recency <= $2 AND day_rank <= $3" in " ".join(query.split())
    assert args == (timedelta(days=30), 14, 35, 500)


@pytest.mark.asyncio
async def test_timeline_candidates_spread_the_quota_over_the_day_span():
    pool = RecordingPool()
    repo = _repo(pool)

    await repo.timeline_cluster_candidates(since_days=30, limit=500, day_span=10)
    await repo.timeline_cluster_candidates(since_days=7, limit=64, day_span=7)

    assert pool.calls[0][1] == (timedelta(days=30), 10, 50, 500)
    assert pool.calls[1][1] == (timedelta(days=7), 7, 9, 64)


@pytest.mark.asyncio
async def test_timeline_candidates_reject_unbounded_inputs_before_db():
    pool = RecordingPool()
    repo = _repo(pool)

    with pytest.raises(ValueError):
        await repo.timeline_cluster_candidates(
            since_days=0, limit=500, day_span=14
        )
    with pytest.raises(ValueError):
        await repo.timeline_cluster_candidates(
            since_days=30, limit=501, day_span=14
        )
    with pytest.raises(ValueError):
        await repo.timeline_cluster_candidates(
            since_days=30, limit=63, day_span=14
        )
    with pytest.raises(ValueError):
        await repo.timeline_cluster_candidates(
            since_days=30, limit=500, day_span=2
        )
    with pytest.raises(ValueError):
        await repo.timeline_cluster_candidates(
            since_days=30, limit=500, day_span=31
        )

    assert pool.calls == []


class TimelineClaimConnection:
    def __init__(self, existing):
        self.existing = existing
        self.calls: list[tuple[str, str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "SELECT now()" in query:
            return datetime(2026, 8, 3, 12, tzinfo=UTC)
        return None

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "WHERE input_hash=$1 OR candidate_slot=$2" in query:
            return self.existing
        raise AssertionError(f"unexpected fetchrow query: {query}")


class TimelineClaimPool:
    def __init__(self, existing):
        self.connection = TimelineClaimConnection(existing)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_timeline_claim_same_slot_different_input_is_noop_without_insert():
    existing = {
        "id": CATALOG_ID,
        "input_hash": "a" * 64,
        "candidate_slot": "2026-08-03T12:00:00Z",
        "status": "published",
    }
    pool = TimelineClaimPool(existing)

    claimed = await _repo(pool).claim_explore_timeline_catalog(
        cluster_snapshot_id="snapshot-1",
        cluster_slot="2026-08-03T12:00:00Z",
        candidate_slot="2026-08-03T12:00:00Z",
        input_hash="b" * 64,
        candidate_count=64,
        max_attempts=3,
        lease_s=60,
    )

    assert claimed is None
    queries = [query for _kind, query, _args in pool.connection.calls]
    assert any("input_hash=$1 OR candidate_slot=$2" in query for query in queries)
    assert not any("INSERT INTO explore_timeline_catalogs" in query for query in queries)
    assert not any("status='processing', attempt_count" in query for query in queries)


@pytest.mark.asyncio
async def test_historic_timeline_search_deduplicates_latest_stable_key_and_hydrates_tree():
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    rows = []
    for index in range(1, 5):
        rows.append(
            {
                "timeline_id": TIMELINE_ID,
                "search_rank": 1,
                "stable_key": "stable-1",
                "timeline_title": "AI 인프라 사건 흐름",
                "timeline_summary": "과거 판을 포함한 타임라인입니다.",
                "topic": "AI 인프라",
                "started_at": now - timedelta(days=4),
                "ended_at": now,
                "node_id": NODE_IDS[index - 1],
                "anchor_node_id": None if index <= 3 else NODE_IDS[0],
                "lane": "main" if index <= 3 else "branch",
                "display_order": index,
                "update_kind": "existing",
                "relevance_score": 1 - index / 10,
                "published_at_snapshot": now - timedelta(days=5 - index),
                "date_source": "provider",
                "article_id": ARTICLE_IDS[index - 1],
                "source_title": f"기사 {index}",
                "source_url": f"https://example.test/{index}",
                "domain": "example.test",
                "published_at": now - timedelta(days=5 - index),
                "source_language": "Korean",
                "sourcecountry": "South Korea",
                "map_status": "ok",
                "data": {"format_version": "3"},
            }
        )
    pool = RecordingPool(rows=rows)

    result = await _repo(pool).search_explore_timelines(query="AI 인프라", limit=8)

    assert len(result) == 1
    assert result[0]["stable_key"] == "stable-1"
    assert [node["display_order"] for node in result[0]["nodes"]] == [1, 2, 3, 4]
    query, args = pool.calls[0]
    assert "DISTINCT ON (timeline.stable_key)" in query
    assert "catalog.status IN ('published','superseded')" in query
    assert "ORDER BY timeline.stable_key,catalog.generated_at DESC" in query
    assert "map.data->'editorial'->>'critic_verdict'='pass'" in query
    assert args == ("%ai 인프라%", 8)


@pytest.mark.asyncio
async def test_current_timeline_catalog_revalidates_public_maps_and_groups_tree():
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    header = {
        "id": CATALOG_ID,
        "status": "published",
        "generated_at": now - timedelta(hours=1),
        "valid_until": now + timedelta(hours=1),
        "updated_at": now,
    }
    rows = []
    for rank in range(1, 5):
        timeline_id = UUID(f"00000000-0000-4000-8000-{200 + rank:012d}")
        node_ids = [
            UUID(f"00000000-0000-4000-8000-{rank * 10 + index:012d}")
            for index in range(1, 5)
        ]
        for index in range(1, 5):
            rows.append(
                {
                    "timeline_id": timeline_id,
                    "rank": rank,
                    "stable_key": f"stable-{rank}",
                    "cluster_key": f"cluster-{rank}",
                    "timeline_title": f"주요 사건 흐름 {rank}",
                    "timeline_summary": "주요 사건을 발행 시각과 인과 흐름으로 정리한 타임라인입니다.",
                    "topic": f"주제 {rank}",
                    "started_at": now - timedelta(days=4),
                    "ended_at": now,
                    "node_id": node_ids[index - 1],
                    "anchor_node_id": None if index <= 3 else node_ids[0],
                    "lane": "main" if index <= 3 else "branch",
                    "display_order": index,
                    "update_kind": "existing",
                    "relevance_score": 1 - index / 10,
                    "published_at_snapshot": now - timedelta(days=index),
                    "date_source": "provider",
                    "article_id": f"timeline-{rank}-article-{index}",
                    "source_title": f"기사 {rank}-{index}",
                    "source_url": f"https://news.example/{rank}/{index}",
                    "domain": "news.example",
                    "published_at": now - timedelta(days=index),
                    "source_language": "Korean",
                    "sourcecountry": "South Korea",
                    "map_status": "ok",
                    "data": {"format_version": "3"},
                }
            )
    pool = RecordingPool(rows=rows, row=header)

    catalog = await _repo(pool).current_explore_timeline_catalog(
        now=now,
        stale_hours=72,
    )

    assert catalog is not None
    assert catalog["timeline_status"] == "fresh"
    assert len(catalog["timelines"]) == 4
    assert len(catalog["timelines"][0]["nodes"]) == 4
    header_query = pool.calls[0][0]
    tree_query = pool.calls[1][0]
    assert "valid_until > COALESCE($1::timestamptz, now())-$2::interval" in header_query
    assert "m.status='ok'" in tree_query
    assert "critic_verdict'='pass'" in tree_query
    assert "p.draft_id IS NULL OR p.state='published'" in tree_query


def _catalog_rows(now, *, ranks=4, nodes_per_timeline=6, drop=()):
    """Build reader rows for `ranks` timelines, omitting `drop` (rank, order) nodes."""
    rows = []
    for rank in range(1, ranks + 1):
        timeline_id = UUID(f"00000000-0000-4000-8000-{200 + rank:012d}")
        node_ids = [
            UUID(f"00000000-0000-4000-8000-{rank * 10 + index:012d}")
            for index in range(1, nodes_per_timeline + 1)
        ]
        for index in range(1, nodes_per_timeline + 1):
            if (rank, index) in drop:
                continue
            rows.append(
                {
                    "timeline_id": timeline_id,
                    "rank": rank,
                    "stable_key": f"stable-{rank}",
                    "cluster_key": f"cluster-{rank}",
                    "timeline_title": f"주요 사건 흐름 {rank}",
                    "timeline_summary": "주요 사건을 발행 시각 흐름으로 정리한 타임라인입니다.",
                    "topic": f"주제 {rank}",
                    "started_at": now - timedelta(days=4),
                    "ended_at": now,
                    "node_id": node_ids[index - 1],
                    "anchor_node_id": None if index <= 4 else node_ids[0],
                    "lane": "main" if index <= 4 else "branch",
                    "display_order": index,
                    "update_kind": "existing",
                    "relevance_score": 1 - index / 10,
                    "published_at_snapshot": now - timedelta(days=index),
                    "date_source": "provider",
                    "article_id": f"timeline-{rank}-article-{index}",
                    "source_title": f"기사 {rank}-{index}",
                    "source_url": f"https://news.example/{rank}/{index}",
                    "domain": "news.example",
                    "published_at": now - timedelta(days=index),
                    "source_language": "Korean",
                    "sourcecountry": "South Korea",
                    "map_status": "ok",
                    "data": {"format_version": "3"},
                }
            )
    return rows


@pytest.mark.asyncio
async def test_current_timeline_catalog_renumbers_after_an_article_leaves_the_gate():
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    header = {
        "id": CATALOG_ID,
        "status": "published",
        "generated_at": now - timedelta(hours=1),
        "valid_until": now + timedelta(hours=1),
        "updated_at": now,
    }
    # Rank 2 loses its second node, so the stored display order now has a gap.
    rows = _catalog_rows(now, drop={(2, 2)})
    pool = RecordingPool(rows=rows, row=header)

    catalog = await _repo(pool).current_explore_timeline_catalog(
        now=now, stale_hours=72
    )

    assert catalog is not None
    assert len(catalog["timelines"]) == 4
    assert [timeline["rank"] for timeline in catalog["timelines"]] == [1, 2, 3, 4]
    survivor = catalog["timelines"][1]
    assert [node["display_order"] for node in survivor["nodes"]] == [1, 2, 3, 4, 5]
    assert "timeline-2-article-2" not in {
        node["article_id"] for node in survivor["nodes"]
    }


@pytest.mark.asyncio
async def test_current_timeline_catalog_drops_only_the_broken_timeline():
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    header = {
        "id": CATALOG_ID,
        "status": "published",
        "generated_at": now - timedelta(hours=1),
        "valid_until": now + timedelta(hours=1),
        "updated_at": now,
    }
    # Rank 1 falls to two main nodes: its branches anchored on the removed node 1.
    rows = _catalog_rows(now, drop={(1, 1), (1, 2)})
    pool = RecordingPool(rows=rows, row=header)

    catalog = await _repo(pool).current_explore_timeline_catalog(
        now=now, stale_hours=72
    )

    assert catalog is not None
    assert catalog["published_timeline_count"] == 4
    assert [timeline["stable_key"] for timeline in catalog["timelines"]] == [
        "stable-2",
        "stable-3",
        "stable-4",
    ]
    assert [timeline["rank"] for timeline in catalog["timelines"]] == [1, 2, 3]


def _node(index, *, lane="main", anchor=None):
    return {
        "node_id": NODE_IDS[index - 1] if index <= 4 else UUID(
            f"00000000-0000-4000-8000-{index:012d}"
        ),
        "lane": lane,
        "anchor_node_id": anchor,
        "display_order": index,
    }


def test_repaired_nodes_renumber_survivors_in_place():
    nodes = [
        _node(1),
        _node(3),
        _node(4),
        _node(5, lane="branch", anchor=NODE_IDS[0]),
        _node(6, lane="branch", anchor=NODE_IDS[2]),
    ]

    repaired = repaired_timeline_nodes(nodes)

    assert [node["display_order"] for node in repaired] == [1, 2, 3, 4, 5]
    assert [node["node_id"] for node in repaired] == [
        node["node_id"] for node in nodes
    ]


def test_repaired_nodes_drop_branches_whose_anchor_is_gone():
    missing = UUID("00000000-0000-4000-8000-000000000099")
    nodes = [
        _node(1),
        _node(2),
        _node(3),
        _node(4),
        _node(5, lane="branch", anchor=missing),
    ]

    repaired = repaired_timeline_nodes(nodes)

    assert [node["display_order"] for node in repaired] == [1, 2, 3, 4]
    assert all(node["lane"] == "main" for node in repaired)


def test_repaired_nodes_reject_a_timeline_below_the_published_floor():
    # Two mains plus two orphaned branches is no longer a readable timeline.
    missing = UUID("00000000-0000-4000-8000-000000000099")
    assert repaired_timeline_nodes(
        [
            _node(1),
            _node(2),
            _node(5, lane="branch", anchor=missing),
            _node(6, lane="branch", anchor=missing),
        ]
    ) is None
    # Three mains and one branch is exactly the floor and stays readable.
    assert repaired_timeline_nodes(
        [_node(1), _node(2), _node(3), _node(4, lane="branch", anchor=NODE_IDS[0])]
    ) is not None


def test_repaired_nodes_drop_duplicate_node_identities():
    nodes = [_node(1), _node(1), _node(2), _node(3), _node(4)]

    repaired = repaired_timeline_nodes(nodes)

    assert len(repaired) == 4
    assert [node["display_order"] for node in repaired] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_current_timeline_catalog_marks_grace_catalog_stale():
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    pool = RecordingPool(
        row={
            "id": CATALOG_ID,
            "status": "published",
            "generated_at": now - timedelta(hours=40),
            "valid_until": now - timedelta(hours=4),
            "updated_at": now - timedelta(hours=4),
        },
        rows=[],
    )

    # An incomplete public tree is fail-closed even when the header is in grace.
    assert await _repo(pool).current_explore_timeline_catalog(
        now=now,
        stale_hours=72,
    ) is None


class PublishConnection:
    def __init__(self, *, public_ids: list[str], fail_on_outbox: bool = False):
        self.public_ids = public_ids
        self.fail_on_outbox = fail_on_outbox
        self.calls: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "SELECT * FROM explore_timeline_catalogs" in query:
            return {"id": CATALOG_ID, "status": "processing"}
        if "UPDATE explore_timeline_catalogs" in query and "RETURNING *" in query:
            return {"id": CATALOG_ID, "status": "published"}
        if "INSERT INTO explore_timeline_push_notifications" in query:
            if self.fail_on_outbox:
                raise RuntimeError("timeline outbox failure")
            return {"id": args[0]}
        return None

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if "INSERT INTO explore_timeline_push_deliveries" in query:
            return 2
        return 0

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "SELECT a.id AS article_id" in query:
            return [{"article_id": article_id} for article_id in self.public_ids]
        return []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"


class PublishPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _timeline_payload() -> list[dict]:
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    timelines: list[dict] = []
    for rank in range(1, 5):
        timeline_id = UUID(f"00000000-0000-4000-8000-{200 + rank:012d}")
        node_ids = [
            UUID(f"00000000-0000-4000-8000-{rank * 10 + index:012d}")
            for index in range(1, 5)
        ]
        timelines.append(
            {
                "id": timeline_id,
                "stable_key": f"stable-{rank}",
                "cluster_key": f"cluster-{rank}",
                "rank": rank,
                "title": f"주요 사건 흐름 {rank}",
                "summary": "주요 사건을 발행 시각과 인과 흐름으로 정리한 타임라인입니다.",
                "topic": f"주제 {rank}",
                "started_at": now - timedelta(days=4),
                "ended_at": now,
                "nodes": [
                    {
                        "node_id": node_ids[index - 1],
                        "article_id": f"timeline-{rank}-article-{index}",
                        "anchor_node_id": None if index <= 3 else node_ids[0],
                        "lane": "main" if index <= 3 else "branch",
                        "display_order": index,
                        "update_kind": "existing",
                        "relevance_score": 1 - index / 10,
                        "published_at": now - timedelta(days=index),
                        "date_source": "provider",
                    }
                    for index in range(1, 5)
                ],
            }
        )
    return timelines


@pytest.mark.asyncio
async def test_publish_timeline_catalog_revalidates_every_article_before_inserts():
    expected_ids = [
        node["article_id"]
        for timeline in _timeline_payload()
        for node in timeline["nodes"]
    ]
    connection = PublishConnection(public_ids=expected_ids[:-1])
    repo = _repo(PublishPool(connection))

    with pytest.raises(LookupError, match="public article set changed"):
        await repo.publish_explore_timeline_catalog(
            CATALOG_ID,
            timelines=_timeline_payload(),
            model="cohere/rerank-4-pro",
            valid_until=datetime(2026, 8, 4, tzinfo=UTC),
        )

    assert not any(
        "INSERT INTO explore_timelines" in query for query, _args in connection.calls
    )


@pytest.mark.asyncio
async def test_publish_timeline_outbox_emits_zero_for_initial_existing_nodes():
    payload = _timeline_payload()
    expected_ids = [
        node["article_id"] for timeline in payload for node in timeline["nodes"]
    ]
    connection = PublishConnection(public_ids=expected_ids)

    await _repo(PublishPool(connection)).publish_explore_timeline_catalog(
        CATALOG_ID,
        timelines=payload,
        model="cohere/rerank-4-pro",
        valid_until=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert not any(
        "INSERT INTO explore_timeline_push_notifications" in query
        for query, _ in connection.calls
    )


@pytest.mark.asyncio
async def test_publish_timeline_outbox_emits_kind_once_with_last_display_representative_and_subscribers():
    payload = _timeline_payload()
    payload[0]["nodes"][0]["update_kind"] = "occurred"
    payload[0]["nodes"][1]["update_kind"] = "inserted"
    payload[0]["nodes"][2]["update_kind"] = "inserted"
    expected_ids = [
        node["article_id"] for timeline in payload for node in timeline["nodes"]
    ]
    connection = PublishConnection(public_ids=expected_ids)

    await _repo(PublishPool(connection)).publish_explore_timeline_catalog(
        CATALOG_ID,
        timelines=payload,
        model="cohere/rerank-4-pro",
        valid_until=datetime(2026, 8, 4, tzinfo=UTC),
    )

    notifications = [
        (query, args) for query, args in connection.calls
        if "INSERT INTO explore_timeline_push_notifications" in query
    ]
    assert len(notifications) == 2
    by_kind = {args[5]: args for _, args in notifications}
    assert by_kind["occurred"][4] == payload[0]["nodes"][0]["node_id"]
    assert by_kind["inserted"][4] == payload[0]["nodes"][2]["node_id"]
    fanouts = [
        (query, args) for query, args in connection.calls
        if "INSERT INTO explore_timeline_push_deliveries" in query
    ]
    assert len(fanouts) == 2
    assert all("app_timeline_subscriptions" in query for query, _ in fanouts)
    assert all("$3='inserted'" in query for query, _ in fanouts)
    assert all("registration.timeline_refreshes_enabled" in query for query, _ in fanouts)
    assert all("$3='occurred'" in query for query, _ in fanouts)
    assert all("registration.timeline_additions_enabled" in query for query, _ in fanouts)
    assert all("session.expires_at > now()" in query for query, _ in fanouts)
    assert {args[2] for _, args in fanouts} == {"inserted", "occurred"}


@pytest.mark.asyncio
async def test_timeline_outbox_failure_prevents_catalog_publication():
    payload = _timeline_payload()
    payload[0]["nodes"][0]["update_kind"] = "occurred"
    expected_ids = [
        node["article_id"] for timeline in payload for node in timeline["nodes"]
    ]
    connection = PublishConnection(public_ids=expected_ids, fail_on_outbox=True)

    with pytest.raises(RuntimeError, match="timeline outbox failure"):
        await _repo(PublishPool(connection)).publish_explore_timeline_catalog(
            CATALOG_ID,
            timelines=payload,
            model="cohere/rerank-4-pro",
            valid_until=datetime(2026, 8, 4, tzinfo=UTC),
        )

    assert not any(
        "SET status='published'" in query for query, _ in connection.calls
    )
