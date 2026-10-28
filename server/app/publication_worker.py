"""Persistent executor for human-approved editorial publication schedules."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from .config import Settings


log = logging.getLogger("gdelt.publication_worker")


class PublicationScheduleWorker:
    """Claim due DB rows and materialize their frozen publication snapshots."""

    batch_limit = 10
    lease_seconds = 60.0

    def __init__(self, settings: Settings, repo: Any):
        self.settings = settings
        self.repo = repo
        self._task: asyncio.Task[None] | None = None

    def start(self) -> bool:
        if not self.settings.publication_worker_enabled:
            log.info("publication worker disabled")
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(
            self._run(), name="editorial-publication-worker"
        )
        log.info(
            "publication worker started poll_seconds=%s max_attempts=%s",
            self.settings.publication_poll_seconds,
            self.settings.publication_max_attempts,
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
        log.info("publication worker stopped")

    async def run_once(self) -> dict[str, int]:
        rows = await self.repo.claim_due_editorial_schedules(
            limit=self.batch_limit,
            lease_seconds=self.lease_seconds,
        )
        published = 0
        retried = 0
        failed = 0
        skipped = 0
        for row in rows:
            try:
                result = await self.repo.publish_claimed_editorial_schedule(row["id"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - row is safely returned to its ledger
                result = {"kind": "publication_unavailable"}

            kind = result.get("kind")
            if kind == "published":
                published += 1
                continue
            if kind in {
                "schedule_terminal",
                "schedule_not_found",
                "not_found",
            }:
                skipped += 1
                continue
            retry = await self.repo.retry_editorial_schedule(
                row["id"],
                error_code=(
                    "publication_conflict"
                    if kind in {"conflict", "schedule_conflict"}
                    else "publication_unavailable"
                ),
                max_attempts=self.settings.publication_max_attempts,
            )
            if retry.get("kind") == "failed":
                failed += 1
            elif retry.get("kind") == "retry":
                retried += 1
            else:
                skipped += 1
        return {
            "claimed": len(rows),
            "published": published,
            "retried": retried,
            "failed": failed,
            "skipped": skipped,
        }

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - DB outage is retried on the next poll
                log.warning("publication worker poll failed code=claim_unavailable")
                result = {"claimed": 0}
            if result.get("claimed", 0) >= self.batch_limit:
                await asyncio.sleep(0)
                continue
            await asyncio.sleep(self.settings.publication_poll_seconds)
