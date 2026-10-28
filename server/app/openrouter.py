"""Minimal async OpenRouter chat client with vision (image) support.

OpenRouter exposes an OpenAI-compatible /chat/completions endpoint. We send a
multimodal message (text + image_url parts, where images are base64 data URLs
built from the bytes we already stored) and ask for a single JSON object back.

Model is fully env-driven (OPENROUTER_MODEL) and must be vision-capable.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings

log = logging.getLogger("gdelt.openrouter")
_STRUCTURED_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class OpenRouterError(RuntimeError):
    pass


class OpenRouterTransientError(OpenRouterError):
    """A provider or transport failure that can recover within this request."""


class OpenRouterRequestError(OpenRouterError):
    """A non-retryable rejection scoped to one request payload."""


class OpenRouterPermanentError(OpenRouterError):
    """A provider-scoped rejection that requires configuration changes."""


class OpenRouterRateLimiter:
    """Process-local, evenly paced limiter for actual provider attempts."""

    def __init__(
        self,
        max_requests_per_minute: int,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        if max_requests_per_minute < 0:
            raise ValueError("max_requests_per_minute must be non-negative")
        self.max_requests_per_minute = max_requests_per_minute
        self._interval_s = (
            60.0 / max_requests_per_minute
            if max_requests_per_minute
            else 0.0
        )
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Reserve the next request start, sleeping outside the state lock."""
        if self.max_requests_per_minute == 0:
            return
        loop = asyncio.get_running_loop()
        clock = self._clock or loop.time
        async with self._lock:
            now = clock()
            scheduled = max(now, self._next_at)
            self._next_at = scheduled + self._interval_s
            delay = scheduled - now
        if delay > 0:
            await (self._sleep or asyncio.sleep)(delay)


@dataclass(slots=True)
class ImagePart:
    content_type: str
    data: bytes

    def to_data_url(self) -> str:
        ct = self.content_type or "image/jpeg"
        b64 = base64.b64encode(self.data).decode("ascii")
        return f"data:{ct};base64,{b64}"


@dataclass(frozen=True, slots=True)
class StructuredOutputSpec:
    """One named JSON Schema passed to a native structured-output provider."""

    name: str
    schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _STRUCTURED_OUTPUT_NAME_RE.fullmatch(self.name)
            or not isinstance(self.schema, Mapping)
            or not self.schema
            or self.schema.get("type") != "object"
        ):
            raise ValueError("invalid structured output specification")


class OpenRouterClient:
    provider_name = "openrouter"

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
        self.model = (model or settings.openrouter_model).strip()

    async def __aenter__(self) -> "OpenRouterClient":
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
                timeout=self.s.openrouter_timeout_s, headers=headers
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.s.openrouter_api_key)

    @property
    def model_identity(self) -> str:
        return f"{self.provider_name}:{self.model}"

    @property
    def structured_outputs_enabled(self) -> bool:
        return self.s.openrouter_structured_outputs_enabled

    @property
    def max_images(self) -> int:
        return self.s.llm_max_images

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, OpenRouterTransientError)),
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
        """Send one prompt, return the parsed JSON object from the reply."""
        if not self.configured:
            raise OpenRouterError("OPENROUTER_API_KEY not set")
        assert self._client is not None

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for img in images or []:
            content.append(
                {"type": "image_url", "image_url": {"url": img.to_data_url()}}
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": self.s.openrouter_temperature,
            "max_tokens": self.s.openrouter_max_tokens,
        }
        if self.s.openrouter_structured_outputs_enabled and output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.name,
                    "strict": True,
                    "schema": copy.deepcopy(dict(output_schema.schema)),
                },
            }
            payload["provider"] = {"require_parameters": True}
        else:
            payload["response_format"] = {"type": "json_object"}

        if self._request_limiter is not None:
            await self._request_limiter.acquire()
        resp = await self._client.post(self.s.openrouter_base_url, json=payload)
        if resp.status_code == 408:
            raise OpenRouterTransientError("request timeout (408)")
        if resp.status_code == 429:
            raise OpenRouterTransientError("rate limited (429)")
        if resp.status_code >= 500:
            raise OpenRouterTransientError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            # Permanent 4xx responses can contain workspace/key management URLs.
            # Classify them without propagating the provider response body.
            error_type = (
                OpenRouterRequestError
                if resp.status_code in {400, 409, 413, 415, 422}
                else OpenRouterPermanentError
            )
            raise error_type(_permanent_error_message(resp))

        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise OpenRouterTransientError("invalid JSON response envelope") from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterTransientError("unexpected response envelope") from exc
        if not isinstance(text, str):
            raise OpenRouterTransientError("unexpected response envelope")
        return _loads_json_or_transient(text)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, OpenRouterTransientError)),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str = "high",
        session_id: str | None = None,
        tool_choice: str | Mapping[str, Any] = "auto",
    ) -> dict[str, Any]:
        """Return one assistant message from an OpenRouter tool-use turn."""
        if not self.configured:
            raise OpenRouterError("OPENROUTER_API_KEY not set")
        if (
            not self.model
            or not messages
            or not tools
            or reasoning_effort != "high"
            or (session_id is not None and not 1 <= len(session_id) <= 256)
        ):
            raise ValueError("invalid tool completion request")
        _validate_tools(tools)
        selected_tool_choice = _validated_tool_choice(tool_choice, tools)
        assert self._client is not None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.s.openrouter_temperature,
            "max_tokens": self.s.openrouter_max_tokens,
            "tools": tools,
            "tool_choice": selected_tool_choice,
            "parallel_tool_calls": False,
            "reasoning": {"effort": reasoning_effort},
        }
        if session_id is not None:
            payload["session_id"] = session_id

        if self._request_limiter is not None:
            await self._request_limiter.acquire()
        resp = await self._client.post(self.s.openrouter_base_url, json=payload)
        if resp.status_code == 408:
            raise OpenRouterTransientError("request timeout (408)")
        if resp.status_code == 429:
            raise OpenRouterTransientError("rate limited (429)")
        if resp.status_code >= 500:
            raise OpenRouterTransientError(f"server error {resp.status_code}")
        if resp.status_code >= 400:
            error_type = (
                OpenRouterRequestError
                if resp.status_code in {400, 409, 413, 415, 422}
                else OpenRouterPermanentError
            )
            raise error_type(_permanent_error_message(resp))
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OpenRouterTransientError("unexpected response envelope") from exc
        if (
            not isinstance(choice, dict)
            or choice.get("finish_reason") == "error"
            or not isinstance(message, dict)
            or message.get("role") not in (None, "assistant")
        ):
            raise OpenRouterTransientError("unexpected response envelope")
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        if content is not None and not isinstance(content, str):
            raise OpenRouterTransientError("unexpected response envelope")
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise OpenRouterTransientError("unexpected response envelope")
        if content is None and not tool_calls:
            raise OpenRouterTransientError("unexpected response envelope")
        allowed = {
            "role",
            "content",
            "tool_calls",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            # Server-side tools (web search) report what they read here as
            # url_citation entries. Dropping it would leave a caller unable to
            # tell which URLs the model actually saw, which is the whole basis
            # of the publisher-interest evidence rule (DR-13245).
            "annotations",
        }
        preserved = {
            key: copy.deepcopy(value)
            for key, value in message.items()
            if key in allowed
        }
        preserved["role"] = "assistant"
        return preserved


