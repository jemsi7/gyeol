"""Independent bounded worker for the shared discovery curation catalog."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Callable

from .config import Settings
from .curation import (
    CurationValidationError,
    curation_input_hash,
    curation_plan_payload,
    curation_refresh_slot,
    validate_curation_plan,
)
from .curation_agent import CurationAgent
from .curation_audience import (
    AUDIENCE_EVENT_LIMIT_PER_ACCOUNT,
    AUDIENCE_LOOKBACK_DAYS,
    AUDIENCE_SAMPLE_LIMIT,
    build_curation_audience_aggregate,
)
from .feed import seoul_editorial_day
from .openrouter import OpenRouterPermanentError, OpenRouterTransientError
from .repository import Repository
from .telemetry import NullWorkerTelemetry, WorkerTelemetryProtocol

log = logging.getLogger("gdelt.curation_worker")


class CurationCatalogWorker:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        agent: CurationAgent,
        telemetry: WorkerTelemetryProtocol | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = settings
        self.repo = repo
        self.agent = agent
        self.telemetry = telemetry or NullWorkerTelemetry()
        self.clock = clock
        self._task: asyncio.Task[None] | None = None

    def start(self) -> bool:
        if not self.settings.curation_worker_enabled or not self.agent.configured:
            log.info("curation worker disabled or provider unavailable")
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(
            self._run(), name="article-curation-catalog-worker"
        )
        log.info(
            "curation worker started model=%s poll_seconds=%s refresh_min_seconds=%s",
            self.settings.curation_agent_model,
            self.settings.curation_poll_seconds,
            self.settings.curation_refresh_min_seconds,
        )
        return True

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        log.info("curation worker stopped")

    async def run_once(self) -> dict[str, int | str]:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        pool_state = await self.repo.curation_pool_state(
            since_days=self.settings.curation_candidate_days,
        )
        candidate_count = int(pool_state["candidate_count"])
        minimum_articles = self.settings.curation_min_routes * 4
        if candidate_count < minimum_articles:
            return {
                "kind": "insufficient_candidates",
                "candidates": candidate_count,
                "published": 0,
            }
        editorial_day = seoul_editorial_day(now)
        refresh_slot = curation_refresh_slot(
            now,
            self.settings.curation_refresh_min_seconds,
        )
        input_hash = curation_input_hash(
            editorial_day,
            refresh_slot=refresh_slot,
            pool_fingerprint=str(pool_state["pool_fingerprint"]),
        )
        claimed = await self.repo.claim_curation_catalog(
            editorial_day=editorial_day,
            input_hash=input_hash,
            candidate_count=candidate_count,
            min_interval_s=self.settings.curation_refresh_min_seconds,
            max_attempts=self.settings.curation_max_attempts,
            lease_s=self.settings.curation_processing_lease_s,
        )
        if claimed is None:
            return {
                "kind": "not_due",
                "candidates": candidate_count,
                "published": 0,
            }

        catalog_id = claimed["id"]
        try:
            sample_seed = int(input_hash[:16], 16) & ((1 << 63) - 1)
            sample = await self.repo.curation_sample_accounts(
                snapshot_at=now,
                since_days=AUDIENCE_LOOKBACK_DAYS,
                limit=AUDIENCE_SAMPLE_LIMIT,
                seed=sample_seed,
            )
            account_ids = sample["account_ids"]
            signal_rows = await self.repo.curation_audience_signal_rows(
                account_ids=account_ids,
                snapshot_at=now,
                since_days=AUDIENCE_LOOKBACK_DAYS,
                event_limit_per_account=AUDIENCE_EVENT_LIMIT_PER_ACCOUNT,
            )
            audience = build_curation_audience_aggregate(
                signal_rows,
                eligible_user_count=int(sample["eligible_user_count"]),
                sampled_user_count=len(account_ids),
                snapshot_at=now,
            )
            async with self.telemetry.activity("curation", input_hash[:12]):
                generated = await self.agent.generate(
                    audience,
                    editorial_day=editorial_day,
                    refresh_slot=refresh_slot,
                )
                plan = validate_curation_plan(
                    curation_plan_payload(generated.plan),
                    generated.candidates,
                    min_routes=self.settings.curation_min_routes,
                    max_routes=self.settings.curation_max_routes,
                )
                await self.repo.publish_curation_catalog(
                    catalog_id,
                    plan=plan,
                    model=self.settings.curation_agent_model,
                    valid_until=now + timedelta(hours=self.settings.curation_valid_hours),
                )
        except asyncio.CancelledError:
            raise
        except OpenRouterPermanentError:
            status = await self.repo.retry_curation_catalog(
                catalog_id,
                error_code="provider_permanent",
                base_delay_s=self.settings.curation_retry_base_delay_s,
            )
            return {"kind": status, "candidates": candidate_count, "published": 0}
        except OpenRouterTransientError:
            status = await self.repo.retry_curation_catalog(
                catalog_id,
                error_code="provider_transient",
                base_delay_s=self.settings.curation_retry_base_delay_s,
            )
            return {"kind": status, "candidates": candidate_count, "published": 0}
        except CurationValidationError as error:
            status = await self.repo.retry_curation_catalog(
                catalog_id,
                error_code=error.code,
                base_delay_s=self.settings.curation_retry_base_delay_s,
            )
            return {"kind": status, "candidates": candidate_count, "published": 0}
        except Exception as error:  # noqa: BLE001 - safe retry ledger owns details
            log.warning("curation run failed error=%s", type(error).__name__)
            status = await self.repo.retry_curation_catalog(
                catalog_id,
                error_code="curation_unavailable",
                base_delay_s=self.settings.curation_retry_base_delay_s,
            )
            return {"kind": status, "candidates": candidate_count, "published": 0}
        return {
            "kind": "published",
            "candidates": candidate_count,
            "routes": len(plan.routes),
            "published": 1,
        }

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - DB outages retry next poll
                log.warning("curation poll failed error=%s", type(error).__name__)
            await asyncio.sleep(self.settings.curation_poll_seconds)
