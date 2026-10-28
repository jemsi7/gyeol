"""Derived pgvector queue, index and progress ledger for article text.

The main PostgreSQL database remains the source of truth for Article Maps and
their publication eligibility.  This store contains only rebuildable text,
vectors, queue state, a monotonic discovery cursor and operational backfill
history in the separate vector database.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg

from .article_clustering import (
    CLUSTER_ALGORITHM_VERSION,
    MAX_CLUSTER_CANDIDATES,
    MAX_CLUSTER_EDGES,
    MAX_CLUSTER_NEIGHBORS,
    ArticleCluster,
    ArticleClusterMember,
    ArticleVectorCandidate,
    ArticleVectorEdge,
    ClusterPlan,
    validate_cluster_plan,
)
from .config import Settings
from .vector_store import (
    PGVECTOR_VERSION,
    VectorStoreError,
    embedding_literal,
    error_code,
)

log = logging.getLogger("gdelt.article_embeddings.vector")

ARTICLE_SOURCE_MAX_CHARS = 12_000
ARTICLE_CLAIM_LIMIT = 50
ARTICLE_NEAREST_LIMIT = 60
TIMELINE_SIMILARITY_MAX_CANDIDATES = 20
_SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Two disjoint namespaces.  The scheduler only ever emits the four six-hour
# boundaries; the `manual:` minute-precision form belongs to the /ops refresh,
# which needs a slot the cadence can never take so it can publish a new snapshot
# without deleting the one the current slot already holds.
_CLUSTER_SLOT_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T(?:00|06|12|18):00:00Z"
    r"|manual:\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\dZ)$"
)
_MANUAL_CLUSTER_SLOT_PREFIX = "manual:"


ARTICLE_VECTOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS article_text_embeddings (
    article_id      TEXT NOT NULL,
    model           TEXT NOT NULL,
    dimensions      SMALLINT NOT NULL CHECK (dimensions = 768),
    source_hash     TEXT CHECK (
                        source_hash IS NULL OR char_length(source_hash) = 64
                    ),
    source_text     TEXT CHECK (
                        source_text IS NULL
                        OR char_length(source_text) <= 12000
                    ),
    embedding       vector(768),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN (
                        'queued','processing','retry','indexed','dead','inactive'
                    )),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error_code      TEXT,
    embedded_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, model, dimensions),
    CHECK (
        status NOT IN ('queued','processing','retry')
        OR (
            source_hash IS NOT NULL
            AND char_length(source_hash) = 64
            AND source_text IS NOT NULL
            AND char_length(source_text) BETWEEN 1 AND 12000
        )
    ),
    CHECK (
        status <> 'indexed'
        OR (embedding IS NOT NULL AND source_hash IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS article_embedding_cursors (
    model           TEXT NOT NULL,
    dimensions      SMALLINT NOT NULL CHECK (dimensions = 768),
    last_changed_at TIMESTAMPTZ,
    last_article_id TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model, dimensions)
);

CREATE TABLE IF NOT EXISTS article_embedding_progress_history (
    id                 BIGSERIAL PRIMARY KEY,
    model              TEXT NOT NULL,
    dimensions         SMALLINT NOT NULL CHECK (dimensions = 768),
    trigger_article_id TEXT NOT NULL,
    embedded_articles  BIGINT NOT NULL CHECK (embedded_articles >= 0),
    total_articles     BIGINT NOT NULL CHECK (total_articles >= 0),
    coverage_percent   NUMERIC(5,1) NOT NULL
                       CHECK (coverage_percent BETWEEN 0.0 AND 100.0),
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_article_text_embeddings_due
    ON article_text_embeddings (status, next_attempt_at, updated_at, article_id);

CREATE INDEX IF NOT EXISTS idx_article_embedding_progress_recent
    ON article_embedding_progress_history (model, dimensions, id DESC);

CREATE INDEX IF NOT EXISTS idx_article_text_embeddings_hnsw_768
    ON article_text_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE status = 'indexed'
      AND model = 'google/gemini-embedding-2'
      AND dimensions = 768;

CREATE TABLE IF NOT EXISTS article_cluster_snapshots (
    id                UUID PRIMARY KEY,
    input_hash        CHAR(64) NOT NULL UNIQUE,
    slot              TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'processing'
                      CHECK (status IN (
                          'queued','processing','published','failed','superseded'
                      )),
    model             TEXT NOT NULL,
    dimensions        SMALLINT NOT NULL CHECK (dimensions = 768),
    algorithm_version TEXT NOT NULL,
    article_count     INTEGER NOT NULL CHECK (
                          article_count BETWEEN 0 AND 500
                      ),
    indexed_count     INTEGER NOT NULL CHECK (
                          indexed_count BETWEEN 0 AND article_count
                      ),
    coverage          NUMERIC(5,4) NOT NULL CHECK (
                          coverage BETWEEN 0.0 AND 1.0
                      ),
    generated_at      TIMESTAMPTZ,
    valid_until       TIMESTAMPTZ,
    error_code        TEXT,
    lease_expires_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_article_cluster_snapshot_current
    ON article_cluster_snapshots (status, valid_until, generated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_article_cluster_one_processing
    ON article_cluster_snapshots ((status)) WHERE status='processing';

CREATE TABLE IF NOT EXISTS article_clusters (
    snapshot_id        UUID NOT NULL REFERENCES article_cluster_snapshots(id)
                       ON DELETE CASCADE,
    cluster_key        CHAR(64) NOT NULL,
    rank               SMALLINT NOT NULL CHECK (rank BETWEEN 1 AND 8),
    medoid_article_id  TEXT NOT NULL,
    member_count       INTEGER NOT NULL CHECK (member_count BETWEEN 3 AND 500),
    domain_count       INTEGER NOT NULL CHECK (
                           domain_count BETWEEN 2 AND member_count
                       ),
    first_published_at TIMESTAMPTZ NOT NULL,
    last_published_at  TIMESTAMPTZ NOT NULL,
    mean_edge_similarity DOUBLE PRECISION NOT NULL CHECK (
                           mean_edge_similarity BETWEEN 0.0 AND 1.0
                       ),
    major_score        DOUBLE PRECISION NOT NULL CHECK (
                           major_score BETWEEN 0.0 AND 1.0
                       ),
    PRIMARY KEY (snapshot_id, cluster_key),
    UNIQUE (snapshot_id, rank)
);

CREATE TABLE IF NOT EXISTS article_cluster_members (
    snapshot_id          UUID NOT NULL,
    cluster_key          CHAR(64) NOT NULL,
    article_id           TEXT NOT NULL,
    similarity_to_medoid DOUBLE PRECISION NOT NULL CHECK (
                             similarity_to_medoid BETWEEN 0.0 AND 1.0
                         ),
    centrality_rank      INTEGER NOT NULL CHECK (
                             centrality_rank BETWEEN 1 AND 500
                         ),
    PRIMARY KEY (snapshot_id, cluster_key, article_id),
    UNIQUE (snapshot_id, cluster_key, centrality_rank),
    FOREIGN KEY (snapshot_id, cluster_key)
        REFERENCES article_clusters(snapshot_id, cluster_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_cluster_members_article
    ON article_cluster_members (article_id, snapshot_id);
"""


