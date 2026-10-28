"""Read-only production audit for Bucket-only image persistence."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from .config import get_settings
from .db import Database

_AUDIT_SQL = """
SELECT
    count(*) AS total_images,
    count(*) FILTER (WHERE backend = 's3') AS s3_images,
    count(*) FILTER (WHERE backend = 'db') AS db_images,
    count(*) FILTER (WHERE backend = 'local') AS local_images,
    count(*) FILTER (WHERE backend NOT IN ('s3', 'db', 'local')) AS other_images,
    count(*) FILTER (WHERE data IS NOT NULL) AS non_null_image_data,
    count(*) FILTER (
        WHERE backend = 's3'
          AND (storage_ref IS NULL OR btrim(storage_ref) = '')
    ) AS missing_s3_storage_ref,
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
FROM article_images
"""


@dataclass(frozen=True, slots=True)
class ImageStorageAuditReport:
    total_images: int
    s3_images: int
    db_images: int
    local_images: int
    other_images: int
    non_null_image_data: int
    missing_s3_storage_ref: int
    dangling_article_images: int
    dangling_image_links: int
    has_images: bool
    ok: bool


async def audit_image_storage(db: Database) -> ImageStorageAuditReport:
    if db.pool is None:
        raise RuntimeError("database is not connected")
    row = await db.pool.fetchrow(_AUDIT_SQL)
    if row is None:
        raise RuntimeError("image storage audit returned no row")
    values = {name: int(value) for name, value in dict(row).items()}
    total_images = values["total_images"]
    ok = (
        values["s3_images"] == total_images
        and values["db_images"] == 0
        and values["local_images"] == 0
        and values["other_images"] == 0
        and values["non_null_image_data"] == 0
        and values["missing_s3_storage_ref"] == 0
        and values["dangling_article_images"] == 0
        and values["dangling_image_links"] == 0
    )
    return ImageStorageAuditReport(
        **values,
        has_images=total_images > 0,
        ok=ok,
    )


def audit_passes(report: ImageStorageAuditReport, *, require_images: bool) -> bool:
    return report.ok and (report.has_images or not require_images)


async def _run(*, require_images: bool) -> int:
    db = Database(get_settings())
    try:
        await db.connect()
        report = await audit_image_storage(db)
    except Exception:
        print(json.dumps({"ok": False, "error": "image_storage_audit_failed"}))
        return 1
    finally:
        await db.close()
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if audit_passes(report, require_images=require_images) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-images", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(require_images=args.require_images))


if __name__ == "__main__":
    raise SystemExit(main())
