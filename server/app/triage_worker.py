"""Independent worker that scores Map candidates and expires stale ones.

It never touches the Map pipeline itself: it only writes the triage score the
budgeted claim orders by, and deletes candidates the budget never reached
within the retention window.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Callable

from .config import Settings
from .repository import Repository
from .telemetry import NullWorkerTelemetry, WorkerTelemetryProtocol
from .triage import (
    NEUTRAL_SCORE,
    TriageCandidate,
    TriageScorer,
    TriageValidationError,
    build_results,
    candidate_from_row,
    normalized_title,
)

log = logging.getLogger("gdelt.triage_worker")

DUPLICATE_WINDOW_HOURS = 48.0


class TriageWorker:
    """Poll for unscored candidates, score one batch, and run retention."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        scorer: TriageScorer,
        telemetry: WorkerTelemetryProtocol | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ):
        self.s = settings
        self.repo = repo
        self.scorer = scorer
        self.telemetry = telemetry or NullWorkerTelemetry()
        self.clock = clock or (lambda: asyncio.get_event_loop().time())
        self._task: asyncio.Task[None] | None = None
        self._last_purge_at: float | None = None

    @property
    def scoring_enabled(self) -> bool:
        core_enabled = self.s.triage_mode != "off"
        # Crypto is always enforce/fail-closed while automatic mapping is live,
        # even when the legacy global mode deliberately leaves core scoring off.
        crypto_enabled = self.s.enable_mapping and self.s.map_budget_enabled
        return (core_enabled or crypto_enabled) and self.scorer.configured

    def start(self) -> bool:
        """Start once when scoring or retention has work to do."""
        if not self.scoring_enabled and not self.s.map_budget_enabled:
            log.info("triage worker disabled (scoring off and budget off)")
            return False
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self._run(), name="map-triage-worker")
        log.info(
            "triage worker started mode=%s batch=%d ttl_days=%d",
            self.s.triage_mode,
            self.s.triage_batch_size,
            self.s.map_candidate_ttl_days,
        )
        return True

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> dict[str, int | str]:
        purged = await self._purge_if_due()
        if not self.scoring_enabled:
            return {"kind": "retention_only", "scored": 0, "deleted": purged}
        core_scoring_enabled = self.s.triage_mode != "off"
        rows = await self.repo.triage_pending_rows(
            limit=self.s.triage_batch_size,
            max_attempts=self.s.triage_max_attempts,
            retry_after_s=self.s.triage_stale_hours * 3600.0,
            coverage_domain=None if core_scoring_enabled else "crypto",
        )
        if not rows:
            kind = "idle" if core_scoring_enabled else "retention_only"
            return {"kind": kind, "scored": 0, "deleted": purged}

        candidates = [candidate_from_row(row) for row in rows]
        if not core_scoring_enabled:
            # Repository fakes and rolling old implementations may ignore the
            # optional filter. Never let TRIAGE_MODE=off score a core row.
            candidates = [
                candidate
                for candidate in candidates
                if candidate.coverage_domain.casefold() == "crypto"
            ]
        if not candidates:
            return {"kind": "retention_only", "scored": 0, "deleted": purged}
        attempts_by_id = {
            str(row["article_id"]): int(row.get("triage_attempts") or 0)
            for row in rows
        }
        duplicates = await self._duplicates(candidates)

        kinds: list[str] = []
        saved_total = 0
        fallbacks = 0
        groups = (
            (
                "core",
                [c for c in candidates if c.coverage_domain.casefold() != "crypto"],
                False,
            ),
            (
                "crypto",
                [c for c in candidates if c.coverage_domain.casefold() == "crypto"],
                True,
            ),
        )
        for group_name, group, require_relevance in groups:
            if not group:
                continue
            scoreable = [candidate for candidate in group if candidate.title.strip()]
            # Core keeps its deterministic missing-title result. Crypto cannot
            # manufacture an actual provider relevance decision for no title.
            to_save = (
                [candidate for candidate in group if not candidate.title.strip()]
                if not require_relevance
                else []
            )
            axes_by_id: dict[str, dict[str, int | str]] = {}
            kind = "scored"
            if scoreable:
                try:
                    work_key = (
                        f"crypto:{len(scoreable)}"
                        if require_relevance
                        else str(len(scoreable))
                    )
                    async with self.telemetry.activity("triage", work_key):
                        if require_relevance:
                            axes_by_id = await self.scorer.score(
                                scoreable,
                                require_crypto_relevance=True,
                            )
                        else:
                            # Preserve the legacy core scorer call contract too;
                            # the new keyword belongs only to the crypto batch.
                            axes_by_id = await self.scorer.score(scoreable)
                except asyncio.CancelledError:
                    raise
                except TriageValidationError as error:
                    kind = f"invalid:{error.code}"
                except Exception as error:  # noqa: BLE001 - provider stays recoverable
                    kind = f"provider:{type(error).__name__}"
                if kind == "scored":
                    to_save.extend(scoreable)
                else:
                    bumped = await self.repo.bump_triage_attempts(
                        [candidate.article_id for candidate in scoreable]
                    )
                    log.warning(
                        "triage batch failed domain=%s reason=%s candidates=%d bumped=%d",
                        group_name,
                        kind,
                        len(scoreable),
                        bumped,
                    )
                    if not require_relevance:
                        # Core keeps the existing max-attempt neutral fallback.
                        exhausted = [
                            candidate
                            for candidate in scoreable
                            if attempts_by_id.get(candidate.article_id, 0) + 1
                            >= self.s.triage_max_attempts
                        ]
                        to_save.extend(exhausted)
                        fallbacks += len(exhausted)
            elif require_relevance:
                kind = "invalid:missing_title"
                await self.repo.bump_triage_attempts(
                    [candidate.article_id for candidate in group]
                )

            kinds.append(kind)
            if not to_save:
                continue
            results = build_results(
                to_save,
                axes_by_id=axes_by_id,
                duplicates=duplicates,
                mode="enforce" if require_relevance else self.s.triage_mode,
            )
            saved_total += await self.repo.save_triage_scores(
                [
                    {
                        "article_id": result.article_id,
                        "score": result.score,
                        "signals": result.signals,
                    }
                    for result in results
                ],
                model=self.scorer.model,
            )

        result_kind = next(
            (kind for kind in kinds if kind != "scored"),
            "scored",
        )
        if fallbacks:
            result_kind = "fallback"
        log.info(
            "triage batch mode=%s scored=%d duplicates=%d fallback=%d",
            self.s.triage_mode,
            saved_total,
            len(duplicates),
            fallbacks,
        )
        return {
            "kind": result_kind,
            "scored": saved_total,
            "deleted": purged,
        }

    async def _duplicates(self, candidates: list[TriageCandidate]) -> set[str]:
        if not candidates:
            return set()
        keys = [normalized_title(candidate.title) for candidate in candidates]
        ids = [candidate.article_id for candidate in candidates]
        seen: dict[str, str] = {}
        duplicates: set[str] = set()
        for article_id, key in zip(ids, keys):
            if not key:
                continue
            if key in seen:
                duplicates.add(article_id)
            else:
                seen[key] = article_id
        try:
            stored = await self.repo.duplicate_title_ids(
                article_ids=ids,
                title_keys=[key.strip() for key in keys],
                window_hours=DUPLICATE_WINDOW_HOURS,
            )
        except Exception:  # noqa: BLE001 - duplicate detection is enrichment
            log.warning("triage duplicate lookup skipped code=duplicate_lookup_failed")
            return duplicates
        return duplicates | stored

    async def _purge_if_due(self) -> int:
        if not self.s.map_candidate_purge_enabled:
            return 0
        interval = self.s.map_candidate_purge_interval_s
        now = self.clock()
        if self._last_purge_at is not None and now - self._last_purge_at < interval:
            return 0
        self._last_purge_at = now
        try:
            result = await self.repo.purge_expired_candidates(
                ttl_days=self.s.map_candidate_ttl_days,
                limit=self.s.map_candidate_purge_limit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - retention retries next cycle
            log.warning("candidate purge failed error=%s", type(error).__name__)
            return 0
        deleted = int(result.get("deleted", 0))
        if deleted:
            log.info("candidate purge deleted=%d", deleted)
        return deleted

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - keep the loop alive
                    log.warning(
                        "triage poll failed error=%s", type(error).__name__
                    )
                await asyncio.sleep(self.s.triage_poll_seconds)
        finally:
            if self._task is asyncio.current_task():
                self._task = None


__all__ = ["TriageWorker", "NEUTRAL_SCORE"]
