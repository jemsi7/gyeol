"""APScheduler wrapper that runs `run_all` on a cron cadence.

Single-process AsyncIO scheduler: fine for one Railway service. If you scale
to multiple replicas, either run the scheduler in a dedicated worker service
(set SCHEDULER_ENABLED=false on the web replicas) or add a DB advisory lock.
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .engine import LoaderEngine
from .jobs import load_jobs

log = logging.getLogger("gdelt.scheduler")


class RefreshScheduler:
    def __init__(
        self,
        settings: Settings,
        engine: LoaderEngine,
        primary_document_worker: Any | None = None,
    ):
        self.s = settings
        self.engine = engine
        self.primary_document_worker = primary_document_worker
        self._sched = AsyncIOScheduler(timezone="UTC")
        self._running = False

    async def _tick(self) -> None:
        if self._running:
            log.warning("previous refresh still running; skipping this tick")
            return
        self._running = True
        try:
            try:
                await self.engine.sync_image_rag()
            except Exception:  # noqa: BLE001 - derived pool must not stop ingestion
                log.warning("image RAG sync skipped code=scheduler_sync_failed")
            jobs = load_jobs(self.s.jobs_file)
            await self.engine.run_all(jobs)
            if self.primary_document_worker is not None:
                try:
                    await self.primary_document_worker.run_once()
                except Exception:  # noqa: BLE001 - one lane cannot stop refresh
                    log.warning(
                        "primary document discovery skipped "
                        "code=primary_document_refresh_failed"
                    )
        finally:
            self._running = False

    def start(self) -> None:
        if not self.s.scheduler_enabled:
            log.info("scheduler disabled (SCHEDULER_ENABLED=false)")
            return
        trigger = CronTrigger.from_crontab(self.s.refresh_cron, timezone="UTC")
        self._sched.add_job(
            self._tick,
            trigger=trigger,
            id="gdelt-refresh",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._sched.start()
        log.info("scheduler started (cron=%s UTC)", self.s.refresh_cron)

    def shutdown(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)
