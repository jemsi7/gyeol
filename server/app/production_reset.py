"""Guarded one-time reset for production database rows and image objects.

The two storage scopes deliberately run in separate Railway variable contexts:
``bucket`` under the server service and ``database`` under Postgres with its
public DATABASE_URL.  Operators must verify bucket zero before invoking the
database scope.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Literal
from uuid import UUID

from app.config import Settings, get_settings
from app.db import Database
from app.object_storage import S3ObjectStore

Action = Literal["plan", "execute", "verify"]
Scope = Literal["bucket", "database"]

EXPECTED_PROJECT = "devoted-commitment"
EXPECTED_ENVIRONMENT = "production"
CONFIRMATION = f"{EXPECTED_PROJECT}/{EXPECTED_ENVIRONMENT}"
RESET_TABLES: tuple[str, ...] = (
    "article_images",
    "articles",
    "article_image_links",
    "article_maps",
    "article_pipeline_artifacts",
    "app_attest_challenges",
    "app_attest_keys",
    "map_queue",
    "load_runs",
    "worker_instances",
    "worker_activity",
)
RESET_INDEXES: tuple[str, ...] = (
    "idx_articles_seendate",
    "idx_articles_domain",
    "idx_articles_country",
    "idx_articles_query",
    "idx_articles_image",
    "idx_link_article",
    "idx_link_image",
    "idx_maps_status",
    "idx_pipeline_artifact_status",
    "idx_attest_challenge_lookup",
    "idx_attest_one_active_installation",
    "idx_attest_key_installation",
    "idx_queue_due",
    "idx_queue_fifo",
    "idx_queue_lease",
    "idx_load_runs_started",
    "idx_worker_instances_seen",
    "idx_worker_activity_window",
    "idx_worker_activity_open",
)

_COUNT_SQL = "SELECT\n" + ",\n".join(
    f"    (SELECT count(*) FROM {table}) AS {table}" for table in RESET_TABLES
)
_TRUNCATE_SQL = (
    "TRUNCATE TABLE " + ", ".join(RESET_TABLES) + " RESTART IDENTITY CASCADE"
)
_INVARIANT_SQL = """
SELECT
    (SELECT count(*) FROM article_images WHERE data IS NOT NULL)
        AS non_null_image_data,
    (SELECT count(*)
       FROM articles a
       LEFT JOIN article_images i ON i.id = a.image_id
      WHERE a.image_id IS NOT NULL AND i.id IS NULL)
        AS dangling_article_images,
    (SELECT count(*)
       FROM article_image_links l
       LEFT JOIN articles a ON a.id = l.article_id
       LEFT JOIN article_images i ON i.id = l.image_id
      WHERE a.id IS NULL OR i.id IS NULL)
        AS dangling_image_links
"""


class ProductionResetError(RuntimeError):
    """A redacted failure code safe to print in an operations terminal."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BucketSnapshot:
    objects: int
    bytes: int
    bucket_sha256: str


@dataclass(frozen=True, slots=True)
class ResetReport:
    action: str
    scope: str
    database_counts: dict[str, int] | None
    bucket: BucketSnapshot | None
    schema_ok: bool | None
    indexes_ok: bool | None
    invariants_ok: bool | None
    deleted_items: int
    ok: bool


