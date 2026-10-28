"""Durable APNs delivery worker for first-publication editorial alerts."""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from contextlib import suppress
from typing import Any
from uuid import UUID, uuid5

from .apns import APNsProvider
from .apple_identity import TokenCipher
from .config import Settings


log = logging.getLogger("gdelt.push_worker")


class EditorialPushWorker:
    batch_limit = 100

    def __init__(
        self,
        settings: Settings,
        repo: Any,
        provider: APNsProvider,
        cipher: TokenCipher,
    ):
        self.settings = settings
        self.repo = repo
        self.provider = provider
        self.cipher = cipher
        self._task: asyncio.Task[None] | None = None

    def start(self) -> bool:
        if not self.settings.push_notifications_enabled:
            log.info("editorial push worker disabled")
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self._run(), name="editorial-push-worker")
        log.info(
            "editorial push worker started poll_seconds=%s max_attempts=%s",
            self.settings.push_poll_seconds,
            self.settings.push_max_attempts,
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
        log.info("editorial push worker stopped")

    async def run_once(self) -> dict[str, int]:
        cancelled = await self.repo.cancel_ineligible_push_deliveries()
        cancelled += await self.repo.cancel_ineligible_timeline_push_deliveries()
        editorial_rows = await self.repo.claim_push_deliveries(
            limit=self.batch_limit,
            lease_seconds=self.settings.push_lease_seconds,
        )
        remaining = self.batch_limit - len(editorial_rows)
        timeline_rows = (
            await self.repo.claim_timeline_push_deliveries(
                limit=remaining,
                lease_seconds=self.settings.push_lease_seconds,
            )
            if remaining > 0
            else []
        )
        rows = [(False, row) for row in editorial_rows] + [
            (True, row) for row in timeline_rows
        ]
        totals = {
            "claimed": len(rows),
            "sent": 0,
            "retried": 0,
            "failed": 0,
            "invalid": 0,
            "cancelled": cancelled,
            "skipped": 0,
        }
        for is_timeline, row in rows:
            notification_id = UUID(str(row["notification_id"]))
            registration_id = UUID(str(row["registration_id"]))
            apns_id = uuid5(notification_id, str(registration_id))
            try:
                encoded_token = self.cipher.decrypt(row["token_ciphertext"])
                device_token = _decode_base64url(encoded_token)
            except Exception:  # noqa: BLE001 - invalid ciphertext is isolated
                await self.repo.invalidate_push_registration(
                    registration_id=registration_id,
                    error_code="token_decryption_failed",
                )
                totals["invalid"] += 1
                continue
            try:
                if is_timeline:
                    result = await self.provider.send_timeline_update(
                        device_token=device_token,
                        environment=str(row["environment"]),
                        apns_id=apns_id,
                        stable_key=str(row["timeline_stable_key"]),
                        node_id=UUID(str(row["node_id"])),
                        update_kind=str(row["update_kind"]),
                        event_id=UUID(str(row["event_id"])),
                    )
                else:
                    result = await self.provider.send(
                        device_token=device_token,
                        environment=str(row["environment"]),
                        apns_id=apns_id,
                        headline=str(row["headline"]),
                        body=str(row["body"]),
                        article_id=str(row["article_id"]),
                        publication_kind=str(
                            row.get("publication_kind", "editorial")
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - lease ledger owns safe retry
                status = await self._retry(
                    row,
                    notification_id=notification_id,
                    registration_id=registration_id,
                    error_code="apns_delivery_unavailable",
                    transient=True,
                    is_timeline=is_timeline,
                )
                totals["retried" if status == "queued" else "failed"] += 1
                continue
            if result.kind == "sent":
                complete = (
                    self.repo.complete_timeline_push_delivery
                    if is_timeline
                    else self.repo.complete_push_delivery
                )
                completed = await complete(
                    notification_id=notification_id,
                    registration_id=registration_id,
                    apns_id=apns_id,
                )
                totals["sent" if completed else "skipped"] += 1
            elif result.kind == "invalid_token":
                await self.repo.invalidate_push_registration(
                    registration_id=registration_id,
                    error_code=result.code,
                )
                totals["invalid"] += 1
            else:
                status = await self._retry(
                    row,
                    notification_id=notification_id,
                    registration_id=registration_id,
                    error_code=result.code,
                    transient=result.kind == "transient",
                    is_timeline=is_timeline,
                )
                totals["retried" if status == "queued" else "failed"] += 1
        return totals

    async def _retry(
        self,
        row: dict,
        *,
        notification_id: UUID,
        registration_id: UUID,
        error_code: str,
        transient: bool,
        is_timeline: bool,
    ) -> str:
        attempts = int(row["attempt_count"])
        delay = min(300.0, 5.0 * (2 ** max(0, attempts - 1)))
        retry = (
            self.repo.retry_timeline_push_delivery
            if is_timeline
            else self.repo.retry_push_delivery
        )
        return await retry(
            notification_id=notification_id,
            registration_id=registration_id,
            error_code=error_code,
            transient=transient,
            max_attempts=self.settings.push_max_attempts,
            delay_seconds=delay,
        )

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - DB outage retries next poll
                log.warning("editorial push poll failed code=push_poll_unavailable")
                result = {"claimed": 0}
            if result.get("claimed", 0) >= self.batch_limit:
                await asyncio.sleep(0)
                continue
            await asyncio.sleep(self.settings.push_poll_seconds)


def _decode_base64url(value: str) -> bytes:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid encrypted device token") from exc
    if not decoded or len(decoded) > 256:
        raise ValueError("invalid encrypted device token")
    return decoded
