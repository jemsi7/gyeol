from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_c10007_article_embedding_defaults_are_bounded():
    settings = Settings(_env_file=None)

    assert settings.article_embedding_enabled is True
    assert settings.article_embedding_poll_seconds == 5.0
    assert settings.article_embedding_sync_limit == 50
    assert settings.article_embedding_batch_size == 16
    assert settings.article_embedding_max_attempts == 5
    assert settings.article_embedding_requests_per_minute == 12
    assert settings.article_embedding_source_max_chars == 12_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ARTICLE_EMBEDDING_POLL_SECONDS", 0.1),
        ("ARTICLE_EMBEDDING_SYNC_LIMIT", 51),
        ("ARTICLE_EMBEDDING_BATCH_SIZE", 17),
        ("ARTICLE_EMBEDDING_MAX_ATTEMPTS", 0),
        ("ARTICLE_EMBEDDING_REQUESTS_PER_MINUTE", 0),
        ("ARTICLE_EMBEDDING_SOURCE_MAX_CHARS", 12_001),
    ],
)
def test_c10007_article_embedding_settings_reject_unbounded_values(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
