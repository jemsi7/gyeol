"""Operator control of the daily Map budget from /ops."""
from __future__ import annotations

from datetime import UTC, date, datetime
import re

import httpx
import pytest

from app.api import app, state
from app.config import Settings


NOW = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
TODAY = date(2026, 7, 25)


class FakeBudgetRepo:
    def __init__(self):
        self.daily_map_limit = 60
        self.fresh_share_percent = 75
        self.daily_run_count = 1
        self.updates: list[tuple[int, int, int]] = []
        self.domain_updates: list[
            list[tuple[str, int, int | None]] | None
        ] = []
        self.domains: list[dict] = [
            {"coverage_domain": "core", "daily_map_limit": 40,
             "document_map_limit": 0, "article_map_limit": 40,
             "reader_exposed": True, "quarantined": 0, "held_maps": 0,
             "subtypes": {}},
            {"coverage_domain": "crypto", "daily_map_limit": 20,
             "document_map_limit": 7, "article_map_limit": 13,
             "reader_exposed": False, "quarantined": 9, "held_maps": 0,
             "subtypes": {}},
        ]

    async def map_budget_status(
        self, *, default_limit, default_share, default_run_count
    ):
        fresh_quota = self.daily_map_limit * self.fresh_share_percent // 100
        backlog_quota = self.daily_map_limit - fresh_quota
        return {
            "daily_map_limit": self.daily_map_limit,
            "fresh_share_percent": self.fresh_share_percent,
            "daily_run_count": self.daily_run_count,
            "domains": self.domains,
            "updated_at": NOW,
            "source": "db",
            "editorial_day": TODAY,
            "current_run": 1,
            "current_run_allowance": self.daily_map_limit,
            "current_allowance": self.daily_map_limit,
            "current_remaining": max(0, self.daily_map_limit - 12),
            "next_run_at": None,
            "buckets": {
                "fresh": {
                    "quota": fresh_quota,
                    "consumed": 10,
                    "remaining": max(0, fresh_quota - 10),
                },
                "backlog": {
                    "quota": backlog_quota,
                    "consumed": 2,
                    "remaining": max(0, backlog_quota - 2),
                },
            },
            "total_consumed": 12,
            "total_remaining": max(0, self.daily_map_limit - 12),
            "triage": {"scored": 20, "pending": 5},
        }

    async def set_map_budget(
        self, *, daily_map_limit, fresh_share_percent, daily_run_count, domains=None
    ):
        self.updates.append((daily_map_limit, fresh_share_percent, daily_run_count))
        self.domain_updates.append(domains)
        self.daily_map_limit = daily_map_limit
        self.fresh_share_percent = fresh_share_percent
        self.daily_run_count = daily_run_count
        return await self.map_budget_status(
            default_limit=0, default_share=0, default_run_count=1
        )


@pytest.fixture
def budget_state(monkeypatch):
    repo = FakeBudgetRepo()
    monkeypatch.setattr(state, "repo", repo, raising=False)
    monkeypatch.setattr(
        state,
        "settings",
        Settings(_env_file=None, API_TOKEN="ops-secret"),
        raising=False,
    )
    return repo


async def _put(payload, *, token="ops-secret"):
    headers = {"X-API-Token": token} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.put("/ops/api/map-budget", json=payload, headers=headers)


