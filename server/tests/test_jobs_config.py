"""Regression tests for the deployed multi-provider job configuration."""

from pathlib import Path

import pytest
import yaml

from app.jobs import Job, load_jobs


JOBS_FILE = Path(__file__).resolve().parents[1] / "jobs.yaml"
TECHNOLOGY_FOCUS_TERMS = (
    '"artificial intelligence"',
    '"machine learning"',
    '"generative AI"',
    '"technology industry"',
    '"computer science"',
    "computing",
    '"cloud computing"',
    '"quantum computing"',
    "semiconductor",
    "semiconductors",
    "microchip",
    "microchips",
)
TECHNOLOGY_FOCUS_QUERY = f"({' OR '.join(TECHNOLOGY_FOCUS_TERMS)})"
CRYPTO_LANE = "blockchain-crypto"
CRYPTO_QUERY = (
    '(blockchain OR cryptocurrency OR cryptocurrencies OR bitcoin OR ethereum '
    'OR stablecoin OR stablecoins OR "digital asset" OR "digital assets" '
    'OR tokenization OR "smart contract" OR "smart contracts" OR defi)'
)
NEWSAPI_LANES = {
    "newsapi:ai-models-agents": '("large language model" OR LLM OR "foundation model" OR "AI agent" OR agentic)',
    "newsapi:developer-open-source": '("AI coding" OR "code generation" OR "developer tools" OR "open source AI" OR "model release")',
    "newsapi:inference-infrastructure": '("AI inference" OR "model serving" OR GPU OR "AI accelerator" OR datacenter OR "data center")',
    "newsapi:robotics-edge-security": '(robotics OR humanoid OR "edge AI" OR cybersecurity OR "software supply chain") AND (AI OR software OR technology)',
}


def test_deployed_jobs_keep_climate_disabled_and_preserve_other_active_jobs():
    raw_jobs = yaml.safe_load(JOBS_FILE.read_text(encoding="utf-8"))["jobs"]
    climate = next(job for job in raw_jobs if job["name"] == "climate")

    assert climate == {
        "name": "climate",
        "provider": "gdelt",
        "query": "climate change",
        "timespan": "1d",
        "source_lang": "english",
        "enabled": False,
    }
    active_names = {job.name for job in load_jobs(str(JOBS_FILE))}
    assert "climate" not in active_names
    assert "ai-technology" in active_names


def test_deployed_jobs_split_one_gdelt_job_and_four_newsapi_gap_lanes():
    raw_jobs = yaml.safe_load(JOBS_FILE.read_text(encoding="utf-8"))["jobs"]
    jobs_by_name = {job["name"]: job for job in raw_jobs}

    assert {
        name for name, job in jobs_by_name.items() if job.get("enabled", True)
    } == {"ai-technology", CRYPTO_LANE, *NEWSAPI_LANES}
    assert {
        name for name, job in jobs_by_name.items() if not job.get("enabled", True)
    } == {"korea-politics", "climate", "korea-domestic"}
    assert jobs_by_name["ai-technology"] == {
        "name": "ai-technology",
        "provider": "gdelt",
        "query": TECHNOLOGY_FOCUS_QUERY,
        "window_hours": 24,
        "slice_hours": 6,
        "enabled": True,
    }

    jobs = load_jobs(str(JOBS_FILE))
    assert [job.name for job in jobs] == [
        "ai-technology",
        *NEWSAPI_LANES,
        CRYPTO_LANE,
    ]
    assert jobs[0].provider == "gdelt"
    assert jobs[0].query == TECHNOLOGY_FOCUS_QUERY
    assert jobs[0].window_hours == 24
    assert jobs[0].slice_hours == 6
    assert jobs[0].source_lang is None
    assert jobs[0].source_country is None


def test_newsapi_gap_lanes_keep_exact_queries_and_everything_options():
    jobs = {job.name: job for job in load_jobs(str(JOBS_FILE))}

    for name, query in NEWSAPI_LANES.items():
        job = jobs[name]
        assert job.provider == "newsapi"
        assert job.query == query
        assert job.timespan is None
        assert job.window_hours == 24
        assert job.max_records == 100
        assert job.language == "en"
        assert job.search_in == "title,description"
        assert job.sort_by == "publishedAt"
        assert job.source_lang is None
        assert job.source_country is None

    assert len(NEWSAPI_LANES) * 48 == 192


