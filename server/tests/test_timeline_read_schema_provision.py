from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from scripts import provision_timeline_read_schema as provision


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transaction_entries = 0

    @asynccontextmanager
    async def transaction(self):
        self.transaction_entries += 1
        yield

    async def execute(self, statement: str, *_args):
        self.statements.append(statement)


def report(*, pristine: bool, ready: bool):
    return provision.TimelineReadSchemaReport(
        relations=(),
        indexes=(),
        columns_exact=ready,
        pristine=pristine,
        ready=ready,
    )


def test_public_timeline_source_ddl_is_minimal_and_non_destructive():
    sql = "\n".join(provision.DDL).upper()

    assert sql.count("CREATE TABLE PUBLIC.EXPLORE_") == 3
    assert sql.count("CREATE INDEX IDX_EXPLORE_") == 5
    assert sql.count("CREATE UNIQUE INDEX IDX_EXPLORE_") == 1
    assert "UPDATE " not in sql
    assert "DELETE FROM" not in sql
    assert "TRUNCATE " not in sql
    assert "DROP " not in sql
    assert "ALTER TABLE" not in sql


@pytest.mark.asyncio
async def test_public_timeline_source_pristine_plan_and_exact_apply(monkeypatch):
    conn = FakeConnection()
    inspections = iter((report(pristine=True, ready=False), report(pristine=False, ready=True)))

    async def fake_inspect(_conn):
        return next(inspections)

    monkeypatch.setattr(provision, "inspect", fake_inspect)
    result = await provision.apply(conn, confirm=provision.CONFIRMATION)

    assert result.ready is True
    assert result.applied is True
    assert conn.transaction_entries == 1
    assert conn.statements[0] == "SELECT pg_advisory_xact_lock($1)"
    assert conn.statements[1:] == list(provision.DDL)


@pytest.mark.asyncio
async def test_public_timeline_source_partial_conflict_runs_no_ddl(monkeypatch):
    conn = FakeConnection()

    async def fake_inspect(_conn):
        return report(pristine=False, ready=False)

    monkeypatch.setattr(provision, "inspect", fake_inspect)
    with pytest.raises(provision.TimelineReadSchemaError) as caught:
        await provision.apply(conn, confirm=provision.CONFIRMATION)

    assert caught.value.code == "public_timeline_source_conflict"
    assert conn.statements == ["SELECT pg_advisory_xact_lock($1)"]


@pytest.mark.asyncio
async def test_public_timeline_source_confirmation_fails_before_transaction():
    conn = FakeConnection()

    with pytest.raises(provision.TimelineReadSchemaError) as caught:
        await provision.apply(conn, confirm="wrong")

    assert caught.value.code == "confirmation_mismatch"
    assert conn.transaction_entries == 0
    assert conn.statements == []
