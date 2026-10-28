"""FastAPI web server: health, manual refresh trigger, and read endpoints.

Wiring lives in the lifespan handler so the DB pool + scheduler share one
lifecycle with the web process. On Railway, `uvicorn app.api:app` is the
whole service.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field
from fastapi import Depends, FastAPI, File, Header, HTTPException, Path as APIPath, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.requests import ClientDisconnect

from .app_api_models import (
    AccountActivityEventBatch,
    AccountActivityEventBatchResponse,
    AccountDeletionResponse,
    AccountPrivacyConsentLink,
    AccountProfile,
    AccountRecommendationRequest,
    AccountRecommendationResponse,
    AccountReadingStatePatch,
    AccountReadingStateResponse,
    AccountSessionResponse,
    ArticleFeedbackRequest,
    ArticleFeedbackResponse,
    ArticleViewResponse,
    AppleAccountSignInRequest,
    AuthenticatedArticleViewRequest,
    AuthenticatedDiscoveryRequest,
    AuthenticatedFeedRequest,
    AuthenticatedPushRegistrationRequest,
    AuthenticatedPrivacyConsentRequest,
    ChallengeRequest,
    ChallengeResponse,
    FeedResponse,
    DiscoveryResponse,
    RegisterRequest,
    RegisterResponse,
    PushPreferences,
    PushRegistrationResponse,
    PrivacyConsentResponse,
    TimelineSubscriptionListResponse,
    TimelineSubscriptionHistoryResponse,
    TimelineSubscriptionResponse,
)
from .account_service import AccountService, extract_bearer
from .apns import APNsProvider
from .app_attest import AppAttestVerifier
from .app_service import AppClientError, AppClientService, decode_base64url
from .apple_identity import AppleIdentityError, AppleIdentityProvider, TokenCipher
from .article_embedding_worker import ArticleEmbeddingWorker
from .article_cluster_worker import ArticleClusterWorker
from .config import Settings, get_settings
from .curation_agent import CurationAgent
from .curation_tools import CurationArticleTools
from .curation_worker import CurationCatalogWorker
from .db import Database
from .engine import LoaderEngine
from .editorial_models import (
    EditorOperatorCreateRequest,
    EditorOperatorCreatedResponse,
    EditorOperatorResponse,
    EditorPrincipal,
    EditorialDraftCreateRequest,
    EditorialDraftResponse,
    EditorialDraftUpdateRequest,
    EditorialDraftVersionRequest,
    EditorialDocument,
    EditorialAssetResponse,
    EditorialPublicationResponse,
    EditorialPublishResponse,
    EditorialRevisionResponse,
    EditorialScheduleResponse,
    EditorialScheduleUpsertRequest,
    EditorialScheduleVersionRequest,
    EditorialTodayOrderRequest,
    EditorialTodayResponse,
    EditorialValidationResponse,
    EditorialVersionRequest,
)
from .editorial_compiler import validate_editorial_document
from .editorial_service import EditorialError, authenticate_editor, store_editorial_image
from .feed import (
    FeedCursor,
    InvalidFeedCursor,
    build_feed_item,
    build_feed_page,
    decode_cursor,
    encode_cursor,
    seoul_editorial_day,
)
from .image_rag import ImageRagService
from .images import _ext_for  # noqa: F401  (re-export for callers/tests)
from .jobs import Job, load_jobs
from .logging_conf import configure_logging
from .map_loader import resolve_map
from .map_worker import MapQueueWorker
from .openrouter import OpenRouterClient
from .openrouter_rerank import OpenRouterRerankClient
from .object_storage import ObjectStorageError, S3ObjectStore
from .ops import build_worker_metrics
from .publication_worker import PublicationScheduleWorker
from .chain_notes import attach_chain_progress, attach_revision_notes
from .claim_diff import build_revisions
from .event_chain_worker import EventChainWorker
from .market_data import MarketDataClient, MarketDataFacade, SymbolDirectory
from .primary_document_worker import PrimaryDocumentWorker
from .staleness import attach_staleness_notes, collect_staleness_subjects
from .push_worker import EditorialPushWorker
from .repository import Repository, repaired_timeline_nodes
from .explore_timeline import TIMELINE_TIME_ZONE
from .explore_timeline_worker import ExploreTimelineWorker
from .timeline_copy import TimelineCopyPolisher
from .scheduler import RefreshScheduler
from .telemetry import WorkerTelemetry
from .triage import TriageScorer
from .triage_worker import TriageWorker

log = logging.getLogger("gdelt.api")
OPS_DASHBOARD_PATH = Path(__file__).with_name("ops_dashboard.html")


class AppState:
    settings: Settings
    db: Database
    repo: Repository
    engine: LoaderEngine
    map_worker: MapQueueWorker
    publication_worker: PublicationScheduleWorker
    curation_worker: CurationCatalogWorker
    curation_llm: OpenRouterClient
    curation_llm_started: bool
    scheduler: RefreshScheduler
    app_client_service: AppClientService
    account_service: AccountService
    apple_http: httpx.AsyncClient | None
    telemetry: WorkerTelemetry
    object_store: S3ObjectStore | None
    image_rag: ImageRagService
    article_embedding_worker: ArticleEmbeddingWorker
    article_cluster_worker: ArticleClusterWorker
    explore_timeline_worker: ExploreTimelineWorker
    timeline_rerank: OpenRouterRerankClient
    timeline_rerank_started: bool
    apns_provider: APNsProvider | None
    push_worker: EditorialPushWorker | None
    triage_worker: TriageWorker
    primary_document_worker: PrimaryDocumentWorker
    market_data: MarketDataFacade | None
    event_chain_worker: EventChainWorker | None


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    state.settings = settings
    state.object_store = None
    if not settings.is_staging_app and settings.image_backend == "s3":
        state.object_store = S3ObjectStore(settings)
    state.db = Database(settings)
    await state.db.connect()
    if settings.is_staging_app:
        state.apple_http = None
        state.apns_provider = None
        state.push_worker = None
        try:
            await state.db.validate_runtime_contract()
            state.repo = Repository(state.db)
            verifier = None
            if settings.app_attest_enabled and settings.app_attest_app_id():
                verifier = AppAttestVerifier(
                    app_id=settings.app_attest_app_id(),
                    environment=settings.app_attest_environment,
                    allowed_bundle_versions=(
                        settings.app_attest_bundle_version_set()
                    ),
                    require_extended_attributes=(
                        settings.app_attest_require_extended_attributes
                    ),
                )
            state.app_client_service = AppClientService(
                settings,
                state.repo,
                verifier,
            )
            apple_provider = None
            apple_cipher = None
            if settings.apple_sign_in_enabled:
                try:
                    settings.validate_apple_sign_in()
                    state.apple_http = httpx.AsyncClient(
                        timeout=settings.apple_sign_in_http_timeout_s
                    )
                    apple_provider = AppleIdentityProvider(
                        settings,
                        state.apple_http,
                    )
                    apple_cipher = TokenCipher(
                        settings.apple_token_encryption_key.get_secret_value()
                    )
                except ValueError:
                    log.error(
                        "Staging Sign in with Apple configuration invalid "
                        "error_code=apple_sign_in_settings_invalid"
                    )
                except AppleIdentityError as error:
                    log.error(
                        "Staging Sign in with Apple configuration invalid "
                        "error_code=%s",
                        error.code,
                    )
            state.account_service = AccountService(
                settings,
                state.repo,
                apple_provider,
                apple_cipher,
            )
            log.info(
                "staging app API up runtime_profile=staging_app "
                "read_schema=public write_schema=staging_app "
                "background_workers=0 outbound_providers=0"
            )
            yield
        finally:
            if state.apple_http is not None:
                await state.apple_http.aclose()
            await state.db.close()
            log.info("staging app API down")
        return

    await state.db.ensure_schema()
    state.repo = Repository(state.db)
    try:
        feature_backfill = await state.repo.backfill_article_recommendation_features(
            limit=500
        )
        if feature_backfill["inserted"] or feature_backfill["skipped"]:
            log.info(
                "recommendation feature backfill inserted=%d skipped=%d",
                feature_backfill["inserted"],
                feature_backfill["skipped"],
            )
    except Exception:  # noqa: BLE001 - shared feature index is best effort
        log.warning("recommendation feature backfill skipped")
    await state.repo.reconcile_map_queue(
        max_attempts=settings.map_max_attempts
    )
    state.image_rag = ImageRagService(settings, state.repo, state.object_store)
    await state.image_rag.start()
    state.article_embedding_worker = ArticleEmbeddingWorker(settings, state.repo)
    try:
        await state.article_embedding_worker.start()
    except Exception:  # noqa: BLE001 - optional derived index must fail open
        log.warning("article embedding worker unavailable code=start_failed")
    state.telemetry = WorkerTelemetry(settings, state.repo)
    await state.telemetry.start()
    verifier = None
    if settings.app_attest_enabled and settings.app_attest_app_id():
        verifier = AppAttestVerifier(
            app_id=settings.app_attest_app_id(),
            environment=settings.app_attest_environment,
            allowed_bundle_versions=settings.app_attest_bundle_version_set(),
            require_extended_attributes=settings.app_attest_require_extended_attributes,
        )
    state.app_client_service = AppClientService(settings, state.repo, verifier)
    state.apple_http = None
    apple_provider = None
    apple_cipher = None
    if settings.apple_sign_in_enabled:
        try:
            settings.validate_apple_sign_in()
            state.apple_http = httpx.AsyncClient(
                timeout=settings.apple_sign_in_http_timeout_s
            )
            apple_provider = AppleIdentityProvider(settings, state.apple_http)
            apple_cipher = TokenCipher(
                settings.apple_token_encryption_key.get_secret_value()
            )
        except ValueError:
            log.error(
                "Sign in with Apple is configured incorrectly "
                "error_code=apple_sign_in_settings_invalid"
            )
        except AppleIdentityError as error:
            log.error(
                "Sign in with Apple is configured incorrectly error_code=%s",
                error.code,
            )
    state.account_service = AccountService(
        settings,
        state.repo,
        apple_provider,
        apple_cipher,
    )
    state.apns_provider = None
    state.push_worker = None
    if settings.push_notifications_enabled:
        settings.validate_apns()
        if apple_cipher is None:
            raise ValueError(
                "PUSH_NOTIFICATIONS_ENABLED requires configured Sign in with Apple"
            )
        state.apns_provider = APNsProvider(settings)
        state.push_worker = EditorialPushWorker(
            settings,
            state.repo,
            state.apns_provider,
            apple_cipher,
        )
    state.engine = LoaderEngine(
        settings,
        state.repo,
        state.telemetry,
        image_rag=state.image_rag,
    )
    state.map_worker = MapQueueWorker(
        settings,
        state.engine,
        lease_owner=state.telemetry.instance_id,
    )
    state.publication_worker = PublicationScheduleWorker(settings, state.repo)
    state.curation_llm = OpenRouterClient(
        settings,
        model=settings.curation_agent_model,
    )
    state.curation_llm_started = bool(
        (settings.curation_worker_enabled or settings.timeline_worker_enabled)
        and state.curation_llm.configured
    )
    if state.curation_llm_started:
        await state.curation_llm.__aenter__()
    state.curation_worker = CurationCatalogWorker(
        settings,
        state.repo,
        CurationAgent(
            settings,
            state.curation_llm,
            lambda: CurationArticleTools(settings, state.repo),
        ),
        state.telemetry,
    )
    state.timeline_rerank = OpenRouterRerankClient(
        settings,
        model=settings.timeline_rerank_model,
    )
    state.timeline_rerank_started = bool(
        settings.timeline_worker_enabled and state.timeline_rerank.configured
    )
    if state.timeline_rerank_started:
        await state.timeline_rerank.__aenter__()
    state.article_cluster_worker = ArticleClusterWorker(
        settings,
        state.repo,
        getattr(state.article_embedding_worker, "vector", None),
    )
    state.explore_timeline_worker = ExploreTimelineWorker(
        settings,
        state.repo,
        getattr(state.article_embedding_worker, "vector", None),
        state.article_cluster_worker,
        state.timeline_rerank,
        TimelineCopyPolisher(state.curation_llm),
    )
    state.triage_worker = TriageWorker(
        settings,
        state.repo,
        TriageScorer(settings),
        state.telemetry,
    )
    state.primary_document_worker = PrimaryDocumentWorker(settings, state.repo)
    # Read-time market values behind the staleness comparison. Built from the
    # shared HTTP client so a provider outage degrades to no annotation rather
    # than to a failed response.
    state.market_data = MarketDataFacade(
        MarketDataClient(
            httpx.AsyncClient(),
            directory=SymbolDirectory.load(settings.market_data_ids_file),
        ),
        state.repo,
    )
    state.event_chain_worker = EventChainWorker(
        state.repo,
        getattr(state.article_embedding_worker, "vector", None),
    )
    state.scheduler = RefreshScheduler(
        settings,
        state.engine,
        state.primary_document_worker,
    )
    state.map_worker.start()
    state.triage_worker.start()
    state.publication_worker.start()
    if state.push_worker is not None:
        state.push_worker.start()
    state.curation_worker.start()
    state.explore_timeline_worker.start()
    if state.event_chain_worker is not None:
        state.event_chain_worker.start()
    state.scheduler.start()
    log.info("gdelt-loader up")
    try:
        yield
    finally:
        state.scheduler.shutdown()
        await state.primary_document_worker.close()
        await state.triage_worker.stop()
        await state.curation_worker.stop()
        await state.explore_timeline_worker.stop()
        if state.event_chain_worker is not None:
            await state.event_chain_worker.stop()
        if state.push_worker is not None:
            await state.push_worker.stop()
        await state.publication_worker.stop()
        await state.map_worker.stop()
        if state.curation_llm_started:
            await state.curation_llm.__aexit__(None, None, None)
        if state.timeline_rerank_started:
            await state.timeline_rerank.__aexit__(None, None, None)
        await state.image_rag.close()
        await state.article_embedding_worker.stop()
        await state.telemetry.stop()
        if state.apple_http is not None:
            await state.apple_http.aclose()
        if state.apns_provider is not None:
            await state.apns_provider.close()
        await state.db.close()
        log.info("gdelt-loader down")


app = FastAPI(title="Multi-source News Loading Engine", version="1.0.0", lifespan=lifespan)


def _staging_surface_allowed(path: str) -> bool:
    return (
        path == "/health"
        or path == "/v1/account"
        or path.startswith("/v1/account/")
        or path.startswith("/v1/app/")
    )


def _mark_staging_response(response: Response, settings: Settings) -> None:
    response.headers["X-Backend-Environment"] = settings.runtime_profile
    response.headers["X-Database-Read-Schema"] = (
        settings.database_read_schema
    )
    response.headers["X-Database-Write-Schema"] = (
        settings.database_write_schema
    )


@app.middleware("http")
async def staging_surface_gate(request: Request, call_next):
    settings = getattr(state, "settings", None)
    if settings is None or not settings.is_staging_app:
        return await call_next(request)
    if not _staging_surface_allowed(request.url.path):
        response = JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "staging_surface_disabled",
                    "message": "staging surface disabled",
                    "retry_after_seconds": None,
                }
            },
        )
    else:
        response = await call_next(request)
    _mark_staging_response(response, settings)
    return response


@app.exception_handler(AppClientError)
async def app_client_error_handler(request: Request, exc: AppClientError):
    if exc.status_code >= 400:
        log.warning(
            "app client error path=%s status=%d code=%s",
            request.url.path,
            exc.status_code,
            exc.code,
        )
    headers = {}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = f"{max(0.0, exc.retry_after_seconds):.3f}"
    return JSONResponse(
        status_code=exc.status_code,
        headers=headers,
        content={
            "error": {
                "code": exc.code,
                "message": exc.code.replace("_", " "),
                "retry_after_seconds": exc.retry_after_seconds,
            }
        },
    )


@app.exception_handler(EditorialError)
async def editorial_error_handler(request: Request, exc: EditorialError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.code.replace("_", " "),
                "detail": jsonable_encoder(exc.detail),
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def app_validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith(("/v1/app/", "/v1/account", "/v1/editor/")):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "request body does not match the app API contract",
                    "retry_after_seconds": None,
                }
            },
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def require_token(x_api_token: Optional[str] = Header(default=None)) -> None:
    """Guard mutating endpoints when API_TOKEN is configured."""
    token = state.settings.api_token
    if token and x_api_token != token:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Token")


def require_ops_token(x_api_token: Optional[str] = Header(default=None)) -> None:
    """Fail-closed guard for operational data displayed by /ops."""
    token = state.settings.api_token
    if not token:
        raise HTTPException(status_code=503, detail="ops_token_not_configured")
    if x_api_token is None or not secrets.compare_digest(token, x_api_token):
        raise HTTPException(status_code=401, detail="invalid_or_missing_ops_token")


def require_editor_master_token(
    x_api_token: Optional[str] = Header(default=None),
) -> None:
    token = state.settings.api_token
    if not token:
        raise EditorialError(503, "editor_master_token_not_configured")
    if x_api_token is None or not secrets.compare_digest(token, x_api_token):
        raise EditorialError(401, "invalid_or_missing_editor_master_token")


async def require_editor(
    authorization: Optional[str] = Header(default=None),
) -> EditorPrincipal:
    return await authenticate_editor(state.repo, authorization)


# ---- editorial operators ---------------------------------------------
@app.post(
    "/v1/editor/operators",
    response_model=EditorOperatorCreatedResponse,
    dependencies=[Depends(require_editor_master_token)],
    status_code=201,
)
async def create_editor_operator(body: EditorOperatorCreateRequest):
    row, token = await state.repo.create_editor_operator(
        display_name=body.display_name,
        role=body.role,
    )
    return {**row, "token": token}


@app.delete(
    "/v1/editor/operators/{operator_id}",
    response_model=EditorOperatorResponse,
    dependencies=[Depends(require_editor_master_token)],
)
async def revoke_editor_operator(operator_id: UUID):
    row = await state.repo.revoke_editor_operator(operator_id)
    if row is None:
        raise EditorialError(404, "editor_operator_not_found")
    return row


@app.get("/v1/editor/me", response_model=EditorOperatorResponse)
async def editor_me(operator: EditorPrincipal = Depends(require_editor)):
    return {
        "id": operator.id,
        "display_name": operator.display_name,
        "role": operator.role,
        "status": "active",
        "created_at": operator.created_at,
        "last_used_at": operator.last_used_at,
        "revoked_at": None,
    }


# ---- editorial drafts -------------------------------------------------
@app.post(
    "/v1/editor/drafts",
    response_model=EditorialDraftResponse,
    status_code=201,
)
async def create_editorial_draft(
    body: EditorialDraftCreateRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    return await state.repo.create_editorial_draft(
        document=body.document.model_dump(mode="json"),
        operator_id=operator.id,
    )


@app.get("/v1/editor/drafts", response_model=list[EditorialDraftResponse])
async def editorial_drafts(operator: EditorPrincipal = Depends(require_editor)):
    return await state.repo.editorial_drafts(100)


@app.get("/v1/editor/trash", response_model=list[EditorialDraftResponse])
async def editorial_trash(operator: EditorPrincipal = Depends(require_editor)):
    return await state.repo.editorial_trashed_drafts(100)


@app.get("/v1/editor/drafts/{draft_id}", response_model=EditorialDraftResponse)
async def get_editorial_draft(
    draft_id: UUID,
    operator: EditorPrincipal = Depends(require_editor),
):
    row = await state.repo.get_editorial_draft(draft_id)
    if row is None:
        raise EditorialError(404, "editorial_draft_not_found")
    return row


@app.put("/v1/editor/drafts/{draft_id}", response_model=EditorialDraftResponse)
async def update_editorial_draft(
    draft_id: UUID,
    body: EditorialDraftUpdateRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    row = await state.repo.update_editorial_draft(
        draft_id=draft_id,
        expected_version=body.expected_version,
        document=body.document.model_dump(mode="json"),
        operator_id=operator.id,
    )
    if row is not None:
        return row
    current = await state.repo.get_editorial_draft(draft_id)
    if current is None:
        raise EditorialError(404, "editorial_draft_not_found")
    raise EditorialError(409, "draft_version_conflict", detail={"current": current})


@app.post(
    "/v1/editor/drafts/{draft_id}/trash",
    response_model=EditorialDraftResponse,
)
async def trash_editorial_draft(
    draft_id: UUID,
    body: EditorialDraftVersionRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    try:
        result = await state.repo.trash_editorial_draft(
            draft_id=draft_id,
            expected_version=body.expected_version,
            operator_id=operator.id,
        )
    except Exception:
        raise EditorialError(503, "editorial_draft_trash_unavailable") from None
    if result["kind"] == "not_found":
        raise EditorialError(404, "editorial_draft_not_found")
    if result["kind"] == "conflict":
        raise EditorialError(
            409,
            "draft_version_conflict",
            detail={"current": result["current"]},
        )
    if result["kind"] == "not_eligible":
        raise EditorialError(
            409,
            "editorial_draft_not_trashable",
            detail={"current": result["current"]},
        )
    if result["kind"] == "schedule_processing":
        raise EditorialError(409, "editorial_schedule_not_mutable")
    return result["draft"]


@app.post(
    "/v1/editor/drafts/{draft_id}/restore",
    response_model=EditorialDraftResponse,
)
async def restore_editorial_draft(
    draft_id: UUID,
    body: EditorialDraftVersionRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    try:
        result = await state.repo.restore_editorial_draft(
            draft_id=draft_id,
            expected_version=body.expected_version,
            operator_id=operator.id,
        )
    except Exception:
        raise EditorialError(503, "editorial_draft_restore_unavailable") from None
    if result["kind"] == "not_found":
        raise EditorialError(404, "editorial_draft_not_found")
    if result["kind"] == "conflict":
        raise EditorialError(
            409,
            "draft_version_conflict",
            detail={"current": result["current"]},
        )
    if result["kind"] == "not_trashed":
        raise EditorialError(
            409,
            "editorial_draft_not_trashed",
            detail={"current": result["current"]},
        )
    return result["draft"]


@app.post(
    "/v1/editor/drafts/{draft_id}/validate",
    response_model=EditorialValidationResponse,
)
async def validate_editorial_draft(
    draft_id: UUID,
    operator: EditorPrincipal = Depends(require_editor),
):
    row = await state.repo.get_editorial_draft(draft_id)
    if row is None:
        raise EditorialError(404, "editorial_draft_not_found")
    document = EditorialDocument.model_validate(row["document"])
    image_records = await state.repo.editorial_image_records(
        [item.image_id for item in document.images]
    )
    result = validate_editorial_document(document, image_records)
    return {
        "valid": result.valid,
        "errors": result.errors,
        "map": resolve_map(result.map_data, _image_url_for),
    }


@app.post(
    "/v1/editor/assets",
    response_model=EditorialAssetResponse,
    status_code=201,
)
async def upload_editorial_asset(
    file: UploadFile = File(...),
    operator: EditorPrincipal = Depends(require_editor),
):
    return await store_editorial_image(
        file,
        operator,
        settings=state.settings,
        repo=state.repo,
        object_store=state.object_store,
    )


_EDITORIAL_SCHEDULE_RESPONSE_FIELDS = frozenset(
    EditorialScheduleResponse.model_fields
)


def _editorial_schedule_response(row: dict) -> dict:
    """Return public metadata only; frozen publication payloads stay private."""
    return {
        key: value
        for key, value in row.items()
        if key in _EDITORIAL_SCHEDULE_RESPONSE_FIELDS
    }


def _editorial_schedule_window_error(
    scheduled_for: datetime,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return the stable API error for the bounded scheduling window."""
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    scheduled_utc = scheduled_for.astimezone(UTC)
    if scheduled_utc < now_utc + timedelta(seconds=60):
        return "editorial_schedule_too_soon"
    if scheduled_utc > now_utc + timedelta(hours=1):
        return "editorial_schedule_too_far"
    return None


