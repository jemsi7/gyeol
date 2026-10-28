"""Fail-open worker activity telemetry for API and CLI processes."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager, suppress
from typing import AsyncContextManager, AsyncIterator, Protocol
from uuid import uuid4

from .config import Settings
from .repository import Repository

log = logging.getLogger("gdelt.telemetry")


class WorkerTelemetryProtocol(Protocol):
    def activity(
        self, worker_kind: str, work_key: str | None = None
    ) -> AsyncContextManager[None]: ...


class NullWorkerTelemetry:
    @asynccontextmanager
    async def activity(
        self, worker_kind: str, work_key: str | None = None
    ) -> AsyncIterator[None]:
        yield


class WorkerTelemetry:
    def __init__(self, settings: Settings, repo: Repository):
        self.s = settings
        self.repo = repo
        self.instance_id = (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:10]}"
        )
        mapping_capacity = (
            settings.map_concurrency
            if settings.enable_mapping and settings.has_map_provider_key
            else 0
        )
        self.capacities = {
            "ingest": 1,
            "image": (
                max(1, settings.image_concurrency)
                if settings.fetch_images and settings.image_backend != "none"
                else 0
            ),
            "llm_map": mapping_capacity,
            "retry_drain": 1 if mapping_capacity else 0,
            "curation": (
                1
                if settings.curation_worker_enabled
                and bool(settings.openrouter_api_key)
                else 0
            ),
            "triage": (
                1
                if settings.triage_mode != "off"
                and bool(settings.openrouter_api_key)
                else 0
            ),
        }
        self._heartbeat_task: asyncio.Task | None = None
        self._register_lock = asyncio.Lock()
        self._registered = False

    async def _ensure_registered(self) -> bool:
        if self._registered:
            return True
        async with self._register_lock:
            if self._registered:
                return True
            try:
                await self.repo.worker_instance_start(
                    self.instance_id, self.capacities
                )
                self._registered = True
            except Exception as exc:  # noqa: BLE001 - telemetry is fail-open
                log.warning("worker telemetry registration failed: %s", exc)
        return self._registered

    async def start(self) -> None:
        if self._heartbeat_task is not None:
            return
        if await self._ensure_registered():
            try:
                await self.repo.worker_telemetry_cleanup(
                    self.s.ops_telemetry_retention_hours
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("worker telemetry cleanup failed: %s", exc)
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="worker-telemetry-heartbeat"
        )

    async def stop(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._registered:
            try:
                await self.repo.worker_instance_stop(self.instance_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("worker telemetry shutdown failed: %s", exc)
        self._registered = False

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.ops_heartbeat_seconds)
            if not await self._ensure_registered():
                continue
            try:
                await self.repo.worker_instance_heartbeat(self.instance_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("worker telemetry heartbeat failed: %s", exc)
                self._registered = False

    @asynccontextmanager
    async def activity(
        self, worker_kind: str, work_key: str | None = None
    ) -> AsyncIterator[None]:
        activity_id: int | None = None
        if await self._ensure_registered():
            try:
                activity_id = await self.repo.worker_activity_start(
                    self.instance_id, worker_kind, work_key
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("worker telemetry activity start failed: %s", exc)

        outcome = "success"
        try:
            yield
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            if activity_id is not None:
                try:
                    await self.repo.worker_activity_finish(activity_id, outcome)
                except Exception as exc:  # noqa: BLE001
                    log.warning("worker telemetry activity finish failed: %s", exc)
