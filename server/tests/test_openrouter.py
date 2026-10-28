"""Outbound OpenRouter request contract tests."""

import json

import httpx
import pytest

from app.config import Settings
from app.openrouter import (
    OpenRouterClient,
    OpenRouterPermanentError,
    OpenRouterRateLimiter,
    OpenRouterRequestError,
    OpenRouterTransientError,
    StructuredOutputSpec,
)
from tenacity import wait_none


@pytest.mark.asyncio
async def test_complete_json_uses_default_openrouter_model(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    captured_payload = None

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await OpenRouterClient(settings, http_client).complete_json(
            system="Return JSON.",
            user_text="Map this article.",
        )

    assert captured_payload is not None
    assert captured_payload["model"] == "x-ai/grok-4.3"
    assert captured_payload["response_format"] == {"type": "json_object"}
    assert "provider" not in captured_payload
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_sends_strict_named_schema_and_requires_parameter_support():
    captured_payload = None
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await OpenRouterClient(settings, http_client).complete_json(
            system="Return JSON.",
            user_text="Map this article.",
            output_schema=StructuredOutputSpec(
                name="article_map_test_v1",
                schema=schema,
            ),
        )

    assert captured_payload is not None
    assert captured_payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "article_map_test_v1",
            "strict": True,
            "schema": schema,
        },
    }
    assert captured_payload["provider"] == {"require_parameters": True}
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_kill_switch_restores_json_object_contract():
    captured_payload = None

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    settings = Settings(
        _env_file=None,
        OPENROUTER_API_KEY="test-key",
        OPENROUTER_STRUCTURED_OUTPUTS_ENABLED=False,
    )
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        await OpenRouterClient(settings, http_client).complete_json(
            system="Return JSON.",
            user_text="Map this article.",
            output_schema=StructuredOutputSpec(
                name="article_map_test_v1",
                schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            ),
        )

    assert captured_payload is not None
    assert captured_payload["response_format"] == {"type": "json_object"}
    assert "provider" not in captured_payload


def test_structured_output_spec_rejects_invalid_contract_before_http():
    with pytest.raises(ValueError, match="invalid structured output"):
        StructuredOutputSpec(name="spaces are invalid", schema={"type": "object"})
    with pytest.raises(ValueError, match="invalid structured output"):
        StructuredOutputSpec(name="valid_name", schema={"type": "array"})


@pytest.mark.asyncio
async def test_tool_completion_uses_dedicated_model_and_preserves_reasoning_details():
    captured_payload = None
    reasoning = [{"type": "reasoning.summary", "summary": "opaque"}]

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_details": reasoning,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_articles",
                                        "arguments": '{"query":"AI"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await OpenRouterClient(
            settings,
            http_client,
            model="deepseek/deepseek-v4-flash",
        ).complete_with_tools(
            messages=[{"role": "user", "content": "curate"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_articles",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            reasoning_effort="high",
            session_id="curation-session",
        )

    assert captured_payload is not None
    assert captured_payload["model"] == "deepseek/deepseek-v4-flash"
    assert captured_payload["reasoning"] == {"effort": "high"}
    assert captured_payload["parallel_tool_calls"] is False
    assert captured_payload["tool_choice"] == "auto"
    assert "response_format" not in captured_payload
    assert result["reasoning_details"] == reasoning
    assert result["tool_calls"][0]["function"]["name"] == "search_articles"


@pytest.mark.asyncio
async def test_tool_completion_requires_allowlisted_specific_function():
    captured_payload = None
    request_count = 0
    submit_tool = {
        "type": "function",
        "function": {
            "name": "submit_catalog",
            "parameters": {"type": "object"},
        },
    }
    specific_choice = {
        "type": "function",
        "function": {"name": "submit_catalog"},
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload, request_count
        request_count += 1
        captured_payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "submit-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_catalog",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = OpenRouterClient(settings, http_client)
        await client.complete_with_tools(
            messages=[{"role": "user", "content": "submit"}],
            tools=[submit_tool],
            tool_choice=specific_choice,
        )
        with pytest.raises(ValueError, match="invalid tool completion request"):
            await client.complete_with_tools(
                messages=[{"role": "user", "content": "submit"}],
                tools=[submit_tool],
                tool_choice={
                    "type": "function",
                    "function": {"name": "search_articles"},
                },
            )

    assert request_count == 1
    assert captured_payload is not None
    assert captured_payload["tools"] == [submit_tool]
    assert captured_payload["tool_choice"] == specific_choice


@pytest.mark.asyncio
async def test_permanent_403_is_not_retried_or_exposed():
    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": (
                        "Key limit exceeded (total limit). Manage it using "
                        "https://openrouter.ai/workspaces/private/keys/sensitive-id"
                    )
                }
            },
        )

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(OpenRouterPermanentError) as caught:
            await OpenRouterClient(settings, http_client).complete_json(
                system="Return JSON.",
                user_text="Map this article.",
            )

    assert calls == 1
    assert str(caught.value) == "key limit exceeded (403)"
    assert "http" not in str(caught.value)
    assert "sensitive-id" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 409, 413, 415, 422])