async def inspect_database(
    db: Database,
) -> tuple[dict[str, int], bool, bool]:
    if db.pool is None:
        raise ProductionResetError("database_not_connected")
    try:
        table_rows = await db.pool.fetch(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename
            """
        )
        index_rows = await db.pool.fetch(
            """
            SELECT indexname
            FROM pg_catalog.pg_indexes
            WHERE schemaname = 'public' AND indexname = ANY($1::text[])
            """,
            list(RESET_INDEXES),
        )
    except Exception:
        raise ProductionResetError("database_inspect_failed") from None

    present_tables = {str(row["tablename"]) for row in table_rows}
    present_indexes = {str(row["indexname"]) for row in index_rows}
    schema_ok = present_tables == set(RESET_TABLES)
    indexes_ok = set(RESET_INDEXES).issubset(present_indexes)
    if not schema_ok:
        return {}, False, indexes_ok

    try:
        row = await db.pool.fetchrow(_COUNT_SQL)
    except Exception:
        raise ProductionResetError("database_inspect_failed") from None
    if row is None:
        raise ProductionResetError("database_inspect_failed")
    return (
        {name: int(value) for name, value in dict(row).items()},
        True,
        indexes_ok,
    )


async def inspect_database_invariants(db: Database) -> bool:
    if db.pool is None:
        raise ProductionResetError("database_not_connected")
    try:
        row = await db.pool.fetchrow(_INVARIANT_SQL)
    except Exception:
        raise ProductionResetError("database_inspect_failed") from None
    if row is None:
        raise ProductionResetError("database_inspect_failed")
    values = {name: int(value) for name, value in dict(row).items()}
    return all(value == 0 for value in values.values())


def inspect_bucket(store: S3ObjectStore) -> BucketSnapshot:
    objects = 0
    byte_size = 0
    try:
        paginator = store.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=store.s.s3_bucket)
        for page in pages:
            for item in page.get("Contents", []):
                objects += 1
                byte_size += int(item.get("Size", 0))
    except Exception:
        raise ProductionResetError("bucket_inspect_failed") from None
    return BucketSnapshot(
        objects=objects,
        bytes=byte_size,
        bucket_sha256=hashlib.sha256(store.s.s3_bucket.encode("utf-8")).hexdigest(),
    )


def purge_bucket(store: S3ObjectStore, *, batch_size: int = 1000) -> int:
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    deleted = 0
    try:
        paginator = store.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=store.s.s3_bucket)
        for page in pages:
            contents = page.get("Contents", [])
            for start in range(0, len(contents), batch_size):
                chunk = contents[start : start + batch_size]
                objects = []
                for item in chunk:
                    key = item.get("Key")
                    if not isinstance(key, str) or not key:
                        raise ProductionResetError("bucket_listing_invalid")
                    objects.append({"Key": key})
                if not objects:
                    continue
                response = store.client.delete_objects(
                    Bucket=store.s.s3_bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                if response.get("Errors"):
                    raise ProductionResetError("bucket_delete_failed")
                deleted += len(objects)
    except ProductionResetError:
        raise
    except Exception:
        raise ProductionResetError("bucket_delete_failed") from None
    return deleted


async def truncate_database(db: Database) -> None:
    if db.pool is None:
        raise ProductionResetError("database_not_connected")
    try:
        await db.pool.execute(_TRUNCATE_SQL)
    except Exception:
        raise ProductionResetError("database_truncate_failed") from None


def _validate_common_target() -> None:
    if os.getenv("RAILWAY_PROJECT_NAME") != EXPECTED_PROJECT:
        raise ProductionResetError("target_project_mismatch")
    if os.getenv("RAILWAY_ENVIRONMENT_NAME") != EXPECTED_ENVIRONMENT:
        raise ProductionResetError("target_environment_mismatch")


def _validate_confirmation(action: Action, confirm: str | None) -> None:
    if action == "execute" and confirm != CONFIRMATION:
        raise ProductionResetError("confirmation_mismatch")


def _validate_freeze_deployment(value: str | None) -> None:
    try:
        parsed = UUID(value or "")
    except (TypeError, ValueError, AttributeError):
        raise ProductionResetError("freeze_deployment_required") from None
    if str(parsed) != value:
        raise ProductionResetError("freeze_deployment_required")


async def _run_bucket(
    action: Action,
    *,
    settings: Settings,
    confirm: str | None,
    bucket_sha256: str | None,
) -> ResetReport:
    if not settings.s3_bucket or settings.image_backend != "s3":
        raise ProductionResetError("target_bucket_mismatch")
    _validate_confirmation(action, confirm)
    if action == "execute" and settings.scheduler_enabled:
        raise ProductionResetError("scheduler_not_frozen")
    actual_bucket_sha256 = hashlib.sha256(
        settings.s3_bucket.encode("utf-8")
    ).hexdigest()
    if action == "execute" and bucket_sha256 != actual_bucket_sha256:
        raise ProductionResetError("bucket_fingerprint_mismatch")

    try:
        store = S3ObjectStore(settings)
    except Exception:
        raise ProductionResetError("bucket_configuration_invalid") from None

    deleted = 0
    snapshot = inspect_bucket(store)
    if action == "execute":
        deleted = purge_bucket(store)
        snapshot = inspect_bucket(store)
    ok = True if action == "plan" else snapshot.objects == 0
    return ResetReport(
        action=action,
        scope="bucket",
        database_counts=None,
        bucket=snapshot,
        schema_ok=None,
        indexes_ok=None,
        invariants_ok=None,
        deleted_items=deleted,
        ok=ok,
    )


async def _run_database(
    action: Action,
    *,
    settings: Settings,
    confirm: str | None,
    freeze_deployment: str | None,
) -> ResetReport:
    _validate_confirmation(action, confirm)
    if action == "execute":
        _validate_freeze_deployment(freeze_deployment)

    db = Database(settings)
    try:
        await db.connect()
        counts, schema_ok, indexes_ok = await inspect_database(db)
        invariants_ok = (
            await inspect_database_invariants(db) if schema_ok else False
        )
        if action == "execute":
            if not schema_ok or not indexes_ok:
                raise ProductionResetError("database_schema_drift")
            deleted = sum(counts.values())
            await truncate_database(db)
            counts, schema_ok, indexes_ok = await inspect_database(db)
            invariants_ok = await inspect_database_invariants(db)
            ok = (
                schema_ok
                and indexes_ok
                and invariants_ok
                and all(value == 0 for value in counts.values())
            )
        elif action == "verify":
            deleted = 0
            ok = (
                schema_ok
                and indexes_ok
                and invariants_ok
                and all(value == 0 for value in counts.values())
            )
        else:
            deleted = 0
            ok = schema_ok and indexes_ok and invariants_ok
    except ProductionResetError:
        raise
    except Exception:
        raise ProductionResetError("database_operation_failed") from None
    finally:
        try:
            await db.close()
        except Exception:
            pass

    return ResetReport(
        action=action,
        scope="database",
        database_counts=counts,
        bucket=None,
        schema_ok=schema_ok,
        indexes_ok=indexes_ok,
        invariants_ok=invariants_ok,
        deleted_items=deleted,
        ok=ok,
    )


async def run(
    action: Action,
    *,
    scope: Scope,
    confirm: str | None = None,
    freeze_deployment: str | None = None,
    bucket_sha256: str | None = None,
) -> ResetReport:
    _validate_common_target()
    settings = get_settings()
    if scope == "bucket":
        return await _run_bucket(
            action,
            settings=settings,
            confirm=confirm,
            bucket_sha256=bucket_sha256,
        )
    if scope == "database":
        return await _run_database(
            action,
            settings=settings,
            confirm=confirm,
            freeze_deployment=freeze_deployment,
        )
    raise ProductionResetError("scope_invalid")


async def _run_cli(args: argparse.Namespace) -> int:
    try:
        report = await run(
            args.action,
            scope=args.scope,
            confirm=args.confirm,
            freeze_deployment=args.freeze_deployment,
            bucket_sha256=args.bucket_sha256,
        )
    except ProductionResetError as exc:
        print(json.dumps({"ok": False, "error": exc.code}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "production_reset_failed"}))
        return 1
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute", "verify"))
    parser.add_argument("--scope", choices=("bucket", "database"), required=True)
    parser.add_argument("--confirm")
    parser.add_argument("--freeze-deployment")
    parser.add_argument("--bucket-sha256")
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
