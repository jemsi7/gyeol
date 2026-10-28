"""Derived pgvector store for cross-modal article-image retrieval.

The main PostgreSQL database remains the source of truth.  Everything in this
module can be dropped and rebuilt from article image metadata + Bucket bytes.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import asyncpg

from .config import Settings

log = logging.getLogger("gdelt.image_rag.vector")

PGVECTOR_VERSION = "0.8.2"
VECTOR_DIMENSIONS = 768
_ERROR_CODE_RE = re.compile(r"[^a-z0-9_]+")

VECTOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_embeddings (
    image_id            TEXT NOT NULL,
    model               TEXT NOT NULL,
    dimensions          SMALLINT NOT NULL CHECK (dimensions = 768),
    embedding           vector(768),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','processing','retry','indexed','dead')),
    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_code          TEXT,
    source_content_type TEXT,
    source_byte_size    INTEGER NOT NULL DEFAULT 0 CHECK (source_byte_size >= 0),
    embedded_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (image_id, model, dimensions),
    CHECK (status <> 'indexed' OR embedding IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS image_embedding_cursors (
    model              TEXT NOT NULL,
    dimensions         SMALLINT NOT NULL CHECK (dimensions = 768),
    last_downloaded_at TIMESTAMPTZ,
    last_image_id      TEXT NOT NULL DEFAULT '',
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model, dimensions)
);

CREATE INDEX IF NOT EXISTS idx_image_embeddings_due
    ON image_embeddings (status, next_attempt_at, updated_at, image_id);

CREATE INDEX IF NOT EXISTS idx_image_embeddings_hnsw_768
    ON image_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE status = 'indexed'
      AND model = 'google/gemini-embedding-2'
      AND dimensions = 768;
"""


class VectorStoreError(RuntimeError):
    """A redacted vector-store failure safe to propagate to RAG callers."""


@dataclass(frozen=True, slots=True)
class ImageCursor:
    downloaded_at: datetime | None = None
    image_id: str = ""


@dataclass(frozen=True, slots=True)
class VectorWorkItem:
    image_id: str
    content_type: str | None
    byte_size: int
    attempts: int


@dataclass(frozen=True, slots=True)
class VectorHit:
    image_id: str
    similarity: float


