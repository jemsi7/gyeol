"""Turn a stored article into a grounded Delivery Map v3.

Per article:
  1. extract body text + candidate image URLs from the live page
  2. build the image set (GDELT social image first, then in-body images),
     download+store each, assign a per-article ref index, persist the links
  3. run evidence -> combined brief+plan -> scene batches -> critic/repair
  4. compile and validate Delivery Map v3 deterministically
  5. persist the map (article_maps)

The result body_markdown carries <img:N> placeholders; each N resolves through
article_image_links -> article_images, so a reader loads the map from text +
server-side image references.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

import httpx

from .cerebras import (
    CerebrasClient,
    CerebrasPermanentError,
    CerebrasTransientError,
)
from .config import Settings
from .extractor import ArticleExtractor
from .images import ImageFetcher, StoredRef
from .editorial_pipeline import (
    EditorialPipeline,
    PipelineStageError,
    build_source_packet,
    validate_source_packet,
)
from .mapping_schema import MAP_FORMAT_VERSION
from .models import Article
from .primary_documents import select_sections
from .openrouter import (
    ImagePart,
    OpenRouterClient,
    OpenRouterPermanentError,
    OpenRouterRateLimiter,
    OpenRouterTransientError,
)
from .repository import Repository
from .telemetry import NullWorkerTelemetry, WorkerTelemetryProtocol

log = logging.getLogger("gdelt.mapper")


def permanent_provider_reason(error: BaseException) -> str | None:
    """Return the already-redacted permanent provider reason in a cause chain."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (OpenRouterPermanentError, CerebrasPermanentError),
        ):
            return str(current)
        current = current.__cause__ or current.__context__
    return None


