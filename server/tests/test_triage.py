"""Candidate triage: deterministic signals, response contract and worker modes."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.triage import (
    NEUTRAL_SCORE,
    TriageCandidate,
    TriageScorer,
    TriageValidationError,
    build_results,
    candidate_from_row,
    compose_score,
    deterministic_penalty,
    normalize_triage_response,
    normalized_title,
)
from app.triage_worker import TriageWorker


def _candidate(
    article_id="article-1",
    title="Regulators publish the full agentic AI containment rules",
    domain="news.example",
    url="https://news.example/story",
) -> TriageCandidate:
    return TriageCandidate(
        article_id=article_id,
        title=title,
        domain=domain,
        source_query="ai-technology",
        url=url,
    )


def _axes(**overrides) -> dict[str, int]:
    axes = {
        "durability": 4,
        "significance": 4,
        "explanatory_value": 4,
        "ephemeral_risk": 1,
        "slop_risk": 1,
    }
    axes.update(overrides)
    return axes


def _settings(**overrides) -> Settings:
    base = {
        "_env_file": None,
        "OPENROUTER_API_KEY": "test-key",
        "TRIAGE_BATCH_SIZE": 5,
        "TRIAGE_MAX_ATTEMPTS": 2,
    }
    base.update(overrides)
    return Settings(**base)


# ---- deterministic layer ------------------------------------------------


def test_normalized_title_folds_case_spacing_and_source_suffix():
    assert normalized_title("  Foo   Bar — Reuters ") == "foo bar"
    assert normalized_title("Foo Bar | Reuters") == "foo bar"
    assert normalized_title("FOO, BAR!") == "foo bar"
    assert normalized_title("") == ""
    assert normalized_title("한국어 제목 - 연합뉴스") == "한국어 제목"


def test_deterministic_penalty_flags_wire_sponsored_ticker_and_listicle():
    wire = deterministic_penalty(_candidate(domain="www.prnewswire.com"))
    assert wire == (30, ["wire_domain"])

    sponsored = deterministic_penalty(
        _candidate(url="https://news.example/sponsored/new-thing")
    )
    assert sponsored == (30, ["sponsored_path"])

    ticker = deterministic_penalty(
        _candidate(title="Acme shares rise after quarterly update")
    )
    assert ticker == (25, ["ticker_title"])

    listicle = deterministic_penalty(
        _candidate(title="10 best deals on laptops right now")
    )
    assert listicle == (15, ["listicle_title"])


def test_deterministic_penalty_accumulates_and_caps_at_full_scale():
    penalty, reasons = deterministic_penalty(
        _candidate(
            domain="globenewswire.com",
            title="Top 5 stock to watch price target updates",
            url="https://globenewswire.com/press-release/2026/thing",
        ),
        duplicate=True,
    )

    assert penalty == 100
    assert set(reasons) == {
        "wire_domain",
        "sponsored_path",
        "ticker_title",
        "listicle_title",
        "duplicate_title",
    }


def test_missing_title_is_pinned_to_zero_without_other_reasons():
    assert deterministic_penalty(_candidate(title="   ")) == (100, ["missing_title"])


def test_ordinary_reporting_gets_no_penalty():
    assert deterministic_penalty(_candidate()) == (0, [])


# ---- score composition --------------------------------------------------


def test_compose_score_spans_the_full_range():
    best = _axes(
        durability=5, significance=5, explanatory_value=5,
        ephemeral_risk=0, slop_risk=0,
    )
    worst = _axes(
        durability=0, significance=0, explanatory_value=0,
        ephemeral_risk=5, slop_risk=5,
    )

    assert compose_score(best, 0) == 100
    assert compose_score(worst, 0) == 0
    assert compose_score(best, 30) == 70
    assert compose_score(best, 100) == 0


def test_compose_score_rejects_invalid_axis_values():
    with pytest.raises(TriageValidationError, match="axis_out_of_range"):
        compose_score(_axes(durability=6), 0)
    with pytest.raises(TriageValidationError, match="axis_not_integer"):
        compose_score(_axes(durability="4"), 0)
    with pytest.raises(TriageValidationError, match="axis_not_integer"):
        compose_score(_axes(durability=True), 0)


# ---- provider response contract ----------------------------------------


def test_normalize_triage_response_maps_batch_refs_back_to_article_ids():
    candidates = [_candidate("a"), _candidate("b")]
    raw = {
        "scores": [
            {"ref": 1, **_axes()},
            {"ref": "2", **_axes(durability=1)},
        ]
    }

    parsed = normalize_triage_response(raw, candidates)

    assert set(parsed) == {"a", "b"}
    assert parsed["b"]["durability"] == 1


def test_prompt_never_carries_the_hash_article_id():
    from app.triage_prompts import build_triage_prompt

    _system, user_text = build_triage_prompt([_candidate("a" * 64), _candidate("b" * 64)])

    assert "a" * 64 not in user_text
    assert '"ref":1' in user_text
    assert '"ref":2' in user_text


@pytest.mark.parametrize(
    "raw, code",
    [
        ([], "response_not_object"),
        ({"scores": {}}, "scores_not_list"),
        ({"scores": ["x"]}, "score_not_object"),
        ({"scores": [{"ref": 9, **_axes()}]}, "unknown_ref"),
        ({"scores": [{"ref": 0, **_axes()}]}, "unknown_ref"),
        ({"scores": [{"ref": True, **_axes()}]}, "unknown_ref"),
        ({"scores": [{"article_id": "a", **_axes()}]}, "unknown_ref"),
        ({"scores": []}, "missing_ref"),
    ],
)
def test_normalize_triage_response_rejects_contract_violations(raw, code):
    with pytest.raises(TriageValidationError, match=code):
        normalize_triage_response(raw, [_candidate("a")])


def test_normalize_triage_response_rejects_duplicate_refs():
    raw = {"scores": [{"ref": 1, **_axes()}, {"ref": 1, **_axes()}]}

    with pytest.raises(TriageValidationError, match="duplicate_ref"):
        normalize_triage_response(raw, [_candidate("a"), _candidate("b")])


def test_build_results_marks_fallback_only_for_titled_candidates():
    titled = _candidate("a")
    untitled = _candidate("b", title="")

    results = {
        result.article_id: result
        for result in build_results(
            [titled, untitled],
            axes_by_id={},
            duplicates=set(),
            mode="shadow",
        )
    }

    assert results["a"].score == NEUTRAL_SCORE
    assert results["a"].signals["fallback"] is True
    assert results["b"].score == 0
    assert results["b"].signals["fallback"] is False


def test_build_results_subtracts_duplicate_penalty_from_model_score():
    candidate = _candidate("a")

    [result] = build_results(
        [candidate],
        axes_by_id={"a": _axes()},
        duplicates={"a"},
        mode="enforce",
    )

    assert result.signals["reasons"] == ["duplicate_title"]
    assert result.signals["penalty"] == 20
    assert result.score == compose_score(_axes(), 20)
    assert result.signals["mode"] == "enforce"


def test_candidate_from_row_tolerates_missing_metadata():
    candidate = candidate_from_row({"article_id": "a", "title": None})

    assert candidate.article_id == "a"
    assert candidate.title == ""
    assert candidate.domain == ""


def test_scorer_uses_the_dedicated_triage_model():
    scorer = TriageScorer(_settings(TRIAGE_MODEL="cheap/model-1"))
    assert scorer.model == "cheap/model-1"

    inherited = TriageScorer(_settings(OPENROUTER_MODEL="x-ai/grok-4.3"))
    assert inherited.model == "x-ai/grok-4.3"


# ---- worker -------------------------------------------------------------


class FakeScorer:
    model = "cheap/model-1"
    configured = True

    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls: list[list[str]] = []

    async def score(self, candidates, *, require_crypto_relevance=False):
        self.calls.append([candidate.article_id for candidate in candidates])
        if self.error is not None:
            raise self.error
        return self.result


class FakeRepo:
    def __init__(self, rows=(), duplicates=None, purged=0):
        self.rows = list(rows)
        self.duplicates = duplicates or set()
        self.purged = purged
        self.saved: list[dict] = []
        self.saved_model: str | None = None
        self.bumped: list[str] = []
        self.purge_calls = 0

    async def triage_pending_rows(
        self, *, limit, max_attempts, retry_after_s, coverage_domain=None
    ):
        return self.rows

    async def duplicate_title_ids(self, *, article_ids, title_keys, window_hours):
        return set(self.duplicates)

    async def save_triage_scores(self, results, *, model):
        self.saved.extend(results)
        self.saved_model = model
        return len(results)

    async def bump_triage_attempts(self, article_ids):
        self.bumped.extend(article_ids)
        return len(article_ids)

    async def purge_expired_candidates(self, *, ttl_days, limit):
        self.purge_calls += 1
        return {"deleted": self.purged, "limit": limit}


def _row(
    article_id="a",
    title="Regulators publish containment rules",
    attempts=0,
    coverage_domain="core",
    document_type=None,
):
    return {
        "article_id": article_id,
        "title": title,
        "domain": "news.example",
        "url": f"https://news.example/{article_id}",
        "source_query": "ai-technology",
        "triage_attempts": attempts,
        "coverage_domain": coverage_domain,
        "document_type": document_type,
    }


def _worker(settings, repo, scorer) -> TriageWorker:
    return TriageWorker(settings, repo, scorer, clock=lambda: 0.0)


@pytest.mark.asyncio
async def test_worker_off_mode_runs_retention_without_any_provider_call():
    repo = FakeRepo(rows=[_row()], purged=7)
    scorer = FakeScorer()
    worker = _worker(_settings(TRIAGE_MODE="off"), repo, scorer)

    result = await worker.run_once()

    assert result == {"kind": "retention_only", "scored": 0, "deleted": 7}
    assert scorer.calls == []
    assert repo.saved == []
    assert repo.purge_calls == 1


@pytest.mark.asyncio
async def test_worker_scores_one_batch_and_persists_with_the_triage_model():
    repo = FakeRepo(rows=[_row("a"), _row("b")])
    scorer = FakeScorer({"a": _axes(), "b": _axes(durability=0)})
    worker = _worker(_settings(TRIAGE_MODE="shadow"), repo, scorer)

    result = await worker.run_once()

    assert result["kind"] == "scored"
    assert result["scored"] == 2
    assert scorer.calls == [["a", "b"]]
    assert repo.saved_model == "cheap/model-1"
    scores = {item["article_id"]: item["score"] for item in repo.saved}
    assert scores["a"] > scores["b"]


@pytest.mark.asyncio
async def test_worker_scores_untitled_candidates_without_the_provider():
    repo = FakeRepo(rows=[_row("a", title="  ")])
    scorer = FakeScorer()
    worker = _worker(_settings(TRIAGE_MODE="shadow"), repo, scorer)

    result = await worker.run_once()

    assert scorer.calls == []
    assert result["scored"] == 1
    assert repo.saved[0]["score"] == 0


@pytest.mark.asyncio
async def test_worker_retries_a_failed_batch_before_the_last_attempt():
    repo = FakeRepo(rows=[_row("a", attempts=0)])
    scorer = FakeScorer(error=TriageValidationError("missing_article_id"))
    worker = _worker(_settings(TRIAGE_MODE="shadow"), repo, scorer)

    result = await worker.run_once()

    assert result["kind"] == "invalid:missing_article_id"
    assert result["scored"] == 0
    assert repo.bumped == ["a"]
    assert repo.saved == []


@pytest.mark.asyncio
async def test_worker_stores_neutral_fallback_on_the_last_attempt():
    repo = FakeRepo(rows=[_row("a", attempts=1)])
    scorer = FakeScorer(error=RuntimeError("provider down"))
    worker = _worker(_settings(TRIAGE_MODE="enforce"), repo, scorer)

    result = await worker.run_once()

    assert result["kind"] == "fallback"
    assert repo.bumped == ["a"]
    assert repo.saved[0]["score"] == NEUTRAL_SCORE
    assert repo.saved[0]["signals"]["fallback"] is True


@pytest.mark.asyncio
async def test_worker_penalises_repeat_headlines_inside_one_batch():
    repo = FakeRepo(rows=[_row("a"), _row("b")])
    scorer = FakeScorer({"a": _axes(), "b": _axes()})
    worker = _worker(_settings(TRIAGE_MODE="shadow"), repo, scorer)

    await worker.run_once()

    scores = {item["article_id"]: item for item in repo.saved}
    assert scores["a"]["signals"]["reasons"] == []
    assert scores["b"]["signals"]["reasons"] == ["duplicate_title"]
    assert scores["b"]["score"] < scores["a"]["score"]


@pytest.mark.asyncio
async def test_worker_survives_a_failed_duplicate_lookup():
    class BrokenDuplicates(FakeRepo):
        async def duplicate_title_ids(self, **_kwargs):
            raise RuntimeError("db down")

    repo = BrokenDuplicates(rows=[_row("a")])
    scorer = FakeScorer({"a": _axes()})
    worker = _worker(_settings(TRIAGE_MODE="shadow"), repo, scorer)

    result = await worker.run_once()

    assert result["scored"] == 1


@pytest.mark.asyncio
async def test_worker_purges_only_once_inside_the_interval():
    repo = FakeRepo(rows=[], purged=3)
    worker = _worker(_settings(TRIAGE_MODE="off"), repo, FakeScorer())

    first = await worker.run_once()
    second = await worker.run_once()

    assert first["deleted"] == 3
    assert second["deleted"] == 0
    assert repo.purge_calls == 1


@pytest.mark.asyncio
async def test_worker_skips_purge_when_candidate_retention_is_disabled():
    """An inspection-only profile must not delete candidate rows.

    TRIAGE_MODE=off stops scoring but not retention, so the isolated staging
    `serve` surface previously had no way to keep the worker's first tick from
    purging the corpus it was meant to only look at.
    """
    repo = FakeRepo(rows=[], purged=3)
    worker = _worker(
        _settings(TRIAGE_MODE="off", MAP_CANDIDATE_PURGE_ENABLED=False),
        repo,
        FakeScorer(),
    )

    result = await worker.run_once()

    assert result["deleted"] == 0
    assert repo.purge_calls == 0


def test_worker_start_is_skipped_only_when_nothing_needs_it():
    repo = FakeRepo()
    scorer = FakeScorer()
    disabled = TriageWorker(
        _settings(TRIAGE_MODE="off", MAP_BUDGET_ENABLED=False), repo, scorer
    )
    assert disabled.start() is False
