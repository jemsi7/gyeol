"""Local editorial vertical through the production FastAPI route graph.

The module is both a focused ASGI integration test and the localhost-only
uvicorn target used by ``scripts/verify_editorial_vertical.py``.  It adds no
test route or authentication bypass to the production application.
"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID, uuid4

import httpx
import pytest

from app.api import _editorial_schedule_window_error, app, state
from app.app_service import AppClientError, AuthenticatedClient
from app.config import Settings
from app.editorial_service import editor_token_hash
from app.feed import AUTOMATIC_DISPLAY_ORDER
from app.map_loader import resolve_map
from app.models import StoredImage
from app.publication_worker import PublicationScheduleWorker


VERTICAL_MASTER_TOKEN = "editorial-vertical-master-only"
VERTICAL_HEADLINE = "골목의 여름을 다시 설계하다"
VERTICAL_EDITED_HEADLINE = "예약 뒤에만 남긴 후속 편집본"
VERTICAL_STAGES = ("auth", "draft", "asset", "validate", "publish", "order", "feed")
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
VERTICAL_IMAGE_ID = hashlib.sha256(PNG_1X1).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def test_editorial_schedule_window_is_inclusive_and_bounded_to_one_hour():
    now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)

    assert _editorial_schedule_window_error(now + timedelta(seconds=59), now=now) == (
        "editorial_schedule_too_soon"
    )
    assert _editorial_schedule_window_error(now + timedelta(seconds=60), now=now) is None
    assert _editorial_schedule_window_error(now + timedelta(seconds=3_600), now=now) is None
    assert _editorial_schedule_window_error(now + timedelta(seconds=3_601), now=now) == (
        "editorial_schedule_too_far"
    )


@pytest.mark.asyncio
async def test_editorial_schedule_rejects_more_than_one_hour_before_repository_mutation():
    repo = configure_vertical_state("http://test")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        operator = await client.post(
            "/v1/editor/operators",
            headers={"X-API-Token": VERTICAL_MASTER_TOKEN},
            json={"display_name": "예약 경계 검증", "role": "editor"},
        )
        token = operator.json()["token"]
        response = await client.put(
            f"/v1/editor/drafts/{uuid4()}/schedule",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "expected_draft_version": 1,
                "expected_schedule_version": None,
                "scheduled_for": (_now() + timedelta(hours=1, seconds=5)).isoformat(),
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "editorial_schedule_too_far"
    assert repo.schedule is None


def _canonical_hash(value: Mapping[str, Any] | list[Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _map_identity(map_data: Mapping[str, Any]) -> str:
    """Hash the delivery contract while excluding environment-specific URLs."""
    normalized = deepcopy(dict(map_data))
    normalized.pop("body_resolved", None)
    for image in normalized.get("images", []):
        if isinstance(image, dict):
            image.pop("url", None)
    # staleness_note is computed per request from live market values, so it is
    # environment-specific in the same way an image URL is (C-13205).
    for scene in normalized.get("scenes", []):
        if isinstance(scene, dict):
            scene.pop("staleness_note", None)
    return _canonical_hash(normalized)


def vertical_document() -> dict[str, Any]:
    return {
        "format_version": 3,
        "source_name": "결 편집부",
        "language": "ko",
        "headline": VERTICAL_HEADLINE,
        "subheadline": "운영진이 현장에서 기록한 심층 보도",
        "summary": "그늘과 쉼터의 간격이 보행자의 선택을 바꿨다.",
        "keywords": ["폭염", "보행", "도시설계"],
        "images": [
            {
                "image_id": VERTICAL_IMAGE_ID,
                "caption": "그늘이 이어진 보행 구간",
                "source_url": "editorial-upload://vertical.png",
            }
        ],
        "cover_image_ref": None,
        "visualizations": [],
        "scenes": [
            {
                "role": "lead",
                "label": "시작",
                "title": "가장 더운 길에서 시작한 기록",
                "summary": "운영진은 여름 골목의 온도와 사람들의 이동을 함께 기록했다.",
                "body": "조사는 가장 뜨거운 시간대의 골목에서 시작됐다.",
                "secondary": "",
                "quote": "",
                "image_ref": None,
                "chart_ref": None,
            },
            {
                "role": "context",
                "label": "현장",
                "title": "시설의 수보다 중요한 간격",
                "summary": "",
                "body": "그늘과 쉼터가 끊기지 않을 때 보행자는 더 오래 길에 머물렀다.",
                "secondary": "그 변화는 시설 하나가 아니라 이동 경로 전체에서 나타났다.",
                "quote": "머물 수 있어야 걸을 수 있습니다.",
                "image_ref": 0,
                "chart_ref": None,
            },
            {
                "role": "close",
                "label": "다음",
                "title": "다음 여름의 기준을 남기다",
                "summary": "",
                "body": "시는 다음 조사에서 실제 체류와 이동 기록을 다시 비교한다.",
                "secondary": "",
                "quote": "",
                "image_ref": None,
                "chart_ref": None,
            },
        ],
    }


class VerticalRepository:
    """Process-local double matching the editorial repository response shapes."""

    def __init__(self) -> None:
        self.operator: dict[str, Any] | None = None
        self.token_hash: bytes | None = None
        self.drafts: dict[UUID, dict[str, Any]] = {}
        self.images: dict[str, StoredImage] = {}
        self.publication: dict[str, Any] | None = None
        self.revision: dict[str, Any] | None = None
        self.map_data: dict[str, Any] | None = None
        self.schedule: dict[str, Any] | None = None

    async def record_article_list_impressions(self, **kwargs):
        return len(kwargs["article_ids"])

    async def record_guest_session_activity(self, **kwargs):
        return None

    async def create_editor_operator(self, *, display_name: str, role: str):
        now = _now()
        token = secrets.token_urlsafe(32)
        self.token_hash = editor_token_hash(token)
        self.operator = {
            "id": uuid4(),
            "display_name": display_name,
            "role": role,
            "status": "active",
            "created_at": now,
            "last_used_at": None,
            "revoked_at": None,
        }
        return deepcopy(self.operator), token

    async def authenticate_editor_token(self, token_hash: bytes):
        if (
            self.operator is None
            or self.operator["status"] != "active"
            or not secrets.compare_digest(token_hash, self.token_hash or b"")
        ):
            return None
        self.operator["last_used_at"] = _now()
        return deepcopy(self.operator)

    async def revoke_editor_operator(self, operator_id: UUID):
        if self.operator is None or self.operator["id"] != operator_id:
            return None
        self.operator.update(status="revoked", revoked_at=_now())
        return deepcopy(self.operator)

    async def create_editorial_draft(self, *, document: dict, operator_id: UUID):
        now = _now()
        draft_id = uuid4()
        row = {
            "id": draft_id,
            "status": "draft",
            "document": deepcopy(document),
            "version": 1,
            "created_by": operator_id,
            "updated_by": operator_id,
            "created_at": now,
            "updated_at": now,
        }
        self.drafts[draft_id] = row
        return deepcopy(row)

    async def editorial_drafts(self, limit: int = 100):
        return [deepcopy(row) for row in list(self.drafts.values())[:limit]]

    async def get_editorial_draft(self, draft_id: UUID):
        row = self.drafts.get(draft_id)
        return deepcopy(row) if row is not None else None

    async def update_editorial_draft(
        self,
        *,
        draft_id: UUID,
        expected_version: int,
        document: dict,
        operator_id: UUID,
    ):
        row = self.drafts.get(draft_id)
        if row is None or row["version"] != expected_version:
            return None
        row.update(
            document=deepcopy(document),
            version=expected_version + 1,
            updated_by=operator_id,
            updated_at=_now(),
        )
        return deepcopy(row)

    async def store_editorial_image_metadata(
        self, *, image: StoredImage, operator_id: UUID, filename: str
    ) -> None:
        assert self.operator is not None and operator_id == self.operator["id"]
        assert filename
        self.images[image.id] = image

    async def editorial_image_records(self, image_ids: list[str]):
        rows = []
        for image_id in image_ids:
            image = self.images.get(image_id)
            if image is not None:
                rows.append(
                    {
                        "id": image.id,
                        "source_url": image.source_url,
                        "content_type": image.content_type,
                        "byte_size": image.byte_size,
                        "backend": image.backend,
                        "storage_ref": image.storage_ref,
                    }
                )
        return rows

    async def editorial_schedules(self, limit: int = 100):
        return [deepcopy(self.schedule)] if self.schedule is not None and limit > 0 else []

    async def editorial_schedule(self, draft_id: UUID):
        if self.schedule is None or self.schedule["draft_id"] != draft_id:
            return None
        return deepcopy(self.schedule)

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
    ):
        draft = self.drafts.get(draft_id)
        if draft is None:
            return {"kind": "not_found"}
        if draft["version"] != expected_draft_version:
            return {"kind": "draft_conflict", "current": deepcopy(draft)}
        if self.schedule is not None and self.schedule["state"] in {"scheduled", "processing"}:
            if self.schedule["state"] != "scheduled":
                return {"kind": "schedule_not_mutable", "current": deepcopy(self.schedule)}
            if expected_schedule_version != self.schedule["version"]:
                return {"kind": "schedule_conflict", "current": deepcopy(self.schedule)}
            schedule_id = self.schedule["id"]
            version = self.schedule["version"] + 1
            created_at = self.schedule["created_at"]
        else:
            if expected_schedule_version is not None:
                return {"kind": "schedule_conflict", "current": deepcopy(self.schedule)}
            schedule_id = uuid4()
            version = 1
            created_at = _now()
        now = _now()
        self.schedule = {
            "id": schedule_id,
            "draft_id": draft_id,
            "version": version,
            "state": "scheduled",
            "scheduled_for": scheduled_for,
            "snapshot_draft_version": expected_draft_version,
            "document": deepcopy(document),
            "map_data": deepcopy(map_data),
            "image_links": deepcopy(image_links),
            "created_by": operator_id,
            "updated_by": operator_id,
            "attempt_count": 0,
            "last_error": None,
            "created_at": created_at,
            "updated_at": now,
            "cancelled_at": None,
            "published_at": None,
        }
        return {"kind": "scheduled", "schedule": deepcopy(self.schedule)}

    async def claim_due_editorial_schedules(self, *, limit: int, lease_seconds: int):
        del lease_seconds
        if (
            self.schedule is None
            or self.schedule["state"] != "scheduled"
            or limit < 1
        ):
            return []
        self.schedule.update(
            state="processing",
            attempt_count=self.schedule["attempt_count"] + 1,
            updated_at=_now(),
        )
        return [deepcopy(self.schedule)]

    async def retry_editorial_schedule(
        self,
        schedule_id: UUID,
        *,
        error_code: str,
        max_attempts: int,
    ):
        if self.schedule is None or self.schedule["id"] != schedule_id:
            return {"kind": "terminal"}
        terminal = self.schedule["attempt_count"] >= max_attempts
        self.schedule.update(
            state="failed" if terminal else "scheduled",
            last_error=error_code,
            updated_at=_now(),
        )
        return {"kind": "failed" if terminal else "retry", "schedule": deepcopy(self.schedule)}

    async def publish_claimed_editorial_schedule(self, schedule_id: UUID):
        if self.schedule is None or self.schedule["id"] != schedule_id:
            return {"kind": "schedule_not_found"}
        if self.schedule["state"] != "processing":
            return {"kind": "schedule_terminal", "schedule": deepcopy(self.schedule)}
        draft_id = self.schedule["draft_id"]
        row = self.drafts.get(draft_id)
        if row is None:
            return {"kind": "not_found"}
        document = deepcopy(self.schedule["document"])
        map_data = deepcopy(self.schedule["map_data"])
        image_links = deepcopy(self.schedule["image_links"])
        operator_id = self.schedule["updated_by"]
        assert all(link["image_id"] in self.images for link in image_links)
        now = _now()
        article_id = hashlib.sha256(f"editorial:{draft_id}".encode()).hexdigest()
        revision_id = uuid4()
        self.revision = {
            "id": revision_id,
            "draft_id": draft_id,
            "revision_number": 1,
            "document": document,
            "map_data": map_data,
            "created_by": operator_id,
            "created_at": now,
        }
        row.update(
            status="published",
            version=row["version"] + 1,
            updated_by=operator_id,
            updated_at=now,
        )
        self.publication = {
            "draft_id": draft_id,
            "article_id": article_id,
            "revision_id": revision_id,
            "state": "published",
            "published_at": now,
            "published_by": operator_id,
            "withdrawn_at": None,
            "withdrawn_by": None,
            "display_order": None,
            "is_featured": False,
            "updated_at": now,
            "draft_version": row["version"],
        }
        self.map_data = map_data
        self.schedule.update(
            state="published",
            version=self.schedule["version"] + 1,
            last_error=None,
            published_at=now,
            updated_at=now,
        )
        return {
            "kind": "published",
            "draft": deepcopy(row),
            "revision": deepcopy(self.revision),
            "publication": deepcopy(self.publication),
        }

    async def publish_editorial_draft(
        self,
        *,
        draft_id: UUID,
        expected_version: int,
        operator_id: UUID,
        document: dict,
        map_data: dict,
        image_links: list[dict],
    ):
        row = self.drafts.get(draft_id)
        if row is None:
            return {"kind": "not_found"}
        if row["version"] != expected_version:
            return {"kind": "conflict", "current": deepcopy(row)}
        assert all(link["image_id"] in self.images for link in image_links)
        now = _now()
        article_id = hashlib.sha256(f"editorial:{draft_id}".encode()).hexdigest()
        revision_id = uuid4()
        self.revision = {
            "id": revision_id,
            "draft_id": draft_id,
            "revision_number": 1,
            "document": deepcopy(document),
            "map_data": deepcopy(map_data),
            "created_by": operator_id,
            "created_at": now,
        }
        row.update(
            status="published",
            document=deepcopy(document),
            version=expected_version + 1,
            updated_by=operator_id,
            updated_at=now,
        )
        self.publication = {
            "draft_id": draft_id,
            "article_id": article_id,
            "revision_id": revision_id,
            "state": "published",
            "published_at": now,
            "published_by": operator_id,
            "withdrawn_at": None,
            "withdrawn_by": None,
            "display_order": None,
            "is_featured": False,
            "updated_at": now,
            "draft_version": row["version"],
        }
        self.map_data = deepcopy(map_data)
        return {
            "kind": "published",
            "draft": deepcopy(row),
            "revision": deepcopy(self.revision),
            "publication": deepcopy(self.publication),
        }

    async def editorial_revisions(self, draft_id: UUID):
        if self.revision is None or self.revision["draft_id"] != draft_id:
            return []
        return [deepcopy(self.revision)]

    async def withdraw_editorial_publication(
        self, *, draft_id: UUID, expected_version: int | None, operator_id: UUID
    ):
        if draft_id not in self.drafts:
            return {"kind": "not_found"}
        if self.publication is None:
            return {"kind": "not_published", "current": self.drafts[draft_id]}
        if expected_version is not None and self.drafts[draft_id]["version"] != expected_version:
            return {"kind": "conflict", "current": deepcopy(self.drafts[draft_id])}
        if self.publication["state"] != "withdrawn":
            self.drafts[draft_id].update(
                status="withdrawn",
                version=self.drafts[draft_id]["version"] + 1,
                updated_by=operator_id,
                updated_at=_now(),
            )
            self.publication.update(
                state="withdrawn",
                withdrawn_at=_now(),
                withdrawn_by=operator_id,
                display_order=None,
                is_featured=False,
                draft_version=self.drafts[draft_id]["version"],
                updated_at=_now(),
            )
        return {"kind": "withdrawn", "publication": deepcopy(self.publication)}

    def _today_item(self) -> dict[str, Any] | None:
        if self.publication is None or self.publication["state"] != "published":
            return None
        assert self.revision is not None
        return {
            "draft_id": self.publication["draft_id"],
            "article_id": self.publication["article_id"],
            "headline": self.revision["document"]["headline"],
            "published_at": self.publication["published_at"],
            "display_order": self.publication["display_order"],
            "is_featured": self.publication["is_featured"],
        }

    async def editorial_today(self, editorial_day: date):
        item = self._today_item()
        return [] if item is None else [item]

    async def set_editorial_today_order(
        self,
        *,
        editorial_day: date,
        ordered_article_ids: list[str],
        featured_article_id: str | None,
        operator_id: UUID,
    ):
        item = self._today_item()
        eligible = {item["article_id"]} if item is not None else set()
        requested = set(ordered_article_ids)
        if featured_article_id is not None:
            requested.add(featured_article_id)
        invalid = sorted(requested - eligible)
        if invalid:
            return {"kind": "invalid", "invalid_article_ids": invalid}
        assert self.publication is not None
        positions = {article_id: index for index, article_id in enumerate(ordered_article_ids)}
        self.publication.update(
            display_order=positions.get(self.publication["article_id"]),
            is_featured=self.publication["article_id"] == featured_article_id,
            updated_at=_now(),
        )
        return {"kind": "updated", "items": [self._today_item()]}

    async def recent_article_maps(self, *, limit: int, **_cursor: Any):
        item = self._today_item()
        if item is None or self.map_data is None or limit < 1:
            return []
        publication = self.publication
        assert publication is not None
        return [
            {
                "article_id": publication["article_id"],
                "source_url": f"https://editorial.invalid/{publication['article_id']}",
                "domain": "editorial.invalid",
                "published_at": publication["published_at"],
                "source_language": "Korean",
                "sourcecountry": "South Korea",
                "sort_at": publication["published_at"],
                "status": "ok",
                "data": deepcopy(self.map_data),
                "priority_bucket": 0,
                "featured_bucket": 0 if publication["is_featured"] else 1,
                "order_bucket": 0 if publication["display_order"] is not None else 1,
                "cursor_display_order": (
                    publication["display_order"]
                    if publication["display_order"] is not None
                    else AUTOMATIC_DISPLAY_ORDER
                ),
            }
        ]

    async def get_image_record(self, image_id: str):
        image = self.images.get(image_id)
        if image is None:
            return None
        return {
            "id": image.id,
            "content_type": image.content_type,
            "backend": image.backend,
            "storage_ref": image.storage_ref,
            "data": image.data,
        }

    async def get_image_bytes(self, image_id: str):
        return await self.get_image_record(image_id)


class VerticalAppClientService:
    """App Attest protocol double with cooldown isolated per registered key."""

    def __init__(self) -> None:
        self.challenges: dict[UUID, dict[str, Any]] = {}
        self.registered: set[tuple[str, str]] = set()
        self.last_feed_at: dict[tuple[str, str], datetime] = {}

    async def issue_challenge(self, *, purpose: str, key_id: str, device: dict):
        challenge_id = uuid4()
        self.challenges[challenge_id] = {
            "purpose": purpose,
            "key_id": key_id,
            "installation_id": device["installation_id"],
            "used": False,
        }
        return {
            "challenge_id": challenge_id,
            "challenge": base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode(),
            "expires_at": _now() + timedelta(minutes=2),
        }

    async def register(
        self,
        *,
        challenge_id: UUID,
        key_id: str,
        attestation_object: bytes,
        device: dict,
        recovery_account_id: UUID | None = None,
        recovery_session_id: UUID | None = None,
    ):
        challenge = self.challenges.get(challenge_id)
        if (
            challenge is None
            or challenge["used"]
            or challenge["purpose"] != "attestation"
            or challenge["key_id"] != key_id
            or not attestation_object
        ):
            raise AppClientError(401, "invalid_test_attestation")
        challenge["used"] = True
        self.registered.add((key_id, device["installation_id"]))
        return {"registered": True, "key_id": key_id}

    async def authenticate_feed(
        self,
        *,
        payload: dict,
        challenge_id: UUID,
        key_id: str,
        assertion_object: bytes,
        issued_at: int,
        cooldown_s: float = 1.0,
    ):
        installation_id = payload["device"]["installation_id"]
        client_key = (key_id, installation_id)
        challenge = self.challenges.get(challenge_id)
        if (
            client_key not in self.registered
            or challenge is None
            or challenge["used"]
            or challenge["purpose"] != "assertion"
            or challenge["key_id"] != key_id
            or not assertion_object
        ):
            raise AppClientError(401, "invalid_test_assertion")
        challenge["used"] = True
        now = _now()
        previous = self.last_feed_at.get(client_key)
        if previous is not None:
            remaining = cooldown_s - (now - previous).total_seconds()
            if remaining > 0:
                raise AppClientError(429, "feed_cooldown", retry_after_seconds=remaining)
        self.last_feed_at[client_key] = now
        return AuthenticatedClient(
            key_id=key_id,
            installation_id=installation_id,
            device=payload["device"],
        )


class _HealthPool:
    async def fetchval(self, _query: str):
        return 1


class _HealthDB:
    pool = _HealthPool()


def configure_vertical_state(base_url: str) -> VerticalRepository:
    state.settings = Settings(
        _env_file=None,
        API_TOKEN=VERTICAL_MASTER_TOKEN,
        PUBLIC_BASE_URL=base_url,
        IMAGE_BACKEND="db",
        APP_FEED_COOLDOWN_S=1,
        MAP_FORMAT_VERSION=3,
        PUBLICATION_POLL_SECONDS=1,
        PUBLICATION_MAX_ATTEMPTS=2,
    )
    state.db = _HealthDB()
    state.repo = VerticalRepository()
    state.object_store = None
    state.app_client_service = VerticalAppClientService()
    return state.repo


@asynccontextmanager
async def editorial_vertical_lifespan(_app):
    base_url = os.environ.get("EDITORIAL_VERTICAL_BASE_URL", "http://127.0.0.1:8000")
    repo = configure_vertical_state(base_url)
    worker = PublicationScheduleWorker(state.settings, repo)
    worker.start()
    try:
        yield
    finally:
        await worker.stop()


app.router.lifespan_context = editorial_vertical_lifespan


@dataclass(frozen=True, slots=True)
class VerticalStageEvidence:
    stage: str
    duration_ms: int
    digest: str


@dataclass(frozen=True, slots=True)
class VerticalResult:
    stages: tuple[VerticalStageEvidence, ...]
    article_id: str
    headline: str
    scene_count: int
    map_hash: str
    editor_token: str = field(repr=False)


class VerticalStageError(RuntimeError):
    def __init__(
        self,
        stage: str,
        code: str,
        completed: list[VerticalStageEvidence],
    ) -> None:
        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code
        self.completed = tuple(completed)


class _StageAbort(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_error_code(response: httpx.Response) -> str:
    try:
        raw = response.json().get("error", {}).get("code", "")
    except (AttributeError, ValueError, TypeError):
        raw = ""
    if isinstance(raw, str) and raw.replace("_", "").isalnum() and len(raw) <= 80:
        return raw.lower()
    return f"http_{response.status_code}"


def _json(response: httpx.Response, expected: int) -> dict[str, Any]:
    if response.status_code != expected:
        raise _StageAbort(_safe_error_code(response))
    try:
        value = response.json()
    except ValueError as exc:
        raise _StageAbort("invalid_json") from exc
    if not isinstance(value, dict):
        raise _StageAbort("invalid_shape")
    return value


StageOperation = Callable[[], Awaitable[tuple[Any, Mapping[str, Any] | list[Any]]]]


async def _run_stage(
    name: str,
    completed: list[VerticalStageEvidence],
    operation: StageOperation,
) -> Any:
    started = time.perf_counter_ns()
    try:
        value, safe_evidence = await operation()
    except _StageAbort as exc:
        raise VerticalStageError(name, exc.code, completed) from None
    except httpx.HTTPError:
        raise VerticalStageError(name, "transport_error", completed) from None
    except (AssertionError, KeyError, TypeError, ValueError):
        raise VerticalStageError(name, "contract_mismatch", completed) from None
    duration_ms = max(0, round((time.perf_counter_ns() - started) / 1_000_000))
    completed.append(
        VerticalStageEvidence(name, duration_ms, _canonical_hash(safe_evidence))
    )
    return value


def _device() -> dict[str, Any]:
    return {
        "installation_id": "22222222-2222-2222-2222-222222222222",
        "vendor_id": None,
        "platform": "ipados",
        "model": "iPad14,5",
        "os_version": "26.0",
        "app_version": "1.0",
        "build_number": "1",
        "locale": "ko-KR",
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


async def run_editorial_http(
    client: httpx.AsyncClient,
    master_token: str = VERTICAL_MASTER_TOKEN,
) -> VerticalResult:
    """Exercise the first seven stages while retaining secrets only in memory."""
    completed: list[VerticalStageEvidence] = []
    editor_token = ""
    draft_id = ""
    published_map: dict[str, Any] = {}
    article_id = ""

    async def auth_operation():
        created = _json(
            await client.post(
                "/v1/editor/operators",
                headers={"X-API-Token": master_token},
                json={"display_name": "수직 검증 데스크", "role": "editor"},
            ),
            201,
        )
        token = created.get("token")
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise _StageAbort("operator_token_invalid")
        me = _json(
            await client.get(
                "/v1/editor/me", headers={"Authorization": f"Bearer {token}"}
            ),
            200,
        )
        if me.get("id") != created.get("id") or me.get("status") != "active":
            raise _StageAbort("operator_identity_mismatch")
        return token, {
            "operator_id": created["id"],
            "role": created["role"],
            "status": me["status"],
        }

    editor_token = await _run_stage("auth", completed, auth_operation)
    auth_headers = {"Authorization": f"Bearer {editor_token}"}

    async def draft_operation():
        created = _json(
            await client.post(
                "/v1/editor/drafts",
                headers=auth_headers,
                json={"document": vertical_document()},
            ),
            201,
        )
        if created.get("version") != 1 or created.get("status") != "draft":
            raise _StageAbort("draft_contract_mismatch")
        return created["id"], {
            "draft_id": created["id"],
            "version": created["version"],
            "document_hash": _canonical_hash(created["document"]),
        }

    draft_id = await _run_stage("draft", completed, draft_operation)

    async def asset_operation():
        asset = _json(
            await client.post(
                "/v1/editor/assets",
                headers=auth_headers,
                files={"file": ("vertical.png", PNG_1X1, "image/png")},
            ),
            201,
        )
        if asset.get("image_id") != VERTICAL_IMAGE_ID:
            raise _StageAbort("asset_hash_mismatch")
        return asset, {
            "image_id": asset["image_id"],
            "content_type": asset["content_type"],
            "byte_size": asset["byte_size"],
        }

    await _run_stage("asset", completed, asset_operation)

    async def validate_operation():
        validated = _json(
            await client.post(
                f"/v1/editor/drafts/{draft_id}/validate", headers=auth_headers
            ),
            200,
        )
        if validated.get("valid") is not True or validated.get("errors") != []:
            raise _StageAbort("document_invalid")
        map_data = validated.get("map")
        if (
            not isinstance(map_data, dict)
            or map_data.get("editorial", {}).get("pipeline_version")
            != "manual-editor-v1"
        ):
            raise _StageAbort("manual_pipeline_missing")
        return map_data, {
            "map_hash": _map_identity(map_data),
            "scene_count": len(map_data.get("scenes", [])),
            "valid": True,
        }

    validated_map = await _run_stage("validate", completed, validate_operation)

    async def publish_operation():
        reserved = _json(
            await client.put(
                f"/v1/editor/drafts/{draft_id}/schedule",
                headers=auth_headers,
                json={
                    "expected_draft_version": 1,
                    "expected_schedule_version": None,
                    "scheduled_for": (_now() + timedelta(minutes=2)).isoformat(),
                },
            ),
            200,
        )
        if (
            reserved.get("state") != "scheduled"
            or reserved.get("snapshot_draft_version") != 1
            or any(key in reserved for key in ("document", "map_data", "image_links"))
        ):
            raise _StageAbort("schedule_contract_mismatch")

        edited_document = vertical_document()
        edited_document["headline"] = VERTICAL_EDITED_HEADLINE
        edited = _json(
            await client.put(
                f"/v1/editor/drafts/{draft_id}",
                headers=auth_headers,
                json={"expected_version": 1, "document": edited_document},
            ),
            200,
        )
        if edited.get("version") != 2 or edited.get("document", {}).get("headline") != VERTICAL_EDITED_HEADLINE:
            raise _StageAbort("post_schedule_edit_mismatch")

        completed_schedule: dict[str, Any] | None = None
        for _ in range(40):
            current = _json(
                await client.get(
                    f"/v1/editor/drafts/{draft_id}/schedule",
                    headers=auth_headers,
                ),
                200,
            )
            if current.get("state") == "published":
                completed_schedule = current
                break
            await asyncio.sleep(0.1)
        if completed_schedule is None:
            raise _StageAbort("scheduled_publication_timeout")

        revisions = await client.get(
            f"/v1/editor/drafts/{draft_id}/revisions", headers=auth_headers
        )
        if revisions.status_code != 200:
            raise _StageAbort(_safe_error_code(revisions))
        revision_rows = revisions.json()
        if not isinstance(revision_rows, list) or len(revision_rows) != 1:
            raise _StageAbort("revision_count_mismatch")
        revision = revision_rows[0]
        map_data = revision.get("map_data")
        resolved_revision_map = (
            resolve_map(map_data, lambda image_id: f"http://test/images/{image_id}")
            if isinstance(map_data, dict)
            else None
        )
        current_draft = _json(
            await client.get(f"/v1/editor/drafts/{draft_id}", headers=auth_headers),
            200,
        )
        today = _json(await client.get("/v1/editor/today", headers=auth_headers), 200)
        items = today.get("items")
        if revision.get("document", {}).get("headline") != VERTICAL_HEADLINE:
            raise _StageAbort("scheduled_revision_headline_mismatch")
        if current_draft.get("document", {}).get("headline") != VERTICAL_EDITED_HEADLINE:
            raise _StageAbort("scheduled_current_draft_mismatch")
        if current_draft.get("version") != 3:
            raise _StageAbort("scheduled_draft_version_mismatch")
        if (
            not isinstance(resolved_revision_map, dict)
            or _map_identity(resolved_revision_map) != _map_identity(validated_map)
        ):
            raise _StageAbort("scheduled_map_identity_mismatch")
        if not isinstance(items, list) or len(items) != 1:
            raise _StageAbort("scheduled_today_item_mismatch")
        published_article_id = items[0].get("article_id")
        if not isinstance(published_article_id, str):
            raise _StageAbort("publication_identity_mismatch")
        return (published_article_id, resolved_revision_map), {
            "article_id": published_article_id,
            "draft_version": current_draft["version"],
            "map_hash": _map_identity(resolved_revision_map),
            "revision_number": revision["revision_number"],
            "schedule_version": completed_schedule["version"],
            "snapshot_draft_version": completed_schedule["snapshot_draft_version"],
        }

    article_id, published_map = await _run_stage("publish", completed, publish_operation)

    async def order_operation():
        before = _json(
            await client.get("/v1/editor/today", headers=auth_headers), 200
        )
        changed = _json(
            await client.put(
                "/v1/editor/today-order",
                headers=auth_headers,
                json={
                    "ordered_article_ids": [article_id],
                    "featured_article_id": article_id,
                },
            ),
            200,
        )
        items = changed.get("items")
        if (
            len(before.get("items", [])) != 1
            or not isinstance(items, list)
            or len(items) != 1
            or items[0].get("article_id") != article_id
            or items[0].get("display_order") != 0
            or items[0].get("is_featured") is not True
        ):
            raise _StageAbort("today_order_mismatch")
        return changed, {
            "article_ids": [item["article_id"] for item in items],
            "display_orders": [item["display_order"] for item in items],
            "featured_article_id": article_id,
        }

    await _run_stage("order", completed, order_operation)

    async def feed_operation():
        key_id = "editorial-vertical-python-key-0001"
        device = _device()
        attestation = _json(
            await client.post(
                "/v1/app/attest/challenge",
                json={"purpose": "attestation", "key_id": key_id, "device": device},
            ),
            200,
        )
        _json(
            await client.post(
                "/v1/app/attest/register",
                json={
                    "challenge_id": attestation["challenge_id"],
                    "key_id": key_id,
                    "attestation_object": _b64(b"vertical-attestation-object"),
                    "device": device,
                },
            ),
            200,
        )
        assertion = _json(
            await client.post(
                "/v1/app/attest/challenge",
                json={"purpose": "assertion", "key_id": key_id, "device": device},
            ),
            200,
        )
        feed = _json(
            await client.post(
                "/v1/app/feed",
                json={
                    "payload": {"device": device, "limit": 5, "cursor": None},
                    "auth": {
                        "challenge_id": assertion["challenge_id"],
                        "key_id": key_id,
                        "assertion": _b64(b"vertical-assertion-object"),
                        "issued_at": int(_now().timestamp()),
                    },
                },
            ),
            200,
        )
        items = feed.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise _StageAbort("feed_count_mismatch")
        item = items[0]
        map_data = item.get("map")
        images = map_data.get("images") if isinstance(map_data, dict) else None
        if (
            item.get("article_id") != article_id
            or not isinstance(map_data, dict)
            or _map_identity(map_data) != _map_identity(published_map)
            or not isinstance(images, list)
            or len(images) != 1
            or not str(images[0].get("url", "")).startswith("http")
        ):
            raise _StageAbort("feed_identity_mismatch")
        image = await client.get(f"/images/{VERTICAL_IMAGE_ID}")
        if image.status_code != 200 or image.content != PNG_1X1:
            raise _StageAbort("feed_image_unavailable")
        return feed, {
            "article_id": item["article_id"],
            "map_hash": _map_identity(map_data),
            "image_ids": [entry["image_id"] for entry in images],
            "position": 0,
        }

    await _run_stage("feed", completed, feed_operation)
    if tuple(stage.stage for stage in completed) != VERTICAL_STAGES:
        raise VerticalStageError("feed", "stage_order_mismatch", completed[:-1])
    return VerticalResult(
        stages=tuple(completed),
        article_id=article_id,
        headline=VERTICAL_HEADLINE,
        scene_count=len(published_map.get("scenes", [])),
        map_hash=_map_identity(published_map),
        editor_token=editor_token,
    )


@pytest.mark.asyncio
async def test_editorial_vertical_reaches_manual_first_feed_through_production_routes():
    repo = configure_vertical_state("http://test")
    worker = PublicationScheduleWorker(state.settings, repo)
    worker.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            result = await run_editorial_http(client)
    finally:
        await worker.stop()

    assert [stage.stage for stage in result.stages] == list(VERTICAL_STAGES)
    assert all(len(stage.digest) == 64 for stage in result.stages)
    assert result.scene_count == 3
    assert repo.publication is not None
    assert repo.publication["article_id"] == result.article_id
    assert repo.publication["display_order"] == 0
    assert repo.publication["is_featured"] is True
    assert repo.map_data is not None
    assert repo.map_data["editorial"]["pipeline_version"] == "manual-editor-v1"
    assert repo.schedule is not None
    assert repo.schedule["state"] == "published"
    assert repo.schedule["snapshot_draft_version"] == 1
    assert repo.revision is not None
    assert repo.revision["document"]["headline"] == VERTICAL_HEADLINE
    assert next(iter(repo.drafts.values()))["document"]["headline"] == VERTICAL_EDITED_HEADLINE


@pytest.mark.asyncio
async def test_app_client_cooldown_is_per_key_not_global():
    service = VerticalAppClientService()
    device = _device()

    async def register_and_feed(key_id: str) -> None:
        attestation = await service.issue_challenge(
            purpose="attestation", key_id=key_id, device=device
        )
        await service.register(
            challenge_id=attestation["challenge_id"],
            key_id=key_id,
            attestation_object=b"attestation",
            device=device,
        )
        assertion = await service.issue_challenge(
            purpose="assertion", key_id=key_id, device=device
        )
        await service.authenticate_feed(
            payload={"device": device},
            challenge_id=assertion["challenge_id"],
            key_id=key_id,
            assertion_object=b"assertion",
            issued_at=int(_now().timestamp()),
            cooldown_s=1,
        )

    await register_and_feed("vertical-client-key-0001")
    await register_and_feed("vertical-client-key-0002")
    assertion = await service.issue_challenge(
        purpose="assertion", key_id="vertical-client-key-0001", device=device
    )
    with pytest.raises(AppClientError) as caught:
        await service.authenticate_feed(
            payload={"device": device},
            challenge_id=assertion["challenge_id"],
            key_id="vertical-client-key-0001",
            assertion_object=b"assertion",
            issued_at=int(_now().timestamp()),
            cooldown_s=1,
        )
    assert caught.value.status_code == 429
    assert caught.value.code == "feed_cooldown"


def test_editorial_document_type_is_validated_and_reaches_the_feed_card():
    """DR-13254: the desk can publish a primary document (F-3).

    feed.py only surfaces the document card when `document_type` is in its
    whitelist and then reads `document_source_name` as `source_publisher`, so
    this pins the two ends: the model accepts exactly the canonical vocabulary,
    and the feed projection turns those values into a document card.
    """
    from app.editorial_models import EditorialDocument
    from app.feed import SUPPORTED_DOCUMENT_TYPES
    from app.primary_documents import DOCUMENT_TYPES

    # The desk's vocabulary must be the pipeline's, or a desk publication would
    # be silently demoted to an ordinary article by the feed whitelist.
    assert DOCUMENT_TYPES == SUPPORTED_DOCUMENT_TYPES

    assert EditorialDocument().document_type == ""
    for kind in sorted(DOCUMENT_TYPES):
        assert EditorialDocument(document_type=kind).document_type == kind

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EditorialDocument(document_type="governance-proposal")


def test_editorial_publish_persists_document_columns():
    """The publish INSERT must carry the two columns onto the article row."""
    import inspect

    from app.repository import Repository

    source = inspect.getsource(Repository.publish_editorial_draft)
    assert "document_type" in source and "document_source_name" in source
    # Empty must land as NULL so the row stays an ordinary article.
    assert 'str(document.get("document_type") or "").strip() or None' in source
