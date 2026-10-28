"""Async Cerebras JSON chat client for article Map pipelines."""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings
from .openrouter import ImagePart, OpenRouterRateLimiter, StructuredOutputSpec

_MAX_STRICT_SCHEMA_CHARS = 5_000
_MAX_IMAGE_PAYLOAD_CHARS = 10_000_000
_UNSUPPORTED_STRICT_SCHEMA_KEYS = frozenset(
    {
        "description",
        "maxItems",
        "maxLength",
        "minItems",
        "minLength",
        "pattern",
    }
)


class CerebrasError(RuntimeError):
    pass


class CerebrasTransientError(CerebrasError):
    """A provider or transport failure that can recover within this request."""


class CerebrasRequestError(CerebrasError):
    """A non-retryable rejection scoped to one request payload."""


class CerebrasPermanentError(CerebrasError):
    """A provider-scoped rejection that requires configuration changes."""


class CerebrasClient:
    """OpenAI-compatible Cerebras transport with bounded vision input."""

    provider_name = "cerebras"

    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.AsyncClient] = None,
        request_limiter: OpenRouterRateLimiter | None = None,
        *,
        model: str | None = None,
    ):
        self.s = settings
        self._client = client
        self._owns = client is None
        self._request_limiter = request_limiter
        self.model = (model or settings.cerebras_model).strip()

    async def __aenter__(self) -> "CerebrasClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.s.cerebras_timeout_s)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.s.cerebras_api_key.strip() and self.model)

    @property
    def model_identity(self) -> str:
        return f"{self.provider_name}:{self.model}"

    @property
    def structured_outputs_enabled(self) -> bool:
        return self.s.cerebras_structured_outputs_enabled

    @property
    def max_images(self) -> int:
        return self.s.cerebras_max_images

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, CerebrasTransientError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def complete_json(
        self,
        system: str,
        user_text: str,
        images: list[ImagePart] | None = None,
        *,
        output_schema: StructuredOutputSpec | None = None,
    ) -> dict[str, Any]:
        """Send one prompt and return a parsed JSON object."""
        if not self.configured:
            raise CerebrasError("CEREBRAS_API_KEY not set")
        assert self._client is not None

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for image in _bounded_images(
            images or [],
            max_images=self.s.cerebras_max_images,
        ):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image.to_data_url()},
                }
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.s.cerebras_temperature,
            "max_completion_tokens": self.s.cerebras_max_completion_tokens,
        }
        projected_schema = (
            _cerebras_strict_schema(output_schema.schema)
            if output_schema is not None
            else None
        )
        if (
            self.s.cerebras_structured_outputs_enabled
            and output_schema is not None
            and projected_schema is not None
            and _schema_char_count(projected_schema) <= _MAX_STRICT_SCHEMA_CHARS
        ):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.name,
                    "strict": True,
                    "schema": projected_schema,
                },
            }
        else:
            payload["response_format"] = {"type": "json_object"}

        if self._request_limiter is not None:
            await self._request_limiter.acquire()
        response = await self._client.post(
            self.s.cerebras_base_url,
            headers={
                "Authorization": f"Bearer {self.s.cerebras_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code == 408:
            raise CerebrasTransientError("request timeout (408)")
        if response.status_code == 429:
            raise CerebrasTransientError("rate limited (429)")
        if response.status_code >= 500:
            raise CerebrasTransientError(f"server error {response.status_code}")
        if response.status_code >= 400:
            error_type = (
                CerebrasRequestError
                if response.status_code in {400, 409, 413, 415, 422}
                else CerebrasPermanentError
            )
            raise error_type(_permanent_error_message(response.status_code))

        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CerebrasTransientError("unexpected response envelope") from exc
        if not isinstance(text, str):
            raise CerebrasTransientError("unexpected response envelope")
        return _loads_json_or_transient(text)


def _schema_char_count(schema: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _cerebras_strict_schema(
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a schema onto Cerebras' supported strict-mode subset."""

    def project(node: Any, *, names: bool = False) -> Any:
        if isinstance(node, list):
            return [project(item) for item in node]
        if not isinstance(node, Mapping):
            return copy.deepcopy(node)
        if names:
            return {
                str(name): project(value)
                for name, value in node.items()
            }
        projected: dict[str, Any] = {}
        for key, value in node.items():
            if key in _UNSUPPORTED_STRICT_SCHEMA_KEYS:
                continue
            if key == "const":
                projected["enum"] = [copy.deepcopy(value)]
                continue
            projected[str(key)] = project(
                value,
                names=key in {"properties", "$defs"},
            )
        return projected

    return project(schema)


def _bounded_images(
    images: list[ImagePart],
    *,
    max_images: int,
    max_payload_chars: int = _MAX_IMAGE_PAYLOAD_CHARS,
) -> list[ImagePart]:
    selected: list[ImagePart] = []
    payload_chars = 0
    for image in images:
        if len(selected) >= max_images:
            break
        content_type = image.content_type or "image/jpeg"
        encoded_chars = 4 * ((len(image.data) + 2) // 3)
        data_url_chars = len(f"data:{content_type};base64,") + encoded_chars
        if payload_chars + data_url_chars > max_payload_chars:
            continue
        selected.append(image)
        payload_chars += data_url_chars
    return selected


def _permanent_error_message(status: int) -> str:
    if status == 401:
        return "authentication failed (401)"
    if status == 403:
        return "access denied (403)"
    return f"request rejected ({status})"


def _loads_json_or_transient(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise CerebrasTransientError("invalid JSON response content") from exc
    if not isinstance(value, dict):
        raise CerebrasTransientError("invalid JSON response content")
    return value
