"""Periodic image-embedding pool and article-time RAG enrichment service."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .editorial_pipeline import (
    MAX_SCENE_MEDIA,
    build_rag_query,
    eligible_rag_scenes,
    image_density_target,
    visible_image_views,
    enrich_planning_media,
    validate_planning,
)
from .image_rag_prompts import (
    RAG_VERIFICATION_SYSTEM_PROMPT,
    RagApproval,
    build_rag_verification_prompt,
    normalize_rag_verdict,
    validate_rag_verdict,
)
from .map_output_schemas import rag_verdict_output_schema
from .object_storage import ObjectStorageError, S3ObjectStore
from .openrouter import (
    ImagePart,
    OpenRouterPermanentError,
    OpenRouterTransientError,
)
from .openrouter_embeddings import OpenRouterEmbeddingClient, SUPPORTED_IMAGE_TYPES
from .vector_store import ImageCursor, VectorStore, VectorWorkItem

log = logging.getLogger("gdelt.image_rag")
_RECONNECT_COOLDOWN_S = 60.0


@dataclass(slots=True)
class RagSyncStats:
    discovered: int = 0
    claimed: int = 0
    indexed: int = 0
    retried: int = 0
    dead: int = 0
    disabled: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RagCandidate:
    image_id: str
    scene_id: str
    claim_refs: tuple[str, ...]
    similarity: float
    origin_article_id: str
    origin_title: str
    origin_url: str
    origin_domain: str
    source_url: str
    content_type: str
    byte_size: int
    backend: str
    storage_ref: str | None


@dataclass(frozen=True, slots=True)
class RagRetrieval:
    target_views: int
    current_views: int
    missing_views: int
    eligible_scene_ids: list[str]
    candidates: list[RagCandidate]


@dataclass(frozen=True, slots=True)
class RagAugmentation:
    ledger: dict
    planning: dict
    images: list[dict]
    target_views: int
    approved_count: int


@dataclass(frozen=True, slots=True)
class _PreparedImage:
    work: VectorWorkItem
    part: ImagePart


class ImageRagService:
    """Own the derived vector pool and its bounded indexing lifecycle."""

    def __init__(
        self,
        settings: Settings,
        repo: Any,
        object_store: S3ObjectStore | None,
        *,
        vector_store: VectorStore | Any | None = None,
        embeddings: OpenRouterEmbeddingClient | Any | None = None,
    ) -> None:
        self.s = settings
        self.repo = repo
        self.object_store = object_store
        self.vector = vector_store or VectorStore(settings)
        self.embeddings = embeddings or OpenRouterEmbeddingClient(settings)
        self.ready = False
        self._embeddings_open = False
        self._last_connect_attempt = 0.0

    @property
    def configured(self) -> bool:
        return (
            self.s.image_rag_enabled
            and bool(self.s.vector_asyncpg_dsn())
            and bool(self.s.openrouter_api_key)
        )

    async def start(self) -> None:
        if not self.configured:
            self.ready = False
            return
        if not self._embeddings_open:
            await self.embeddings.__aenter__()
            self._embeddings_open = True
        self._last_connect_attempt = time.monotonic()
        self.ready = bool(await self.vector.start())

    async def close(self) -> None:
        self.ready = False
        try:
            await self.vector.close()
        except Exception:  # noqa: BLE001
            log.warning("image RAG close failed code=vector_close_failed")
        if self._embeddings_open:
            try:
                await self.embeddings.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.warning("image RAG close failed code=embedding_close_failed")
            finally:
                self._embeddings_open = False

    async def _ensure_ready(self) -> bool:
        if not self.configured:
            return False
        if self.ready:
            return True
        now = time.monotonic()
        if now - self._last_connect_attempt < _RECONNECT_COOLDOWN_S:
            return False
        if not self._embeddings_open:
            await self.embeddings.__aenter__()
            self._embeddings_open = True
        self._last_connect_attempt = now
        self.ready = bool(await self.vector.start())
        return self.ready

    async def sync(self, limit: int | None = None) -> RagSyncStats:
        stats = RagSyncStats(disabled=not self.configured)
        if not self.configured:
            return stats
        if not await self._ensure_ready():
            stats.error_code = "vector_unavailable"
            return stats
        bounded = self.s.image_rag_sync_limit if limit is None else max(
            0, min(int(limit), self.s.image_rag_sync_limit)
        )
        if bounded == 0:
            return stats
        try:
            cursor = await self.vector.get_cursor()
            discovered = await self.repo.list_images_after(
                downloaded_at=cursor.downloaded_at,
                image_id=cursor.image_id,
                limit=bounded,
            )
            if discovered:
                last = discovered[-1]
                next_cursor = ImageCursor(
                    downloaded_at=last.get("downloaded_at"),
                    image_id=str(last["id"]),
                )
                stats.discovered = await self.vector.enqueue_discovered(
                    discovered,
                    next_cursor,
                )

            work = await self.vector.claim_due(bounded)
            stats.claimed = len(work)
            prepared: list[_PreparedImage] = []
            for item in work:
                try:
                    record = await self.repo.get_image_record(item.image_id)
                    if record is None:
                        await self.vector.mark_dead([item.image_id], "source_missing")
                        stats.dead += 1
                        continue
                    row = dict(record)
                    content_type = _normalize_content_type(row.get("content_type"))
                    if content_type not in SUPPORTED_IMAGE_TYPES:
                        await self.vector.mark_dead([item.image_id], "unsupported_mime")
                        stats.dead += 1
                        continue
                    data = await self._load_verified_bytes(item.image_id, row)
                    prepared.append(
                        _PreparedImage(
                            work=item,
                            part=ImagePart(content_type=content_type, data=data),
                        )
                    )
                except ObjectStorageError:
                    await self._retry_or_dead([item], "storage_read_failed", stats)
                except (TypeError, ValueError):
                    await self._retry_or_dead([item], "source_integrity_failed", stats)
                except Exception:  # noqa: BLE001
                    await self._retry_or_dead([item], "source_read_failed", stats)

            batch_size = self.s.image_rag_embed_batch_size
            for offset in range(0, len(prepared), batch_size):
                batch = prepared[offset : offset + batch_size]
                try:
                    vectors = await self.embeddings.embed_images(
                        [item.part for item in batch]
                    )
                    if len(vectors) != len(batch):
                        raise OpenRouterTransientError("embedding response count mismatch")
                    for item, vector in zip(batch, vectors, strict=True):
                        await self.vector.mark_indexed(item.work.image_id, vector)
                        stats.indexed += 1
                except OpenRouterPermanentError:
                    await self.vector.mark_dead(
                        [item.work.image_id for item in batch],
                        "embedding_permanent",
                    )
                    stats.dead += len(batch)
                except Exception:  # noqa: BLE001
                    await self._retry_or_dead(
                        [item.work for item in batch],
                        "embedding_transient",
                        stats,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            stats.error_code = "sync_failed"
            self.ready = False
            log.warning("image RAG sync skipped code=sync_failed")
        return stats

    async def retrieve(
        self,
        article_id: str,
        *,
        title: str,
        ledger: dict,
        planning: dict,
        images: list[dict],
        cover_ref: int | None,
    ) -> RagRetrieval:
        """Return bounded, revalidated vector hits for under-dense scenes."""
        fallback = RagRetrieval(0, 0, 0, [], [])
        try:
            plan = planning.get("plan") if isinstance(planning.get("plan"), dict) else planning
            scenes = plan.get("scenes") or []
            target = image_density_target(len(scenes))
            current = visible_image_views(planning, cover_ref)
            missing = max(0, target - current)
            eligible = eligible_rag_scenes(planning, cover_ref)
            empty = RagRetrieval(
                target_views=target,
                current_views=current,
                missing_views=missing,
                eligible_scene_ids=[str(scene.get("id")) for scene in eligible],
                candidates=[],
            )
            fallback = empty
            if missing == 0 or not eligible:
                return empty
            if not await self._ensure_ready():
                return empty

            queries = [
                build_rag_query(article_title=title, scene=scene, ledger=ledger)
                for scene in eligible
            ]
            vectors = await self.embeddings.embed_texts(queries)
            if len(vectors) != len(eligible):
                return empty

            current_ids = {
                str(image.get("image_id"))
                for image in images
                if isinstance(image, dict) and image.get("image_id")
            }
            excluded = set(current_ids)
            raw_hits: list[tuple[str, str, float]] = []
            article_limit = min(
                self.s.image_rag_max_candidates,
                missing * self.s.image_rag_candidates_per_scene,
            )
            for scene, vector in zip(eligible, vectors, strict=True):
                hits = await self.vector.search(
                    vector,
                    limit=self.s.image_rag_candidates_per_scene,
                    min_similarity=self.s.image_rag_min_similarity,
                    exclude_ids=excluded,
                )
                ordered = sorted(
                    hits,
                    key=lambda hit: (-float(hit.similarity), str(hit.image_id)),
                )
                for hit in ordered:
                    similarity = float(hit.similarity)
                    image_id = str(hit.image_id)
                    if (
                        not image_id
                        or image_id in excluded
                        or not math.isfinite(similarity)
                        or similarity < self.s.image_rag_min_similarity
                    ):
                        continue
                    raw_hits.append((str(scene["id"]), image_id, similarity))
                    excluded.add(image_id)
                    if len(raw_hits) >= article_limit:
                        break
                if len(raw_hits) >= article_limit:
                    break
            if not raw_hits:
                return empty

            rows = await self.repo.rag_candidate_records(
                [image_id for _, image_id, _ in raw_hits],
                exclude_article_id=article_id,
            )
            by_id = {
                str(row.get("image_id")): row
                for row in rows
                if isinstance(row, dict) and row.get("image_id")
            }
            scene_claims = {
                str(scene.get("id")): tuple(
                    str(ref) for ref in scene.get("claim_refs") or [] if ref
                )
                for scene in eligible
            }
            candidates: list[RagCandidate] = []
            for scene_id, image_id, similarity in raw_hits:
                row = by_id.get(image_id)
                if row is None or not row.get("origin_article_id"):
                    continue
                candidates.append(
                    RagCandidate(
                        image_id=image_id,
                        scene_id=scene_id,
                        claim_refs=scene_claims.get(scene_id, ()),
                        similarity=similarity,
                        origin_article_id=str(row["origin_article_id"]),
                        origin_title=str(row.get("origin_title") or ""),
                        origin_url=str(row.get("origin_url") or ""),
                        origin_domain=str(row.get("origin_domain") or ""),
                        source_url=str(row.get("source_url") or ""),
                        content_type=_normalize_content_type(row.get("content_type")),
                        byte_size=max(0, int(row.get("byte_size") or 0)),
                        backend=str(row.get("backend") or ""),
                        storage_ref=(
                            str(row.get("storage_ref"))
                            if row.get("storage_ref")
                            else None
                        ),
                    )
                )
            return RagRetrieval(
                target_views=target,
                current_views=current,
                missing_views=missing,
                eligible_scene_ids=empty.eligible_scene_ids,
                candidates=candidates[:article_limit],
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - article mapping must keep its base plan
            log.warning("image RAG retrieval skipped code=retrieval_failed")
            return fallback

    async def augment(
        self,
        article_id: str,
        *,
        source: dict,
        ledger: dict,
        planning: dict,
        images: list[dict],
        cover_ref: int | None,
        vision: Any,
    ) -> RagAugmentation:
        """Vision-verify retrieved candidates and merge only strict approvals."""
        base = RagAugmentation(
            ledger=ledger,
            planning=planning,
            images=images,
            target_views=(
                image_density_target(len((planning.get("plan") or {}).get("scenes") or []))
                if 3 <= len((planning.get("plan") or {}).get("scenes") or []) <= 9
                else 0
            ),
            approved_count=0,
        )
        try:
            retrieval = await self.retrieve(
                article_id,
                title=str(source.get("title") or ""),
                ledger=ledger,
                planning=planning,
                images=images,
                cover_ref=cover_ref,
            )
            if not retrieval.candidates or retrieval.missing_views <= 0:
                return base

            approvals: list[RagApproval] = []
            approved_scenes: set[str] = set()
            candidate_by_id = {
                candidate.image_id: candidate for candidate in retrieval.candidates
            }
            provider_image_limit = getattr(
                vision,
                "max_images",
                self.s.image_rag_vision_batch_size,
            )
            batch_size = min(
                self.s.image_rag_vision_batch_size,
                max(1, int(provider_image_limit)),
            )
            for offset in range(0, len(retrieval.candidates), batch_size):
                if len(approvals) >= retrieval.missing_views:
                    break
                candidates = [
                    candidate
                    for candidate in retrieval.candidates[offset : offset + batch_size]
                    if candidate.scene_id not in approved_scenes
                ]
                if not candidates:
                    continue
                parts: list[ImagePart] = []
                for candidate in candidates:
                    record = await self.repo.get_image_record(candidate.image_id)
                    if record is None:
                        raise ValueError("RAG candidate disappeared before verification")
                    row = dict(record)
                    content_type = _normalize_content_type(row.get("content_type"))
                    if content_type not in SUPPORTED_IMAGE_TYPES:
                        raise ValueError("RAG candidate MIME became unsupported")
                    data = await self._load_verified_bytes(candidate.image_id, row)
                    parts.append(ImagePart(content_type=content_type, data=data))

                raw = await vision.complete_json(
                    RAG_VERIFICATION_SYSTEM_PROMPT,
                    build_rag_verification_prompt(
                        article_title=str(source.get("title") or ""),
                        ledger=ledger,
                        candidates=candidates,
                    ),
                    parts,
                    output_schema=rag_verdict_output_schema(
                        candidates,
                        missing_views=min(
                            retrieval.missing_views - len(approvals),
                            18,
                        ),
                    ),
                )
                batch_approvals = normalize_rag_verdict(raw)
                remaining = retrieval.missing_views - len(approvals)
                errors = validate_rag_verdict(
                    batch_approvals,
                    candidates,
                    ledger,
                    missing_views=remaining,
                )
                if errors:
                    raise ValueError("invalid RAG vision verdict")
                for approval in batch_approvals:
                    if approval.scene_id in approved_scenes:
                        raise ValueError("duplicate RAG scene approval")
                    approvals.append(approval)
                    approved_scenes.add(approval.scene_id)
                    if len(approvals) >= retrieval.missing_views:
                        break
            if not approvals:
                return base

            augmented_ledger = deepcopy(ledger)
            augmented_planning = deepcopy(planning)
            augmented_images = deepcopy(images)
            for approval in approvals:
                candidate = candidate_by_id[approval.candidate_id]
                ref = len(augmented_images)
                augmented_images.append(
                    {
                        "ref": ref,
                        "image_id": candidate.image_id,
                        "source_url": candidate.source_url,
                        "role": "rag",
                        "origin_article_id": candidate.origin_article_id,
                        "retrieval_score": round(candidate.similarity, 6),
                    }
                )
                augmented_ledger.setdefault("media", []).append(
                    {
                        "ref": ref,
                        "caption": approval.caption,
                        "reason": approval.reason,
                        "claim_refs": list(approval.claim_refs),
                        "role": "rag",
                    }
                )
            augmented_planning = enrich_planning_media(
                augmented_planning,
                augmented_ledger,
                limit=MAX_SCENE_MEDIA,
                cover_ref=cover_ref,
                density_target_views=retrieval.target_views,
            )
            if validate_planning(augmented_planning, augmented_ledger, self.s):
                return base
            views = visible_image_views(augmented_planning, cover_ref)
            if not retrieval.current_views < views <= retrieval.target_views:
                return base
            return RagAugmentation(
                ledger=augmented_ledger,
                planning=augmented_planning,
                images=augmented_images,
                target_views=retrieval.target_views,
                approved_count=len(approvals),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fallback enrichment is all-or-nothing
            log.warning("image RAG admission skipped code=admission_failed")
            return base

    async def _retry_or_dead(
        self,
        items: list[VectorWorkItem],
        code: str,
        stats: RagSyncStats,
    ) -> None:
        dead = [
            item.image_id
            for item in items
            if item.attempts >= self.s.image_rag_max_attempts
        ]
        retry = [
            item.image_id
            for item in items
            if item.attempts < self.s.image_rag_max_attempts
        ]
        if dead:
            await self.vector.mark_dead(dead, code)
            stats.dead += len(dead)
        if retry:
            await self.vector.mark_retry(retry, code)
            stats.retried += len(retry)

    async def _load_verified_bytes(self, image_id: str, row: dict) -> bytes:
        expected_size = int(row.get("byte_size") or 0)
        if expected_size < 0 or expected_size > self.s.image_max_bytes:
            raise ValueError("image size outside configured bound")
        backend = str(row.get("backend") or "")
        if backend == "s3":
            key = str(row.get("storage_ref") or "")
            if self.object_store is None or not key:
                raise ObjectStorageError("read", key or "unavailable")
            return await asyncio.to_thread(
                self.object_store.read_verified_image,
                key,
                expected_sha256=image_id,
                expected_size=expected_size,
            )
        if backend == "db":
            raw = row.get("data")
            if isinstance(raw, memoryview):
                raw = raw.tobytes()
            if not isinstance(raw, bytes):
                raise ValueError("database image bytes unavailable")
            if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != image_id:
                raise ValueError("database image integrity mismatch")
            return raw
        raise ValueError("unsupported image backend")


def _normalize_content_type(value: str | None) -> str:
    normalized = (value or "").split(";", 1)[0].strip().lower()
    return "image/jpeg" if normalized == "image/jpg" else normalized
