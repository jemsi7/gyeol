"""Persistence for articles, images and run audit records.

Upsert semantics:
  * articles  -> INSERT ... ON CONFLICT (id) DO UPDATE (refreshes mutable
    fields, preserves first_seen_at). Return value tells us insert vs update
    so the run audit can report real deltas.
  * images    -> INSERT by sha256. A verified upload may promote an existing
    db/local row to S3 metadata-only, but a later non-S3 write never downgrades
    an S3 row or restores bytea.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from typing import Any, Final, Iterable, Mapping, Optional, Sequence
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

import asyncpg

from .db import Database
from .models import Article, StoredImage
from .recommendation import (
    ArticleRecommendationFeature,
    extract_article_recommendation_feature,
)
from .trending_search import (
    TRENDING_METRIC_ORDER,
    TrendingArticle,
    TrendingMetric,
    TrendingSearchSnapshot,
    build_keyword_pool,
    rank_category_keywords,
    sample_keyword_pool,
    trending_bucket_started_at,
)

log = logging.getLogger("gdelt.repo")

MAP_BUDGET_BUCKETS: Final = ("fresh", "backlog")
SEOUL_TIME_ZONE: Final = ZoneInfo("Asia/Seoul")
_DAY_MICROSECONDS: Final = 86_400_000_000
_MAP_EVENT_SECRET_RE = re.compile(
    r"(?i)(?:"
    r"Authorization\s*[:=]\s*(?:Bearer\s+)?\S+"
    r"|Bearer\s+\S+"
    r"|sk-or-[A-Za-z0-9_-]+"
    r")"
)
_MAP_EVENT_URL_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?[^\s]+")


def _sanitized_map_event_detail(value: str | None) -> str | None:
    if not value:
        return None
    text = _MAP_EVENT_SECRET_RE.sub("[redacted]", str(value))
    text = _MAP_EVENT_URL_QUERY_RE.sub(r"\1?[redacted]", text)
    return text[:1000]


def _bounded_map_event_metadata(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    bounded: dict[str, object] = {}
    for key, item in list(value.items())[:20]:
        name = str(key)[:80]
        if item is None or isinstance(item, (bool, int, float)):
            bounded[name] = item
        elif isinstance(item, str):
            bounded[name] = _sanitized_map_event_detail(item[:300])
    return bounded


@dataclass(frozen=True, slots=True)
class MapBudget:
    """One editorial day's Map conversion allowance and its fresh/backlog split."""

    daily_limit: int
    fresh_share_percent: int
    daily_run_count: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.daily_limit <= 1000:
            raise ValueError("daily_limit must be between 0 and 1000")
        if not 0 <= self.fresh_share_percent <= 100:
            raise ValueError("fresh_share_percent must be between 0 and 100")
        if not 1 <= self.daily_run_count <= 24:
            raise ValueError("daily_run_count must be between 1 and 24")


class EmptyChainRollback(Exception):
    """Abort an event-chain transaction that would leave a chain of one."""


@dataclass(frozen=True, slots=True)
class DomainBudget:
    """One coverage domain's share of the daily Map allowance (DR-13206).

    Unused allowance never moves between domains, so each domain carries its
    own limit and is charged against its own ledger rows.
    """

    coverage_domain: str
    daily_limit: int
    document_map_limit: int = 0

    def __post_init__(self) -> None:
        if not self.coverage_domain.strip():
            raise ValueError("coverage_domain must not be blank")
        if not 0 <= self.daily_limit <= 1000:
            raise ValueError("daily_limit must be between 0 and 1000")
        if not 0 <= self.document_map_limit <= 1000:
            raise ValueError("document_map_limit must be between 0 and 1000")

    @property
    def effective_document_map_limit(self) -> int:
        return min(self.daily_limit, self.document_map_limit)

    @property
    def article_map_limit(self) -> int:
        return self.daily_limit - self.effective_document_map_limit