_ENQUEUE_SQL = """
INSERT INTO article_text_embeddings AS target
    (article_id, model, dimensions, source_hash, source_text, embedding,
     status, attempts, next_attempt_at, error_code, embedded_at, updated_at)
VALUES (
    $1, $2, $3,
    CASE WHEN NOT $4 OR $7::text IS NOT NULL THEN NULL ELSE $5 END,
    CASE WHEN NOT $4 OR $7::text IS NOT NULL THEN NULL ELSE $6 END,
    NULL,
    CASE WHEN NOT $4 THEN 'inactive'
         WHEN $7::text IS NOT NULL THEN 'dead'
         ELSE 'queued' END,
    0,
    now(),
    CASE WHEN $4 THEN $7::text ELSE NULL END,
    NULL,
    now()
)
ON CONFLICT (article_id, model, dimensions) DO UPDATE SET
    status = CASE
        WHEN NOT $4 THEN 'inactive'
        WHEN $7::text IS NOT NULL THEN 'dead'
        WHEN target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN 'queued'
        ELSE target.status
    END,
    source_hash = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL THEN NULL
        WHEN target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN EXCLUDED.source_hash
        ELSE target.source_hash
    END,
    source_text = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL THEN NULL
        WHEN target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN EXCLUDED.source_text
        ELSE target.source_text
    END,
    embedding = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL
             OR target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN NULL
        ELSE target.embedding
    END,
    attempts = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL
             OR target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN 0
        ELSE target.attempts
    END,
    next_attempt_at = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL
             OR target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN now()
        ELSE target.next_attempt_at
    END,
    error_code = CASE
        WHEN NOT $4 THEN NULL
        WHEN $7::text IS NOT NULL THEN $7::text
        WHEN target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN NULL
        ELSE target.error_code
    END,
    embedded_at = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL
             OR target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN NULL
        ELSE target.embedded_at
    END,
    updated_at = CASE
        WHEN NOT $4 OR $7::text IS NOT NULL
             OR target.source_hash IS DISTINCT FROM EXCLUDED.source_hash
             OR target.status = 'inactive' THEN now()
        ELSE target.updated_at
    END
"""


_CURSOR_SQL = """
INSERT INTO article_embedding_cursors
    (model, dimensions, last_changed_at, last_article_id, updated_at)
VALUES ($1, $2, $3, $4, now())
ON CONFLICT (model, dimensions) DO UPDATE SET
    last_changed_at = EXCLUDED.last_changed_at,
    last_article_id = EXCLUDED.last_article_id,
    updated_at = now()
WHERE EXCLUDED.last_changed_at IS NOT NULL
  AND (
      article_embedding_cursors.last_changed_at IS NULL
      OR (EXCLUDED.last_changed_at, EXCLUDED.last_article_id)
         > (article_embedding_cursors.last_changed_at,
            article_embedding_cursors.last_article_id)
  )
"""


