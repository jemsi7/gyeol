"""Continuous Map queue worker, independent from the ingestion scheduler."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .config import Settings
from .engine import LoaderEngine

log = logging.getLogger("gdelt.map_worker")


class MapQueueWorker:
    """Own one supervisor that drains bounded batches until no work is due."""

    def __init__(
        self,
        settings: Settings,
        engine: LoaderEngine,
        *,
        poll_interval_s: float = 5.0,
        lease_owner: str | None = None,
    ):
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.s = settings
        self.engine = engine
        self.poll_interval_s = poll_interval_s
        self.lease_owner = lease_owner
        self._task: asyncio.Task[None] | None = None

    def start(self) -> bool:
        """Start once when mapping is configured; return whether it started."""
        configured_providers = self.s.configured_map_provider_names
        if not self.s.enable_mapping or not configured_providers:
            log.info(
                "map worker disabled (mapping disabled or provider key missing)"
            )
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(
            self._run(),
            name="map-queue-worker",
        )
        log.info(
            "map worker started concurrency=%d providers=%s",
            self.s.map_concurrency,
            ",".join(configured_providers),
        )
        return True

    async def stop(self) -> None:
        """Cancel and consume the supervisor before its dependencies close."""
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if self._task is task:
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                try:
                    result = await self.engine.process_queue(
                        lease_owner=self.lease_owner
                    )
                    claimed = int(result.get("claimed", 0))
                    log.info(
                        "map worker drain claimed=%d mapped=%d failed=%d",
                        claimed,
                        int(result.get("mapped", 0)),
                        int(result.get("failed", 0)),
                    )
                    if (
                        self.s.queue_drain_limit > 0
                        and claimed >= self.s.queue_drain_limit
                    ):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep API/ingest alive
                    log.warning(
                        "map worker drain failed error=%s",
                        type(exc).__name__,
                    )
                await asyncio.sleep(self.poll_interval_s)
        finally:
            if self._task is asyncio.current_task():
                self._task = None