class VectorStore:
    """Small asyncpg repository around the separate pgvector service."""

    def __init__(
        self,
        settings: Settings,
        pool: asyncpg.Pool | Any | None = None,
    ) -> None:
        self.s = settings
        self._pool = pool
        self._owns_pool = pool is None
        self.ready = False

    @property
    def configured(self) -> bool:
        return self.s.image_rag_enabled and bool(self.s.vector_asyncpg_dsn())

    @property
    def pool(self) -> asyncpg.Pool | Any:
        if self._pool is None:
            raise VectorStoreError("vector_store_not_connected")
        return self._pool

    async def start(self) -> bool:
        if not self.configured:
            self.ready = False
            return False
        try:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=self.s.vector_asyncpg_dsn(),
                    min_size=1,
                    max_size=3,
                    command_timeout=30,
                )
            await self.ensure_schema()
        except Exception:  # noqa: BLE001 - caller only gets a redacted state
            log.warning("image RAG vector store unavailable code=start_failed")
            await self.close()
            self.ready = False
            return False
        self.ready = True
        return True

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        self.ready = False
        if self._owns_pool and pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                log.warning("image RAG vector store close failed code=close_failed")

    async def ensure_schema(self) -> None:
        await self.pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
        version = await self.pool.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        if version != PGVECTOR_VERSION:
            raise VectorStoreError("unsupported_pgvector_version")
        await self.pool.execute(VECTOR_SCHEMA_SQL)

    async def get_cursor(self) -> ImageCursor:
        row = await self.pool.fetchrow(
            """
            SELECT last_downloaded_at, last_image_id
            FROM image_embedding_cursors
            WHERE model=$1 AND dimensions=$2
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
        )
        if not row:
            return ImageCursor()
        return ImageCursor(
            downloaded_at=row["last_downloaded_at"],
            image_id=str(row["last_image_id"] or ""),
        )

    async def enqueue_discovered(
        self,
        rows: list[dict],
        cursor: ImageCursor,
    ) -> int:
        values = [
            (
                str(row["id"]),
                self.s.image_rag_model,
                self.s.image_rag_dimensions,
                str(row.get("content_type") or "") or None,
                max(0, int(row.get("byte_size") or 0)),
            )
            for row in rows
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if values:
                    await conn.executemany(
                        """
                        INSERT INTO image_embeddings
                            (image_id, model, dimensions, source_content_type,
                             source_byte_size)
                        VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (image_id, model, dimensions) DO NOTHING
                        """,
                        values,
                    )
                await conn.execute(
                    """
                    INSERT INTO image_embedding_cursors
                        (model, dimensions, last_downloaded_at, last_image_id, updated_at)
                    VALUES ($1,$2,$3,$4,now())
                    ON CONFLICT (model, dimensions) DO UPDATE SET
                        last_downloaded_at = EXCLUDED.last_downloaded_at,
                        last_image_id = EXCLUDED.last_image_id,
                        updated_at = now()
                    """,
                    self.s.image_rag_model,
                    self.s.image_rag_dimensions,
                    cursor.downloaded_at,
                    cursor.image_id,
                )
        return len(values)

    async def claim_due(self, limit: int) -> list[VectorWorkItem]:
        bounded = max(0, min(int(limit), self.s.image_rag_sync_limit))
        if bounded == 0:
            return []
        rows = await self.pool.fetch(
            """
            WITH due AS (
                SELECT image_id, model, dimensions
                FROM image_embeddings
                WHERE model=$1 AND dimensions=$2
                  AND attempts < $3
                  AND (
                    (status IN ('queued','retry') AND next_attempt_at <= now())
                    OR (status='processing' AND updated_at <= now() - interval '30 minutes')
                  )
                ORDER BY next_attempt_at, updated_at, image_id
                FOR UPDATE SKIP LOCKED
                LIMIT $4
            )
            UPDATE image_embeddings AS target
            SET status='processing', attempts=target.attempts+1,
                error_code=NULL, updated_at=now()
            FROM due
            WHERE target.image_id=due.image_id
              AND target.model=due.model
              AND target.dimensions=due.dimensions
            RETURNING target.image_id, target.source_content_type,
                      target.source_byte_size, target.attempts
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            self.s.image_rag_max_attempts,
            bounded,
        )
        return [
            VectorWorkItem(
                image_id=str(row["image_id"]),
                content_type=row["source_content_type"],
                byte_size=int(row["source_byte_size"] or 0),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    async def mark_indexed(self, image_id: str, embedding: list[float]) -> None:
        literal = embedding_literal(embedding, dimensions=self.s.image_rag_dimensions)
        await self.pool.execute(
            """
            UPDATE image_embeddings
            SET embedding=$4::vector(768), status='indexed', error_code=NULL,
                embedded_at=now(), next_attempt_at=now(), updated_at=now()
            WHERE image_id=$1 AND model=$2 AND dimensions=$3
            """,
            image_id,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            literal,
        )

    async def mark_retry(self, image_ids: list[str], code: str) -> None:
        if not image_ids:
            return
        await self.pool.execute(
            """
            UPDATE image_embeddings
            SET status = CASE WHEN attempts >= $4 THEN 'dead' ELSE 'retry' END,
                error_code=$5,
                next_attempt_at = now() + LEAST(
                    interval '6 hours',
                    power(2, GREATEST(attempts - 1, 0)) * interval '60 seconds'
                ),
                updated_at=now()
            WHERE image_id=ANY($1::text[]) AND model=$2 AND dimensions=$3
            """,
            image_ids,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            self.s.image_rag_max_attempts,
            error_code(code),
        )

    async def mark_dead(self, image_ids: list[str], code: str) -> None:
        if not image_ids:
            return
        await self.pool.execute(
            """
            UPDATE image_embeddings
            SET status='dead', error_code=$4, next_attempt_at=now(), updated_at=now()
            WHERE image_id=ANY($1::text[]) AND model=$2 AND dimensions=$3
            """,
            image_ids,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            error_code(code),
        )

    async def search(
        self,
        embedding: list[float],
        *,
        limit: int,
        min_similarity: float,
        exclude_ids: set[str],
    ) -> list[VectorHit]:
        bounded = max(0, min(int(limit), self.s.image_rag_max_candidates))
        threshold = float(min_similarity)
        if bounded == 0 or not 0.0 <= threshold <= 1.0:
            return []
        literal = embedding_literal(embedding, dimensions=self.s.image_rag_dimensions)
        rows = await self.pool.fetch(
            """
            SELECT image_id, 1 - (embedding <=> $4::vector(768)) AS similarity
            FROM image_embeddings
            WHERE model=$1 AND dimensions=$2 AND status='indexed'
              AND embedding IS NOT NULL
              AND NOT (image_id = ANY($5::text[]))
              AND 1 - (embedding <=> $4::vector(768)) >= $3
            ORDER BY embedding <=> $4::vector(768), image_id
            LIMIT $6
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            threshold,
            literal,
            sorted(exclude_ids),
            bounded,
        )
        hits: list[VectorHit] = []
        for row in rows:
            similarity = float(row["similarity"])
            if math.isfinite(similarity) and similarity >= threshold:
                hits.append(VectorHit(str(row["image_id"]), similarity))
        return hits


def embedding_literal(values: Iterable[float], *, dimensions: int = VECTOR_DIMENSIONS) -> str:
    vector = list(values)
    if len(vector) != dimensions:
        raise ValueError(f"embedding must have exactly {dimensions} dimensions")
    floats: list[float] = []
    for value in vector:
        if isinstance(value, bool):
            raise ValueError("embedding values must be finite numbers")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding values must be finite numbers") from exc
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite numbers")
        floats.append(number)
    return "[" + ",".join(repr(value) for value in floats) + "]"


def error_code(value: str) -> str:
    normalized = _ERROR_CODE_RE.sub("_", str(value).strip().lower()).strip("_")
    return (normalized or "unknown_error")[:64]