def test_technology_focus_query_is_one_non_nested_or_block():
    assert TECHNOLOGY_FOCUS_QUERY.startswith("(")
    assert TECHNOLOGY_FOCUS_QUERY.endswith(")")
    assert TECHNOLOGY_FOCUS_QUERY.count("(") == 1
    assert TECHNOLOGY_FOCUS_QUERY.count(")") == 1
    assert TECHNOLOGY_FOCUS_QUERY.count(" OR ") == 11
    assert TECHNOLOGY_FOCUS_QUERY[1:-1].split(" OR ") == list(
        TECHNOLOGY_FOCUS_TERMS
    )


def test_technology_focus_query_stays_within_verified_gdelt_length_gate():
    assert len(TECHNOLOGY_FOCUS_QUERY) == 234
    assert '"AI technology"' not in TECHNOLOGY_FOCUS_QUERY
    assert '"computer chip"' not in TECHNOLOGY_FOCUS_QUERY
    assert '"computer chips"' not in TECHNOLOGY_FOCUS_QUERY


def test_load_jobs_filters_disabled_entries(tmp_path: Path):
    jobs_file = tmp_path / "jobs.yaml"
    jobs_file.write_text(
        """jobs:
  - name: active
    query: active topic
  - name: paused
    query: paused topic
    enabled: false
""",
        encoding="utf-8",
    )

    jobs = load_jobs(str(jobs_file))

    assert [job.name for job in jobs] == ["active"]


def test_load_jobs_missing_file_keeps_builtin_fallback(tmp_path: Path):
    jobs = load_jobs(str(tmp_path / "missing.yaml"))

    assert len(jobs) == 1
    assert jobs[0].name == "breaking-news"
    assert jobs[0].query == "breaking news"
    assert jobs[0].provider == "gdelt"
    assert jobs[0].timespan == "1d"


def _valid_newsapi_job(**changes):
    raw = {
        "name": "newsapi:test",
        "provider": "newsapi",
        "query": "AI",
        "window_hours": 24,
        "max_records": 100,
        "language": "en",
        "search_in": "title,description",
        "sort_by": "publishedAt",
    }
    raw.update(changes)
    return raw


@pytest.mark.parametrize(
    "changes",
    (
        {"provider": "unknown"},
        {"name": "missing-prefix"},
        {"timespan": "1d"},
        {"window_hours": 23},
        {"max_records": 101},
        {"language": "ko"},
        {"search_in": "title"},
        {"sort_by": "relevancy"},
        {"source_lang": "english"},
        {"query": "x" * 501},
    ),
)
def test_newsapi_job_validation_rejects_provider_contract_violations(changes):
    with pytest.raises(ValueError):
        Job.from_dict(_valid_newsapi_job(**changes))


def test_newsapi_query_accepts_500_character_boundary():
    job = Job.from_dict(_valid_newsapi_job(query="x" * 500, max_records=1))

    assert len(job.query) == 500
    assert job.max_records == 1


@pytest.mark.parametrize("field", ("language", "search_in", "sort_by"))
def test_gdelt_jobs_reject_newsapi_only_fields(field):
    with pytest.raises(ValueError):
        Job.from_dict({"name": "gdelt", "query": "AI", field: "value"})


