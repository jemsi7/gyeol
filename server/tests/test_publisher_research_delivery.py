"""Research wired into the Map pipeline (I-13212-f, DR-13247, DR-13248).

Two properties matter more than the happy path. Nothing here may cost money it
was not authorised to spend -- so an unreadable spend ledger blocks research
rather than allowing it -- and nothing here may fail a Map, so every error ends
as an article with no sentence.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.editorial_pipeline import EditorialPipeline
from app.map_loader import resolve_map
from app.publisher_research import InterestEvidence, InterestFinding


SIGNALS = {"reasons": ["wire_domain"], "axes": {"slop_risk": 2}}
FINDING = InterestFinding(
    relation="exchange_listed",
    owner="예시거래소",
    asset="BTC",
    confidence="likely",
    evidence=(InterestEvidence(url="https://x.test/listings", quote="lists BTC"),),
)


class _Repo:
    def __init__(self, *, signals=SIGNALS, cached=None, spent=0, count_error=None):
        self.signals = signals
        self.cached = cached
        self.spent = spent
        self.count_error = count_error
        self.written: list[dict] = []
        self.events: list[dict] = []

    async def read_triage_signals(self, article_id):
        return self.signals

    async def read_publisher_interest(self, domain, assets_key, *, max_age_days=30):
        return self.cached

    async def count_publisher_research_runs(self, editorial_day):
        if self.count_error is not None:
            raise self.count_error
        assert isinstance(editorial_day, date)
        return self.spent

    async def write_publisher_interest(self, entry):
        self.written.append(dict(entry))

    async def append_map_attempt_event(self, article_id, **kwargs):
        self.events.append({"article_id": article_id, **kwargs})


class _Researcher:
    def __init__(self, finding=FINDING, error=None):
        self.finding = finding
        self.error = error
        self.calls: list[dict] = []

    async def research(self, *, domain, title, subject_assets):
        self.calls.append(
            {"domain": domain, "title": title, "assets": set(subject_assets)}
        )
        if self.error is not None:
            raise self.error
        return self.finding


def _settings(**values) -> Settings:
    values.setdefault("PUBLISHER_RESEARCH_MODE", "on")
    return Settings(_env_file=None, OPENROUTER_API_KEY="test", **values)


def _pipeline(repo, researcher=None, **values) -> EditorialPipeline:
    pipeline = EditorialPipeline(_settings(**values), repo, llm=None)
    if researcher is not None or values.get("PUBLISHER_RESEARCH_MODE") == "off":
        pipeline.publisher_researcher = researcher
    return pipeline


async def _run(pipeline, *, assets=("BTC",), domain="news.test"):
    return await pipeline._research_publisher_context(
        "article-1",
        domain=domain,
        title="Example lists an asset",
        subject_assets=frozenset(assets),
    )


@pytest.mark.asyncio
async def test_on_mode_displays_the_hedged_sentence():
    repo, researcher = _Repo(), _Researcher()
    context = await _run(_pipeline(repo, researcher))
    assert context is not None
    assert "공개된 자료 기준" in context.sentence
    assert context.basis == ("researched_exchange_listed",)
    assert len(researcher.calls) == 1


@pytest.mark.asyncio
async def test_mode_off_builds_no_researcher_at_all():
    pipeline = EditorialPipeline(
        _settings(PUBLISHER_RESEARCH_MODE="off"), _Repo(), llm=None
    )
    assert pipeline.publisher_researcher is None
    assert await _run(pipeline) is None


@pytest.mark.asyncio
async def test_shadow_stores_without_displaying():
    repo, researcher = _Repo(), _Researcher()
    context = await _run(_pipeline(repo, researcher, PUBLISHER_RESEARCH_MODE="shadow"))
    assert context is None
    assert len(repo.written) == 1
    # The receipt still carries the sentence a reader would have seen.
    assert "공개된 자료 기준" in repo.written[0]["sentence"]
    assert repo.written[0]["evidence"] == [
        {"url": "https://x.test/listings", "quote": "lists BTC"}
    ]


@pytest.mark.asyncio
async def test_gate_failure_skips_research():
    repo, researcher = _Repo(signals={"reasons": [], "axes": {"slop_risk": 1}}), _Researcher()
    assert await _run(_pipeline(repo, researcher)) is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_roundup_skips_research():
    repo, researcher = _Repo(), _Researcher()
    assert await _run(_pipeline(repo, researcher), assets=("BTC", "ETH", "SOL", "XRP")) is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_registered_publisher_skips_research():
    repo, researcher = _Repo(), _Researcher()
    # coinbase.com is in the shipped registry, so the factual axis owns it.
    assert await _run(_pipeline(repo, researcher), domain="coinbase.com") is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_cache_hit_skips_research_but_still_displays():
    cached = {
        "relation": "exchange_listed",
        "owner": "예시거래소",
        "asset": "BTC",
        "confidence": "likely",
        "evidence": [{"url": "https://x.test/listings", "quote": "lists BTC"}],
    }
    repo, researcher = _Repo(cached=cached), _Researcher()
    context = await _run(_pipeline(repo, researcher))
    assert context is not None and "예시거래소" in context.sentence
    assert researcher.calls == []
    assert repo.written == []


@pytest.mark.asyncio
async def test_cached_none_verdict_stays_silent():
    repo, researcher = _Repo(cached={"relation": "none", "confidence": "likely"}), _Researcher()
    assert await _run(_pipeline(repo, researcher)) is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_daily_limit_blocks_research():
    repo, researcher = _Repo(spent=20), _Researcher()
    assert await _run(_pipeline(repo, researcher)) is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_unreadable_spend_ledger_blocks_research():
    """Fail-closed on cost: unknown spend is treated as spent (DR-13247)."""
    repo, researcher = _Repo(count_error=RuntimeError("db down")), _Researcher()
    assert await _run(_pipeline(repo, researcher)) is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_research_failure_is_silent_not_fatal():
    repo, researcher = _Repo(), _Researcher(error=RuntimeError("provider down"))
    assert await _run(_pipeline(repo, researcher)) is None
    assert any(
        event.get("error_code") == "research_unavailable" for event in repo.events
    )


@pytest.mark.asyncio
async def test_inconclusive_research_is_recorded_and_silent():
    repo, researcher = _Repo(), _Researcher(finding=None)
    assert await _run(_pipeline(repo, researcher)) is None
    assert repo.written == []
    assert any(
        event.get("error_code") == "research_inconclusive" for event in repo.events
    )


@pytest.mark.asyncio
async def test_none_verdict_is_stored_so_it_is_not_researched_twice():
    finding = InterestFinding(relation="none", owner="", asset="", confidence="likely")
    repo, researcher = _Repo(), _Researcher(finding=finding)
    assert await _run(_pipeline(repo, researcher)) is None
    assert repo.written[0]["relation"] == "none"
    assert repo.written[0]["sentence"] == ""


@pytest.mark.asyncio
async def test_uncertain_verdict_is_stored_but_not_shown():
    finding = InterestFinding(
        relation="exchange_listed",
        owner="예시거래소",
        asset="BTC",
        confidence="uncertain",
        evidence=(InterestEvidence(url="https://x.test/a", quote="q"),),
    )
    repo, researcher = _Repo(), _Researcher(finding=finding)
    assert await _run(_pipeline(repo, researcher)) is None
    assert repo.written[0]["confidence"] == "uncertain"


@pytest.mark.asyncio
async def test_a_repo_without_the_new_methods_is_skipped():
    class _Old:
        async def append_map_attempt_event(self, article_id, **kwargs):
            return None

    researcher = _Researcher()
    assert await _run(_pipeline(_Old(), researcher)) is None
    assert researcher.calls == []


@pytest.mark.asyncio
async def test_write_failure_does_not_lose_the_sentence():
    class _Failing(_Repo):
        async def write_publisher_interest(self, entry):
            raise RuntimeError("write failed")

    context = await _run(_pipeline(_Failing(), _Researcher()))
    assert context is not None


def test_researched_basis_is_not_delivered_to_the_app():
    resolved = resolve_map(
        {
            "format_version": 3,
            "headline": "제목",
            "body_markdown": "본문",
            "publisher_context": "예시거래소이(가) 발행했고, 공개된 자료 기준 …",
            "publisher_context_basis": ["researched_exchange_listed"],
        },
        lambda image_id: "",
    )
    assert resolved["publisher_context"].startswith("예시거래소")
    assert "publisher_context_basis" not in resolved


# --- the registry promotion queue on /ops (I-13212-f, DR-13248) -----------


class _OpsRepo:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.limit = None

    async def list_publisher_interest_proposals(self, *, limit=50):
        self.limit = limit
        if self.error is not None:
            raise self.error
        return self.rows


@pytest.fixture
def ops_client(monkeypatch):
    import httpx

    from app.api import app, state

    def build(repo):
        monkeypatch.setattr(state, "repo", repo, raising=False)
        monkeypatch.setattr(
            state,
            "settings",
            _settings(API_TOKEN="ops-secret"),
            raising=False,
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    return build


@pytest.mark.asyncio
async def test_ops_endpoint_lists_proposals_with_evidence(ops_client):
    repo = _OpsRepo(
        [
            {
                "domain": "news.test",
                "assets_key": "BTC",
                "relation": "exchange_listed",
                "owner": "예시거래소",
                "asset": "BTC",
                "confidence": "likely",
                "sentence": "예시거래소이(가) 발행했고, 공개된 자료 기준 …",
                "evidence": '[{"url": "https://x.test/a", "quote": "lists BTC"}]',
                "article_id": "article-1",
                "registry_status": "proposed",
                "researched_at": None,
            }
        ]
    )
    async with ops_client(repo) as client:
        response = await client.get(
            "/ops/api/publisher-interest",
            headers={"X-API-Token": "ops-secret"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "on"
    assert body["daily_limit"] == 20
    proposal = body["proposals"][0]
    assert proposal["domain"] == "news.test"
    assert proposal["evidence"] == [{"url": "https://x.test/a", "quote": "lists BTC"}]


@pytest.mark.asyncio
async def test_ops_endpoint_requires_the_token(ops_client):
    async with ops_client(_OpsRepo()) as client:
        assert (await client.get("/ops/api/publisher-interest")).status_code == 401


@pytest.mark.asyncio
async def test_ops_endpoint_bounds_the_limit(ops_client):
    async with ops_client(_OpsRepo()) as client:
        response = await client.get(
            "/ops/api/publisher-interest?limit=500",
            headers={"X-API-Token": "ops-secret"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_ops_endpoint_hides_database_detail(ops_client):
    repo = _OpsRepo(error=RuntimeError("database password must not leak"))
    async with ops_client(repo) as client:
        response = await client.get(
            "/ops/api/publisher-interest",
            headers={"X-API-Token": "ops-secret"},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "ops_publisher_interest_unavailable"
    assert "password" not in response.text
