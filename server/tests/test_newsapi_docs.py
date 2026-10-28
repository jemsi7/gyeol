"""Safe configuration and operator documentation contracts for NewsAPI."""
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_has_safe_newsapi_defaults_without_a_credential():
    lines = (SERVER_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = {
        key: value
        for line in lines
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }

    assert values["NEWSAPI_ENABLED"] == "false"
    assert values["NEWSAPI_API_KEY"] == ""
    assert values["NEWSAPI_BASE_URL"] == "https://newsapi.org/v2/everything"
    assert values["NEWSAPI_TIMEOUT_S"] == "30"
    assert values["NEWSAPI_REQUEST_BUDGET_24H"] == "900"


def test_readme_documents_provider_split_gap_lanes_and_hard_budget():
    readme = (SERVER_ROOT / "README.md").read_text(encoding="utf-8")

    # Two GDELT jobs now run four 6h slices each per tick; NewsAPI is unchanged
    # because the crypto lane is deliberately not a fifth NewsAPI lane.
    assert "GDELT 8 : NewsAPI 4" in readme
    assert "4 × 48 = 192회/24h" in readme
    assert "225 × 4 = 900회" in readme
    assert "100회를 남깁니다" in readme
    assert "X-Api-Key" in readme
    assert "production 사용 권한" in readme
    for job_name in (
        "newsapi:ai-models-agents",
        "newsapi:developer-open-source",
        "newsapi:inference-infrastructure",
        "newsapi:robotics-edge-security",
        "blockchain-crypto",
    ):
        assert job_name in readme


def test_readme_documents_the_crypto_quarantine_and_both_gates():
    """The quarantine only holds if both map_queue entry paths are gated, so
    the runbook must name both."""
    readme = (SERVER_ROOT / "README.md").read_text(encoding="utf-8")

    assert "coverage_domain" in readme
    assert "upsert_article" in readme
    assert "reconcile_map_queue" in readme
    assert "WHERE a.coverage_domain = 'core'" in readme


def test_named_cli_and_api_are_provider_common_but_adhoc_query_stays_gdelt():
    cli = (SERVER_ROOT / "app" / "cli.py").read_text(encoding="utf-8")
    api = (SERVER_ROOT / "app" / "api.py").read_text(encoding="utf-8")

    assert "single GDELT or NewsAPI job by name" in cli
    assert 'help="ad-hoc GDELT query"' in cli
    assert "run one named GDELT or NewsAPI job" in api
    assert 'description="ad-hoc GDELT query"' in api
