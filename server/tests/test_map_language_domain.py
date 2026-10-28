"""Per-domain output language (I-13201-a, DR-13201).

Crypto sources are English while the product stays a Korean reader, so the
crypto domain pins its output language instead of following the source. Every
other domain keeps the global setting untouched -- the whole point of the
domain axis is that opening crypto does not move existing coverage.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.engine import _RowArticle


def _settings(**env) -> Settings:
    return Settings(_env_file=None, **env)


def test_crypto_pins_korean_by_default():
    assert _settings().map_language_for("crypto") == "ko"


def test_other_domains_keep_the_global_value():
    settings = _settings()
    assert settings.map_language == "auto"
    assert settings.map_language_for("core") == "auto"


def test_unknown_domain_takes_the_global_value():
    # A domain that has not declared a policy must not inherit another one's.
    settings = _settings()
    for domain in ("", "sports", "unknown"):
        assert settings.map_language_for(domain) == settings.map_language


def test_domain_match_ignores_case_and_padding():
    settings = _settings()
    assert settings.map_language_for("  CRYPTO ") == "ko"


def test_crypto_language_is_operable_without_a_deploy():
    # Reverting to "auto" has to be a setting change, not a code change.
    settings = _settings(MAP_LANGUAGE_CRYPTO="auto")
    assert settings.map_language_for("crypto") == "auto"


def test_global_override_does_not_leak_into_crypto():
    settings = _settings(MAP_LANGUAGE="en")
    assert settings.map_language_for("core") == "en"
    assert settings.map_language_for("crypto") == "ko"


def test_mapper_view_defaults_to_core():
    # Callers that predate DR-13200 must keep the previous behaviour.
    article = _RowArticle(
        id="a",
        url="https://example.com/a",
        title="t",
        domain="example.com",
        socialimage_url=None,
    )
    assert article.coverage_domain == "core"
    assert _settings().map_language_for(article.coverage_domain) == "auto"


def test_mapper_asks_per_article_not_per_process():
    """Both build_source_packet calls must read the article's own domain.

    Reading ``settings.map_language`` directly -- as the code did before
    DR-13201 -- would give every article the global language regardless of
    which lane collected it, which is exactly the bug this unit removes.
    """
    import inspect

    from app.mapper import Mapper

    body = inspect.getsource(Mapper._map_one)
    assert body.count("output_language=self.s.map_language_for(") == 2
    assert 'getattr(article, "coverage_domain", "core")' in body
    assert "output_language=self.s.map_language," not in body
