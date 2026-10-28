"""DR-13219 crypto relevance admission without changing core triage."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.repository import MapBudget, Repository
from app.triage import (
    CRYPTO_RELEVANCE_MIN_PRECISION,
    CRYPTO_RELEVANCE_SAMPLE_SIZE,
    CRYPTO_RELEVANCE_TOP_K,
    CRYPTO_TRIAGE_PROFILE_VERSION,
    TriageCandidate,
    TriageValidationError,
    crypto_relevance_required,
    measure_crypto_relevance_gate,
    normalize_triage_response,
)
from app.triage_prompts import (
    CRYPTO_TRIAGE_PROMPT_VERSION,
    TRIAGE_SYSTEM_PROMPT,
    build_triage_prompt,
)
from app.triage_worker import TriageWorker
from tests.test_map_budget import BudgetConnection, BudgetPool


def _axes(*, relevance: str | None = None) -> dict[str, int | str]:
    result: dict[str, int | str] = {
        "durability": 4,
        "significance": 4,
        "explanatory_value": 4,
        "ephemeral_risk": 1,
        "slop_risk": 1,
    }
    if relevance is not None:
        result["relevance"] = relevance
    return result


def _row(
    article_id: str,
    *,
    coverage_domain: str,
    attempts: int = 0,
    document_type: str | None = None,
) -> dict:
    return {
        "article_id": article_id,
        "title": (
            "Ethereum governance proposal changes validator exits"
            if coverage_domain == "crypto"
            else "Regulators publish containment rules"
        ),
        "domain": "news.example",
        "url": f"https://news.example/{article_id}",
        "source_query": "primary-document:test" if document_type else "topic",
        "coverage_domain": coverage_domain,
        "document_type": document_type,
        "triage_attempts": attempts,
    }


class Repo:
    def __init__(self, rows):
        self.rows = list(rows)
        self.saved: list[dict] = []
        self.bumped: list[str] = []
        self.pending_kwargs = None

    async def triage_pending_rows(self, **kwargs):
        self.pending_kwargs = kwargs
        domain = kwargs.get("coverage_domain")
        if domain is None:
            return self.rows
        return [row for row in self.rows if row["coverage_domain"] == domain]

    async def duplicate_title_ids(self, **_kwargs):
        return set()

    async def bump_triage_attempts(self, article_ids):
        self.bumped.extend(article_ids)
        return len(article_ids)

    async def save_triage_scores(self, results, *, model):
        self.saved.extend(results)
        return len(results)

    async def purge_expired_candidates(self, **_kwargs):
        return {"deleted": 0}


class Scorer:
    configured = True
    model = "provider/crypto-test"

    def __init__(self, *, fail_crypto: bool = False, fail_core: bool = False):
        self.fail_crypto = fail_crypto
        self.fail_core = fail_core
        self.calls: list[tuple[list[str], bool]] = []

    async def score(self, candidates, *, require_crypto_relevance=False):
        self.calls.append(
            ([candidate.article_id for candidate in candidates], require_crypto_relevance)
        )
        if require_crypto_relevance and self.fail_crypto:
            raise RuntimeError("crypto provider unavailable")
        if not require_crypto_relevance and self.fail_core:
            raise RuntimeError("core provider unavailable")
        return {
            candidate.article_id: _axes(
                relevance="on_topic" if require_crypto_relevance else None
            )
            for candidate in candidates
        }


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "OPENROUTER_API_KEY": "test",
        "TRIAGE_BATCH_SIZE": 5,
        "TRIAGE_MAX_ATTEMPTS": 2,
        "TRIAGE_STALE_HOURS": 1.0,
        "ENABLE_MAPPING": True,
        "MAP_BUDGET_ENABLED": True,
    }
    values.update(overrides)
    return Settings(**values)


class RecordingPool:
    def __init__(self):
        self.query = ""
        self.args = ()

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return []


def test_core_prompt_bytes_and_response_shape_remain_unchanged():
    candidate = TriageCandidate("core", "A title", "news.example", "topic")
    system, _user = build_triage_prompt([candidate])
    assert system == TRIAGE_SYSTEM_PROMPT
    parsed = normalize_triage_response(
        {"scores": [{"ref": 1, **_axes()}]}, [candidate]
    )
    assert parsed == {"core": _axes()}


def test_crypto_schema_requires_binary_relevance():
    candidate = TriageCandidate(
        "crypto", "Bitcoin protocol update", "news.example", "topic",
        coverage_domain="crypto",
    )
    with pytest.raises(TriageValidationError, match="relevance_not_enum"):
        normalize_triage_response(
            {"scores": [{"ref": 1, **_axes()}]},
            [candidate],
            require_crypto_relevance=True,
        )
    parsed = normalize_triage_response(
        {"scores": [{"ref": 1, **_axes(relevance="on_topic")}]},
        [candidate],
        require_crypto_relevance=True,
    )
    assert parsed["crypto"]["relevance"] == "on_topic"


@pytest.mark.asyncio
async def test_worker_splits_core_and_crypto_provider_batches():
    repo = Repo([_row("core", coverage_domain="core"), _row("crypto", coverage_domain="crypto")])
    scorer = Scorer()
    worker = TriageWorker(_settings(TRIAGE_MODE="shadow"), repo, scorer, clock=lambda: 0.0)

    result = await worker.run_once()

    assert result["scored"] == 2
    assert scorer.calls == [(["core"], False), (["crypto"], True)]
    saved = {item["article_id"]: item for item in repo.saved}
    assert "relevance" not in saved["core"]["signals"]
    crypto = saved["crypto"]["signals"]
    assert crypto["relevance"] == "on_topic"
    assert crypto["fallback"] is False
    assert crypto["prompt_version"] == CRYPTO_TRIAGE_PROMPT_VERSION
    assert crypto["profile_version"] == CRYPTO_TRIAGE_PROFILE_VERSION


@pytest.mark.asyncio
async def test_global_off_still_scores_only_crypto():
    repo = Repo([_row("core", coverage_domain="core"), _row("crypto", coverage_domain="crypto")])
    scorer = Scorer()
    worker = TriageWorker(_settings(TRIAGE_MODE="off"), repo, scorer, clock=lambda: 0.0)

    result = await worker.run_once()

    assert result["scored"] == 1
    assert scorer.calls == [(["crypto"], True)]
    assert repo.pending_kwargs["coverage_domain"] == "crypto"


@pytest.mark.asyncio
async def test_crypto_provider_failure_never_saves_neutral_at_max_attempts():
    repo = Repo([_row("crypto", coverage_domain="crypto", attempts=1)])
    scorer = Scorer(fail_crypto=True)
    worker = TriageWorker(_settings(TRIAGE_MODE="shadow"), repo, scorer, clock=lambda: 0.0)

    result = await worker.run_once()

    assert result["scored"] == 0
    assert repo.saved == []
    assert repo.bumped == ["crypto"]
    assert repo.pending_kwargs["retry_after_s"] == 3600.0


@pytest.mark.asyncio
async def test_crypto_provider_recovers_after_max_attempt_cooldown_query():
    pool = RecordingPool()
    repo = Repository(type("Database", (), {"pool": pool})())

    await repo.triage_pending_rows(
        limit=5,
        max_attempts=2,
        retry_after_s=3600.0,
        coverage_domain="crypto",
    )

    assert pool.args == (5, 2, 3600.0, "crypto")
    assert "q.triage_attempts < $2" in pool.query
    assert "q.updated_at <= now()" in pool.query
    assert "$3::double precision" in pool.query
    assert "IN ('on_topic', 'off_topic')" in pool.query


@pytest.mark.asyncio
async def test_crypto_provider_recovery_saves_actual_relevance_after_cooldown():
    repo = Repo([_row("crypto", coverage_domain="crypto", attempts=2)])
    scorer = Scorer()
    worker = TriageWorker(_settings(TRIAGE_MODE="off"), repo, scorer, clock=lambda: 0.0)

    result = await worker.run_once()

    assert result["scored"] == 1
    assert repo.saved[0]["signals"]["fallback"] is False
    assert repo.saved[0]["signals"]["relevance"] == "on_topic"


@pytest.mark.asyncio
async def test_core_failure_fallback_is_isolated_from_crypto_success():
    repo = Repo(
        [
            _row("core", coverage_domain="core", attempts=1),
            _row("crypto", coverage_domain="crypto", attempts=1),
        ]
    )
    scorer = Scorer(fail_core=True)
    worker = TriageWorker(_settings(TRIAGE_MODE="enforce"), repo, scorer, clock=lambda: 0.0)

    result = await worker.run_once()

    saved = {item["article_id"]: item for item in repo.saved}
    assert result["kind"] == "fallback"
    assert saved["core"]["signals"]["fallback"] is True
    assert saved["crypto"]["signals"]["relevance"] == "on_topic"


def _claim_calls(connection):
    return [call for call in connection.calls if "WITH candidate AS" in call[1]]


@pytest.mark.asyncio
async def test_crypto_claim_forces_nonfallback_on_topic_score_while_global_shadow():
    connection = BudgetConnection(
        domain_rows=(
            {"coverage_domain": "crypto", "daily_map_limit": 20, "document_map_limit": 7},
        ),
        claims=[{"article_id": "on-topic", "coverage_domain": "crypto", "bucket": "fresh"}],
    )
    repo = Repository(type("Database", (), {"pool": BudgetPool(connection)})())

    claimed = await repo.queue_claim_next(
        lease_seconds=300,
        max_active=3,
        budget=MapBudget(daily_limit=60, fresh_share_percent=75),
        ranked=False,
    )

    assert claimed == "on-topic"
    _kind, query, args = _claim_calls(connection)[0]
    assert args[4] is True  # crypto ranks even when global shadow is false
    assert args[10] is True
    assert '"relevance": "on_topic"' in query
    assert '"fallback": false' in query
    assert "q.attempts > 0" in query  # present only in NOT strict branch
    assert "q.triage_score" in query
    assert "NOT $11::boolean" in query
    assert "$11::boolean\n                OR NOT $5::boolean" in query


@pytest.mark.asyncio
async def test_crypto_document_does_not_bypass_relevance_by_allowlist():
    assert crypto_relevance_required(
        coverage_domain="crypto", document_type="governance_proposal"
    )
    connection = BudgetConnection(
        domain_rows=(
            {"coverage_domain": "crypto", "daily_map_limit": 7, "document_map_limit": 7},
        ),
        claims=[None, None, None],
    )
    repo = Repository(type("Database", (), {"pool": BudgetPool(connection)})())

    assert await repo.queue_claim_next(
        lease_seconds=300,
        max_active=3,
        budget=MapBudget(daily_limit=7, fresh_share_percent=75),
    ) is None
    assert all(call[2][10] is True for call in _claim_calls(connection))


def _measurement_rows(*, top_false_articles: int = 0, document_false_positive: bool = False):
    rows = []
    for index in range(43):
        rows.append(
            {
                "sample_id": f"article-{index:02d}",
                "subtype": "article",
                "on_topic": index >= top_false_articles,
                "duplicate_family": f"article-family-{index:02d}",
                "score": 100 - index,
                "model": "provider/model-v1",
                "signals": {
                    "relevance": "on_topic",
                    "fallback": False,
                    "profile": "crypto",
                    "profile_version": CRYPTO_TRIAGE_PROFILE_VERSION,
                    "prompt_version": CRYPTO_TRIAGE_PROMPT_VERSION,
                },
            }
        )
    for index in range(7):
        rows.append(
            {
                "sample_id": f"document-{index:02d}",
                "subtype": "document",
                "on_topic": not (document_false_positive and index == 0),
                "duplicate_family": f"document-family-{index:02d}",
                "score": 40 - index,
                "model": "provider/model-v1",
                "signals": {
                    "relevance": "on_topic",
                    "fallback": False,
                    "profile": "crypto",
                    "profile_version": CRYPTO_TRIAGE_PROFILE_VERSION,
                    "prompt_version": CRYPTO_TRIAGE_PROMPT_VERSION,
                },
            }
        )
    return rows


def test_measurement_constants_are_the_frozen_gate_contract():
    assert CRYPTO_RELEVANCE_SAMPLE_SIZE == 50
    assert CRYPTO_RELEVANCE_TOP_K == 20
    assert CRYPTO_RELEVANCE_MIN_PRECISION == 0.90


def test_gate_threshold_requires_eighteen_of_twenty():
    passed = measure_crypto_relevance_gate(_measurement_rows(top_false_articles=2))
    failed = measure_crypto_relevance_gate(_measurement_rows(top_false_articles=3))

    assert passed["status"] == "pass"
    assert passed["article_family"]["precision"] == 0.90
    assert passed["minimum_correct"] == 18
    assert failed["status"] == "fail"
    assert failed["article_family"]["precision"] == 0.85


def test_document_false_positive_fails_gate():
    result = measure_crypto_relevance_gate(
        _measurement_rows(document_false_positive=True)
    )
    assert result["status"] == "fail"
    assert result["document_false_positives"] == 1
    assert result["subtype_confusion"]["document"] == {
        "true_positive": 6,
        "false_positive": 1,
        "true_negative": 0,
        "false_negative": 0,
    }


def test_gate_is_inconclusive_below_twenty_positive_article_families():
    rows = _measurement_rows()
    for index, row in enumerate(rows[:43]):
        row["on_topic"] = index < 19

    result = measure_crypto_relevance_gate(rows)

    assert result["status"] == "inconclusive"
    assert "fewer_than_twenty_positive_article_families" in result["reasons"]


def test_gate_collapses_duplicate_families_using_highest_score():
    rows = _measurement_rows()
    rows[1]["duplicate_family"] = rows[0]["duplicate_family"]
    rows[0]["score"] = 98
    rows[1]["score"] = 99

    result = measure_crypto_relevance_gate(rows)

    family = result["family"]["ranking"]
    assert family[0]["sample_id"] == "article-01"
    assert len({item["duplicate_family"] for item in family}) == len(family)
