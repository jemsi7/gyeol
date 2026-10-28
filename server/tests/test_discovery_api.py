from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import app, state
from app.app_service import AppClientError, AuthenticatedClient
from app.article_vector_store import ArticleVectorHit
from app.config import Settings
from app.trending_search import TrendingMetric, TrendingSearchSnapshot


KEY_ID = "apple-app-attest-key-id"
INSTALLATION_ID = "11111111-1111-1111-1111-111111111111"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _device():
    return {
        "installation_id": INSTALLATION_ID,
        "vendor_id": None,
        "platform": "ios",
        "model": "iPhone18,1",
        "os_version": "26.0",
        "app_version": "1.0",
        "build_number": "1",
        "locale": "ko-KR",
    }


def _map(index: int, scene_count: int = 4):
    return {
        "format_version": 1,
        "language": "ko",
        "headline": f"지속가치 기사 {index}",
        "subheadline": "",
        "summary": "구조 변화와 근거를 설명하는 기사 요약입니다.",
        "keywords": ["인공지능", "인프라", "근거"],
        "body_markdown": "첫 문단\n둘째 문단",
        "image_placements": [],
        "images": [],
        "scenes": [{"id": f"S-{value + 1:02d}"} for value in range(scene_count)],
    }


def _article_row(index: int, *, scene_count: int = 4, catalog: bool = False):
    row = {
        "article_id": f"{index:064x}",
        "source_url": f"https://news.example/{index}",
        "domain": f"source-{index % 4}.example",
        "published_at": datetime(2026, 7, 21, 1, tzinfo=UTC) - timedelta(minutes=index),
        "source_language": "Korean",
        "sourcecountry": "South Korea",
        "data": _map(index, scene_count),
    }
    row["map_status" if catalog else "status"] = "ok"
    return row


def _catalog():
    roles = ("start", "context", "evidence", "counterpoint")
    routes = []
    for route_index in range(2):
        routes.append(
            {
                "id": UUID(f"00000000-0000-0000-0000-{route_index + 1:012d}"),
                "rank": route_index + 1,
                "slug": f"durable-route-{route_index + 1}",
                "title": f"AI 인프라를 네 번에 이해하기 {route_index + 1}",
                "summary": "사실과 영향, 근거와 반론을 순서대로 읽는 자동 경로입니다.",
                "topic": f"AI 인프라 {route_index + 1}",
                "is_representative": route_index == 0,
                "items": [
                    {
                        **_article_row(route_index * 4 + item, catalog=True),
                        "position": item + 1,
                        "role": roles[item],
                        "reason": "앞뒤 기사와 연결되는 근거가 충분한 선정 이유입니다.",
                        "lasting_value": "며칠 뒤에도 구조 변화를 이해하는 근거로 남는 기사입니다.",
                        "quality": {},
                    }
                    for item in range(4)
                ],
            }
        )
    return {
        "id": uuid4(),
        "generated_at": datetime(2026, 7, 21, 1, tzinfo=UTC),
        "valid_until": datetime(2026, 7, 22, 13, tzinfo=UTC),
        "search_suggestions": ["AI 인프라", "전력과 데이터센터", "반도체 공급망"],
        "routes": routes,
    }