async def test_payload_rejection_is_request_scoped(status):
    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": "private payload detail"}},
        )

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(OpenRouterRequestError) as caught:
            await OpenRouterClient(settings, http_client).complete_json(
                system="Return JSON.",
                user_text="Map this article.",
            )

    assert str(caught.value) == f"request rejected ({status})"
    assert "payload" not in str(caught.value)


@pytest.mark.asyncio
async def test_transient_429_is_retried(monkeypatch):
    calls = 0

    class CountingLimiter:
        def __init__(self):
            self.acquisitions = 0

        async def acquire(self):
            self.acquisitions += 1

    limiter = CountingLimiter()

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    monkeypatch.setattr(OpenRouterClient.complete_json.retry, "wait", wait_none())
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await OpenRouterClient(
            settings,
            http_client,
            request_limiter=limiter,
        ).complete_json(
            system="Return JSON.",
            user_text="Map this article.",
        )

    assert calls == 2
    assert limiter.acquisitions == 2
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_rate_limiter_evenly_spaces_attempts_and_zero_is_unlimited():
    now = [100.0]
    sleeps: list[float] = []

    async def advance(delay: float):
        sleeps.append(delay)
        now[0] += delay

    limited = OpenRouterRateLimiter(
        2,
        clock=lambda: now[0],
        sleep=advance,
    )
    await limited.acquire()
    await limited.acquire()
    await limited.acquire()

    assert sleeps == [30.0, 30.0]

    unlimited = OpenRouterRateLimiter(
        0,
        clock=lambda: now[0],
        sleep=advance,
    )
    await unlimited.acquire()
    assert sleeps == [30.0, 30.0]


@pytest.mark.asyncio
async def test_malformed_success_envelope_is_retried(monkeypatch):
    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"error": {"message": "temporary private provider detail"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    monkeypatch.setattr(OpenRouterClient.complete_json.retry, "wait", wait_none())
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await OpenRouterClient(settings, http_client).complete_json(
            system="Return JSON.",
            user_text="Map this article.",
        )

    assert calls == 2
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_malformed_success_envelope_stops_without_exposing_body(monkeypatch):
    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"error": {"message": "workspace/private/sensitive-provider-detail"}},
        )

    monkeypatch.setattr(OpenRouterClient.complete_json.retry, "wait", wait_none())
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(OpenRouterTransientError) as caught:
            await OpenRouterClient(settings, http_client).complete_json(
                system="Return JSON.",
                user_text="Map this article.",
            )

    assert calls == 3
    assert str(caught.value) == "unexpected response envelope"
    assert "sensitive-provider-detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_completion_content_is_retried_then_succeeds(monkeypatch):
    calls = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = '{"ok":' if calls == 1 else '{"ok": true}'
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(OpenRouterClient.complete_json.retry, "wait", wait_none())
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await OpenRouterClient(settings, http_client).complete_json(
            system="Return JSON.",
            user_text="Map this article.",
        )

    assert calls == 2
    assert result == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['{"secret":', "[]", "null"])
async def test_invalid_completion_content_exhausts_with_sanitized_error(
    monkeypatch,
    content,
):
    calls = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(OpenRouterClient.complete_json.retry, "wait", wait_none())
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-key")
    transport = httpx.MockTransport(handle_request)
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(OpenRouterTransientError) as caught:
            await OpenRouterClient(settings, http_client).complete_json(
                system="Return JSON.",
                user_text="Map this article.",
            )

    assert calls == 3
    assert str(caught.value) == "invalid JSON response content"
    assert content not in str(caught.value)
