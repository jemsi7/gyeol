from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.repository import Repository


POLICY_VERSION = "1.5.0"
POLICY_URL = "https://example.invalid/privacy"


def _postgres_dsn() -> str:
    value = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not value:
        pytest.skip("TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    return value


async def _create_fixture(
    connection: asyncpg.Connection,
    schema: str,
    *,
    account_id: UUID,
    consent_id: UUID,
    key_id: str,
    key_status: str = "active",
) -> None:
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(
        f"""
        CREATE TABLE "{schema}".app_accounts (
            id UUID PRIMARY KEY
        );
        CREATE TABLE "{schema}".app_attest_keys (
            key_id TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE "{schema}".app_privacy_consents (
            id UUID PRIMARY KEY,
            installation_id TEXT NOT NULL,
            key_id TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            policy_url TEXT NOT NULL,
            accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE "{schema}".app_privacy_consent_accounts (
            consent_id UUID NOT NULL,
            account_id UUID NOT NULL,
            linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (consent_id, account_id)
        )
        """
    )
    installation_id = str(uuid4())
    await connection.execute(
        f'INSERT INTO "{schema}".app_accounts(id) VALUES($1)',
        account_id,
    )
    await connection.execute(
        f"""
        INSERT INTO "{schema}".app_attest_keys(
            key_id, installation_id, status
        ) VALUES($1, $2, $3)
        """,
        key_id,
        installation_id,
        key_status,
    )
    await connection.execute(
        f"""
        INSERT INTO "{schema}".app_privacy_consents(
            id, installation_id, key_id, policy_version, policy_url
        ) VALUES($1, $2, $3, $4, $5)
        """,
        consent_id,
        installation_id,
        key_id,
        POLICY_VERSION,
        POLICY_URL,
    )


async def _repository(
    dsn: str,
    schema: str,
) -> tuple[asyncpg.Connection, Repository]:
    connection = await asyncpg.connect(dsn)
    await connection.execute(f'SET search_path TO "{schema}"')
    return connection, Repository(SimpleNamespace(pool=connection))


@pytest.mark.asyncio
async def test_first_account_consent_link_returns_inserted_row_and_is_idempotent():
    dsn = _postgres_dsn()
    schema = f"test_consent_{uuid4().hex}"
    account_id = uuid4()
    consent_id = uuid4()
    setup = await asyncpg.connect(dsn)
    try:
        await _create_fixture(
            setup,
            schema,
            account_id=account_id,
            consent_id=consent_id,
            key_id="first-link-key",
        )
        connection, repository = await _repository(dsn, schema)
        try:
            first = await repository.link_account_privacy_consent(
                account_id,
                consent_id,
                POLICY_VERSION,
            )
            repeated = await repository.link_account_privacy_consent(
                account_id,
                consent_id,
                POLICY_VERSION,
            )
            link_count = await connection.fetchval(
                "SELECT count(*) FROM app_privacy_consent_accounts"
            )
        finally:
            await connection.close()

        assert first is not None
        assert repeated is not None
        assert first["consent_id"] == consent_id
        assert first["account_id"] == account_id
        assert first["linked_at"] == repeated["linked_at"]
        assert link_count == 1
    finally:
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()


@pytest.mark.asyncio
async def test_concurrent_account_consent_link_returns_one_stable_row():
    dsn = _postgres_dsn()
    schema = f"test_consent_{uuid4().hex}"
    account_id = uuid4()
    consent_id = uuid4()
    setup = await asyncpg.connect(dsn)
    first_connection: asyncpg.Connection | None = None
    second_connection: asyncpg.Connection | None = None
    try:
        await _create_fixture(
            setup,
            schema,
            account_id=account_id,
            consent_id=consent_id,
            key_id="concurrent-link-key",
        )
        first_connection, first_repository = await _repository(dsn, schema)
        second_connection, second_repository = await _repository(dsn, schema)
        first, second = await asyncio.gather(
            first_repository.link_account_privacy_consent(
                account_id,
                consent_id,
                POLICY_VERSION,
            ),
            second_repository.link_account_privacy_consent(
                account_id,
                consent_id,
                POLICY_VERSION,
            ),
        )
        link_count = await setup.fetchval(
            f'SELECT count(*) FROM "{schema}".app_privacy_consent_accounts'
        )

        assert first is not None
        assert second is not None
        assert first["linked_at"] == second["linked_at"]
        assert link_count == 1
    finally:
        if first_connection is not None:
            await first_connection.close()
        if second_connection is not None:
            await second_connection.close()
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_policy", "key_status"),
    [
        ("1.4.0", "active"),
        (POLICY_VERSION, "revoked"),
    ],
)
async def test_account_consent_link_rejects_stale_or_inactive(
    requested_policy: str,
    key_status: str,
):
    dsn = _postgres_dsn()
    schema = f"test_consent_{uuid4().hex}"
    account_id = uuid4()
    consent_id = uuid4()
    setup = await asyncpg.connect(dsn)
    try:
        await _create_fixture(
            setup,
            schema,
            account_id=account_id,
            consent_id=consent_id,
            key_id="ineligible-link-key",
            key_status=key_status,
        )
        connection, repository = await _repository(dsn, schema)
        try:
            result = await repository.link_account_privacy_consent(
                account_id,
                consent_id,
                requested_policy,
            )
            link_count = await connection.fetchval(
                "SELECT count(*) FROM app_privacy_consent_accounts"
            )
        finally:
            await connection.close()

        assert result is None
        assert link_count == 0
    finally:
        await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()