_MARK_INDEXED_SQL = """
WITH indexed_before AS (
    SELECT count(*)::bigint AS embedded_articles
    FROM article_text_embeddings
    WHERE model=$2 AND dimensions=$3 AND status='indexed'
), indexed AS (
    UPDATE article_text_embeddings
    SET embedding=$5::vector(768), status='indexed', error_code=NULL,
        embedded_at=now(), next_attempt_at=now(), updated_at=now()
    WHERE article_id=$1 AND model=$2 AND dimensions=$3
      AND source_hash=$4 AND status='processing'
    RETURNING article_id
), progress_value AS (
    SELECT indexed.article_id,
           LEAST(
               $6::bigint,
               GREATEST(0::bigint, indexed_before.embedded_articles + 1)
           ) AS embedded_articles,
           $6::bigint AS total_articles
    FROM indexed CROSS JOIN indexed_before
), recorded AS (
    INSERT INTO article_embedding_progress_history
        (model, dimensions, trigger_article_id, embedded_articles,
         total_articles, coverage_percent)
    SELECT $2, $3, article_id, embedded_articles, total_articles,
           CASE WHEN total_articles = 0 THEN 0.0::numeric
                ELSE round(
                    embedded_articles::numeric * 100 / total_articles,
                    1
                ) END
    FROM progress_value
    RETURNING embedded_articles, total_articles, coverage_percent, recorded_at
)
SELECT embedded_articles, total_articles, coverage_percent, recorded_at
FROM recorded
"""


@dataclass(frozen=True, slots=True)
class ArticleEmbeddingCursor:
    changed_at: datetime | None = None
    article_id: str = ""


@dataclass(frozen=True, slots=True)
class ArticleEmbeddingTarget:
    article_id: str
    eligible: bool
    source_hash: str | None = None
    source_text: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ArticleVectorWorkItem:
    article_id: str
    source_hash: str
    source_text: str
    attempts: int


@dataclass(frozen=True, slots=True)
class ArticleEmbeddingProgress:
    embedded_count: int
    total_count: int
    percent: float
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ArticleVectorHit:
    article_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class ArticleClusterSnapshot:
    id: UUID
    input_hash: str
    slot: str
    status: str
    model: str
    dimensions: int
    algorithm_version: str
    article_count: int
    indexed_count: int
    coverage: float
    generated_at: datetime | None
    valid_until: datetime | None
    clusters: tuple[ArticleCluster, ...] = ()