def _validate_tools(tools: list[dict[str, Any]]) -> None:
    """Accept function tools and OpenRouter-executed server tools, nothing else.

    A server tool (``{"type": "openrouter:web_search", ...}``) is run by the
    provider and never comes back as a tool call, so it carries no function
    definition and cannot be named in ``tool_choice``.
    """
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValueError("invalid tool completion request")
        kind = tool.get("type")
        if isinstance(kind, str) and kind.startswith("openrouter:"):
            if "function" in tool or not isinstance(
                tool.get("parameters", {}), Mapping
            ):
                raise ValueError("invalid tool completion request")
            continue
        function = tool.get("function")
        if (
            kind not in (None, "function")
            or not isinstance(function, Mapping)
            or not isinstance(function.get("name"), str)
            or not function["name"]
        ):
            raise ValueError("invalid tool completion request")


def _validated_tool_choice(
    tool_choice: str | Mapping[str, Any],
    tools: list[dict[str, Any]],
) -> str | dict[str, Any]:
    if tool_choice == "auto":
        return "auto"
    if (
        not isinstance(tool_choice, Mapping)
        or set(tool_choice) != {"type", "function"}
        or tool_choice.get("type") != "function"
        or not isinstance(tool_choice.get("function"), Mapping)
        or set(tool_choice["function"]) != {"name"}
        or not isinstance(tool_choice["function"].get("name"), str)
    ):
        raise ValueError("invalid tool completion request")
    selected_name = tool_choice["function"]["name"]
    available_names = {
        function["name"]
        for tool in tools
        if isinstance(tool, Mapping)
        and isinstance(tool.get("function"), Mapping)
        and isinstance((function := tool["function"]).get("name"), str)
    }
    if selected_name not in available_names:
        raise ValueError("invalid tool completion request")
    return {
        "type": "function",
        "function": {"name": selected_name},
    }


def _permanent_error_message(response: httpx.Response) -> str:
    status = response.status_code
    if status == 401:
        return "authentication failed (401)"
    if status == 403:
        message = ""
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"].lower()
        except (ValueError, TypeError):
            pass
        if "key limit exceeded" in message:
            return "key limit exceeded (403)"
        return "access denied (403)"
    return f"request rejected ({status})"


def _loads_lenient(text: str) -> dict[str, Any]:
    """Parse JSON, tolerating ```json fences or leading prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _loads_json_or_transient(text: str) -> dict[str, Any]:
    """Classify malformed completion content as a recoverable provider reply."""
    try:
        value = _loads_lenient(text)
    except (TypeError, ValueError) as exc:
        raise OpenRouterTransientError("invalid JSON response content") from exc
    if not isinstance(value, dict):
        raise OpenRouterTransientError("invalid JSON response content")
    return value
