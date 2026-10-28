"""SQL and concurrency contracts for App Attest challenge state."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.repository import Repository


NOW = datetime(2026, 8, 8, 14, 10, tzinfo=UTC)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ChallengeConnection:
    def __init__(self):
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return Transaction()

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        assert "INSERT INTO app_attest_challenges" in normalized
        return {
            "id": args[0],
            "installation_id": args[1],
            "key_id": args[2],
            "purpose": args[3],
            "challenge": args[4],
            "expires_at": NOW + timedelta(seconds=args[5]),
            "used_at": None,
            "created_at": NOW,
        }

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("execute", normalized, args))
        return "DELETE 0"


class AssertionConnection:
    def __init__(self, *, current_counter: int, new_counter: int):
        self.current_counter = current_counter
        self.new_counter = new_counter
        self.challenge_id = uuid4()
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return Transaction()

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "FROM app_attest_challenges" in normalized:
            return {
                "id": self.challenge_id,
                "used_at": None,
                "expires_at": NOW + timedelta(seconds=120),
                "purpose": "assertion",
            }
        if "FROM app_attest_keys" in normalized:
            return {"key_id": "key", "status": "active", "sign_count": self.current_counter}
        raise AssertionError(normalized)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return NOW

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("execute", normalized, args))
        return "UPDATE 1"


class RotationConnection:
    def __init__(self, *, proof_matches: bool):
        self.proof_matches = proof_matches
        self.challenge_id = uuid4()
        self.account_id = uuid4()
        self.session_id = uuid4()
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return Transaction()

    async def fetchrow(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "FROM app_attest_challenges" in normalized:
            return {
                "id": self.challenge_id,
                "installation_id": "installation",
                "key_id": "new-key",
                "purpose": "attestation",
                "expires_at": NOW + timedelta(seconds=120),
                "used_at": None,
            }
        if "FROM app_attest_keys" in normalized and "WHERE key_id=$1" in normalized:
            return None
        if (
            "FROM app_attest_keys" in normalized
            and "installation_id=$1" in normalized
            and "status='active'" in normalized
        ):
            return {
                "key_id": "old-key",
                "installation_id": "installation",
                "status": "active",
            }
        if (
            "FROM app_push_registrations" in normalized
            and "JOIN app_account_sessions" in normalized
        ):
            if not self.proof_matches:
                return None
            return {
                "id": uuid4(),
                "account_id": self.account_id,
                "session_id": self.session_id,
                "installation_id": "installation",
                "key_id": "old-key",
            }
        raise AssertionError(normalized)

    async def fetchval(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("fetchval", normalized, args))
        return NOW

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append(("execute", normalized, args))
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return "SELECT 1"
        if normalized.startswith("UPDATE app_attest_keys SET status='revoked'"):
            return "UPDATE 1"
        if normalized.startswith("INSERT INTO app_attest_keys"):
            return "INSERT 0 1"
        if normalized.startswith("UPDATE app_privacy_consents"):
            return "UPDATE 1"
        if normalized.startswith("UPDATE app_push_registrations"):
            return "UPDATE 1"
        if normalized.startswith("UPDATE app_request_cooldowns"):
            return "UPDATE 4"
        if normalized.startswith("UPDATE app_attest_challenges SET used_at"):
            return "UPDATE 1"
        raise AssertionError(normalized)


class ContextPool:
    def __init__(self):
        self.query = ""

    async def fetchrow(self, query, *args):
        self.query = " ".join(query.split())
        return None


class DB:
    def __init__(self, pool):
        self.pool = pool


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


@pytest.mark.asyncio
async def test_issue_app_challenge_does_not_invalidate_outstanding_rows():
    connection = ChallengeConnection()
    repository = Repository(DB(Pool(connection)))

    await repository.issue_app_challenge(
        installation_id="installation",
        key_id="key",
        purpose="assertion",
        ttl_s=120,
    )

    queries = [query for _, query, _ in connection.calls]
    assert sum("INSERT INTO app_attest_challenges" in query for query in queries) == 1
    assert not any(
        query.startswith("UPDATE app_attest_challenges SET used_at")
        for query in queries
    )


@pytest.mark.asyncio
async def test_commit_accepts_forward_counter_after_another_assertion_advanced_key():
    connection = AssertionConnection(current_counter=1, new_counter=2)
    repository = Repository(DB(Pool(connection)))

    committed = await repository.commit_verified_assertion(
        key_id="key",
        challenge_id=connection.challenge_id,
        expected_counter=0,
        new_counter=connection.new_counter,
    )

    assert committed is True
    assert sum(method == "execute" for method, _, _ in connection.calls) == 2


@pytest.mark.asyncio
async def test_commit_rejects_past_counter_after_newer_assertion_committed():
    connection = AssertionConnection(current_counter=2, new_counter=1)
    repository = Repository(DB(Pool(connection)))

    committed = await repository.commit_verified_assertion(
        key_id="key",
        challenge_id=connection.challenge_id,
        expected_counter=0,
        new_counter=connection.new_counter,
    )

    assert committed is False
    assert not any(method == "execute" for method, _, _ in connection.calls)


@pytest.mark.asyncio
async def test_assertion_context_starts_from_challenge_and_preserves_missing_key():
    pool = ContextPool()
    repository = Repository(DB(pool))

    await repository.get_app_attest_context(
        key_id="key",
        installation_id="installation",
        challenge_id=uuid4(),
    )

    assert "FROM app_attest_challenges c" in pool.query
    assert "LEFT JOIN app_attest_keys k" in pool.query


@pytest.mark.asyncio
async def test_register_rotates_only_with_exact_push_session_proof_and_rebinds_dependents():
    connection = RotationConnection(proof_matches=True)
    repository = Repository(DB(Pool(connection)))

    saved = await repository.register_app_attest_key(
        challenge_id=connection.challenge_id,
        installation_id="installation",
        key_id="new-key",
        public_key_pem=b"public-key",
        receipt=b"receipt",
        environment="production",
        device={"installation_id": "installation"},
        validation_category=3,
        bundle_version="2",
        recovery_account_id=connection.account_id,
        recovery_session_id=connection.session_id,
    )

    assert saved is True
    queries = [query for _, query, _ in connection.calls]
    assert queries[0].startswith("SELECT pg_advisory_xact_lock")
    assert any("JOIN app_account_sessions" in query for query in queries)
    assert any("session.expires_at >" in query for query in queries)
    assert any("UPDATE app_attest_keys SET status='revoked'" in query for query in queries)
    assert any("INSERT INTO app_attest_keys" in query for query in queries)
    assert any("UPDATE app_privacy_consents" in query for query in queries)
    assert any("UPDATE app_push_registrations" in query for query in queries)
    assert any("UPDATE app_request_cooldowns" in query for query in queries)
    assert not any(query.startswith("DELETE") for query in queries)


@pytest.mark.asyncio
async def test_register_recovery_proof_mismatch_has_no_key_or_dependent_mutation():
    connection = RotationConnection(proof_matches=False)
    repository = Repository(DB(Pool(connection)))

    saved = await repository.register_app_attest_key(
        challenge_id=connection.challenge_id,
        installation_id="installation",
        key_id="new-key",
        public_key_pem=b"public-key",
        receipt=b"receipt",
        environment="production",
        device={"installation_id": "installation"},
        validation_category=3,
        bundle_version="2",
        recovery_account_id=connection.account_id,
        recovery_session_id=connection.session_id,
    )

    assert saved is False
    mutating = [
        query
        for method, query, _ in connection.calls
        if method == "execute" and not query.startswith("SELECT")
    ]
    assert mutating == []