class ArticleVectorStore:
    """Small asyncpg repository around the article-text vector namespace."""

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
        return bool(self.s.vector_asyncpg_dsn())

    @property
    def pool(self) -> asyncpg.Pool | Any:
        if self._pool is None:
            raise VectorStoreError("article_vector_store_not_connected")
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
        except Exception:  # noqa: BLE001 - expose only a stable availability bit
            log.warning("article vector store unavailable code=start_failed")
            await self.close()
            return False
        self.ready = True
        return True

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        self.ready = False
        if self._owns_pool and pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001 - credentials/errors stay redacted
                log.warning("article vector store close failed code=close_failed")

    async def ensure_schema(self) -> None:
        await self.pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
        version = await self.pool.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        if version != PGVECTOR_VERSION:
            raise VectorStoreError("unsupported_pgvector_version")
        await self.pool.execute(ARTICLE_VECTOR_SCHEMA_SQL)

    async def get_cursor(self) -> ArticleEmbeddingCursor:
        row = await self.pool.fetchrow(
            """
            SELECT last_changed_at, last_article_id
            FROM article_embedding_cursors
            WHERE model=$1 AND dimensions=$2
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
        )
        if not row:
            return ArticleEmbeddingCursor()
        return ArticleEmbeddingCursor(
            changed_at=row["last_changed_at"],
            article_id=str(row["last_article_id"] or ""),
        )

    async def enqueue_changes(
        self,
        targets: Sequence[ArticleEmbeddingTarget],
        cursor: ArticleEmbeddingCursor,
    ) -> int:
        _validate_cursor(cursor)
        values = [_target_values(self.s, target) for target in targets]
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                if values:
                    await connection.executemany(_ENQUEUE_SQL, values)
                await connection.execute(
                    _CURSOR_SQL,
                    self.s.image_rag_model,
                    self.s.image_rag_dimensions,
                    cursor.changed_at,
                    cursor.article_id,
                )
        return len(values)

    async def claim_due(self, limit: int) -> list[ArticleVectorWorkItem]:
        bounded = max(0, min(int(limit), ARTICLE_CLAIM_LIMIT))
        if bounded == 0:
            return []
        max_attempts = int(getattr(self.s, "article_embedding_max_attempts", 5))
        rows = await self.pool.fetch(
            """
            WITH exhausted AS (
                UPDATE article_text_embeddings
                SET status='dead', error_code='lease_expired',
                    next_attempt_at=now(), updated_at=now()
                WHERE model=$1 AND dimensions=$2
                  AND status='processing' AND attempts >= $3
                  AND updated_at <= now() - interval '30 minutes'
                RETURNING article_id
            ), due AS (
                SELECT article_id, model, dimensions
                FROM article_text_embeddings
                WHERE model=$1 AND dimensions=$2
                  AND attempts < $3
                  AND source_hash IS NOT NULL AND source_text IS NOT NULL
                  AND (
                    (status IN ('queued','retry') AND next_attempt_at <= now())
                    OR (status='processing'
                        AND updated_at <= now() - interval '30 minutes')
                  )
                ORDER BY next_attempt_at, updated_at, article_id
                FOR UPDATE SKIP LOCKED
                LIMIT $4
            )
            UPDATE article_text_embeddings AS target
            SET status='processing', attempts=target.attempts+1,
                error_code=NULL, updated_at=now()
            FROM due
            WHERE target.article_id=due.article_id
              AND target.model=due.model
              AND target.dimensions=due.dimensions
            RETURNING target.article_id, target.source_hash, target.source_text,
                      target.attempts
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            max_attempts,
            bounded,
        )
        return [
            ArticleVectorWorkItem(
                article_id=str(row["article_id"]),
                source_hash=str(row["source_hash"]),
                source_text=str(row["source_text"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    async def mark_indexed(
        self,
        article_id: str,
        source_hash: str,
        embedding: list[float],
        *,
        total_articles: int,
    ) -> ArticleEmbeddingProgress | None:
        _validate_article_id(article_id)
        _validate_source_hash(source_hash)
        total = _bounded_total(total_articles)
        literal = embedding_literal(
            embedding,
            dimensions=self.s.image_rag_dimensions,
        )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                # Separate commands are intentional: waiting for this lock before
                # the CTE starts gives each concurrent success a fresh count
                # snapshot while UPDATE + history INSERT remain one SQL statement.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    (
                        "article-embedding-progress:"
                        f"{self.s.image_rag_model}:{self.s.image_rag_dimensions}"
                    ),
                )
                row = await connection.fetchrow(
                    _MARK_INDEXED_SQL,
                    article_id,
                    self.s.image_rag_model,
                    self.s.image_rag_dimensions,
                    source_hash,
                    literal,
                    total,
                )
        if row is None:
            return None
        return ArticleEmbeddingProgress(
            embedded_count=int(row["embedded_articles"]),
            total_count=int(row["total_articles"]),
            percent=float(Decimal(str(row["coverage_percent"]))),
            updated_at=row["recorded_at"],
        )

    async def mark_retry(self, article_ids: list[str], code: str) -> None:
        ids = _article_ids(article_ids)
        if not ids:
            return
        max_attempts = int(getattr(self.s, "article_embedding_max_attempts", 5))
        await self.pool.execute(
            """
            UPDATE article_text_embeddings
            SET status = CASE WHEN attempts >= $4 THEN 'dead' ELSE 'retry' END,
                error_code=$5,
                next_attempt_at = now() + LEAST(
                    interval '6 hours',
                    power(2, GREATEST(attempts - 1, 0)) * interval '60 seconds'
                ),
                updated_at=now()
            WHERE article_id=ANY($1::text[]) AND model=$2 AND dimensions=$3
              AND status='processing'
            """,
            ids,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            max_attempts,
            error_code(code),
        )

    async def mark_dead(self, article_ids: list[str], code: str) -> None:
        ids = _article_ids(article_ids)
        if not ids:
            return
        await self.pool.execute(
            """
            UPDATE article_text_embeddings
            SET status='dead', error_code=$4, next_attempt_at=now(),
                updated_at=now()
            WHERE article_id=ANY($1::text[]) AND model=$2 AND dimensions=$3
              AND status='processing'
            """,
            ids,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            error_code(code),
        )

    async def progress(self, total_articles: int) -> ArticleEmbeddingProgress:
        total = _bounded_total(total_articles)
        row = await self.pool.fetchrow(
            """
            SELECT count(*) FILTER (WHERE status='indexed') AS embedded_count,
                   (
                       SELECT recorded_at
                       FROM article_embedding_progress_history
                       WHERE model=$1 AND dimensions=$2
                       ORDER BY id DESC
                       LIMIT 1
                   ) AS updated_at
            FROM article_text_embeddings
            WHERE model=$1 AND dimensions=$2
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
        )
        raw_count = int(row["embedded_count"] or 0) if row else 0
        embedded = min(total, max(0, raw_count))
        percent = _coverage_percent(embedded, total)
        return ArticleEmbeddingProgress(
            embedded_count=embedded,
            total_count=total,
            percent=percent,
            updated_at=row["updated_at"] if row else None,
        )

    async def article_knn_graph(
        self,
        candidates: Sequence[ArticleVectorCandidate],
        *,
        neighbors: int,
        min_similarity: float,
    ) -> list[ArticleVectorEdge]:
        """Return a bounded directed kNN graph for exact current source hashes."""
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a sequence")
        if len(candidates) > MAX_CLUSTER_CANDIDATES:
            raise ValueError("article cluster candidate limit exceeds 500")
        if (
            isinstance(neighbors, bool)
            or not isinstance(neighbors, int)
            or not 1 <= neighbors <= MAX_CLUSTER_NEIGHBORS
            or isinstance(min_similarity, bool)
            or not isinstance(min_similarity, (int, float))
            or not math.isfinite(float(min_similarity))
            or not 0.0 <= float(min_similarity) <= 1.0
        ):
            raise ValueError("invalid article kNN bounds")
        candidate_by_id: dict[str, ArticleVectorCandidate] = {}
        for candidate in candidates:
            if not isinstance(candidate, ArticleVectorCandidate):
                raise TypeError("candidates must contain ArticleVectorCandidate values")
            if candidate.article_id in candidate_by_id:
                raise ValueError("article kNN candidate IDs must be unique")
            candidate_by_id[candidate.article_id] = candidate
        if not candidate_by_id:
            return []

        ordered = [candidate_by_id[key] for key in sorted(candidate_by_id)]
        rows = await self.pool.fetch(
            """
            WITH requested AS (
                SELECT article_id, source_hash
                FROM unnest($1::text[], $2::text[])
                     AS requested(article_id, source_hash)
            ), eligible AS MATERIALIZED (
                SELECT stored.article_id, stored.embedding
                FROM article_text_embeddings AS stored
                JOIN requested
                  ON requested.article_id=stored.article_id
                 AND requested.source_hash=stored.source_hash
                WHERE stored.model=$3 AND stored.dimensions=$4
                  AND stored.status='indexed' AND stored.embedding IS NOT NULL
            )
            SELECT seed.article_id AS source_article_id,
                   neighbor.article_id AS target_article_id,
                   neighbor.similarity
            FROM eligible AS seed
            CROSS JOIN LATERAL (
                SELECT target.article_id,
                       1 - (target.embedding <=> seed.embedding) AS similarity
                FROM eligible AS target
                WHERE target.article_id<>seed.article_id
                  AND 1 - (target.embedding <=> seed.embedding) >= $5
                ORDER BY target.embedding <=> seed.embedding, target.article_id
                LIMIT $6
            ) AS neighbor
            ORDER BY seed.article_id, neighbor.similarity DESC, neighbor.article_id
            """,
            [candidate.article_id for candidate in ordered],
            [candidate.source_hash for candidate in ordered],
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            float(min_similarity),
            neighbors,
        )
        if len(rows) > min(MAX_CLUSTER_EDGES, len(ordered) * neighbors):
            raise VectorStoreError("invalid_article_knn_result")
        edges: list[ArticleVectorEdge] = []
        seen_pairs: set[tuple[str, str]] = set()
        source_counts: dict[str, int] = {}
        for row in rows:
            source = str(row["source_article_id"])
            target = str(row["target_article_id"])
            pair = (source, target)
            try:
                edge = ArticleVectorEdge(source, target, float(row["similarity"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise VectorStoreError("invalid_article_knn_result") from exc
            if (
                source not in candidate_by_id
                or target not in candidate_by_id
                or pair in seen_pairs
                or edge.similarity < float(min_similarity)
            ):
                raise VectorStoreError("invalid_article_knn_result")
            source_counts[source] = source_counts.get(source, 0) + 1
            if source_counts[source] > neighbors:
                raise VectorStoreError("invalid_article_knn_result")
            seen_pairs.add(pair)
            edges.append(edge)
        edges.sort(
            key=lambda edge: (
                edge.source_article_id,
                -edge.similarity,
                edge.target_article_id,
            )
        )
        return edges

    async def article_similarity_graph(
        self,
        candidates: Sequence[ArticleVectorCandidate],
        *,
        min_similarity: float,
    ) -> list[ArticleVectorEdge]:
        """Return every bounded rerank-window pair above a similarity floor.

        Timeline node selection needs to reject paraphrases before it chooses
        its 4--8 display nodes.  Unlike the clustering kNN graph, this is an
        exact, small (at most 20 articles) pairwise graph so a duplicate cannot
        be hidden just beyond a nearest-neighbour cutoff.
        """
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a sequence")
        if len(candidates) > TIMELINE_SIMILARITY_MAX_CANDIDATES:
            raise ValueError("timeline similarity candidate limit exceeds 20")
        if (
            isinstance(min_similarity, bool)
            or not isinstance(min_similarity, (int, float))
            or not math.isfinite(float(min_similarity))
            or not 0.0 <= float(min_similarity) <= 1.0
        ):
            raise ValueError("invalid timeline similarity bounds")
        candidate_by_id: dict[str, ArticleVectorCandidate] = {}
        for candidate in candidates:
            if not isinstance(candidate, ArticleVectorCandidate):
                raise TypeError("candidates must contain ArticleVectorCandidate values")
            if candidate.article_id in candidate_by_id:
                raise ValueError("timeline similarity article IDs must be unique")
            candidate_by_id[candidate.article_id] = candidate
        if not candidate_by_id:
            return []

        ordered = [candidate_by_id[key] for key in sorted(candidate_by_id)]
        rows = await self.pool.fetch(
            """
            WITH requested AS (
                SELECT article_id, source_hash
                FROM unnest($1::text[], $2::text[])
                     AS requested(article_id, source_hash)
            ), eligible AS MATERIALIZED (
                SELECT stored.article_id, stored.embedding
                FROM article_text_embeddings AS stored
                JOIN requested
                  ON requested.article_id=stored.article_id
                 AND requested.source_hash=stored.source_hash
                WHERE stored.model=$3 AND stored.dimensions=$4
                  AND stored.status='indexed' AND stored.embedding IS NOT NULL
            )
            SELECT seed.article_id AS source_article_id,
                   target.article_id AS target_article_id,
                   1 - (target.embedding <=> seed.embedding) AS similarity
            FROM eligible AS seed
            JOIN eligible AS target ON target.article_id<>seed.article_id
            WHERE 1 - (target.embedding <=> seed.embedding) >= $5
            ORDER BY seed.article_id, similarity DESC, target.article_id
            """,
            [candidate.article_id for candidate in ordered],
            [candidate.source_hash for candidate in ordered],
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            float(min_similarity),
        )
        max_edges = len(ordered) * max(0, len(ordered) - 1)
        if len(rows) > max_edges:
            raise VectorStoreError("invalid_timeline_similarity_result")
        edges: list[ArticleVectorEdge] = []
        seen_pairs: set[tuple[str, str]] = set()
        for row in rows:
            source = str(row["source_article_id"])
            target = str(row["target_article_id"])
            pair = (source, target)
            try:
                edge = ArticleVectorEdge(source, target, float(row["similarity"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise VectorStoreError("invalid_timeline_similarity_result") from exc
            if (
                source not in candidate_by_id
                or target not in candidate_by_id
                or pair in seen_pairs
                or edge.similarity < float(min_similarity)
            ):
                raise VectorStoreError("invalid_timeline_similarity_result")
            seen_pairs.add(pair)
            edges.append(edge)
        edges.sort(
            key=lambda edge: (
                edge.source_article_id,
                -edge.similarity,
                edge.target_article_id,
            )
        )
        return edges

    async def indexed_article_ids(
        self,
        candidates: Sequence[ArticleVectorCandidate],
    ) -> frozenset[str]:
        """Return exact current-hash indexed IDs for the cluster coverage gate."""
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a sequence")
        if len(candidates) > MAX_CLUSTER_CANDIDATES:
            raise ValueError("article cluster candidate limit exceeds 500")
        candidate_by_id: dict[str, ArticleVectorCandidate] = {}
        for candidate in candidates:
            if not isinstance(candidate, ArticleVectorCandidate):
                raise TypeError("candidates must contain ArticleVectorCandidate values")
            if candidate.article_id in candidate_by_id:
                raise ValueError("article indexed candidate IDs must be unique")
            candidate_by_id[candidate.article_id] = candidate
        if not candidate_by_id:
            return frozenset()
        ordered = [candidate_by_id[key] for key in sorted(candidate_by_id)]
        rows = await self.pool.fetch(
            """
            WITH requested AS (
                SELECT article_id,source_hash
                FROM unnest($1::text[],$2::text[])
                     AS requested(article_id,source_hash)
            )
            SELECT stored.article_id
            FROM article_text_embeddings AS stored
            JOIN requested
              ON requested.article_id=stored.article_id
             AND requested.source_hash=stored.source_hash
            WHERE stored.model=$3 AND stored.dimensions=$4
              AND stored.status='indexed' AND stored.embedding IS NOT NULL
            ORDER BY stored.article_id
            """,
            [candidate.article_id for candidate in ordered],
            [candidate.source_hash for candidate in ordered],
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
        )
        result: set[str] = set()
        for row in rows:
            article_id = str(row["article_id"])
            if article_id not in candidate_by_id or article_id in result:
                raise VectorStoreError("invalid_indexed_article_result")
            result.add(article_id)
        return frozenset(result)

    async def publish_cluster_snapshot(
        self,
        plan: ClusterPlan,
        *,
        input_hash: str,
        slot: str,
    ) -> ArticleClusterSnapshot:
        """Atomically publish one validated derived cluster snapshot per UTC slot."""
        validate_cluster_plan(plan)
        if _SOURCE_HASH_RE.fullmatch(input_hash) is None:
            raise ValueError("cluster input_hash must be lowercase sha256")
        slot_start = _cluster_slot_start(slot)
        snapshot_id = uuid5(
            NAMESPACE_URL,
            f"article-cluster:{CLUSTER_ALGORITHM_VERSION}:{slot}:{input_hash}",
        )
        valid_until = slot_start + timedelta(hours=6)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('article-cluster-publish'))"
                )
                existing = await connection.fetchrow(
                    """
                    SELECT * FROM article_cluster_snapshots
                    WHERE input_hash=$1 OR slot=$2
                    ORDER BY CASE WHEN input_hash=$1 THEN 0 ELSE 1 END
                    LIMIT 1 FOR UPDATE
                    """,
                    input_hash,
                    slot,
                )
                if existing is not None:
                    return await self._load_cluster_snapshot(connection, existing)

                inserted = await connection.fetchrow(
                    """
                    INSERT INTO article_cluster_snapshots(
                        id,input_hash,slot,status,model,dimensions,
                        algorithm_version,article_count,indexed_count,coverage,
                        valid_until,lease_expires_at
                    )
                    VALUES ($1,$2,$3,'processing',$4,$5,$6,$7,$8,$9,$10,$11)
                    RETURNING *
                    """,
                    snapshot_id,
                    input_hash,
                    slot,
                    self.s.image_rag_model,
                    self.s.image_rag_dimensions,
                    plan.algorithm_version,
                    plan.article_count,
                    plan.indexed_count,
                    plan.coverage,
                    valid_until,
                    slot_start + timedelta(minutes=30),
                )
                if inserted is None:
                    raise VectorStoreError("cluster_snapshot_insert_failed")

                cluster_values = [
                    (
                        snapshot_id,
                        cluster.cluster_key,
                        cluster.rank,
                        cluster.medoid_article_id,
                        cluster.member_count,
                        cluster.domain_count,
                        cluster.first_published_at,
                        cluster.last_published_at,
                        cluster.mean_edge_similarity,
                        cluster.major_score,
                    )
                    for cluster in plan.clusters
                ]
                if cluster_values:
                    await connection.executemany(
                        """
                        INSERT INTO article_clusters(
                            snapshot_id,cluster_key,rank,medoid_article_id,
                            member_count,domain_count,first_published_at,
                            last_published_at,mean_edge_similarity,major_score
                        )
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        """,
                        cluster_values,
                    )
                member_values = [
                    (
                        snapshot_id,
                        cluster.cluster_key,
                        member.article_id,
                        member.similarity_to_medoid,
                        member.centrality_rank,
                    )
                    for cluster in plan.clusters
                    for member in cluster.members
                ]
                if member_values:
                    await connection.executemany(
                        """
                        INSERT INTO article_cluster_members(
                            snapshot_id,cluster_key,article_id,
                            similarity_to_medoid,centrality_rank
                        )
                        VALUES ($1,$2,$3,$4,$5)
                        """,
                        member_values,
                    )
                await connection.execute(
                    """
                    UPDATE article_cluster_snapshots
                    SET status='superseded', updated_at=now()
                    WHERE status='published' AND id<>$1
                    """,
                    snapshot_id,
                )
                published = await connection.fetchrow(
                    """
                    UPDATE article_cluster_snapshots
                    SET status='published', generated_at=now(), valid_until=$2,
                        error_code=NULL, lease_expires_at=NULL, updated_at=now()
                    WHERE id=$1 AND status='processing'
                    RETURNING *
                    """,
                    snapshot_id,
                    valid_until,
                )
                if published is None:
                    raise VectorStoreError("cluster_snapshot_publish_failed")
        return _decode_cluster_snapshot(published, plan.clusters)

    async def current_cluster_snapshot(self) -> ArticleClusterSnapshot | None:
        header = await self.pool.fetchrow(
            """
            SELECT * FROM article_cluster_snapshots
            WHERE status='published' AND valid_until > now()
            ORDER BY generated_at DESC, id DESC LIMIT 1
            """
        )
        if header is None:
            return None
        return await self._load_cluster_snapshot(self.pool, header)

    async def _load_cluster_snapshot(
        self,
        reader: Any,
        header: Any,
    ) -> ArticleClusterSnapshot:
        rows = await reader.fetch(
            """
            SELECT cluster.cluster_key,cluster.rank,cluster.medoid_article_id,
                   cluster.member_count,cluster.domain_count,
                   cluster.first_published_at,cluster.last_published_at,
                   cluster.mean_edge_similarity,cluster.major_score,
                   member.article_id,member.similarity_to_medoid,
                   member.centrality_rank
            FROM article_clusters AS cluster
            JOIN article_cluster_members AS member
              ON member.snapshot_id=cluster.snapshot_id
             AND member.cluster_key=cluster.cluster_key
            WHERE cluster.snapshot_id=$1
            ORDER BY cluster.rank,member.centrality_rank,member.article_id
            """,
            header["id"],
        )
        return _decode_cluster_snapshot(header, _decode_cluster_rows(rows))

    async def nearest_articles(
        self,
        embedding: list[float],
        *,
        exclude_ids: Sequence[str],
        limit: int = ARTICLE_NEAREST_LIMIT,
        min_similarity: float = 0.35,
    ) -> list[ArticleVectorHit]:
        bounded = max(0, min(int(limit), ARTICLE_NEAREST_LIMIT))
        if isinstance(min_similarity, bool):
            return []
        threshold = float(min_similarity)
        if bounded == 0 or not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            return []
        literal = embedding_literal(
            embedding,
            dimensions=self.s.image_rag_dimensions,
        )
        excluded = _article_ids(exclude_ids)
        rows = await self.pool.fetch(
            """
            SELECT article_id,
                   1 - (embedding <=> $4::vector(768)) AS similarity
            FROM article_text_embeddings
            WHERE model=$1 AND dimensions=$2 AND status='indexed'
              AND embedding IS NOT NULL
              AND NOT (article_id = ANY($5::text[]))
              AND 1 - (embedding <=> $4::vector(768)) >= $3
            ORDER BY embedding <=> $4::vector(768), article_id
            LIMIT $6
            """,
            self.s.image_rag_model,
            self.s.image_rag_dimensions,
            threshold,
            literal,
            excluded,
            bounded,
        )
        hits: list[ArticleVectorHit] = []
        for row in rows:
            similarity = float(row["similarity"])
            if math.isfinite(similarity) and similarity >= threshold:
                hits.append(
                    ArticleVectorHit(
                        article_id=str(row["article_id"]),
                        similarity=similarity,
                    )
                )
        return hits


def _validate_cursor(cursor: ArticleEmbeddingCursor) -> None:
    if not isinstance(cursor, ArticleEmbeddingCursor):
        raise TypeError("cursor must be an ArticleEmbeddingCursor")
    changed_at = cursor.changed_at
    if changed_at is not None and (
        changed_at.tzinfo is None or changed_at.utcoffset() is None
    ):
        raise ValueError("article embedding cursor must be timezone-aware")
    if not isinstance(cursor.article_id, str):
        raise ValueError("article embedding cursor article_id must be text")


def _target_values(settings: Settings, target: ArticleEmbeddingTarget) -> tuple:
    if not isinstance(target, ArticleEmbeddingTarget):
        raise TypeError("targets must contain ArticleEmbeddingTarget values")
    _validate_article_id(target.article_id)
    if not isinstance(target.eligible, bool):
        raise ValueError("article embedding eligibility must be boolean")
    safe_error = error_code(target.error_code) if target.error_code else None
    if target.eligible and safe_error is None:
        _validate_source_hash(target.source_hash)
        if not isinstance(target.source_text, str) or not target.source_text.strip():
            raise ValueError("eligible article embedding source must not be empty")
        if len(target.source_text) > ARTICLE_SOURCE_MAX_CHARS:
            raise ValueError("article embedding source exceeds 12000 characters")
    return (
        target.article_id,
        settings.image_rag_model,
        settings.image_rag_dimensions,
        target.eligible,
        target.source_hash if target.eligible and safe_error is None else None,
        target.source_text if target.eligible and safe_error is None else None,
        safe_error,
    )


def _validate_article_id(article_id: str) -> None:
    if not isinstance(article_id, str) or not article_id:
        raise ValueError("article_id must not be empty")


def _validate_source_hash(source_hash: str | None) -> None:
    if not isinstance(source_hash, str) or _SOURCE_HASH_RE.fullmatch(source_hash) is None:
        raise ValueError("source_hash must be 64 lowercase hexadecimal characters")


def _bounded_total(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("total_articles must be an integer")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("total_articles must be an integer") from exc
    return max(0, total)


def _article_ids(values: Sequence[str]) -> list[str]:
    return sorted({str(value) for value in values if isinstance(value, str) and value})


def _coverage_percent(embedded: int, total: int) -> float:
    if total <= 0:
        return 0.0
    bounded = min(total, max(0, embedded))
    percent = (Decimal(bounded) * Decimal(100) / Decimal(total)).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )
    return float(percent)


def _cluster_slot_start(slot: str) -> datetime:
    if not isinstance(slot, str) or _CLUSTER_SLOT_RE.fullmatch(slot) is None:
        raise ValueError("cluster slot must be a scheduled or manual UTC slot")
    normalized = slot.removeprefix(_MANUAL_CLUSTER_SLOT_PREFIX)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "cluster slot must be a scheduled or manual UTC slot"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("cluster slot must be UTC")
    return parsed.astimezone(UTC)


def _decode_cluster_rows(rows: Sequence[Any]) -> tuple[ArticleCluster, ...]:
    clusters: list[ArticleCluster] = []
    current_key: str | None = None
    current: dict[str, Any] | None = None
    for row in rows:
        cluster_key = str(row["cluster_key"]).strip()
        if cluster_key != current_key:
            if current is not None:
                clusters.append(_cluster_from_group(current))
            current_key = cluster_key
            current = {
                "cluster_key": cluster_key,
                "rank": int(row["rank"]),
                "medoid_article_id": str(row["medoid_article_id"]),
                "domain_count": int(row["domain_count"]),
                "first_published_at": row["first_published_at"],
                "last_published_at": row["last_published_at"],
                "mean_edge_similarity": float(row["mean_edge_similarity"]),
                "major_score": float(row["major_score"]),
                "members": [],
            }
        if current is None:
            raise VectorStoreError("invalid_cluster_snapshot")
        current["members"].append(
            ArticleClusterMember(
                article_id=str(row["article_id"]),
                similarity_to_medoid=float(row["similarity_to_medoid"]),
                centrality_rank=int(row["centrality_rank"]),
            )
        )
    if current is not None:
        clusters.append(_cluster_from_group(current))
    return tuple(clusters)


def _cluster_from_group(value: dict[str, Any]) -> ArticleCluster:
    return ArticleCluster(
        cluster_key=value["cluster_key"],
        rank=value["rank"],
        medoid_article_id=value["medoid_article_id"],
        members=tuple(value["members"]),
        domain_count=value["domain_count"],
        first_published_at=value["first_published_at"],
        last_published_at=value["last_published_at"],
        mean_edge_similarity=value["mean_edge_similarity"],
        major_score=value["major_score"],
    )


def _decode_cluster_snapshot(
    header: Any,
    clusters: Sequence[ArticleCluster],
) -> ArticleClusterSnapshot:
    raw_id = header["id"]
    snapshot_id = raw_id if isinstance(raw_id, UUID) else UUID(str(raw_id))
    return ArticleClusterSnapshot(
        id=snapshot_id,
        input_hash=str(header["input_hash"]).strip(),
        slot=str(header["slot"]),
        status=str(header["status"]),
        model=str(header["model"]),
        dimensions=int(header["dimensions"]),
        algorithm_version=str(header["algorithm_version"]),
        article_count=int(header["article_count"]),
        indexed_count=int(header["indexed_count"]),
        coverage=float(header["coverage"]),
        generated_at=header["generated_at"],
        valid_until=header["valid_until"],
        clusters=tuple(clusters),
    )
