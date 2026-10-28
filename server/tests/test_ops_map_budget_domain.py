"""Per-domain budget on the /ops surface (I-13200-f, DR-13206/DR-13209).

The domain split is optional on the wire so an operator client that predates
DR-13206 keeps working unchanged, and the total is a hard ceiling: a split that
oversubscribes it would let the two domains together outspend the budget the
operator actually set.
"""
from __future__ import annotations

import httpx
import pytest

from app.api import app, state

from tests.test_ops_budget import NOW, FakeBudgetRepo, budget_state  # noqa: F401


BASE = {"daily_map_limit": 60, "fresh_share_percent": 75, "daily_run_count": 1}


async def _put(**overrides):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.put(
            "/ops/api/map-budget",
            json={**BASE, **overrides},
            headers={"X-API-Token": "ops-secret"},
        )


@pytest.mark.asyncio
async def test_update_without_domains_leaves_the_split_untouched(budget_state):
    response = await _put(daily_map_limit=50)

    assert response.status_code == 200
    assert budget_state.updates == [(50, 75, 1)]
    # None, not an empty list: "say nothing about the split" and "clear the
    # split" must not collapse into the same request.
    assert budget_state.domain_updates == [None]


@pytest.mark.asyncio
async def test_update_persists_the_split(budget_state):
    response = await _put(
        domains=[
            {"coverage_domain": "core", "daily_map_limit": 45},
            {"coverage_domain": "crypto", "daily_map_limit": 15},
        ],
    )

    assert response.status_code == 200
    assert budget_state.domain_updates == [
        [("core", 45, None), ("crypto", 15, None)]
    ]


@pytest.mark.asyncio
async def test_split_may_not_exceed_the_total(budget_state):
    response = await _put(
        domains=[
            {"coverage_domain": "core", "daily_map_limit": 50},
            {"coverage_domain": "crypto", "daily_map_limit": 20},
        ],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "domain_limits_exceed_total"
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_split_below_the_total_is_allowed(budget_state):
    # Deliberate headroom is a legitimate operator choice, not an error.
    response = await _put(
        domains=[
            {"coverage_domain": "core", "daily_map_limit": 30},
            {"coverage_domain": "crypto", "daily_map_limit": 10},
        ],
    )

    assert response.status_code == 200
    assert budget_state.domain_updates == [
        [("core", 30, None), ("crypto", 10, None)]
    ]


@pytest.mark.asyncio
async def test_duplicate_domain_is_rejected(budget_state):
    response = await _put(
        domains=[
            {"coverage_domain": "crypto", "daily_map_limit": 10},
            {"coverage_domain": "crypto", "daily_map_limit": 10},
        ],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "duplicate_coverage_domain"
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_unsupported_domain_is_rejected_before_the_repository(budget_state):
    response = await _put(
        domains=[{"coverage_domain": "sports", "daily_map_limit": 5}],
    )

    assert response.status_code == 422
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_zero_domain_limit_is_a_valid_hold(budget_state):
    response = await _put(
        domains=[{"coverage_domain": "crypto", "daily_map_limit": 0}],
    )

    assert response.status_code == 200
    assert budget_state.domain_updates == [[("crypto", 0, None)]]


@pytest.mark.asyncio
async def test_explicit_document_limit_is_persisted_with_domain_split(budget_state):
    response = await _put(
        domains=[
            {
                "coverage_domain": "core",
                "daily_map_limit": 40,
                "document_map_limit": 0,
            },
            {
                "coverage_domain": "crypto",
                "daily_map_limit": 20,
                "document_map_limit": 7,
            },
        ],
    )

    assert response.status_code == 200
    assert budget_state.domain_updates == [
        [("core", 40, 0), ("crypto", 20, 7)]
    ]


@pytest.mark.asyncio
async def test_document_limit_may_not_exceed_domain_limit(budget_state):
    response = await _put(
        domains=[
            {
                "coverage_domain": "crypto",
                "daily_map_limit": 6,
                "document_map_limit": 7,
            }
        ],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "document_map_limit_exceeds_domain_limit"
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_core_document_limit_must_stay_zero(budget_state):
    response = await _put(
        domains=[
            {
                "coverage_domain": "core",
                "daily_map_limit": 40,
                "document_map_limit": 1,
            }
        ],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "core_document_map_limit_must_be_zero"
    assert budget_state.updates == []


@pytest.mark.asyncio
async def test_snapshot_separates_quarantine_from_the_reader_gate(
    budget_state, monkeypatch
):
    class SnapshotRepo(FakeBudgetRepo):
        async def worker_telemetry_rows(self, window_minutes):
            return {"now": NOW, "instances": [], "activities": []}

        async def available_article_count(self):
            return 0

        async def queue_stats(self):
            return {"queued": 0, "due_now": 0}

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
    domains = response.json()["map_budget"]["domains"]
    by_name = {item["coverage_domain"]: item for item in domains}
    assert by_name["core"]["reader_exposed"] is True
    assert by_name["crypto"]["reader_exposed"] is False
    # Collected but unmapped (DR-13208) is a different state from mapped but
    # withheld (DR-13209); an operator must be able to tell them apart.
    assert by_name["crypto"]["quarantined"] == 9
    assert by_name["crypto"]["held_maps"] == 0