def _timeline_catalog(*, status: str = "fresh"):
    generated_at = datetime(2026, 8, 2, 3, tzinfo=UTC)
    timelines = []
    for rank in range(1, 5):
        timeline_id = UUID(f"00000000-0000-4000-8000-{200 + rank:012d}")
        node_ids = [
            UUID(f"00000000-0000-4000-8000-{rank * 10 + index:012d}")
            for index in range(1, 5)
        ]
        nodes = []
        for index in range(1, 5):
            nodes.append(
                {
                    **_article_row(200 + rank * 10 + index, catalog=True),
                    "node_id": node_ids[index - 1],
                    "anchor_node_id": None if index <= 3 else node_ids[0],
                    "lane": "main" if index <= 3 else "branch",
                    "display_order": index,
                    "update_kind": "occurred" if index == 1 else "existing",
                    "relevance_score": 1 - index / 10,
                    "published_at_snapshot": generated_at - timedelta(hours=index),
                    "date_source": "provider",
                }
            )
        timelines.append(
            {
                "id": timeline_id,
                "rank": rank,
                "stable_key": f"stable-{rank}",
                "cluster_key": f"cluster-{rank}",
                "title": f"주요 사건 흐름 {rank}",
                "summary": "주요 사건을 발행 시각과 인과 흐름으로 정리한 타임라인입니다.",
                "topic": f"주제 {rank}",
                "started_at": generated_at - timedelta(days=4),
                "ended_at": generated_at,
                "nodes": nodes,
            }
        )
    return {
        "id": uuid4(),
        "generated_at": generated_at,
        "updated_at": generated_at + timedelta(minutes=1),
        "valid_until": generated_at + timedelta(hours=36),
        "timeline_status": status,
        "timelines": timelines,
    }


