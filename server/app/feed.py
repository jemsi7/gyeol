"""Article-map feed cursor and response construction."""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .config import Settings
from .map_loader import resolve_map

ARTICLE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SEOUL = ZoneInfo("Asia/Seoul")
AUTOMATIC_DISPLAY_ORDER = 2_147_483_647
SUPPORTED_DOCUMENT_TYPES = frozenset(
    {
        "governance_proposal",
        "post_mortem",
        "audit_report",
        "regulatory_filing",
        "foundation_notice",
    }
)


class InvalidFeedCursor(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeedCursor:
    sort_at: datetime
    article_id: str
    editorial_day: date | None = None
    priority_bucket: int = 1
    featured_bucket: int = 1
    order_bucket: int = 1
    display_order: int = AUTOMATIC_DISPLAY_ORDER


def seoul_editorial_day(now: datetime | None = None) -> date:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(SEOUL).date()


def feed_order_key(row: dict) -> tuple[int, int, int, int, float, int]:
    """Python mirror of the PostgreSQL mixed-direction editorial feed key."""
    sort_at = row.get("sort_at")
    if not isinstance(sort_at, datetime):
        sort_at = datetime.min.replace(tzinfo=UTC)
    elif sort_at.tzinfo is None:
        sort_at = sort_at.replace(tzinfo=UTC)
    article_id = str(row.get("article_id") or "")
    article_number = int(article_id, 16) if ARTICLE_ID_RE.fullmatch(article_id) else 0
    return (
        int(row.get("priority_bucket", 1)),
        int(row.get("featured_bucket", 1)),
        int(row.get("order_bucket", 1)),
        int(row.get("cursor_display_order", AUTOMATIC_DISPLAY_ORDER)),
        -sort_at.astimezone(UTC).timestamp(),
        -article_number,
    )


def encode_cursor(cursor: FeedCursor) -> str:
    sort_at = cursor.sort_at
    if sort_at.tzinfo is None:
        sort_at = sort_at.replace(tzinfo=UTC)
    payload: dict = {
        "article_id": cursor.article_id,
        "sort_at": sort_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "v": 1,
    }
    if cursor.editorial_day is not None:
        payload = {
            **payload,
            "v": 2,
            "editorial_day": cursor.editorial_day.isoformat(),
            "priority_bucket": cursor.priority_bucket,
            "featured_bucket": cursor.featured_bucket,
            "order_bucket": cursor.order_bucket,
            "display_order": cursor.display_order,
        }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def decode_cursor(raw: str | None) -> FeedCursor | None:
    if raw is None:
        return None
    if not raw or len(raw) > 1024:
        raise InvalidFeedCursor("invalid_cursor")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or payload.get("v") not in {1, 2}:
            raise ValueError
        version = payload["v"]
        article_id = payload["article_id"]
        if not isinstance(article_id, str) or not ARTICLE_ID_RE.fullmatch(article_id):
            raise ValueError
        sort_raw = payload["sort_at"]
        if not isinstance(sort_raw, str):
            raise ValueError
        sort_at = datetime.fromisoformat(sort_raw.replace("Z", "+00:00"))
        if sort_at.tzinfo is None:
            raise ValueError
        if version == 2:
            editorial_day = date.fromisoformat(payload["editorial_day"])
            priority_bucket = payload["priority_bucket"]
            featured_bucket = payload["featured_bucket"]
            order_bucket = payload["order_bucket"]
            display_order = payload["display_order"]
            if (
                priority_bucket not in {0, 1}
                or featured_bucket not in {0, 1}
                or order_bucket not in {0, 1}
                or isinstance(display_order, bool)
                or not isinstance(display_order, int)
                or not 0 <= display_order <= AUTOMATIC_DISPLAY_ORDER
            ):
                raise ValueError
        else:
            editorial_day = None
            priority_bucket = featured_bucket = order_bucket = 1
            display_order = AUTOMATIC_DISPLAY_ORDER
    except (KeyError, ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidFeedCursor("invalid_cursor") from exc
    return FeedCursor(
        sort_at=sort_at.astimezone(UTC),
        article_id=article_id,
        editorial_day=editorial_day,
        priority_bucket=priority_bucket,
        featured_bucket=featured_bucket,
        order_bucket=order_bucket,
        display_order=display_order,
    )


def build_feed_page(
    rows: list[dict],
    *,
    limit: int,
    settings: Settings,
    request_base_url: str,
    editorial_day: date | None = None,
) -> tuple[list[dict], str | None]:
    ordered_rows = sorted(rows, key=feed_order_key)
    selected = ordered_rows[:limit]
    has_more = len(rows) > limit
    items: list[dict] = []
    for row in selected:
        item = build_feed_item(
            row, settings=settings, request_base_url=request_base_url
        )
        if item is not None:
            items.append(item)
    next_cursor = None
    if has_more and selected:
        last = selected[-1]
        next_cursor = encode_cursor(
            FeedCursor(
                sort_at=last["sort_at"],
                article_id=last["article_id"],
                editorial_day=(
                    editorial_day
                    or last.get("editorial_day")
                    or seoul_editorial_day()
                ),
                priority_bucket=int(last.get("priority_bucket", 1)),
                featured_bucket=int(last.get("featured_bucket", 1)),
                order_bucket=int(last.get("order_bucket", 1)),
                display_order=int(
                    last.get("cursor_display_order", AUTOMATIC_DISPLAY_ORDER)
                ),
            )
        )
    return items, next_cursor


def build_feed_item(
    row: dict, *, settings: Settings, request_base_url: str
) -> dict | None:
    """Resolve one stored Map into the shared app-facing FeedItem shape."""
    map_data = row.get("data")
    if not isinstance(map_data, dict):
        return None
    public_base_url = _public_base_url(settings.public_base_url, request_base_url)

    def image_url_for(image_id: str) -> str:
        return f"{public_base_url}/images/{image_id}"

    raw_document_type = str(row.get("document_type") or "").strip()
    document_type = (
        raw_document_type
        if raw_document_type in SUPPORTED_DOCUMENT_TYPES
        else ""
    )
    source_publisher = (
        str(row.get("document_source_name") or "").strip()
        if document_type
        else ""
    )

    return {
        "article_id": row["article_id"],
        "source_url": row.get("source_url"),
        "domain": row.get("domain"),
        "published_at": row.get("published_at"),
        "source_language": row.get("source_language"),
        "source_country": row.get("sourcecountry"),
        "document_type": document_type,
        "source_publisher": source_publisher,
        "status": row.get("status"),
        "map": resolve_map(map_data, image_url_for),
    }


def _public_base_url(configured: str, request_base_url: str) -> str:
    value = (configured or request_base_url).strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("invalid public base URL")
    return value
