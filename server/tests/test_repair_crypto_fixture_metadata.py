from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SERVER_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repair_crypto_fixture_metadata",
    SERVER_ROOT / "scripts" / "repair_crypto_fixture_metadata.py",
)
repair = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        row = self.connection.row
        self.connection.snapshot = dict(row) if row is not None else None
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.connection.committed = True
        else:
            snapshot = self.connection.snapshot
            self.connection.row = dict(snapshot) if snapshot is not None else None
            self.connection.rolled_back = True
        return False


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


class FakeConnection:
    def __init__(self, row, *, command="UPDATE 1"):
        self.row = dict(row) if row is not None else None
        self.command = command
        self.snapshot = None
        self.fetch_query = ""
        self.fetch_args = ()
        self.execute_query = ""
        self.execute_args = ()
        self.committed = False
        self.rolled_back = False

    def transaction(self):
        return FakeTransaction(self)

    async def fetchrow(self, query, *args):
        self.fetch_query = query
        self.fetch_args = args
        if self.row is None or self.row["id"] != args[0]:
            return None
        return dict(self.row)

    async def execute(self, query, *args):
        self.execute_query = query
        self.execute_args = args
        replacement, article_id, current = args
        if self.command in {"UPDATE 1", "UPDATE 2"}:
            assert self.row is not None
            assert self.row["id"] == article_id
            assert self.row["title"] == current
            self.row["title"] = replacement
        return self.command


class FakeRepo:
    def __init__(self, connection):
        self.pool = FakePool(connection)


def _valid_case(*, command="UPDATE 1"):
    replacement = repair.load_frozen_replacement_title()
    current = replacement + ("x" * 13)
    row = {
        "id": repair.ARTICLE_ID,
        "title": current,
        "coverage_domain": "crypto",
        "document_type": None,
    }
    connection = FakeConnection(row, command=command)
    return FakeRepo(connection), connection, current, replacement


def test_fixed_repair_identity_and_fixture_are_locked():
    replacement = repair.load_frozen_replacement_title()

    assert repair.TARGET_DATABASE == "gyeol_crypto_staging"
    assert repair.SAMPLE_ID == "v1-a29"
    assert repair.ARTICLE_ID == (
        "1ec5f4c679045d41b69ccf56d6df228e80e127831ba1847b580b7cf80daec086"
    )
    assert repair.EXPECTED_CURRENT_SHA256 == (
        "e9abc1369abe2a2a2c94224a4197f96f469315e3530cef8027df5b86a3f38086"
    )
    assert repair.EXPECTED_REPLACEMENT_SHA256 == (
        "f3bbbd1d5f248f6e530254b081bde8989f045d5659cbb55ecf6ca0bb89d906ae"
    )
    assert len(replacement) == 240
    assert _sha256(replacement) == repair.EXPECTED_REPLACEMENT_SHA256
    assert repair.fixture_digest() == repair.EXPECTED_FIXTURE_SHA256


@pytest.mark.asyncio
async def test_plan_locks_and_validates_without_mutation():
    repo, connection, current, replacement = _valid_case()

    report = await repair.repair_fixture_title(
        repo,
        article_id=repair.ARTICLE_ID,
        expected_current_sha256=_sha256(current),
        replacement_title=replacement,
        expected_replacement_sha256=_sha256(replacement),
        apply=False,
    )

    assert "FOR UPDATE" in connection.fetch_query
    assert connection.fetch_args == (repair.ARTICLE_ID,)
    assert connection.execute_query == ""
    assert connection.row["title"] == current
    assert connection.committed is True
    assert report["rowcount"] == 0
    assert report["applied_at"] is None
    assert report["production_mutations"] == 0


@pytest.mark.asyncio
async def test_apply_uses_current_title_cas_and_returns_secret_free_receipt():
    repo, connection, current, replacement = _valid_case()

    report = await repair.repair_fixture_title(
        repo,
        article_id=repair.ARTICLE_ID,
        expected_current_sha256=_sha256(current),
        replacement_title=replacement,
        expected_replacement_sha256=_sha256(replacement),
        apply=True,
    )

    assert "WHERE id = $2" in connection.execute_query
    assert "title = $3" in connection.execute_query
    assert connection.execute_args == (replacement, repair.ARTICLE_ID, current)
    assert connection.row["title"] == replacement
    assert connection.committed is True
    assert report["rowcount"] == 1
    assert report["after_sha256"] == repair.EXPECTED_REPLACEMENT_SHA256
    assert report["applied_at"] is not None
    assert set(report) == {
        "database",
        "sample_id",
        "article_id",
        "field",
        "before_sha256",
        "before_length",
        "after_sha256",
        "after_length",
        "common_prefix",
        "rowcount",
        "fixture_digest",
        "production_mutations",
        "applied_at",
    }
    encoded = json.dumps(report)
    assert current not in encoded
    assert replacement not in encoded
    assert "postgresql://" not in encoded