@app.get(
    "/v1/editor/schedules",
    response_model=list[EditorialScheduleResponse],
)
async def editorial_schedules(operator: EditorPrincipal = Depends(require_editor)):
    return [
        _editorial_schedule_response(row)
        for row in await state.repo.editorial_schedules(100)
    ]


@app.get(
    "/v1/editor/drafts/{draft_id}/schedule",
    response_model=EditorialScheduleResponse,
)
async def get_editorial_schedule(
    draft_id: UUID,
    operator: EditorPrincipal = Depends(require_editor),
):
    row = await state.repo.editorial_schedule(draft_id)
    if row is None:
        raise EditorialError(404, "editorial_schedule_not_found")
    return _editorial_schedule_response(row)


@app.put(
    "/v1/editor/drafts/{draft_id}/schedule",
    response_model=EditorialScheduleResponse,
)
async def upsert_editorial_schedule(
    draft_id: UUID,
    body: EditorialScheduleUpsertRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    scheduled_for = body.scheduled_for.astimezone(UTC)
    if window_error := _editorial_schedule_window_error(scheduled_for):
        raise EditorialError(422, window_error)
    row = await state.repo.get_editorial_draft(draft_id)
    if row is None:
        raise EditorialError(404, "editorial_draft_not_found")
    if int(row["version"]) != body.expected_draft_version:
        raise EditorialError(409, "draft_version_conflict", detail={"current": row})
    document = EditorialDocument.model_validate(row["document"])
    image_records = await state.repo.editorial_image_records(
        [item.image_id for item in document.images]
    )
    validation = validate_editorial_document(document, image_records)
    if not validation.valid:
        raise EditorialError(
            422,
            "editorial_document_invalid",
            detail={"errors": validation.errors},
        )
    image_links = [
        {
            "ref_index": image["ref"],
            "image_id": image["image_id"],
            "source_url": image.get("source_url") or "",
        }
        for image in validation.map_data["images"]
    ]
    try:
        result = await state.repo.upsert_editorial_schedule(
            draft_id=draft_id,
            expected_draft_version=body.expected_draft_version,
            expected_schedule_version=body.expected_schedule_version,
            scheduled_for=scheduled_for,
            operator_id=operator.id,
            document=document.model_dump(mode="json"),
            map_data=validation.map_data,
            image_links=image_links,
        )
    except Exception:
        raise EditorialError(503, "editorial_schedule_unavailable") from None
    if result["kind"] == "not_found":
        raise EditorialError(404, "editorial_draft_not_found")
    if result["kind"] == "draft_conflict":
        raise EditorialError(
            409,
            "draft_version_conflict",
            detail={"current": result["current"]},
        )
    if result["kind"] in {"schedule_conflict", "schedule_not_mutable"}:
        current = result.get("current")
        raise EditorialError(
            409,
            (
                "editorial_schedule_not_mutable"
                if result["kind"] == "schedule_not_mutable"
                else "editorial_schedule_conflict"
            ),
            detail={
                "current_schedule": (
                    _editorial_schedule_response(current) if current else None
                )
            },
        )
    return _editorial_schedule_response(result["schedule"])


@app.post(
    "/v1/editor/drafts/{draft_id}/schedule/cancel",
    response_model=EditorialScheduleResponse,
)
async def cancel_editorial_schedule(
    draft_id: UUID,
    body: EditorialScheduleVersionRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    try:
        result = await state.repo.cancel_editorial_schedule(
            draft_id=draft_id,
            expected_schedule_version=body.expected_schedule_version,
            operator_id=operator.id,
        )
    except Exception:
        raise EditorialError(503, "editorial_schedule_unavailable") from None
    if result["kind"] == "not_found":
        raise EditorialError(404, "editorial_draft_not_found")
    if result["kind"] in {
        "schedule_conflict",
        "schedule_not_active",
        "schedule_not_mutable",
    }:
        current = result.get("current")
        raise EditorialError(
            409,
            (
                "editorial_schedule_not_mutable"
                if result["kind"] == "schedule_not_mutable"
                else "editorial_schedule_conflict"
            ),
            detail={
                "current_schedule": (
                    _editorial_schedule_response(current) if current else None
                )
            },
        )
    return _editorial_schedule_response(result["schedule"])


@app.post(
    "/v1/editor/drafts/{draft_id}/publish",
    response_model=EditorialPublishResponse,
)
async def publish_editorial_draft(
    draft_id: UUID,
    body: EditorialVersionRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    if body.expected_version is None:
        raise EditorialError(422, "expected_draft_version_required")
    row = await state.repo.get_editorial_draft(draft_id)
    if row is None:
        raise EditorialError(404, "editorial_draft_not_found")
    if int(row["version"]) != body.expected_version:
        raise EditorialError(409, "draft_version_conflict", detail={"current": row})
    document = EditorialDocument.model_validate(row["document"])
    image_records = await state.repo.editorial_image_records(
        [item.image_id for item in document.images]
    )
    validation = validate_editorial_document(document, image_records)
    if not validation.valid:
        raise EditorialError(
            422,
            "editorial_document_invalid",
            detail={"errors": validation.errors},
        )
    image_links = [
        {
            "ref_index": image["ref"],
            "image_id": image["image_id"],
            "source_url": image.get("source_url") or "",
        }
        for image in validation.map_data["images"]
    ]
    try:
        result = await state.repo.publish_editorial_draft(
            draft_id=draft_id,
            expected_version=body.expected_version,
            operator_id=operator.id,
            document=document.model_dump(mode="json"),
            map_data=validation.map_data,
            image_links=image_links,
        )
    except Exception:
        raise EditorialError(503, "editorial_publish_unavailable") from None
    if result["kind"] == "not_found":
        raise EditorialError(404, "editorial_draft_not_found")
    if result["kind"] == "conflict":
        raise EditorialError(
            409,
            "draft_version_conflict",
            detail={"current": result["current"]},
        )
    return {
        "publication": result["publication"],
        "revision": result["revision"],
        "map": resolve_map(validation.map_data, _image_url_for),
    }


@app.get(
    "/v1/editor/drafts/{draft_id}/revisions",
    response_model=list[EditorialRevisionResponse],
)
async def editorial_revisions(
    draft_id: UUID,
    operator: EditorPrincipal = Depends(require_editor),
):
    if await state.repo.get_editorial_draft(draft_id) is None:
        raise EditorialError(404, "editorial_draft_not_found")
    return await state.repo.editorial_revisions(draft_id)


@app.post(
    "/v1/editor/drafts/{draft_id}/withdraw",
    response_model=EditorialPublicationResponse,
)
async def withdraw_editorial_publication(
    draft_id: UUID,
    body: EditorialVersionRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    try:
        result = await state.repo.withdraw_editorial_publication(
            draft_id=draft_id,
            expected_version=body.expected_version,
            operator_id=operator.id,
        )
    except Exception:
        raise EditorialError(503, "editorial_withdraw_unavailable") from None
    if result["kind"] == "not_found":
        raise EditorialError(404, "editorial_draft_not_found")
    if result["kind"] == "not_published":
        raise EditorialError(409, "editorial_draft_not_published")
    if result["kind"] == "conflict":
        raise EditorialError(
            409,
            "draft_version_conflict",
            detail={"current": result["current"]},
        )
    return result["publication"]


@app.get("/v1/editor/today", response_model=EditorialTodayResponse)
async def editorial_today(operator: EditorPrincipal = Depends(require_editor)):
    editorial_day = seoul_editorial_day()
    return {
        "editorial_day": editorial_day.isoformat(),
        "items": await state.repo.editorial_today(editorial_day),
    }


@app.put("/v1/editor/today-order", response_model=EditorialTodayResponse)
async def update_editorial_today_order(
    body: EditorialTodayOrderRequest,
    operator: EditorPrincipal = Depends(require_editor),
):
    editorial_day = seoul_editorial_day()
    try:
        result = await state.repo.set_editorial_today_order(
            editorial_day=editorial_day,
            ordered_article_ids=body.ordered_article_ids,
            featured_article_id=body.featured_article_id,
            operator_id=operator.id,
        )
    except Exception:
        raise EditorialError(503, "editorial_today_order_unavailable") from None
    if result["kind"] == "invalid":
        raise EditorialError(
            422,
            "invalid_editorial_today_articles",
            detail={"invalid_article_ids": result["invalid_article_ids"]},
        )
    return {
        "editorial_day": editorial_day.isoformat(),
        "items": result["items"],
    }


# ---- health ------------------------------------------------------------
@app.get("/health")
async def health():
    try:
        await state.db.pool.fetchval("SELECT 1")  # type: ignore[union-attr]
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    payload = {"status": "ok" if db_ok else "degraded", "db": db_ok}
    if state.settings.is_staging_app:
        payload.update(
            {
                "runtime_profile": state.settings.runtime_profile,
                "read_schema": state.settings.database_read_schema,
                "write_schema": state.settings.database_write_schema,
            }
        )
    return payload


@app.get("/stats")
async def stats():
    return {"counts": await state.repo.counts(), "recent_runs": await state.repo.last_runs(5)}


@app.get("/ops", response_class=HTMLResponse)
async def ops_dashboard():
    """Public shell only. Operational data remains behind API_TOKEN."""
    return HTMLResponse(OPS_DASHBOARD_PATH.read_text(encoding="utf-8"))


@app.get("/ops/api/snapshot", dependencies=[Depends(require_ops_token)])
async def ops_snapshot(
    status: Literal["queued", "processing", "done", "dead"] | None = Query(
        default=None
    ),
    limit: int = Query(default=100, ge=1, le=200),
):
    raw = await state.repo.worker_telemetry_rows(
        state.settings.ops_window_minutes
    )
    workers = build_worker_metrics(
        now=raw["now"],
        instances=raw["instances"],
        activities=raw["activities"],
        window_minutes=state.settings.ops_window_minutes,
        stale_after_s=state.settings.ops_stale_after_seconds,
    )
    push_stats = await state.repo.push_delivery_stats()
    available_articles = await state.repo.available_article_count()
    article_embeddings = await _article_embedding_snapshot(available_articles)
    return {
        "generated_at": raw["now"],
        "window_minutes": state.settings.ops_window_minutes,
        "available_articles": available_articles,
        "article_embeddings": article_embeddings,
        "queue": {
            "stats": await state.repo.queue_stats(),
            "items": await state.repo.queue_items(limit, status),
        },
        "workers": workers,
        "push": {
            "enabled": state.settings.push_notifications_enabled,
            "deliveries": {
                key: int(push_stats.get(key) or 0)
                for key in (
                    "queued",
                    "processing",
                    "sent",
                    "failed",
                    "cancelled",
                    "retry_attempts",
                )
            },
            "registrations": {
                "active": int(push_stats.get("active_registrations") or 0),
                "invalid": int(push_stats.get("invalid_registrations") or 0),
            },
            "last_sent_at": push_stats.get("last_sent_at"),
            "last_failed_at": push_stats.get("last_failed_at"),
            "last_retry_at": push_stats.get("last_retry_at"),
        },
        "map_budget": await _map_budget_snapshot(),
        "recent_runs": await state.repo.last_runs(10),
    }


@app.get("/ops/api/timelines", dependencies=[Depends(require_ops_token)])
async def ops_timelines():
    """Timeline production state: worker, current edition, totals, recent runs."""
    worker = getattr(state, "explore_timeline_worker", None)
    payload: dict = {
        "worker": (
            worker.status
            if worker is not None
            else {
                "enabled": state.settings.timeline_worker_enabled,
                "rerank_configured": False,
                "running": False,
                "started": False,
            }
        ),
        "current": None,
        "totals": {},
        "recent_catalogs": [],
        "candidate_pool": {},
        "cluster_snapshot": None,
    }
    try:
        metrics = await state.repo.explore_timeline_ops_metrics(
            candidate_days=state.settings.timeline_candidate_days,
        )
        payload["totals"] = metrics["totals"]
        payload["recent_catalogs"] = metrics["recent_catalogs"]
        payload["candidate_pool"] = metrics["candidate_pool"]
    except Exception:  # noqa: BLE001 - never expose DB details to /ops
        log.warning("timeline ops metrics unavailable")
        payload["metrics_error"] = "timeline_metrics_unavailable"
    try:
        catalog = await state.repo.current_explore_timeline_catalog(
            now=datetime.now(UTC),
            stale_hours=state.settings.timeline_stale_hours,
        )
        if catalog is not None:
            payload["current"] = {
                "id": catalog["id"],
                "timeline_status": catalog["timeline_status"],
                "generated_at": catalog["generated_at"],
                "valid_until": catalog["valid_until"],
                "model": catalog.get("model"),
                "candidate_slot": catalog.get("candidate_slot"),
                "cluster_slot": catalog.get("cluster_slot"),
                "published_timeline_count": catalog["published_timeline_count"],
                "readable_timeline_count": len(catalog["timelines"]),
                "timelines": [
                    {
                        "rank": timeline["rank"],
                        "stable_key": timeline["stable_key"],
                        "title": timeline["title"],
                        "topic": timeline["topic"],
                        "started_at": timeline["started_at"],
                        "ended_at": timeline["ended_at"],
                        "node_count": len(timeline["nodes"]),
                        "main_node_count": sum(
                            1
                            for node in timeline["nodes"]
                            if node["lane"] == "main"
                        ),
                        "main_day_count": len(
                            {
                                _timeline_ops_day(node["published_at_snapshot"])
                                for node in timeline["nodes"]
                                if node["lane"] == "main"
                            }
                        ),
                        "domain_count": len(
                            {node.get("domain") for node in timeline["nodes"]}
                        ),
                    }
                    for timeline in catalog["timelines"]
                ],
            }
    except Exception:  # noqa: BLE001 - the panel degrades, /ops stays up
        log.warning("timeline ops current edition unavailable")
        payload["current_error"] = "timeline_current_unavailable"
    vector = getattr(
        getattr(state, "article_embedding_worker", None), "vector", None
    )
    if vector is not None:
        try:
            snapshot = await vector.current_cluster_snapshot()
            if snapshot is not None:
                payload["cluster_snapshot"] = {
                    "id": snapshot.id,
                    "slot": snapshot.slot,
                    "algorithm_version": snapshot.algorithm_version,
                    "article_count": snapshot.article_count,
                    "indexed_count": snapshot.indexed_count,
                    "coverage": snapshot.coverage,
                    "cluster_count": len(snapshot.clusters),
                    "generated_at": snapshot.generated_at,
                    "valid_until": snapshot.valid_until,
                }
        except Exception:  # noqa: BLE001 - vector DB is optional for /ops
            log.warning("timeline ops cluster snapshot unavailable")
            payload["cluster_snapshot_error"] = "cluster_snapshot_unavailable"
    return payload


def _timeline_ops_day(value: datetime) -> str:
    return value.astimezone(TIMELINE_TIME_ZONE).date().isoformat()


@app.post("/ops/api/timelines/refresh", dependencies=[Depends(require_ops_token)])
async def ops_refresh_timelines():
    """Rebuild the timeline catalog now instead of waiting for the next slot.

    Clustering runs on a six-hour slot and publication on a four-hour one, both
    keyed by a unique slot so the schedule cannot pay twice.  That also means a
    code change only reaches Explore at the next boundary.  This operator-only
    trigger publishes into a slot the schedule never emits, so nothing stored is
    deleted or overwritten.  It spends one rerank batch and one copy call.
    """
    worker = getattr(state, "explore_timeline_worker", None)
    if worker is None or not state.settings.timeline_worker_enabled:
        raise HTTPException(status_code=503, detail="timeline_worker_disabled")
    if not bool(getattr(state.timeline_rerank, "configured", False)):
        raise HTTPException(status_code=503, detail="timeline_rerank_not_configured")
    try:
        result = await worker.run_once(force=True)
    except Exception:  # noqa: BLE001 - never expose provider or DB detail to /ops
        log.warning("manual timeline refresh failed")
        raise HTTPException(
            status_code=503,
            detail="timeline_refresh_unavailable",
        ) from None
    log.info("manual timeline refresh kind=%s", result.get("kind"))
    return {"result": result, "cluster": worker.last_cluster_run}


@app.get("/ops/api/analytics", dependencies=[Depends(require_ops_token)])
async def ops_analytics(
    daily_days: int = Query(default=90),
    monthly_months: int = Query(default=12),
):
    """Return bounded, aggregate-only member and content analytics."""
    if daily_days not in {30, 90, 365} or monthly_months not in {6, 12}:
        raise HTTPException(status_code=422, detail="invalid_ops_analytics_period")
    try:
        return await state.repo.ops_analytics(
            daily_days=daily_days,
            monthly_months=monthly_months,
        )
    except Exception:  # noqa: BLE001 - never expose DB details to /ops
        log.warning("ops analytics unavailable")
        raise HTTPException(
            status_code=503,
            detail="ops_analytics_unavailable",
        ) from None


@app.get("/ops/api/publisher-interest", dependencies=[Depends(require_ops_token)])
async def ops_publisher_interest(limit: int = Query(default=50, ge=1, le=200)):
    """The registry promotion queue for web-researched interests (DR-13248).

    Operator data, not reader data: it carries the evidence URLs behind each
    sentence so a publisher can be checked before being promoted into
    ``publishers.yaml``, after which the deterministic registry answers first
    and research stops running for that domain.
    """
    try:
        rows = await state.repo.list_publisher_interest_proposals(limit=limit)
    except Exception:  # noqa: BLE001 - never expose DB details to /ops
        log.warning("ops publisher interest unavailable")
        raise HTTPException(
            status_code=503,
            detail="ops_publisher_interest_unavailable",
        ) from None
    return {
        "mode": state.settings.publisher_research_mode,
        "daily_limit": state.settings.publisher_research_daily_limit,
        "proposals": [
            {
                "domain": row.get("domain"),
                "assets": row.get("assets_key"),
                "relation": row.get("relation"),
                "owner": row.get("owner"),
                "asset": row.get("asset"),
                "confidence": row.get("confidence"),
                "sentence": row.get("sentence"),
                "evidence": _interest_evidence(row.get("evidence")),
                "article_id": row.get("article_id"),
                "registry_status": row.get("registry_status"),
                "researched_at": row.get("researched_at"),
            }
            for row in rows
        ],
    }


def _interest_evidence(raw: Any) -> list[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [
        {"url": str(item.get("url") or ""), "quote": str(item.get("quote") or "")}
        for item in raw
        if isinstance(item, dict)
    ]


async def _article_embedding_snapshot(total_articles: int) -> dict:
    worker = getattr(state, "article_embedding_worker", None)
    if worker is None:
        return {
            "status": "unavailable",
            "embedded_count": None,
            "total_count": None,
            "percent": None,
            "updated_at": None,
        }
    try:
        return await worker.progress(total_articles=total_articles)
    except Exception:  # noqa: BLE001 - optional vector metric must not break /ops
        log.warning("article embedding ops metric unavailable")
        return {
            "status": "unavailable",
            "embedded_count": None,
            "total_count": None,
            "percent": None,
            "updated_at": None,
        }


async def _map_budget_snapshot() -> dict:
    """Live budget, today's per-bucket spend and triage coverage."""
    status = await state.repo.map_budget_status(
        default_limit=state.settings.map_daily_limit_default,
        default_share=state.settings.map_fresh_share_default,
        default_run_count=state.settings.map_daily_run_count_default,
    )
    status["enabled"] = state.settings.map_budget_enabled
    status["triage"] = {
        **status.get("triage", {}),
        "mode": state.settings.triage_mode,
    }
    status["candidate_ttl_days"] = state.settings.map_candidate_ttl_days
    return status


class DomainBudgetUpdate(BaseModel):
    """One coverage domain's share of the daily allowance (DR-13206)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    coverage_domain: Literal["core", "crypto"]
    daily_map_limit: int = Field(ge=0, le=1000)
    # None preserves an existing row so older ops clients cannot silently
    # reset the primary-document lane. A newly inserted row defaults to zero.
    document_map_limit: int | None = Field(default=None, ge=0, le=1000)


class MapBudgetUpdate(BaseModel):
    """The operator-editable Map budget and daily pacing values."""

    model_config = ConfigDict(extra="forbid", strict=True)

    daily_map_limit: int = Field(ge=0, le=1000)
    fresh_share_percent: int = Field(ge=0, le=100)
    daily_run_count: int = Field(ge=1, le=24)
    # Optional so an existing client that knows nothing about domains keeps
    # working: omitting it changes the global pacing and leaves the shares.
    domains: list[DomainBudgetUpdate] | None = None


@app.put("/ops/api/map-budget", dependencies=[Depends(require_ops_token)])
async def ops_update_map_budget(payload: MapBudgetUpdate):
    """Change the daily Map allowance; the next claim already uses it."""
    domains = payload.domains
    if domains is not None:
        names = [item.coverage_domain for item in domains]
        if len(names) != len(set(names)):
            raise HTTPException(status_code=400, detail="duplicate_coverage_domain")
        if sum(item.daily_map_limit for item in domains) > payload.daily_map_limit:
            raise HTTPException(
                status_code=400, detail="domain_limits_exceed_total"
            )
        for item in domains:
            document_limit = item.document_map_limit
            if (
                document_limit is not None
                and document_limit > item.daily_map_limit
            ):
                raise HTTPException(
                    status_code=400,
                    detail="document_map_limit_exceeds_domain_limit",
                )
            if item.coverage_domain == "core" and document_limit not in (None, 0):
                raise HTTPException(
                    status_code=400,
                    detail="core_document_map_limit_must_be_zero",
                )
    await state.repo.set_map_budget(
        daily_map_limit=payload.daily_map_limit,
        fresh_share_percent=payload.fresh_share_percent,
        daily_run_count=payload.daily_run_count,
        domains=(
            None
            if domains is None
            else [
                (
                    item.coverage_domain,
                    item.daily_map_limit,
                    item.document_map_limit,
                )
                for item in domains
            ]
        ),
    )
    return await _map_budget_snapshot()


# ---- manual trigger ----------------------------------------------------
@app.post("/refresh", dependencies=[Depends(require_token)])
async def refresh(
    job: Optional[str] = Query(
        default=None, description="run one named GDELT or NewsAPI job"
    )
):
    jobs = load_jobs(state.settings.jobs_file)
    if job:
        jobs = [j for j in jobs if j.name == job]
        if not jobs:
            raise HTTPException(404, f"job '{job}' not found")
    results = await state.engine.run_all(jobs)
    return {"results": [r.as_dict() for r in results]}


@app.post("/refresh/query", dependencies=[Depends(require_token)])
async def refresh_query(
    q: str = Query(..., description="ad-hoc GDELT query"),
    timespan: str = Query("1d"),
    max_records: int = Query(250, ge=1, le=250),
):
    """Fire a one-off query without editing jobs.yaml."""
    result = await state.engine.run_job(
        Job(name="adhoc", query=q, timespan=timespan, max_records=max_records)
    )
    return result.as_dict()


# ---- reads -------------------------------------------------------------
@app.get("/articles")
async def articles(
    limit: int = Query(50, ge=1, le=500),
    with_image: bool = Query(False, description="only articles that have a stored image"),
):
    return {"articles": await state.repo.recent_articles(limit, with_image_only=with_image)}


# ---- authenticated iOS/iPadOS app API ---------------------------------
async def signed_payload(request: Request, parsed: BaseModel) -> dict:
    """Return the payload object exactly as the client serialised it.

    An App Attest assertion binds ``canonical_json(payload)`` as the client
    encoded it. Re-serialising the parsed request model instead would bind
    server-side defaults, so every optional field added to a request model
    later silently invalidates the assertions of already-shipped app builds
    (they sign the old key set, the server hashes the new one, and every
    request fails with ``invalid_signature``). Hash what arrived on the wire;
    the validated model is still what the handler acts on.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError, ClientDisconnect):
        return parsed.model_dump(mode="json")
    if isinstance(body, dict) and isinstance(body.get("payload"), dict):
        return body["payload"]
    return parsed.model_dump(mode="json")


@app.post("/v1/account/apple", response_model=AccountSessionResponse)
async def account_apple_sign_in(body: AppleAccountSignInRequest):
    return await state.account_service.sign_in(
        identity_token=body.identity_token,
        authorization_code=body.authorization_code,
        nonce=body.nonce,
        given_name=body.given_name,
        family_name=body.family_name,
        privacy_consent_id=body.privacy_consent_id,
    )


@app.post(
    "/v1/app/privacy/consents",
    response_model=PrivacyConsentResponse,
)
async def app_privacy_consent(body: AuthenticatedPrivacyConsentRequest, request: Request):
    if body.payload.policy_version != state.settings.privacy_policy_version:
        raise AppClientError(409, "privacy_policy_version_outdated")
    payload = await signed_payload(request, body.payload)
    assertion = decode_base64url(body.auth.assertion, max_bytes=64 * 1024)
    client = await state.app_client_service.authenticate_privacy_consent(
        payload=payload,
        challenge_id=body.auth.challenge_id,
        key_id=body.auth.key_id,
        assertion_object=assertion,
        issued_at=body.auth.issued_at,
    )
    installation_id = str(body.payload.device.installation_id)
    if installation_id != client.installation_id:
        raise AppClientError(401, "privacy_consent_installation_mismatch")
    row = await state.repo.record_privacy_consent(
        consent_id=uuid4(),
        installation_id=installation_id,
        key_id=client.key_id,
        policy_version=state.settings.privacy_policy_version,
        policy_url=state.settings.privacy_policy_url,
        app_version=body.payload.device.app_version,
        build_number=body.payload.device.build_number,
    )
    if row is None:
        raise AppClientError(401, "attested_installation_not_active")
    return {
        "consent_id": row["id"],
        "policy_version": row["policy_version"],
        "policy_url": row["policy_url"],
        "accepted_at": row["accepted_at"],
    }


@app.post(
    "/v1/app/article-views",
    response_model=ArticleViewResponse,
)
async def app_article_view(body: AuthenticatedArticleViewRequest, request: Request):
    payload = await signed_payload(request, body.payload)
    assertion = decode_base64url(body.auth.assertion, max_bytes=64 * 1024)
    client = await state.app_client_service.authenticate_article_view(
        payload=payload,
        challenge_id=body.auth.challenge_id,
        key_id=body.auth.key_id,
        assertion_object=assertion,
        issued_at=body.auth.issued_at,
    )
    await _record_guest_session_activity(client.installation_id)
    try:
        row = await state.repo.record_article_view(
            event_id=body.payload.event_id,
            article_id=body.payload.article_id,
        )
    except Exception as exc:
        log.exception("Article view persistence failed")
        raise AppClientError(503, "article_view_unavailable") from exc
    if row is None:
        raise AppClientError(404, "article_not_found")
    return ArticleViewResponse(**row)


@app.get("/v1/account/me", response_model=AccountProfile)
async def account_me(authorization: str | None = Header(default=None)):
    return await state.account_service.me(extract_bearer(authorization))


@app.get(
    "/v1/account/reading-state",
    response_model=AccountReadingStateResponse,
)
async def account_reading_state(
    request: Request,
    authorization: str | None = Header(default=None),
):
    return await state.account_service.reading_state(
        bearer_token=extract_bearer(authorization),
        request_base_url=str(request.base_url),
    )


@app.patch(
    "/v1/account/reading-state",
    response_model=AccountReadingStateResponse,
)
async def account_reading_state_patch(
    body: AccountReadingStatePatch,
    request: Request,
    authorization: str | None = Header(default=None),
):
    return await state.account_service.patch_reading_state(
        bearer_token=extract_bearer(authorization),
        patch=body.model_dump(mode="python"),
        request_base_url=str(request.base_url),
    )


@app.post(
    "/v1/account/activity-events",
    response_model=AccountActivityEventBatchResponse,
)
async def account_activity_events(
    body: AccountActivityEventBatch,
    authorization: str | None = Header(default=None),
):
    return await state.account_service.submit_activity_events(
        bearer_token=extract_bearer(authorization),
        events=[event.model_dump(mode="python") for event in body.events],
    )


@app.post(
    "/v1/account/recommendations",
    response_model=AccountRecommendationResponse,
)
async def account_recommendations(
    body: AccountRecommendationRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    return await state.account_service.recommendations(
        bearer_token=extract_bearer(authorization),
        request=body.model_dump(mode="python"),
        request_base_url=str(request.base_url),
    )


@app.post(
    "/v1/account/privacy-consents/{consent_id}",
    response_model=AccountPrivacyConsentLink,
)
async def account_privacy_consent_link(
    consent_id: UUID,
    authorization: str | None = Header(default=None),
):
    return await state.account_service.link_privacy_consent(
        bearer_token=extract_bearer(authorization),
        consent_id=consent_id,
    )


@app.post("/v1/account/logout", status_code=204)
async def account_logout(authorization: str | None = Header(default=None)):
    await state.account_service.logout(extract_bearer(authorization))
    return Response(status_code=204)


@app.delete("/v1/account", response_model=AccountDeletionResponse)
async def account_delete(authorization: str | None = Header(default=None)):
    return await state.account_service.delete(extract_bearer(authorization))


@app.put(
    "/v1/account/article-feedback/{article_id}",
    response_model=ArticleFeedbackResponse,
)
async def account_article_feedback(
    body: ArticleFeedbackRequest,
    article_id: str = APIPath(min_length=1, max_length=128),
    authorization: str | None = Header(default=None),
):
    return await state.account_service.submit_article_feedback(
        bearer_token=extract_bearer(authorization),
        article_id=article_id,
        reason=body.reason.value,
    )


@app.get(
    "/v1/account/timeline-subscriptions",
    response_model=TimelineSubscriptionListResponse,
)
async def account_timeline_subscriptions(
    authorization: str | None = Header(default=None),
):
    return await state.account_service.list_timeline_subscriptions(
        bearer_token=extract_bearer(authorization)
    )


@app.get(
    "/v1/account/timeline-subscriptions/history",
    response_model=TimelineSubscriptionHistoryResponse,
)
async def account_timeline_subscription_history(
    authorization: str | None = Header(default=None),
):
    return await state.account_service.timeline_subscription_history(
        bearer_token=extract_bearer(authorization)
    )


@app.put(
    "/v1/account/timeline-subscriptions/{stable_key}",
    response_model=TimelineSubscriptionResponse,
)
async def account_timeline_subscribe(
    stable_key: str = APIPath(
        min_length=1,
        max_length=128,
        pattern=r"^[^/\x00-\x1f\x7f]+$",
    ),
    authorization: str | None = Header(default=None),
):
    return await state.account_service.subscribe_timeline(
        bearer_token=extract_bearer(authorization),
        stable_key=stable_key,
    )


@app.delete(
    "/v1/account/timeline-subscriptions/{stable_key}",
    status_code=204,
)
async def account_timeline_unsubscribe(
    stable_key: str = APIPath(
        min_length=1,
        max_length=128,
        pattern=r"^[^/\x00-\x1f\x7f]+$",
    ),
    authorization: str | None = Header(default=None),
):
    await state.account_service.unsubscribe_timeline(
        bearer_token=extract_bearer(authorization),
        stable_key=stable_key,
    )
    return Response(status_code=204)


@app.post(
    "/v1/app/push/registrations",
    response_model=PushRegistrationResponse,
)
async def app_push_register(
    body: AuthenticatedPushRegistrationRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    payload = await signed_payload(request, body.payload)
    assertion = decode_base64url(body.auth.assertion, max_bytes=64 * 1024)
    client = await state.app_client_service.authenticate_push_registration(
        payload=payload,
        challenge_id=body.auth.challenge_id,
        key_id=body.auth.key_id,
        assertion_object=assertion,
        issued_at=body.auth.issued_at,
    )
    installation_id = str(body.payload.installation_id)
    if (
        installation_id != client.installation_id
        or body.payload.device.installation_id != body.payload.installation_id
    ):
        raise AppClientError(401, "push_installation_mismatch")
    token = decode_base64url(body.payload.device_token, max_bytes=256)
    return await state.account_service.register_push(
        bearer_token=extract_bearer(authorization),
        installation_id=installation_id,
        key_id=client.key_id,
        device_token=token,
        environment=body.payload.environment,
        preferences=body.payload.preferences.model_dump(mode="python"),
    )


@app.patch(
    "/v1/app/push/registrations/{installation_id}",
    response_model=PushRegistrationResponse,
)
async def app_push_preferences(
    body: PushPreferences,
    installation_id: UUID,
    authorization: str | None = Header(default=None),
):
    return await state.account_service.update_push_preferences(
        bearer_token=extract_bearer(authorization),
        installation_id=installation_id,
        preferences=body.model_dump(mode="python"),
    )


@app.delete("/v1/app/push/registrations/{installation_id}", status_code=204)
async def app_push_unregister(
    installation_id: UUID,
    authorization: str | None = Header(default=None),
):
    await state.account_service.unregister_push(
        bearer_token=extract_bearer(authorization),
        installation_id=installation_id,
    )
    return Response(status_code=204)


@app.post("/v1/app/attest/challenge", response_model=ChallengeResponse)
async def app_attest_challenge(body: ChallengeRequest):
    return await state.app_client_service.issue_challenge(
        purpose=body.purpose,
        key_id=body.key_id,
        device=body.device.model_dump(mode="json"),
    )


@app.post("/v1/app/attest/register", response_model=RegisterResponse)
async def app_attest_register(
    body: RegisterRequest,
    authorization: str | None = Header(default=None),
):
    attestation = decode_base64url(body.attestation_object, max_bytes=128 * 1024)
    recovery_account_id = None
    recovery_session_id = None
    if authorization is not None:
        recovery_account_id, recovery_session_id = (
            await state.account_service.app_attest_recovery_identity(
                extract_bearer(authorization)
            )
        )
    return await state.app_client_service.register(
        challenge_id=body.challenge_id,
        key_id=body.key_id,
        attestation_object=attestation,
        device=body.device.model_dump(mode="json"),
        recovery_account_id=recovery_account_id,
        recovery_session_id=recovery_session_id,
    )


async def _record_anonymous_article_list_impressions(
    article_ids: list[str], *, error_code: str
) -> None:
    if not article_ids:
        return
    try:
        recorded = await state.repo.record_article_list_impressions(
            article_ids=article_ids,
            served_at=datetime.now(UTC),
            account_id=None,
        )
    except Exception as exc:
        log.warning(
            "anonymous article-list impression unavailable code=%s reason=%s",
            error_code,
            type(exc).__name__,
        )
        raise AppClientError(503, error_code) from exc
    if recorded != len(article_ids):
        log.warning(
            "anonymous article-list impression count mismatch code=%s expected=%d recorded=%d",
            error_code,
            len(article_ids),
            recorded,
        )
        raise AppClientError(503, error_code)


async def _record_guest_session_activity(installation_id: str) -> None:
    """Record a verified guest installation without retaining its raw ID."""
    try:
        await state.repo.record_guest_session_activity(
            installation_id=installation_id,
            observed_at=datetime.now(UTC),
        )
    except Exception as exc:
        log.warning("guest session activity unavailable reason=%s", type(exc).__name__)
        raise AppClientError(503, "guest_activity_unavailable") from exc


async def _annotate_crypto_items(
    items: list[dict],
    *,
    saved_at: dict | None = None,
) -> list[dict]:
    """Attach the read-time crypto annotations, or leave the items untouched.

    Staleness has to be computed per request because it states what a number is
    now; revisions and chain progress are cheap reads that only exist for
    articles inside an incident chain. All three are enrichment: any failure
    here degrades to a plain article rather than to a failed response.
    """
    if not items:
        return items
    values: dict = {}
    try:
        subjects = collect_staleness_subjects(items)
        if subjects and state.market_data is not None:
            values = await state.market_data.current(subjects)
    except Exception:  # noqa: BLE001 - never fail a reader response
        log.warning("staleness lookup unavailable")
    # Attached unconditionally: the field's presence is a client contract, and
    # a provider outage must leave it empty rather than missing.
    attach_staleness_notes(items, values, now=datetime.now(UTC))

    article_ids = [
        str(item.get("article_id") or "")
        for item in items
        if isinstance(item, dict) and item.get("article_id")
    ]
    # get_settings() rather than state.settings: this runs on the reader path,
    # which must not depend on lifespan having populated state.
    if not article_ids or not get_settings().event_chain_annotations_enabled:
        return items
    try:
        chain_ids = await state.repo.chain_ids_for_articles(article_ids)
        revisions: list = []
        for chain_id in dict.fromkeys(chain_ids.values()):
            revisions.extend(build_revisions(await state.repo.chain_articles(chain_id)))
        attach_revision_notes(items, revisions)
        if saved_at:
            attach_chain_progress(
                items,
                saved_at=saved_at,
                chain_joins=await state.repo.chain_join_times(article_ids),
            )
    except Exception:  # noqa: BLE001 - never fail a reader response
        log.warning("incident chain annotation unavailable")
    return items



@app.post("/v1/app/feed", response_model=FeedResponse)
async def app_feed(body: AuthenticatedFeedRequest, request: Request):
    payload = await signed_payload(request, body.payload)
    try:
        cursor = decode_cursor(body.payload.cursor)
    except InvalidFeedCursor as exc:
        raise AppClientError(400, "invalid_cursor") from exc
    assertion = decode_base64url(body.auth.assertion, max_bytes=64 * 1024)
    is_random = body.payload.mode.value == "random"
    is_latest = body.payload.mode.value == "latest"
    authentication = {
        "payload": payload,
        "challenge_id": body.auth.challenge_id,
        "key_id": body.auth.key_id,
        "assertion_object": assertion,
        "issued_at": body.auth.issued_at,
        "cooldown_s": (
            state.settings.app_feed_random_cooldown_s
            if is_random
            else (
                state.settings.app_feed_cooldown_s
                if cursor is None
                else state.settings.app_feed_continuation_cooldown_s
            )
        ),
    }
    if is_random or is_latest:
        authentication["cooldown_scope"] = (
            "feed_random" if is_random else "feed_latest"
        )
    client = await state.app_client_service.authenticate_feed(
        **authentication,
    )
    await _record_guest_session_activity(client.installation_id)
    if is_random:
        rows = await state.repo.random_article_maps(
            limit=body.payload.limit,
            excluded_article_ids=body.payload.exclude_article_ids,
            random_seed=secrets.token_hex(16),
        )
        try:
            items = [
                item
                for row in rows
                if (
                    item := build_feed_item(
                        row,
                        settings=state.settings,
                        request_base_url=str(request.base_url),
                    )
                ) is not None
            ]
        except ValueError as exc:
            raise AppClientError(503, "invalid_public_base_url") from exc
        items = await _annotate_crypto_items(items)
        response = FeedResponse(
            items=items,
            next_cursor=None,
            cooldown_seconds=state.settings.app_feed_random_cooldown_s,
        )
        await _record_anonymous_article_list_impressions(
            [item.article_id for item in response.items],
            error_code="feed_impression_unavailable",
        )
        return response
    if is_latest:
        rows = await state.repo.latest_article_maps(
            limit=body.payload.limit,
            excluded_article_ids=body.payload.exclude_article_ids,
        )
        try:
            items = [
                item
                for row in rows
                if (
                    item := build_feed_item(
                        row,
                        settings=state.settings,
                        request_base_url=str(request.base_url),
                    )
                ) is not None
            ]
        except ValueError as exc:
            raise AppClientError(503, "invalid_public_base_url") from exc
        items = await _annotate_crypto_items(items)
        response = FeedResponse(
            items=items,
            next_cursor=None,
            cooldown_seconds=state.settings.app_feed_cooldown_s,
        )
        await _record_anonymous_article_list_impressions(
            [item.article_id for item in response.items],
            error_code="feed_impression_unavailable",
        )
        return response
    editorial_day = cursor.editorial_day if cursor and cursor.editorial_day else seoul_editorial_day()
    rows = await state.repo.recent_article_maps(
        limit=body.payload.limit + 1,
        cursor_sort_at=cursor.sort_at if cursor else None,
        cursor_article_id=cursor.article_id if cursor else None,
        editorial_day=editorial_day,
        cursor_priority_bucket=cursor.priority_bucket if cursor else 1,
        cursor_featured_bucket=cursor.featured_bucket if cursor else 1,
        cursor_order_bucket=cursor.order_bucket if cursor else 1,
        cursor_display_order=cursor.display_order if cursor else 2_147_483_647,
    )
    try:
        items, next_cursor = build_feed_page(
            rows,
            limit=body.payload.limit,
            settings=state.settings,
            request_base_url=str(request.base_url),
            editorial_day=editorial_day,
        )
    except ValueError as exc:
        raise AppClientError(503, "invalid_public_base_url") from exc
    items = await _annotate_crypto_items(items)
    response = FeedResponse(
        items=items,
        next_cursor=next_cursor,
        cooldown_seconds=state.settings.app_feed_cooldown_s,
    )
    await _record_anonymous_article_list_impressions(
        [item.article_id for item in response.items],
        error_code="feed_impression_unavailable",
    )
    return response


def _featured_timeline_payload(
    raw_timeline: dict,
    *,
    request_base_url: str,
) -> Optional[dict]:
    """Project one stored timeline edition, or None when it is no longer usable.

    A node whose Map is gone cannot be rendered, but that is a reason to drop
    that node — and at worst that one timeline — not the whole Explore catalog.
    """
    nodes: list[dict] = []
    for raw_node in raw_timeline["nodes"]:
        article = build_feed_item(
            {
                "article_id": raw_node["article_id"],
                "source_url": raw_node.get("source_url"),
                "domain": raw_node.get("domain"),
                "published_at": raw_node.get("published_at"),
                "source_language": raw_node.get("source_language"),
                "sourcecountry": raw_node.get("sourcecountry"),
                "document_type": raw_node.get("document_type"),
                "document_source_name": raw_node.get("document_source_name"),
                "status": raw_node.get("map_status"),
                "data": raw_node.get("data"),
            },
            settings=state.settings,
            request_base_url=request_base_url,
        )
        if article is None:
            continue
        nodes.append(
            {
                "node_id": raw_node["node_id"],
                "lane": raw_node["lane"],
                "anchor_node_id": raw_node.get("anchor_node_id"),
                "update_kind": raw_node["update_kind"],
                "published_at_snapshot": raw_node["published_at_snapshot"],
                "date_source": raw_node["date_source"],
                "article": article,
            }
        )
    repaired = repaired_timeline_nodes(nodes)
    if repaired is None:
        return None
    return {
        "id": raw_timeline["id"],
        "rank": raw_timeline["rank"],
        "stable_key": raw_timeline["stable_key"],
        "title": raw_timeline["title"],
        "summary": raw_timeline["summary"],
        "topic": raw_timeline["topic"],
        "started_at": raw_timeline["started_at"],
        "ended_at": raw_timeline["ended_at"],
        "nodes": [
            {
                "id": node["node_id"],
                "lane": node["lane"],
                "anchor_node_id": node["anchor_node_id"],
                "update_kind": node["update_kind"],
                "published_at": node["published_at_snapshot"],
                "date_source": node["date_source"],
                "article": node["article"],
            }
            for node in repaired
        ],
    }


@app.post("/v1/app/discovery", response_model=DiscoveryResponse)
async def app_discovery(body: AuthenticatedDiscoveryRequest, request: Request):
    payload = await signed_payload(request, body.payload)
    assertion = decode_base64url(body.auth.assertion, max_bytes=64 * 1024)
    query = body.payload.query
    collection = body.payload.collection
    try:
        cursor = decode_cursor(body.payload.cursor)
    except InvalidFeedCursor as exc:
        raise AppClientError(400, "invalid_cursor") from exc
    client = await state.app_client_service.authenticate_discovery(
        payload=payload,
        challenge_id=body.auth.challenge_id,
        key_id=body.auth.key_id,
        assertion_object=assertion,
        issued_at=body.auth.issued_at,
        query_present=query is not None or collection is not None,
    )
    await _record_guest_session_activity(client.installation_id)
    query_embedding: list[float] | None = None
    rag_worker = None
    if query is not None:
        rag_worker = getattr(state, "article_embedding_worker", None)
        if rag_worker is not None:
            try:
                query_embedding = await rag_worker.embed_search_query(query)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - RAG is an additive enhancement
                log.warning("discovery RAG skipped code=query_embedding_failed")
    trending_search_suggestions: list[str] = []
    try:
        async with asyncio.timeout(1.2):
            trending_snapshot = await state.repo.current_trending_search_snapshot(
                as_of=datetime.now(UTC)
            )
        trending_search_suggestions = list(trending_snapshot.suggestions)
    except Exception as error:  # noqa: BLE001 - optional discovery enhancement
        log.warning(
            "trending discovery read failed error=%s",
            type(error).__name__,
        )
    timeline_catalog = None
    featured_timelines: list[dict] = []
    timeline_results: list[dict] = []
    if query is None and collection is None:
        try:
            timeline_catalog = await state.repo.current_explore_timeline_catalog(
                now=datetime.now(UTC),
                stale_hours=state.settings.timeline_stale_hours,
            )
            if timeline_catalog is not None:
                for raw_timeline in timeline_catalog["timelines"]:
                    payload = _featured_timeline_payload(
                        raw_timeline,
                        request_base_url=str(request.base_url),
                    )
                    if payload is None:
                        log.warning(
                            "timeline dropped code=timeline_items_unavailable rank=%s",
                            raw_timeline.get("rank"),
                        )
                        continue
                    payload["rank"] = len(featured_timelines) + 1
                    featured_timelines.append(payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - optional precomputed feature
            timeline_catalog = None
            featured_timelines = []
            log.warning(
                "timeline discovery read failed error=%s",
                type(error).__name__,
            )
    if query is not None:
        try:
            raw_results = await state.repo.search_explore_timelines(
                query=query,
                limit=8,
            )
            for raw_timeline in raw_results:
                payload = _featured_timeline_payload(
                    raw_timeline,
                    request_base_url=str(request.base_url),
                )
                if payload is None:
                    continue
                payload["rank"] = len(timeline_results) + 1
                timeline_results.append(payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - additive search enhancement
            timeline_results = []
            log.warning(
                "timeline search read failed error=%s",
                type(error).__name__,
            )
    try:
        catalog = await state.repo.current_curation_catalog()
        if collection is not None:
            search_rows = await state.repo.published_editorial_maps(
                publication_kind=collection,
                limit=body.payload.limit + 1,
                cursor_published_at=cursor.sort_at if cursor else None,
                cursor_article_id=cursor.article_id if cursor else None,
            )
        elif query is not None:
            search_rows = await state.repo.search_article_maps(
                query=query,
                limit=body.payload.limit,
            )
        else:
            search_rows = []
        routes: list[dict] = []
        representative_route_id = None
        if catalog is not None:
            representatives = [
                route for route in catalog["routes"] if route["is_representative"]
            ]
            if len(representatives) != 1:
                raise ValueError("invalid representative route contract")
            representative_route_id = representatives[0]["id"]
            for route in catalog["routes"]:
                route_items: list[dict] = []
                for raw_item in route["items"]:
                    article = build_feed_item(
                        {
                            "article_id": raw_item["article_id"],
                            "source_url": raw_item.get("source_url"),
                            "domain": raw_item.get("domain"),
                            "published_at": raw_item.get("published_at"),
                            "source_language": raw_item.get("source_language"),
                            "sourcecountry": raw_item.get("sourcecountry"),
                            "document_type": raw_item.get("document_type"),
                            "document_source_name": raw_item.get("document_source_name"),
                            "status": raw_item.get("map_status"),
                            "data": raw_item.get("data"),
                        },
                        settings=state.settings,
                        request_base_url=str(request.base_url),
                    )
                    if article is None:
                        raise ValueError("curation item map missing")
                    route_items.append(
                        {
                            "position": raw_item["position"],
                            "role": raw_item["role"],
                            "reason": raw_item["reason"],
                            "lasting_value": raw_item["lasting_value"],
                            "article": article,
                        }
                    )
                routes.append(
                    {
                        "id": route["id"],
                        "rank": route["rank"],
                        "slug": route["slug"],
                        "title": route["title"],
                        "summary": route["summary"],
                        "topic": route["topic"],
                        "is_representative": route["is_representative"],
                        "items": route_items,
                    }
                )
        selected_search_rows = search_rows[: body.payload.limit]
        results: list[dict] = []
        for row in selected_search_rows:
            article = build_feed_item(
                row,
                settings=state.settings,
                request_base_url=str(request.base_url),
            )
            if article is None:
                continue
            scenes = row.get("data", {}).get("scenes")
            results.append(
                {
                    "depth": (
                        "quick" if isinstance(scenes, list) and len(scenes) <= 4 else "deep"
                    ),
                    "retrieval": "keyword",
                    "article": article,
                }
            )
        if query is not None and query_embedding is not None and rag_worker is not None:
            try:
                keyword_ids = [str(row["article_id"]) for row in selected_search_rows]
                hits = await rag_worker.nearest_search_articles(
                    query_embedding,
                    exclude_ids=keyword_ids,
                )
                rag_ids: list[str] = []
                seen_ids = set(keyword_ids)
                for hit in hits:
                    article_id = str(getattr(hit, "article_id", ""))
                    if not article_id or article_id in seen_ids:
                        continue
                    seen_ids.add(article_id)
                    rag_ids.append(article_id)
                    if len(rag_ids) >= state.settings.article_search_rag_limit:
                        break
                if rag_ids:
                    rag_rows = await state.repo.published_article_maps_by_ids(
                        article_ids=rag_ids,
                    )
                    for row in rag_rows:
                        article = build_feed_item(
                            row,
                            settings=state.settings,
                            request_base_url=str(request.base_url),
                        )
                        if article is None:
                            continue
                        scenes = row.get("data", {}).get("scenes")
                        results.append(
                            {
                                "depth": (
                                    "quick"
                                    if isinstance(scenes, list) and len(scenes) <= 4
                                    else "deep"
                                ),
                                "retrieval": "rag",
                                "article": article,
                            }
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - preserve keyword-only success
                log.warning("discovery RAG skipped code=retrieval_failed")
        next_cursor = None
        if collection is not None and len(search_rows) > body.payload.limit:
            last = selected_search_rows[-1]
            next_cursor = encode_cursor(
                FeedCursor(sort_at=last["sort_at"], article_id=last["article_id"])
            )
    except Exception as error:  # noqa: BLE001 - app receives stable redacted code
        log.warning("discovery read failed error=%s", type(error).__name__)
        raise AppClientError(503, "discovery_unavailable") from error

    cooldown = (
        state.settings.app_discovery_search_cooldown_s
        if query is not None or collection is not None
        else state.settings.app_discovery_cooldown_s
    )
    response = DiscoveryResponse(
        generated_at=catalog.get("generated_at") if catalog else None,
        expires_at=catalog.get("valid_until") if catalog else None,
        representative_route_id=representative_route_id,
        routes=routes,
        search_suggestions=(catalog.get("search_suggestions", []) if catalog else []),
        trending_search_suggestions=trending_search_suggestions,
        query=query,
        collection=collection,
        results=results,
        timeline_status=(
            timeline_catalog.get("timeline_status", "unavailable")
            if timeline_catalog
            else "unavailable"
        ),
        timelines_generated_at=(
            timeline_catalog.get("generated_at") if timeline_catalog else None
        ),
        timelines_refreshed_at=(
            timeline_catalog.get("updated_at") if timeline_catalog else None
        ),
        featured_timelines=featured_timelines,
        timeline_results=timeline_results,
        next_cursor=next_cursor,
        cooldown_seconds=cooldown,
    )
    discovery_article_ids = [
        item.article.article_id
        for route in response.routes
        for item in route.items
    ]
    discovery_article_ids.extend(
        result.article.article_id
        for result in response.results
    )
    discovery_article_ids.extend(
        node.article.article_id
        for timeline in response.featured_timelines
        for node in timeline.nodes
    )
    discovery_article_ids.extend(
        node.article.article_id
        for timeline in response.timeline_results
        for node in timeline.nodes
    )
    await _record_anonymous_article_list_impressions(
        discovery_article_ids,
        error_code="discovery_impression_unavailable",
    )
    return response


def _image_url_for(image_id: str) -> str:
    """Keep storage credentials and expiring URLs out of persisted map data."""
    return f"/images/{image_id}"


# ---- maps --------------------------------------------------------------
@app.get("/articles/{article_id}/map")
async def get_map(article_id: str, resolved: bool = Query(True)):
    """Load the processed map. When resolved=true, <img:N> placeholders are
    rewritten into server image references so the client can render directly."""
    row = await state.repo.get_map_row(article_id)
    if row is None:
        raise HTTPException(404, "map not found (not processed yet)")
    if row["status"] == "failed" or not row.get("data"):
        return {"article_id": article_id, "status": row["status"], "error": row.get("error")}
    if not resolved:
        return {"article_id": article_id, "status": row["status"], "map": row["data"]}
    links = await state.repo.image_links(article_id)
    view = resolve_map(row["data"], _image_url_for, links=links)
    return {"article_id": article_id, "status": row["status"], "map": view}


@app.post("/articles/{article_id}/map", dependencies=[Depends(require_token)])
async def make_map(article_id: str):
    """Generate (or regenerate) the map for one already-loaded article."""
    from .engine import _RowArticle

    row = await state.repo.get_article(article_id)
    if row is None:
        raise HTTPException(404, "article not found")
    art = _RowArticle(
        id=row["id"], url=row["url"], title=row.get("title"),
        domain=row.get("domain"), socialimage_url=row.get("socialimage_url"),
        coverage_domain=str(row.get("coverage_domain") or "core"),
        document_type=row.get("document_type"),
        document_source_name=row.get("document_source_name"),
    )
    if (
        not state.settings.enable_mapping
        or not state.settings.has_map_provider_key
    ):
        raise HTTPException(400, "mapping disabled or Map provider key not set")
    outcomes = await state.engine.map_article(
        art,
        force=True,
        reset_pipeline=True,
    )
    serialized_outcome = asdict(outcomes[0]) if outcomes else None
    if serialized_outcome is not None:
        # Stable failure codes are internal telemetry; preserve the public
        # manual-Map response contract used by existing operator clients.
        serialized_outcome.pop("error_code", None)
        serialized_outcome.pop("provider", None)
    return {"article_id": article_id, "outcome": serialized_outcome}


@app.post("/map/pending", dependencies=[Depends(require_token)])
async def map_pending(limit: int = Query(50, ge=1, le=500)):
    """Backfill: map articles that don't have a successful map yet."""
    return await state.engine.map_pending(limit)


@app.get("/queue")
async def queue_status(items: bool = Query(False)):
    """Persistent Map-work status: counts and optional recent items."""
    out: dict = {"stats": await state.repo.queue_stats()}
    if items:
        out["items"] = await state.repo.queue_items(50)
    return out


@app.post("/queue/drain", dependencies=[Depends(require_token)])
async def queue_drain(limit: int = Query(50, ge=1, le=500)):
    """Process due persistent Map work in strict FIFO order now."""
    return await state.engine.process_queue(limit)


@app.get("/images/{image_id}")
async def image(image_id: str):
    """Stream a legacy DB image or redirect a private S3 object."""
    rec = await state.repo.get_image_record(image_id)
    if rec is None:
        raise HTTPException(404, "image not found")
    backend = rec["backend"]
    ctype = rec["content_type"] or "application/octet-stream"
    if backend == "db" and rec["data"] is not None:
        return StreamingResponse(io.BytesIO(rec["data"]), media_type=ctype)
    if backend == "s3":
        key = rec["storage_ref"]
        if not key or state.object_store is None:
            raise HTTPException(503, "image_storage_invalid")
        try:
            presigned_url = state.object_store.presign_get(key)
        except (ObjectStorageError, ValueError):
            log.error("image presign failed for %s", image_id)
            raise HTTPException(503, "image_storage_unavailable") from None
        return RedirectResponse(
            presigned_url,
            status_code=307,
            headers={
                "Cache-Control": (
                    f"public, max-age={state.settings.s3_redirect_cache_s}"
                )
            },
        )
    if backend == "local":
        raw_path = rec["storage_ref"]
        if not raw_path:
            raise HTTPException(503, "image_storage_invalid")
        path = Path(raw_path).resolve()
        root = Path(state.settings.image_local_dir).resolve()
        if root != path.parent or not path.is_file():
            raise HTTPException(503, "image_storage_invalid")
        return FileResponse(path, media_type=ctype)
    raise HTTPException(503, "image_storage_invalid")
