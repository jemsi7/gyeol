"""Bounded OpenRouter rerank client for background timeline curation."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .openrouter import (
    OpenRouterError,
    OpenRouterPermanentError,
    OpenRouterRateLimiter,
    OpenRouterRequestError,
    OpenRouterTransientError,
)

OPENROUTER_RERANK_URL = "https://openrouter.ai/api/v1/rerank"
OPENROUTER_RERANK_MODEL = "cohere/rerank-4-pro"
RerankModel = Literal["cohere/rerank-4-pro"]


@dataclass(frozen=True, slots=True)
class RerankDocument:
    article_id: str
    text: str


@dataclass(frozen=True, slots=True)
class RerankResult:
    article_id: str
    index: int
    relevance_score: float


class OpenRouterRerankClient:
    """Strict client for the one bounded rerank request made per cluster."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
        request_limiter: OpenRouterRateLimiter | Any | None = None,
        *,
        model: RerankModel = OPENROUTER_RERANK_MODEL,
    ) -> None:
        if model != OPENROUTER_RERANK_MODEL:
            raise ValueError("unsupported rerank model")
        self.s = settings
        self.model = model
        self._client = client
        self._owns_client = client is None
        self._request_limiter = request_limiter

    @property
    def configured(self) -> bool:
        return bool(self.s.openrouter_api_key)

    async def __aenter__(self) -> "OpenRouterRerankClient":
        if self._client is None:
            headers = {
                "Authorization": f"Bearer {self.s.openrouter_api_key}",
                "Content-Type": "application/json",
            }
            if self.s.openrouter_referer:
                headers["HTTP-Referer"] = self.s.openrouter_referer
            if self.s.openrouter_title:
                headers["X-Title"] = self.s.openrouter_title
            self._client = httpx.AsyncClient(
                timeout=self.s.openrouter_timeout_s,
                headers=headers,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[RerankDocument],
        top_n: int,
    ) -> list[RerankResult]:
        normalized_query, normalized_documents = _validate_request(
            query=query,
            documents=documents,
            top_n=top_n,
        )
        return await self._rerank(
            query=normalized_query,
            documents=normalized_documents,
            top_n=top_n,
        )

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, OpenRouterTransientError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _rerank(
        self,
        *,
        query: str,
        documents: tuple[RerankDocument, ...],
        top_n: int,
    ) -> list[RerankResult]:
        if not self.configured:
            raise OpenRouterError("OPENROUTER_API_KEY not set")
        if self._client is None or self._client.is_closed:
            raise OpenRouterError("rerank client is not open")
        if self._request_limiter is not None:
            await self._request_limiter.acquire()
        try:
            response = await self._client.post(
                OPENROUTER_RERANK_URL,
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [document.text for document in documents],
                    "top_n": top_n,
                },
            )
        except httpx.TransportError:
            raise OpenRouterTransientError("rerank provider unavailable") from None
        status = response.status_code
        if status in {408, 429, 529} or status >= 500:
            raise OpenRouterTransientError(f"rerank provider unavailable ({status})")
        if status in {400, 409, 413, 415, 422}:
            raise OpenRouterRequestError(f"rerank request rejected ({status})")
        if status >= 400:
            raise OpenRouterPermanentError(_permanent_rerank_error(status))
        return _validate_response(
            response,
            documents=documents,
            top_n=top_n,
        )


def _validate_request(
    *,
    query: str,
    documents: Sequence[RerankDocument],
    top_n: int,
) -> tuple[str, tuple[RerankDocument, ...]]:
    if not isinstance(query, str):
        raise ValueError("rerank query must be a string")
    normalized_query = query.strip()
    if not 2 <= len(normalized_query) <= 500:
        raise ValueError("rerank query must contain 2 to 500 characters")
    if isinstance(documents, (str, bytes)) or not isinstance(documents, Sequence):
        raise ValueError("rerank documents must be a sequence")
    if not 4 <= len(documents) <= 20:
        raise ValueError("rerank documents must contain 4 to 20 items")
    if isinstance(top_n, bool) or not isinstance(top_n, int):
        raise ValueError("rerank top_n must be an integer")
    if not 4 <= top_n <= 20 or top_n > len(documents):
        raise ValueError("rerank top_n must be 4 to 20 and not exceed documents")

    normalized_documents: list[RerankDocument] = []
    article_ids: set[str] = set()
    for document in documents:
        if not isinstance(document, RerankDocument):
            raise ValueError("invalid rerank document")
        if not isinstance(document.article_id, str) or not document.article_id.strip():
            raise ValueError("rerank article_id must be non-empty")
        if document.article_id in article_ids:
            raise ValueError("rerank article_id values must be unique")
        if not isinstance(document.text, str):
            raise ValueError("rerank document text must be a string")
        normalized_text = document.text.strip()
        if not 1 <= len(normalized_text) <= 4000:
            raise ValueError("rerank document text must contain 1 to 4000 characters")
        article_ids.add(document.article_id)
        normalized_documents.append(
            RerankDocument(article_id=document.article_id, text=normalized_text)
        )
    return normalized_query, tuple(normalized_documents)


def _validate_response(
    response: httpx.Response,
    *,
    documents: tuple[RerankDocument, ...],
    top_n: int,
) -> list[RerankResult]:
    try:
        envelope = response.json()
        raw_results = envelope["results"]
    except (KeyError, TypeError, ValueError) as exc:
        raise OpenRouterTransientError("invalid rerank response envelope") from exc
    if not isinstance(raw_results, list) or len(raw_results) != top_n:
        raise OpenRouterTransientError("invalid rerank response count")

    results: list[RerankResult] = []
    seen_indices: set[int] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            raise OpenRouterTransientError("invalid rerank response item")
        index = item.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index in seen_indices
            or not 0 <= index < len(documents)
        ):
            raise OpenRouterTransientError("invalid rerank response index")
        score = item.get("relevance_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise OpenRouterTransientError("invalid rerank relevance score")
        normalized_score = float(score)
        if not math.isfinite(normalized_score) or not 0.0 <= normalized_score <= 1.0:
            raise OpenRouterTransientError("invalid rerank relevance score")
        seen_indices.add(index)
        results.append(
            RerankResult(
                article_id=documents[index].article_id,
                index=index,
                relevance_score=normalized_score,
            )
        )
    results.sort(key=lambda result: (-result.relevance_score, result.index))
    return results


def _permanent_rerank_error(status: int) -> str:
    if status == 401:
        return "rerank authentication failed (401)"
    if status == 402:
        return "rerank credits unavailable (402)"
    if status == 403:
        return "rerank access denied (403)"
    if status == 404:
        return "rerank model unavailable (404)"
    return f"rerank request rejected ({status})"
