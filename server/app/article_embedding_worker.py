"""Bounded background article-text embedding and progress service."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .article_vector_store import (
    ArticleEmbeddingCursor,
    ArticleEmbeddingProgress,
    ArticleEmbeddingTarget,
    ArticleVectorHit,
    ArticleVectorStore,
    ArticleVectorWorkItem,
)
from .config import Settings
from .openrouter import (
    OpenRouterPermanentError,
    OpenRouterRateLimiter,
    OpenRouterTransientError,
)
from .openrouter_embeddings import OpenRouterEmbeddingClient


log = logging.getLogger("gdelt.article_embeddings")
_RECONNECT_COOLDOWN_S = 60.0


@dataclass(frozen=True, slots=True)
class ArticleEmbeddingSource:
    text: str
    source_hash: str


@dataclass(slots=True)
class ArticleEmbeddingStats:
    discovered: int = 0
    claimed: int = 0
    indexed: int = 0
    retried: int = 0
    dead: int = 0
    disabled: bool = False
    error_code: str | None = None
    progress: ArticleEmbeddingProgress | None = None


def _source_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def build_article_embedding_source(
    map_data: Mapping[str, Any],
    *,
    max_chars: int = 12_000,
) -> ArticleEmbeddingSource:
    """Return the deterministic, deliberately narrow Map text document."""
    if not isinstance(map_data, Mapping):
        raise ValueError("article_embedding_source_empty")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")

    lines: list[str] = []
    for field in ("headline", "subheadline", "summary"):
        value = _source_text(map_data.get(field))
        if value:
            lines.append(f"{field}: {value}")

    keywords = map_data.get("keywords")
    if isinstance(keywords, Sequence) and not isinstance(keywords, (str, bytes)):
        values = [_source_text(value) for value in keywords[:64]]
        values = [value for value in values if value]
        if values:
            lines.append("keywords: " + " | ".join(values))

    scenes = map_data.get("scenes")
    if isinstance(scenes, Sequence) and not isinstance(scenes, (str, bytes)):
        for index, scene in enumerate(scenes[:20], start=1):
            if not isinstance(scene, Mapping):
                continue
            for field in ("title", "summary"):
                value = _source_text(scene.get(field))
                if value:
                    lines.append(f"scene_{index}_{field}: {value}")

    text = "\n".join(lines)[:max_chars].strip()
    if not text:
        raise ValueError("article_embedding_source_empty")
    return ArticleEmbeddingSource(
        text=text,
        source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _empty_progress(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "embedded_count": None,
        "total_count": None,
        "percent": None,
        "updated_at": None,
    }


class ArticleEmbeddingWorker:
    """Poll Map changes and maintain a rebuildable article vector index."""

    def __init__(
        self,
        settings: Settings,
        repo: Any,
        *,
        vector_store: ArticleVectorStore | Any | None = None,
        embeddings: OpenRouterEmbeddingClient | Any | None = None,
    ) -> None:
        self.s = settings
        self.repo = repo
        self.vector = vector_store or ArticleVectorStore(settings)
        self.embeddings = embeddings or OpenRouterEmbeddingClient(
            settings,
            request_limiter=OpenRouterRateLimiter(
                settings.article_embedding_requests_per_minute
            ),
        )
        self.ready = False
        self._embeddings_open = False
        self._last_connect_attempt = 0.0
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def configured(self) -> bool:
        return (
            self.s.article_embedding_enabled
            and bool(self.s.vector_asyncpg_dsn())
            and bool(self.s.openrouter_api_key)
        )

    async def start(self) -> bool:
        """Start one fail-open production poll loop without blocking startup."""
        if not self.configured or (self._task is not None and not self._task.done()):
            return False
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="article-embedding-worker"
        )
        return True

    async def stop(self) -> None:
        self._stop_event.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.ready = False
        try:
            await self.vector.close()
        except Exception:  # noqa: BLE001
            log.warning("article embedding close failed code=vector_close_failed")
        if self._embeddings_open:
            try:
                await self.embeddings.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.warning("article embedding close failed code=client_close_failed")
            finally:
                self._embeddings_open = False

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background index must fail open
                log.warning("article embedding poll skipped code=poll_failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.s.article_embedding_poll_seconds,
                )
            except TimeoutError:
                continue

    async def _ensure_ready(self) -> bool:
        if not self.configured:
            return False
        if self.ready:
            return True
        now = time.monotonic()
        if self._last_connect_attempt and (
            now - self._last_connect_attempt < _RECONNECT_COOLDOWN_S
        ):
            return False
        if not self._embeddings_open:
            await self.embeddings.__aenter__()
            self._embeddings_open = True
        self._last_connect_attempt = now
        self.ready = bool(await self.vector.start())
        return self.ready

    def _target(self, row: Mapping[str, Any]) -> ArticleEmbeddingTarget:
        article_id = str(row.get("article_id") or "")
        if not row.get("eligible"):
            return ArticleEmbeddingTarget(article_id=article_id, eligible=False)
        try:
            source = build_article_embedding_source(
                row.get("map_data"),
                max_chars=self.s.article_embedding_source_max_chars,
            )
        except (TypeError, ValueError):
            return ArticleEmbeddingTarget(
                article_id=article_id,
                eligible=True,
                error_code="empty_source",
            )
        return ArticleEmbeddingTarget(
            article_id=article_id,
            eligible=True,
            source_hash=source.source_hash,
            source_text=source.text,
        )

    async def sync(self, limit: int | None = None) -> ArticleEmbeddingStats:
        stats = ArticleEmbeddingStats(disabled=not self.s.article_embedding_enabled)
        if not self.configured:
            return stats
        if not await self._ensure_ready():
            stats.error_code = "vector_unavailable"
            return stats
        bounded = self.s.article_embedding_sync_limit if limit is None else max(
            0, min(int(limit), self.s.article_embedding_sync_limit)
        )
        if bounded == 0:
            return stats

        try:
            cursor = await self.vector.get_cursor()
            changes = await self.repo.list_article_embedding_changes(
                changed_at=cursor.changed_at,
                article_id=cursor.article_id,
                limit=bounded,
            )
            if changes:
                last = changes[-1]
                next_cursor = ArticleEmbeddingCursor(
                    changed_at=last.get("changed_at"),
                    article_id=str(last.get("article_id") or ""),
                )
                stats.discovered = await self.vector.enqueue_changes(
                    [self._target(row) for row in changes],
                    next_cursor,
                )

            work = await self.vector.claim_due(bounded)
            stats.claimed = len(work)
            if not work:
                return stats
            # The denominator is required before the paid provider call and
            # success ledger, but an idle poll must not repeatedly count all
            # main-DB articles. Return claimed rows immediately on count failure.
            try:
                total_articles = await self.repo.available_article_count()
            except Exception:  # noqa: BLE001 - caller receives stable code below
                await self._retry_or_dead(
                    work,
                    "main_count_unavailable",
                    stats,
                )
                raise
            size = self.s.article_embedding_batch_size
            for offset in range(0, len(work), size):
                batch = work[offset : offset + size]
                try:
                    vectors = await self.embeddings.embed_documents(
                        [item.source_text for item in batch]
                    )
                    if len(vectors) != len(batch):
                        raise OpenRouterTransientError(
                            "embedding response count mismatch"
                        )
                except OpenRouterPermanentError:
                    await self.vector.mark_dead(
                        [item.article_id for item in batch],
                        "embedding_permanent",
                    )
                    stats.dead += len(batch)
                    continue
                except Exception:  # noqa: BLE001
                    await self._retry_or_dead(batch, "embedding_transient", stats)
                    continue

                for item, vector in zip(batch, vectors, strict=True):
                    progress = await self.vector.mark_indexed(
                        item.article_id,
                        item.source_hash,
                        vector,
                        total_articles=total_articles,
                    )
                    if progress is not None:
                        stats.indexed += 1
                        stats.progress = progress
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep Map/feed/runtime alive
            stats.error_code = "sync_failed"
            self.ready = False
            log.warning("article embedding sync skipped code=sync_failed")
        return stats

    async def _retry_or_dead(
        self,
        work: Sequence[ArticleVectorWorkItem],
        code: str,
        stats: ArticleEmbeddingStats,
    ) -> None:
        dead = [
            item.article_id
            for item in work
            if item.attempts >= self.s.article_embedding_max_attempts
        ]
        retry = [
            item.article_id
            for item in work
            if item.attempts < self.s.article_embedding_max_attempts
        ]
        if retry:
            await self.vector.mark_retry(retry, code)
            stats.retried += len(retry)
        if dead:
            await self.vector.mark_dead(dead, code)
            stats.dead += len(dead)

    async def embed_search_query(self, query: str) -> list[float] | None:
        """Embed one transient discovery query without weakening keyword search."""
        if not self.s.article_search_rag_enabled or not self.ready:
            return None
        if not self._embeddings_open:
            return None
        normalized = " ".join(unicodedata.normalize("NFKC", str(query)).split())
        if not 2 <= len(normalized) <= 80:
            return None
        try:
            async with asyncio.timeout(self.s.article_search_rag_timeout_seconds):
                vectors = await self.embeddings.embed_texts([normalized])
            if len(vectors) != 1:
                return None
            return vectors[0]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - semantic search is additive
            log.warning("article search RAG skipped code=query_embedding_failed")
            return None

    async def nearest_search_articles(
        self,
        embedding: list[float],
        *,
        exclude_ids: Sequence[str],
    ) -> list[ArticleVectorHit]:
        """Return bounded cosine hits or an empty additive enhancement."""
        if not self.s.article_search_rag_enabled or not self.ready:
            return []
        try:
            async with asyncio.timeout(self.s.article_search_rag_timeout_seconds):
                return await self.vector.nearest_articles(
                    embedding,
                    exclude_ids=exclude_ids,
                    limit=self.s.article_search_rag_limit,
                    min_similarity=self.s.article_search_rag_min_similarity,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - semantic search is additive
            log.warning("article search RAG skipped code=vector_search_failed")
            return []

    async def progress(self, total_articles: int | None = None) -> dict[str, Any]:
        if not self.s.article_embedding_enabled:
            return _empty_progress("disabled")
        if not self.configured:
            return _empty_progress("unavailable")
        try:
            if not await self._ensure_ready():
                return _empty_progress("unavailable")
            total = (
                await self.repo.available_article_count()
                if total_articles is None
                else max(0, int(total_articles))
            )
            value = await self.vector.progress(total)
            embedded = min(total, max(0, int(value.embedded_count)))
            percent = 0.0
            if total:
                percent = float(
                    (
                        Decimal(embedded) * Decimal(100) / Decimal(total)
                    ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                )
            return {
                "status": "ready",
                "embedded_count": embedded,
                "total_count": total,
                "percent": percent,
                "updated_at": value.updated_at,
            }
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - optional ops metric only
            self.ready = False
            log.warning("article embedding progress unavailable")
            return _empty_progress("unavailable")