@pytest.mark.asyncio
async def test_wrong_database_is_rejected_before_database_construction(
    monkeypatch,
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.example/railway")
    monkeypatch.delenv("DATABASE_PUBLIC_URL", raising=False)

    class ForbiddenDatabase:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("wrong database must fail before construction")

    monkeypatch.setattr(repair, "Database", ForbiddenDatabase)
    args = repair.parser().parse_args(["plan"])

    with pytest.raises(repair.RepairError, match="database_not_crypto_staging"):
        await repair.run(args)


@pytest.mark.asyncio
async def test_wrong_article_id_is_rejected_before_row_lock():
    repo, connection, current, replacement = _valid_case()

    with pytest.raises(repair.RepairError, match="article_id_mismatch"):
        await repair.repair_fixture_title(
            repo,
            article_id="wrong-id",
            expected_current_sha256=_sha256(current),
            replacement_title=replacement,
            expected_replacement_sha256=_sha256(replacement),
            apply=True,
        )

    assert connection.fetch_query == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hash_field", "error"),
    [
        ("current", "current_hash_mismatch"),
        ("replacement", "replacement_hash_mismatch"),
    ],
)
async def test_hash_mismatch_rolls_back(hash_field, error):
    repo, connection, current, replacement = _valid_case()
    current_hash = _sha256(current)
    replacement_hash = _sha256(replacement)
    if hash_field == "current":
        current_hash = "0" * 64
    else:
        replacement_hash = "0" * 64

    with pytest.raises(repair.RepairError, match=error):
        await repair.repair_fixture_title(
            repo,
            article_id=repair.ARTICLE_ID,
            expected_current_sha256=current_hash,
            replacement_title=replacement,
            expected_replacement_sha256=replacement_hash,
            apply=True,
        )

    assert connection.row["title"] == current
    assert connection.rolled_back is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"coverage_domain": "core"}, "coverage_domain_mismatch"),
        ({"document_type": "governance_proposal"}, "article_subtype_mismatch"),
        ({"title": None}, "current_title_invalid"),
        ({"short": True}, "current_length_mismatch"),
        ({"prefix": True}, "common_prefix_mismatch"),
    ],
)
async def test_row_shape_mismatch_rolls_back(mutation, error):
    repo, connection, current, replacement = _valid_case()
    if "short" in mutation:
        connection.row["title"] = replacement + ("x" * 12)
    elif "prefix" in mutation:
        connection.row["title"] = ("z" + replacement[1:]) + ("x" * 13)
    else:
        connection.row.update(mutation)
    actual = connection.row["title"]
    current_hash = _sha256(actual) if isinstance(actual, str) else "0" * 64

    with pytest.raises(repair.RepairError, match=error):
        await repair.repair_fixture_title(
            repo,
            article_id=repair.ARTICLE_ID,
            expected_current_sha256=current_hash,
            replacement_title=replacement,
            expected_replacement_sha256=_sha256(replacement),
            apply=True,
        )

    assert connection.rolled_back is True


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["UPDATE 0", "UPDATE 2"])
async def test_non_one_rowcount_rolls_back(command):
    repo, connection, current, replacement = _valid_case(command=command)

    with pytest.raises(repair.RepairError, match="update_rowcount_not_one"):
        await repair.repair_fixture_title(
            repo,
            article_id=repair.ARTICLE_ID,
            expected_current_sha256=_sha256(current),
            replacement_title=replacement,
            expected_replacement_sha256=_sha256(replacement),
            apply=True,
        )

    assert connection.row["title"] == current
    assert connection.rolled_back is True


def test_receipt_is_written_atomically(tmp_path):
    target = tmp_path / "nested" / "repair.json"
    payload = {
        "database": repair.TARGET_DATABASE,
        "rowcount": 1,
        "production_mutations": 0,
    }

    repair.write_receipt(target, payload)

    assert json.loads(target.read_text()) == payload
    assert not target.with_suffix(".json.tmp").exists()
