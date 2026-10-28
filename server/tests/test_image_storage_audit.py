from __future__ import annotations

import pytest

from app.image_storage_audit import audit_image_storage, audit_passes


def _values(**overrides):
    values = {
        "total_images": 2,
        "s3_images": 2,
        "db_images": 0,
        "local_images": 0,
        "other_images": 0,
        "non_null_image_data": 0,
        "missing_s3_storage_ref": 0,
        "dangling_article_images": 0,
        "dangling_image_links": 0,
    }
    values.update(overrides)
    return values


class FakePool:
    def __init__(self, values):
        self.values = values
        self.query = ""

    async def fetchrow(self, query):
        self.query = query
        return self.values


class FakeDB:
    def __init__(self, values):
        self.pool = FakePool(values)


@pytest.mark.asyncio
async def test_audit_accepts_all_s3_metadata_only_images():
    db = FakeDB(_values())

    report = await audit_image_storage(db)

    assert report.ok is True
    assert report.has_images is True
    assert audit_passes(report, require_images=True) is True
    assert "data IS NOT NULL" in db.pool.query
    assert "storage_ref IS NULL" in db.pool.query


@pytest.mark.asyncio
async def test_audit_empty_database_passes_only_without_require_images():
    report = await audit_image_storage(
        FakeDB(_values(total_images=0, s3_images=0))
    )

    assert report.ok is True
    assert report.has_images is False
    assert audit_passes(report, require_images=False) is True
    assert audit_passes(report, require_images=True) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"non_null_image_data": 1},
        {"total_images": 2, "s3_images": 1, "db_images": 1},
        {"total_images": 2, "s3_images": 1, "local_images": 1},
        {"total_images": 2, "s3_images": 1, "other_images": 1},
        {"missing_s3_storage_ref": 1},
        {"dangling_article_images": 1},
        {"dangling_image_links": 1},
    ],
)
async def test_audit_fails_closed_on_storage_violation(overrides):
    report = await audit_image_storage(FakeDB(_values(**overrides)))

    assert report.ok is False
    assert audit_passes(report, require_images=False) is False
