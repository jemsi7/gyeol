from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from app.recommendation import Daypart, RecommendationSurface
from app.recommendation_cursor import (
    InvalidRecommendationCursor,
    RecommendationCursor,
    decode_recommendation_cursor,
    encode_recommendation_cursor,
    recommendation_session_scope,
)


NOW = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)


def _cursor() -> RecommendationCursor:
    return RecommendationCursor(
        surface=RecommendationSurface.HOME,
        snapshot_at=NOW,
        session_scope=recommendation_session_scope(
            b"x" * 32,
            snapshot_at=NOW,
            ranking_version="content-v1",
        ),
        last_score_micros=712_345,
        last_published_at=NOW,
        last_article_id="a" * 64,
        daypart=Daypart.AFTERNOON,
    )


def test_recommendation_cursor_roundtrips_canonical_session_scoped_state():
    cursor = _cursor()

    encoded = encode_recommendation_cursor(cursor)
    decoded = decode_recommendation_cursor(encoded)

    assert decoded == cursor
    assert encoded == encode_recommendation_cursor(decoded)
    assert len(encoded) <= 2048
    assert recommendation_session_scope(
        b"y" * 32,
        snapshot_at=NOW,
        ranking_version="content-v1",
    ) != cursor.session_scope


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "!" * 12,
        "a" * 2049,
        base64.urlsafe_b64encode(b"{}").decode(),
    ],
)
def test_recommendation_cursor_rejects_corruption(raw):
    with pytest.raises(InvalidRecommendationCursor):
        decode_recommendation_cursor(raw)


def test_recommendation_cursor_rejects_unknown_or_identity_fields():
    cursor = _cursor()
    payload = json.loads(
        base64.urlsafe_b64decode(
            encode_recommendation_cursor(cursor)
            + "=" * (-len(encode_recommendation_cursor(cursor)) % 4)
        )
    )
    payload["account_id"] = "secret"
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()

    with pytest.raises(InvalidRecommendationCursor):
        decode_recommendation_cursor(raw)


def test_boundary_cursor_requires_context_article():
    cursor = _cursor()
    boundary = RecommendationCursor(
        surface=RecommendationSurface.ARTICLE_BOUNDARY,
        snapshot_at=cursor.snapshot_at,
        session_scope=cursor.session_scope,
        last_score_micros=cursor.last_score_micros,
        last_published_at=cursor.last_published_at,
        last_article_id=cursor.last_article_id,
        daypart=cursor.daypart,
        context_article_id=None,
    )
    with pytest.raises(ValueError):
        encode_recommendation_cursor(boundary)