def transient_provider_failure(error: BaseException) -> bool:
    """Return whether a cause chain represents a recoverable provider attempt."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                httpx.TransportError,
                OpenRouterTransientError,
                CerebrasTransientError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(slots=True)
class MapOutcome:
    article_id: str
    status: str  # ok | retry | dead | failed
    n_images: int = 0
    error: Optional[str] = None
    error_code: Optional[str] = None
    provider: Optional[str] = None


@dataclass(frozen=True, slots=True)
class MapProvider:
    name: Literal["openrouter", "cerebras"]
    model: str
    max_images: int
    request_limiter: OpenRouterRateLimiter | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.model.strip() or self.max_images < 0:
            raise ValueError("invalid Map provider configuration")

    @property
    def identity(self) -> str:
        return f"{self.name}:{self.model.strip()}"


@dataclass(slots=True)
class _ImageSlot:
    ref: int
    image_id: str
    source_url: str
    role: str
    part: ImagePart = field(repr=False, default=None)  # type: ignore[assignment]


class Mapper:
    """Reusable single-article mapping core with pooled HTTP clients."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        telemetry: WorkerTelemetryProtocol | None = None,
        image_rag: object | None = None,
        request_limiter: OpenRouterRateLimiter | None = None,
        provider: MapProvider | None = None,
        allow_provider_failover: bool = False,
    ):
        self.s = settings
        self.repo = repo
        self.telemetry = telemetry or NullWorkerTelemetry()
        self.image_rag = image_rag
        self.provider = provider or MapProvider(
            name="openrouter",
            model=settings.openrouter_model,
            max_images=settings.llm_max_images,
            request_limiter=request_limiter,
        )
        self.allow_provider_failover = allow_provider_failover
        self._extractor: Optional[ArticleExtractor] = None
        self._images: Optional[ImageFetcher] = None
        self._llm: OpenRouterClient | CerebrasClient | None = None
        self._permanent_provider_error: str | None = None

    async def __aenter__(self) -> "Mapper":
        self._extractor = await ArticleExtractor(self.s).__aenter__()
        self._images = await ImageFetcher(
            self.s, self.repo, telemetry=self.telemetry
        ).__aenter__()
        if self.provider.name == "openrouter":
            llm = OpenRouterClient(
                self.s,
                request_limiter=self.provider.request_limiter,
                model=self.provider.model,
            )
        elif self.provider.name == "cerebras":
            llm = CerebrasClient(
                self.s,
                request_limiter=self.provider.request_limiter,
                model=self.provider.model,
            )
        else:  # pragma: no cover - MapProvider validates the finite name
            raise ValueError(f"unsupported Map provider: {self.provider.name}")
        self._llm = await llm.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        for c in (self._extractor, self._images, self._llm):
            if c is not None:
                await c.__aexit__(*exc)

    @property
    def enabled(self) -> bool:
        if self.provider.name == "openrouter":
            configured = bool(self.s.openrouter_api_key.strip())
        else:
            configured = bool(self.s.cerebras_api_key.strip())
        return self.s.enable_mapping and configured

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def provider_available(self) -> bool:
        return self._permanent_provider_error is None

    async def _commit_budget_claim(self, article_id: str) -> bool:
        commit = getattr(self.repo, "commit_map_budget_claim", None)
        return bool(await commit(article_id)) if commit is not None else False

    async def _release_budget_claim(self, article_id: str, reason: str) -> bool:
        release = getattr(self.repo, "release_map_budget_claim", None)
        if release is None:
            return False
        return bool(await release(article_id, reason=reason))

    async def _event(
        self,
        article_id: str,
        *,
        stage: str,
        outcome: str,
        error_code: str | None = None,
        error_detail: str | None = None,
        retryable: bool = False,
        metadata: dict | None = None,
    ) -> None:
        append = getattr(self.repo, "append_map_attempt_event", None)
        if append is None:
            return
        try:
            await append(
                article_id,
                stage=stage,
                outcome=outcome,
                error_code=error_code,
                error_detail=error_detail,
                retryable=retryable,
                model=self.provider.identity,
                structured_output=bool(
                    getattr(self._llm, "structured_outputs_enabled", False)
                ),
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 - telemetry is not a correctness gate
            log.warning("Map attempt event insert failed code=map_event_write_failed")

    async def run_over(
        self,
        articles: list[Article],
        *,
        force: bool = False,
        reset_pipeline: bool = False,
    ) -> list[MapOutcome]:
        if not self.enabled:
            log.info(
                "mapping disabled or %s key missing; skipping",
                self.provider.name,
            )
            return []
        targets = articles
        if not force:
            targets = [a for a in articles if not await self.repo.has_map(a.id)]
        outcomes: list[MapOutcome] = []
        for article in targets:
            try:
                outcome = await self._guarded(
                    article,
                    reset_pipeline=reset_pipeline,
                )
            except Exception as exc:  # noqa: BLE001 - keep work recoverable
                log.warning("map crashed for %s: %s", article.id, exc)
                outcome = await self._requeue(
                    article.id,
                    0,
                    f"mapping error: {exc}",
                    error_code="preflight_transient",
                )
            outcomes.append(outcome)
        return outcomes

    async def _guarded(
        self,
        article: Article,
        *,
        reset_pipeline: bool = False,
    ) -> MapOutcome:
        if self._permanent_provider_error is not None:
            if self.allow_provider_failover:
                return await self._requeue(
                    article.id,
                    0,
                    f"provider circuit open: {self._permanent_provider_error}",
                    error_code="provider_permanent",
                )
            return await self._dead_letter(
                article.id,
                0,
                f"provider circuit open: {self._permanent_provider_error}",
            )
        async with self.telemetry.activity("llm_map", article.id):
            if reset_pipeline:
                await self.repo.delete_pipeline_artifacts(article.id)
            return await self._map_one(article)

    async def _map_one(self, article: Article) -> MapOutcome:
        assert self._extractor and self._images and self._llm
        n = 0
        try:
            # Extraction and deterministic source eligibility happen before
            # image downloads and before a budget reservation is committed.
            ex = await self._extractor.extract(article.url)
            document_type = getattr(article, "document_type", None)
            if document_type and (not ex.ok or not (ex.body_text or "").strip()):
                reason = str(
                    getattr(ex, "error", None)
                    or "primary document extraction returned no text"
                )
                await self._release_budget_claim(
                    article.id, "document_source_retryable"
                )
                await self._event(
                    article.id,
                    stage="source",
                    outcome="requeued",
                    error_code="document_source_retryable",
                    error_detail=reason,
                    retryable=True,
                    metadata={
                        "strategy": str(getattr(ex, "strategy", "unknown")),
                        "document_type": str(document_type),
                    },
                )
                return await self._requeue(
                    article.id,
                    0,
                    reason,
                    error_code="document_source_retryable",
                )
            source_body = ex.body_text
            if document_type:
                source_body = select_sections(
                    source_body,
                    document_type=str(document_type),
                    min_chars=self.s.map_min_source_chars,
                ).body_text
            preflight_source = build_source_packet(
                title=article.title,
                source_url=article.url,
                domain=article.domain,
                body_text=source_body,
                images=[],
                visible_image_refs=set(),
                output_language=self.s.map_language_for(
                    getattr(article, "coverage_domain", "core")
                ),
                document_type=getattr(article, "document_type", None),
                document_source_name=getattr(article, "document_source_name", None),
            )
            source_errors = validate_source_packet(
                preflight_source,
                min_chars=self.s.map_min_source_chars,
            )
            if source_errors:
                char_count = int(
                    (preflight_source.get("quality") or {}).get("char_count") or 0
                )
                if not preflight_source.get("body_text"):
                    source_code = "source_fetch_failed"
                elif char_count < self.s.map_min_source_chars:
                    source_code = "source_too_short"
                else:
                    source_code = "source_invalid"
                reason = "; ".join(source_errors[:6])
                await self._release_budget_claim(article.id, source_code)
                await self._event(
                    article.id,
                    stage="source",
                    outcome="terminal",
                    error_code=source_code,
                    error_detail=reason,
                    metadata={
                        "char_count": char_count,
                        "strategy": str(getattr(ex, "strategy", "unknown")),
                    },
                )
                return await self._dead_letter(
                    article.id,
                    0,
                    reason,
                    error_code="input_ineligible",
                )

            # Assemble image set: social image first, then in-body, deduped.
            candidate_urls: list[tuple[str, str]] = []
            if article.socialimage_url:
                candidate_urls.append((article.socialimage_url, "social"))
            for u in ex.image_urls:
                candidate_urls.append((u, "content"))

            slots = await self._collect_images(article.id, candidate_urls)
            n = len(slots)
            images = [
                {
                    "ref": slot.ref,
                    "image_id": slot.image_id,
                    "source_url": slot.source_url,
                    "role": slot.role,
                }
                for slot in slots
            ]
            parts = [slot.part for slot in slots if slot.part is not None]
            source = build_source_packet(
                title=article.title,
                source_url=article.url,
                domain=article.domain,
                body_text=source_body,
                images=images,
                visible_image_refs={
                    slot.ref for slot in slots if slot.part is not None
                },
                output_language=self.s.map_language_for(
                    getattr(article, "coverage_domain", "core")
                ),
                document_type=getattr(article, "document_type", None),
                document_source_name=getattr(article, "document_source_name", None),
            )
            final_source_errors = validate_source_packet(
                source,
                min_chars=self.s.map_min_source_chars,
            )
            if final_source_errors:
                reason = "; ".join(final_source_errors[:6])
                await self._release_budget_claim(article.id, "source_invalid")
                return await self._dead_letter(
                    article.id,
                    n,
                    reason,
                    error_code="input_ineligible",
                )
            await self._commit_budget_claim(article.id)
        except Exception:
            await self._release_budget_claim(article.id, "preflight_transient")
            raise

        pipeline = EditorialPipeline(
            self.s,
            self.repo,
            self._llm,
            image_rag=self.image_rag,
            model_identity=self.provider.identity,
        )
        try:
            canonical = await pipeline.compose(
                article.id,
                source=source,
                images=images,
                image_parts=parts,
                as_of=getattr(article, "seendate", None),
            )
        except PipelineStageError as exc:
            permanent_reason = permanent_provider_reason(exc)
            if permanent_reason is not None:
                self._permanent_provider_error = permanent_reason
                reason = f"pipeline stage {exc.stage} failed: LLM error: {permanent_reason}"
                log.warning("map permanently rejected for %s: %s", article.id, reason)
                if self.allow_provider_failover:
                    return await self._requeue(
                        article.id,
                        n,
                        reason,
                        error_code="provider_permanent",
                    )
                return await self._dead_letter(
                    article.id,
                    n,
                    reason,
                    error_code="provider_permanent",
                )
            reason = f"pipeline stage {exc.stage} failed: {'; '.join(exc.errors[:6])}"
            if exc.retryable or transient_provider_failure(exc):
                log.warning("map transient for %s -> requeue: %s", article.id, reason)
                return await self._requeue(
                    article.id,
                    n,
                    reason,
                    error_code=exc.error_code,
                )
            log.warning("map rejected for %s -> terminal: %s", article.id, reason)
            return await self._dead_letter(
                article.id,
                n,
                reason,
                error_code=exc.error_code,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._requeue(
                article.id,
                n,
                f"pipeline error: {exc}",
                error_code="pipeline_transient",
            )

        # 4/5. All stages and the deterministic Delivery Map gate passed.
        status = "ok"
        rag_links = [
            {
                "ref_index": image.get("ref"),
                "image_id": image.get("image_id"),
                "role": "rag",
                "source_url": image.get("source_url") or "",
            }
            for image in canonical.get("images") or []
            if isinstance(image, dict) and image.get("role") == "rag"
        ]
        save_kwargs = {
            "format_version": MAP_FORMAT_VERSION,
            "model": self.provider.identity,
            "status": status,
            "data": canonical,
            "error": None,
        }
        if rag_links:
            save_kwargs["rag_links"] = rag_links
        await self.repo.save_map_and_mark_done(article.id, **save_kwargs)
        await self._event(
            article.id,
            stage="map",
            outcome="succeeded",
            metadata={"image_count": len(canonical.get("images") or [])},
        )
        return MapOutcome(
            article.id,
            status,
            n_images=len(canonical.get("images") or []),
            provider=self.provider.name,
        )

    async def _dead_letter(
        self,
        article_id: str,
        n: int,
        reason: str,
        *,
        error_code: str = "terminal_failure",
    ) -> MapOutcome:
        """Persist a non-retryable failure without another provider attempt."""
        _status, attempts = await self.repo.queue_requeue(
            article_id,
            error=reason,
            max_attempts=1,
            base_delay_s=self.s.map_retry_base_delay_s,
        )
        await self.repo.upsert_map(
            article_id,
            format_version=MAP_FORMAT_VERSION,
            model=self.provider.identity,
            status="failed",
            data=None,
            error=f"dead-letter after {attempts} attempts: {reason}",
        )
        await self._event(
            article_id,
            stage="map",
            outcome="terminal",
            error_code=error_code,
            error_detail=reason,
        )
        return MapOutcome(
            article_id,
            "dead",
            n_images=n,
            error=reason,
            error_code=error_code,
            provider=self.provider.name,
        )

    async def _requeue(
        self,
        article_id: str,
        n: int,
        reason: str,
        *,
        error_code: str = "retryable_failure",
    ) -> MapOutcome:
        """Record a failed attempt with backoff. On dead-letter, write a
        'failed' map row so we stop re-mapping this article."""
        status, attempts = await self.repo.queue_requeue(
            article_id,
            error=reason,
            max_attempts=self.s.map_max_attempts,
            base_delay_s=self.s.map_retry_base_delay_s,
        )
        if status == "dead":
            await self.repo.upsert_map(
                article_id,
                format_version=MAP_FORMAT_VERSION,
                model=self.provider.identity,
                status="failed",
                data=None,
                error=f"dead-letter after {attempts} attempts: {reason}",
            )
            await self._event(
                article_id,
                stage="map",
                outcome="terminal",
                error_code=error_code,
                error_detail=reason,
            )
            return MapOutcome(
                article_id,
                "dead",
                n_images=n,
                error=reason,
                error_code=error_code,
                provider=self.provider.name,
            )
        await self._event(
            article_id,
            stage="map",
            outcome="requeued",
            error_code=error_code,
            error_detail=reason,
            retryable=True,
        )
        return MapOutcome(
            article_id,
            "retry",
            n_images=n,
            error=reason,
            error_code=error_code,
            provider=self.provider.name,
        )

    async def _collect_images(
        self, article_id: str, candidates: list[tuple[str, str]]
    ) -> list[_ImageSlot]:
        """Download+store up to max_images_per_article, dedup by image bytes,
        assign stable ref indices, and persist article_image_links."""
        assert self._images is not None
        slots: list[_ImageSlot] = []
        seen_ids: set[str] = set()
        for src_url, role in candidates:
            if len(slots) >= self.s.max_images_per_article:
                break
            ref = await self._images.store_url(src_url)
            if ref is None or ref.image_id in seen_ids:
                continue
            seen_ids.add(ref.image_id)
            idx = len(slots)
            await self.repo.link_article_image(
                article_id=article_id,
                ref_index=idx,
                image_id=ref.image_id,
                role=role,
                source_url=src_url,
            )
            part = None
            if len(slots) < self.provider.max_images:
                part = ImagePart(content_type=ref.content_type or "image/jpeg", data=ref.data)
            slots.append(
                _ImageSlot(ref=idx, image_id=ref.image_id, source_url=src_url, role=role, part=part)
            )
        return slots
