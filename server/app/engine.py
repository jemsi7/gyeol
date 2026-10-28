"""The loading engine: fetch -> normalize -> fetch images -> upsert -> audit.

This is the reusable core. It has no web/scheduler dependencies, so you can
drive it from FastAPI, a cron container, a one-off CLI, or tests.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .gdelt_client import GdeltClient
from .images import ImageFetcher
from .jobs import Job
from .mapper import Mapper, MapOutcome, MapProvider
from .models import Article
from .newsapi_client import NewsApiClient
from .openrouter import OpenRouterRateLimiter
from .repository import MapBudget, Repository
from .telemetry import NullWorkerTelemetry, WorkerTelemetryProtocol
from .triage import NEUTRAL_SCORE as NEUTRAL_TRIAGE_SCORE

log = logging.getLogger("gdelt.engine")


@dataclass(slots=True)
class LoadResult:
    job: str
    query: str
    fetched: int
    inserted: int
    updated: int
    images_stored: int
    duration_s: float
    ok: bool
    error: str | None = None
    mapped: int = 0
    maps_failed: int = 0
    skipped_reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class LoaderEngine:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        telemetry: WorkerTelemetryProtocol | None = None,
        image_rag: Any | None = None,
    ):
        self.s = settings
        self.repo = repo
        self.telemetry = telemetry or NullWorkerTelemetry()
        self.image_rag = image_rag
        self._map_slots = asyncio.Semaphore(settings.map_concurrency)
        self._map_request_limiters = {
            "openrouter": OpenRouterRateLimiter(
                settings.map_requests_per_minute
            ),
            "cerebras": OpenRouterRateLimiter(
                settings.cerebras_requests_per_minute
            ),
        }
        self._disabled_map_providers: set[str] = set()
        self._manual_provider_cursor = 0
        self._manual_provider_lock = asyncio.Lock()

    def _configured_map_providers(
        self, coverage_domain: str | None = None
    ) -> list[MapProvider]:
        """Providers for one domain. None keeps the pre-domain global model."""
        providers: list[MapProvider] = []
        configured_names = set(self.s.configured_map_provider_names)
        if "openrouter" in configured_names:
            providers.append(
                MapProvider(
                    name="openrouter",
                    model=(
                        self.s.openrouter_model
                        if coverage_domain is None
                        else self.s.map_model_for(coverage_domain)
                    ),
                    max_images=self.s.llm_max_images,
                    request_limiter=self._map_request_limiters["openrouter"],
                )
            )
        if "cerebras" in configured_names:
            providers.append(
                MapProvider(
                    name="cerebras",
                    model=self.s.cerebras_model,
                    max_images=self.s.cerebras_max_images,
                    request_limiter=self._map_request_limiters["cerebras"],
                )
            )
        return providers

    def _available_map_providers(
        self, coverage_domain: str | None = None
    ) -> list[MapProvider]:
        return [
            provider
            for provider in self._configured_map_providers(coverage_domain)
            if provider.name not in self._disabled_map_providers
        ]

    @property
    def mapping_enabled(self) -> bool:
        return self.s.enable_mapping and bool(self._available_map_providers())

    def _daily_budget(self) -> MapBudget | None:
        """Env fallback for the claim budget; the live values live in the DB."""
        if not self.s.map_budget_enabled:
            return None
        return MapBudget(
            daily_limit=self.s.map_daily_limit_default,
            fresh_share_percent=self.s.map_fresh_share_default,
            daily_run_count=self.s.map_daily_run_count_default,
        )

    async def sync_image_rag(self) -> Any | None:
        """Best-effort refresh of the derived image retrieval pool."""
        if self.image_rag is None:
            return None
        try:
            return await self.image_rag.sync()
        except Exception:  # noqa: BLE001 - derived enrichment must fail open
            log.warning("image RAG sync skipped code=engine_sync_failed")
            return None

    async def run_job(self, job: Job) -> LoadResult:
        async with self.telemetry.activity("ingest", job.name):
            return await self._run_job(job)

    async def _run_job(self, job: Job) -> LoadResult:
        t0 = time.perf_counter()
        if job.provider not in {"gdelt", "newsapi"}:
            raise ValueError(f"unsupported job provider: {job.provider!r}")
        if job.provider == "newsapi":
            if not self.s.newsapi_enabled:
                return self._skipped_result(job, t0, "newsapi_disabled")
            if not self.s.newsapi_api_key.get_secret_value().strip():
                return self._skipped_result(job, t0, "newsapi_key_missing")

        try:
            if job.provider == "newsapi":
                run_id = await self.repo.start_bounded_load_run(
                    job.name,
                    job.query,
                    provider_prefix="newsapi:",
                    max_runs=self.s.newsapi_logical_run_limit(),
                    window_hours=24.0,
                )
                if run_id is None:
                    return self._skipped_result(
                        job, t0, "newsapi_budget_exhausted"
                    )
            else:
                run_id = await self.repo.start_run(job.name, job.query)
        except Exception as error:  # noqa: BLE001 - no run exists to audit
            duration = time.perf_counter() - t0
            log.exception("job=%s run reservation failed", job.name)
            return LoadResult(
                job.name,
                job.query,
                0,
                0,
                0,
                0,
                duration,
                ok=False,
                error=str(error),
            )

        fetched = inserted = updated = images_stored = 0
        try:
            articles = await self._fetch_articles(job)
            # Coverage domain is a collection policy, not a provider contract,
            # so the clients stay unaware of it and the job stamps its own
            # articles here (DR-13208). Stamped before the NewsAPI gap filter
            # so every downstream path sees a domain.
            for article in articles:
                article.coverage_domain = job.coverage_domain
            fetched = len(articles)
            if job.provider == "newsapi":
                articles = await self._newsapi_gap(articles)

            # Fetch + store images first so image_id is set before the article
            # upsert — that guarantees the pairing lands atomically per row.
            async with ImageFetcher(
                self.s, self.repo, telemetry=self.telemetry
            ) as imgs:
                images_stored = await imgs.attach_images(articles)

            inserted, updated = await self._upsert_articles(articles)

            # Article persistence is the ingest completion boundary. Every
            # article already has a durable queue row at this point; the
            # scheduler drains that queue in a separate single-flight task so
            # slow LLM work cannot hold the load run or HTTP response open.
            mapped = maps_failed = 0

            dur = time.perf_counter() - t0
            await self.repo.finish_run(
                run_id,
                fetched=fetched,
                inserted=inserted,
                updated=updated,
                images_stored=images_stored,
                ok=True,
                mapped=mapped,
                maps_failed=maps_failed,
            )
            log.info(
                "job=%s fetched=%d inserted=%d updated=%d images=%d "
                "map_drain=deferred in %.1fs",
                job.name, fetched, inserted, updated, images_stored,
                dur,
            )
            return LoadResult(
                job.name, job.query, fetched, inserted, updated,
                images_stored, dur, ok=True, mapped=mapped, maps_failed=maps_failed,
            )
        except Exception as e:  # noqa: BLE001 - record then re-surface
            dur = time.perf_counter() - t0
            log.exception("job=%s failed", job.name)
            await self.repo.finish_run(
                run_id,
                fetched=fetched,
                inserted=inserted,
                updated=updated,
                images_stored=images_stored,
                ok=False,
                error=str(e),
            )
            return LoadResult(
                job.name, job.query, fetched, inserted, updated,
                images_stored, dur, ok=False, error=str(e),
            )

    def _skipped_result(
        self,
        job: Job,
        started_at: float,
        reason: str,
    ) -> LoadResult:
        return LoadResult(
            job.name,
            job.query,
            0,
            0,
            0,
            0,
            time.perf_counter() - started_at,
            ok=True,
            skipped_reason=reason,
        )

    async def _fetch_articles(self, job: Job) -> list[Article]:
        if job.provider == "gdelt":
            async with GdeltClient(self.s) as gdelt:
                if job.window_hours:
                    return await gdelt.fetch_window(
                        job.query,
                        hours=job.window_hours,
                        slice_hours=job.slice_hours,
                        max_records=job.max_records,
                        source_lang=job.source_lang,
                        source_country=job.source_country,
                    )
                return await gdelt.fetch(
                    job.query,
                    timespan=job.timespan,
                    max_records=job.max_records,
                    source_lang=job.source_lang,
                    source_country=job.source_country,
                )
        if job.provider == "newsapi":
            end = datetime.now(UTC)
            start = end - timedelta(hours=24)
            async with NewsApiClient(self.s) as newsapi:
                return await newsapi.fetch(
                    job.query,
                    start=start,
                    end=end,
                    page_size=job.max_records,
                    language=job.language or "en",
                    search_in=job.search_in or "title,description",
                    sort_by=job.sort_by or "publishedAt",
                    page=1,
                )
        raise ValueError(f"unsupported job provider: {job.provider!r}")

    async def _newsapi_gap(self, articles: list[Article]) -> list[Article]:
        if not articles:
            return []
        missing = await self.repo.missing_article_ids(
            [article.id for article in articles]
        )
        return [article for article in articles if article.id in missing]

    async def _upsert_articles(self, articles: list[Article]) -> tuple[int, int]:
        """Persist articles; each repository call also ensures its queue row."""
        inserted = updated = 0
        for article in articles:
            outcome = await self.repo.upsert_article(article)
            if outcome == "inserted":
                inserted += 1
            elif outcome == "updated":
                updated += 1
            else:
                raise ValueError(
                    f"unexpected article upsert outcome for {article.id}: {outcome!r}"
                )
        return inserted, updated

    async def run_all(self, jobs: list[Job]) -> list[LoadResult]:
        results = []
        for job in jobs:
            if not job.enabled:
                continue
            results.append(await self.run_job(job))
        return results

    async def process_queue(
        self,
        limit: int | None = None,
        *,
        lease_owner: str | None = None,
        coverage_domain: str | None = None,
    ) -> dict:
        """Process globally oldest due Map work with configured bounded slots."""
        lim = self.s.queue_drain_limit if limit is None else max(0, limit)
        if lim == 0:
            return {"claimed": 0, "mapped": 0, "failed": 0}
        recover_orphans = getattr(self.repo, "recover_orphaned_map_leases", None)
        if recover_orphans is not None:
            recovered_orphans = int(
                await recover_orphans(
                    stale_after_s=self.s.ops_stale_after_seconds
                )
            )
            if recovered_orphans:
                log.info(
                    "recovered orphaned Map leases count=%d",
                    recovered_orphans,
                )
        recover = getattr(self.repo, "recover_expired_map_leases", None)
        if recover is not None:
            recovered = int(
                await recover(lease_seconds=self.s.map_processing_lease_s)
            )
            if recovered:
                log.info("recovered expired Map leases count=%d", recovered)
        providers = self._available_map_providers(coverage_domain)
        if not self.s.enable_mapping or not providers:
            return {
                "claimed": 0,
                "mapped": 0,
                "failed": 0,
                "note": "mapping disabled or no Map provider available",
            }

        claimed = 0
        claim_lock = asyncio.Lock()
        failover_enabled = len(self._configured_map_providers()) > 1
        slot_results: list[list[MapOutcome] | None] = [
            None for _ in range(min(lim, self.s.map_concurrency))
        ]

        async def drain_slot(index: int, provider: MapProvider) -> None:
            nonlocal claimed
            outcomes: list[MapOutcome] = []
            async with Mapper(
                self.s,
                self.repo,
                telemetry=self.telemetry,
                image_rag=self.image_rag,
                request_limiter=provider.request_limiter,
                provider=provider,
                allow_provider_failover=failover_enabled,
            ) as mapper:
                while True:
                    async with self._map_slots:
                        async with claim_lock:
                            if (
                                claimed >= lim
                                or provider.name in self._disabled_map_providers
                            ):
                                break
                            claim_kwargs = dict(
                                lease_seconds=self.s.map_processing_lease_s,
                                max_active=self.s.map_concurrency,
                                lease_owner=lease_owner,
                                budget=self._daily_budget(),
                                ranked=self.s.triage_mode == "enforce",
                                stale_after_s=self.s.triage_stale_hours * 3600.0,
                                neutral_score=NEUTRAL_TRIAGE_SCORE,
                            )
                            if coverage_domain is not None:
                                claim_kwargs["coverage_domain"] = coverage_domain
                            article_id = await self.repo.queue_claim_next(
                                **claim_kwargs
                            )
                            if article_id is None:
                                break
                            claimed += 1
                        row = await self.repo.get_article(article_id)
                        if row is None:
                            continue
                        article = _RowArticle(
                            id=row["id"],
                            url=row["url"],
                            title=row.get("title"),
                            domain=row.get("domain"),
                            socialimage_url=row.get("socialimage_url"),
                            coverage_domain=str(
                                row.get("coverage_domain") or "core"
                            ),
                            document_type=row.get("document_type"),
                            document_source_name=row.get(
                                "document_source_name"
                            ),
                        )
                        current = await mapper.run_over([article], force=True)
                        outcomes.extend(current)
                        if any(
                            getattr(outcome, "error_code", None)
                            == "provider_permanent"
                            for outcome in current
                        ):
                            async with claim_lock:
                                self._disabled_map_providers.add(provider.name)
                            break
            slot_results[index] = outcomes

        async with self.telemetry.activity("retry_drain", "persistent FIFO"):
            async with asyncio.TaskGroup() as group:
                for index in range(len(slot_results)):
                    provider = providers[index % len(providers)]
                    group.create_task(
                        drain_slot(index, provider),
                        name=(
                            f"map-drain-{provider.name}-slot-{index + 1}"
                        ),
                    )

        outcomes = [
            outcome
            for result in slot_results
            for outcome in (result or [])
        ]
        return {
            "claimed": claimed,
            "mapped": sum(
                1 for outcome in outcomes
                if outcome.status in ("ok", "degraded")
            ),
            "failed": sum(
                1 for outcome in outcomes
                if outcome.status in ("retry", "dead", "failed")
            ),
        }

    async def map_article(
        self,
        article: "_RowArticle",
        *,
        force: bool = True,
        reset_pipeline: bool = False,
    ) -> list[MapOutcome]:
        """Map one explicit article through the shared web-process guards."""
        if article.coverage_domain.strip().casefold() == "crypto":
            ready = getattr(self.repo, "crypto_relevance_ready", None)
            if ready is None or not bool(await ready(article.id)):
                return [
                    MapOutcome(
                        article.id,
                        "failed",
                        error="crypto relevance gate closed",
                        error_code="crypto_relevance_gate_closed",
                    )
                ]
        providers = self._available_map_providers(article.coverage_domain)
        if not self.s.enable_mapping or not providers:
            return []
        async with self._manual_provider_lock:
            provider = providers[self._manual_provider_cursor % len(providers)]
            self._manual_provider_cursor += 1
        failover_enabled = len(self._configured_map_providers()) > 1
        async with self._map_slots:
            async with Mapper(
                self.s,
                self.repo,
                telemetry=self.telemetry,
                image_rag=self.image_rag,
                request_limiter=provider.request_limiter,
                provider=provider,
                allow_provider_failover=failover_enabled,
            ) as mapper:
                if not mapper.enabled:
                    return []
                outcomes = await mapper.run_over(
                    [article],
                    force=force,
                    reset_pipeline=reset_pipeline,
                )
                if any(
                    getattr(outcome, "error_code", None)
                    == "provider_permanent"
                    for outcome in outcomes
                ):
                    self._disabled_map_providers.add(provider.name)
                return outcomes

    async def map_pending(self, limit: int = 100) -> dict:
        """Reconcile legacy articles, then use the same persistent FIFO drain."""
        reconciled = await self.repo.reconcile_map_queue(
            max_attempts=self.s.map_max_attempts
        )
        result = await self.process_queue(limit)
        return {
            "reconciled": reconciled,
            "requested": int(result.get("claimed", 0)),
            **result,
        }


@dataclass(slots=True)
class _RowArticle:
    """Lightweight article view built from a DB row, carrying only the fields
    the Mapper needs. Avoids re-fetching from GDELT for backfills."""

    id: str
    url: str
    title: str | None
    domain: str | None
    socialimage_url: str | None
    # Selects the output language and the reader-exposure gate; defaults to the
    # core domain so a caller that predates DR-13200 behaves as before.
    coverage_domain: str = "core"
    document_type: str | None = None
    document_source_name: str | None = None
