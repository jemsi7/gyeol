"""Strict OpenRouter embeddings client for image/text cross-modal search."""
from __future__ import annotations

import math
from typing import Any, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings
from .openrouter import (
    ImagePart,
    OpenRouterError,
    OpenRouterPermanentError,
    OpenRouterRateLimiter,
    OpenRouterTransientError,
)

OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png"}


class OpenRouterEmbeddingClient:
    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
        request_limiter: OpenRouterRateLimiter | Any | None = None,
    ) -> None:
        self.s = settings
        self._client = client
        self._owns_client = client is None
        self._request_limiter = request_limiter

    @property
    def configured(self) -> bool:
        return bool(self.s.openrouter_api_key)

    async def __aenter__(self) -> "OpenRouterEmbeddingClient":
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

    async def embed_images(self, images: list[ImagePart]) -> list[list[float]]:
        if not images:
            return []
        if len(images) > self.s.image_rag_embed_batch_size:
            raise ValueError("image embedding batch exceeds configured limit")
        inputs: list[dict[str, Any]] = []
        for image in images:
            content_type = _normalize_content_type(image.content_type)
            if content_type not in SUPPORTED_IMAGE_TYPES:
                raise ValueError("image embedding supports JPEG and PNG only")
            inputs.append(
                {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image.to_data_url()},
                        }
                    ]
                }
            )
        return await self._embed(inputs, input_type="search_document")

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > 9:
            raise ValueError("text embedding batch exceeds scene limit")
        normalized = [str(text).strip() for text in texts]
        if any(not text for text in normalized):
            raise ValueError("text embedding inputs must be non-empty")
        return await self._embed(normalized, input_type="search_query")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed bounded article documents for the background vector index."""
        if not texts:
            return []
        if len(texts) > self.s.article_embedding_batch_size:
            raise ValueError("document embedding batch exceeds configured limit")
        normalized = [str(text).strip() for text in texts]
        if any(not text for text in normalized):
            raise ValueError("document embedding inputs must be non-empty")
        return await self._embed(normalized, input_type="search_document")

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, OpenRouterTransientError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _embed(
        self,
        inputs: list[Any],
        *,
        input_type: str,
    ) -> list[list[float]]:
        if not self.configured:
            raise OpenRouterError("OPENROUTER_API_KEY not set")
        if self._client is None:
            raise OpenRouterError("embedding client is not open")
        payload = {
            "model": self.s.image_rag_model,
            "input": inputs,
            "dimensions": self.s.image_rag_dimensions,
            "encoding_format": "float",
            "input_type": input_type,
        }
        if self._request_limiter is not None:
            await self._request_limiter.acquire()
        response = await self._client.post(OPENROUTER_EMBEDDINGS_URL, json=payload)
        if response.status_code in {408, 429, 529} or response.status_code >= 500:
            raise OpenRouterTransientError(
                f"embedding provider unavailable ({response.status_code})"
            )
        if response.status_code >= 400:
            raise OpenRouterPermanentError(_permanent_embedding_error(response.status_code))
        try:
            envelope = response.json()
            raw_data = envelope["data"]
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenRouterTransientError("invalid embedding response envelope") from exc
        if not isinstance(raw_data, list) or len(raw_data) != len(inputs):
            raise OpenRouterTransientError("invalid embedding response count")
        by_index: dict[int, list[float]] = {}
        for item in raw_data:
            if not isinstance(item, dict):
                raise OpenRouterTransientError("invalid embedding response item")
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                raise OpenRouterTransientError("invalid embedding response index")
            if index in by_index or not 0 <= index < len(inputs):
                raise OpenRouterTransientError("invalid embedding response index")
            by_index[index] = _validate_embedding(
                item.get("embedding"),
                dimensions=self.s.image_rag_dimensions,
            )
        if set(by_index) != set(range(len(inputs))):
            raise OpenRouterTransientError("invalid embedding response index set")
        return [by_index[index] for index in range(len(inputs))]


def _validate_embedding(value: Any, *, dimensions: int) -> list[float]:
    if not isinstance(value, list) or len(value) != dimensions:
        raise OpenRouterTransientError(
            f"embedding response must contain {dimensions} values"
        )
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise OpenRouterTransientError("embedding response contains non-finite value")
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise OpenRouterTransientError(
                "embedding response contains non-finite value"
            ) from exc
        if not math.isfinite(number):
            raise OpenRouterTransientError("embedding response contains non-finite value")
        result.append(number)
    return result


def _normalize_content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _permanent_embedding_error(status: int) -> str:
    if status == 400:
        return "embedding request rejected (400)"
    if status == 401:
        return "embedding authentication failed (401)"
    if status == 402:
        return "embedding credits unavailable (402)"
    if status == 403:
        return "embedding access denied (403)"
    if status == 404:
        return "embedding model unavailable (404)"
    return f"embedding request rejected ({status})"
