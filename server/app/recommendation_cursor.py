"""Opaque, session-scoped pagination state for account recommendations."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .recommendation import (
    ARTICLE_ID_PATTERN,
    RANKING_VERSION,
    Daypart,
    RecommendationSortKey,
    RecommendationSurface,
)


class InvalidRecommendationCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RecommendationCursor:
    surface: RecommendationSurface
    snapshot_at: datetime
    session_scope: str
    last_score_micros: int
    last_published_at: datetime
    last_article_id: str
    daypart: Daypart
    ranking_version: str = RANKING_VERSION
    context_article_id: str | None = None
    version: int = 1

    @property
    def sort_key(self) -> RecommendationSortKey:
        return RecommendationSortKey(
            score_micros=self.last_score_micros,
            published_at=self.last_published_at,
            article_id=self.last_article_id,
        )


def recommendation_session_scope(
    token_hash: bytes, *, snapshot_at: datetime, ranking_version: str
) -> str:
    if len(token_hash) != 32:
        raise ValueError("session token hash must contain 32 bytes")
    instant = _aware_utc(snapshot_at)
    material = (
        token_hash
        + instant.isoformat().replace("+00:00", "Z").encode("ascii")
        + ranking_version.encode("ascii")
    )
    return hashlib.sha256(material).hexdigest()[:16]


def encode_recommendation_cursor(cursor: RecommendationCursor) -> str:
    _validate_cursor(cursor)
    payload = {
        "context_article_id": cursor.context_article_id,
        "daypart": cursor.daypart.value,
        "last_article_id": cursor.last_article_id,
        "last_published_at": _format_datetime(cursor.last_published_at),
        "last_score_micros": cursor.last_score_micros,
        "ranking_version": cursor.ranking_version,
        "session_scope": cursor.session_scope,
        "snapshot_at": _format_datetime(cursor.snapshot_at),
        "surface": cursor.surface.value,
        "v": cursor.version,
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(encoded) > 2048:
        raise ValueError("recommendation cursor is too large")
    return encoded


def decode_recommendation_cursor(raw: str | None) -> RecommendationCursor | None:
    if raw is None:
        return None
    if not raw or len(raw) > 2048:
        raise InvalidRecommendationCursor("invalid_recommendation_cursor")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or set(payload) != {
            "context_article_id",
            "daypart",
            "last_article_id",
            "last_published_at",
            "last_score_micros",
            "ranking_version",
            "session_scope",
            "snapshot_at",
            "surface",
            "v",
        }:
            raise ValueError
        context_article_id = payload["context_article_id"]
        if context_article_id is not None and (
            not isinstance(context_article_id, str)
            or ARTICLE_ID_PATTERN.fullmatch(context_article_id) is None
        ):
            raise ValueError
        cursor = RecommendationCursor(
            version=payload["v"],
            surface=RecommendationSurface(payload["surface"]),
            snapshot_at=_parse_datetime(payload["snapshot_at"]),
            session_scope=payload["session_scope"],
            last_score_micros=payload["last_score_micros"],
            last_published_at=_parse_datetime(payload["last_published_at"]),
            last_article_id=payload["last_article_id"],
            daypart=Daypart(payload["daypart"]),
            ranking_version=payload["ranking_version"],
            context_article_id=context_article_id,
        )
        _validate_cursor(cursor)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise InvalidRecommendationCursor("invalid_recommendation_cursor") from exc
    return cursor


def _validate_cursor(cursor: RecommendationCursor) -> None:
    if cursor.version != 1 or cursor.ranking_version != RANKING_VERSION:
        raise ValueError("unsupported recommendation cursor version")
    _aware_utc(cursor.snapshot_at)
    _aware_utc(cursor.last_published_at)
    if (
        not isinstance(cursor.session_scope, str)
        or len(cursor.session_scope) != 16
        or any(character not in "0123456789abcdef" for character in cursor.session_scope)
    ):
        raise ValueError("invalid recommendation session scope")
    if isinstance(cursor.last_score_micros, bool) or not isinstance(
        cursor.last_score_micros, int
    ):
        raise ValueError("invalid recommendation score")
    if not -250_000 <= cursor.last_score_micros <= 1_000_000:
        raise ValueError("recommendation score is out of range")
    if ARTICLE_ID_PATTERN.fullmatch(cursor.last_article_id) is None:
        raise ValueError("invalid recommendation article id")
    if (
        cursor.surface is RecommendationSurface.ARTICLE_BOUNDARY
        and cursor.context_article_id is None
    ):
        raise ValueError("article boundary cursor requires context")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(parsed)


def _format_datetime(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    return value.astimezone(UTC)