def order_domains_by_fill(
    domains: Sequence[DomainBudget],
    consumed: Mapping[str, int],
) -> tuple[DomainBudget, ...]:
    """Order spendable domains by how little of their share they have used.

    Comparing ``consumed / limit`` by cross-multiplication keeps the domains
    interleaved through the day instead of letting one spend its whole share
    before the other starts. Domains with a zero limit are dropped: a zero
    share is the operator's way of holding a domain back, not a tie.
    """
    spendable = [item for item in domains if item.daily_limit > 0]
    return tuple(
        sorted(
            spendable,
            key=lambda item: (
                Fraction(consumed.get(item.coverage_domain, 0), item.daily_limit),
                item.coverage_domain,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class MapBudgetWindow:
    """The cumulative Map allowance open in one Seoul editorial-day run."""

    editorial_day: date
    current_run: int
    run_count: int
    current_run_allowance: int
    current_allowance: int
    next_run_at: datetime | None


def map_budget_window(budget: MapBudget, now: datetime) -> MapBudgetWindow:
    """Return the Seoul run and cumulative allowance open at ``now``.

    Integer microsecond arithmetic makes every boundary deterministic. The
    cumulative floor formula carries unused allowance forward and assigns any
    indivisible remainder to later runs while still summing to the daily cap.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(SEOUL_TIME_ZONE)
    elapsed_microseconds = (
        ((local_now.hour * 60 + local_now.minute) * 60 + local_now.second)
        * 1_000_000
        + local_now.microsecond
    )
    run_index = min(
        budget.daily_run_count - 1,
        elapsed_microseconds * budget.daily_run_count // _DAY_MICROSECONDS,
    )
    current_run = run_index + 1
    previous_allowance = budget.daily_limit * run_index // budget.daily_run_count
    current_allowance = (
        budget.daily_limit * current_run // budget.daily_run_count
    )
    next_run_at: datetime | None = None
    if current_run < budget.daily_run_count:
        next_boundary_microseconds = (
            _DAY_MICROSECONDS * current_run + budget.daily_run_count - 1
        ) // budget.daily_run_count
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_run_at = midnight + timedelta(
            microseconds=next_boundary_microseconds
        )
    return MapBudgetWindow(
        editorial_day=local_now.date(),
        current_run=current_run,
        run_count=budget.daily_run_count,
        current_run_allowance=current_allowance - previous_allowance,
        current_allowance=current_allowance,
        next_run_at=next_run_at,
    )


def bucket_quotas(limit: int, share: int) -> tuple[int, int]:
    """Split one day's limit into (fresh, backlog); backlog takes the remainder."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    if not 0 <= share <= 100:
        raise ValueError("share must be between 0 and 100")
    fresh = limit * share // 100
    return fresh, limit - fresh


def preferred_bucket(
    fresh_quota: int,
    fresh_consumed: int,
    backlog_quota: int,
    backlog_consumed: int,
) -> Optional[str]:
    """Return the bucket with room and the lower fill ratio, or ``None``.

    Comparing ``consumed / quota`` by cross-multiplication keeps the two shares
    interleaved through the day instead of spending all of one bucket first.
    """
    fresh_room = fresh_consumed < fresh_quota
    backlog_room = backlog_consumed < backlog_quota
    if fresh_room and backlog_room:
        if fresh_consumed * backlog_quota <= backlog_consumed * fresh_quota:
            return "fresh"
        return "backlog"
    if fresh_room:
        return "fresh"
    if backlog_room:
        return "backlog"
    return None


DEFAULT_ACCOUNT_READER_SETTINGS = {
    "type_size": "standard",
    "leading": "comfortable",
    "theme": "light",
    "motion_enabled": True,
    "media_saver": False,
    "offline_cache": True,
}


class Repository:
    def __init__(self, db: Database):
        self.db = db

    @property
    def pool(self) -> asyncpg.Pool:
        assert self.db.pool is not None, "Database not connected"
        return self.db.pool

    # ---- editorial operators ------------------------------------------
    async def create_editor_operator(
        self, *, display_name: str, role: str
    ) -> tuple[dict, str]:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()
        operator_id = uuid4()
        row = await self.pool.fetchrow(
            """
            WITH created AS (
                INSERT INTO editor_operators
                    (id, display_name, role, token_hash, token_prefix)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING id, display_name, role, status, created_at,
                          last_used_at, revoked_at
            ), audited AS (
                INSERT INTO editorial_audit_log
                    (operator_id, action, target_type, target_id, metadata)
                SELECT id, 'operator.created', 'operator', id::text,
                       jsonb_build_object('role', role)
                FROM created
            )
            SELECT * FROM created
            """,
            operator_id,
            display_name,
            role,
            token_hash,
            raw_token[:8],
        )
        if row is None:
            raise RuntimeError("editor operator insert returned no row")
        return dict(row), raw_token

    async def authenticate_editor_token(self, token_hash: bytes) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            UPDATE editor_operators
            SET last_used_at=now()
            WHERE token_hash=$1 AND status='active'
            RETURNING id, display_name, role, status, created_at,
                      last_used_at, revoked_at
            """,
            token_hash,
        )
        return dict(row) if row else None

    async def revoke_editor_operator(self, operator_id: UUID) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            WITH revoked AS (
                UPDATE editor_operators
                SET status='revoked', revoked_at=COALESCE(revoked_at, now())
                WHERE id=$1
                RETURNING id, display_name, role, status, created_at,
                          last_used_at, revoked_at
            ), audited AS (
                INSERT INTO editorial_audit_log
                    (operator_id, action, target_type, target_id, metadata)
                SELECT id, 'operator.revoked', 'operator', id::text, '{}'::jsonb
                FROM revoked
                WHERE status='revoked'
            )
            SELECT * FROM revoked
            """,
            operator_id,
        )
        return dict(row) if row else None

    # ---- editorial drafts ---------------------------------------------
    @staticmethod
    def _decode_editorial_row(row) -> dict:
        item = dict(row)
        if isinstance(item.get("document"), str):
            item["document"] = json.loads(item["document"])
        if isinstance(item.get("map_data"), str):
            item["map_data"] = json.loads(item["map_data"])
        if isinstance(item.get("image_links"), str):
            item["image_links"] = json.loads(item["image_links"])
        return item

    async def create_editorial_draft(
        self, *, document: dict, operator_id: UUID
    ) -> dict:
        draft_id = uuid4()
        row = await self.pool.fetchrow(
            """
            WITH created AS (
                INSERT INTO editorial_drafts
                    (id, document, created_by, updated_by)
                VALUES ($1,$2::jsonb,$3,$3)
                RETURNING *
            ), audited AS (
                INSERT INTO editorial_audit_log
                    (operator_id, action, target_type, target_id, metadata)
                SELECT $3, 'draft.created', 'draft', id::text,
                       jsonb_build_object('version', version)
                FROM created
            )
            SELECT * FROM created
            """,
            draft_id,
            json.dumps(document, ensure_ascii=False),
            operator_id,
        )
        if row is None:
            raise RuntimeError("editorial draft insert returned no row")
        return self._decode_editorial_row(row)

    async def editorial_drafts(self, limit: int = 100) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM editorial_drafts
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC, id DESC
            LIMIT $1
            """,
            limit,
        )
        return [self._decode_editorial_row(row) for row in rows]

    async def get_editorial_draft(self, draft_id: UUID) -> Optional[dict]:
        row = await self.pool.fetchrow(
            "SELECT * FROM editorial_drafts WHERE id=$1 AND deleted_at IS NULL",
            draft_id,
        )
        return self._decode_editorial_row(row) if row else None

    async def update_editorial_draft(
        self,
        *,
        draft_id: UUID,
        expected_version: int,
        document: dict,
        operator_id: UUID,
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            WITH updated AS (
                UPDATE editorial_drafts
                SET document=$3::jsonb,
                    version=version+1,
                    updated_by=$4,
                    updated_at=now()
                WHERE id=$1 AND version=$2 AND deleted_at IS NULL
                RETURNING *
            ), audited AS (
                INSERT INTO editorial_audit_log
                    (operator_id, action, target_type, target_id, metadata)
                SELECT $4, 'draft.updated', 'draft', id::text,
                       jsonb_build_object('version', version)
                FROM updated
            )
            SELECT * FROM updated
            """,
            draft_id,
            expected_version,
            json.dumps(document, ensure_ascii=False),
            operator_id,
        )
        return self._decode_editorial_row(row) if row else None

    async def editorial_trashed_drafts(self, limit: int = 100) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM editorial_drafts
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC, id DESC
            LIMIT $1
            """,
            limit,
        )
        return [self._decode_editorial_row(row) for row in rows]

    async def trash_editorial_draft(
        self,
        *,
        draft_id: UUID,
        expected_version: int,
        operator_id: UUID,
    ) -> dict:
        cancelled_schedule = None
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked = await connection.fetchrow(
                    "SELECT * FROM editorial_drafts WHERE id=$1 FOR UPDATE",
                    draft_id,
                )
                if locked is None:
                    return {"kind": "not_found"}
                current = self._decode_editorial_row(locked)
                if int(current["version"]) != expected_version:
                    return {"kind": "conflict", "current": current}
                if current.get("deleted_at") is not None or current["status"] != "draft":
                    return {"kind": "not_eligible", "current": current}

                has_history = bool(
                    await connection.fetchval(
                        """
                        SELECT
                            EXISTS(SELECT 1 FROM editorial_revisions WHERE draft_id=$1)
                            OR EXISTS(SELECT 1 FROM editorial_publications WHERE draft_id=$1)
                        """,
                        draft_id,
                    )
                )
                if has_history:
                    return {"kind": "not_eligible", "current": current}

                active_schedule = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_schedules
                    WHERE draft_id=$1 AND state IN ('scheduled','processing')
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if active_schedule is not None and active_schedule["state"] == "processing":
                    return {
                        "kind": "schedule_processing",
                        "current": current,
                    }
                if active_schedule is not None:
                    cancelled_schedule = await connection.fetchrow(
                        """
                        UPDATE editorial_schedules
                        SET state='cancelled', version=version+1, updated_by=$2,
                            lease_expires_at=NULL, cancelled_at=now(), updated_at=now()
                        WHERE id=$1 AND state='scheduled'
                        RETURNING *
                        """,
                        active_schedule["id"],
                        operator_id,
                    )
                    if cancelled_schedule is None:
                        raise RuntimeError("scheduled draft cancellation returned no row")

                trashed = await connection.fetchrow(
                    """
                    UPDATE editorial_drafts
                    SET deleted_at=now(), deleted_by=$3, version=version+1,
                        updated_by=$3, updated_at=now()
                    WHERE id=$1 AND version=$2 AND deleted_at IS NULL
                    RETURNING *
                    """,
                    draft_id,
                    expected_version,
                    operator_id,
                )
                if trashed is None:
                    raise RuntimeError("editorial draft trash update returned no row")
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,'draft.trashed','draft',$2,
                            jsonb_strip_nulls(jsonb_build_object(
                                'draft_version',$3::bigint,
                                'cancelled_schedule_id',$4::text,
                                'cancelled_schedule_version',$5::bigint)))
                    """,
                    operator_id,
                    str(draft_id),
                    int(trashed["version"]),
                    str(cancelled_schedule["id"]) if cancelled_schedule else None,
                    int(cancelled_schedule["version"]) if cancelled_schedule else None,
                )
        result = {"kind": "trashed", "draft": self._decode_editorial_row(trashed)}
        if cancelled_schedule is not None:
            result["schedule"] = self._decode_editorial_row(cancelled_schedule)
        return result

    async def restore_editorial_draft(
        self,
        *,
        draft_id: UUID,
        expected_version: int,
        operator_id: UUID,
    ) -> dict:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_drafts
                    WHERE id=$1 AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if locked is None:
                    return {"kind": "not_found"}
                current = self._decode_editorial_row(locked)
                if int(current["version"]) != expected_version:
                    return {"kind": "conflict", "current": current}
                if current.get("deleted_at") is None:
                    return {"kind": "not_trashed", "current": current}
                restored = await connection.fetchrow(
                    """
                    UPDATE editorial_drafts
                    SET deleted_at=NULL, deleted_by=NULL, version=version+1,
                        updated_by=$3, updated_at=now()
                    WHERE id=$1 AND version=$2 AND deleted_at IS NOT NULL
                    RETURNING *
                    """,
                    draft_id,
                    expected_version,
                    operator_id,
                )
                if restored is None:
                    return {"kind": "conflict", "current": current}
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,'draft.restored','draft',$2,
                            jsonb_build_object('draft_version',$3::bigint))
                    """,
                    operator_id,
                    str(draft_id),
                    int(restored["version"]),
                )
        return {"kind": "restored", "draft": self._decode_editorial_row(restored)}

    async def editorial_image_records(self, image_ids: list[str]) -> list[dict]:
        """Return existing image metadata in document order, once per id."""
        ordered_ids = list(dict.fromkeys(str(value) for value in image_ids if value))
        if not ordered_ids:
            return []
        rows = await self.pool.fetch(
            """
            SELECT id, source_url, content_type, byte_size, backend, storage_ref,
                   downloaded_at
            FROM article_images
            WHERE id = ANY($1::text[])
            ORDER BY array_position($1::text[], id)
            """,
            ordered_ids,
        )
        return [dict(row) for row in rows]

    async def store_editorial_image_metadata(
        self,
        *,
        image: StoredImage,
        operator_id: UUID,
        filename: str,
    ) -> None:
        """Upsert deduplicated image metadata and audit it in one DB statement."""
        await self.pool.execute(
            """
            WITH persisted AS (
                INSERT INTO article_images
                    (id, source_url, content_type, byte_size, backend, storage_ref, data)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (id) DO UPDATE SET
                    source_url = CASE WHEN EXCLUDED.backend='s3'
                                      THEN EXCLUDED.source_url
                                      ELSE article_images.source_url END,
                    content_type = COALESCE(article_images.content_type, EXCLUDED.content_type),
                    byte_size = article_images.byte_size,
                    backend = CASE WHEN EXCLUDED.backend='s3'
                                   THEN 's3' ELSE article_images.backend END,
                    storage_ref = CASE WHEN EXCLUDED.backend='s3'
                                       THEN EXCLUDED.storage_ref
                                       ELSE article_images.storage_ref END,
                    data = CASE WHEN EXCLUDED.backend='s3'
                                THEN NULL ELSE article_images.data END,
                    downloaded_at = CASE WHEN EXCLUDED.backend='s3'
                                         THEN now() ELSE article_images.downloaded_at END
                RETURNING id
            )
            INSERT INTO editorial_audit_log
                (operator_id, action, target_type, target_id, metadata)
            SELECT $8, 'asset.uploaded', 'image', id,
                   jsonb_build_object(
                       'filename', $9::text,
                       'content_type', $3::text,
                       'byte_size', $4::integer
                   )
            FROM persisted
            """,
            image.id,
            image.source_url,
            image.content_type,
            image.byte_size,
            image.backend,
            image.storage_ref,
            image.data,
            operator_id,
            filename,
        )

    # ---- editorial publication schedules -----------------------------
    async def editorial_schedules(self, limit: int = 100) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM (
                SELECT DISTINCT ON (schedule.draft_id) schedule.*
                FROM editorial_schedules schedule
                JOIN editorial_drafts draft ON draft.id=schedule.draft_id
                WHERE draft.deleted_at IS NULL
                ORDER BY schedule.draft_id, schedule.updated_at DESC, schedule.id DESC
            ) latest
            ORDER BY updated_at DESC, id DESC
            LIMIT $1
            """,
            limit,
        )
        return [self._decode_editorial_row(row) for row in rows]

    async def editorial_schedule(self, draft_id: UUID) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            SELECT schedule.* FROM editorial_schedules schedule
            JOIN editorial_drafts draft ON draft.id=schedule.draft_id
            WHERE schedule.draft_id=$1 AND draft.deleted_at IS NULL
            ORDER BY schedule.updated_at DESC, schedule.id DESC
            LIMIT 1
            """,
            draft_id,
        )
        return self._decode_editorial_row(row) if row else None

    async def upsert_editorial_schedule(
        self,
        *,
        draft_id: UUID,
        expected_draft_version: int,
        expected_schedule_version: int | None,
        scheduled_for: datetime,
        operator_id: UUID,
        document: dict,
        map_data: dict,
        image_links: list[dict],
    ) -> dict:
        schedule_id = uuid4()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked_draft = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_drafts
                    WHERE id=$1 AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if locked_draft is None:
                    return {"kind": "not_found"}
                current_draft = self._decode_editorial_row(locked_draft)
                if int(current_draft["version"]) != expected_draft_version:
                    return {"kind": "draft_conflict", "current": current_draft}

                active = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_schedules
                    WHERE draft_id=$1 AND state IN ('scheduled','processing')
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if active is not None:
                    current_schedule = self._decode_editorial_row(active)
                    if active["state"] != "scheduled":
                        return {
                            "kind": "schedule_not_mutable",
                            "current": current_schedule,
                        }
                    if expected_schedule_version != int(active["version"]):
                        return {
                            "kind": "schedule_conflict",
                            "current": current_schedule,
                        }
                    row = await connection.fetchrow(
                        """
                        UPDATE editorial_schedules
                        SET version=version+1,
                            scheduled_for=$3,
                            snapshot_draft_version=$4,
                            document=$5::jsonb,
                            map_data=$6::jsonb,
                            image_links=$7::jsonb,
                            updated_by=$8,
                            attempt_count=0,
                            next_attempt_at=$3,
                            lease_expires_at=NULL,
                            last_error=NULL,
                            cancelled_at=NULL,
                            published_at=NULL,
                            publication_revision_id=NULL,
                            updated_at=now()
                        WHERE id=$1 AND version=$2 AND state='scheduled'
                        RETURNING *
                        """,
                        active["id"],
                        expected_schedule_version,
                        scheduled_for,
                        expected_draft_version,
                        json.dumps(document, ensure_ascii=False),
                        json.dumps(map_data, ensure_ascii=False),
                        json.dumps(image_links, ensure_ascii=False),
                        operator_id,
                    )
                    action = "draft.rescheduled"
                else:
                    if expected_schedule_version is not None:
                        latest = await connection.fetchrow(
                            """
                            SELECT * FROM editorial_schedules
                            WHERE draft_id=$1
                            ORDER BY updated_at DESC, id DESC
                            LIMIT 1
                            """,
                            draft_id,
                        )
                        return {
                            "kind": "schedule_conflict",
                            "current": (
                                self._decode_editorial_row(latest) if latest else None
                            ),
                        }
                    row = await connection.fetchrow(
                        """
                        INSERT INTO editorial_schedules
                            (id,draft_id,state,scheduled_for,snapshot_draft_version,
                             document,map_data,image_links,created_by,updated_by,
                             next_attempt_at)
                        VALUES ($1,$2,'scheduled',$3,$4,$5::jsonb,$6::jsonb,
                                $7::jsonb,$8,$8,$3)
                        RETURNING *
                        """,
                        schedule_id,
                        draft_id,
                        scheduled_for,
                        expected_draft_version,
                        json.dumps(document, ensure_ascii=False),
                        json.dumps(map_data, ensure_ascii=False),
                        json.dumps(image_links, ensure_ascii=False),
                        operator_id,
                    )
                    action = "draft.scheduled"
                if row is None:
                    raise RuntimeError("editorial schedule write returned no row")
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,$2,'draft',$3,
                            jsonb_build_object('schedule_id',$4::text,
                                               'schedule_version',$5::bigint,
                                               'snapshot_draft_version',$6::bigint,
                                               'scheduled_for',$7::timestamptz))
                    """,
                    operator_id,
                    action,
                    str(draft_id),
                    str(row["id"]),
                    int(row["version"]),
                    expected_draft_version,
                    scheduled_for,
                )
        return {"kind": "scheduled", "schedule": self._decode_editorial_row(row)}

    async def cancel_editorial_schedule(
        self,
        *,
        draft_id: UUID,
        expected_schedule_version: int,
        operator_id: UUID,
    ) -> dict:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                draft = await connection.fetchrow(
                    """
                    SELECT id FROM editorial_drafts
                    WHERE id=$1 AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if draft is None:
                    return {"kind": "not_found"}
                active = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_schedules
                    WHERE draft_id=$1 AND state IN ('scheduled','processing')
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if active is None:
                    latest = await connection.fetchrow(
                        """
                        SELECT * FROM editorial_schedules
                        WHERE draft_id=$1
                        ORDER BY updated_at DESC, id DESC LIMIT 1
                        """,
                        draft_id,
                    )
                    return {
                        "kind": "schedule_not_active",
                        "current": self._decode_editorial_row(latest) if latest else None,
                    }
                current = self._decode_editorial_row(active)
                if int(active["version"]) != expected_schedule_version:
                    return {"kind": "schedule_conflict", "current": current}
                if active["state"] != "scheduled":
                    return {"kind": "schedule_not_mutable", "current": current}
                row = await connection.fetchrow(
                    """
                    UPDATE editorial_schedules
                    SET state='cancelled', version=version+1, updated_by=$3,
                        lease_expires_at=NULL, cancelled_at=now(), updated_at=now()
                    WHERE id=$1 AND version=$2 AND state='scheduled'
                    RETURNING *
                    """,
                    active["id"],
                    expected_schedule_version,
                    operator_id,
                )
                if row is None:
                    return {"kind": "schedule_conflict", "current": current}
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,'draft.schedule_cancelled','draft',$2,
                            jsonb_build_object('schedule_id',$3::text,
                                               'schedule_version',$4::bigint))
                    """,
                    operator_id,
                    str(draft_id),
                    str(row["id"]),
                    int(row["version"]),
                )
        return {"kind": "cancelled", "schedule": self._decode_editorial_row(row)}

    async def claim_due_editorial_schedules(
        self, *, limit: int = 10, lease_seconds: float = 60.0
    ) -> list[dict]:
        rows = await self.pool.fetch(
            """
            WITH due AS (
                SELECT id
                FROM editorial_schedules
                WHERE (
                    state='scheduled'
                    AND scheduled_for <= now()
                    AND next_attempt_at <= now()
                ) OR (
                    state='processing'
                    AND lease_expires_at <= now()
                )
                ORDER BY scheduled_for, created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            )
            UPDATE editorial_schedules schedule
            SET state='processing',
                attempt_count=attempt_count+1,
                lease_expires_at=now() + ($2 * interval '1 second'),
                updated_at=now()
            FROM due
            WHERE schedule.id=due.id
            RETURNING schedule.*
            """,
            limit,
            lease_seconds,
        )
        return [self._decode_editorial_row(row) for row in rows]

    async def retry_editorial_schedule(
        self,
        schedule_id: UUID,
        *,
        error_code: str,
        max_attempts: int = 5,
    ) -> dict:
        safe_error = (
            error_code
            if error_code in {"publication_unavailable", "publication_conflict"}
            else "publication_unavailable"
        )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM editorial_schedules WHERE id=$1 FOR UPDATE",
                    schedule_id,
                )
                if current is None:
                    return {"kind": "not_found"}
                if current["state"] != "processing":
                    return {
                        "kind": "terminal",
                        "schedule": self._decode_editorial_row(current),
                    }
                attempts = int(current["attempt_count"])
                terminal = attempts >= max_attempts
                delay = min(300.0, 15.0 * (2 ** max(0, attempts - 1)))
                row = await connection.fetchrow(
                    """
                    UPDATE editorial_schedules
                    SET state=$2,
                        next_attempt_at=CASE WHEN $2='failed' THEN now()
                                             ELSE now()+$3::interval END,
                        lease_expires_at=NULL,
                        last_error=$4,
                        updated_at=now()
                    WHERE id=$1 AND state='processing'
                    RETURNING *
                    """,
                    schedule_id,
                    "failed" if terminal else "scheduled",
                    timedelta(seconds=delay),
                    safe_error,
                )
        if row is None:
            return {"kind": "terminal", "schedule": self._decode_editorial_row(current)}
        return {
            "kind": "failed" if terminal else "retry",
            "schedule": self._decode_editorial_row(row),
        }

    async def publish_claimed_editorial_schedule(self, schedule_id: UUID) -> dict:
        current = await self.pool.fetchrow(
            "SELECT draft_id FROM editorial_schedules WHERE id=$1",
            schedule_id,
        )
        if current is None:
            return {"kind": "not_found"}
        return await self.publish_editorial_draft(
            draft_id=current["draft_id"],
            expected_version=None,
            operator_id=None,
            document=None,
            map_data=None,
            image_links=None,
            schedule_id=schedule_id,
        )

    async def publish_editorial_draft(
        self,
        *,
        draft_id: UUID,
        expected_version: int | None,
        operator_id: UUID | None,
        document: dict | None,
        map_data: dict | None,
        image_links: list[dict] | None,
        schedule_id: UUID | None = None,
    ) -> dict:
        """Atomically materialize one immutable editorial revision."""
        article_id = hashlib.sha256(f"editorial:{draft_id}".encode("utf-8")).hexdigest()
        revision_id = uuid4()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_drafts
                    WHERE id=$1 AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    draft_id,
                )
                if locked is None:
                    return {"kind": "not_found"}
                current = self._decode_editorial_row(locked)
                if schedule_id is None and int(current["version"]) != expected_version:
                    return {"kind": "conflict", "current": current}
                schedule = None
                if schedule_id is not None:
                    schedule = await connection.fetchrow(
                        """
                        SELECT * FROM editorial_schedules
                        WHERE id=$1 AND draft_id=$2
                        FOR UPDATE
                        """,
                        schedule_id,
                        draft_id,
                    )
                    if schedule is None:
                        return {"kind": "schedule_not_found"}
                    if schedule["state"] != "processing":
                        return {
                            "kind": "schedule_terminal",
                            "schedule": self._decode_editorial_row(schedule),
                        }
                    decoded_schedule = self._decode_editorial_row(schedule)
                    operator_id = schedule["updated_by"]
                    document = decoded_schedule["document"]
                    map_data = decoded_schedule["map_data"]
                    image_links = decoded_schedule["image_links"]

                if operator_id is None or document is None or map_data is None:
                    raise RuntimeError("editorial publication input is incomplete")
                image_links = image_links or []

                revision_number = int(
                    await connection.fetchval(
                        """
                        SELECT COALESCE(max(revision_number), 0) + 1
                        FROM editorial_revisions WHERE draft_id=$1
                        """,
                        draft_id,
                    )
                )
                cover_ref = map_data.get("cover_image_ref")
                cover_image_id = None
                if isinstance(cover_ref, int) and not isinstance(cover_ref, bool):
                    for link in image_links:
                        if link.get("ref_index") == cover_ref:
                            cover_image_id = link.get("image_id")
                            break

                await connection.execute(
                    """
                    INSERT INTO articles
                        (id, url, title, seendate, domain, language, sourcecountry,
                         socialimage_url, source_query, image_id, raw,
                         document_type, document_source_name, updated_at)
                    VALUES ($1,$2,$3,now(),'editorial',$4,'South Korea',NULL,
                            'editorial',$5,$6::jsonb,$7,$8,now())
                    ON CONFLICT (id) DO UPDATE SET
                        title=EXCLUDED.title,
                        seendate=EXCLUDED.seendate,
                        language=EXCLUDED.language,
                        image_id=EXCLUDED.image_id,
                        raw=EXCLUDED.raw,
                        document_type=EXCLUDED.document_type,
                        document_source_name=EXCLUDED.document_source_name,
                        updated_at=now()
                    """,
                    article_id,
                    f"editorial://{draft_id}",
                    str(document.get("headline") or ""),
                    str(document.get("language") or "ko"),
                    cover_image_id,
                    json.dumps(
                        {
                            "provider": "editorial",
                            "draft_id": str(draft_id),
                            "source_name": document.get("source_name") or "결 편집부",
                        },
                        ensure_ascii=False,
                    ),
                    # DR-13254: empty stays NULL so the row remains an ordinary
                    # article; feed.py only surfaces the document card when the
                    # type is set.
                    str(document.get("document_type") or "").strip() or None,
                    str(document.get("document_source_name") or "").strip() or None,
                )
                await connection.execute(
                    "DELETE FROM article_image_links WHERE article_id=$1",
                    article_id,
                )
                if image_links:
                    await connection.executemany(
                        """
                        INSERT INTO article_image_links
                            (article_id, ref_index, image_id, role, source_url)
                        VALUES ($1,$2,$3,$4,$5)
                        """,
                        [
                            (
                                article_id,
                                int(link["ref_index"]),
                                str(link["image_id"]),
                                "editorial",
                                str(link.get("source_url") or ""),
                            )
                            for link in image_links
                        ],
                    )
                await connection.execute(
                    """
                    INSERT INTO article_maps
                        (article_id, format_version, model, status, data, error, updated_at)
                    VALUES ($1,3,'manual-editor-v1','ok',$2::jsonb,NULL,now())
                    ON CONFLICT (article_id) DO UPDATE SET
                        format_version=3,
                        model='manual-editor-v1',
                        status='ok',
                        data=EXCLUDED.data,
                        error=NULL,
                        updated_at=now()
                    """,
                    article_id,
                    json.dumps(map_data, ensure_ascii=False),
                )
                await connection.execute(
                    """
                    INSERT INTO map_queue
                        (article_id,status,next_attempt_at,lease_expires_at,last_error,updated_at)
                    VALUES ($1,'done',now(),NULL,NULL,now())
                    ON CONFLICT (article_id) DO UPDATE SET
                        status='done', next_attempt_at=now(), lease_expires_at=NULL,
                        lease_owner=NULL, last_error=NULL, updated_at=now()
                    """,
                    article_id,
                )
                revision = await connection.fetchrow(
                    """
                    INSERT INTO editorial_revisions
                        (id,draft_id,revision_number,document,map_data,created_by)
                    VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6)
                    RETURNING *
                    """,
                    revision_id,
                    draft_id,
                    revision_number,
                    json.dumps(document, ensure_ascii=False),
                    json.dumps(map_data, ensure_ascii=False),
                    operator_id,
                )
                publication = await connection.fetchrow(
                    """
                    INSERT INTO editorial_publications
                        (draft_id,article_id,revision_id,state,published_at,published_by)
                    VALUES ($1,$2,$3,'published',now(),$4)
                    ON CONFLICT (draft_id) DO UPDATE SET
                        revision_id=EXCLUDED.revision_id,
                        state='published',
                        published_at=now(),
                        published_by=EXCLUDED.published_by,
                        withdrawn_at=NULL,
                        withdrawn_by=NULL,
                        updated_at=now()
                    RETURNING *
                    """,
                    draft_id,
                    article_id,
                    revision_id,
                    operator_id,
                )
                updated = await connection.fetchrow(
                    """
                    UPDATE editorial_drafts
                    SET status='published', version=version+1,
                        updated_by=$2, updated_at=now()
                    WHERE id=$1
                    RETURNING *
                    """,
                    draft_id,
                    operator_id,
                )
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,'draft.published','draft',$2,
                            jsonb_build_object('article_id',$3::text,
                                               'revision_number',$4::integer))
                    """,
                    operator_id,
                    str(draft_id),
                    article_id,
                    revision_number,
                )
                if schedule_id is not None:
                    completed_schedule = await connection.fetchrow(
                        """
                        UPDATE editorial_schedules
                        SET state='published', version=version+1,
                            publication_revision_id=$2,
                            lease_expires_at=NULL,
                            last_error=NULL,
                            published_at=now(),
                            updated_at=now()
                        WHERE id=$1 AND state='processing'
                        RETURNING *
                        """,
                        schedule_id,
                        revision_id,
                    )
                    if completed_schedule is None:
                        raise RuntimeError("editorial schedule completion returned no row")
                    await connection.execute(
                        """
                        INSERT INTO editorial_audit_log
                            (operator_id,action,target_type,target_id,metadata)
                        VALUES ($1,'draft.schedule_published','draft',$2,
                                jsonb_build_object('schedule_id',$3::text,
                                                   'revision_number',$4::integer))
                        """,
                        operator_id,
                        str(draft_id),
                        str(schedule_id),
                        revision_number,
                    )
                else:
                    await connection.execute(
                        """
                        WITH cancelled AS (
                            UPDATE editorial_schedules
                            SET state='cancelled', version=version+1,
                                updated_by=$2, lease_expires_at=NULL,
                                cancelled_at=now(), updated_at=now()
                            WHERE draft_id=$1
                              AND state IN ('scheduled','processing')
                            RETURNING id, version
                        )
                        INSERT INTO editorial_audit_log
                            (operator_id,action,target_type,target_id,metadata)
                        SELECT $2,'draft.schedule_cancelled_by_publish','draft',$3,
                               jsonb_build_object('schedule_id',id::text,
                                                  'schedule_version',version)
                        FROM cancelled
                        """,
                        draft_id,
                        operator_id,
                        str(draft_id),
                    )
                await self._enqueue_first_editorial_push(
                    connection,
                    notification_id=uuid4(),
                    draft_id=draft_id,
                    article_id=article_id,
                    revision_id=revision_id,
                    revision_number=revision_number,
                    document=document,
                )
        if revision is None or publication is None or updated is None:
            raise RuntimeError("editorial publish transaction returned no row")
        await self._best_effort_refresh_article_recommendation_feature(
            article_id=article_id,
            map_data=map_data,
            status="ok",
        )
        result = {
            "kind": "published",
            "draft": self._decode_editorial_row(updated),
            "revision": self._decode_editorial_row(revision),
            "publication": {**dict(publication), "draft_version": int(updated["version"])},
        }
        if schedule_id is not None:
            result["schedule"] = self._decode_editorial_row(completed_schedule)
        return result

    async def _enqueue_first_editorial_push(
        self,
        connection,
        *,
        notification_id: UUID,
        draft_id: UUID,
        article_id: str,
        revision_id: UUID,
        revision_number: int,
        document: dict,
    ) -> int:
        if revision_number != 1:
            return 0
        publication_kind = document.get("publication_kind", "editorial")
        if publication_kind not in {"editorial", "weekly_insight"}:
            raise ValueError("invalid publication kind")
        headline = _normalized_push_copy(document.get("headline"), limit=200)
        body = _normalized_push_copy(document.get("subheadline"), limit=240)
        if not body:
            body = _normalized_push_copy(document.get("summary"), limit=240)
        notification = await connection.fetchrow(
            """
            INSERT INTO editorial_push_notifications
                (id,draft_id,article_id,revision_id,publication_kind,headline,body)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (draft_id) DO NOTHING
            RETURNING id
            """,
            notification_id,
            draft_id,
            article_id,
            revision_id,
            publication_kind,
            headline,
            body,
        )
        if notification is None:
            return 0
        return int(
            await connection.fetchval(
                """
                WITH inserted AS (
                    INSERT INTO editorial_push_deliveries
                        (notification_id,registration_id)
                    SELECT $1,r.id
                    FROM app_push_registrations r
                    JOIN app_account_sessions s ON s.id=r.session_id
                    JOIN editorial_push_notifications n ON n.id=$1
                    WHERE r.status='active' AND s.expires_at > now()
                      AND ((n.publication_kind='editorial' AND r.editorial_enabled)
                           OR (n.publication_kind='weekly_insight'
                               AND r.weekly_insight_enabled))
                    ON CONFLICT (notification_id,registration_id) DO NOTHING
                    RETURNING 1
                )
                SELECT count(*) FROM inserted
                """,
                notification_id,
            )
        )

    async def editorial_revisions(self, draft_id: UUID) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM editorial_revisions
            WHERE draft_id=$1
            ORDER BY revision_number DESC
            """,
            draft_id,
        )
        return [self._decode_editorial_row(row) for row in rows]

    async def withdraw_editorial_publication(
        self,
        *,
        draft_id: UUID,
        expected_version: int | None,
        operator_id: UUID,
    ) -> dict:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked = await connection.fetchrow(
                    "SELECT * FROM editorial_drafts WHERE id=$1 FOR UPDATE",
                    draft_id,
                )
                if locked is None:
                    return {"kind": "not_found"}
                current = self._decode_editorial_row(locked)
                publication = await connection.fetchrow(
                    """
                    SELECT * FROM editorial_publications
                    WHERE draft_id=$1 FOR UPDATE
                    """,
                    draft_id,
                )
                if publication is None:
                    return {"kind": "not_published", "current": current}
                if publication["state"] == "withdrawn":
                    return {
                        "kind": "withdrawn",
                        "publication": {
                            **dict(publication),
                            "draft_version": int(current["version"]),
                        },
                    }
                if expected_version is not None and int(current["version"]) != expected_version:
                    return {"kind": "conflict", "current": current}
                publication = await connection.fetchrow(
                    """
                    UPDATE editorial_publications
                    SET state='withdrawn', withdrawn_at=now(), withdrawn_by=$2,
                        display_order=NULL, is_featured=FALSE, updated_at=now()
                    WHERE draft_id=$1
                    RETURNING *
                    """,
                    draft_id,
                    operator_id,
                )
                updated = await connection.fetchrow(
                    """
                    UPDATE editorial_drafts
                    SET status='withdrawn', version=version+1,
                        updated_by=$2, updated_at=now()
                    WHERE id=$1
                    RETURNING *
                    """,
                    draft_id,
                    operator_id,
                )
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,'draft.withdrawn','draft',$2,'{}'::jsonb)
                    """,
                    operator_id,
                    str(draft_id),
                )
        if publication is None or updated is None:
            raise RuntimeError("editorial withdraw transaction returned no row")
        return {
            "kind": "withdrawn",
            "publication": {
                **dict(publication),
                "draft_version": int(updated["version"]),
            },
        }

    # ---- images ---------------------------------------------------------
    async def upsert_image(self, img: StoredImage) -> None:
        await self.pool.execute(
            """
            INSERT INTO article_images
                (id, source_url, content_type, byte_size, backend, storage_ref, data)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (id) DO UPDATE SET
                source_url  = EXCLUDED.source_url,
                content_type = COALESCE(EXCLUDED.content_type, article_images.content_type),
                byte_size   = EXCLUDED.byte_size,
                backend     = 's3',
                storage_ref = EXCLUDED.storage_ref,
                data        = NULL,
                downloaded_at = now()
            WHERE EXCLUDED.backend = 's3'
              AND EXCLUDED.storage_ref IS NOT NULL
              AND EXCLUDED.data IS NULL
            """,
            img.id,
            img.source_url,
            img.content_type,
            img.byte_size,
            img.backend,
            img.storage_ref,
            img.data,
        )

    # ---- articles -------------------------------------------------------
    async def upsert_article(self, a: Article) -> str:
        """Persist an article and atomically ensure its Map work row exists.

        The queue conflict intentionally does nothing: rediscovering an article
        must not reset an existing queued/processing/done/dead lifecycle.

        Only domains with a positive live allowance are enqueued. The gate
        reads the domain back from ``persisted`` rather than trusting EXCLUDED,
        so rediscovery cannot move an article across its first-attribution
        boundary. Document type/source follow the same first-writer rule.
        """
        row = await self.pool.fetchrow(
            """
            WITH persisted AS (
                INSERT INTO articles
                    (id, url, title, seendate, domain, language, sourcecountry,
                     url_mobile, socialimage_url, source_query, image_id, raw,
                     coverage_domain, document_type, document_source_name,
                     updated_at)
                VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$15,$16,now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    title           = EXCLUDED.title,
                    seendate        = EXCLUDED.seendate,
                    domain          = EXCLUDED.domain,
                    language        = EXCLUDED.language,
                    sourcecountry   = EXCLUDED.sourcecountry,
                    url_mobile      = EXCLUDED.url_mobile,
                    socialimage_url = EXCLUDED.socialimage_url,
                    source_query    = COALESCE(EXCLUDED.source_query, articles.source_query),
                    -- never blank out an image we already have
                    image_id        = COALESCE(EXCLUDED.image_id, articles.image_id),
                    raw             = EXCLUDED.raw,
                    -- coverage_domain is deliberately absent: first attribution
                    -- wins, so neither lane can silently move an article across
                    -- the quarantine boundary in either direction.
                    updated_at      = now()
                RETURNING id, (xmax = 0) AS inserted, coverage_domain
            ), enqueued AS (
                -- Only domains that own a positive daily budget may enter the
                -- queue. Keeping this as a set membership rather than deleting
                -- the gate means a future third domain is quarantined by
                -- default instead of silently spending someone else's budget.
                INSERT INTO map_queue (article_id)
                SELECT id FROM persisted
                WHERE coverage_domain = ANY($14::text[])
                ON CONFLICT (article_id) DO NOTHING
                RETURNING article_id
            )
            SELECT inserted FROM persisted
            """,
            a.id,
            a.url,
            a.title,
            a.seendate,
            a.domain,
            a.language,
            a.sourcecountry,
            a.url_mobile,
            a.socialimage_url,
            a.source_query,
            a.image_id,
            json.dumps(a.raw, ensure_ascii=False),
            a.coverage_domain,
            list(await self.mappable_domains()),
            a.document_type,
            a.document_source_name,
        )
        return "inserted" if row and row["inserted"] else "updated"

    async def missing_article_ids(self, article_ids: list[str]) -> set[str]:
        """Return candidate IDs that do not already exist in the article store."""
        ordered_ids = list(dict.fromkeys(value for value in article_ids if value))
        if not ordered_ids:
            return set()
        rows = await self.pool.fetch(
            """
            SELECT candidate.id
            FROM unnest($1::text[]) WITH ORDINALITY AS candidate(id, position)
            LEFT JOIN articles AS stored ON stored.id = candidate.id
            WHERE stored.id IS NULL
            ORDER BY candidate.position
            """,
            ordered_ids,
        )
        return {str(row["id"]) for row in rows}

    async def primary_document_outstanding_count(self) -> int:
        """Queued or processing primary documents waiting for Map work."""
        value = await self.pool.fetchval(
            """
            SELECT count(*)
            FROM map_queue q
            JOIN articles a ON a.id = q.article_id
            WHERE a.document_type IS NOT NULL
              AND q.status IN ('queued', 'processing')
            """
        )
        return int(value or 0)

    async def link_image(self, article_id: str, image_id: str) -> None:
        await self.pool.execute(
            "UPDATE articles SET image_id = $2, updated_at = now() WHERE id = $1",
            article_id,
            image_id,
        )

    # ---- multi-image links (for maps) ----------------------------------
    async def link_article_image(
        self,
        *,
        article_id: str,
        ref_index: int,
        image_id: str,
        role: str,
        source_url: str,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO article_image_links
                (article_id, ref_index, image_id, role, source_url)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (article_id, ref_index) DO UPDATE SET
                image_id = EXCLUDED.image_id,
                role = EXCLUDED.role,
                source_url = EXCLUDED.source_url
            """,
            article_id,
            ref_index,
            image_id,
            role,
            source_url,
        )

    async def clear_image_links(self, article_id: str) -> None:
        await self.pool.execute(
            "DELETE FROM article_image_links WHERE article_id = $1", article_id
        )

    async def image_links(self, article_id: str) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT ref_index, image_id, role, source_url
            FROM article_image_links WHERE article_id = $1 ORDER BY ref_index
            """,
            article_id,
        )
        return [dict(r) for r in rows]

    # ---- maps -----------------------------------------------------------
    async def has_map(self, article_id: str) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT 1 FROM article_maps WHERE article_id = $1 AND status <> 'failed'",
                article_id,
            )
        )

    async def upsert_map(
        self,
        article_id: str,
        *,
        format_version: int,
        model: str,
        status: str,
        data: Optional[dict],
        error: Optional[str],
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO article_maps
                (article_id, format_version, model, status, data, error, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6, now())
            ON CONFLICT (article_id) DO UPDATE SET
                format_version = EXCLUDED.format_version,
                model  = EXCLUDED.model,
                status = EXCLUDED.status,
                data   = EXCLUDED.data,
                error  = EXCLUDED.error,
                updated_at = now()
            """,
            article_id,
            format_version,
            model,
            status,
            json.dumps(data, ensure_ascii=False) if data is not None else None,
            error,
        )
        if data is not None and status in {"ok", "degraded"}:
            await self._best_effort_refresh_article_recommendation_feature(
                article_id=article_id,
                map_data=data,
                status=status,
            )

    async def save_map_and_mark_done(
        self,
        article_id: str,
        *,
        format_version: int,
        model: str,
        status: str,
        data: dict,
        error: Optional[str],
        rag_links: list[dict] | None = None,
    ) -> None:
        """Atomically replace RAG links, persist Map and complete its work row."""
        if status not in ("ok", "degraded"):
            raise ValueError("successful map status must be ok or degraded")
        if not isinstance(data, dict):
            raise ValueError("successful map data must be an object")
        normalized_rag_links: list[dict] = []
        for link in rag_links or []:
            if not isinstance(link, dict) or link.get("role") != "rag":
                raise ValueError("RAG links must be role=rag objects")
            ref_index = link.get("ref_index")
            if (
                not isinstance(ref_index, int)
                or isinstance(ref_index, bool)
                or ref_index < 0
                or not link.get("image_id")
            ):
                raise ValueError("RAG link ref/image_id is invalid")
            normalized_rag_links.append(
                {
                    "ref_index": ref_index,
                    "image_id": str(link["image_id"]),
                    "role": "rag",
                    "source_url": str(link.get("source_url") or ""),
                }
            )
        await self.pool.execute(
            """
            WITH removed_rag AS (
                DELETE FROM article_image_links
                WHERE article_id=$1 AND role='rag'
                RETURNING ref_index
            ), remove_guard AS (
                SELECT count(*) AS removed FROM removed_rag
            ), rag_rows AS (
                SELECT payload.ref_index, payload.image_id,
                       payload.role, payload.source_url
                FROM jsonb_to_recordset($7::jsonb) AS payload(
                    ref_index INTEGER,
                    image_id TEXT,
                    role TEXT,
                    source_url TEXT
                )
                CROSS JOIN remove_guard
                WHERE payload.role='rag'
            ), linked AS (
                INSERT INTO article_image_links
                    (article_id, ref_index, image_id, role, source_url)
                SELECT $1, ref_index, image_id, role, source_url
                FROM rag_rows
                ON CONFLICT (article_id, ref_index) DO UPDATE SET
                    image_id=EXCLUDED.image_id,
                    role=EXCLUDED.role,
                    source_url=EXCLUDED.source_url
                RETURNING ref_index
            ), link_guard AS (
                SELECT count(*) AS linked FROM linked
            ), exposure AS (
                -- A domain with no settings row never wins a budget claim, so
                -- it never reaches this statement; defaulting to exposed keeps
                -- deployments without the DR-13206 axis behaving as before.
                SELECT COALESCE(d.reader_exposed, TRUE) AS reader_exposed
                FROM articles a
                LEFT JOIN map_budget_domain_settings d
                       ON d.coverage_domain = a.coverage_domain
                WHERE a.id = $1
            ), saved AS (
                INSERT INTO article_maps
                    (article_id, format_version, model, status, data, error, updated_at)
                SELECT $1,$2,$3,
                       CASE
                           WHEN COALESCE(
                               (SELECT reader_exposed FROM exposure), TRUE
                           ) THEN $4
                           ELSE 'held_' || $4
                       END,
                       $5,$6,now() FROM link_guard
                ON CONFLICT (article_id) DO UPDATE SET
                    format_version = EXCLUDED.format_version,
                    model  = EXCLUDED.model,
                    status = EXCLUDED.status,
                    data   = EXCLUDED.data,
                    error  = EXCLUDED.error,
                    updated_at = now()
                RETURNING article_id
            )
            INSERT INTO map_queue
                (article_id, status, next_attempt_at, lease_expires_at, last_error)
            SELECT article_id, 'done', now(), NULL, NULL FROM saved
            ON CONFLICT (article_id) DO UPDATE SET
                status = 'done',
                next_attempt_at = now(),
                lease_expires_at = NULL,
                lease_owner = NULL,
                last_error = NULL,
                updated_at = now()
            """,
            article_id,
            format_version,
            model,
            status,
            json.dumps(data, ensure_ascii=False),
            error,
            json.dumps(normalized_rag_links, ensure_ascii=False),
        )
        await self._best_effort_refresh_article_recommendation_feature(
            article_id=article_id,
            map_data=data,
            status=status,
        )

    async def upsert_article_recommendation_feature(
        self, feature: ArticleRecommendationFeature
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO article_recommendation_features(
                article_id, feature_version, terms, term_weights,
                representative_topic, domain, published_at, quality_score,
                updated_at
            )
            VALUES($1,1,$2::text[],$3::jsonb,$4,$5,$6,$7,now())
            ON CONFLICT (article_id) DO UPDATE SET
                feature_version=1,
                terms=EXCLUDED.terms,
                term_weights=EXCLUDED.term_weights,
                representative_topic=EXCLUDED.representative_topic,
                domain=EXCLUDED.domain,
                published_at=EXCLUDED.published_at,
                quality_score=EXCLUDED.quality_score,
                updated_at=now()
            """,
            feature.article_id,
            list(feature.terms),
            json.dumps(dict(feature.term_weights), ensure_ascii=False),
            feature.representative_topic,
            feature.domain,
            feature.published_at,
            feature.quality_score,
        )

    async def _best_effort_refresh_article_recommendation_feature(
        self,
        *,
        article_id: str,
        map_data: dict,
        status: str,
    ) -> bool:
        try:
            metadata = await self.pool.fetchrow(
                """
                SELECT a.domain,
                       COALESCE(p.published_at, a.seendate, a.first_seen_at)
                           AS published_at
                FROM articles a
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                WHERE a.id=$1
                """,
                article_id,
            )
            if metadata is None:
                return False
            feature = extract_article_recommendation_feature(
                article_id=article_id,
                map_data=map_data,
                domain=metadata["domain"],
                published_at=metadata["published_at"],
                status=status,
            )
            await self.upsert_article_recommendation_feature(feature)
            return True
        except Exception:  # noqa: BLE001 - recommendation index is best effort
            log.warning("article recommendation feature refresh skipped")
            return False

    async def backfill_article_recommendation_features(
        self, *, limit: int = 500
    ) -> dict[str, int]:
        if not 1 <= limit <= 500:
            raise ValueError("recommendation feature backfill limit is out of range")
        rows = await self.pool.fetch(
            """
            SELECT m.article_id, m.status, m.data, a.domain,
                   COALESCE(p.published_at, a.seendate, a.first_seen_at)
                       AS published_at
            FROM article_maps m
            JOIN articles a ON a.id=m.article_id
            LEFT JOIN editorial_publications p ON p.article_id=a.id
            LEFT JOIN article_recommendation_features f
                   ON f.article_id=m.article_id
            WHERE m.status IN ('ok','degraded')
              AND m.data IS NOT NULL
              AND f.article_id IS NULL
              AND (p.draft_id IS NULL OR p.state='published')
            ORDER BY m.updated_at DESC, m.article_id DESC
            LIMIT $1
            """,
            limit,
        )
        inserted = 0
        skipped = 0
        for raw in rows:
            row = dict(raw)
            data = row.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    data = None
            if not isinstance(data, dict):
                skipped += 1
                continue
            try:
                feature = extract_article_recommendation_feature(
                    article_id=str(row["article_id"]),
                    map_data=data,
                    domain=row.get("domain"),
                    published_at=row.get("published_at"),
                    status=str(row["status"]),
                )
                await self.upsert_article_recommendation_feature(feature)
                inserted += 1
            except Exception:  # noqa: BLE001 - preserve readable Map availability
                skipped += 1
        return {"inserted": inserted, "skipped": skipped}

    async def get_map_row(self, article_id: str) -> Optional[dict]:
        row = await self.pool.fetchrow(
            "SELECT article_id, format_version, model, status, data, error, "
            "created_at, updated_at FROM article_maps WHERE article_id = $1 "
            "AND status IN ('ok', 'degraded')",
            article_id,
        )
        if not row:
            return None
        d = dict(row)
        if isinstance(d.get("data"), str):
            d["data"] = json.loads(d["data"])
        return d

    # ---- editorial pipeline artifacts ---------------------------------
    async def get_pipeline_artifact(
        self,
        article_id: str,
        stage: str,
        input_hash: str,
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            SELECT article_id, stage, input_hash, artifact_version, model,
                   status, data, error, attempts, created_at, updated_at
            FROM article_pipeline_artifacts
            WHERE article_id=$1 AND stage=$2 AND input_hash=$3
            """,
            article_id,
            stage,
            input_hash,
        )
        if not row:
            return None
        value = dict(row)
        if isinstance(value.get("data"), str):
            value["data"] = json.loads(value["data"])
        return value

    async def upsert_pipeline_artifact(
        self,
        article_id: str,
        *,
        stage: str,
        input_hash: str,
        artifact_version: int,
        model: str,
        status: str,
        data: Optional[dict],
        error: Optional[str],
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO article_pipeline_artifacts
                (article_id, stage, input_hash, artifact_version, model,
                 status, data, error, attempts, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,1,now())
            ON CONFLICT (article_id, stage) DO UPDATE SET
                input_hash = EXCLUDED.input_hash,
                artifact_version = EXCLUDED.artifact_version,
                model = EXCLUDED.model,
                status = EXCLUDED.status,
                data = EXCLUDED.data,
                error = EXCLUDED.error,
                attempts = CASE
                    WHEN article_pipeline_artifacts.input_hash = EXCLUDED.input_hash
                    THEN article_pipeline_artifacts.attempts + 1
                    ELSE 1
                END,
                updated_at = now()
            """,
            article_id,
            stage,
            input_hash,
            artifact_version,
            model,
            status,
            json.dumps(data, ensure_ascii=False) if data is not None else None,
            error,
        )

    async def delete_pipeline_artifacts(self, article_id: str) -> None:
        await self.pool.execute(
            "DELETE FROM article_pipeline_artifacts WHERE article_id=$1",
            article_id,
        )

    async def append_map_attempt_event(
        self,
        article_id: str,
        *,
        stage: str,
        outcome: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        retryable: bool = False,
        correction_index: int | None = None,
        model: str | None = None,
        structured_output: bool | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Append a bounded, secret-free Map stage observation."""
        allowed_outcomes = {
            "started",
            "cached",
            "succeeded",
            "failed",
            "terminal",
            "requeued",
        }
        if not stage or outcome not in allowed_outcomes:
            raise ValueError("invalid Map attempt event")
        if correction_index is not None and correction_index < 0:
            raise ValueError("correction_index must not be negative")
        await self.pool.execute(
            """
            INSERT INTO map_attempt_events
                (article_id, stage, outcome, error_code, error_detail,
                 retryable, correction_index, model, structured_output, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
            """,
            article_id,
            stage[:120],
            outcome,
            (error_code or "")[:120] or None,
            _sanitized_map_event_detail(error_detail),
            bool(retryable),
            correction_index,
            (model or "")[:200] or None,
            structured_output,
            json.dumps(
                _bounded_map_event_metadata(metadata),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    # ---- Apple App Attest ----------------------------------------------
    async def issue_app_challenge(
        self,
        *,
        installation_id: str,
        key_id: str,
        purpose: str,
        ttl_s: int,
    ) -> dict:
        challenge_id = uuid4()
        challenge = secrets.token_bytes(32)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO app_attest_challenges
                        (id, installation_id, key_id, purpose, challenge, expires_at)
                    VALUES ($1,$2,$3,$4,$5, now() + ($6 * interval '1 second'))
                    RETURNING id, installation_id, key_id, purpose, challenge,
                              expires_at, used_at, created_at
                    """,
                    challenge_id,
                    installation_id,
                    key_id,
                    purpose,
                    challenge,
                    ttl_s,
                )
                await conn.execute(
                    "DELETE FROM app_attest_challenges "
                    "WHERE expires_at < now() - interval '1 day'"
                )
        return dict(row)

    async def get_app_challenge(self, challenge_id: UUID) -> Optional[dict]:
        row = await self.pool.fetchrow(
            "SELECT * FROM app_attest_challenges WHERE id = $1", challenge_id
        )
        return dict(row) if row else None

    async def register_app_attest_key(
        self,
        *,
        challenge_id: UUID,
        installation_id: str,
        key_id: str,
        public_key_pem: bytes,
        receipt: bytes,
        environment: str,
        device: dict,
        validation_category: int | None,
        bundle_version: str | None,
        recovery_account_id: UUID | None = None,
        recovery_session_id: UUID | None = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    installation_id,
                )
                challenge = await conn.fetchrow(
                    "SELECT * FROM app_attest_challenges WHERE id=$1 FOR UPDATE",
                    challenge_id,
                )
                if (
                    not challenge
                    or challenge["used_at"] is not None
                    or challenge["expires_at"] <= await conn.fetchval("SELECT now()")
                    or challenge["purpose"] != "attestation"
                    or challenge["installation_id"] != installation_id
                    or challenge["key_id"] != key_id
                ):
                    return False
                requested_key = await conn.fetchrow(
                    """
                    SELECT key_id, installation_id, status
                    FROM app_attest_keys
                    WHERE key_id=$1
                    FOR UPDATE
                    """,
                    key_id,
                )
                active_key = await conn.fetchrow(
                    """
                    SELECT key_id, installation_id, status
                    FROM app_attest_keys
                    WHERE installation_id=$1 AND status='active'
                    FOR UPDATE
                    """,
                    installation_id,
                )
                if requested_key is not None:
                    if (
                        requested_key["installation_id"] != installation_id
                        or requested_key["status"] != "active"
                        or active_key is None
                        or active_key["key_id"] != key_id
                    ):
                        return False
                    await conn.execute(
                        """
                        UPDATE app_attest_keys
                        SET device=$2, validation_category=$3, bundle_version=$4,
                            updated_at=now(), last_seen_at=now()
                        WHERE key_id=$1 AND installation_id=$5 AND status='active'
                        """,
                        key_id,
                        json.dumps(device, ensure_ascii=False),
                        validation_category,
                        bundle_version,
                        installation_id,
                    )
                    await conn.execute(
                        "UPDATE app_attest_challenges SET used_at=now() WHERE id=$1",
                        challenge_id,
                    )
                    return True

                recovery_registration = None
                old_key_id = None
                if active_key is not None:
                    if recovery_account_id is None or recovery_session_id is None:
                        return False
                    old_key_id = str(active_key["key_id"])
                    recovery_registration = await conn.fetchrow(
                        """
                        SELECT registration.id, registration.account_id,
                               registration.session_id,
                               registration.installation_id, registration.key_id
                        FROM app_push_registrations registration
                        JOIN app_account_sessions session
                          ON session.id=registration.session_id
                        WHERE registration.installation_id=$1
                          AND registration.key_id=$2
                          AND registration.account_id=$3
                          AND registration.session_id=$4
                          AND registration.status='active'
                          AND session.account_id=$3
                          AND session.expires_at > now()
                        FOR UPDATE OF registration, session
                        """,
                        installation_id,
                        old_key_id,
                        recovery_account_id,
                        recovery_session_id,
                    )
                    if recovery_registration is None:
                        return False
                    revoked = await conn.execute(
                        """
                        UPDATE app_attest_keys SET status='revoked', updated_at=now()
                        WHERE key_id=$1 AND installation_id=$2 AND status='active'
                        """,
                        old_key_id,
                        installation_id,
                    )
                    if revoked != "UPDATE 1":
                        raise RuntimeError("App Attest rotation lost active key")

                inserted = await conn.execute(
                    """
                    INSERT INTO app_attest_keys
                        (key_id, installation_id, public_key_pem, receipt,
                         environment, device, validation_category, bundle_version)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    """,
                    key_id,
                    installation_id,
                    public_key_pem,
                    receipt,
                    environment,
                    json.dumps(device, ensure_ascii=False),
                    validation_category,
                    bundle_version,
                )
                if inserted != "INSERT 0 1":
                    raise RuntimeError("App Attest key insert failed")

                if recovery_registration is not None and old_key_id is not None:
                    await conn.execute(
                        """
                        UPDATE app_privacy_consents
                        SET key_id=$2
                        WHERE installation_id=$1 AND key_id=$3
                        """,
                        installation_id,
                        key_id,
                        old_key_id,
                    )
                    rebound_push = await conn.execute(
                        """
                        UPDATE app_push_registrations
                        SET key_id=$2, updated_at=now(), last_seen_at=now()
                        WHERE installation_id=$1 AND key_id=$3 AND id=$4
                        """,
                        installation_id,
                        key_id,
                        old_key_id,
                        recovery_registration["id"],
                    )
                    if rebound_push != "UPDATE 1":
                        raise RuntimeError("App Attest rotation lost push registration")
                    await conn.execute(
                        """
                        UPDATE app_request_cooldowns
                        SET key_id=$2
                        WHERE key_id=$1
                        """,
                        old_key_id,
                        key_id,
                    )

                consumed = await conn.execute(
                    "UPDATE app_attest_challenges SET used_at=now() WHERE id=$1",
                    challenge_id,
                )
                if consumed != "UPDATE 1":
                    raise RuntimeError("App Attest challenge consume failed")
                return True

    async def get_app_attest_context(
        self, *, key_id: str, installation_id: str, challenge_id: UUID
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            SELECT c.key_id, c.installation_id, k.public_key_pem, k.sign_count,
                   k.environment, k.device, k.status,
                   c.id AS challenge_id, c.challenge, c.purpose,
                   c.expires_at, c.used_at
            FROM app_attest_challenges c
            LEFT JOIN app_attest_keys k
              ON k.key_id = c.key_id AND k.installation_id = c.installation_id
            WHERE c.key_id=$1 AND c.installation_id=$2 AND c.id=$3
            """,
            key_id,
            installation_id,
            challenge_id,
        )
        return dict(row) if row else None

    async def commit_verified_assertion(
        self,
        *,
        key_id: str,
        challenge_id: UUID,
        expected_counter: int,
        new_counter: int,
    ) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                challenge = await conn.fetchrow(
                    "SELECT * FROM app_attest_challenges WHERE id=$1 FOR UPDATE",
                    challenge_id,
                )
                key = await conn.fetchrow(
                    "SELECT * FROM app_attest_keys WHERE key_id=$1 FOR UPDATE",
                    key_id,
                )
                now = await conn.fetchval("SELECT now()")
                if (
                    not challenge
                    or not key
                    or challenge["used_at"] is not None
                    or challenge["expires_at"] <= now
                    or challenge["purpose"] != "assertion"
                    or key["status"] != "active"
                    or key["sign_count"] < expected_counter
                    or new_counter <= key["sign_count"]
                ):
                    return False
                await conn.execute(
                    "UPDATE app_attest_challenges SET used_at=now() WHERE id=$1",
                    challenge_id,
                )
                await conn.execute(
                    """
                    UPDATE app_attest_keys
                    SET sign_count=$2, last_seen_at=now(), updated_at=now()
                    WHERE key_id=$1
                    """,
                    key_id,
                    new_counter,
                )
                return True

    async def record_privacy_consent(
        self,
        *,
        consent_id: UUID,
        installation_id: str,
        key_id: str,
        policy_version: str,
        policy_url: str,
        app_version: str,
        build_number: str,
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            INSERT INTO app_privacy_consents
                (id, installation_id, key_id, policy_version, policy_url,
                 app_version, build_number)
            SELECT $1,$2,$3,$4,$5,$6,$7
            FROM app_attest_keys key
            WHERE key.key_id=$3
              AND key.installation_id=$2
              AND key.status='active'
            ON CONFLICT (installation_id, policy_version) DO UPDATE SET
                key_id=EXCLUDED.key_id,
                policy_url=EXCLUDED.policy_url,
                app_version=EXCLUDED.app_version,
                build_number=EXCLUDED.build_number
            RETURNING id, installation_id, key_id, policy_version, policy_url,
                      app_version, build_number, accepted_at
            """,
            consent_id,
            installation_id,
            key_id,
            policy_version,
            policy_url,
            app_version,
            build_number,
        )
        return dict(row) if row else None

    async def current_privacy_consent(
        self, *, consent_id: UUID, policy_version: str
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            SELECT consent.id, consent.installation_id, consent.key_id,
                   consent.policy_version, consent.policy_url,
                   consent.app_version, consent.build_number,
                   consent.accepted_at
            FROM app_privacy_consents consent
            JOIN app_attest_keys key ON key.key_id=consent.key_id
            WHERE consent.id=$1
              AND consent.policy_version=$2
              AND key.installation_id=consent.installation_id
              AND key.status='active'
            """,
            consent_id,
            policy_version,
        )
        return dict(row) if row else None

    async def claim_feed_cooldown(
        self, *, key_id: str, installation_id: str, cooldown_s: float
    ) -> float:
        """Return 0 when claimed, otherwise seconds until the next request."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT last_feed_at FROM app_attest_keys
                    WHERE key_id=$1 AND installation_id=$2 AND status='active'
                    FOR UPDATE
                    """,
                    key_id,
                    installation_id,
                )
                if not row:
                    raise LookupError("active App Attest key not found")
                now = await conn.fetchval("SELECT now()")
                last = row["last_feed_at"]
                if last is not None:
                    remaining = cooldown_s - (now - last).total_seconds()
                    if remaining > 0:
                        return remaining
                await conn.execute(
                    """
                    UPDATE app_attest_keys
                    SET last_feed_at=$3, last_seen_at=$3, updated_at=$3
                    WHERE key_id=$1 AND installation_id=$2
                    """,
                    key_id,
                    installation_id,
                    now,
                )
                return 0.0

    # ---- Sign in with Apple accounts -----------------------------------
    async def upsert_apple_account_and_create_session(
        self,
        *,
        apple_subject: str,
        email: str | None,
        given_name: str | None,
        family_name: str | None,
        encrypted_refresh_token: bytes,
        token_hash: bytes,
        ttl_days: int,
        privacy_consent_id: UUID,
        privacy_policy_version: str,
    ) -> dict:
        account_id = uuid4()
        session_id = uuid4()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                consent = await conn.fetchrow(
                    """
                    SELECT consent.id
                    FROM app_privacy_consents consent
                    JOIN app_attest_keys key ON key.key_id=consent.key_id
                    WHERE consent.id=$1
                      AND consent.policy_version=$2
                      AND key.installation_id=consent.installation_id
                      AND key.status='active'
                    FOR UPDATE OF consent
                    """,
                    privacy_consent_id,
                    privacy_policy_version,
                )
                if consent is None:
                    raise LookupError("current privacy consent not found")
                account = await conn.fetchrow(
                    """
                    INSERT INTO app_accounts
                        (id, apple_subject, email, given_name, family_name,
                         apple_refresh_token)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (apple_subject) DO UPDATE SET
                        email = COALESCE(EXCLUDED.email, app_accounts.email),
                        given_name = COALESCE(EXCLUDED.given_name, app_accounts.given_name),
                        family_name = COALESCE(EXCLUDED.family_name, app_accounts.family_name),
                        apple_refresh_token = EXCLUDED.apple_refresh_token,
                        updated_at = now(),
                        last_signed_in_at = now()
                    RETURNING id, apple_subject, email, given_name, family_name,
                              apple_refresh_token, created_at, updated_at,
                              last_signed_in_at
                    """,
                    account_id,
                    apple_subject,
                    email,
                    given_name,
                    family_name,
                    encrypted_refresh_token,
                )
                session = await conn.fetchrow(
                    """
                    INSERT INTO app_account_sessions
                        (id, account_id, token_hash, expires_at)
                    VALUES ($1,$2,$3, now() + ($4 * interval '1 day'))
                    RETURNING id, account_id, expires_at, created_at, last_seen_at
                    """,
                    session_id,
                    account["id"],
                    token_hash,
                    ttl_days,
                )
                await conn.execute(
                    """
                    INSERT INTO app_privacy_consent_accounts
                        (consent_id, account_id)
                    VALUES ($1,$2)
                    ON CONFLICT (consent_id, account_id) DO NOTHING
                    """,
                    privacy_consent_id,
                    account["id"],
                )
                await conn.execute(
                    """
                    DELETE FROM app_account_sessions
                    WHERE id IN (
                        SELECT id FROM app_account_sessions
                        WHERE expires_at <= now()
                        ORDER BY expires_at
                        LIMIT 1000
                    )
                    """
                )
        return {**dict(account), "session_expires_at": session["expires_at"]}

    async def account_for_session(self, token_hash: bytes) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            WITH authenticated AS (
                UPDATE app_account_sessions s
                SET last_seen_at = now()
                FROM app_accounts a
                WHERE s.token_hash=$1
                  AND s.account_id=a.id
                  AND s.expires_at > now()
                RETURNING a.id, a.apple_subject, a.email, a.given_name,
                          a.family_name, a.apple_refresh_token,
                          a.created_at, a.updated_at, a.last_signed_in_at,
                          s.id AS session_id,
                          s.expires_at AS session_expires_at,
                          s.last_seen_at AS authenticated_at
            ),
            recorded AS (
                INSERT INTO app_account_session_activity_days(
                    account_id,
                    active_date,
                    first_seen_at,
                    last_seen_at
                )
                SELECT id,
                       (authenticated_at AT TIME ZONE 'Asia/Seoul')::date,
                       authenticated_at,
                       authenticated_at
                FROM authenticated
                ON CONFLICT (account_id, active_date) DO UPDATE SET
                    first_seen_at=LEAST(
                        app_account_session_activity_days.first_seen_at,
                        EXCLUDED.first_seen_at
                    ),
                    last_seen_at=GREATEST(
                        app_account_session_activity_days.last_seen_at,
                        EXCLUDED.last_seen_at
                    )
                RETURNING account_id
            ),
            expired_candidates AS (
                SELECT activity.ctid
                FROM app_account_session_activity_days activity
                WHERE activity.active_date < (
                    (now() AT TIME ZONE 'Asia/Seoul')::date
                    - interval '399 days'
                )
                  AND EXISTS (SELECT 1 FROM authenticated)
                ORDER BY activity.active_date, activity.account_id
                LIMIT 1000
            ),
            pruned AS (
                DELETE FROM app_account_session_activity_days activity
                USING expired_candidates expired
                WHERE activity.ctid=expired.ctid
                RETURNING activity.account_id
            )
            SELECT id, apple_subject, email, given_name, family_name,
                   apple_refresh_token, created_at, updated_at,
                   last_signed_in_at, session_id, session_expires_at
            FROM authenticated
            """,
            token_hash,
        )
        return dict(row) if row else None

    async def record_guest_session_activity(
        self, *, installation_id: str, observed_at: datetime
    ) -> None:
        """Persist one KST activity day for an attested guest installation."""
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        installation_hash = hashlib.sha256(
            installation_id.encode("utf-8")
        ).digest()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO app_guest_session_activity_days(
                        installation_hash, active_date, first_seen_at, last_seen_at
                    )
                    VALUES ($1, ($2 AT TIME ZONE 'Asia/Seoul')::date, $2, $2)
                    ON CONFLICT (installation_hash, active_date) DO UPDATE SET
                        first_seen_at=LEAST(
                            app_guest_session_activity_days.first_seen_at,
                            EXCLUDED.first_seen_at
                        ),
                        last_seen_at=GREATEST(
                            app_guest_session_activity_days.last_seen_at,
                            EXCLUDED.last_seen_at
                        )
                    """,
                    installation_hash,
                    observed_at,
                )
                await conn.execute(
                    """
                    DELETE FROM app_guest_session_activity_days activity
                    WHERE activity.ctid IN (
                        SELECT ctid FROM app_guest_session_activity_days
                        WHERE active_date < (
                            ($1 AT TIME ZONE 'Asia/Seoul')::date - interval '399 days'
                        )
                        ORDER BY active_date, installation_hash
                        LIMIT 1000
                    )
                    """,
                    observed_at,
                )

    async def delete_account_session(self, token_hash: bytes) -> bool:
        result = await self.pool.execute(
            "DELETE FROM app_account_sessions WHERE token_hash=$1", token_hash
        )
        return result == "DELETE 1"

    async def delete_account(self, account_id: UUID) -> bool:
        result = await self.pool.execute(
            "DELETE FROM app_accounts WHERE id=$1", account_id
        )
        return result == "DELETE 1"

    async def upsert_article_feedback(
        self,
        *,
        account_id: UUID,
        article_id: str,
        reason: str,
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            INSERT INTO app_article_feedback
                (account_id, article_id, reason)
            SELECT $1, article.id, $3
            FROM articles article
            WHERE article.id=$2
            ON CONFLICT (account_id, article_id) DO UPDATE SET
                reason=EXCLUDED.reason,
                updated_at=now()
            RETURNING account_id, article_id, reason, created_at, updated_at
            """,
            account_id,
            article_id,
            reason,
        )
        return dict(row) if row else None

    async def record_article_view(
        self,
        *,
        event_id: UUID,
        article_id: str,
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            WITH target AS (
                SELECT id AS article_id
                FROM articles
                WHERE id=$2
            ),
            inserted AS (
                INSERT INTO article_view_events(id, article_id)
                SELECT $1, target.article_id
                FROM target
                ON CONFLICT (id) DO NOTHING
                RETURNING article_id, received_at
            ),
            bumped AS (
                INSERT INTO article_view_counts(
                    article_id,
                    view_count,
                    first_viewed_at,
                    last_viewed_at,
                    updated_at
                )
                SELECT
                    article_id,
                    1,
                    received_at,
                    received_at,
                    received_at
                FROM inserted
                ON CONFLICT (article_id) DO UPDATE SET
                    view_count=article_view_counts.view_count + 1,
                    first_viewed_at=COALESCE(
                        article_view_counts.first_viewed_at,
                        EXCLUDED.first_viewed_at
                    ),
                    last_viewed_at=GREATEST(
                        COALESCE(
                            article_view_counts.last_viewed_at,
                            EXCLUDED.last_viewed_at
                        ),
                        EXCLUDED.last_viewed_at
                    ),
                    updated_at=now()
                RETURNING article_id, view_count
            )
            SELECT
                target.article_id,
                EXISTS(SELECT 1 FROM inserted) AS counted,
                COALESCE(
                    (SELECT view_count FROM bumped),
                    (
                        SELECT view_count
                        FROM article_view_counts
                        WHERE article_id=target.article_id
                    ),
                    0
                ) AS view_count
            FROM target
            """,
            event_id,
            article_id,
        )
        if row is None:
            return None
        return {
            "article_id": str(row["article_id"]),
            "counted": bool(row["counted"]),
            "view_count": int(row["view_count"]),
        }

    async def record_article_list_impressions(
        self,
        *,
        article_ids: list[str],
        served_at: datetime,
        account_id: UUID | None = None,
        surface: str | None = None,
        response_id: UUID | None = None,
    ) -> int:
        """Atomically record final server-served article-card occurrences."""
        if served_at.tzinfo is None:
            raise ValueError("article list impression time must be aware")
        has_account_context = any(
            value is not None for value in (account_id, surface, response_id)
        )
        if has_account_context and (
            account_id is None or surface is None or response_id is None
        ):
            raise ValueError("account article list impressions require full context")
        if surface is not None and surface != "library":
            raise ValueError("unsupported account article list surface")
        if not article_ids:
            return 0

        if account_id is None:
            recorded = await self.pool.fetchval(
                """
                WITH requested AS (
                    SELECT article_id, ordinal
                    FROM unnest($1::text[]) WITH ORDINALITY
                         AS requested(article_id, ordinal)
                ),
                valid AS (
                    SELECT requested.article_id, count(*)::bigint AS delta
                    FROM requested
                    JOIN articles article ON article.id=requested.article_id
                    GROUP BY requested.article_id
                ),
                bumped AS (
                    INSERT INTO article_feed_impression_counts(
                        article_id, impression_count, first_impressed_at,
                        last_impressed_at, updated_at
                    )
                    SELECT article_id, delta, $2, $2, $2
                    FROM valid
                    ON CONFLICT (article_id) DO UPDATE SET
                        impression_count=(
                            article_feed_impression_counts.impression_count
                            + EXCLUDED.impression_count
                        ),
                        first_impressed_at=COALESCE(
                            article_feed_impression_counts.first_impressed_at,
                            EXCLUDED.first_impressed_at
                        ),
                        last_impressed_at=GREATEST(
                            COALESCE(
                                article_feed_impression_counts.last_impressed_at,
                                EXCLUDED.last_impressed_at
                            ),
                            EXCLUDED.last_impressed_at
                        ),
                        updated_at=EXCLUDED.updated_at
                    RETURNING article_id
                )
                SELECT COALESCE(sum(delta), 0)::bigint FROM valid
                """,
                article_ids,
                served_at,
            )
            return int(recorded or 0)

        recorded = await self.pool.fetchval(
            """
            WITH expired AS (
                DELETE FROM app_article_list_exposures
                WHERE served_at < $2::timestamptz - interval '90 days'
            ),
            requested AS (
                SELECT article_id, (ordinal - 1)::smallint AS position
                FROM unnest($1::text[]) WITH ORDINALITY
                     AS requested(article_id, ordinal)
            ),
            inserted AS (
                INSERT INTO app_article_list_exposures(
                    response_id, position, account_id, article_id,
                    surface, served_at
                )
                SELECT $5, requested.position, $3, requested.article_id,
                       $4, $2::timestamptz
                FROM requested
                JOIN articles article ON article.id=requested.article_id
                ON CONFLICT (response_id, position) DO NOTHING
                RETURNING article_id, served_at
            ),
            grouped AS (
                SELECT article_id, count(*)::bigint AS delta,
                       min(served_at) AS first_at, max(served_at) AS last_at
                FROM inserted
                GROUP BY article_id
            ),
            bumped AS (
                INSERT INTO article_feed_impression_counts(
                    article_id, impression_count, first_impressed_at,
                    last_impressed_at, updated_at
                )
                SELECT article_id, delta, first_at, last_at, $2::timestamptz
                FROM grouped
                ON CONFLICT (article_id) DO UPDATE SET
                    impression_count=(
                        article_feed_impression_counts.impression_count
                        + EXCLUDED.impression_count
                    ),
                    first_impressed_at=COALESCE(
                        article_feed_impression_counts.first_impressed_at,
                        EXCLUDED.first_impressed_at
                    ),
                    last_impressed_at=GREATEST(
                        COALESCE(
                            article_feed_impression_counts.last_impressed_at,
                            EXCLUDED.last_impressed_at
                        ),
                        EXCLUDED.last_impressed_at
                    ),
                    updated_at=EXCLUDED.updated_at
                RETURNING article_id
            )
            SELECT count(*)::bigint FROM inserted
            """,
            article_ids,
            served_at,
            account_id,
            surface,
            response_id,
        )
        return int(recorded or 0)

    async def append_account_activity_events(
        self,
        account_id: UUID,
        events: list[dict],
    ) -> dict[str, int]:
        accepted = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for event in events:
                    occurred_at = event["occurred_at"]
                    if (
                        occurred_at.tzinfo is None
                        or occurred_at.utcoffset() is None
                    ):
                        raise ValueError("occurred_at must include a timezone")
                    offset_minutes = int(event["utc_offset_minutes"])
                    if not -720 <= offset_minutes <= 840:
                        raise ValueError("utc_offset_minutes is out of range")
                    local_time = occurred_at.astimezone(UTC) + timedelta(
                        minutes=offset_minutes
                    )
                    inserted_id = await conn.fetchval(
                        """
                        INSERT INTO app_account_activity_events(
                            id, account_id, event_type, occurred_at,
                            time_zone, utc_offset_minutes, local_date, local_hour,
                            query, result_count, article_id, scene_index, scene_count
                        )
                        VALUES(
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
                        )
                        ON CONFLICT (id) DO NOTHING
                        RETURNING id
                        """,
                        event["event_id"],
                        account_id,
                        str(event["event_type"]),
                        occurred_at,
                        event["time_zone"],
                        offset_minutes,
                        local_time.date(),
                        local_time.hour,
                        event.get("query"),
                        event.get("result_count"),
                        event.get("article_id"),
                        event.get("scene_index"),
                        event.get("scene_count"),
                    )
                    if inserted_id is not None:
                        accepted += 1
                        if str(event["event_type"]) == "article_opened":
                            await conn.execute(
                                """
                                UPDATE app_recommendation_exposures exposure
                                SET selected_at=COALESCE(
                                    exposure.selected_at,
                                    $3
                                )
                                WHERE (exposure.request_id, exposure.article_id)=(
                                    SELECT recent.request_id, recent.article_id
                                    FROM app_recommendation_exposures recent
                                    WHERE recent.account_id=$1
                                      AND recent.article_id=$2
                                      AND recent.served_at <= $3
                                      AND recent.served_at
                                          >= $3 - interval '7 days'
                                    ORDER BY recent.served_at DESC
                                    LIMIT 1
                                )
                                """,
                                account_id,
                                event.get("article_id"),
                                occurred_at,
                            )
        return {
            "accepted": accepted,
            "duplicates": len(events) - accepted,
        }

    async def recommendation_signal_rows(
        self,
        account_id: UUID,
        *,
        snapshot_at: datetime,
        since: datetime,
        limit: int = 500,
    ) -> list[dict]:
        if snapshot_at.tzinfo is None or since.tzinfo is None:
            raise ValueError("recommendation signal bounds must be timezone-aware")
        if not 1 <= limit <= 500:
            raise ValueError("recommendation signal limit is out of range")
        rows = await self.pool.fetch(
            """
            SELECT event_type, occurred_at, received_at, time_zone,
                   utc_offset_minutes, local_date, local_hour, query,
                   result_count, article_id, scene_index, scene_count
            FROM app_account_activity_events
            WHERE account_id=$1
              AND occurred_at >= $2
              AND occurred_at <= $3
              AND received_at <= $3
            ORDER BY occurred_at DESC, id DESC
            LIMIT $4
            """,
            account_id,
            since,
            snapshot_at,
            limit,
        )
        return [dict(row) for row in rows]

    async def recommendation_snapshot_at(self) -> datetime:
        value = await self.pool.fetchval("SELECT now()")
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("database did not return an aware recommendation snapshot")
        return value

    async def recommendation_feedback_rows(
        self, account_id: UUID, *, limit: int = 200
    ) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("recommendation feedback limit is out of range")
        rows = await self.pool.fetch(
            """
            SELECT article_id, reason, created_at, updated_at
            FROM app_article_feedback
            WHERE account_id=$1
            ORDER BY updated_at DESC, article_id DESC
            LIMIT $2
            """,
            account_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def recommendation_saved_rows(
        self, account_id: UUID, *, limit: int = 100
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("recommendation saved limit is out of range")
        rows = await self.pool.fetch(
            """
            SELECT saved.article_id,
                   saved.saved_at::timestamptz AS saved_at
            FROM app_account_reading_states state
            CROSS JOIN LATERAL jsonb_each_text(state.saved_articles)
                 AS saved(article_id, saved_at)
            WHERE state.account_id=$1
            ORDER BY saved.saved_at::timestamptz DESC, saved.article_id DESC
            LIMIT $2
            """,
            account_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def recommendation_exposure_rows(
        self,
        account_id: UUID,
        *,
        snapshot_at: datetime,
        continuation: bool,
        limit: int = 500,
    ) -> list[dict]:
        if snapshot_at.tzinfo is None:
            raise ValueError("recommendation exposure snapshot must be aware")
        if not 1 <= limit <= 500:
            raise ValueError("recommendation exposure limit is out of range")
        rows = await self.pool.fetch(
            """
            WITH account_exposures AS (
                SELECT request_id, article_id, position, lane, page_kind,
                       served_at, selected_at
                FROM app_recommendation_exposures
                WHERE account_id=$1 AND surface='home'
                UNION ALL
                SELECT response_id AS request_id, article_id, position,
                       'library'::text AS lane, 'initial'::text AS page_kind,
                       served_at, NULL::timestamptz AS selected_at
                FROM app_article_list_exposures
                WHERE account_id=$1 AND surface='library'
            )
            SELECT request_id, article_id, position, lane, page_kind,
                   served_at, selected_at
            FROM account_exposures
            WHERE (
                    ($3::boolean AND served_at >= $2)
                    OR (
                        NOT $3::boolean
                        AND page_kind='initial'
                        AND served_at >= $2 - interval '72 hours'
                        AND served_at <= $2
                    )
                  )
            ORDER BY served_at DESC, position ASC
            LIMIT $4
            """,
            account_id,
            snapshot_at,
            continuation,
            limit,
        )
        return [dict(row) for row in rows]

    async def record_recommendation_exposures(
        self,
        account_id: UUID,
        *,
        request_id: UUID,
        ranking_version: str,
        surface: str,
        page_kind: str,
        served_at: datetime,
        items: list[dict],
    ) -> int:
        if served_at.tzinfo is None:
            raise ValueError("recommendation exposure time must be aware")
        if not items:
            return 0
        payload = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
        recorded = await self.pool.fetchval(
            """
            WITH expired AS (
                DELETE FROM app_recommendation_exposures
                WHERE served_at < $6::timestamptz - interval '90 days'
            ),
            inserted AS (
                INSERT INTO app_recommendation_exposures(
                    request_id, account_id, article_id, ranking_version,
                    surface, page_kind, position, lane, served_at
                )
                SELECT $1, $2, item.article_id, $3, $4, $5,
                       item.position, item.lane, $6::timestamptz
                FROM jsonb_to_recordset($7::jsonb)
                     AS item(article_id text, position smallint, lane text)
                ON CONFLICT (request_id, article_id) DO NOTHING
                RETURNING article_id, served_at
            ),
            grouped AS (
                SELECT article_id, count(*)::bigint AS delta,
                       min(served_at) AS first_at, max(served_at) AS last_at
                FROM inserted
                GROUP BY article_id
            ),
            bumped AS (
                INSERT INTO article_feed_impression_counts(
                    article_id, impression_count, first_impressed_at,
                    last_impressed_at, updated_at
                )
                SELECT article_id, delta, first_at, last_at, $6::timestamptz
                FROM grouped
                ON CONFLICT (article_id) DO UPDATE SET
                    impression_count=(
                        article_feed_impression_counts.impression_count
                        + EXCLUDED.impression_count
                    ),
                    first_impressed_at=COALESCE(
                        article_feed_impression_counts.first_impressed_at,
                        EXCLUDED.first_impressed_at
                    ),
                    last_impressed_at=GREATEST(
                        COALESCE(
                            article_feed_impression_counts.last_impressed_at,
                            EXCLUDED.last_impressed_at
                        ),
                        EXCLUDED.last_impressed_at
                    ),
                    updated_at=EXCLUDED.updated_at
                RETURNING article_id
            )
            SELECT count(*)::bigint FROM inserted
            """,
            request_id,
            account_id,
            ranking_version,
            surface,
            page_kind,
            served_at,
            payload,
        )
        return int(recorded or 0)

    async def recommendation_article_features(
        self, article_ids: list[str]
    ) -> list[dict]:
        if not article_ids:
            return []
        rows = await self.pool.fetch(
            """
            SELECT f.article_id, f.terms, f.term_weights,
                   f.representative_topic, f.domain, f.published_at,
                   f.quality_score, COALESCE(m.data->>'headline','') AS headline
            FROM article_recommendation_features f
            JOIN article_maps m ON m.article_id=f.article_id
            WHERE f.article_id=ANY($1::text[])
              AND m.status IN ('ok','degraded')
              AND m.data IS NOT NULL
            """,
            list(dict.fromkeys(article_ids))[:700],
        )
        return [_decode_recommendation_row(row) for row in rows]

    async def recommendation_candidates(
        self,
        *,
        profile_terms: list[str],
        short_terms: list[str],
        saved_terms: list[str],
        context_terms: list[str],
        snapshot_at: datetime,
        editorial_day: date,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            """
            WITH eligible AS (
                SELECT a.id AS article_id,
                       COALESCE(f.terms, ARRAY[]::text[]) AS terms,
                       COALESCE(f.term_weights, '{}'::jsonb) AS term_weights,
                       f.representative_topic,
                       COALESCE(f.domain, a.domain) AS domain,
                       COALESCE(f.published_at, p.published_at, a.seendate,
                                a.first_seen_at) AS published_at,
                       COALESCE(
                           f.quality_score,
                           CASE WHEN m.status='ok' THEN 1.0 ELSE 0.65 END
                       ) AS quality_score,
                       COALESCE(m.data->>'headline', a.title, '') AS headline,
                       p.state AS publication_state,
                       p.published_at AS editorial_published_at
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                LEFT JOIN article_recommendation_features f
                       ON f.article_id=a.id
                WHERE m.status IN ('ok','degraded')
                  AND m.data IS NOT NULL
                  AND (p.draft_id IS NULL OR p.state='published')
                  AND COALESCE(f.published_at, p.published_at, a.seendate,
                               a.first_seen_at) <= $5
            ),
            fresh AS (
                SELECT eligible.*, 1 AS source_rank, 'now'::text AS source_name,
                       row_number() OVER (
                           ORDER BY published_at DESC, article_id DESC
                       ) AS source_position
                FROM eligible
                ORDER BY published_at DESC, article_id DESC
                LIMIT 120
            ),
            profile_matches AS (
                SELECT eligible.*, 2 AS source_rank, 'core'::text AS source_name,
                       row_number() OVER (
                           ORDER BY published_at DESC, article_id DESC
                       ) AS source_position
                FROM eligible
                WHERE cardinality($1::text[]) > 0
                  AND terms && $1::text[]
                ORDER BY published_at DESC, article_id DESC
                LIMIT 120
            ),
            short_matches AS (
                SELECT eligible.*, 3 AS source_rank, 'now'::text AS source_name,
                       row_number() OVER (
                           ORDER BY published_at DESC, article_id DESC
                       ) AS source_position
                FROM eligible
                WHERE cardinality($2::text[]) > 0
                  AND terms && $2::text[]
                ORDER BY published_at DESC, article_id DESC
                LIMIT 80
            ),
            saved_matches AS (
                SELECT eligible.*, 4 AS source_rank, 'adjacent'::text AS source_name,
                       row_number() OVER (
                           ORDER BY published_at DESC, article_id DESC
                       ) AS source_position
                FROM eligible
                WHERE cardinality($3::text[]) > 0
                  AND terms && $3::text[]
                ORDER BY published_at DESC, article_id DESC
                LIMIT 80
            ),
            context_matches AS (
                SELECT eligible.*, 5 AS source_rank, 'adjacent'::text AS source_name,
                       row_number() OVER (
                           ORDER BY published_at DESC, article_id DESC
                       ) AS source_position
                FROM eligible
                WHERE cardinality($4::text[]) > 0
                  AND terms && $4::text[]
                ORDER BY published_at DESC, article_id DESC
                LIMIT 80
            ),
            today_editorial AS (
                SELECT eligible.*, 6 AS source_rank, 'surprise'::text AS source_name,
                       row_number() OVER (
                           ORDER BY editorial_published_at DESC, article_id DESC
                       ) AS source_position
                FROM eligible
                WHERE publication_state='published'
                  AND (editorial_published_at AT TIME ZONE 'Asia/Seoul')::date=$6
                ORDER BY editorial_published_at DESC, article_id DESC
                LIMIT 20
            ),
            sources AS (
                SELECT * FROM fresh
                UNION ALL SELECT * FROM profile_matches
                UNION ALL SELECT * FROM short_matches
                UNION ALL SELECT * FROM saved_matches
                UNION ALL SELECT * FROM context_matches
                UNION ALL SELECT * FROM today_editorial
            ),
            source_summary AS (
                SELECT article_id,
                       min(source_rank) AS source_rank,
                       min(source_position) AS source_position,
                       array_agg(
                           DISTINCT source_name
                           ORDER BY source_name
                       ) AS candidate_sources
                FROM sources
                GROUP BY article_id
            ),
            first_source AS (
                SELECT DISTINCT ON (article_id) *
                FROM sources
                ORDER BY article_id, source_rank, source_position
            )
            SELECT first_source.article_id, first_source.terms,
                   first_source.term_weights,
                   first_source.representative_topic,
                   first_source.domain, first_source.published_at,
                   first_source.quality_score, first_source.headline,
                   source_summary.source_rank,
                   source_summary.source_position,
                   source_summary.candidate_sources
            FROM first_source
            JOIN source_summary
              ON source_summary.article_id=first_source.article_id
            ORDER BY source_summary.source_rank,
                     source_summary.source_position,
                     first_source.article_id DESC
            LIMIT 300
            """,
            profile_terms[:128],
            short_terms[:128],
            saved_terms[:128],
            context_terms[:64],
            snapshot_at,
            editorial_day,
        )
        return [_decode_recommendation_row(row) for row in rows]

    async def recommendation_feed_rows(self, article_ids: list[str]) -> list[dict]:
        if not article_ids:
            return []
        rows = await self.pool.fetch(
            """
            WITH requested AS (
                SELECT article_id, ordinal
                FROM unnest($1::text[]) WITH ORDINALITY
                     AS selected(article_id, ordinal)
            )
            SELECT a.id AS article_id, a.url AS source_url, a.domain,
                   COALESCE(p.published_at, a.seendate, a.first_seen_at)
                       AS published_at,
                   a.language AS source_language, a.sourcecountry,
                   a.document_type, a.document_source_name,
                   m.status, m.data
            FROM requested r
            JOIN articles a ON a.id=r.article_id
            JOIN article_maps m ON m.article_id=a.id
            LEFT JOIN editorial_publications p ON p.article_id=a.id
            WHERE m.status IN ('ok','degraded')
              AND m.data IS NOT NULL
              AND (p.draft_id IS NULL OR p.state='published')
            ORDER BY r.ordinal
            """,
            article_ids[:6],
        )
        out: list[dict] = []
        for raw in rows:
            row = dict(raw)
            if isinstance(row.get("data"), str):
                row["data"] = json.loads(row["data"])
            out.append(row)
        return out

    async def has_current_account_consent(
        self, account_id: UUID, policy_version: str
    ) -> bool:
        return bool(
            await self.pool.fetchval(
                """
                SELECT 1
                FROM app_privacy_consent_accounts link
                JOIN app_privacy_consents consent ON consent.id=link.consent_id
                JOIN app_attest_keys key ON key.key_id=consent.key_id
                WHERE link.account_id=$1
                  AND consent.policy_version=$2
                  AND key.installation_id=consent.installation_id
                  AND key.status='active'
                LIMIT 1
                """,
                account_id,
                policy_version,
            )
        )

    async def link_account_privacy_consent(
        self, account_id: UUID, consent_id: UUID, policy_version: str
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            """
            WITH eligible AS (
                SELECT consent.id AS consent_id, account.id AS account_id,
                       consent.policy_version, consent.policy_url,
                       consent.accepted_at
                FROM app_accounts account
                JOIN app_privacy_consents consent ON consent.id=$2
                JOIN app_attest_keys key ON key.key_id=consent.key_id
                WHERE account.id=$1
                  AND consent.policy_version=$3
                  AND key.installation_id=consent.installation_id
                  AND key.status='active'
            ),
            linked AS (
                INSERT INTO app_privacy_consent_accounts(consent_id,account_id)
                SELECT consent_id,account_id FROM eligible
                ON CONFLICT (consent_id,account_id) DO UPDATE SET
                    linked_at=app_privacy_consent_accounts.linked_at
                RETURNING consent_id,account_id,linked_at
            )
            SELECT eligible.consent_id, eligible.account_id,
                   eligible.policy_version, eligible.policy_url,
                   eligible.accepted_at, linked.linked_at
            FROM eligible
            JOIN linked
              ON linked.consent_id=eligible.consent_id
             AND linked.account_id=eligible.account_id
            """,
            account_id,
            consent_id,
            policy_version,
        )
        return dict(row) if row else None

    # ---- account-scoped reading state ---------------------------------
    async def get_account_reading_state(self, account_id: UUID) -> dict:
        return await self.patch_account_reading_state(
            account_id,
            saved_changes=[],
            progress_changes=[],
            settings=None,
            update_last_article=False,
            last_article_id=None,
        )

    async def patch_account_reading_state(
        self,
        account_id: UUID,
        *,
        saved_changes: list[dict],
        progress_changes: list[dict],
        settings: dict | None,
        update_last_article: bool,
        last_article_id: str | None,
    ) -> dict:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO app_account_reading_states(account_id)
                    SELECT id FROM app_accounts WHERE id=$1
                    ON CONFLICT (account_id) DO NOTHING
                    """,
                    account_id,
                )
                row = await conn.fetchrow(
                    """
                    SELECT account_id,revision,saved_articles,scene_progress,
                           last_article_id,settings,created_at,updated_at
                    FROM app_account_reading_states
                    WHERE account_id=$1
                    FOR UPDATE
                    """,
                    account_id,
                )
                if row is None:
                    raise LookupError("account not found")

                saved = _json_object(row["saved_articles"])
                progress = _json_object(row["scene_progress"])
                current_settings = _normalized_reader_settings(row["settings"])
                current_last = row["last_article_id"]
                before = (saved.copy(), progress.copy(), current_last, current_settings.copy())

                candidate_ids = set(saved) | set(progress)
                candidate_ids.update(
                    change["article_id"]
                    for change in saved_changes
                    if change.get("saved")
                )
                candidate_ids.update(change["article_id"] for change in progress_changes)
                if current_last:
                    candidate_ids.add(str(current_last))
                if update_last_article and last_article_id:
                    candidate_ids.add(last_article_id)
                available_ids: set[str] = set()
                if candidate_ids:
                    rows = await conn.fetch(
                        """
                        SELECT article.id
                        FROM articles article
                        JOIN article_maps map ON map.article_id=article.id
                        LEFT JOIN editorial_publications publication
                          ON publication.article_id=article.id
                        WHERE article.id=ANY($1::text[])
                          AND map.status IN ('ok','degraded')
                          AND map.data IS NOT NULL
                          AND (publication.draft_id IS NULL
                               OR publication.state='published')
                        """,
                        sorted(candidate_ids),
                    )
                    available_ids = {str(item["id"]) for item in rows}

                saved = {
                    article_id: value
                    for article_id, value in saved.items()
                    if article_id in available_ids and isinstance(value, str)
                }
                saved_before_changes = set(saved)
                progress = {
                    article_id: value
                    for article_id, value in progress.items()
                    if article_id in available_ids and _valid_progress_value(value)
                }
                if current_last not in available_ids:
                    current_last = None

                bookmark_transitions: list[tuple[UUID, str, str]] = []
                for change in saved_changes:
                    article_id = change["article_id"]
                    was_saved = article_id in saved_before_changes
                    desired_saved = bool(change["saved"])
                    if not change["saved"]:
                        saved.pop(article_id, None)
                    elif article_id in available_ids:
                        saved_at = change["saved_at"]
                        if saved_at.tzinfo is None or saved_at.utcoffset() is None:
                            raise ValueError("saved_at must include a timezone")
                        saved[article_id] = saved_at.astimezone(UTC).isoformat()
                    if (
                        article_id in available_ids
                        and was_saved != desired_saved
                    ):
                        bookmark_transitions.append(
                            (
                                uuid4(),
                                article_id,
                                "saved" if desired_saved else "unsaved",
                            )
                        )
                saved = dict(
                    sorted(
                        saved.items(),
                        key=lambda item: (item[1], item[0]),
                        reverse=True,
                    )[:100]
                )

                now = await conn.fetchval("SELECT now()")
                if bookmark_transitions:
                    await conn.executemany(
                        """
                        INSERT INTO article_bookmark_events(
                            id,
                            account_id,
                            article_id,
                            event_type,
                            received_at
                        )
                        VALUES ($1,$2,$3,$4,$5)
                        """,
                        [
                            (event_id, account_id, article_id, event_type, now)
                            for event_id, article_id, event_type
                            in bookmark_transitions
                        ],
                    )
                await conn.execute(
                    """
                    DELETE FROM article_bookmark_events
                    WHERE id IN (
                        SELECT id
                        FROM article_bookmark_events
                        WHERE received_at < now() - interval '30 days'
                        ORDER BY received_at, id
                        LIMIT 1000
                    )
                    """
                )
                now_text = now.astimezone(UTC).isoformat()
                for change in progress_changes:
                    article_id = change["article_id"]
                    if article_id in available_ids:
                        desired_index = int(change["scene_index"])
                        current = progress.get(article_id)
                        if not isinstance(current, dict) or current.get("scene_index") != desired_index:
                            progress[article_id] = {
                                "scene_index": desired_index,
                                "updated_at": now_text,
                            }
                progress = dict(
                    sorted(
                        progress.items(),
                        key=lambda item: (item[1]["updated_at"], item[0]),
                        reverse=True,
                    )[:500]
                )

                if settings is not None:
                    current_settings = _normalized_reader_settings(settings)
                if update_last_article:
                    current_last = (
                        last_article_id if last_article_id in available_ids else None
                    )

                changed = before != (saved, progress, current_last, current_settings)
                revision = int(row["revision"])
                if changed:
                    revision += 1
                    updated = await conn.fetchrow(
                        """
                        UPDATE app_account_reading_states
                        SET revision=$2, saved_articles=$3::jsonb,
                            scene_progress=$4::jsonb, last_article_id=$5,
                            settings=$6::jsonb, updated_at=$7
                        WHERE account_id=$1
                        RETURNING created_at,updated_at
                        """,
                        account_id,
                        revision,
                        json.dumps(saved, ensure_ascii=False, sort_keys=True),
                        json.dumps(progress, ensure_ascii=False, sort_keys=True),
                        current_last,
                        json.dumps(current_settings, ensure_ascii=False, sort_keys=True),
                        now,
                    )
                    created_at = updated["created_at"]
                    updated_at = updated["updated_at"]
                else:
                    created_at = row["created_at"]
                    updated_at = row["updated_at"]
                return {
                    "account_id": account_id,
                    "revision": revision,
                    "saved_articles": saved,
                    "scene_progress": {
                        article_id: int(value["scene_index"])
                        for article_id, value in progress.items()
                    },
                    "last_article_id": current_last,
                    "settings": current_settings,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }

    async def account_saved_article_maps(self, account_id: UUID) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT article.id AS article_id, article.url AS source_url,
                   article.domain, article.seendate AS published_at,
                   article.language AS source_language, article.sourcecountry,
                   article.document_type, article.document_source_name,
                   map.status, map.data,
                   (state.saved_articles->>article.id)::timestamptz AS saved_at
            FROM app_account_reading_states state
            JOIN articles article
              ON state.saved_articles ? article.id
            JOIN article_maps map ON map.article_id=article.id
            LEFT JOIN editorial_publications publication
              ON publication.article_id=article.id
            WHERE state.account_id=$1
              AND map.status IN ('ok','degraded')
              AND map.data IS NOT NULL
              AND (publication.draft_id IS NULL
                   OR publication.state='published')
            ORDER BY saved_at DESC, article.id DESC
            LIMIT 100
            """,
            account_id,
        )
        out: list[dict] = []
        for raw_row in rows:
            item = dict(raw_row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    # ---- signed-in APNs registrations ---------------------------------
    async def upsert_push_registration(
        self,
        *,
        account_id: UUID,
        session_id: UUID,
        installation_id: str,
        key_id: str,
        token_hash: bytes,
        token_ciphertext: bytes,
        environment: str,
        editorial_enabled: bool,
        weekly_insight_enabled: bool,
        timeline_updates_enabled: bool,
        timeline_refreshes_enabled: bool = False,
        timeline_additions_enabled: bool = False,
    ) -> dict:
        registration_id = uuid4()
        try:
            row = await self.pool.fetchrow(
                """
                INSERT INTO app_push_registrations
                    (id,account_id,session_id,installation_id,key_id,
                     token_hash,token_ciphertext,environment,
                     editorial_enabled,weekly_insight_enabled,
                     timeline_updates_enabled,timeline_refreshes_enabled,
                     timeline_additions_enabled)
                SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
                FROM app_attest_keys key
                WHERE key.key_id=$5 AND key.installation_id=$4
                  AND key.status='active'
                ON CONFLICT (installation_id) DO UPDATE SET
                    account_id=EXCLUDED.account_id,
                    session_id=EXCLUDED.session_id,
                    key_id=EXCLUDED.key_id,
                    token_hash=EXCLUDED.token_hash,
                    token_ciphertext=EXCLUDED.token_ciphertext,
                    environment=EXCLUDED.environment,
                    editorial_enabled=EXCLUDED.editorial_enabled,
                    weekly_insight_enabled=EXCLUDED.weekly_insight_enabled,
                    timeline_updates_enabled=EXCLUDED.timeline_updates_enabled,
                    timeline_refreshes_enabled=EXCLUDED.timeline_refreshes_enabled,
                    timeline_additions_enabled=EXCLUDED.timeline_additions_enabled,
                    status='active', last_error=NULL, invalidated_at=NULL,
                    updated_at=now(), last_seen_at=now()
                RETURNING id,account_id,session_id,installation_id,key_id,
                          environment,editorial_enabled,weekly_insight_enabled,
                          timeline_updates_enabled,
                          timeline_refreshes_enabled,timeline_additions_enabled,
                          status,created_at,updated_at,last_seen_at
                """,
                registration_id,
                account_id,
                session_id,
                installation_id,
                key_id,
                token_hash,
                token_ciphertext,
                environment,
                editorial_enabled,
                weekly_insight_enabled,
                timeline_updates_enabled,
                timeline_refreshes_enabled,
                timeline_additions_enabled,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError("push token belongs to another installation") from exc
        if row is None:
            raise LookupError("attested installation is not active")
        return dict(row)

    async def update_push_preferences(
        self,
        *,
        account_id: UUID,
        installation_id: str,
        editorial_enabled: bool,
        weekly_insight_enabled: bool,
        timeline_updates_enabled: bool = False,
        timeline_refreshes_enabled: bool = False,
        timeline_additions_enabled: bool = False,
    ) -> dict | None:
        row = await self.pool.fetchrow(
            """
            WITH updated AS (
                UPDATE app_push_registrations
                SET editorial_enabled=$3, weekly_insight_enabled=$4,
                    timeline_updates_enabled=$5,
                    timeline_refreshes_enabled=$6,
                    timeline_additions_enabled=$7,
                    updated_at=now(), last_seen_at=now()
                WHERE account_id=$1 AND installation_id=$2
                  AND status='active'
                RETURNING id,installation_id,environment,
                          editorial_enabled,weekly_insight_enabled
                          ,timeline_updates_enabled
                          ,timeline_refreshes_enabled,timeline_additions_enabled
            ), cancelled_editorial AS (
                UPDATE editorial_push_deliveries delivery
                SET status='cancelled', lease_expires_at=NULL,
                    last_error='preference_disabled', updated_at=now()
                FROM editorial_push_notifications notification, updated
                WHERE delivery.registration_id=updated.id
                  AND delivery.notification_id=notification.id
                  AND delivery.status IN ('queued','processing')
                  AND ((notification.publication_kind='editorial'
                        AND NOT updated.editorial_enabled)
                       OR (notification.publication_kind='weekly_insight'
                           AND NOT updated.weekly_insight_enabled))
                RETURNING 1
            ), cancelled_timeline AS (
                UPDATE explore_timeline_push_deliveries delivery
                SET status='cancelled', lease_expires_at=NULL,
                    last_error='preference_disabled', updated_at=now()
                FROM explore_timeline_push_notifications notification, updated
                WHERE delivery.registration_id=updated.id
                  AND delivery.notification_id=notification.id
                  AND delivery.status IN ('queued','processing')
                  AND ((notification.update_kind='inserted'
                        AND NOT updated.timeline_refreshes_enabled)
                       OR (notification.update_kind='occurred'
                           AND NOT updated.timeline_additions_enabled))
                RETURNING 1
            )
            SELECT installation_id,environment,
                   editorial_enabled,weekly_insight_enabled,
                   timeline_updates_enabled
                   ,timeline_refreshes_enabled,timeline_additions_enabled
            FROM updated
            """,
            account_id,
            installation_id,
            editorial_enabled,
            weekly_insight_enabled,
            timeline_updates_enabled,
            timeline_refreshes_enabled,
            timeline_additions_enabled,
        )
        return dict(row) if row else None

    async def delete_push_registration(
        self, *, account_id: UUID, installation_id: str
    ) -> bool:
        result = await self.pool.execute(
            """
            DELETE FROM app_push_registrations
            WHERE account_id=$1 AND installation_id=$2
            """,
            account_id,
            installation_id,
        )
        return result == "DELETE 1"

    async def timeline_subscriptions(self, account_id: UUID) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT subscription.timeline_stable_key,subscription.created_at,
                   subscription.updated_at,metadata.title,metadata.summary,
                   metadata.started_at,metadata.ended_at
            FROM app_timeline_subscriptions subscription
            LEFT JOIN LATERAL (
                SELECT timeline.title,timeline.summary,timeline.started_at,
                       timeline.ended_at
                FROM explore_timelines timeline
                JOIN explore_timeline_catalogs catalog
                  ON catalog.id=timeline.catalog_id
                WHERE timeline.stable_key=subscription.timeline_stable_key
                  AND catalog.status IN ('published','superseded')
                ORDER BY catalog.generated_at DESC,timeline.id DESC
                LIMIT 1
            ) metadata ON TRUE
            WHERE subscription.account_id=$1
            ORDER BY subscription.created_at,subscription.timeline_stable_key
            LIMIT 64
            """,
            account_id,
        )
        return [dict(row) for row in rows]

    async def upsert_timeline_subscription(
        self,
        *,
        account_id: UUID,
        stable_key: str,
        stale_hours: int = 72,
        max_subscriptions: int = 64,
    ) -> dict | None:
        if (
            not 1 <= len(stable_key) <= 128
            or not 1 <= stale_hours <= 168
            or not 1 <= max_subscriptions <= 64
        ):
            raise ValueError("invalid timeline subscription bounds")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                account = await connection.fetchrow(
                    "SELECT id FROM app_accounts WHERE id=$1 FOR UPDATE",
                    account_id,
                )
                if account is None:
                    raise LookupError("account not found")
                existing = await connection.fetchrow(
                    """
                    SELECT timeline_stable_key,created_at,updated_at
                    FROM app_timeline_subscriptions
                    WHERE account_id=$1 AND timeline_stable_key=$2
                    """,
                    account_id,
                    stable_key,
                )
                if existing is not None:
                    return dict(existing)
                count = int(
                    await connection.fetchval(
                        """
                        SELECT count(*) FROM app_timeline_subscriptions
                        WHERE account_id=$1
                        """,
                        account_id,
                    )
                )
                if count >= max_subscriptions:
                    raise OverflowError("timeline subscription limit reached")
                row = await connection.fetchrow(
                    """
                    INSERT INTO app_timeline_subscriptions
                        (account_id,timeline_stable_key)
                    SELECT $1,$2
                    WHERE EXISTS (
                        SELECT 1
                        FROM explore_timelines timeline
                        JOIN explore_timeline_catalogs catalog
                          ON catalog.id=timeline.catalog_id
                        WHERE timeline.stable_key=$2
                          AND catalog.status='published'
                          AND catalog.valid_until > now()-$3::interval
                    )
                    ON CONFLICT (account_id,timeline_stable_key) DO UPDATE SET
                        updated_at=app_timeline_subscriptions.updated_at
                    RETURNING timeline_stable_key,created_at,updated_at
                    """,
                    account_id,
                    stable_key,
                    timedelta(hours=stale_hours),
                )
                if row is not None:
                    await connection.execute(
                        """
                        INSERT INTO app_timeline_subscription_events
                            (id,account_id,timeline_stable_key,event_type)
                        VALUES ($1,$2,$3,'subscribe')
                        """,
                        uuid4(),
                        account_id,
                        stable_key,
                    )
        return dict(row) if row else None

    async def delete_timeline_subscription(
        self, *, account_id: UUID, stable_key: str
    ) -> bool:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """
                    DELETE FROM app_timeline_subscriptions
                    WHERE account_id=$1 AND timeline_stable_key=$2
                    """,
                    account_id,
                    stable_key,
                )
                if result == "DELETE 1":
                    await connection.execute(
                        """
                        INSERT INTO app_timeline_subscription_events
                            (id,account_id,timeline_stable_key,event_type)
                        VALUES ($1,$2,$3,'unsubscribe')
                        """,
                        uuid4(),
                        account_id,
                        stable_key,
                    )
                await connection.execute(
                    """
                    UPDATE explore_timeline_push_deliveries delivery
                    SET status='cancelled',lease_expires_at=NULL,
                        last_error='subscription_disabled',updated_at=now()
                    FROM explore_timeline_push_notifications notification,
                         app_push_registrations registration
                    WHERE notification.id=delivery.notification_id
                      AND registration.id=delivery.registration_id
                      AND registration.account_id=$1
                      AND notification.timeline_stable_key=$2
                      AND delivery.status IN ('queued','processing')
                    """,
                    account_id,
                    stable_key,
                )
        return result == "DELETE 1"

    async def timeline_subscription_history(
        self, account_id: UUID, *, limit: int = 100
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid timeline subscription history limit")
        rows = await self.pool.fetch(
            """
            SELECT event.id,event.timeline_stable_key,event.event_type,
                   event.occurred_at,
                   (subscription.account_id IS NOT NULL) AS is_currently_subscribed,
                   metadata.title
            FROM app_timeline_subscription_events event
            LEFT JOIN app_timeline_subscriptions subscription
              ON subscription.account_id=event.account_id
             AND subscription.timeline_stable_key=event.timeline_stable_key
            LEFT JOIN LATERAL (
                SELECT timeline.title
                FROM explore_timelines timeline
                JOIN explore_timeline_catalogs catalog
                  ON catalog.id=timeline.catalog_id
                WHERE timeline.stable_key=event.timeline_stable_key
                  AND catalog.status IN ('published','superseded')
                ORDER BY catalog.generated_at DESC,timeline.id DESC
                LIMIT 1
            ) metadata ON TRUE
            WHERE event.account_id=$1
            ORDER BY event.occurred_at DESC,event.id DESC
            LIMIT $2
            """,
            account_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def cancel_ineligible_push_deliveries(self) -> int:
        result = await self.pool.execute(
            """
            UPDATE editorial_push_deliveries delivery
            SET status='cancelled', lease_expires_at=NULL,
                last_error='registration_ineligible', updated_at=now()
            WHERE delivery.status IN ('queued','processing')
              AND NOT EXISTS (
                  SELECT 1
                  FROM app_push_registrations registration
                  JOIN app_account_sessions session
                    ON session.id=registration.session_id
                  WHERE registration.id=delivery.registration_id
                    AND registration.status='active'
                    AND session.expires_at > now()
              )
            """
        )
        return int(result.rsplit(" ", 1)[-1])

    async def cancel_ineligible_timeline_push_deliveries(self) -> int:
        result = await self.pool.execute(
            """
            UPDATE explore_timeline_push_deliveries delivery
            SET status='cancelled',lease_expires_at=NULL,
                last_error='registration_ineligible',updated_at=now()
            FROM explore_timeline_push_notifications notification,
                 app_push_registrations registration
            WHERE delivery.notification_id=notification.id
              AND delivery.registration_id=registration.id
              AND delivery.status IN ('queued','processing')
              AND (
                  registration.status<>'active'
                  OR (notification.update_kind='inserted'
                      AND NOT registration.timeline_refreshes_enabled)
                  OR (notification.update_kind='occurred'
                      AND NOT registration.timeline_additions_enabled)
                  OR NOT EXISTS (
                      SELECT 1 FROM app_account_sessions session
                      WHERE session.id=registration.session_id
                        AND session.expires_at > now()
                  )
                  OR NOT EXISTS (
                      SELECT 1 FROM app_timeline_subscriptions subscription
                      WHERE subscription.account_id=registration.account_id
                        AND subscription.timeline_stable_key=
                            notification.timeline_stable_key
                  )
              )
            """
        )
        return int(result.rsplit(" ", 1)[-1])

    async def claim_push_deliveries(
        self, *, limit: int, lease_seconds: float
    ) -> list[dict]:
        bounded = max(1, min(int(limit), 100))
        rows = await self.pool.fetch(
            """
            WITH due AS (
                SELECT delivery.notification_id, delivery.registration_id
                FROM editorial_push_deliveries delivery
                JOIN app_push_registrations registration
                  ON registration.id=delivery.registration_id
                JOIN app_account_sessions session
                  ON session.id=registration.session_id
                WHERE (
                    (delivery.status='queued'
                     AND delivery.next_attempt_at <= now())
                    OR
                    (delivery.status='processing'
                     AND delivery.lease_expires_at <= now())
                )
                AND registration.status='active'
                AND session.expires_at > now()
                ORDER BY delivery.next_attempt_at, delivery.created_at,
                         delivery.notification_id, delivery.registration_id
                FOR UPDATE OF delivery SKIP LOCKED
                LIMIT $1
            )
            UPDATE editorial_push_deliveries delivery
            SET status='processing', attempt_count=attempt_count+1,
                lease_expires_at=now()+($2 * interval '1 second'),
                last_error=NULL, updated_at=now()
            FROM due, editorial_push_notifications notification,
                 app_push_registrations registration
            WHERE delivery.notification_id=due.notification_id
              AND delivery.registration_id=due.registration_id
              AND notification.id=delivery.notification_id
              AND registration.id=delivery.registration_id
            RETURNING delivery.notification_id, delivery.registration_id,
                      delivery.attempt_count, notification.headline,
                      notification.body, notification.article_id,
                      notification.publication_kind,
                      registration.token_ciphertext, registration.environment
            """,
            bounded,
            lease_seconds,
        )
        return [dict(row) for row in rows]

    async def claim_timeline_push_deliveries(
        self, *, limit: int, lease_seconds: float
    ) -> list[dict]:
        bounded = max(1, min(int(limit), 100))
        rows = await self.pool.fetch(
            """
            WITH due AS (
                SELECT delivery.notification_id,delivery.registration_id
                FROM explore_timeline_push_deliveries delivery
                JOIN explore_timeline_push_notifications notification
                  ON notification.id=delivery.notification_id
                JOIN app_push_registrations registration
                  ON registration.id=delivery.registration_id
                JOIN app_account_sessions session
                  ON session.id=registration.session_id
                JOIN app_timeline_subscriptions subscription
                  ON subscription.account_id=registration.account_id
                 AND subscription.timeline_stable_key=
                     notification.timeline_stable_key
                WHERE (
                    (delivery.status='queued'
                     AND delivery.next_attempt_at <= now())
                    OR
                    (delivery.status='processing'
                     AND delivery.lease_expires_at <= now())
                )
                  AND registration.status='active'
                  AND ((notification.update_kind='inserted'
                        AND registration.timeline_refreshes_enabled)
                       OR (notification.update_kind='occurred'
                           AND registration.timeline_additions_enabled))
                  AND session.expires_at > now()
                ORDER BY delivery.next_attempt_at,delivery.created_at,
                         delivery.notification_id,delivery.registration_id
                FOR UPDATE OF delivery SKIP LOCKED
                LIMIT $1
            )
            UPDATE explore_timeline_push_deliveries delivery
            SET status='processing',attempt_count=attempt_count+1,
                lease_expires_at=now()+($2 * interval '1 second'),
                last_error=NULL,updated_at=now()
            FROM due,explore_timeline_push_notifications notification,
                 app_push_registrations registration
            WHERE delivery.notification_id=due.notification_id
              AND delivery.registration_id=due.registration_id
              AND notification.id=delivery.notification_id
              AND registration.id=delivery.registration_id
            RETURNING delivery.notification_id,delivery.registration_id,
                      delivery.attempt_count,notification.id AS event_id,
                      notification.timeline_stable_key,
                      notification.node_id,notification.update_kind,
                      registration.token_ciphertext,registration.environment
            """,
            bounded,
            lease_seconds,
        )
        return [dict(row) for row in rows]

    async def complete_push_delivery(
        self,
        *,
        notification_id: UUID,
        registration_id: UUID,
        apns_id: UUID,
    ) -> bool:
        result = await self.pool.execute(
            """
            UPDATE editorial_push_deliveries
            SET status='sent', apns_id=$3, delivered_at=now(),
                lease_expires_at=NULL, last_error=NULL, updated_at=now()
            WHERE notification_id=$1 AND registration_id=$2
              AND status='processing'
            """,
            notification_id,
            registration_id,
            apns_id,
        )
        return result == "UPDATE 1"

    async def complete_timeline_push_delivery(
        self,
        *,
        notification_id: UUID,
        registration_id: UUID,
        apns_id: UUID,
    ) -> bool:
        result = await self.pool.execute(
            """
            UPDATE explore_timeline_push_deliveries
            SET status='sent',apns_id=$3,delivered_at=now(),
                lease_expires_at=NULL,last_error=NULL,updated_at=now()
            WHERE notification_id=$1 AND registration_id=$2
              AND status='processing'
            """,
            notification_id,
            registration_id,
            apns_id,
        )
        return result == "UPDATE 1"

    async def retry_push_delivery(
        self,
        *,
        notification_id: UUID,
        registration_id: UUID,
        error_code: str,
        transient: bool,
        max_attempts: int,
        delay_seconds: float,
    ) -> str:
        safe_error = _safe_push_error(error_code)
        terminal = not transient
        row = await self.pool.fetchrow(
            """
            UPDATE editorial_push_deliveries
            SET status=CASE
                    WHEN $4 OR attempt_count >= $5 THEN 'failed'
                    ELSE 'queued'
                END,
                next_attempt_at=CASE
                    WHEN $4 OR attempt_count >= $5 THEN now()
                    ELSE now()+($6 * interval '1 second')
                END,
                lease_expires_at=NULL, last_error=$3, updated_at=now()
            WHERE notification_id=$1 AND registration_id=$2
              AND status='processing'
            RETURNING status
            """,
            notification_id,
            registration_id,
            safe_error,
            terminal,
            max_attempts,
            delay_seconds,
        )
        return str(row["status"]) if row else "missing"

    async def retry_timeline_push_delivery(
        self,
        *,
        notification_id: UUID,
        registration_id: UUID,
        error_code: str,
        transient: bool,
        max_attempts: int,
        delay_seconds: float,
    ) -> str:
        safe_error = _safe_push_error(error_code)
        terminal = not transient
        row = await self.pool.fetchrow(
            """
            UPDATE explore_timeline_push_deliveries
            SET status=CASE
                    WHEN $4 OR attempt_count >= $5 THEN 'failed'
                    ELSE 'queued'
                END,
                next_attempt_at=CASE
                    WHEN $4 OR attempt_count >= $5 THEN now()
                    ELSE now()+($6 * interval '1 second')
                END,
                lease_expires_at=NULL,last_error=$3,updated_at=now()
            WHERE notification_id=$1 AND registration_id=$2
              AND status='processing'
            RETURNING status
            """,
            notification_id,
            registration_id,
            safe_error,
            terminal,
            max_attempts,
            delay_seconds,
        )
        return str(row["status"]) if row else "missing"

    async def invalidate_push_registration(
        self, *, registration_id: UUID, error_code: str
    ) -> bool:
        safe_error = _safe_push_error(error_code)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """
                    UPDATE app_push_registrations
                    SET status='invalid', last_error=$2, invalidated_at=now(),
                        updated_at=now()
                    WHERE id=$1 AND status='active'
                    """,
                    registration_id,
                    safe_error,
                )
                await connection.execute(
                    """
                    UPDATE editorial_push_deliveries
                    SET status='failed', last_error=$2, lease_expires_at=NULL,
                        updated_at=now()
                    WHERE registration_id=$1
                      AND status IN ('queued','processing')
                    """,
                    registration_id,
                    safe_error,
                )
                await connection.execute(
                    """
                    UPDATE explore_timeline_push_deliveries
                    SET status='failed',last_error=$2,lease_expires_at=NULL,
                        updated_at=now()
                    WHERE registration_id=$1
                      AND status IN ('queued','processing')
                    """,
                    registration_id,
                    safe_error,
                )
        return result == "UPDATE 1"

    async def push_delivery_stats(self) -> dict:
        """Return aggregate-only editorial push operations metrics."""
        row = await self.pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE delivery.status='queued') AS queued,
                count(*) FILTER (WHERE delivery.status='processing') AS processing,
                count(*) FILTER (WHERE delivery.status='sent') AS sent,
                count(*) FILTER (WHERE delivery.status='failed') AS failed,
                count(*) FILTER (WHERE delivery.status='cancelled') AS cancelled,
                COALESCE(sum(
                    CASE
                        WHEN delivery.status='queued'
                             AND delivery.attempt_count > 0
                            THEN delivery.attempt_count
                        ELSE greatest(delivery.attempt_count - 1, 0)
                    END
                ), 0) AS retry_attempts,
                max(delivery.delivered_at)
                    FILTER (WHERE delivery.status='sent') AS last_sent_at,
                max(delivery.updated_at)
                    FILTER (WHERE delivery.status='failed') AS last_failed_at,
                max(delivery.updated_at)
                    FILTER (
                        WHERE (
                            delivery.status='queued'
                            AND delivery.attempt_count > 0
                        ) OR (
                            delivery.status <> 'queued'
                            AND delivery.attempt_count > 1
                        )
                    ) AS last_retry_at,
                (
                    SELECT count(*)
                    FROM app_push_registrations registration
                    WHERE registration.status='active'
                ) AS active_registrations,
                (
                    SELECT count(*)
                    FROM app_push_registrations registration
                    WHERE registration.status='invalid'
                ) AS invalid_registrations
            FROM editorial_push_deliveries delivery
            """
        )
        if row is None:
            return {
                "queued": 0,
                "processing": 0,
                "sent": 0,
                "failed": 0,
                "cancelled": 0,
                "retry_attempts": 0,
                "active_registrations": 0,
                "invalid_registrations": 0,
                "last_sent_at": None,
                "last_failed_at": None,
                "last_retry_at": None,
            }
        return dict(row)

    async def unmapped_article_ids(self, limit: int = 100) -> list[str]:
        rows = await self.pool.fetch(
            """
            SELECT a.id FROM articles a
            LEFT JOIN article_maps m
              ON m.article_id = a.id AND m.status <> 'failed'
            WHERE m.article_id IS NULL
            ORDER BY a.first_seen_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [r["id"] for r in rows]

    # ---- automatic discovery curation ---------------------------------
    async def curation_candidate_rows(
        self, *, limit: int, since_days: int
    ) -> list[dict]:
        if not 8 <= limit <= 64 or not 1 <= since_days <= 90:
            raise ValueError("invalid curation candidate bounds")
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id, a.title, a.domain,
                   a.document_type, a.document_source_name,
                   COALESCE(a.seendate, a.first_seen_at) AS published_at,
                   m.status, m.data, m.updated_at
            FROM articles a
            JOIN article_maps m ON m.article_id=a.id
            LEFT JOIN editorial_publications p ON p.article_id=a.id
            WHERE m.status='ok'
              AND m.data IS NOT NULL
              AND m.data->>'format_version'='3'
              AND m.data->'editorial'->>'critic_verdict'='pass'
              AND COALESCE(a.seendate, a.first_seen_at) >= now()-$2::interval
              AND (p.draft_id IS NULL OR p.state='published')
            ORDER BY COALESCE(a.seendate, a.first_seen_at) DESC, a.id DESC
            LIMIT $1
            """,
            limit,
            timedelta(days=since_days),
        )
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def curation_sample_accounts(
        self,
        *,
        snapshot_at: datetime,
        since_days: int = 180,
        limit: int = 100,
        seed: int,
    ) -> dict:
        """Return a seeded uniform account sample without exposing it externally."""
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("snapshot_at must be timezone-aware")
        if not 1 <= since_days <= 365 or not 1 <= limit <= 100:
            raise ValueError("invalid curation audience bounds")
        if not -(2**63) <= seed < 2**63:
            raise ValueError("curation audience seed is out of range")
        row = await self.pool.fetchrow(
            """
            WITH eligible AS (
                SELECT e.account_id
                FROM app_account_activity_events e
                WHERE e.occurred_at >= $1::timestamptz-$2::interval
                  AND e.occurred_at <= $1::timestamptz
                UNION
                SELECT f.account_id
                FROM app_article_feedback f
                WHERE f.updated_at >= $1::timestamptz-$2::interval
                  AND f.updated_at <= $1::timestamptz
                UNION
                SELECT s.account_id
                FROM app_account_reading_states s
                WHERE s.updated_at >= $1::timestamptz-$2::interval
                  AND s.updated_at <= $1::timestamptz
                  AND s.saved_articles <> '{}'::jsonb
            ), sampled AS (
                SELECT account_id
                FROM eligible
                ORDER BY hashtextextended(account_id::text,$3), account_id
                LIMIT $4
            )
            SELECT
                (SELECT count(*) FROM eligible) AS eligible_user_count,
                COALESCE(
                    (SELECT array_agg(account_id ORDER BY
                        hashtextextended(account_id::text,$3), account_id)
                     FROM sampled),
                    ARRAY[]::uuid[]
                ) AS account_ids
            """,
            snapshot_at,
            timedelta(days=since_days),
            seed,
            limit,
        )
        if row is None:
            return {"eligible_user_count": 0, "account_ids": []}
        return {
            "eligible_user_count": int(row["eligible_user_count"] or 0),
            "account_ids": list(row["account_ids"] or []),
        }

    async def curation_audience_signal_rows(
        self,
        *,
        account_ids: Sequence[UUID],
        snapshot_at: datetime,
        since_days: int = 180,
        event_limit_per_account: int = 500,
    ) -> list[dict]:
        """Hydrate bounded sample signals for in-memory global aggregation."""
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise ValueError("snapshot_at must be timezone-aware")
        unique_ids = list(dict.fromkeys(account_ids))
        if (
            len(unique_ids) > 100
            or not 1 <= since_days <= 365
            or not 1 <= event_limit_per_account <= 500
        ):
            raise ValueError("invalid curation audience signal bounds")
        if not unique_ids:
            return []
        rows = await self.pool.fetch(
            """
            WITH ranked_events AS (
                SELECT e.*,
                       row_number() OVER (
                           PARTITION BY e.account_id
                           ORDER BY e.occurred_at DESC, e.id DESC
                       ) AS event_rank
                FROM app_account_activity_events e
                WHERE e.account_id=ANY($1::uuid[])
                  AND e.occurred_at >= $2::timestamptz-$3::interval
                  AND e.occurred_at <= $2::timestamptz
            ), activity AS (
                SELECT account_id,event_type AS signal_type,occurred_at,
                       local_date,query,article_id,scene_index,scene_count,
                       NULL::text AS feedback_reason
                FROM ranked_events
                WHERE event_rank <= $4
            ), ranked_saved AS (
                SELECT s.account_id,'saved'::text AS signal_type,
                       saved.value::timestamptz AS occurred_at,
                       (saved.value::timestamptz AT TIME ZONE 'UTC')::date
                           AS local_date,
                       NULL::text AS query,saved.key::text AS article_id,
                       NULL::smallint AS scene_index,
                       NULL::smallint AS scene_count,
                       NULL::text AS feedback_reason,
                       row_number() OVER (
                           PARTITION BY s.account_id
                           ORDER BY saved.value::timestamptz DESC, saved.key DESC
                       ) AS saved_rank
                FROM app_account_reading_states s
                CROSS JOIN LATERAL jsonb_each_text(s.saved_articles) saved
                WHERE s.account_id=ANY($1::uuid[])
            ), saved AS (
                SELECT account_id,signal_type,occurred_at,local_date,query,
                       article_id,scene_index,scene_count,feedback_reason
                FROM ranked_saved
                WHERE saved_rank <= 100
                  AND occurred_at >= $2::timestamptz-$3::interval
                  AND occurred_at <= $2::timestamptz
            ), feedback AS (
                SELECT f.account_id,'feedback'::text AS signal_type,
                       f.updated_at AS occurred_at,
                       (f.updated_at AT TIME ZONE 'UTC')::date AS local_date,
                       NULL::text AS query,f.article_id,
                       NULL::smallint AS scene_index,
                       NULL::smallint AS scene_count,
                       f.reason AS feedback_reason
                FROM app_article_feedback f
                WHERE f.account_id=ANY($1::uuid[])
                  AND f.updated_at >= $2::timestamptz-$3::interval
                  AND f.updated_at <= $2::timestamptz
            ), signals AS (
                SELECT * FROM activity
                UNION ALL SELECT * FROM saved
                UNION ALL SELECT * FROM feedback
            )
            SELECT signals.*,a.domain,m.data AS map_data
            FROM signals
            LEFT JOIN articles a ON a.id=signals.article_id
            LEFT JOIN article_maps m ON m.article_id=signals.article_id
            ORDER BY signals.account_id,signals.occurred_at DESC,
                     signals.signal_type,signals.article_id
            """,
            unique_ids,
            snapshot_at,
            timedelta(days=since_days),
            event_limit_per_account,
        )
        out: list[dict] = []
        for raw_row in rows:
            item = dict(raw_row)
            if isinstance(item.get("map_data"), str):
                try:
                    item["map_data"] = json.loads(item["map_data"])
                except (TypeError, ValueError):
                    item["map_data"] = None
            out.append(item)
        return out

    async def curation_pool_state(self, *, since_days: int) -> dict:
        """Return a stable fingerprint for the strict agent-searchable pool."""
        if not 1 <= since_days <= 90:
            raise ValueError("invalid curation pool window")
        row = await self.pool.fetchrow(
            """
            WITH eligible AS (
                SELECT a.id AS article_id,
                       COALESCE(
                           m.data->'editorial'->>'source_hash',
                           m.updated_at::text
                       ) AS source_hash
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                WHERE m.status='ok'
                  AND m.data IS NOT NULL
                  AND m.data->>'format_version'='3'
                  AND m.data->'editorial'->>'critic_verdict'='pass'
                  AND COALESCE(a.seendate,a.first_seen_at)
                        >= now()-$1::interval
                  AND (p.draft_id IS NULL OR p.state='published')
            )
            SELECT count(*) AS candidate_count,
                   md5(COALESCE(string_agg(
                       article_id || ':' || source_hash,
                       ',' ORDER BY article_id
                   ),'')) AS pool_fingerprint
            FROM eligible
            """,
            timedelta(days=since_days),
        )
        if row is None:
            return {
                "candidate_count": 0,
                "pool_fingerprint": hashlib.md5(b"").hexdigest(),  # noqa: S324
            }
        return {
            "candidate_count": int(row["candidate_count"] or 0),
            "pool_fingerprint": str(
                row["pool_fingerprint"] or hashlib.md5(b"").hexdigest()  # noqa: S324
            ),
        }

    async def curation_search_rows(
        self,
        *,
        query: str,
        offset: int,
        limit: int,
        since_days: int,
    ) -> list[dict]:
        """Search or browse only strict curation-eligible Map v3 articles."""
        normalized = " ".join(query.casefold().split())
        if (
            (normalized and not 2 <= len(normalized) <= 80)
            or not 0 <= offset <= 200
            or not 1 <= limit <= 20
            or not 1 <= since_days <= 90
        ):
            raise ValueError("invalid curation search bounds")

        def pattern(value: str) -> str:
            escaped = (
                value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            return f"%{escaped}%"

        tokens = [token for token in normalized.split() if len(token) >= 2][:6]
        patterns = (
            [pattern(normalized)] + [pattern(token) for token in tokens]
            if normalized
            else []
        )
        rows = await self.pool.fetch(
            """
            WITH eligible AS (
                SELECT a.id AS article_id,a.title,a.domain,
                       COALESCE(a.seendate,a.first_seen_at) AS published_at,
                       m.status,m.data,m.updated_at,
                       lower(concat_ws(' ',a.title,m.data->>'headline',
                           m.data->>'summary',(m.data->'keywords')::text))
                           AS haystack
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                WHERE m.status='ok'
                  AND m.data IS NOT NULL
                  AND m.data->>'format_version'='3'
                  AND m.data->'editorial'->>'critic_verdict'='pass'
                  AND COALESCE(a.seendate,a.first_seen_at)
                        >= now()-$3::interval
                  AND (p.draft_id IS NULL OR p.state='published')
            )
            SELECT * FROM eligible
            WHERE $1='' OR haystack ILIKE ANY($2::text[])
            ORDER BY
                CASE WHEN $1<>'' AND
                          lower(COALESCE(data->>'headline',title,''))
                          ILIKE $1 THEN 0 ELSE 1 END,
                published_at DESC,article_id DESC
            OFFSET $4 LIMIT $5
            """,
            pattern(normalized) if normalized else "",
            patterns,
            timedelta(days=since_days),
            offset,
            limit,
        )
        return _curation_rows(rows)

    async def curation_detail_rows(
        self,
        *,
        article_ids: Sequence[str],
        since_days: int,
    ) -> list[dict]:
        """Re-read seen article IDs through the same strict pool boundary."""
        ids = list(article_ids)
        if (
            not 1 <= len(ids) <= 8
            or len(set(ids)) != len(ids)
            or any(
                not isinstance(article_id, str)
                or not 1 <= len(article_id) <= 128
                for article_id in ids
            )
            or not 1 <= since_days <= 90
        ):
            raise ValueError("invalid curation detail bounds")
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id,a.title,a.domain,
                   COALESCE(a.seendate,a.first_seen_at) AS published_at,
                   m.status,m.data,m.updated_at
            FROM articles a
            JOIN article_maps m ON m.article_id=a.id
            LEFT JOIN editorial_publications p ON p.article_id=a.id
            WHERE a.id=ANY($1::text[])
              AND m.status='ok'
              AND m.data IS NOT NULL
              AND m.data->>'format_version'='3'
              AND m.data->'editorial'->>'critic_verdict'='pass'
              AND COALESCE(a.seendate,a.first_seen_at) >= now()-$2::interval
              AND (p.draft_id IS NULL OR p.state='published')
            ORDER BY array_position($1::text[],a.id)
            """,
            ids,
            timedelta(days=since_days),
        )
        return _curation_rows(rows)

    async def timeline_cluster_candidates(
        self,
        *,
        since_days: int,
        limit: int,
        day_span: int,
    ) -> list[dict]:
        """Return the bounded strict-public pool used by offline clustering.

        A timeline is a sequence of *days*, so the pool is stratified by KST
        date instead of simply truncated.  Ordering the whole window by date and
        applying ``LIMIT`` would hand clustering a single day of wire copy — the
        oldest one — which can never produce a multi-day main lane.  Each of the
        ``day_span`` most recent KST days that has eligible articles instead
        contributes an equal, deterministic quota, newest articles first.
        """
        if (
            not 1 <= since_days <= 90
            or not 64 <= limit <= 500
            or not 3 <= day_span <= 30
        ):
            raise ValueError("invalid timeline candidate bounds")
        per_day = max(1, limit // day_span)
        rows = await self.pool.fetch(
            """
            WITH eligible AS (
                SELECT a.id AS article_id,a.title AS source_title,a.domain,
                       COALESCE(a.seendate,a.first_seen_at) AS published_at,
                       CASE WHEN a.seendate IS NULL THEN 'first_seen'
                            ELSE 'provider' END AS date_source,
                       m.status,m.data,m.updated_at AS map_updated_at,
                       (COALESCE(a.seendate,a.first_seen_at)
                        AT TIME ZONE 'Asia/Seoul')::date AS kst_day
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                WHERE m.status='ok'
                  AND m.data IS NOT NULL
                  AND m.data->>'format_version'='3'
                  AND m.data->'editorial'->>'critic_verdict'='pass'
                  AND m.data->>'headline' ~ '[가-힣]'
                  AND COALESCE(a.seendate,a.first_seen_at) >= now()-$1::interval
                  AND (p.draft_id IS NULL OR p.state='published')
            ), stratified AS (
                SELECT *,
                       row_number() OVER (
                           PARTITION BY kst_day
                           ORDER BY published_at DESC,article_id ASC
                       ) AS day_rank,
                       dense_rank() OVER (ORDER BY kst_day DESC) AS day_recency
                FROM eligible
            )
            SELECT article_id,source_title,domain,published_at,date_source,
                   status,data,map_updated_at
            FROM stratified
            WHERE day_recency <= $2 AND day_rank <= $3
            ORDER BY kst_day ASC,article_id ASC
            LIMIT $4
            """,
            timedelta(days=since_days),
            day_span,
            per_day,
            limit,
        )
        return _curation_rows(rows)

    async def claim_curation_catalog(
        self,
        *,
        editorial_day: date,
        input_hash: str,
        candidate_count: int,
        min_interval_s: float,
        max_attempts: int,
        lease_s: float,
    ) -> Optional[dict]:
        if len(input_hash) != 64 or candidate_count < 0:
            raise ValueError("invalid curation claim identity")
        if min_interval_s <= 0 or max_attempts < 1 or lease_s <= 0:
            raise ValueError("invalid curation claim bounds")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('article-curation-claim'))"
                )
                await conn.execute(
                    """
                    UPDATE article_curation_catalogs
                    SET status='queued', lease_expires_at=NULL,
                        next_attempt_at=now(), updated_at=now(),
                        error_code='processing_lease_expired'
                    WHERE status='processing' AND lease_expires_at <= now()
                    """
                )
                now = await conn.fetchval("SELECT now()")
                latest = await conn.fetchrow(
                    """
                    SELECT generated_at
                    FROM article_curation_catalogs
                    WHERE status='published' AND generated_at IS NOT NULL
                    ORDER BY generated_at DESC, id DESC LIMIT 1
                    FOR UPDATE
                    """
                )
                if latest is not None and latest["generated_at"] is not None:
                    if (now - latest["generated_at"]).total_seconds() < min_interval_s:
                        return None
                # `input_hash` is this table's only claim identity; unlike the
                # timeline catalog there is no candidate_slot column to fall
                # back on.
                existing = await conn.fetchrow(
                    """
                    SELECT * FROM article_curation_catalogs
                    WHERE input_hash=$1
                    LIMIT 1 FOR UPDATE
                    """,
                    input_hash,
                )
                if existing is not None:
                    if (
                        existing["status"] != "queued"
                        or existing["next_attempt_at"] > now
                        or int(existing["attempt_count"]) >= int(existing["max_attempts"])
                    ):
                        return None
                    row = await conn.fetchrow(
                        """
                        UPDATE article_curation_catalogs
                        SET status='processing', attempt_count=attempt_count+1,
                            candidate_count=$2,
                            lease_expires_at=now()+$3::interval,
                            error_code=NULL, updated_at=now()
                        WHERE id=$1 AND status='queued'
                        RETURNING *
                        """,
                        existing["id"],
                        candidate_count,
                        timedelta(seconds=lease_s),
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO article_curation_catalogs
                            (id,editorial_day,input_hash,status,candidate_count,
                             attempt_count,max_attempts,lease_expires_at)
                        VALUES ($1,$2,$3,'processing',$4,1,$5,now()+$6::interval)
                        RETURNING *
                        """,
                        uuid4(),
                        editorial_day,
                        input_hash,
                        candidate_count,
                        max_attempts,
                        timedelta(seconds=lease_s),
                    )
        return dict(row) if row else None

    async def retry_curation_catalog(
        self,
        catalog_id: UUID,
        *,
        error_code: str,
        base_delay_s: float,
    ) -> str:
        allowed = {
            "curation_unavailable",
            "provider_transient",
            "provider_permanent",
            "invalid_plan_shape",
            "candidate_unknown",
            "candidate_duplicate",
            "quality_gate_failed",
            "catalog_too_small",
            "route_source_diversity_failed",
            "agent_protocol_failed",
            "agent_loop_exhausted",
            "tool_unavailable",
        }
        safe_error = error_code if error_code in allowed else "curation_unavailable"
        if base_delay_s <= 0:
            raise ValueError("base_delay_s must be positive")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM article_curation_catalogs WHERE id=$1 FOR UPDATE",
                    catalog_id,
                )
                if current is None or current["status"] != "processing":
                    return "terminal"
                attempts = int(current["attempt_count"])
                permanent = safe_error == "provider_permanent"
                terminal = permanent or attempts >= int(current["max_attempts"])
                delay = min(6 * 3600.0, base_delay_s * (2 ** max(0, attempts - 1)))
                await conn.execute(
                    """
                    UPDATE article_curation_catalogs
                    SET status=$2,
                        next_attempt_at=CASE WHEN $2='failed' THEN now()
                                             ELSE now()+$3::interval END,
                        lease_expires_at=NULL, error_code=$4, updated_at=now()
                    WHERE id=$1 AND status='processing'
                    """,
                    catalog_id,
                    "failed" if terminal else "queued",
                    timedelta(seconds=delay),
                    safe_error,
                )
        return "failed" if terminal else "queued"

    async def publish_curation_catalog(
        self,
        catalog_id: UUID,
        *,
        plan,
        model: str,
        valid_until: datetime,
    ) -> dict:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM article_curation_catalogs WHERE id=$1 FOR UPDATE",
                    catalog_id,
                )
                if current is None or current["status"] != "processing":
                    raise LookupError("curation catalog is not processing")
                for rank, route in enumerate(plan.routes, start=1):
                    route_id = uuid4()
                    await conn.execute(
                        """
                        INSERT INTO article_curation_routes
                            (id,catalog_id,rank,slug,title,summary,topic,is_representative)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                        """,
                        route_id,
                        catalog_id,
                        rank,
                        route.slug,
                        route.title,
                        route.summary,
                        route.topic,
                        route.representative,
                    )
                    for position, item in enumerate(route.items, start=1):
                        await conn.execute(
                            """
                            INSERT INTO article_curation_items
                                (route_id,position,article_id,role,reason,
                                 lasting_value,quality)
                            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                            """,
                            route_id,
                            position,
                            item.article_id,
                            item.role,
                            item.reason,
                            item.lasting_value,
                            json.dumps(item.quality.to_dict(), ensure_ascii=False),
                        )
                await conn.execute(
                    """
                    UPDATE article_curation_catalogs
                    SET status='superseded', updated_at=now()
                    WHERE editorial_day=$1 AND status='published' AND id<>$2
                    """,
                    current["editorial_day"],
                    catalog_id,
                )
                row = await conn.fetchrow(
                    """
                    UPDATE article_curation_catalogs
                    SET status='published', model=$2,
                        search_suggestions=$3::jsonb,
                        generated_at=now(), valid_until=$4,
                        lease_expires_at=NULL, error_code=NULL, updated_at=now()
                    WHERE id=$1 AND status='processing'
                    RETURNING *
                    """,
                    catalog_id,
                    model,
                    json.dumps(list(plan.search_suggestions), ensure_ascii=False),
                    valid_until,
                )
                if row is None:
                    raise RuntimeError("curation publish lost processing row")
        return dict(row)

    async def current_curation_catalog(
        self, *, now: datetime | None = None
    ) -> Optional[dict]:
        header = await self.pool.fetchrow(
            """
            SELECT * FROM article_curation_catalogs
            WHERE status='published'
              AND valid_until > COALESCE($1::timestamptz, now())
            ORDER BY generated_at DESC, id DESC LIMIT 1
            """,
            now,
        )
        if header is None:
            return None
        rows = await self.pool.fetch(
            """
            SELECT r.id AS route_id, r.rank, r.slug, r.title AS route_title,
                   r.summary AS route_summary, r.topic, r.is_representative,
                   i.position, i.role, i.reason, i.lasting_value, i.quality,
                   a.id AS article_id, a.title AS source_title,
                   a.url AS source_url, a.domain,
                   COALESCE(a.seendate,a.first_seen_at) AS published_at,
                   a.language AS source_language, a.sourcecountry,
                   a.document_type, a.document_source_name,
                   m.status AS map_status, m.data
            FROM article_curation_routes r
            JOIN article_curation_items i ON i.route_id=r.id
            JOIN articles a ON a.id=i.article_id
            JOIN article_maps m ON m.article_id=a.id
            WHERE r.catalog_id=$1
            ORDER BY r.rank, i.position
            """,
            header["id"],
        )
        catalog = dict(header)
        suggestions = catalog.get("search_suggestions")
        if isinstance(suggestions, str):
            catalog["search_suggestions"] = json.loads(suggestions)
        routes: list[dict] = []
        route_by_id: dict[UUID, dict] = {}
        for raw_row in rows:
            item = dict(raw_row)
            route_id = item.pop("route_id")
            route = route_by_id.get(route_id)
            if route is None:
                route = {
                    "id": route_id,
                    "rank": item.pop("rank"),
                    "slug": item.pop("slug"),
                    "title": item.pop("route_title"),
                    "summary": item.pop("route_summary"),
                    "topic": item.pop("topic"),
                    "is_representative": item.pop("is_representative"),
                    "items": [],
                }
                route_by_id[route_id] = route
                routes.append(route)
            else:
                for key in (
                    "rank", "slug", "route_title", "route_summary",
                    "topic", "is_representative",
                ):
                    item.pop(key, None)
            for key in ("quality", "data"):
                if isinstance(item.get(key), str):
                    item[key] = json.loads(item[key])
            route["items"].append(item)
        catalog["routes"] = routes
        return catalog

    async def claim_explore_timeline_catalog(
        self,
        *,
        cluster_snapshot_id: str,
        cluster_slot: str,
        candidate_slot: str,
        input_hash: str,
        candidate_count: int,
        max_attempts: int,
        lease_s: float,
    ) -> Optional[dict]:
        if (
            not 1 <= len(cluster_snapshot_id) <= 128
            or not 1 <= len(cluster_slot) <= 64
            or not 1 <= len(candidate_slot) <= 64
            or len(input_hash) != 64
            or candidate_count < 0
            or not 1 <= max_attempts <= 5
            or lease_s <= 0
        ):
            raise ValueError("invalid timeline claim")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('explore-timeline-claim'))"
                )
                await conn.execute(
                    """
                    UPDATE explore_timeline_catalogs
                    SET status='queued', lease_expires_at=NULL,
                        next_attempt_at=now(), updated_at=now(),
                        error_code='processing_lease_expired'
                    WHERE status='processing' AND lease_expires_at <= now()
                    """
                )
                now = await conn.fetchval("SELECT now()")
                existing = await conn.fetchrow(
                    """
                    SELECT * FROM explore_timeline_catalogs
                    WHERE input_hash=$1 OR candidate_slot=$2
                    ORDER BY CASE WHEN input_hash=$1 THEN 0 ELSE 1 END
                    FOR UPDATE
                    """,
                    input_hash,
                    candidate_slot,
                )
                if existing is not None:
                    # One candidate slot is a hard provider-cost boundary.  New
                    # rows can arrive while that slot is open, changing the
                    # input hash, but they must wait for the next slot rather
                    # than attempting an insert that violates the slot unique
                    # constraint and crashes the worker poll.
                    if existing["input_hash"] != input_hash:
                        return None
                    if (
                        existing["status"] != "queued"
                        or existing["next_attempt_at"] > now
                        or int(existing["attempt_count"])
                        >= int(existing["max_attempts"])
                    ):
                        return None
                    row = await conn.fetchrow(
                        """
                        UPDATE explore_timeline_catalogs
                        SET status='processing', attempt_count=attempt_count+1,
                            candidate_count=$2,
                            lease_expires_at=now()+$3::interval,
                            error_code=NULL, updated_at=now()
                        WHERE id=$1 AND status='queued'
                        RETURNING *
                        """,
                        existing["id"],
                        candidate_count,
                        timedelta(seconds=lease_s),
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO explore_timeline_catalogs
                            (id,input_hash,cluster_snapshot_id,cluster_slot,
                             candidate_slot,status,candidate_count,attempt_count,
                             max_attempts,lease_expires_at)
                        VALUES ($1,$2,$3,$4,$5,'processing',$6,1,$7,
                                now()+$8::interval)
                        RETURNING *
                        """,
                        uuid4(),
                        input_hash,
                        cluster_snapshot_id,
                        cluster_slot,
                        candidate_slot,
                        candidate_count,
                        max_attempts,
                        timedelta(seconds=lease_s),
                    )
        return dict(row) if row else None

    async def retry_explore_timeline_catalog(
        self,
        catalog_id: UUID,
        *,
        error_code: str,
        base_delay_s: float,
    ) -> str:
        allowed = {
            "timeline_unavailable",
            "cluster_unavailable",
            "insufficient_clusters",
            "insufficient_candidates",
            "provider_transient",
            "provider_permanent",
            "invalid_rerank_response",
            "invalid_timeline_plan",
            "public_pool_changed",
        }
        safe_error = error_code if error_code in allowed else "timeline_unavailable"
        if base_delay_s <= 0:
            raise ValueError("base_delay_s must be positive")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM explore_timeline_catalogs WHERE id=$1 FOR UPDATE",
                    catalog_id,
                )
                if current is None or current["status"] != "processing":
                    return "terminal"
                attempts = int(current["attempt_count"])
                terminal = (
                    safe_error == "provider_permanent"
                    or attempts >= int(current["max_attempts"])
                )
                delay = min(4 * 3600.0, base_delay_s * (2 ** max(0, attempts - 1)))
                await conn.execute(
                    """
                    UPDATE explore_timeline_catalogs
                    SET status=$2,
                        next_attempt_at=CASE WHEN $2='failed' THEN now()
                                             ELSE now()+$3::interval END,
                        lease_expires_at=NULL,error_code=$4,updated_at=now()
                    WHERE id=$1 AND status='processing'
                    """,
                    catalog_id,
                    "failed" if terminal else "queued",
                    timedelta(seconds=delay),
                    safe_error,
                )
        return "failed" if terminal else "queued"

    async def publish_explore_timeline_catalog(
        self,
        catalog_id: UUID,
        *,
        timelines: Sequence[dict],
        model: str,
        valid_until: datetime,
    ) -> dict:
        if not 4 <= len(timelines) <= 8:
            raise ValueError("timeline catalog must contain 4..8 timelines")
        if not model or valid_until.tzinfo is None or valid_until.utcoffset() is None:
            raise ValueError("invalid timeline publication metadata")
        stable_keys: set[str] = set()
        cluster_keys: set[str] = set()
        article_ids: list[str] = []
        prepared: list[tuple[dict, list[dict]]] = []
        for expected_rank, timeline in enumerate(timelines, start=1):
            if not isinstance(timeline, dict) or int(timeline.get("rank", 0)) != expected_rank:
                raise ValueError("timeline ranks must be contiguous")
            nodes = timeline.get("nodes")
            if not isinstance(nodes, list) or not 4 <= len(nodes) <= 8:
                raise ValueError("timeline nodes must contain 4..8 items")
            stable_key = timeline.get("stable_key")
            cluster_key = timeline.get("cluster_key")
            if (
                not isinstance(stable_key, str)
                or not 1 <= len(stable_key) <= 128
                or stable_key in stable_keys
                or not isinstance(cluster_key, str)
                or not 1 <= len(cluster_key) <= 128
                or cluster_key in cluster_keys
            ):
                raise ValueError("invalid timeline identity")
            stable_keys.add(stable_key)
            cluster_keys.add(cluster_key)
            node_ids = {node.get("node_id") for node in nodes if isinstance(node, dict)}
            if len(node_ids) != len(nodes) or None in node_ids:
                raise ValueError("duplicate timeline node identity")
            main_count = 0
            local_articles: set[str] = set()
            for expected_order, node in enumerate(nodes, start=1):
                if not isinstance(node, dict) or node.get("display_order") != expected_order:
                    raise ValueError("timeline display order must be contiguous")
                article_id = node.get("article_id")
                lane = node.get("lane")
                anchor = node.get("anchor_node_id")
                if (
                    not isinstance(article_id, str)
                    or not article_id
                    or article_id in local_articles
                    or (lane == "main" and anchor is not None)
                    or (lane == "branch" and anchor not in node_ids)
                    or lane not in {"main", "branch"}
                ):
                    raise ValueError("invalid timeline node")
                if lane == "main":
                    main_count += 1
                local_articles.add(article_id)
                article_ids.append(article_id)
            if main_count < 3:
                raise ValueError("timeline requires at least three main nodes")
            prepared.append((timeline, nodes))
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("catalog article IDs must be unique")

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM explore_timeline_catalogs WHERE id=$1 FOR UPDATE",
                    catalog_id,
                )
                if current is None or current["status"] != "processing":
                    raise LookupError("timeline catalog is not processing")
                public_rows = await conn.fetch(
                    """
                    SELECT a.id AS article_id
                    FROM articles a
                    JOIN article_maps m ON m.article_id=a.id
                    LEFT JOIN editorial_publications p ON p.article_id=a.id
                    WHERE a.id=ANY($1::text[])
                      AND m.status='ok'
                      AND m.data IS NOT NULL
                      AND m.data->>'format_version'='3'
                      AND m.data->'editorial'->>'critic_verdict'='pass'
                      AND (p.draft_id IS NULL OR p.state='published')
                    """,
                    article_ids,
                )
                public_ids = {str(row["article_id"]) for row in public_rows}
                if public_ids != set(article_ids):
                    raise LookupError("public article set changed")
                for timeline, _nodes in prepared:
                    await conn.execute(
                        """
                        INSERT INTO explore_timelines
                            (id,catalog_id,stable_key,cluster_key,rank,title,
                             summary,topic,started_at,ended_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        """,
                        timeline["id"],
                        catalog_id,
                        timeline["stable_key"],
                        timeline["cluster_key"],
                        timeline["rank"],
                        timeline["title"],
                        timeline["summary"],
                        timeline["topic"],
                        timeline["started_at"],
                        timeline["ended_at"],
                    )
                ordered_nodes = [
                    (timeline, node)
                    for timeline, nodes in prepared
                    for node in nodes
                    if node["lane"] == "main"
                ] + [
                    (timeline, node)
                    for timeline, nodes in prepared
                    for node in nodes
                    if node["lane"] == "branch"
                ]
                for timeline, node in ordered_nodes:
                    await conn.execute(
                        """
                        INSERT INTO explore_timeline_nodes
                            (row_id,node_id,timeline_id,article_id,anchor_node_id,lane,
                             display_order,update_kind,relevance_score,
                             published_at_snapshot,date_source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        """,
                        uuid4(),
                        node["node_id"],
                        timeline["id"],
                        node["article_id"],
                        node["anchor_node_id"],
                        node["lane"],
                        node["display_order"],
                        node["update_kind"],
                        node["relevance_score"],
                        node["published_at"],
                        node["date_source"],
                    )
                await self._enqueue_timeline_pushes(
                    conn,
                    catalog_id=catalog_id,
                    timelines=timelines,
                )
                await conn.execute(
                    """
                    UPDATE explore_timeline_catalogs
                    SET status='superseded',updated_at=now()
                    WHERE status='published' AND id<>$1
                    """,
                    catalog_id,
                )
                row = await conn.fetchrow(
                    """
                    UPDATE explore_timeline_catalogs
                    SET status='published',model=$2,generated_at=now(),
                        valid_until=$3,lease_expires_at=NULL,error_code=NULL,
                        updated_at=now()
                    WHERE id=$1 AND status='processing'
                    RETURNING *
                    """,
                    catalog_id,
                    model,
                    valid_until,
                )
                if row is None:
                    raise RuntimeError("timeline publish lost processing row")
        return dict(row)

    async def _enqueue_timeline_pushes(
        self,
        connection,
        *,
        catalog_id: UUID,
        timelines: Sequence[dict],
    ) -> int:
        deliveries = 0
        for timeline in timelines:
            nodes = timeline["nodes"]
            for update_kind in ("inserted", "occurred"):
                candidates = [
                    node for node in nodes
                    if node.get("update_kind") == update_kind
                ]
                if not candidates:
                    continue
                representative = max(candidates, key=lambda node: node["display_order"])
                notification_id = uuid5(
                    catalog_id,
                    f"{timeline['stable_key']}:{update_kind}",
                )
                notification = await connection.fetchrow(
                    """
                    INSERT INTO explore_timeline_push_notifications
                        (id,catalog_id,timeline_id,timeline_stable_key,
                         node_id,update_kind)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (catalog_id,timeline_stable_key,update_kind)
                    DO NOTHING
                    RETURNING id
                    """,
                    notification_id,
                    catalog_id,
                    timeline["id"],
                    timeline["stable_key"],
                    representative["node_id"],
                    update_kind,
                )
                if notification is None:
                    continue
                deliveries += int(
                    await connection.fetchval(
                        """
                        WITH inserted AS (
                            INSERT INTO explore_timeline_push_deliveries
                                (notification_id,registration_id)
                            SELECT $1,registration.id
                            FROM app_timeline_subscriptions subscription
                            JOIN app_push_registrations registration
                              ON registration.account_id=subscription.account_id
                            JOIN app_account_sessions session
                              ON session.id=registration.session_id
                            WHERE subscription.timeline_stable_key=$2
                              AND registration.status='active'
                              AND (($3='inserted'
                                    AND registration.timeline_refreshes_enabled)
                                   OR ($3='occurred'
                                       AND registration.timeline_additions_enabled))
                              AND session.expires_at > now()
                            ON CONFLICT (notification_id,registration_id)
                            DO NOTHING
                            RETURNING 1
                        )
                        SELECT count(*) FROM inserted
                        """,
                        notification_id,
                        timeline["stable_key"],
                        update_kind,
                    )
                )
        return deliveries

    async def explore_timeline_ops_metrics(
        self,
        *,
        candidate_days: int,
        recent_limit: int = 8,
    ) -> dict:
        """Aggregate-only timeline production state for the /ops dashboard.

        Answers the three questions the worker's own logs cannot: how many
        editions exist, whether the newest run published or why it stopped, and
        whether the candidate pool still spans enough days to build a spine.
        """
        if not 1 <= candidate_days <= 90 or not 1 <= recent_limit <= 20:
            raise ValueError("invalid timeline ops bounds")
        totals = await self.pool.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM explore_timelines) AS timeline_editions,
                (SELECT count(DISTINCT stable_key) FROM explore_timelines)
                    AS distinct_timelines,
                (SELECT count(*) FROM explore_timeline_nodes) AS nodes,
                (SELECT count(*) FROM explore_timeline_catalogs) AS catalogs,
                (SELECT count(*) FROM explore_timeline_catalogs
                 WHERE status='published') AS published_catalogs,
                (SELECT count(*) FROM explore_timeline_catalogs
                 WHERE status='failed') AS failed_catalogs,
                (SELECT count(*) FROM explore_timeline_catalogs
                 WHERE status='superseded') AS superseded_catalogs,
                (SELECT count(*) FROM explore_timeline_catalogs
                 WHERE status IN ('queued','processing')) AS pending_catalogs,
                (SELECT count(*) FROM app_timeline_subscriptions) AS subscriptions
            """
        )
        recent = await self.pool.fetch(
            """
            SELECT id,status,error_code,attempt_count,max_attempts,
                   candidate_count,candidate_slot,cluster_slot,model,
                   generated_at,valid_until,next_attempt_at,created_at,updated_at,
                   (SELECT count(*) FROM explore_timelines timeline
                    WHERE timeline.catalog_id=catalog.id) AS timeline_count
            FROM explore_timeline_catalogs catalog
            ORDER BY created_at DESC
            LIMIT $1
            """,
            recent_limit,
        )
        pool_state = await self.pool.fetchrow(
            """
            WITH eligible AS (
                SELECT (COALESCE(a.seendate,a.first_seen_at)
                        AT TIME ZONE 'Asia/Seoul')::date AS kst_day
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                WHERE m.status='ok'
                  AND m.data IS NOT NULL
                  AND m.data->>'format_version'='3'
                  AND m.data->'editorial'->>'critic_verdict'='pass'
                  AND m.data->>'headline' ~ '[가-힣]'
                  AND COALESCE(a.seendate,a.first_seen_at) >= now()-$1::interval
                  AND (p.draft_id IS NULL OR p.state='published')
            )
            SELECT count(*) AS candidate_count,
                   count(DISTINCT kst_day) AS candidate_days,
                   max(kst_day) AS newest_day,
                   min(kst_day) AS oldest_day
            FROM eligible
            """,
            timedelta(days=candidate_days),
        )
        return {
            "totals": {
                key: int(totals[key] or 0)
                for key in (
                    "timeline_editions",
                    "distinct_timelines",
                    "nodes",
                    "catalogs",
                    "published_catalogs",
                    "failed_catalogs",
                    "superseded_catalogs",
                    "pending_catalogs",
                    "subscriptions",
                )
            }
            if totals is not None
            else {},
            "recent_catalogs": [dict(row) for row in recent],
            "candidate_pool": dict(pool_state) if pool_state is not None else {},
        }

    async def current_explore_timeline_catalog(
        self,
        *,
        now: datetime | None = None,
        stale_hours: int = 72,
    ) -> Optional[dict]:
        if now is not None and (now.tzinfo is None or now.utcoffset() is None):
            raise ValueError("now must be timezone-aware")
        if not 1 <= stale_hours <= 168:
            raise ValueError("invalid timeline stale bound")
        header = await self.pool.fetchrow(
            """
            SELECT * FROM explore_timeline_catalogs
            WHERE status='published'
              AND valid_until > COALESCE($1::timestamptz, now())-$2::interval
            ORDER BY generated_at DESC,id DESC LIMIT 1
            """,
            now,
            timedelta(hours=stale_hours),
        )
        if header is None:
            return None
        rows = await self.pool.fetch(
            """
            SELECT t.id AS timeline_id,t.rank,t.stable_key,t.cluster_key,
                   t.title AS timeline_title,t.summary AS timeline_summary,
                   t.topic,t.started_at,t.ended_at,
                   n.node_id,n.anchor_node_id,n.lane,n.display_order,
                   n.update_kind,n.relevance_score,n.published_at_snapshot,
                   n.date_source,a.id AS article_id,a.title AS source_title,
                   a.url AS source_url,a.domain,
                   COALESCE(a.seendate,a.first_seen_at) AS published_at,
                   a.language AS source_language,a.sourcecountry,
                   a.document_type,a.document_source_name,
                   m.status AS map_status,m.data
            FROM explore_timelines t
            JOIN explore_timeline_nodes n ON n.timeline_id=t.id
            JOIN articles a ON a.id=n.article_id
            JOIN article_maps m ON m.article_id=a.id
            LEFT JOIN editorial_publications p ON p.article_id=a.id
            WHERE t.catalog_id=$1
              AND m.status='ok'
              AND m.data IS NOT NULL
              AND m.data->>'format_version'='3'
              AND m.data->'editorial'->>'critic_verdict'='pass'
              AND (p.draft_id IS NULL OR p.state='published')
            ORDER BY t.rank,n.display_order
            """,
            header["id"],
        )
        catalog = dict(header)
        collected: list[dict] = []
        by_id: dict[UUID, dict] = {}
        seen_articles: set[str] = set()
        for raw_row in rows:
            item = dict(raw_row)
            timeline_id = item.pop("timeline_id")
            timeline = by_id.get(timeline_id)
            if timeline is None:
                timeline = {
                    "id": timeline_id,
                    "rank": item.pop("rank"),
                    "stable_key": item.pop("stable_key"),
                    "cluster_key": item.pop("cluster_key"),
                    "title": item.pop("timeline_title"),
                    "summary": item.pop("timeline_summary"),
                    "topic": item.pop("topic"),
                    "started_at": item.pop("started_at"),
                    "ended_at": item.pop("ended_at"),
                    "nodes": [],
                }
                by_id[timeline_id] = timeline
                collected.append(timeline)
            else:
                for key in (
                    "rank",
                    "stable_key",
                    "cluster_key",
                    "timeline_title",
                    "timeline_summary",
                    "topic",
                    "started_at",
                    "ended_at",
                ):
                    item.pop(key, None)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            article_id = str(item.get("article_id", ""))
            if article_id in seen_articles:
                continue
            seen_articles.add(article_id)
            timeline["nodes"].append(item)
        # An edition stays readable until `valid_until`, so an article can leave
        # the strict public gate after it was published.  Serve the timelines
        # that still hold together rather than blanking Explore for everyone.
        timelines: list[dict] = []
        for timeline in collected:
            nodes = repaired_timeline_nodes(timeline["nodes"])
            if nodes is None:
                continue
            timeline["nodes"] = nodes
            timeline["rank"] = len(timelines) + 1
            timelines.append(timeline)
            if len(timelines) == 8:
                break
        if not timelines:
            return None
        effective_now = now or datetime.now(UTC)
        catalog["timeline_status"] = (
            "fresh" if catalog["valid_until"] > effective_now else "stale"
        )
        catalog["timelines"] = timelines
        catalog["published_timeline_count"] = len(collected)
        return catalog

    async def search_article_maps(self, *, query: str, limit: int) -> list[dict]:
        normalized = " ".join(query.casefold().split())
        if not 2 <= len(normalized) <= 80 or not 1 <= limit <= 20:
            raise ValueError("invalid discovery search bounds")

        def pattern(value: str) -> str:
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return f"%{escaped}%"

        tokens = [token for token in normalized.split() if len(token) >= 2][:6]
        patterns = [pattern(normalized)] + [pattern(token) for token in tokens]
        rows = await self.pool.fetch(
            """
            WITH searchable AS (
                SELECT a.id AS article_id, a.title AS source_title,
                       a.url AS source_url, a.domain,
                       COALESCE(a.seendate,a.first_seen_at) AS published_at,
                       a.language AS source_language, a.sourcecountry,
                       a.document_type, a.document_source_name,
                       m.status, m.data,
                       lower(concat_ws(' ',a.title,m.data->>'headline',
                           m.data->>'summary',(m.data->'keywords')::text)) AS haystack
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                LEFT JOIN editorial_publications p ON p.article_id=a.id
                WHERE m.status IN ('ok','degraded') AND m.data IS NOT NULL
                  AND (p.draft_id IS NULL OR p.state='published')
            )
            SELECT * FROM searchable
            WHERE haystack ILIKE ANY($2::text[])
            ORDER BY
                CASE WHEN lower(COALESCE(data->>'headline',source_title,''))
                          ILIKE $1 THEN 0 ELSE 1 END,
                published_at DESC, article_id DESC
            LIMIT $3
            """,
            pattern(normalized),
            patterns,
            limit,
        )
        out: list[dict] = []
        for raw_row in rows:
            item = dict(raw_row)
            item.pop("haystack", None)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def search_explore_timelines(
        self, *, query: str, limit: int = 8
    ) -> list[dict]:
        normalized = " ".join(query.casefold().split())
        if not 2 <= len(normalized) <= 80 or not 1 <= limit <= 8:
            raise ValueError("invalid timeline search bounds")
        escaped = (
            normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        rows = await self.pool.fetch(
            """
            WITH editions AS (
                SELECT DISTINCT ON (timeline.stable_key)
                       timeline.id,timeline.stable_key,timeline.title,
                       timeline.summary,timeline.topic,timeline.started_at,
                       timeline.ended_at,catalog.generated_at
                FROM explore_timelines timeline
                JOIN explore_timeline_catalogs catalog
                  ON catalog.id=timeline.catalog_id
                WHERE catalog.status IN ('published','superseded')
                  AND lower(concat_ws(' ',timeline.title,timeline.summary,
                                      timeline.topic)) ILIKE $1 ESCAPE '\\'
                ORDER BY timeline.stable_key,catalog.generated_at DESC,
                         timeline.id DESC
            ), candidates AS (
                SELECT edition.*,
                       row_number() OVER (
                           ORDER BY edition.generated_at DESC,edition.stable_key
                       )::integer AS search_rank
                FROM editions edition
                ORDER BY edition.generated_at DESC,edition.stable_key
                LIMIT $2
            )
            SELECT candidate.id AS timeline_id,candidate.search_rank,
                   candidate.stable_key,candidate.title AS timeline_title,
                   candidate.summary AS timeline_summary,candidate.topic,
                   candidate.started_at,candidate.ended_at,
                   node.node_id,node.anchor_node_id,node.lane,node.display_order,
                   node.update_kind,node.relevance_score,
                   node.published_at_snapshot,node.date_source,
                   article.id AS article_id,article.title AS source_title,
                   article.url AS source_url,article.domain,
                   COALESCE(article.seendate,article.first_seen_at) AS published_at,
                   article.language AS source_language,article.sourcecountry,
                   map.status AS map_status,map.data
            FROM candidates candidate
            JOIN explore_timeline_nodes node ON node.timeline_id=candidate.id
            JOIN articles article ON article.id=node.article_id
            JOIN article_maps map ON map.article_id=article.id
            LEFT JOIN editorial_publications publication
              ON publication.article_id=article.id
            WHERE map.status='ok' AND map.data IS NOT NULL
              AND map.data->>'format_version'='3'
              AND map.data->'editorial'->>'critic_verdict'='pass'
              AND (publication.draft_id IS NULL OR publication.state='published')
            ORDER BY candidate.search_rank,node.display_order
            """,
            f"%{escaped}%",
            limit,
        )
        by_id: dict[UUID, dict] = {}
        ordered: list[dict] = []
        for raw_row in rows:
            item = dict(raw_row)
            timeline_id = item.pop("timeline_id")
            timeline = by_id.get(timeline_id)
            if timeline is None:
                timeline = {
                    "id": timeline_id,
                    "rank": item.pop("search_rank"),
                    "stable_key": item.pop("stable_key"),
                    "title": item.pop("timeline_title"),
                    "summary": item.pop("timeline_summary"),
                    "topic": item.pop("topic"),
                    "started_at": item.pop("started_at"),
                    "ended_at": item.pop("ended_at"),
                    "nodes": [],
                }
                by_id[timeline_id] = timeline
                ordered.append(timeline)
            else:
                for key in (
                    "search_rank", "stable_key", "timeline_title",
                    "timeline_summary", "topic", "started_at", "ended_at",
                ):
                    item.pop(key, None)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            timeline["nodes"].append(item)

        valid: list[dict] = []
        for timeline in ordered:
            nodes = repaired_timeline_nodes(timeline["nodes"])
            if nodes is None:
                continue
            timeline["nodes"] = nodes
            timeline["rank"] = len(valid) + 1
            valid.append(timeline)
        return valid[:limit]

    async def published_article_maps_by_ids(
        self,
        *,
        article_ids: Sequence[str],
    ) -> list[dict]:
        """Hydrate vector hits through the current public Map boundary."""
        ids = list(article_ids)
        if (
            not 1 <= len(ids) <= 5
            or len(set(ids)) != len(ids)
            or any(
                not isinstance(article_id, str)
                or not 1 <= len(article_id) <= 128
                for article_id in ids
            )
        ):
            raise ValueError("invalid article RAG hydration bounds")
        rows = await self.pool.fetch(
            """
            WITH requested AS (
                SELECT article_id, ordinality
                FROM unnest($1::text[]) WITH ORDINALITY
                    AS requested(article_id, ordinality)
            )
            SELECT DISTINCT ON (requested.ordinality)
                   a.id AS article_id, a.title AS source_title,
                   a.url AS source_url, a.domain,
                   COALESCE(a.seendate,a.first_seen_at) AS published_at,
                   a.language AS source_language, a.sourcecountry,
                   a.document_type, a.document_source_name,
                   m.status, m.data
            FROM requested
            JOIN articles a ON a.id=requested.article_id
            JOIN article_maps m ON m.article_id=a.id
            LEFT JOIN editorial_publications p ON p.article_id=a.id
            WHERE m.status IN ('ok','degraded')
              AND m.data IS NOT NULL
              AND (p.draft_id IS NULL OR p.state='published')
            ORDER BY requested.ordinality
            """,
            ids,
        )
        out: list[dict] = []
        for raw_row in rows:
            item = dict(raw_row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def trending_article_rankings(
        self,
        *,
        as_of: datetime,
        article_age_days: int = 30,
        top_n: int = 10,
        growth_window: timedelta = timedelta(hours=1),
        min_growth_events: int = 5,
    ) -> dict[TrendingMetric, list[TrendingArticle]]:
        return await self._fetch_trending_article_rankings(
            self.pool,
            as_of=as_of,
            article_age_days=article_age_days,
            top_n=top_n,
            growth_window=growth_window,
            min_growth_events=min_growth_events,
        )

    async def _fetch_trending_article_rankings(
        self,
        executor,
        *,
        as_of: datetime,
        article_age_days: int = 30,
        top_n: int = 10,
        growth_window: timedelta = timedelta(hours=1),
        min_growth_events: int = 5,
    ) -> dict[TrendingMetric, list[TrendingArticle]]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        if not 1 <= article_age_days <= 365:
            raise ValueError("article_age_days is out of range")
        if not 1 <= top_n <= 10:
            raise ValueError("top_n is out of range")
        if growth_window != timedelta(hours=1):
            raise ValueError("growth_window must be one hour")
        if not 1 <= min_growth_events <= 1000:
            raise ValueError("min_growth_events is out of range")

        as_of = as_of.astimezone(UTC)
        universe_start = as_of - timedelta(days=article_age_days)
        recent_start = as_of - growth_window
        previous_start = recent_start - growth_window
        rows = await executor.fetch(
            """
            WITH eligible AS (
                SELECT a.id AS article_id,
                       COALESCE(a.seendate,a.first_seen_at) AS published_at,
                       m.data->'keywords' AS keywords
                FROM articles a
                JOIN article_maps m ON m.article_id=a.id
                WHERE COALESCE(a.seendate,a.first_seen_at) >= $2
                  AND COALESCE(a.seendate,a.first_seen_at) <= $1
                  AND m.status IN ('ok','degraded')
                  AND m.data IS NOT NULL
                  AND jsonb_typeof(m.data)='object'
                  AND jsonb_typeof(m.data->'keywords')='array'
                  AND (
                      NOT EXISTS (
                          SELECT 1 FROM editorial_publications p
                          WHERE p.article_id=a.id
                      )
                      OR EXISTS (
                          SELECT 1 FROM editorial_publications p
                          WHERE p.article_id=a.id AND p.state='published'
                      )
                  )
            ),
            view_totals AS (
                SELECT 'view_total'::text AS metric,
                       e.article_id,e.published_at,e.keywords,
                       c.view_count::double precision AS metric_value,
                       0::bigint AS recent_count,
                       0::bigint AS previous_count,
                       row_number() OVER (
                           ORDER BY c.view_count DESC,
                                    e.published_at DESC,e.article_id DESC
                       ) AS rank
                FROM eligible e
                JOIN article_view_counts c ON c.article_id=e.article_id
                WHERE c.view_count > 0
            ),
            growth_counts AS (
                SELECT e.article_id,e.published_at,e.keywords,
                       count(*) FILTER (
                           WHERE event.received_at >= $4
                             AND event.received_at < $1
                       )::bigint AS recent_count,
                       count(*) FILTER (
                           WHERE event.received_at >= $3
                             AND event.received_at < $4
                       )::bigint AS previous_count,
                       COALESCE(c.view_count,0)::bigint AS view_count
                FROM eligible e
                JOIN article_view_events event
                  ON event.article_id=e.article_id
                 AND event.received_at >= $3
                 AND event.received_at < $1
                LEFT JOIN article_view_counts c ON c.article_id=e.article_id
                GROUP BY e.article_id,e.published_at,e.keywords,c.view_count
            ),
            growth_scores AS (
                SELECT article_id,published_at,keywords,
                       recent_count,previous_count,view_count,
                       (recent_count-previous_count)::double precision
                           / GREATEST(previous_count,1) AS metric_value
                FROM growth_counts
                WHERE recent_count+previous_count >= $5
            ),
            view_growth AS (
                SELECT 'view_growth'::text AS metric,
                       article_id,published_at,keywords,metric_value,
                       recent_count,previous_count,
                       row_number() OVER (
                           ORDER BY metric_value DESC,recent_count DESC,
                                    view_count DESC,published_at DESC,
                                    article_id DESC
                       ) AS rank
                FROM growth_scores
            ),
            bookmark_counts AS (
                SELECT saved.key::text AS article_id,
                       count(*)::bigint AS bookmark_count
                FROM app_account_reading_states state
                CROSS JOIN LATERAL jsonb_each_text(state.saved_articles) saved
                GROUP BY saved.key
            ),
            bookmark_totals AS (
                SELECT 'bookmark_total'::text AS metric,
                       e.article_id,e.published_at,e.keywords,
                       b.bookmark_count::double precision AS metric_value,
                       0::bigint AS recent_count,
                       0::bigint AS previous_count,
                       row_number() OVER (
                           ORDER BY b.bookmark_count DESC,
                                    e.published_at DESC,e.article_id DESC
                       ) AS rank
                FROM eligible e
                JOIN bookmark_counts b ON b.article_id=e.article_id
                WHERE b.bookmark_count > 0
            ),
            combined AS (
                SELECT * FROM view_totals
                UNION ALL SELECT * FROM view_growth
                UNION ALL SELECT * FROM bookmark_totals
            )
            SELECT metric,article_id,rank,metric_value,
                   recent_count,previous_count,keywords
            FROM combined
            WHERE rank <= $6
            ORDER BY CASE metric
                         WHEN 'view_total' THEN 1
                         WHEN 'view_growth' THEN 2
                         WHEN 'bookmark_total' THEN 3
                         ELSE 4
                     END,
                     rank
            """,
            as_of,
            universe_start,
            previous_start,
            recent_start,
            min_growth_events,
            top_n,
        )
        rankings = {metric: [] for metric in TRENDING_METRIC_ORDER}
        for raw_row in rows:
            row = dict(raw_row)
            raw_keywords = row["keywords"]
            if isinstance(raw_keywords, str):
                raw_keywords = json.loads(raw_keywords)
            if not isinstance(raw_keywords, list):
                raise ValueError("trending keywords must be a list")
            metric = TrendingMetric(str(row["metric"]))
            rankings[metric].append(
                TrendingArticle(
                    metric=metric,
                    article_id=str(row["article_id"]),
                    rank=int(row["rank"]),
                    metric_value=float(row["metric_value"]),
                    recent_count=int(row["recent_count"]),
                    previous_count=int(row["previous_count"]),
                    keywords=tuple(raw_keywords[:12]),
                )
            )
        return rankings

    async def current_trending_search_snapshot(
        self,
        *,
        as_of: datetime,
    ) -> TrendingSearchSnapshot:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        bucket_started_at = trending_bucket_started_at(as_of)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT bucket_started_at,category_keywords,keyword_pool,
                           suggestions,generated_at
                    FROM trending_search_snapshots
                    WHERE bucket_started_at=$1
                    """,
                    bucket_started_at,
                )
                if row is not None:
                    return _decode_trending_search_snapshot(row)
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "trending-search-snapshot",
                )
                row = await conn.fetchrow(
                    """
                    SELECT bucket_started_at,category_keywords,keyword_pool,
                           suggestions,generated_at
                    FROM trending_search_snapshots
                    WHERE bucket_started_at=$1
                    """,
                    bucket_started_at,
                )
                if row is not None:
                    return _decode_trending_search_snapshot(row)

                rankings = await self._fetch_trending_article_rankings(
                    conn,
                    as_of=as_of,
                )
                category_keywords = {
                    metric: rank_category_keywords(rankings[metric])[:3]
                    for metric in TRENDING_METRIC_ORDER
                }
                keyword_pool = build_keyword_pool(rankings)
                suggestions = sample_keyword_pool(
                    keyword_pool,
                    bucket_started_at=bucket_started_at,
                )
                generated_at = await conn.fetchval("SELECT now()")
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO trending_search_snapshots(
                        bucket_started_at,category_keywords,keyword_pool,
                        suggestions,generated_at
                    )
                    VALUES ($1,$2::jsonb,$3::jsonb,$4::jsonb,$5)
                    ON CONFLICT (bucket_started_at) DO NOTHING
                    RETURNING bucket_started_at,category_keywords,keyword_pool,
                              suggestions,generated_at
                    """,
                    bucket_started_at,
                    json.dumps(
                        {
                            metric.value: list(values)
                            for metric, values in category_keywords.items()
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(list(keyword_pool), ensure_ascii=False),
                    json.dumps(list(suggestions), ensure_ascii=False),
                    generated_at,
                )
                if inserted is None:
                    inserted = await conn.fetchrow(
                        """
                        SELECT bucket_started_at,category_keywords,keyword_pool,
                               suggestions,generated_at
                        FROM trending_search_snapshots
                        WHERE bucket_started_at=$1
                        """,
                        bucket_started_at,
                    )
                if inserted is None:
                    raise RuntimeError("trending snapshot insert was not visible")
                await conn.execute(
                    """
                    DELETE FROM trending_search_snapshots
                    WHERE bucket_started_at IN (
                        SELECT bucket_started_at
                        FROM trending_search_snapshots
                        WHERE bucket_started_at
                              < $1::timestamptz - interval '24 hours'
                        ORDER BY bucket_started_at
                        LIMIT 1000
                    )
                    """,
                    bucket_started_at,
                )
                return _decode_trending_search_snapshot(inserted)

    async def published_editorial_maps(
        self,
        *,
        publication_kind: str,
        limit: int,
        cursor_published_at: datetime | None = None,
        cursor_article_id: str | None = None,
    ) -> list[dict]:
        if publication_kind not in {"editorial", "weekly_insight"}:
            raise ValueError("invalid editorial discovery publication kind")
        if not 1 <= limit <= 21:
            raise ValueError("invalid editorial discovery limit")
        if (cursor_published_at is None) != (cursor_article_id is None):
            raise ValueError("incomplete editorial discovery cursor")
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id, a.url AS source_url, a.domain,
                   COALESCE(a.seendate,a.first_seen_at) AS published_at,
                   a.language AS source_language, a.sourcecountry,
                   a.document_type, a.document_source_name,
                   m.status, m.data, p.published_at AS sort_at
            FROM editorial_publications p
            JOIN editorial_revisions r ON r.id=p.revision_id
            JOIN articles a ON a.id=p.article_id
            JOIN article_maps m ON m.article_id=a.id
            WHERE p.state='published'
              AND COALESCE(r.document->>'publication_kind','editorial')=$4
              AND m.status IN ('ok','degraded')
              AND m.data IS NOT NULL
              AND (
                  $2::timestamptz IS NULL
                  OR p.published_at < $2
                  OR (p.published_at=$2 AND a.id < $3)
              )
            ORDER BY p.published_at DESC, a.id DESC
            LIMIT $1
            """,
            limit,
            cursor_published_at,
            cursor_article_id,
            publication_kind,
        )
        out: list[dict] = []
        for raw_row in rows:
            item = dict(raw_row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def claim_app_request_cooldown(
        self,
        *,
        key_id: str,
        installation_id: str,
        scope: str,
        cooldown_s: float,
    ) -> float:
        if scope not in {
            "discovery_catalog",
            "discovery_search",
            "feed_latest",
            "feed_random",
        } or cooldown_s <= 0:
            raise ValueError("invalid app request cooldown")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                active = await conn.fetchrow(
                    """
                    SELECT key_id FROM app_attest_keys
                    WHERE key_id=$1 AND installation_id=$2 AND status='active'
                    FOR UPDATE
                    """,
                    key_id,
                    installation_id,
                )
                if active is None:
                    raise LookupError("active App Attest key not found")
                row = await conn.fetchrow(
                    """
                    SELECT last_allowed_at FROM app_request_cooldowns
                    WHERE key_id=$1 AND scope=$2 FOR UPDATE
                    """,
                    key_id,
                    scope,
                )
                now = await conn.fetchval("SELECT now()")
                if row is not None:
                    remaining = cooldown_s - (now - row["last_allowed_at"]).total_seconds()
                    if remaining > 0:
                        return remaining
                await conn.execute(
                    """
                    INSERT INTO app_request_cooldowns(key_id,scope,last_allowed_at)
                    VALUES ($1,$2,$3)
                    ON CONFLICT (key_id,scope) DO UPDATE
                    SET last_allowed_at=EXCLUDED.last_allowed_at
                    """,
                    key_id,
                    scope,
                    now,
                )
                return 0.0

    async def get_article(self, article_id: str) -> Optional[dict]:
        row = await self.pool.fetchrow(
            "SELECT * FROM articles WHERE id = $1", article_id
        )
        return dict(row) if row else None

    async def recent_article_maps(
        self,
        *,
        limit: int,
        cursor_sort_at=None,
        cursor_article_id: str | None = None,
        editorial_day: date | None = None,
        cursor_priority_bucket: int = 1,
        cursor_featured_bucket: int = 1,
        cursor_order_bucket: int = 1,
        cursor_display_order: int = 2_147_483_647,
    ) -> list[dict]:
        """Return Maps with today's live manual publications ranked first."""
        if editorial_day is None:
            from .feed import seoul_editorial_day

            editorial_day = seoul_editorial_day()
        rows = await self.pool.fetch(
            """
            WITH ranked AS (
                SELECT a.id AS article_id, a.url AS source_url, a.domain,
                       a.seendate AS published_at, a.language AS source_language,
                       a.sourcecountry, a.document_type, a.document_source_name,
                       COALESCE(p.published_at, a.seendate, a.first_seen_at) AS sort_at,
                       m.status, m.data,
                       $1::date AS editorial_day,
                       CASE
                           WHEN p.state='published'
                            AND (p.published_at AT TIME ZONE 'Asia/Seoul')::date=$1
                           THEN 0 ELSE 1
                       END AS priority_bucket,
                       CASE
                           WHEN p.state='published' AND p.is_featured
                            AND (p.published_at AT TIME ZONE 'Asia/Seoul')::date=$1
                           THEN 0 ELSE 1
                       END AS featured_bucket,
                       CASE
                           WHEN p.state='published' AND p.display_order IS NOT NULL
                            AND (p.published_at AT TIME ZONE 'Asia/Seoul')::date=$1
                           THEN 0 ELSE 1
                       END AS order_bucket,
                       p.display_order,
                       CASE
                           WHEN p.state='published' AND p.display_order IS NOT NULL
                            AND (p.published_at AT TIME ZONE 'Asia/Seoul')::date=$1
                           THEN p.display_order ELSE 2147483647
                       END AS cursor_display_order
                FROM articles a
                JOIN article_maps m ON m.article_id = a.id
                LEFT JOIN editorial_publications p ON p.article_id = a.id
                WHERE m.status IN ('ok', 'degraded')
                  AND m.data IS NOT NULL
                  AND (p.draft_id IS NULL OR p.state='published')
            )
            SELECT * FROM ranked
            WHERE $6::timestamptz IS NULL
               OR priority_bucket > $2
               OR (priority_bucket=$2 AND featured_bucket > $3)
               OR (priority_bucket=$2 AND featured_bucket=$3
                   AND order_bucket > $4)
               OR (priority_bucket=$2 AND featured_bucket=$3
                   AND order_bucket=$4 AND cursor_display_order > $5)
               OR (priority_bucket=$2 AND featured_bucket=$3
                   AND order_bucket=$4 AND cursor_display_order=$5
                   AND sort_at < $6)
               OR (priority_bucket=$2 AND featured_bucket=$3
                   AND order_bucket=$4 AND cursor_display_order=$5
                   AND sort_at=$6 AND article_id < $7)
            ORDER BY priority_bucket ASC, featured_bucket ASC,
                     order_bucket ASC, cursor_display_order ASC,
                     sort_at DESC, article_id DESC
            LIMIT $8
            """,
            editorial_day,
            cursor_priority_bucket,
            cursor_featured_bucket,
            cursor_order_bucket,
            cursor_display_order,
            cursor_sort_at,
            cursor_article_id,
            limit,
        )
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def latest_article_maps(
        self,
        *,
        limit: int,
        excluded_article_ids: list[str],
    ) -> list[dict]:
        """Return public Maps ordered by when they became available in the app."""
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id, a.url AS source_url, a.domain,
                   a.seendate AS published_at, a.language AS source_language,
                   a.sourcecountry, a.document_type, a.document_source_name,
                   COALESCE(p.published_at, m.created_at) AS listed_at,
                   m.status, m.data
            FROM articles a
            JOIN article_maps m ON m.article_id = a.id
            LEFT JOIN editorial_publications p ON p.article_id = a.id
            WHERE m.status IN ('ok', 'degraded')
              AND m.data IS NOT NULL
              AND (p.draft_id IS NULL OR p.state='published')
              AND NOT (a.id = ANY($1::text[]))
            ORDER BY listed_at DESC, a.id DESC
            LIMIT $2
            """,
            excluded_article_ids,
            limit,
        )
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def random_article_maps(
        self,
        *,
        limit: int,
        excluded_article_ids: list[str],
        random_seed: str,
    ) -> list[dict]:
        """Return eligible Maps in a request-seeded random order."""
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id, a.url AS source_url, a.domain,
                   a.seendate AS published_at, a.language AS source_language,
                   a.sourcecountry, a.document_type, a.document_source_name,
                   COALESCE(p.published_at, a.seendate, a.first_seen_at) AS sort_at,
                   m.status, m.data
            FROM articles a
            JOIN article_maps m ON m.article_id = a.id
            LEFT JOIN editorial_publications p ON p.article_id = a.id
            WHERE m.status IN ('ok', 'degraded')
              AND m.data IS NOT NULL
              AND (p.draft_id IS NULL OR p.state='published')
              AND NOT (a.id = ANY($1::text[]))
            ORDER BY md5(a.id || $2)
            LIMIT $3
            """,
            excluded_article_ids,
            random_seed,
            limit,
        )
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("data"), str):
                item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    async def editorial_today(self, editorial_day: date) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT p.draft_id, p.article_id,
                   COALESCE(d.document->>'headline', a.title, '') AS headline,
                   p.published_at, p.display_order, p.is_featured
            FROM editorial_publications p
            JOIN editorial_drafts d ON d.id=p.draft_id
            JOIN articles a ON a.id=p.article_id
            WHERE p.state='published'
              AND (p.published_at AT TIME ZONE 'Asia/Seoul')::date=$1
            ORDER BY p.is_featured DESC,
                     (p.display_order IS NULL) ASC,
                     p.display_order ASC NULLS LAST,
                     p.published_at DESC,
                     p.article_id DESC
            LIMIT 100
            """,
            editorial_day,
        )
        return [dict(row) for row in rows]

    async def set_editorial_today_order(
        self,
        *,
        editorial_day: date,
        ordered_article_ids: list[str],
        featured_article_id: str | None,
        operator_id: UUID,
    ) -> dict:
        requested = set(ordered_article_ids)
        if featured_article_id is not None:
            requested.add(featured_article_id)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                eligible_rows = await connection.fetch(
                    """
                    SELECT article_id
                    FROM editorial_publications
                    WHERE state='published'
                      AND (published_at AT TIME ZONE 'Asia/Seoul')::date=$1
                    FOR UPDATE
                    """,
                    editorial_day,
                )
                eligible = {str(row["article_id"]) for row in eligible_rows}
                invalid = sorted(requested - eligible)
                if invalid:
                    return {"kind": "invalid", "invalid_article_ids": invalid}
                rows = await connection.fetch(
                    """
                    WITH updated AS (
                        UPDATE editorial_publications
                        SET display_order = CASE
                                WHEN article_id = ANY($2::text[])
                                THEN array_position($2::text[], article_id) - 1
                                ELSE NULL
                            END,
                            is_featured = (
                                $3::text IS NOT NULL AND article_id=$3
                            ),
                            updated_at=now()
                        WHERE state='published'
                          AND (published_at AT TIME ZONE 'Asia/Seoul')::date=$1
                        RETURNING *
                    )
                    SELECT u.draft_id, u.article_id,
                           COALESCE(d.document->>'headline', a.title, '') AS headline,
                           u.published_at, u.display_order, u.is_featured
                    FROM updated u
                    JOIN editorial_drafts d ON d.id=u.draft_id
                    JOIN articles a ON a.id=u.article_id
                    ORDER BY u.is_featured DESC,
                             (u.display_order IS NULL) ASC,
                             u.display_order ASC NULLS LAST,
                             u.published_at DESC,
                             u.article_id DESC
                    """,
                    editorial_day,
                    ordered_article_ids,
                    featured_article_id,
                )
                await connection.execute(
                    """
                    INSERT INTO editorial_audit_log
                        (operator_id,action,target_type,target_id,metadata)
                    VALUES ($1,'today.order.updated','editorial_day',$2,
                            $3::jsonb)
                    """,
                    operator_id,
                    editorial_day.isoformat(),
                    json.dumps(
                        {
                            "ordered_article_ids": ordered_article_ids,
                            "featured_article_id": featured_article_id,
                        },
                        ensure_ascii=False,
                    ),
                )
        return {"kind": "updated", "items": [dict(row) for row in rows]}

    # ---- persistent Map work queue -------------------------------------
    async def recover_expired_map_leases(self, *, lease_seconds: float) -> int:
        """Return abandoned processing rows to the queue before admission."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        value = await self.pool.fetchval(
            """
            WITH recovered AS (
                UPDATE map_queue
                SET status = 'queued',
                    lease_expires_at = NULL,
                    lease_owner = NULL,
                    next_attempt_at = LEAST(next_attempt_at, now()),
                    last_error = COALESCE(
                        last_error,
                        'processing lease expired'
                    ),
                    updated_at = now()
                WHERE status = 'processing'
                  AND COALESCE(
                      lease_expires_at,
                      updated_at
                        + ($1::double precision * interval '1 second')
                  ) <= now()
                RETURNING 1
            )
            SELECT count(*) FROM recovered
            """,
            lease_seconds,
        )
        return int(value or 0)

    async def recover_orphaned_map_leases(self, *, stale_after_s: float) -> int:
        """Return owned leases whose worker has stopped or gone stale to due work.

        Ownerless rows deliberately keep the normal expiry-only policy: a row
        created before owner tracking cannot be distinguished safely from a
        live legacy worker during a rolling deployment.
        """
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        value = await self.pool.fetchval(
            """
            WITH recovered AS (
                UPDATE map_queue q
                SET status = 'queued',
                    lease_expires_at = NULL,
                    lease_owner = NULL,
                    next_attempt_at = LEAST(q.next_attempt_at, now()),
                    last_error = COALESCE(
                        q.last_error,
                        'processing lease owner unavailable'
                    ),
                    updated_at = now()
                WHERE q.status = 'processing'
                  AND q.lease_owner IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM worker_instances wi
                      WHERE wi.instance_id = q.lease_owner
                        AND wi.stopped_at IS NULL
                        AND wi.last_seen_at >= now()
                            - ($1::double precision * interval '1 second')
                  )
                RETURNING 1
            )
            SELECT count(*) FROM recovered
            """,
            stale_after_s,
        )
        return int(value or 0)

    async def reconcile_map_queue(self, *, max_attempts: int) -> int:
        """Idempotently cover legacy articles and align successful Maps.

        Existing retry/dead state is preserved unless a successful Map proves
        that the work is complete. The returned count includes inserted rows
        and rows corrected to ``done``.

        Restricted to domains that own a positive daily budget. Gating only
        ``upsert_article`` would leak: this sweep would pull every quarantined
        article into the queue at once on the next ``map_pending`` call.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        value = await self.pool.fetchval(
            """
            WITH reconciled AS (
                INSERT INTO map_queue
                    (article_id, status, attempts, max_attempts,
                     next_attempt_at, lease_expires_at, last_error)
                SELECT
                    a.id,
                    CASE
                        WHEN m.status IN ('ok', 'degraded') THEN 'done'
                        -- held_* is a finished Map withheld from readers.
                        WHEN m.status IN ('held_ok', 'held_degraded') THEN 'done'
                        WHEN m.status = 'failed' THEN 'dead'
                        ELSE 'queued'
                    END,
                    CASE WHEN m.status = 'failed' THEN $1 ELSE 0 END,
                    $1,
                    now(),
                    NULL,
                    CASE WHEN m.status = 'failed' THEN m.error ELSE NULL END
                FROM articles a
                LEFT JOIN article_maps m ON m.article_id = a.id
                WHERE a.coverage_domain = ANY($2::text[])
                ON CONFLICT (article_id) DO UPDATE SET
                    status = 'done',
                    next_attempt_at = now(),
                    lease_expires_at = NULL,
                    lease_owner = NULL,
                    last_error = NULL,
                    updated_at = now()
                WHERE EXCLUDED.status = 'done'
                  AND map_queue.status <> 'done'
                RETURNING 1
            )
            SELECT count(*) FROM reconciled
            """,
            max_attempts,
            list(await self.mappable_domains()),
        )
        return int(value or 0)

    async def queue_requeue(
        self,
        article_id: str,
        *,
        error: str,
        max_attempts: int,
        base_delay_s: float,
    ) -> tuple[str, int]:
        """Register a failed attempt with exponential backoff.

        Returns (status, attempts). status is 'dead' once attempts exhaust
        max_attempts, otherwise 'queued' with next_attempt_at in the future.
        """
        cur = await self.pool.fetchval(
            "SELECT attempts FROM map_queue WHERE article_id=$1", article_id
        )
        attempts = (cur or 0) + 1
        if attempts >= max_attempts:
            status, delay = "dead", 0.0
        else:
            status = "queued"
            delay = base_delay_s * (2 ** (attempts - 1))  # 1x, 2x, 4x, ...
        await self.pool.execute(
            """
            INSERT INTO map_queue
                (article_id, status, attempts, max_attempts, next_attempt_at,
                 lease_expires_at, last_error)
            VALUES ($1,$2,$3,$4, now() + ($5 || ' seconds')::interval, NULL, $6)
            ON CONFLICT (article_id) DO UPDATE SET
                status = EXCLUDED.status,
                attempts = EXCLUDED.attempts,
                max_attempts = EXCLUDED.max_attempts,
                next_attempt_at = EXCLUDED.next_attempt_at,
                lease_expires_at = NULL,
                lease_owner = NULL,
                last_error = EXCLUDED.last_error,
                updated_at = now()
            """,
            article_id,
            status,
            attempts,
            max_attempts,
            str(delay),
            error[:1000],
        )
        return status, attempts

    async def queue_claim_next(
        self,
        *,
        lease_seconds: float,
        max_active: int,
        lease_owner: Optional[str] = None,
        budget: Optional[MapBudget] = None,
        ranked: bool = False,
        stale_after_s: float = 21600.0,
        neutral_score: int = 50,
        coverage_domain: str | None = None,
    ) -> Optional[str]:
        """Claim the globally oldest eligible Map job, or return ``None``.

        A short transaction advisory lock serializes claim decisions across
        processes. The lock is released before any extraction/LLM work; the
        processing leases then enforce the global active ceiling and recover
        crashed workers after a bounded interval.

        With a ``budget`` the same lock also spends one Seoul editorial day's
        allowance: the claim is charged to the fresh or backlog bucket it came
        from, so an exhausted day returns ``None`` instead of a job. ``ranked``
        additionally orders candidates by triage score.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_active < 1:
            raise ValueError("max_active must be at least 1")
        if lease_owner is not None:
            lease_owner = lease_owner.strip()
            if not lease_owner:
                raise ValueError("lease_owner must not be blank")
        if coverage_domain is not None:
            coverage_domain = coverage_domain.strip().casefold()
            if not coverage_domain:
                raise ValueError("coverage_domain must not be blank")
        if budget is not None:
            if stale_after_s <= 0:
                raise ValueError("stale_after_s must be positive")
            if not 0 <= neutral_score <= 100:
                raise ValueError("neutral_score must be between 0 and 100")
            return await self._budgeted_claim(
                lease_seconds=lease_seconds,
                max_active=max_active,
                lease_owner=lease_owner,
                budget=budget,
                ranked=ranked,
                stale_after_s=stale_after_s,
                neutral_score=neutral_score,
                coverage_domain=coverage_domain,
            )
        if coverage_domain == "crypto":
            raise ValueError("crypto claims require the budgeted path")
        domain_clause = (
            "" if coverage_domain is None else "AND a.coverage_domain = $4"
        )
        claim_args = (
            (lease_seconds, max_active, lease_owner)
            if coverage_domain is None
            else (lease_seconds, max_active, lease_owner, coverage_domain)
        )
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('text-shorts-map-fifo'))"
                )
                row = await conn.fetchrow(
                    f"""
                    WITH candidate AS (
                        SELECT q.article_id
                        FROM map_queue q
                        JOIN articles a ON a.id = q.article_id
                        WHERE (
                            (q.status = 'queued' AND q.next_attempt_at <= now())
                            OR (
                                q.status = 'processing'
                                AND COALESCE(
                                    q.lease_expires_at,
                                    q.updated_at
                                      + ($1::double precision * interval '1 second')
                                ) <= now()
                            )
                        )
                        AND (
                            SELECT count(*)
                            FROM map_queue active
                            WHERE active.status = 'processing'
                              AND COALESCE(
                                  active.lease_expires_at,
                                  active.updated_at
                                    + ($1::double precision * interval '1 second')
                              ) > now()
                        ) < $2
                        -- Crypto is budget/domain-gated and may never fall back
                        -- to the legacy unbudgeted FIFO path.
                        AND a.coverage_domain <> 'crypto'
                        {domain_clause}
                        ORDER BY a.first_seen_at ASC,
                                 q.created_at ASC,
                                 q.article_id ASC
                        LIMIT 1
                        FOR UPDATE OF q SKIP LOCKED
                    )
                    UPDATE map_queue q
                    SET status = 'processing',
                        lease_expires_at = now()
                          + ($1::double precision * interval '1 second'),
                        lease_owner = $3,
                        updated_at = now()
                    FROM candidate
                    WHERE q.article_id = candidate.article_id
                    RETURNING q.article_id
                    """,
                    *claim_args,
                )
        return str(row["article_id"]) if row else None

    async def crypto_relevance_ready(self, article_id: str) -> bool:
        """Whether a crypto article has the same hard-gate proof as queue claim."""
        value = await self.pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM articles a
                JOIN map_queue q ON q.article_id = a.id
                WHERE a.id = $1
                  AND a.coverage_domain = 'crypto'
                  AND q.triage_score IS NOT NULL
                  AND q.triage_scored_at IS NOT NULL
                  AND q.triage_model IS NOT NULL
                  AND jsonb_typeof(q.triage_signals->'axes') = 'object'
                  AND q.triage_signals @> '{
                      "fallback": false,
                      "profile": "crypto",
                      "relevance": "on_topic"
                  }'::jsonb
            )
            """,
            article_id,
        )
        return bool(value)

    _BUDGETED_CLAIM_SQL = """
        WITH candidate AS (
            SELECT q.article_id
            FROM map_queue q
            JOIN articles a ON a.id = q.article_id
            WHERE (
                (q.status = 'queued' AND q.next_attempt_at <= now())
                OR (
                    q.status = 'processing'
                    AND COALESCE(
                        q.lease_expires_at,
                        q.updated_at
                          + ($1::double precision * interval '1 second')
                    ) <= now()
                )
            )
            AND (
                SELECT count(*)
                FROM map_queue active
                WHERE active.status = 'processing'
                  AND COALESCE(
                      active.lease_expires_at,
                      active.updated_at
                        + ($1::double precision * interval '1 second')
                  ) > now()
            ) < $2
            AND (
                $3::text IS NULL
                OR CASE
                     WHEN (a.first_seen_at AT TIME ZONE 'Asia/Seoul')::date
                          >= $4::date
                     THEN 'fresh'
                     ELSE 'backlog'
                   END = $3::text
            )
            AND (
                NOT $11::boolean
                OR (
                    q.triage_score IS NOT NULL
                    AND q.triage_scored_at IS NOT NULL
                    AND q.triage_model IS NOT NULL
                    AND jsonb_typeof(q.triage_signals->'axes') = 'object'
                    AND q.triage_signals @> '{
                        "fallback": false,
                        "profile": "crypto",
                        "relevance": "on_topic"
                    }'::jsonb
                )
            )
            AND (
                $11::boolean
                OR NOT $5::boolean
                OR q.triage_scored_at IS NOT NULL
                OR q.attempts > 0
                OR q.created_at <= now()
                     - ($6::double precision * interval '1 second')
                )
            -- NULL means "no domain axis configured": the claim then behaves
            -- exactly as it did before per-domain budgets existed for core.
            -- Crypto remains quarantined until its explicit domain row exists.
            AND ($9::text IS NULL OR a.coverage_domain = $9::text)
            AND ($9::text IS NOT NULL OR a.coverage_domain <> 'crypto')
            -- NULL = either subtype, FALSE = ordinary article, TRUE = curated
            -- primary document. This is the strict DR-13217 13:7 gate.
            AND (
                $10::boolean IS NULL
                OR (a.document_type IS NOT NULL) = $10::boolean
            )
            ORDER BY
                CASE
                    WHEN $5::boolean THEN COALESCE(q.triage_score, $7::int)
                    ELSE 0
                END DESC,
                a.first_seen_at ASC,
                q.created_at ASC,
                q.article_id ASC
            LIMIT 1
            FOR UPDATE OF q SKIP LOCKED
        )
        UPDATE map_queue q
        SET status = 'processing',
            lease_expires_at = now()
              + ($1::double precision * interval '1 second'),
            lease_owner = $8,
            updated_at = now()
        FROM candidate
        JOIN articles claimed ON claimed.id = candidate.article_id
        WHERE q.article_id = candidate.article_id
        RETURNING q.article_id,
                  claimed.coverage_domain,
                  CASE
                      WHEN (claimed.first_seen_at AT TIME ZONE 'Asia/Seoul')::date
                           >= $4::date
                      THEN 'fresh'
                      ELSE 'backlog'
                  END AS bucket
    """

    async def set_domain_reader_exposure(
        self,
        coverage_domain: str,
        *,
        exposed: bool,
    ) -> int:
        """Flip one domain's reader gate and re-state its stored Maps.

        Opening the gate must not lose the ok/degraded distinction the Map was
        written with, so the held status carries it and is unwrapped here. The
        flag and the rows move together in one transaction: a half-applied gate
        would either hide published articles or expose withheld ones.
        """
        if not coverage_domain.strip():
            raise ValueError("coverage_domain must not be blank")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO map_budget_domain_settings
                        (coverage_domain, daily_map_limit, reader_exposed)
                    VALUES ($1, 0, $2)
                    ON CONFLICT (coverage_domain) DO UPDATE SET
                        reader_exposed = EXCLUDED.reader_exposed,
                        updated_at = now()
                    """,
                    coverage_domain,
                    exposed,
                )
                restated = await conn.fetch(
                    """
                    UPDATE article_maps m
                    SET status = CASE
                                     WHEN $2::boolean
                                     THEN substring(m.status FROM 6)
                                     ELSE 'held_' || m.status
                                 END,
                        updated_at = now()
                    FROM articles a
                    WHERE a.id = m.article_id
                      AND a.coverage_domain = $1
                      AND m.status = ANY(
                          CASE WHEN $2::boolean
                               THEN ARRAY['held_ok', 'held_degraded']
                               ELSE ARRAY['ok', 'degraded']
                          END
                      )
                    RETURNING m.article_id
                    """,
                    coverage_domain,
                    exposed,
                )
                return len(restated)

    async def chainable_articles(
        self,
        *,
        coverage_domain: str,
        since: datetime,
        limit: int,
    ) -> list[dict]:
        """Recent mapped articles in one domain, with what identifies them."""
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id,
                   a.title,
                   a.domain,
                   COALESCE(a.seendate, a.first_seen_at) AS published_at,
                   m.data->'editorial'->>'source_hash' AS source_hash,
                   COALESCE(m.data->'tagged_atoms', '[]'::jsonb) AS tagged_atoms
            FROM articles a
            JOIN article_maps m ON m.article_id = a.id
            WHERE a.coverage_domain = $1
              AND m.status IN ('ok','degraded','held_ok','held_degraded')
              AND COALESCE(a.seendate, a.first_seen_at) >= $2
            ORDER BY COALESCE(a.seendate, a.first_seen_at) DESC, a.id
            LIMIT $3
            """,
            coverage_domain,
            since,
            limit,
        )
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            raw = item.get("tagged_atoms")
            item["tagged_atoms"] = (
                json.loads(raw) if isinstance(raw, str) else (raw or [])
            )
            results.append(item)
        return results

    async def chain_ids_for_articles(
        self, article_ids: Sequence[str]
    ) -> dict[str, str]:
        ids = [value for value in dict.fromkeys(article_ids) if value]
        if not ids:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT article_id, chain_id FROM event_chain_members
            WHERE article_id = ANY($1::text[])
            """,
            ids,
        )
        return {str(row["article_id"]): str(row["chain_id"]) for row in rows}

    async def join_event_chain(
        self, *, chain_id: str, article_id: str, similarity: float
    ) -> bool:
        """Add one article to a chain, or do nothing if it already has one.

        The unique index on article_id is what makes a concurrent writer lose
        the race rather than split one article across two chains.
        """
        result = await self.pool.execute(
            """
            INSERT INTO event_chain_members (chain_id, article_id, similarity)
            VALUES ($1::uuid, $2, $3)
            ON CONFLICT (article_id) DO NOTHING
            """,
            chain_id,
            article_id,
            float(similarity),
        )
        if str(result).endswith("1"):
            await self.pool.execute(
                "UPDATE event_chains SET updated_at=now() WHERE id=$1::uuid",
                chain_id,
            )
            return True
        return False

    async def open_event_chain(
        self,
        *,
        chain_id: str,
        coverage_domain: str,
        members: Sequence[tuple[str, float]],
    ) -> bool:
        """Create a chain and seed it. A chain of one is never left behind."""
        if len(members) < 2:
            return False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO event_chains (id, coverage_domain)
                    VALUES ($1::uuid, $2)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    chain_id,
                    coverage_domain,
                )
                inserted = 0
                for article_id, similarity in members:
                    result = await conn.execute(
                        """
                        INSERT INTO event_chain_members
                            (chain_id, article_id, similarity)
                        VALUES ($1::uuid, $2, $3)
                        ON CONFLICT (article_id) DO NOTHING
                        """,
                        chain_id,
                        article_id,
                        float(similarity),
                    )
                    if str(result).endswith("1"):
                        inserted += 1
                if inserted < 2:
                    # Another writer already took the members. A chain of one
                    # is not an event, so leave no empty shell behind.
                    raise EmptyChainRollback()
        return True

    async def chain_articles(self, chain_id: str) -> list[dict]:
        """Every report in one chain, oldest first, with its tagged figures."""
        rows = await self.pool.fetch(
            """
            SELECT a.id AS article_id,
                   a.document_type,
                   COALESCE(a.seendate, a.first_seen_at) AS published_at,
                   COALESCE(m.data->'tagged_atoms', '[]'::jsonb) AS tagged_atoms
            FROM event_chain_members em
            JOIN articles a ON a.id = em.article_id
            JOIN article_maps m ON m.article_id = a.id
            WHERE em.chain_id = $1::uuid
            ORDER BY COALESCE(a.seendate, a.first_seen_at), a.id
            """,
            chain_id,
        )
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            raw = item.get("tagged_atoms")
            item["tagged_atoms"] = (
                json.loads(raw) if isinstance(raw, str) else (raw or [])
            )
            results.append(item)
        return results

    async def chain_join_times(
        self, article_ids: Sequence[str]
    ) -> dict[str, list[datetime]]:
        """When anything joined the chain each given article belongs to."""
        ids = [value for value in dict.fromkeys(article_ids) if value]
        if not ids:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT mine.article_id, sibling.joined_at
            FROM event_chain_members mine
            JOIN event_chain_members sibling
              ON sibling.chain_id = mine.chain_id
             AND sibling.article_id <> mine.article_id
            WHERE mine.article_id = ANY($1::text[])
            ORDER BY mine.article_id, sibling.joined_at
            """,
            ids,
        )
        joins: dict[str, list[datetime]] = {}
        for row in rows:
            joins.setdefault(str(row["article_id"]), []).append(row["joined_at"])
        return joins

    async def read_market_values(
        self,
        subjects: Sequence[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], dict]:
        """Cached values for the given subjects. Missing keys stay missing."""
        keys = list(dict.fromkeys(subjects))
        if not keys:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT tag, subject_kind, subject_id, value, unit, provider,
                   attribution, observed_at, fetched_at
            FROM market_value_cache
            WHERE (tag, subject_kind, subject_id) = ANY($1::record[])
            """,
            keys,
        )
        return {
            (str(row["tag"]), str(row["subject_kind"]), str(row["subject_id"])): dict(row)
            for row in rows
        }

    async def write_market_values(self, entries: Sequence[dict]) -> int:
        """Upsert freshly fetched values; a write failure is never fatal."""
        if not entries:
            return 0
        written = 0
        for entry in entries:
            await self.pool.execute(
                """
                INSERT INTO market_value_cache
                    (tag, subject_kind, subject_id, value, unit, provider,
                     attribution, observed_at, fetched_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now())
                ON CONFLICT (tag, subject_kind, subject_id) DO UPDATE SET
                    value = EXCLUDED.value,
                    unit = EXCLUDED.unit,
                    provider = EXCLUDED.provider,
                    attribution = EXCLUDED.attribution,
                    observed_at = EXCLUDED.observed_at,
                    fetched_at = now()
                """,
                entry["tag"],
                entry["subject_kind"],
                entry["subject_id"],
                float(entry["value"]),
                entry["unit"],
                entry["provider"],
                entry.get("attribution") or "",
                entry["observed_at"],
            )
            written += 1
        return written

    # --- publisher interest research (C-13212, DR-13247) -----------------

    async def read_publisher_interest(
        self,
        domain: str,
        assets_key: str,
        *,
        max_age_days: int = 30,
    ) -> dict | None:
        """The cached verdict for this publisher and asset set, if still fresh.

        The age bound lives in SQL rather than in Python so a stale row can
        never be used by a caller that forgot to check it.
        """
        row = await self.pool.fetchrow(
            """
            SELECT domain, assets_key, relation, owner, asset, confidence,
                   sentence, evidence, registry_status, researched_at
            FROM publisher_interest_findings
            WHERE domain = $1
              AND assets_key = $2
              AND researched_at > now() - make_interval(days => $3::int)
            """,
            domain,
            assets_key,
            int(max_age_days),
        )
        return dict(row) if row is not None else None

    async def write_publisher_interest(self, entry: Mapping[str, Any]) -> None:
        """Upsert one verdict, including 'none' (DR-13247)."""
        await self.pool.execute(
            """
            INSERT INTO publisher_interest_findings
                (domain, assets_key, relation, owner, asset, confidence,
                 sentence, evidence, article_id, editorial_day, researched_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10, now())
            ON CONFLICT (domain, assets_key) DO UPDATE SET
                relation = EXCLUDED.relation,
                owner = EXCLUDED.owner,
                asset = EXCLUDED.asset,
                confidence = EXCLUDED.confidence,
                sentence = EXCLUDED.sentence,
                evidence = EXCLUDED.evidence,
                article_id = EXCLUDED.article_id,
                editorial_day = EXCLUDED.editorial_day,
                registry_status = 'proposed',
                researched_at = now()
            """,
            str(entry["domain"]),
            str(entry["assets_key"]),
            str(entry["relation"]),
            str(entry.get("owner") or ""),
            str(entry.get("asset") or ""),
            str(entry["confidence"]),
            str(entry.get("sentence") or ""),
            json.dumps(entry.get("evidence") or [], ensure_ascii=False),
            entry.get("article_id"),
            entry["editorial_day"],
        )

    async def count_publisher_research_runs(self, editorial_day: date) -> int:
        """Verdicts recorded today, 'none' included: each one cost a search."""
        value = await self.pool.fetchval(
            """
            SELECT count(*)
            FROM publisher_interest_findings
            WHERE editorial_day = $1
            """,
            editorial_day,
        )
        return int(value or 0)

    async def list_publisher_interest_proposals(self, *, limit: int = 50) -> list[dict]:
        """The registry promotion queue an operator reads (DR-13248)."""
        rows = await self.pool.fetch(
            """
            SELECT domain, assets_key, relation, owner, asset, confidence,
                   sentence, evidence, article_id, editorial_day,
                   registry_status, researched_at
            FROM publisher_interest_findings
            WHERE relation <> 'none'
            ORDER BY researched_at DESC
            LIMIT $1
            """,
            max(1, int(limit)),
        )
        return [dict(row) for row in rows or ()]

    async def read_triage_signals(self, article_id: str) -> dict | None:
        """The stored triage verdict for one article, or None."""
        row = await self.pool.fetchrow(
            """
            SELECT triage_signals
            FROM map_queue
            WHERE article_id = $1
            """,
            article_id,
        )
        if row is None:
            return None
        signals = row["triage_signals"]
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except (TypeError, ValueError):
                return None
        return signals if isinstance(signals, dict) else None

    async def mappable_domains(self) -> tuple[str, ...]:
        """Coverage domains allowed into ``map_queue`` right now.

        A domain earns queue access by owning a positive daily budget: without
        one it could never win a claim anyway, and letting it queue would only
        grow an unservable backlog. Before the DR-13206 axis exists the answer
        is 'core' alone, which is the DR-13208 quarantine.
        """
        try:
            rows = await self.pool.fetch(
                """
                SELECT coverage_domain
                FROM map_budget_domain_settings
                WHERE daily_map_limit > 0
                ORDER BY coverage_domain
                """
            )
        except asyncpg.UndefinedTableError:
            return ("core",)
        domains = tuple(str(row["coverage_domain"]) for row in rows or ())
        # An empty axis must not open the floodgates; fall back to quarantine.
        return domains or ("core",)

    async def _domain_budgets(self, conn) -> tuple[DomainBudget, ...]:
        """Live per-domain limits, or an empty tuple when the axis is absent.

        An empty result is not an error: it means this deployment has not run
        the DR-13206 migration yet, and the caller then spends one shared
        allowance exactly as it did before.
        """
        try:
            rows = await conn.fetch(
                """
                SELECT coverage_domain, daily_map_limit, document_map_limit
                FROM map_budget_domain_settings
                """
            )
        except asyncpg.UndefinedTableError:
            return ()
        return tuple(
            DomainBudget(
                coverage_domain=str(row["coverage_domain"]),
                daily_limit=int(row["daily_map_limit"]),
                document_map_limit=int(row["document_map_limit"]),
            )
            for row in rows or ()
        )

    async def _budgeted_claim(
        self,
        *,
        lease_seconds: float,
        max_active: int,
        lease_owner: Optional[str],
        budget: MapBudget,
        ranked: bool,
        stale_after_s: float,
        neutral_score: int,
        coverage_domain: str | None = None,
    ) -> Optional[str]:
        """Claim one job inside the day's remaining allowance and charge it."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('text-shorts-map-fifo'))"
                )
                now = await conn.fetchval("SELECT clock_timestamp()")
                # Read the live budget inside the lock so an /ops change takes
                # effect on the very next claim without a redeploy.
                live = await conn.fetchrow(
                    """
                    SELECT daily_map_limit, fresh_share_percent, daily_run_count
                    FROM map_budget_settings
                    WHERE id = 1
                    """
                )
                if live is not None:
                    budget = MapBudget(
                        daily_limit=int(live["daily_map_limit"]),
                        fresh_share_percent=int(live["fresh_share_percent"]),
                        daily_run_count=int(live["daily_run_count"]),
                    )
                window = map_budget_window(budget, now)
                editorial_day = window.editorial_day
                domains = await self._domain_budgets(conn)
                consumed_rows = await conn.fetch(
                    """
                    SELECT coverage_domain, bucket, consumed
                    FROM map_budget_ledger
                    WHERE editorial_day = $1
                    """,
                    editorial_day,
                )
                per_domain: dict[str, dict[str, int]] = {}
                for ledger_row in consumed_rows:
                    domain_key = str(ledger_row["coverage_domain"])
                    per_domain.setdefault(domain_key, {})[
                        str(ledger_row["bucket"])
                    ] = int(ledger_row["consumed"])

                # Subtype caps are charged from active reservations and
                # committed work. Released pre-provider claims were refunded
                # and therefore do not consume the strict 13:7 split. This
                # read sits under the same advisory transaction as selection
                # and claim insertion, so concurrent claimers cannot overshoot.
                subtype_spend: dict[str, dict[str, int]] = {}
                subtype_domains = [
                    item.coverage_domain
                    for item in domains
                    if item.effective_document_map_limit > 0
                ]
                if subtype_domains:
                    subtype_rows = await conn.fetch(
                        """
                        SELECT c.coverage_domain,
                               count(*) FILTER (
                                   WHERE a.document_type IS NULL
                               ) AS article_consumed,
                               count(*) FILTER (
                                   WHERE a.document_type IS NOT NULL
                               ) AS document_consumed
                        FROM map_budget_claims c
                        JOIN articles a ON a.id = c.article_id
                        WHERE c.editorial_day = $1
                          AND c.coverage_domain = ANY($2::text[])
                          AND c.status IN ('reserved', 'committed')
                        GROUP BY c.coverage_domain
                        """,
                        editorial_day,
                        subtype_domains,
                    )
                    subtype_spend = {
                        str(item["coverage_domain"]): {
                            "article": int(item["article_consumed"]),
                            "document": int(item["document_consumed"]),
                        }
                        for item in subtype_rows
                    }

                # Without a domain axis the claim spends one shared allowance,
                # exactly as it did before DR-13206. The single ``None`` domain
                # filter below keeps that path byte-for-byte identical.
                if not domains:
                    plan: list[
                        tuple[
                            Optional[str],
                            MapBudget,
                            dict[str, int],
                            DomainBudget | None,
                        ]
                    ] = [
                        (
                            None,
                            budget,
                            {
                                bucket: sum(
                                    counts.get(bucket, 0)
                                    for counts in per_domain.values()
                                )
                                for bucket in ("fresh", "backlog")
                            },
                            None,
                        )
                    ]
                else:
                    totals = {
                        item.coverage_domain: sum(
                            per_domain.get(item.coverage_domain, {}).values()
                        )
                        for item in domains
                    }
                    plan = [
                        (
                            item.coverage_domain,
                            MapBudget(
                                daily_limit=item.daily_limit,
                                fresh_share_percent=budget.fresh_share_percent,
                                daily_run_count=budget.daily_run_count,
                            ),
                            per_domain.get(item.coverage_domain, {}),
                            item,
                        )
                        for item in order_domains_by_fill(domains, totals)
                    ]
                if coverage_domain is not None:
                    plan = [item for item in plan if item[0] == coverage_domain]

                row = None
                for (
                    domain_filter,
                    domain_budget,
                    domain_consumed,
                    domain_settings,
                ) in plan:
                    fresh_consumed = domain_consumed.get("fresh", 0)
                    backlog_consumed = domain_consumed.get("backlog", 0)
                    domain_window = (
                        window
                        if domain_filter is None
                        else map_budget_window(domain_budget, now)
                    )
                    if (
                        fresh_consumed + backlog_consumed
                        >= domain_window.current_allowance
                    ):
                        continue
                    document_filter: bool | None = None
                    if domain_settings is not None:
                        document_cap = (
                            domain_settings.effective_document_map_limit
                        )
                        if document_cap == 0:
                            # A settings-backed domain has no document lane
                            # unless the operator explicitly opens one.
                            document_filter = False
                        else:
                            spent = subtype_spend.get(
                                domain_settings.coverage_domain, {}
                            )
                            article_open = (
                                spent.get("article", 0)
                                < domain_settings.article_map_limit
                            )
                            document_open = (
                                spent.get("document", 0) < document_cap
                            )
                            if not article_open and not document_open:
                                continue
                            if document_open and not article_open:
                                document_filter = True
                            elif article_open and not document_open:
                                document_filter = False
                    fresh_quota, backlog_quota = bucket_quotas(
                        domain_budget.daily_limit,
                        domain_budget.fresh_share_percent,
                    )
                    first = preferred_bucket(
                        fresh_quota,
                        fresh_consumed,
                        backlog_quota,
                        backlog_consumed,
                    )
                    second = (
                        "backlog" if first == "fresh" else "fresh"
                    ) if first is not None else None
                    # The trailing ``None`` is the spill attempt: once the
                    # preferred bucket has no eligible candidate, the day's
                    # remaining total may be spent on whatever is actually
                    # there -- inside this domain only, never across domains.
                    attempts: list[Optional[str]] = []
                    for bucket in (first, second, None):
                        if bucket not in attempts:
                            attempts.append(bucket)
                    for bucket in attempts:
                        crypto_relevance_gate = domain_filter == "crypto"
                        claim_ranked = ranked or crypto_relevance_gate
                        row = await conn.fetchrow(
                            self._BUDGETED_CLAIM_SQL,
                            lease_seconds,
                            max_active,
                            bucket,
                            editorial_day,
                            claim_ranked,
                            stale_after_s,
                            neutral_score,
                            lease_owner,
                            domain_filter,
                            document_filter,
                            crypto_relevance_gate,
                        )
                        if row is not None:
                            break
                    if row is not None:
                        break
                if row is None:
                    return None
                charged_domain = str(row["coverage_domain"])
                # A reservation left by a crashed/stale processing lease keeps
                # its historical charge. Close it before creating this claim's
                # independently auditable reservation.
                await conn.execute(
                    """
                    UPDATE map_budget_claims
                    SET status='committed', updated_at=now()
                    WHERE article_id=$1 AND status='reserved'
                    """,
                    str(row["article_id"]),
                )
                await conn.execute(
                    """
                    INSERT INTO map_budget_ledger
                        (editorial_day, coverage_domain, bucket, consumed)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (editorial_day, coverage_domain, bucket)
                    DO UPDATE SET
                        consumed = map_budget_ledger.consumed + 1,
                        updated_at = now()
                    """,
                    editorial_day,
                    charged_domain,
                    str(row["bucket"]),
                )
                await conn.execute(
                    """
                    INSERT INTO map_budget_claims
                        (article_id, editorial_day, coverage_domain, bucket, status)
                    VALUES ($1, $2, $3, $4, 'reserved')
                    """,
                    str(row["article_id"]),
                    editorial_day,
                    charged_domain,
                    str(row["bucket"]),
                )
                return str(row["article_id"])

    async def commit_map_budget_claim(self, article_id: str) -> bool:
        """Commit this article's current budget reservation exactly once."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT id
                    FROM map_budget_claims
                    WHERE article_id=$1 AND status='reserved'
                    ORDER BY id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    article_id,
                )
                if row is None:
                    return False
                result = await conn.execute(
                    """
                    UPDATE map_budget_claims
                    SET status='committed', updated_at=now()
                    WHERE id=$1 AND status='reserved'
                    """,
                    row["id"],
                )
                return result.endswith("1")

    async def release_map_budget_claim(
        self,
        article_id: str,
        *,
        reason: str,
    ) -> bool:
        """Release a pre-provider reservation and refund its exact bucket."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('text-shorts-map-fifo'))"
                )
                row = await conn.fetchrow(
                    """
                    SELECT id, editorial_day, coverage_domain, bucket
                    FROM map_budget_claims
                    WHERE article_id=$1 AND status='reserved'
                    ORDER BY id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    article_id,
                )
                if row is None:
                    return False
                # Refund the exact (day, domain, bucket) that was charged, so a
                # released claim can never leak allowance into another domain.
                ledger = await conn.fetchrow(
                    """
                    UPDATE map_budget_ledger
                    SET consumed=consumed - 1, updated_at=now()
                    WHERE editorial_day=$1 AND coverage_domain=$2
                      AND bucket=$3 AND consumed > 0
                    RETURNING consumed
                    """,
                    row["editorial_day"],
                    row["coverage_domain"],
                    row["bucket"],
                )
                if ledger is None:
                    raise RuntimeError("reserved Map budget claim has no ledger charge")
                result = await conn.execute(
                    """
                    UPDATE map_budget_claims
                    SET status='released', release_reason=$2, updated_at=now()
                    WHERE id=$1 AND status='reserved'
                    """,
                    row["id"],
                    (reason or "pre_provider_failure")[:200],
                )
                if not result.endswith("1"):
                    raise RuntimeError("Map budget claim release lost its reservation")
                return True

    async def map_budget_settings(
        self,
        *,
        default_limit: int,
        default_share: int,
        default_run_count: int,
    ) -> dict:
        """Return the live budget, falling back to first-run env defaults."""
        row = await self.pool.fetchrow(
            """
            SELECT daily_map_limit, fresh_share_percent, daily_run_count, updated_at
            FROM map_budget_settings
            WHERE id = 1
            """
        )
        if row is None:
            budget = MapBudget(
                daily_limit=default_limit,
                fresh_share_percent=default_share,
                daily_run_count=default_run_count,
            )
            return {
                "daily_map_limit": budget.daily_limit,
                "fresh_share_percent": budget.fresh_share_percent,
                "daily_run_count": budget.daily_run_count,
                "updated_at": None,
                "source": "default",
            }
        return {
            "daily_map_limit": int(row["daily_map_limit"]),
            "fresh_share_percent": int(row["fresh_share_percent"]),
            "daily_run_count": int(row["daily_run_count"]),
            "updated_at": row["updated_at"],
            "source": "db",
        }

    async def set_map_budget(
        self,
        *,
        daily_map_limit: int,
        fresh_share_percent: int,
        daily_run_count: int,
        domains: Sequence[tuple[str, int, int | None]] | None = None,
    ) -> dict:
        """Persist a new budget and append the change to the audit ledger.

        ``domains`` is optional and written in the same transaction as the
        global values: a partially applied split would let one domain spend
        against a ceiling the other no longer agrees with.
        """
        budget = MapBudget(
            daily_limit=daily_map_limit,
            fresh_share_percent=fresh_share_percent,
            daily_run_count=daily_run_count,
        )
        shares = list(domains or ())
        if len({name for name, _, _ in shares}) != len(shares):
            raise ValueError("duplicate coverage_domain in budget split")
        if shares and sum(limit for _, limit, _ in shares) > daily_map_limit:
            raise ValueError("domain limits exceed the daily total")
        for name, limit, document_limit in shares:
            if not name.strip():
                raise ValueError("coverage_domain must not be blank")
            if not 0 <= limit <= 1000:
                raise ValueError("domain daily limit must be between 0 and 1000")
            if document_limit is not None:
                if not 0 <= document_limit <= limit:
                    raise ValueError(
                        "document map limit must be between 0 and domain limit"
                    )
                if name == "core" and document_limit != 0:
                    raise ValueError("core document map limit must be zero")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchrow(
                    """
                    SELECT daily_map_limit, fresh_share_percent, daily_run_count
                    FROM map_budget_settings
                    WHERE id = 1
                    FOR UPDATE
                    """
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO map_budget_settings
                        (id, daily_map_limit, fresh_share_percent,
                         daily_run_count, updated_at)
                    VALUES (1, $1, $2, $3, now())
                    ON CONFLICT (id) DO UPDATE SET
                        daily_map_limit = EXCLUDED.daily_map_limit,
                        fresh_share_percent = EXCLUDED.fresh_share_percent,
                        daily_run_count = EXCLUDED.daily_run_count,
                        updated_at = now()
                    RETURNING daily_map_limit, fresh_share_percent,
                              daily_run_count, updated_at
                    """,
                    budget.daily_limit,
                    budget.fresh_share_percent,
                    budget.daily_run_count,
                )
                await conn.execute(
                    """
                    INSERT INTO map_budget_changes
                        (previous_limit, previous_share, previous_run_count,
                         daily_map_limit, fresh_share_percent, daily_run_count)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    int(previous["daily_map_limit"]) if previous else None,
                    int(previous["fresh_share_percent"]) if previous else None,
                    int(previous["daily_run_count"]) if previous else None,
                    budget.daily_limit,
                    budget.fresh_share_percent,
                    budget.daily_run_count,
                )
                for name, limit, document_limit in shares:
                    await conn.execute(
                        """
                        INSERT INTO map_budget_domain_settings
                            (coverage_domain, daily_map_limit,
                             document_map_limit)
                        VALUES ($1, $2, COALESCE($3::integer, 0))
                        ON CONFLICT (coverage_domain) DO UPDATE SET
                            daily_map_limit = EXCLUDED.daily_map_limit,
                            document_map_limit = CASE
                                WHEN $3::integer IS NULL
                                THEN map_budget_domain_settings.document_map_limit
                                ELSE EXCLUDED.document_map_limit
                            END,
                            updated_at = now()
                        """,
                        name,
                        limit,
                        document_limit,
                    )
        return {
            "daily_map_limit": int(row["daily_map_limit"]),
            "fresh_share_percent": int(row["fresh_share_percent"]),
            "daily_run_count": int(row["daily_run_count"]),
            "updated_at": row["updated_at"],
            "source": "db",
        }

    async def domain_budget_status(
        self,
        *,
        editorial_day: date,
        consumed_by_domain: Mapping[str, Mapping[str, int]],
        fresh_share_percent: int,
        daily_run_count: int,
        now: datetime,
    ) -> list[dict]:
        """Per-domain limits, spend and gate state, plus what is being held.

        ``quarantined`` counts articles a domain has collected that carry no
        Map at all -- the DR-13208 state -- while ``held_maps`` counts Maps
        that exist but are withheld from readers by the DR-13209 gate. They are
        different problems and an operator needs to tell them apart.
        """
        rows = await self.pool.fetch(
            """
            SELECT d.coverage_domain,
                   d.daily_map_limit,
                   d.document_map_limit,
                   d.reader_exposed,
                   COALESCE(q.quarantined, 0) AS quarantined,
                   COALESCE(h.held_maps, 0)   AS held_maps,
                   COALESCE(s.article_consumed, 0) AS article_consumed,
                   COALESCE(s.document_consumed, 0) AS document_consumed
            FROM map_budget_domain_settings d
            LEFT JOIN (
                SELECT a.coverage_domain, count(*) AS quarantined
                FROM articles a
                LEFT JOIN map_queue mq ON mq.article_id = a.id
                LEFT JOIN article_maps m ON m.article_id = a.id
                WHERE mq.article_id IS NULL AND m.article_id IS NULL
                GROUP BY a.coverage_domain
            ) q ON q.coverage_domain = d.coverage_domain
            LEFT JOIN (
                SELECT a.coverage_domain, count(*) AS held_maps
                FROM articles a
                JOIN article_maps m ON m.article_id = a.id
                WHERE m.status IN ('held_ok', 'held_degraded')
                GROUP BY a.coverage_domain
            ) h ON h.coverage_domain = d.coverage_domain
            LEFT JOIN (
                SELECT c.coverage_domain,
                       count(*) FILTER (
                           WHERE a.document_type IS NULL
                       ) AS article_consumed,
                       count(*) FILTER (
                           WHERE a.document_type IS NOT NULL
                       ) AS document_consumed
                FROM map_budget_claims c
                JOIN articles a ON a.id = c.article_id
                WHERE c.editorial_day = $1
                  AND c.status IN ('reserved', 'committed')
                GROUP BY c.coverage_domain
            ) s ON s.coverage_domain = d.coverage_domain
            ORDER BY d.coverage_domain
            """,
            editorial_day,
        )
        status: list[dict] = []
        for row in rows:
            domain = str(row["coverage_domain"])
            limit = int(row["daily_map_limit"])
            configured_document_limit = int(row["document_map_limit"])
            document_limit = min(limit, configured_document_limit)
            article_limit = limit - document_limit
            article_consumed = int(row["article_consumed"])
            document_consumed = int(row["document_consumed"])
            spent = dict(consumed_by_domain.get(domain, {}))
            total = sum(spent.values())
            window = map_budget_window(
                MapBudget(
                    daily_limit=limit,
                    fresh_share_percent=fresh_share_percent,
                    daily_run_count=daily_run_count,
                ),
                now,
            )
            status.append(
                {
                    "coverage_domain": domain,
                    "daily_map_limit": limit,
                    "document_map_limit": configured_document_limit,
                    "article_map_limit": article_limit,
                    "reader_exposed": bool(row["reader_exposed"]),
                    "subtypes": {
                        "article": {
                            "consumed": article_consumed,
                            "remaining": max(
                                0, article_limit - article_consumed
                            ),
                        },
                        "document": {
                            "consumed": document_consumed,
                            "remaining": max(
                                0, document_limit - document_consumed
                            ),
                        },
                    },
                    "consumed": {
                        bucket: spent.get(bucket, 0)
                        for bucket in MAP_BUDGET_BUCKETS
                    },
                    "total_consumed": total,
                    "current_allowance": window.current_allowance,
                    "current_remaining": max(
                        0, window.current_allowance - total
                    ),
                    "total_remaining": max(0, limit - total),
                    "quarantined": int(row["quarantined"]),
                    "held_maps": int(row["held_maps"]),
                }
            )
        return status

    async def map_budget_status(
        self,
        *,
        default_limit: int,
        default_share: int,
        default_run_count: int,
    ) -> dict:
        """Budget, today's per-bucket spend and triage coverage for /ops."""
        settings = await self.map_budget_settings(
            default_limit=default_limit,
            default_share=default_share,
            default_run_count=default_run_count,
        )
        now = await self.pool.fetchval("SELECT clock_timestamp()")
        window = map_budget_window(
            MapBudget(
                daily_limit=settings["daily_map_limit"],
                fresh_share_percent=settings["fresh_share_percent"],
                daily_run_count=settings["daily_run_count"],
            ),
            now,
        )
        editorial_day = window.editorial_day
        rows = await self.pool.fetch(
            """
            SELECT coverage_domain, bucket, consumed
            FROM map_budget_ledger
            WHERE editorial_day = $1
            """,
            editorial_day,
        )
        consumed: dict[str, int] = {}
        by_domain: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = str(row["bucket"])
            amount = int(row["consumed"])
            consumed[bucket] = consumed.get(bucket, 0) + amount
            by_domain.setdefault(str(row["coverage_domain"]), {})[bucket] = amount
        fresh_quota, backlog_quota = bucket_quotas(
            settings["daily_map_limit"], settings["fresh_share_percent"]
        )
        quotas = {"fresh": fresh_quota, "backlog": backlog_quota}
        buckets = {
            bucket: {
                "quota": quotas[bucket],
                "consumed": consumed.get(bucket, 0),
                "remaining": max(0, quotas[bucket] - consumed.get(bucket, 0)),
            }
            for bucket in MAP_BUDGET_BUCKETS
        }
        total_consumed = sum(item["consumed"] for item in buckets.values())
        domains = await self.domain_budget_status(
            editorial_day=editorial_day,
            consumed_by_domain=by_domain,
            fresh_share_percent=settings["fresh_share_percent"],
            daily_run_count=settings["daily_run_count"],
            now=now,
        )
        return {
            **settings,
            "domains": domains,
            "editorial_day": editorial_day,
            "current_run": window.current_run,
            "current_run_allowance": window.current_run_allowance,
            "current_allowance": window.current_allowance,
            "current_remaining": max(
                0, window.current_allowance - total_consumed
            ),
            "next_run_at": window.next_run_at,
            "buckets": buckets,
            "total_consumed": total_consumed,
            "total_remaining": max(
                0, settings["daily_map_limit"] - total_consumed
            ),
            "triage": await self.triage_coverage(),
        }

    async def triage_pending_rows(
        self,
        *,
        limit: int,
        max_attempts: int,
        retry_after_s: float = 21600.0,
        coverage_domain: str | None = None,
    ) -> list[dict]:
        """Oldest candidates needing core score or crypto relevance decision.

        Core retains its original finite-attempt predicate. Crypto never gets a
        neutral terminal score: after max attempts it becomes eligible for one
        actual-provider retry per ``retry_after_s`` cooldown.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if retry_after_s <= 0:
            raise ValueError("retry_after_s must be positive")
        if coverage_domain is not None:
            coverage_domain = coverage_domain.strip().casefold()
            if not coverage_domain:
                raise ValueError("coverage_domain must not be blank")
        rows = await self.pool.fetch(
            """
            SELECT q.article_id, q.triage_attempts,
                   a.title, a.domain, a.url, a.source_query,
                   a.coverage_domain, a.document_type,
                   a.document_source_name
            FROM map_queue q
            JOIN articles a ON a.id = q.article_id
            WHERE q.status = 'queued'
              AND ($4::text IS NULL OR a.coverage_domain = $4::text)
              AND (
                    (
                        a.coverage_domain <> 'crypto'
                        AND q.triage_scored_at IS NULL
                        AND q.triage_attempts < $2
                    )
                    OR (
                        a.coverage_domain = 'crypto'
                        AND NOT COALESCE((
                            q.triage_score IS NOT NULL
                            AND q.triage_scored_at IS NOT NULL
                            AND q.triage_model IS NOT NULL
                            AND jsonb_typeof(q.triage_signals->'axes') = 'object'
                            AND q.triage_signals->>'fallback' = 'false'
                            AND q.triage_signals->>'profile' = 'crypto'
                            AND q.triage_signals->>'relevance'
                                  IN ('on_topic', 'off_topic')
                        ), FALSE)
                        AND (
                            q.triage_attempts < $2
                            OR q.updated_at <= now()
                                 - ($3::double precision * interval '1 second')
                        )
                    )
              )
            ORDER BY q.created_at ASC, q.article_id ASC
            LIMIT $1
            """,
            limit,
            max_attempts,
            retry_after_s,
            coverage_domain,
        )
        return [dict(row) for row in rows]

    async def duplicate_title_ids(
        self,
        *,
        article_ids: list[str],
        title_keys: list[str],
        window_hours: float,
    ) -> set[str]:
        """Candidates whose exact headline was already collected earlier.

        Syndicated copies share a headline; only the first arrival keeps the
        unpenalised score so one story does not spend the budget many times.
        """
        if len(article_ids) != len(title_keys):
            raise ValueError("article_ids and title_keys must align")
        if window_hours <= 0:
            raise ValueError("window_hours must be positive")
        if not article_ids:
            return set()
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT candidate.id
            FROM unnest($1::text[], $2::text[]) AS candidate(id, title_key)
            JOIN articles self_row ON self_row.id = candidate.id
            JOIN articles earlier
              ON lower(btrim(earlier.title)) = candidate.title_key
             AND earlier.id <> candidate.id
             AND earlier.first_seen_at < self_row.first_seen_at
             AND earlier.first_seen_at
                   >= now() - ($3::double precision * interval '1 hour')
            WHERE candidate.title_key <> ''
            """,
            article_ids,
            title_keys,
            window_hours,
        )
        return {str(row["id"]) for row in rows}

    async def save_triage_scores(
        self, results: list[dict], *, model: str
    ) -> int:
        """Persist scores for one batch; already-claimed rows are left alone."""
        if not results:
            return 0
        article_ids = [str(item["article_id"]) for item in results]
        scores = [int(item["score"]) for item in results]
        signals = [
            json.dumps(item.get("signals") or {}, ensure_ascii=False)
            for item in results
        ]
        if any(not 0 <= score <= 100 for score in scores):
            raise ValueError("triage score must be between 0 and 100")
        value = await self.pool.fetchval(
            """
            WITH scored AS (
                UPDATE map_queue q
                SET triage_score = incoming.score,
                    triage_signals = incoming.signals::jsonb,
                    triage_model = $4,
                    triage_scored_at = now(),
                    triage_attempts = q.triage_attempts + 1,
                    updated_at = now()
                FROM unnest($1::text[], $2::int[], $3::text[])
                    AS incoming(article_id, score, signals)
                WHERE q.article_id = incoming.article_id
                RETURNING q.article_id
            )
            SELECT count(*) FROM scored
            """,
            article_ids,
            scores,
            signals,
            model,
        )
        return int(value or 0)

    async def bump_triage_attempts(self, article_ids: list[str]) -> int:
        """Record one failed scoring attempt without assigning a score."""
        if not article_ids:
            return 0
        value = await self.pool.fetchval(
            """
            WITH bumped AS (
                UPDATE map_queue
                SET triage_attempts = triage_attempts + 1,
                    updated_at = now()
                WHERE article_id = ANY($1::text[])
                  AND (
                      triage_scored_at IS NULL
                      OR (
                          EXISTS (
                              SELECT 1 FROM articles a
                              WHERE a.id = map_queue.article_id
                                AND a.coverage_domain = 'crypto'
                          )
                          AND NOT COALESCE((
                              triage_score IS NOT NULL
                              AND triage_scored_at IS NOT NULL
                              AND triage_model IS NOT NULL
                              AND jsonb_typeof(triage_signals->'axes') = 'object'
                              AND triage_signals->>'fallback' = 'false'
                              AND triage_signals->>'profile' = 'crypto'
                              AND triage_signals->>'relevance'
                                    IN ('on_topic', 'off_topic')
                          ), FALSE)
                      )
                  )
                RETURNING article_id
            )
            SELECT count(*) FROM bumped
            """,
            article_ids,
        )
        return int(value or 0)

    async def purge_expired_candidates(
        self, *, ttl_days: int, limit: int
    ) -> dict:
        """Delete candidates never selected within the retention window.

        Only never-mapped, unreferenced articles qualify. Published, curated,
        saved and feedback-bearing articles are protected here and again by the
        restricting foreign keys behind them.
        """
        if ttl_days < 1:
            raise ValueError("ttl_days must be at least 1")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        value = await self.pool.fetchval(
            """
            WITH expired AS (
                SELECT a.id
                FROM articles a
                JOIN map_queue q ON q.article_id = a.id
                WHERE q.status = 'queued'
                  AND a.first_seen_at
                        < now() - ($1::double precision * interval '1 day')
                  AND NOT EXISTS (
                      SELECT 1 FROM article_maps m
                      WHERE m.article_id = a.id
                        AND m.status IN ('ok', 'degraded')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM editorial_publications p
                      WHERE p.article_id = a.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM article_curation_items i
                      WHERE i.article_id = a.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_article_feedback f
                      WHERE f.article_id = a.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app_account_reading_states s
                      WHERE s.last_article_id = a.id
                         OR s.saved_articles ? a.id
                  )
                ORDER BY a.first_seen_at ASC
                LIMIT $2
            ), removed AS (
                DELETE FROM articles
                WHERE id IN (SELECT id FROM expired)
                RETURNING id
            )
            SELECT count(*) FROM removed
            """,
            float(ttl_days),
            limit,
        )
        return {"deleted": int(value or 0), "limit": limit}

    async def triage_coverage(self) -> dict:
        """How much of the queued backlog currently carries a triage score."""
        row = await self.pool.fetchrow(
            """
            SELECT count(*) FILTER (WHERE triage_scored_at IS NOT NULL) AS scored,
                   count(*) FILTER (WHERE triage_scored_at IS NULL) AS pending
            FROM map_queue
            WHERE status = 'queued'
            """
        )
        if row is None:
            return {"scored": 0, "pending": 0}
        return {"scored": int(row["scored"]), "pending": int(row["pending"])}

    async def list_article_embedding_changes(
        self,
        *,
        changed_at: datetime | None,
        article_id: str,
        limit: int,
    ) -> list[dict]:
        """Return publication-aware Map changes in stable keyset order."""
        if changed_at is not None and (
            changed_at.tzinfo is None or changed_at.utcoffset() is None
        ):
            raise ValueError("article embedding cursor must be timezone-aware")
        if not 1 <= limit <= 50:
            raise ValueError("article embedding change limit is out of range")

        rows = await self.pool.fetch(
            """
            WITH changes AS (
                SELECT map.article_id,
                       map.status AS map_status,
                       map.data AS map_data,
                       (
                           map.status IN ('ok', 'degraded')
                           AND map.data IS NOT NULL
                           AND (
                               publication.article_id IS NULL
                               OR publication.state = 'published'
                           )
                       ) AS eligible,
                       GREATEST(map.updated_at, COALESCE(publication.updated_at,
                                                        map.updated_at)) AS changed_at
                FROM article_maps AS map
                LEFT JOIN editorial_publications AS publication
                    ON publication.article_id = map.article_id
            )
            SELECT article_id, map_status, map_data, eligible, changed_at
            FROM changes
            WHERE $1::timestamptz IS NULL
               OR (changed_at, article_id) > ($1::timestamptz, $2)
            ORDER BY changed_at, article_id
            LIMIT $3
            """,
            changed_at,
            article_id,
            limit,
        )
        changes: list[dict] = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("map_data"), str):
                try:
                    item["map_data"] = json.loads(item["map_data"])
                except json.JSONDecodeError:
                    # PostgreSQL JSONB normally makes this impossible, but a
                    # custom codec or damaged fixture must become a bounded
                    # worker dead-letter instead of pinning the cursor.
                    pass
            item["eligible"] = bool(item.get("eligible"))
            changes.append(item)
        return changes

    # ---- aggregate-only operations analytics -------------------------
    async def ops_analytics(
        self,
        *,
        daily_days: int,
        monthly_months: int,
    ) -> dict:
        """Return bounded KST member/activity/content aggregates for /ops."""
        if daily_days not in {30, 90, 365}:
            raise ValueError("daily_days must be one of 30, 90, 365")
        if monthly_months not in {6, 12}:
            raise ValueError("monthly_months must be one of 6, 12")

        anchor = await self.pool.fetchrow(
            """
            SELECT /* ops:analytics-anchor */
                   now() AS generated_at,
                   coverage.started_on AS session_activity_started_on,
                   CASE WHEN coverage.started_on IS NULL THEN NULL
                        ELSE (now() AT TIME ZONE 'Asia/Seoul')::date
                   END AS session_activity_through_on,
                   (
                       (SELECT count(*) FROM app_accounts)
                       + (SELECT count(DISTINCT installation_hash)
                          FROM app_guest_session_activity_days)
                   )::bigint AS total_members,
                   CASE WHEN coverage.started_on IS NULL THEN NULL ELSE (
                       SELECT count(*)
                       FROM app_account_session_activity_days day
                       WHERE day.active_date=(
                           now() AT TIME ZONE 'Asia/Seoul'
                       )::date
                   ) + (
                       SELECT count(*)
                       FROM app_guest_session_activity_days day
                       WHERE day.active_date=(
                           now() AT TIME ZONE 'Asia/Seoul'
                       )::date
                   ) END::bigint AS today_dau,
                   CASE WHEN coverage.started_on IS NULL THEN NULL ELSE (
                       SELECT count(DISTINCT day.account_id)
                       FROM app_account_session_activity_days day
                       WHERE day.active_date >= date_trunc(
                           'month', now() AT TIME ZONE 'Asia/Seoul'
                       )::date
                         AND day.active_date <= (
                             now() AT TIME ZONE 'Asia/Seoul'
                         )::date
                   ) + (
                       SELECT count(DISTINCT day.installation_hash)
                       FROM app_guest_session_activity_days day
                       WHERE day.active_date >= date_trunc(
                           'month', now() AT TIME ZONE 'Asia/Seoul'
                       )::date
                         AND day.active_date <= (
                             now() AT TIME ZONE 'Asia/Seoul'
                         )::date
                   ) END::bigint AS current_month_mau,
                   COALESCE((SELECT SUM(view_count)
                             FROM article_view_counts),0)::bigint
                       AS total_article_views
            FROM (
                SELECT min(active_date) AS started_on
                FROM (
                    SELECT active_date FROM app_account_session_activity_days
                    UNION ALL
                    SELECT active_date FROM app_guest_session_activity_days
                ) activity
            ) coverage
            """
        )
        if anchor is None:
            raise RuntimeError("analytics anchor unavailable")
        generated_at = anchor["generated_at"]

        user_rows = await self.pool.fetch(
            """
            WITH bounds AS (
                SELECT ($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                           AS today,
                       (($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                           - ($2::integer - 1))::date AS started_on
            ),
            days AS (
                SELECT generate_series(bounds.started_on, bounds.today,
                                       interval '1 day')::date AS day
                FROM bounds
            ),
            registrations AS (
                SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS day,
                       count(*)::bigint AS count
                FROM app_accounts
                GROUP BY 1
            ),
            baseline AS (
                SELECT count(*)::bigint AS count
                FROM app_accounts, bounds
                WHERE (created_at AT TIME ZONE 'Asia/Seoul')::date
                      < bounds.started_on
            )
            SELECT /* ops:analytics-user-trend */
                   days.day AS date,
                   COALESCE(registrations.count,0)::bigint AS new_members,
                   (baseline.count + SUM(COALESCE(registrations.count,0))
                       OVER (ORDER BY days.day))::bigint AS cumulative_members
            FROM days CROSS JOIN baseline
            LEFT JOIN registrations ON registrations.day=days.day
            ORDER BY days.day
            """,
            generated_at,
            daily_days,
        )
        dau_rows = await self.pool.fetch(
            """
            WITH bounds AS (
                SELECT ($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                           AS today,
                       (($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                           - ($2::integer - 1))::date AS requested_start,
                       min(active_date) AS coverage_start
                FROM (
                    SELECT active_date FROM app_account_session_activity_days
                    UNION ALL
                    SELECT active_date FROM app_guest_session_activity_days
                ) activity
            ),
            days AS (
                SELECT generate_series(
                    GREATEST(bounds.requested_start, bounds.coverage_start),
                    bounds.today, interval '1 day'
                )::date AS day
                FROM bounds WHERE bounds.coverage_start IS NOT NULL
            ),
            counts AS (
                SELECT active_date AS day, count(*)::bigint AS count
                FROM (
                    SELECT active_date, ('account:' || account_id::text) AS unit
                    FROM app_account_session_activity_days
                    UNION ALL
                    SELECT active_date, ('guest:' || encode(installation_hash, 'hex'))
                        AS unit
                    FROM app_guest_session_activity_days
                ) activity, bounds
                WHERE active_date >= GREATEST(bounds.requested_start,
                                               bounds.coverage_start)
                  AND active_date <= bounds.today
                GROUP BY active_date
            )
            SELECT /* ops:analytics-dau */ days.day AS date,
                   COALESCE(counts.count,0)::bigint AS active_members
            FROM days LEFT JOIN counts ON counts.day=days.day
            ORDER BY days.day
            """,
            generated_at,
            daily_days,
        )
        mau_rows = await self.pool.fetch(
            """
            WITH bounds AS (
                SELECT ($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                           AS today,
                       date_trunc(
                           'month', $1::timestamptz AT TIME ZONE 'Asia/Seoul'
                       )::date AS current_month,
                       min(active_date) AS coverage_start
                FROM (
                    SELECT active_date FROM app_account_session_activity_days
                    UNION ALL
                    SELECT active_date FROM app_guest_session_activity_days
                ) activity
            ),
            months AS (
                SELECT generate_series(
                    GREATEST(
                        bounds.current_month
                            - (($2::integer - 1) * interval '1 month'),
                        date_trunc('month', bounds.coverage_start)::date
                    ), bounds.current_month, interval '1 month'
                )::date AS month
                FROM bounds WHERE bounds.coverage_start IS NOT NULL
            )
            SELECT /* ops:analytics-mau */ months.month,
                   count(DISTINCT activity.unit)::bigint
                       AS active_members,
                   (months.month=(SELECT current_month FROM bounds)
                    OR (months.month=date_trunc(
                            'month', (SELECT coverage_start FROM bounds)
                        )::date
                        AND (SELECT coverage_start FROM bounds) > months.month))
                       AS is_partial
            FROM months
            LEFT JOIN (
                SELECT active_date, ('account:' || account_id::text) AS unit
                FROM app_account_session_activity_days
                UNION ALL
                SELECT active_date, ('guest:' || encode(installation_hash, 'hex'))
                FROM app_guest_session_activity_days
            ) activity
              ON activity.active_date >= months.month
             AND activity.active_date < months.month + interval '1 month'
            GROUP BY months.month
            ORDER BY months.month
            """,
            generated_at,
            monthly_months,
        )
        activity = await self.pool.fetchrow(
            """
            WITH bounds AS (
                SELECT ($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                           AS period_end,
                       GREATEST(
                           (($1::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                               - ($2::integer - 1))::date,
                           min(active_date)
                       ) AS period_start,
                       min(active_date) AS coverage_start
                FROM app_account_session_activity_days
            ),
            active AS (
                SELECT count(DISTINCT day.account_id)::bigint AS count
                FROM app_account_session_activity_days day, bounds
                WHERE bounds.coverage_start IS NOT NULL
                  AND day.active_date BETWEEN bounds.period_start
                                          AND bounds.period_end
            ),
            events AS (
                SELECT event_type, count(*)::bigint AS count
                FROM app_account_activity_events event, bounds
                WHERE bounds.coverage_start IS NOT NULL
                  AND (event.occurred_at AT TIME ZONE 'Asia/Seoul')::date
                      BETWEEN bounds.period_start AND bounds.period_end
                GROUP BY event_type
            )
            SELECT /* ops:analytics-activity */ bounds.period_start,
                   bounds.period_end, active.count AS active_members,
                   COALESCE((SELECT count FROM events
                             WHERE event_type='search'),0)::bigint
                       AS search_count,
                   COALESCE((SELECT count FROM events
                             WHERE event_type='article_opened'),0)::bigint
                       AS article_opened_count,
                   COALESCE((SELECT count FROM events
                             WHERE event_type='scene_viewed'),0)::bigint
                       AS scene_viewed_count,
                   COALESCE((SELECT count FROM events
                             WHERE event_type='article_completed'),0)::bigint
                       AS article_completed_count
            FROM bounds CROSS JOIN active
            """,
            generated_at,
            daily_days,
        )
        if activity is None:
            raise RuntimeError("analytics activity unavailable")
        popular_rows = await self.pool.fetch(
            """
            SELECT /* ops:analytics-popular */
                   row_number() OVER (
                       ORDER BY counts.view_count DESC,
                                COALESCE(article.seendate,
                                         article.first_seen_at) DESC,
                                article.id DESC
                   )::integer AS rank,
                   article.id AS article_id,
                   COALESCE(NULLIF(map.data->>'headline',''),
                            NULLIF(article.title,''), '제목 없음') AS headline,
                   article.domain,
                   COALESCE(article.seendate, article.first_seen_at)
                       AS published_at,
                   counts.view_count
            FROM article_view_counts counts
            JOIN articles article ON article.id=counts.article_id
            JOIN article_maps map ON map.article_id=article.id
            WHERE counts.view_count > 0
              AND map.status IN ('ok','degraded')
              AND map.data IS NOT NULL
              AND jsonb_typeof(map.data)='object'
              AND (
                  NOT EXISTS (SELECT 1 FROM editorial_publications publication
                              WHERE publication.article_id=article.id)
                  OR EXISTS (SELECT 1 FROM editorial_publications publication
                             WHERE publication.article_id=article.id
                               AND publication.state='published')
              )
            ORDER BY counts.view_count DESC,
                     COALESCE(article.seendate, article.first_seen_at) DESC,
                     article.id DESC
            LIMIT 100
            """
        )
        view_rate_rows = await self.pool.fetch(
            """
            WITH eligible AS (
                SELECT /* ops:analytics-view-rate */
                       impression.article_id,
                       impression.impression_count,
                       impression.first_impressed_at,
                       COALESCE(NULLIF(map.data->>'headline',''),
                                NULLIF(article.title,''), '제목 없음')
                           AS headline,
                       article.domain,
                       COALESCE(article.seendate, article.first_seen_at)
                           AS published_at
                FROM article_feed_impression_counts impression
                JOIN articles article ON article.id=impression.article_id
                JOIN article_maps map ON map.article_id=article.id
                WHERE impression.impression_count >= 5
                  AND map.status IN ('ok','degraded')
                  AND map.data IS NOT NULL
                  AND jsonb_typeof(map.data)='object'
                  AND (
                      NOT EXISTS (
                          SELECT 1 FROM editorial_publications publication
                          WHERE publication.article_id=article.id
                      )
                      OR EXISTS (
                          SELECT 1 FROM editorial_publications publication
                          WHERE publication.article_id=article.id
                            AND publication.state='published'
                      )
                  )
            ),
            rates AS (
                SELECT eligible.article_id,
                       eligible.headline,
                       eligible.domain,
                       eligible.published_at,
                       eligible.impression_count,
                       eligible.first_impressed_at,
                       count(event.id)::bigint AS view_count,
                       count(event.id)::numeric
                           / eligible.impression_count::numeric
                           AS view_rate_ratio,
                       round(
                           count(event.id)::numeric * 100
                               / eligible.impression_count::numeric,
                           1
                       ) AS view_rate_percent
                FROM eligible
                LEFT JOIN article_view_events event
                  ON event.article_id=eligible.article_id
                 AND event.received_at >= eligible.first_impressed_at
                GROUP BY eligible.article_id,
                         eligible.headline,
                         eligible.domain,
                         eligible.published_at,
                         eligible.impression_count,
                         eligible.first_impressed_at
            ),
            high AS (
                SELECT 'high'::text AS direction,
                       row_number() OVER (
                           ORDER BY view_rate_ratio DESC,
                                    impression_count DESC,
                                    view_count DESC,
                                    published_at DESC NULLS LAST,
                                    article_id DESC
                       )::integer AS rank,
                       article_id, headline, domain, published_at,
                       view_count, impression_count, view_rate_percent,
                       first_impressed_at
                FROM rates
                ORDER BY view_rate_ratio DESC,
                         impression_count DESC,
                         view_count DESC,
                         published_at DESC NULLS LAST,
                         article_id DESC
                LIMIT 100
            ),
            low AS (
                SELECT 'low'::text AS direction,
                       row_number() OVER (
                           ORDER BY view_rate_ratio ASC,
                                    impression_count DESC,
                                    view_count DESC,
                                    published_at DESC NULLS LAST,
                                    article_id DESC
                       )::integer AS rank,
                       article_id, headline, domain, published_at,
                       view_count, impression_count, view_rate_percent,
                       first_impressed_at
                FROM rates
                ORDER BY view_rate_ratio ASC,
                         impression_count DESC,
                         view_count DESC,
                         published_at DESC NULLS LAST,
                         article_id DESC
                LIMIT 100
            )
            SELECT * FROM high
            UNION ALL
            SELECT * FROM low
            ORDER BY direction, rank
            """
        )

        anchor_row = dict(anchor)
        today_dau = anchor_row.get("today_dau")
        current_mau = anchor_row.get("current_month_mau")
        ratio = None
        if current_mau is not None and int(current_mau) > 0 and today_dau is not None:
            ratio = round(int(today_dau) * 100 / int(current_mau), 1)
        activity_row = dict(activity)
        type_keys = (
            ("search", "search_count"),
            ("article_opened", "article_opened_count"),
            ("scene_viewed", "scene_viewed_count"),
            ("article_completed", "article_completed_count"),
        )
        type_counts = [
            (name, int(activity_row.get(key) or 0)) for name, key in type_keys
        ]
        total_events = sum(count for _name, count in type_counts)
        active_members = int(activity_row.get("active_members") or 0)

        return {
            "generated_at": generated_at,
            "timezone": "Asia/Seoul",
            "coverage": {
                "session_activity_started_on": anchor_row.get(
                    "session_activity_started_on"
                ),
                "session_activity_through_on": anchor_row.get(
                    "session_activity_through_on"
                ),
            },
            "summary": {
                "total_members": int(anchor_row.get("total_members") or 0),
                "today_dau": int(today_dau) if today_dau is not None else None,
                "current_month_mau": (
                    int(current_mau) if current_mau is not None else None
                ),
                "dau_mau_ratio": ratio,
                "total_article_views": int(
                    anchor_row.get("total_article_views") or 0
                ),
            },
            "user_trend": [
                {
                    "date": row["date"],
                    "new_members": int(row["new_members"]),
                    "cumulative_members": int(row["cumulative_members"]),
                }
                for row in user_rows
            ],
            "dau_trend": [
                {"date": row["date"], "active_members": int(row["active_members"])}
                for row in dau_rows
            ],
            "mau_trend": [
                {
                    "month": row["month"],
                    "active_members": int(row["active_members"]),
                    "is_partial": bool(row["is_partial"]),
                }
                for row in mau_rows
            ],
            "activity": {
                "period_start": activity_row.get("period_start"),
                "period_end": activity_row.get("period_end"),
                "active_members": active_members,
                "total_events": total_events,
                "events_per_active_member": (
                    round(total_events / active_members, 2)
                    if active_members > 0 else None
                ),
                "event_types": [
                    {
                        "event_type": name,
                        "count": count,
                        "share": (
                            round(count * 100 / total_events, 1)
                            if total_events > 0 else 0.0
                        ),
                    }
                    for name, count in type_counts
                ],
            },
            "popular_articles": [
                {
                    "rank": int(row["rank"]),
                    "article_id": str(row["article_id"]),
                    "headline": str(row["headline"]),
                    "domain": row["domain"],
                    "published_at": row["published_at"],
                    "view_count": int(row["view_count"]),
                }
                for row in popular_rows
            ],
            "high_view_rate_articles": [
                {
                    "rank": int(row["rank"]),
                    "article_id": str(row["article_id"]),
                    "headline": str(row["headline"]),
                    "domain": row["domain"],
                    "published_at": row["published_at"],
                    "view_count": int(row["view_count"]),
                    "impression_count": int(row["impression_count"]),
                    "view_rate_percent": float(row["view_rate_percent"]),
                    "first_impressed_at": row["first_impressed_at"],
                }
                for row in view_rate_rows
                if row["direction"] == "high"
            ],
            "low_view_rate_articles": [
                {
                    "rank": int(row["rank"]),
                    "article_id": str(row["article_id"]),
                    "headline": str(row["headline"]),
                    "domain": row["domain"],
                    "published_at": row["published_at"],
                    "view_count": int(row["view_count"]),
                    "impression_count": int(row["impression_count"]),
                    "view_rate_percent": float(row["view_rate_percent"]),
                    "first_impressed_at": row["first_impressed_at"],
                }
                for row in view_rate_rows
                if row["direction"] == "low"
            ],
        }

    async def available_article_count(self) -> int:
        """Count Maps that are currently eligible for the app feed."""
        value = await self.pool.fetchval(
            """
            SELECT count(*)
            FROM articles a
            JOIN article_maps m ON m.article_id = a.id
            LEFT JOIN editorial_publications p ON p.article_id = a.id
            WHERE m.status IN ('ok', 'degraded')
              AND m.data IS NOT NULL
              AND (p.draft_id IS NULL OR p.state = 'published')
            """
        )
        return int(value or 0)

    async def queue_stats(self) -> dict:
        rows = await self.pool.fetch(
            "SELECT status, count(*) AS n FROM map_queue GROUP BY status"
        )
        stats = {r["status"]: r["n"] for r in rows}
        due = await self.pool.fetchval(
            "SELECT count(*) FROM map_queue WHERE status='queued' AND next_attempt_at <= now()"
        )
        stats["due_now"] = due or 0
        return stats

    async def queue_items(
        self, limit: int = 50, status: str | None = None
    ) -> list[dict]:
        rows = await self.pool.fetch(
            """
            SELECT q.*, a.title, a.domain
            FROM map_queue q
            JOIN articles a ON a.id = q.article_id
            WHERE ($2::text IS NULL OR q.status = $2)
            ORDER BY q.updated_at DESC
            LIMIT $1
            """,
            limit,
            status,
        )
        return [dict(r) for r in rows]

    # ---- worker telemetry ---------------------------------------------
    async def worker_instance_start(
        self, instance_id: str, capacities: dict[str, int]
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO worker_instances
                (instance_id, capacities, started_at, last_seen_at, stopped_at)
            VALUES ($1, $2::jsonb, now(), now(), NULL)
            ON CONFLICT (instance_id) DO UPDATE SET
                capacities = EXCLUDED.capacities,
                last_seen_at = now(),
                stopped_at = NULL
            """,
            instance_id,
            json.dumps(capacities),
        )

    async def worker_instance_heartbeat(self, instance_id: str) -> None:
        await self.pool.execute(
            """
            UPDATE worker_instances
            SET last_seen_at=now()
            WHERE instance_id=$1 AND stopped_at IS NULL
            """,
            instance_id,
        )

    async def worker_instance_stop(self, instance_id: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE worker_activity
                    SET finished_at=now(), outcome=COALESCE(outcome, 'interrupted')
                    WHERE instance_id=$1 AND finished_at IS NULL
                    """,
                    instance_id,
                )
                await conn.execute(
                    """
                    UPDATE worker_instances
                    SET last_seen_at=now(), stopped_at=now()
                    WHERE instance_id=$1
                    """,
                    instance_id,
                )

    async def worker_activity_start(
        self, instance_id: str, worker_kind: str, work_key: str | None
    ) -> int:
        return await self.pool.fetchval(
            """
            INSERT INTO worker_activity (instance_id, worker_kind, work_key)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            instance_id,
            worker_kind,
            work_key,
        )

    async def worker_activity_finish(self, activity_id: int, outcome: str) -> None:
        await self.pool.execute(
            """
            UPDATE worker_activity
            SET finished_at=now(), outcome=$2
            WHERE id=$1 AND finished_at IS NULL
            """,
            activity_id,
            outcome,
        )

    async def worker_telemetry_cleanup(self, retention_hours: int) -> None:
        interval = timedelta(hours=max(1, retention_hours))
        await self.pool.execute(
            """
            DELETE FROM worker_activity
            WHERE COALESCE(finished_at, started_at) < now() - $1::interval
            """,
            interval,
        )
        await self.pool.execute(
            """
            DELETE FROM worker_instances wi
            WHERE COALESCE(wi.stopped_at, wi.last_seen_at) < now() - $1::interval
              AND NOT EXISTS (
                  SELECT 1 FROM worker_activity wa WHERE wa.instance_id=wi.instance_id
              )
            """,
            interval,
        )

    async def worker_telemetry_rows(self, window_minutes: int) -> dict:
        now = await self.pool.fetchval("SELECT now()")
        window_start = now - timedelta(minutes=window_minutes)
        instances = await self.pool.fetch(
            """
            SELECT instance_id, capacities, started_at, last_seen_at, stopped_at
            FROM worker_instances
            WHERE started_at <= $1
              AND COALESCE(stopped_at, last_seen_at) >= $2
            """,
            now,
            window_start,
        )
        activities = await self.pool.fetch(
            """
            SELECT wa.id, wa.instance_id, wa.worker_kind, wa.work_key,
                   wa.started_at, wa.finished_at, wa.outcome
            FROM worker_activity wa
            JOIN worker_instances wi ON wi.instance_id=wa.instance_id
            WHERE wa.started_at <= $1
              AND COALESCE(wa.finished_at, wi.stopped_at, wi.last_seen_at) >= $2
            """,
            now,
            window_start,
        )
        return {
            "now": now,
            "instances": [dict(row) for row in instances],
            "activities": [dict(row) for row in activities],
        }

    # ---- run audit ------------------------------------------------------
    async def start_run(self, job_name: str, query: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO load_runs (job_name, query) VALUES ($1,$2) RETURNING id",
            job_name,
            query,
        )

    async def start_bounded_load_run(
        self,
        job_name: str,
        query: str,
        *,
        provider_prefix: str,
        max_runs: int,
        window_hours: float = 24.0,
    ) -> Optional[int]:
        """Atomically reserve one provider run inside a rolling time budget."""
        if (
            not provider_prefix
            or "%" in provider_prefix
            or "_" in provider_prefix
            or not job_name.startswith(provider_prefix)
        ):
            raise ValueError("job_name must start with a literal provider_prefix")
        if max_runs < 1:
            raise ValueError("max_runs must be positive")
        if window_hours <= 0:
            raise ValueError("window_hours must be positive")

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"source-api-budget:{provider_prefix}",
                )
                recent = await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM load_runs
                    WHERE job_name LIKE $1
                      AND started_at > now()
                          - ($2::double precision * interval '1 hour')
                    """,
                    f"{provider_prefix}%",
                    float(window_hours),
                )
                if int(recent or 0) >= max_runs:
                    return None
                return await connection.fetchval(
                    """
                    INSERT INTO load_runs (job_name, query)
                    VALUES ($1,$2)
                    RETURNING id
                    """,
                    job_name,
                    query,
                )

    async def finish_run(
        self,
        run_id: int,
        *,
        fetched: int,
        inserted: int,
        updated: int,
        images_stored: int,
        ok: bool = True,
        error: Optional[str] = None,
        mapped: int = 0,
        maps_failed: int = 0,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE load_runs SET
                finished_at = now(), fetched=$2, inserted=$3, updated=$4,
                images_stored=$5, ok=$6, error=$7, mapped=$8, maps_failed=$9
            WHERE id=$1
            """,
            run_id,
            fetched,
            inserted,
            updated,
            images_stored,
            ok,
            error,
            mapped,
            maps_failed,
        )

    # ---- reads (for the web API) ---------------------------------------
    async def recent_articles(
        self, limit: int = 50, with_image_only: bool = False
    ) -> list[dict]:
        where = "WHERE image_id IS NOT NULL" if with_image_only else ""
        rows = await self.pool.fetch(
            f"""
            SELECT id, url, title, seendate, domain, language, sourcecountry,
                   socialimage_url, image_id, source_query, first_seen_at
            FROM articles {where}
            ORDER BY seendate DESC NULLS LAST, first_seen_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_image_record(self, image_id: str) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(
            "SELECT id, source_url, content_type, byte_size, backend, storage_ref, "
            "data, downloaded_at "
            "FROM article_images WHERE id = $1",
            image_id,
        )

    async def get_image_bytes(self, image_id: str) -> Optional[asyncpg.Record]:
        """Backward-compatible alias for older local collaborators."""
        return await self.get_image_record(image_id)

    async def list_images_after(
        self,
        *,
        downloaded_at: datetime | None,
        image_id: str,
        limit: int,
    ) -> list[dict]:
        """Discover image metadata in a stable keyset order for RAG indexing."""
        bounded = max(0, min(int(limit), 50))
        if bounded == 0:
            return []
        rows = await self.pool.fetch(
            """
            SELECT id, source_url, content_type, byte_size, backend, storage_ref,
                   downloaded_at
            FROM article_images
            WHERE $1::timestamptz IS NULL
               OR downloaded_at > $1
               OR (downloaded_at = $1 AND id > $2)
            ORDER BY downloaded_at, id
            LIMIT $3
            """,
            downloaded_at,
            image_id,
            bounded,
        )
        return [dict(row) for row in rows]

    async def rag_candidate_records(
        self,
        image_ids: list[str],
        *,
        exclude_article_id: str,
    ) -> list[dict]:
        """Revalidate vector hits against main records and a non-current origin."""
        ordered_ids = list(dict.fromkeys(str(value) for value in image_ids if value))
        if not ordered_ids:
            return []
        rows = await self.pool.fetch(
            """
            SELECT i.id AS image_id, i.source_url, i.content_type, i.byte_size,
                   i.backend, i.storage_ref, i.data, i.downloaded_at,
                   origin.article_id AS origin_article_id,
                   origin.title AS origin_title,
                   origin.url AS origin_url,
                   origin.domain AS origin_domain,
                   origin.seendate AS origin_seen_at
            FROM article_images AS i
            JOIN LATERAL (
                SELECT a.id AS article_id, a.title, a.url, a.domain, a.seendate
                FROM article_image_links AS l
                JOIN articles AS a ON a.id = l.article_id
                WHERE l.image_id = i.id AND a.id <> $2
                ORDER BY a.seendate DESC NULLS LAST, a.first_seen_at DESC, a.id
                LIMIT 1
            ) AS origin ON true
            WHERE i.id = ANY($1::text[])
              AND NOT EXISTS (
                SELECT 1 FROM article_image_links AS current_link
                WHERE current_link.image_id = i.id
                  AND current_link.article_id = $2
              )
            ORDER BY array_position($1::text[], i.id)
            """,
            ordered_ids,
            exclude_article_id,
        )
        return [dict(row) for row in rows]

    async def counts(self) -> dict:
        row = await self.pool.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM articles)                       AS articles,
                (SELECT count(*) FROM articles WHERE image_id IS NOT NULL) AS articles_with_image,
                (SELECT count(*) FROM article_images)                 AS images
            """
        )
        return dict(row) if row else {}

    async def last_runs(self, limit: int = 10) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM load_runs ORDER BY started_at DESC LIMIT $1", limit
        )
        return [dict(r) for r in rows]


def _json_object(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: object) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return list(value) if isinstance(value, list) else []


def _decode_trending_search_snapshot(value: object) -> TrendingSearchSnapshot:
    row = dict(value)
    raw_categories = _json_object(row.get("category_keywords"))
    category_keywords: dict[TrendingMetric, tuple[str, ...]] = {}
    for raw_metric, raw_keywords in raw_categories.items():
        metric = TrendingMetric(str(raw_metric))
        category_keywords[metric] = tuple(_json_list(raw_keywords))
    return TrendingSearchSnapshot(
        bucket_started_at=row["bucket_started_at"],
        category_keywords=category_keywords,
        keyword_pool=tuple(_json_list(row.get("keyword_pool"))),
        suggestions=tuple(_json_list(row.get("suggestions"))),
        generated_at=row["generated_at"],
    )


def repaired_timeline_nodes(nodes: Sequence[dict]) -> Optional[list[dict]]:
    """Return readable timeline nodes with contiguous display order, or None.

    Timeline editions are immutable, but the strict public article gate they are
    read through is not: a re-mapped or withdrawn article silently removes its
    node long after publication.  Dropping the orphaned branches and renumbering
    what is left keeps a timeline usable; a timeline that no longer satisfies the
    published 4..8 node and three-main-node contract is dropped by the caller.
    """
    surviving: list[dict] = []
    seen_nodes: set[object] = set()
    main_ids: set[object] = set()
    for node in nodes:
        node_id = node.get("node_id")
        lane = node.get("lane")
        if node_id is None or node_id in seen_nodes or lane not in {"main", "branch"}:
            continue
        seen_nodes.add(node_id)
        if lane == "main":
            if node.get("anchor_node_id") is not None:
                continue
            main_ids.add(node_id)
        surviving.append(node)
    kept = [
        node
        for node in surviving
        if node.get("lane") == "main" or node.get("anchor_node_id") in main_ids
    ]
    if len(main_ids) < 3 or not 4 <= len(kept) <= 8:
        return None
    return [
        {**node, "display_order": order} for order, node in enumerate(kept, start=1)
    ]


def _curation_rows(values: Iterable[object]) -> list[dict]:
    rows: list[dict] = []
    for value in values:
        item = dict(value)
        item.pop("haystack", None)
        if isinstance(item.get("data"), str):
            try:
                item["data"] = json.loads(item["data"])
            except (TypeError, ValueError):
                item["data"] = None
        rows.append(item)
    return rows


def _decode_recommendation_row(value: object) -> dict:
    row = dict(value)
    weights = row.get("term_weights")
    if isinstance(weights, str):
        try:
            weights = json.loads(weights)
        except json.JSONDecodeError:
            weights = {}
    row["term_weights"] = dict(weights) if isinstance(weights, dict) else {}
    row["terms"] = list(row.get("terms") or [])
    row["candidate_sources"] = list(row.get("candidate_sources") or [])
    return row


def _normalized_reader_settings(value: object) -> dict:
    source = _json_object(value)
    normalized = dict(DEFAULT_ACCOUNT_READER_SETTINGS)
    if source.get("type_size") in {"small", "standard", "large"}:
        normalized["type_size"] = source["type_size"]
    if source.get("leading") in {"compact", "comfortable"}:
        normalized["leading"] = source["leading"]
    if source.get("theme") in {"system", "light", "dark"}:
        normalized["theme"] = source["theme"]
    for key in ("motion_enabled", "media_saver", "offline_cache"):
        if isinstance(source.get(key), bool):
            normalized[key] = source[key]
    return normalized


def _valid_progress_value(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    scene_index = value.get("scene_index")
    updated_at = value.get("updated_at")
    return (
        isinstance(scene_index, int)
        and not isinstance(scene_index, bool)
        and 0 <= scene_index <= 999
        and isinstance(updated_at, str)
        and bool(updated_at)
    )


def _normalized_push_copy(value: object, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_push_error(value: str) -> str:
    bounded = str(value)[:64]
    if bounded and all(character.isalnum() or character in "_-" for character in bounded):
        return bounded
    return "delivery_unavailable"