@pytest.mark.asyncio
async def test_budget_update_requires_a_valid_ops_token(budget_state):
    payload = {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": 5}
    missing = await _put(payload, token=None)
    wrong = await _put(payload, token="nope")

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_budget_update_fails_closed_without_a_configured_token(monkeypatch):
    repo = FakeBudgetRepo()
    monkeypatch.setattr(state, "repo", repo, raising=False)
    monkeypatch.setattr(
        state, "settings", Settings(_env_file=None, API_TOKEN=""), raising=False
    )

    response = await _put(
        {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": 5}
    )

    assert response.status_code == 503
    assert repo.updates == []


def test_config_accepts_new_budget_maximum():
    settings = Settings(
        _env_file=None,
        MAP_DAILY_LIMIT_DEFAULT=1000,
        MAP_DAILY_RUN_COUNT_DEFAULT=24,
    )

    assert settings.map_daily_limit_default == 1000
    assert settings.map_daily_run_count_default == 24
    with pytest.raises(ValueError):
        Settings(_env_file=None, MAP_DAILY_LIMIT_DEFAULT=1001)
    with pytest.raises(ValueError):
        Settings(_env_file=None, MAP_DAILY_RUN_COUNT_DEFAULT=25)


@pytest.mark.parametrize(
    "payload",
    [
        {"daily_map_limit": -1, "fresh_share_percent": 60, "daily_run_count": 5},
        {"daily_map_limit": 1001, "fresh_share_percent": 60, "daily_run_count": 5},
        {"daily_map_limit": 40, "fresh_share_percent": 101, "daily_run_count": 5},
        {"daily_map_limit": 40},
        {"daily_map_limit": 40, "fresh_share_percent": 60},
        {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": 0},
        {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": 25},
        {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": "5"},
        {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": 5, "extra": 1},
        {"daily_map_limit": "40", "fresh_share_percent": 60, "daily_run_count": 5},
    ],
)
@pytest.mark.asyncio
async def test_budget_update_rejects_values_outside_the_allowed_range(
    budget_state, payload
):
    response = await _put(payload)

    assert response.status_code == 422
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_budget_update_persists_and_returns_the_new_status(budget_state):
    response = await _put(
        {"daily_map_limit": 40, "fresh_share_percent": 60, "daily_run_count": 5}
    )

    assert response.status_code == 200
    assert budget_state.updates == [(40, 60, 5)]
    body = response.json()
    assert body["daily_map_limit"] == 40
    assert body["fresh_share_percent"] == 60
    assert body["daily_run_count"] == 5
    assert body["buckets"]["fresh"]["quota"] == 24
    assert body["buckets"]["backlog"]["quota"] == 16
    assert body["triage"]["mode"] == "shadow"
    assert body["candidate_ttl_days"] == 3
    assert body["enabled"] is True


@pytest.mark.asyncio
async def test_budget_update_accepts_new_maximum(budget_state):
    response = await _put(
        {"daily_map_limit": 1000, "fresh_share_percent": 75, "daily_run_count": 24}
    )

    assert response.status_code == 200
    assert budget_state.updates == [(1000, 75, 24)]
    assert response.json()["daily_map_limit"] == 1000


@pytest.mark.asyncio
async def test_zero_limit_is_a_valid_full_stop(budget_state):
    response = await _put(
        {"daily_map_limit": 0, "fresh_share_percent": 75, "daily_run_count": 5}
    )

    assert response.status_code == 200
    assert budget_state.updates == [(0, 75, 5)]
    assert response.json()["total_remaining"] == 0


@pytest.mark.asyncio
async def test_snapshot_exposes_the_budget_block(budget_state, monkeypatch):
    class SnapshotRepo(FakeBudgetRepo):
        async def worker_telemetry_rows(self, window_minutes):
            return {"now": NOW, "instances": [], "activities": []}

        async def available_article_count(self):
            return 37

        async def queue_stats(self):
            return {"queued": 1, "due_now": 1}

        async def queue_items(self, limit, status=None):
            return []

        async def push_delivery_stats(self):
            return {}

        async def last_runs(self, limit):
            return []

    monkeypatch.setattr(state, "repo", SnapshotRepo(), raising=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/ops/api/snapshot", headers={"X-API-Token": "ops-secret"}
        )

    assert response.status_code == 200
    assert response.json()["available_articles"] == 37
    budget = response.json()["map_budget"]
    for key in (
        "daily_map_limit",
        "fresh_share_percent",
        "daily_run_count",
        "current_run",
        "current_run_allowance",
        "current_allowance",
        "current_remaining",
        "next_run_at",
        "buckets",
        "total_consumed",
        "total_remaining",
        "triage",
        "enabled",
        "candidate_ttl_days",
    ):
        assert key in budget
    assert budget["triage"]["mode"] == "shadow"


def test_dashboard_exposes_the_budget_card_and_save_path():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "ops_dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'id="budget-form"' in html
    assert 'id="budget-limit-input"' in html
    assert 'name="daily_map_limit" type="number" min="0" max="1000"' in html
    assert "limit > 1000" in html
    assert "건수는 0~1000, 비율은 0~100, 회차는 1~24" in html
    assert 'id="budget-share-input"' in html
    assert 'id="budget-runs-input"' in html
    assert 'name="daily_run_count" type="number" min="1" max="24"' in html
    assert 'id="budget-current-run"' in html
    assert 'id="budget-current-allowance"' in html
    assert 'id="budget-current-remaining"' in html
    assert 'id="budget-next-run"' in html
    assert "하루 ${limit}건 · ${runCount}회" in html
    assert "daily_run_count: runCount" in html
    assert 'id="budget-domain-panel"' in html
    assert 'id="budget-domain-core-limit"' in html
    assert 'id="budget-domain-core-document"' in html
    assert 'id="budget-domain-crypto-limit"' in html
    assert 'id="budget-domain-crypto-document"' in html
    assert 'id="budget-domain-crypto-article"' in html
    assert 'id="budget-domain-crypto-document-status"' in html
    assert 'id="budget-domain-crypto-held"' in html
    assert 'id="budget-domain-crypto-reader"' in html
    assert "renderBudgetDomains(budget.domains)" in html
    assert 'coverage_domain: "core", daily_map_limit: coreLimit, document_map_limit: 0' in html
    assert 'coverage_domain: "crypto", daily_map_limit: cryptoLimit, document_map_limit: cryptoDocumentLimit' in html
    assert "...(domains ? { domains } : {})" in html
    assert "도메인 한도 합계는 전체 이하" in html
    assert "renderBudget(snapshot.map_budget)" in html
    assert '"/ops/api/map-budget"' in html
    assert 'method: "PUT"' in html
    assert "innerHTML" not in html


def test_dashboard_domain_budget_table_surfaces_isolation_contract():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "ops_dashboard.html").read_text(
        encoding="utf-8"
    )

    assert 'aria-label="도메인별 Map 예산과 격리 상태"' in html
    assert "기사 사용 / 잔량" in html
    assert "문서 사용 / 잔량" in html
    assert "held / 격리" in html
    assert "독자 게이트" in html
    assert 'data-budget-domain="core"' in html
    assert 'data-budget-domain="crypto"' in html
    assert 'reader.textContent = domain.reader_exposed === true ? "독자 노출" : "held 유지"' in html
    assert 'dom["budget-domain-panel"].hidden = !budgetDomainsAvailable' in html


def test_dashboard_dom_registry_covers_every_bracket_reference():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "ops_dashboard.html").read_text(
        encoding="utf-8"
    )
    registry_match = re.search(
        r"const dom = Object\.fromEntries\(\[(.*?)\]\.map",
        html,
        re.DOTALL,
    )
    assert registry_match is not None
    registered = set(re.findall(r'"([a-z0-9-]+)"', registry_match.group(1)))
    referenced = set(re.findall(r'dom\["([a-z0-9-]+)"\]', html))

    assert referenced <= registered, sorted(referenced - registered)