class FakeDiscoveryService:
    def __init__(self):
        self.calls = []
        self.error = None

    async def authenticate_discovery(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return AuthenticatedClient(
            key_id=kwargs["key_id"],
            installation_id=INSTALLATION_ID,
            device=kwargs["payload"]["device"],
        )


class FakeDiscoveryRepo:
    def __init__(self, catalog=None, search_rows=None):
        self.catalog = catalog
        self.search_rows = search_rows or []
        self.editorial_rows = []
        self.search_calls = []
        self.editorial_calls = []
        self.trending_calls = []
        self.trending_error = None
        self.rag_rows = []
        self.rag_error = None
        self.rag_calls = []
        self.events = []
        self.article_list_impression_calls = []
        self.article_list_impression_error = None
        self.guest_activity_calls = []
        self.timeline_catalog = _timeline_catalog()
        self.timeline_error = None
        self.timeline_calls = []
        self.timeline_search_rows = []
        self.timeline_search_error = None
        self.timeline_search_calls = []

    async def record_guest_session_activity(self, **kwargs):
        self.guest_activity_calls.append(kwargs)

    async def record_article_list_impressions(self, **kwargs):
        self.article_list_impression_calls.append(kwargs)
        if self.article_list_impression_error:
            raise self.article_list_impression_error
        return len(kwargs["article_ids"])

    async def current_curation_catalog(self):
        return self.catalog

    async def current_explore_timeline_catalog(self, **kwargs):
        self.timeline_calls.append(kwargs)
        if self.timeline_error is not None:
            raise self.timeline_error
        return self.timeline_catalog

    async def search_explore_timelines(self, **kwargs):
        self.timeline_search_calls.append(kwargs)
        if self.timeline_search_error is not None:
            raise self.timeline_search_error
        return self.timeline_search_rows

    async def search_article_maps(self, **kwargs):
        self.events.append("keyword")
        self.search_calls.append(kwargs)
        return self.search_rows

    async def published_article_maps_by_ids(self, **kwargs):
        self.events.append("hydrate")
        self.rag_calls.append(kwargs)
        if self.rag_error is not None:
            raise self.rag_error
        return self.rag_rows

    async def published_editorial_maps(self, **kwargs):
        self.editorial_calls.append(kwargs)
        return self.editorial_rows

    async def current_trending_search_snapshot(self, **kwargs):
        self.trending_calls.append(kwargs)
        if self.trending_error is not None:
            raise self.trending_error
        generated_at = datetime(2026, 8, 1, 3, 10, 1, tzinfo=UTC)
        return TrendingSearchSnapshot(
            bucket_started_at=datetime(2026, 8, 1, 3, 10, tzinfo=UTC),
            category_keywords={
                TrendingMetric.VIEW_TOTAL: ("AI 인프라",),
            },
            keyword_pool=("AI 인프라", "반도체 공급망", "데이터센터 전력"),
            suggestions=("AI 인프라", "반도체 공급망", "데이터센터 전력"),
            generated_at=generated_at,
        )


class FakeArticleEmbeddingWorker:
    def __init__(self, repo: FakeDiscoveryRepo):
        self.repo = repo
        self.query_vector = None
        self.hits = []
        self.embed_error = None
        self.vector_error = None
        self.embed_calls = []
        self.nearest_calls = []

    async def embed_search_query(self, query):
        self.repo.events.append("embed")
        self.embed_calls.append(query)
        if self.embed_error is not None:
            raise self.embed_error
        return self.query_vector

    async def nearest_search_articles(self, embedding, *, exclude_ids):
        self.repo.events.append("nearest")
        self.nearest_calls.append((embedding, list(exclude_ids)))
        if self.vector_error is not None:
            raise self.vector_error
        return self.hits


@pytest.fixture
def configured_discovery_state():
    service = FakeDiscoveryService()
    repo = FakeDiscoveryRepo(_catalog())
    state.settings = Settings(
        _env_file=None,
        APP_ATTEST_ENABLED=True,
        APP_ATTEST_APP_ID_PREFIX="TEAM123456",
        APP_ATTEST_BUNDLE_ID="com.example.news",
        PUBLIC_BASE_URL="https://api.example",
    )
    state.app_client_service = service
    state.repo = repo
    state.article_embedding_worker = FakeArticleEmbeddingWorker(repo)
    return service, repo


def _request(query=None, limit=12, *, collection=None, cursor=None):
    return {
        "payload": {
            "device": _device(),
            "query": query,
            "collection": collection,
            "cursor": cursor,
            "limit": limit,
        },
        "auth": {
            "challenge_id": str(uuid4()),
            "key_id": KEY_ID,
            "assertion": _b64(b"test-discovery-assertion-object"),
            "issued_at": int(datetime.now(UTC).timestamp()),
        },
    }


@pytest.mark.asyncio
async def test_discovery_returns_representative_and_multi_route_catalog(
    configured_discovery_state,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format_version"] == 1
    assert len(body["routes"]) == 2
    assert body["representative_route_id"] == body["routes"][0]["id"]
    assert [len(route["items"]) for route in body["routes"]] == [4, 4]
    assert len({item["article"]["article_id"] for route in body["routes"] for item in route["items"]}) == 8
    assert body["search_suggestions"][0] == "AI 인프라"
    assert body["trending_search_suggestions"] == [
        "AI 인프라",
        "반도체 공급망",
        "데이터센터 전력",
    ]
    assert body["cooldown_seconds"] == 300
    assert configured_discovery_state[0].calls[0]["query_present"] is False
    assert configured_discovery_state[1].article_list_impression_calls[0]["article_ids"] == [
        item["article"]["article_id"]
        for route in body["routes"]
        for item in route["items"]
    ] + [
        node["article"]["article_id"]
        for timeline in body["featured_timelines"]
        for node in timeline["nodes"]
    ]
    assert body["timeline_status"] == "fresh"
    assert len(body["featured_timelines"]) == 4
    assert body["featured_timelines"][0]["nodes"][0]["update_kind"] == "occurred"


@pytest.mark.asyncio
async def test_discovery_search_returns_matching_historic_timeline_separately(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    repo.timeline_calls.clear()
    repo.timeline_search_rows = [_timeline_catalog()["timelines"][0]]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/app/discovery",
            json=_request("AI 인프라"),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["featured_timelines"] == []
    assert body["timeline_status"] == "unavailable"
    assert len(body["timeline_results"]) == 1
    assert body["timeline_results"][0]["stable_key"] == "stable-1"
    assert body["timeline_results"][0]["title"] == "주요 사건 흐름 1"
    assert repo.timeline_calls == []
    assert repo.timeline_search_calls == [{"query": "AI 인프라", "limit": 8}]
    assert repo.article_list_impression_calls[-1]["article_ids"][-4:] == [
        node["article"]["article_id"]
        for node in body["timeline_results"][0]["nodes"]
    ]


@pytest.mark.asyncio
async def test_discovery_keeps_other_timelines_when_one_loses_its_map(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    catalog = _timeline_catalog()
    # A re-mapped article leaves the first timeline unrenderable; the remaining
    # three editions must still reach Explore instead of blanking the section.
    for node in catalog["timelines"][0]["nodes"][:2]:
        node["data"] = None
    repo.timeline_catalog = catalog
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timeline_status"] == "fresh"
    assert [timeline["stable_key"] for timeline in body["featured_timelines"]] == [
        "stable-2",
        "stable-3",
        "stable-4",
    ]
    assert [timeline["rank"] for timeline in body["featured_timelines"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_discovery_drops_only_the_unrenderable_timeline_node(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    catalog = _timeline_catalog()
    for timeline in catalog["timelines"]:
        node_ids = [node["node_id"] for node in timeline["nodes"]]
        timeline["nodes"].append(
            {
                **timeline["nodes"][0],
                "node_id": UUID(f"00000000-0000-4000-8000-{900 + timeline['rank']:012d}"),
                "anchor_node_id": node_ids[0],
                "lane": "branch",
                "display_order": 5,
            }
        )
    # Only the appended branch has no Map, so its timeline keeps its four nodes.
    catalog["timelines"][0]["nodes"][-1]["data"] = None
    repo.timeline_catalog = catalog
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["featured_timelines"]) == 4
    assert [len(timeline["nodes"]) for timeline in body["featured_timelines"]] == [
        4,
        5,
        5,
        5,
    ]
    dropped = str(UUID("00000000-0000-4000-8000-000000000901"))
    assert dropped not in {
        node["id"] for node in body["featured_timelines"][0]["nodes"]
    }


@pytest.mark.asyncio
async def test_discovery_timeline_read_failure_is_optional(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    repo.timeline_error = RuntimeError("timeline database unavailable")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())

    assert response.status_code == 200, response.text
    assert response.json()["featured_timelines"] == []
    assert response.json()["timeline_status"] == "unavailable"


@pytest.mark.asyncio
async def test_discovery_search_groups_quick_and_deep(configured_discovery_state):
    service, repo = configured_discovery_state
    repo.search_rows = [
        _article_row(20, scene_count=4),
        _article_row(21, scene_count=6),
    ]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/app/discovery", json=_request("  AI   인프라  ", limit=20)
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == "AI 인프라"
    assert [result["depth"] for result in body["results"]] == ["quick", "deep"]
    assert repo.search_calls == [{"query": "AI 인프라", "limit": 20}]
    assert service.calls[0]["query_present"] is True
    assert body["cooldown_seconds"] == 1
    expected_ids = [
        item["article"]["article_id"]
        for route in body["routes"]
        for item in route["items"]
    ] + [item["article"]["article_id"] for item in body["results"]]
    assert repo.article_list_impression_calls[0]["article_ids"] == expected_ids


@pytest.mark.asyncio
async def test_discovery_impression_failure_prevents_success_response(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    repo.article_list_impression_error = RuntimeError("database unavailable")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "discovery_impression_unavailable"


@pytest.mark.asyncio
async def test_c11601_search_embeds_first_and_appends_rag_after_all_keyword_rows(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    keyword_rows = [_article_row(index, scene_count=4 + index % 2) for index in range(20)]
    rag_rows = [_article_row(index + 100, scene_count=4) for index in range(5)]
    worker = state.article_embedding_worker
    worker.query_vector = [0.25] * 768
    worker.hits = [
        ArticleVectorHit(row["article_id"], 0.9 - index * 0.05)
        for index, row in enumerate(rag_rows)
    ]
    repo.search_rows = keyword_rows
    repo.rag_rows = rag_rows

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/app/discovery",
            json=_request("AI 인프라", limit=20),
        )

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert len(results) == 25
    assert [item["article"]["article_id"] for item in results[:20]] == [
        row["article_id"] for row in keyword_rows
    ]
    assert [item["article"]["article_id"] for item in results[20:]] == [
        row["article_id"] for row in rag_rows
    ]
    assert [item["retrieval"] for item in results] == ["keyword"] * 20 + ["rag"] * 5
    assert repo.events == ["embed", "keyword", "nearest", "hydrate"]
    assert worker.nearest_calls[0][1] == [row["article_id"] for row in keyword_rows]
    assert repo.rag_calls == [{"article_ids": [row["article_id"] for row in rag_rows]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["embed", "nearest", "hydrate"])
async def test_c11601_rag_failures_return_keyword_only_200(
    configured_discovery_state,
    failure_stage,
):
    _service, repo = configured_discovery_state
    keyword = _article_row(20, scene_count=4)
    rag = _article_row(120, scene_count=4)
    repo.search_rows = [keyword]
    repo.rag_rows = [rag]
    worker = state.article_embedding_worker
    worker.query_vector = [0.25] * 768
    worker.hits = [ArticleVectorHit(rag["article_id"], 0.8)]
    if failure_stage == "embed":
        worker.embed_error = RuntimeError("provider secret")
    elif failure_stage == "nearest":
        worker.vector_error = RuntimeError("vector dsn secret")
    else:
        repo.rag_error = RuntimeError("main dsn secret")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request("AI"))

    assert response.status_code == 200, response.text
    assert [item["article"]["article_id"] for item in response.json()["results"]] == [
        keyword["article_id"]
    ]


@pytest.mark.asyncio
async def test_c11601_collection_does_not_call_rag(configured_discovery_state):
    _service, repo = configured_discovery_state
    repo.editorial_rows = [{**_article_row(30), "sort_at": datetime.now(UTC)}]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/app/discovery",
            json=_request(collection="editorial"),
        )

    assert response.status_code == 200, response.text
    assert "embed" not in repo.events
    assert "nearest" not in repo.events
    assert repo.rag_calls == []


@pytest.mark.asyncio
async def test_discovery_editorial_returns_published_page_and_opaque_cursor(
    configured_discovery_state,
):
    service, repo = configured_discovery_state
    rows = [
        {
            **_article_row(index, scene_count=4 + index),
            "sort_at": datetime(2026, 7, 22, 11, tzinfo=UTC)
            - timedelta(minutes=index),
        }
        for index in range(3)
    ]
    repo.editorial_rows = rows
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/app/discovery",
            json=_request(limit=2, collection="editorial"),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["collection"] == "editorial"
    assert body["query"] is None
    assert [item["article"]["article_id"] for item in body["results"]] == [
        rows[0]["article_id"],
        rows[1]["article_id"],
    ]
    assert body["next_cursor"]
    assert repo.search_calls == []
    assert repo.editorial_calls == [
        {
            "publication_kind": "editorial",
            "limit": 3,
            "cursor_published_at": None,
            "cursor_article_id": None,
        }
    ]
    assert service.calls[0]["query_present"] is True

    repo.editorial_rows = [rows[2]]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        continuation = await client.post(
            "/v1/app/discovery",
            json=_request(
                limit=2,
                collection="editorial",
                cursor=body["next_cursor"],
            ),
        )
    assert continuation.status_code == 200, continuation.text
    continuation_body = continuation.json()
    assert [item["article"]["article_id"] for item in continuation_body["results"]] == [
        rows[2]["article_id"]
    ]
    assert continuation_body["next_cursor"] is None
    assert repo.editorial_calls[1] == {
        "publication_kind": "editorial",
        "limit": 3,
        "cursor_published_at": rows[1]["sort_at"],
        "cursor_article_id": rows[1]["article_id"],
    }


@pytest.mark.asyncio
async def test_discovery_weekly_insight_returns_collection_page_and_cursor(
    configured_discovery_state,
):
    service, repo = configured_discovery_state
    rows = [
        {
            **_article_row(index + 30, scene_count=4 + index),
            "sort_at": datetime(2026, 7, 22, 10, tzinfo=UTC)
            - timedelta(minutes=index),
        }
        for index in range(3)
    ]
    repo.editorial_rows = rows
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/app/discovery",
            json=_request(limit=2, collection="weekly_insight"),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["collection"] == "weekly_insight"
    assert body["query"] is None
    assert [item["article"]["article_id"] for item in body["results"]] == [
        rows[0]["article_id"],
        rows[1]["article_id"],
    ]
    assert body["next_cursor"]
    assert repo.search_calls == []
    assert repo.editorial_calls == [
        {
            "publication_kind": "weekly_insight",
            "limit": 3,
            "cursor_published_at": None,
            "cursor_article_id": None,
        }
    ]
    assert service.calls[0]["query_present"] is True

    repo.editorial_rows = [rows[2]]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        continuation = await client.post(
            "/v1/app/discovery",
            json=_request(
                limit=2,
                collection="weekly_insight",
                cursor=body["next_cursor"],
            ),
        )
    assert continuation.status_code == 200, continuation.text
    continuation_body = continuation.json()
    assert continuation_body["collection"] == "weekly_insight"
    assert [item["article"]["article_id"] for item in continuation_body["results"]] == [
        rows[2]["article_id"]
    ]
    assert continuation_body["next_cursor"] is None
    assert repo.editorial_calls[1] == {
        "publication_kind": "weekly_insight",
        "limit": 3,
        "cursor_published_at": rows[1]["sort_at"],
        "cursor_article_id": rows[1]["article_id"],
    }


@pytest.mark.asyncio
async def test_discovery_rejects_invalid_collection_cursor_contract(
    configured_discovery_state,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        both = await client.post(
            "/v1/app/discovery",
            json=_request("AI", collection="editorial"),
        )
        cursor_without_collection = await client.post(
            "/v1/app/discovery",
            json=_request(cursor="opaque"),
        )
        damaged_cursor = await client.post(
            "/v1/app/discovery",
            json=_request(collection="editorial", cursor="not-a-cursor"),
        )

    assert both.status_code == 422
    assert cursor_without_collection.status_code == 422
    assert damaged_cursor.status_code == 400
    assert damaged_cursor.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_discovery_empty_catalog_is_success(configured_discovery_state):
    _service, repo = configured_discovery_state
    repo.catalog = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())
    assert response.status_code == 200
    assert response.json()["routes"] == []
    assert response.json()["representative_route_id"] is None


@pytest.mark.asyncio
async def test_discovery_trending_failure_is_empty_and_catalog_still_succeeds(
    configured_discovery_state,
):
    _service, repo = configured_discovery_state
    repo.trending_error = RuntimeError("snapshot unavailable")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/app/discovery", json=_request())

    assert response.status_code == 200
    assert len(response.json()["routes"]) == 2
    assert response.json()["trending_search_suggestions"] == []


@pytest.mark.asyncio
async def test_discovery_cooldown_and_validation_use_stable_envelopes(
    configured_discovery_state,
):
    service, _repo = configured_discovery_state
    service.error = AppClientError(
        429, "discovery_cooldown", retry_after_seconds=0.75
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        cooldown = await client.post("/v1/app/discovery", json=_request("AI"))
        invalid = await client.post("/v1/app/discovery", json=_request("가"))
    assert cooldown.status_code == 429
    assert cooldown.headers["Retry-After"] == "0.750"
    assert cooldown.json()["error"]["code"] == "discovery_cooldown"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"