def test_load_jobs_rejects_duplicate_names(tmp_path: Path):
    jobs_file = tmp_path / "jobs.yaml"
    jobs_file.write_text(
        """jobs:
  - name: duplicate
    query: one
  - name: duplicate
    query: two
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate job name"):
        load_jobs(str(jobs_file))


def test_load_jobs_surfaces_invalid_yaml(tmp_path: Path):
    jobs_file = tmp_path / "jobs.yaml"
    jobs_file.write_text("jobs: [", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_jobs(str(jobs_file))


# ---- Coverage domain quarantine (DR-13208) ------------------------------


def test_articles_schema_declares_coverage_domain():
    from app.db import SCHEMA_SQL

    assert "ADD COLUMN IF NOT EXISTS coverage_domain TEXT NOT NULL DEFAULT 'core'" in (
        " ".join(SCHEMA_SQL.split())
    )
    assert "idx_articles_coverage_domain" in SCHEMA_SQL


def test_job_defaults_to_core_coverage_domain():
    job = Job.from_dict({"name": "gdelt-job", "query": "AI"})

    assert job.coverage_domain == "core"


def test_job_accepts_crypto_coverage_domain():
    job = Job.from_dict(
        {"name": "gdelt-job", "query": "bitcoin", "coverage_domain": "crypto"}
    )

    assert job.coverage_domain == "crypto"


@pytest.mark.parametrize("domain", ("", " ", "Crypto", "btc", "CORE", "unknown"))
def test_job_rejects_unknown_coverage_domain(domain):
    with pytest.raises(ValueError, match="coverage_domain"):
        Job.from_dict(
            {"name": "gdelt-job", "query": "AI", "coverage_domain": domain}
        )


def test_newsapi_jobs_may_also_declare_a_coverage_domain():
    """The domain axis is provider-independent so a crypto NewsAPI lane can
    be opened later without another schema change."""
    job = Job.from_dict(_valid_newsapi_job(coverage_domain="crypto"))

    assert job.coverage_domain == "crypto"


def test_deployed_jobs_add_quarantined_crypto_lane():
    raw_jobs = yaml.safe_load(JOBS_FILE.read_text(encoding="utf-8"))["jobs"]
    jobs_by_name = {job["name"]: job for job in raw_jobs}

    assert jobs_by_name[CRYPTO_LANE] == {
        "name": CRYPTO_LANE,
        "provider": "gdelt",
        "coverage_domain": "crypto",
        "query": CRYPTO_QUERY,
        "window_hours": 24,
        "slice_hours": 6,
        # DR-13228: this lane renders into Korean, so it collects only the two
        # languages it can render faithfully.
        "source_lang": ["english", "korean"],
        "enabled": True,
    }

    job = {job.name: job for job in load_jobs(str(JOBS_FILE))}[CRYPTO_LANE]
    assert job.provider == "gdelt"
    assert job.coverage_domain == "crypto"
    assert job.query == CRYPTO_QUERY
    assert job.source_lang == ["english", "korean"]
    assert job.window_hours == 24
    assert job.slice_hours == 6
    assert job.source_country is None


def test_crypto_query_stays_within_the_verified_gdelt_length_gate():
    """234 chars is the longest query verified against DOC 2.0 in this repo."""
    assert len(CRYPTO_QUERY) == 206
    assert len(CRYPTO_QUERY) <= len(TECHNOLOGY_FOCUS_QUERY) == 234


def test_crypto_query_is_one_non_nested_or_block():
    assert CRYPTO_QUERY.startswith("(") and CRYPTO_QUERY.endswith(")")
    assert CRYPTO_QUERY.count("(") == CRYPTO_QUERY.count(")") == 1


def test_crypto_query_pairs_every_stemmable_singular_with_its_plural():
    """GDELT does not stem, so a missing plural silently halves recall."""
    terms = CRYPTO_QUERY[1:-1].split(" OR ")
    for singular in ("cryptocurrency", "stablecoin", '"digital asset"'):
        plural = (
            "cryptocurrencies"
            if singular == "cryptocurrency"
            else singular.replace('"', "") + "s"
        )
        plural = f'"{plural}"' if singular.startswith('"') else plural
        assert singular in terms and plural in terms


def test_crypto_lane_excludes_price_and_trading_terms():
    """Triage already penalises price moves via ephemeral_risk; collecting
    them would only grow the penalised pile."""
    lowered = CRYPTO_QUERY.lower()
    for noise in ("price", "rally", "surge", "trading", "buy", "sell", "forecast"):
        assert noise not in lowered


def test_only_core_domain_jobs_are_active_besides_the_crypto_lane():
    """Every other active lane must stay 'core' or it would stop being mapped."""
    jobs = load_jobs(str(JOBS_FILE))
    by_domain: dict[str, set[str]] = {}
    for job in jobs:
        by_domain.setdefault(job.coverage_domain, set()).add(job.name)

    assert by_domain["crypto"] == {CRYPTO_LANE}
    assert by_domain["core"] == {"ai-technology", *NEWSAPI_LANES}


def test_crypto_lane_does_not_add_newsapi_budget_pressure():
    """A fifth NewsAPI lane would exceed the 24h logical run budget; the crypto
    lane is GDELT precisely so that it does not."""
    from app.config import NEWSAPI_MAX_ATTEMPTS, Settings

    settings = Settings(_env_file=None)
    newsapi_jobs = [
        job for job in load_jobs(str(JOBS_FILE)) if job.provider == "newsapi"
    ]

    assert len(newsapi_jobs) == 4
    assert len(newsapi_jobs) * 48 <= settings.newsapi_logical_run_limit()
    assert (len(newsapi_jobs) + 1) * 48 > settings.newsapi_logical_run_limit()
    assert settings.newsapi_logical_run_limit() == 900 // NEWSAPI_MAX_ATTEMPTS
