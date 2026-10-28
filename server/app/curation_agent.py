"""Bounded multi-turn curation agent using server-owned article tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from .config import Settings
from .curation import (
    CurationCandidate,
    CurationPlan,
    CurationValidationError,
    validate_curation_plan,
)
from .curation_audience import CurationAudienceAggregate
from .curation_prompts import (
    CURATION_FINALIZATION_MESSAGE,
    CURATION_SUBMIT_TOOL_CHOICE,
    build_curation_agent_messages,
    build_curation_tools,
)
from .curation_tools import CurationArticleTools
from .openrouter import OpenRouterClient


MAX_AGENT_TURNS = 12
MAX_EXPLORATION_TURNS = 8
MAX_AGENT_TOOL_CALLS = 24
MIN_AGENT_SEARCH_CALLS = 2
MIN_AGENT_SEEN_ARTICLES = 8


@dataclass(frozen=True, slots=True)
class CurationAgentResult:
    plan: CurationPlan
    candidates: tuple[CurationCandidate, ...]


class CurationAgent:
    def __init__(
        self,
        settings: Settings,
        llm: OpenRouterClient,
        tools_factory: Callable[[], CurationArticleTools],
    ):
        self.settings = settings
        self.llm = llm
        self.tools_factory = tools_factory

    @property
    def configured(self) -> bool:
        return self.llm.configured

    async def generate(
        self,
        audience: CurationAudienceAggregate,
        *,
        editorial_day: date,
        refresh_slot: str,
    ) -> CurationAgentResult:
        tools = self.tools_factory()
        messages = build_curation_agent_messages(
            audience,
            editorial_day=editorial_day,
            refresh_slot=refresh_slot,
            min_routes=self.settings.curation_min_routes,
            max_routes=self.settings.curation_max_routes,
        )
        tool_definitions = build_curation_tools(
            min_routes=self.settings.curation_min_routes,
            max_routes=self.settings.curation_max_routes,
        )
        submit_tools = [tool_definitions[-1]]
        session_id = "curation-" + hashlib.sha256(
            f"{editorial_day.isoformat()}:{refresh_slot}".encode("utf-8")
        ).hexdigest()[:32]
        used_call_ids: set[str] = set()
        tool_call_count = 0

        finalization_started = False
        for turn in range(MAX_AGENT_TURNS):
            exploration_complete = (
                tools.search_calls >= MIN_AGENT_SEARCH_CALLS
                and len(tools.candidates) >= MIN_AGENT_SEEN_ARTICLES
            )
            finalization = (
                turn >= MAX_EXPLORATION_TURNS and exploration_complete
            )
            if finalization and not finalization_started:
                messages.append(
                    {"role": "user", "content": CURATION_FINALIZATION_MESSAGE}
                )
                finalization_started = True
            assistant = await self.llm.complete_with_tools(
                messages=messages,
                tools=submit_tools if finalization else tool_definitions,
                reasoning_effort="high",
                session_id=session_id,
                tool_choice=(
                    CURATION_SUBMIT_TOOL_CHOICE if finalization else "auto"
                ),
            )
            raw_calls = assistant.get("tool_calls")
            if not isinstance(raw_calls, list) or not raw_calls:
                raise CurationValidationError("agent_protocol_failed")
            messages.append(assistant)
            tool_call_count += len(raw_calls)
            if tool_call_count > MAX_AGENT_TOOL_CALLS:
                raise CurationValidationError("agent_loop_exhausted")

            parsed: list[tuple[str, str, dict[str, Any] | None]] = []
            for raw_call in raw_calls:
                call_id, name, arguments = _parse_tool_call(raw_call)
                if call_id in used_call_ids:
                    raise CurationValidationError("agent_protocol_failed")
                used_call_ids.add(call_id)
                parsed.append((call_id, name, arguments))

            if finalization and any(
                name != "submit_catalog" for _call_id, name, _arguments in parsed
            ):
                for call_id, _name, _arguments in parsed:
                    messages.append(
                        _tool_message(
                            call_id,
                            {
                                "ok": False,
                                "error": "submission_phase_requires_submit",
                            },
                        )
                    )
                continue

            submit_calls = [call for call in parsed if call[1] == "submit_catalog"]
            if submit_calls and len(parsed) != 1:
                for call_id, _name, _arguments in parsed:
                    messages.append(
                        _tool_message(
                            call_id,
                            {"ok": False, "error": "submit_must_be_single"},
                        )
                    )
                continue

            for call_id, name, arguments in parsed:
                if arguments is None:
                    messages.append(
                        _tool_message(
                            call_id,
                            {"ok": False, "error": "tool_invalid_arguments"},
                        )
                    )
                    continue
                if name == "submit_catalog":
                    result = _validate_submission(
                        arguments,
                        tools,
                        min_routes=self.settings.curation_min_routes,
                        max_routes=self.settings.curation_max_routes,
                    )
                    if isinstance(result, CurationPlan):
                        return CurationAgentResult(
                            plan=result,
                            candidates=tools.candidates,
                        )
                    messages.append(_tool_message(call_id, result))
                    continue
                try:
                    if name == "search_articles":
                        result = await _call_search(tools, arguments)
                    elif name == "get_article_details":
                        result = await _call_details(tools, arguments)
                    else:
                        result = {"ok": False, "error": "unknown_tool"}
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - redact DB/tool internals
                    raise CurationValidationError("tool_unavailable") from exc
                messages.append(_tool_message(call_id, result))

        raise CurationValidationError("agent_loop_exhausted")


def _validate_submission(
    arguments: Mapping[str, Any],
    tools: CurationArticleTools,
    *,
    min_routes: int,
    max_routes: int,
) -> CurationPlan | dict[str, Any]:
    if (
        tools.search_calls < MIN_AGENT_SEARCH_CALLS
        or len(tools.candidates) < MIN_AGENT_SEEN_ARTICLES
    ):
        return {"ok": False, "error": "insufficient_exploration"}
    try:
        return validate_curation_plan(
            arguments,
            tools.candidates,
            min_routes=min_routes,
            max_routes=max_routes,
        )
    except CurationValidationError as error:
        return {"ok": False, "error": error.code}


async def _call_search(
    tools: CurationArticleTools, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = {"query", "offset", "limit"}
    if set(arguments) - allowed:
        return {"ok": False, "error": "tool_invalid_arguments"}
    return await tools.search_articles(
        query=arguments.get("query", ""),
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 12),
    )


async def _call_details(
    tools: CurationArticleTools, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    if set(arguments) != {"article_ids"}:
        return {"ok": False, "error": "tool_invalid_arguments"}
    return await tools.get_article_details(article_ids=arguments["article_ids"])


def _parse_tool_call(raw: Any) -> tuple[str, str, dict[str, Any] | None]:
    if not isinstance(raw, Mapping) or raw.get("type") not in (None, "function"):
        raise CurationValidationError("agent_protocol_failed")
    call_id = raw.get("id")
    function = raw.get("function")
    if (
        not isinstance(call_id, str)
        or not 1 <= len(call_id) <= 128
        or not isinstance(function, Mapping)
        or not isinstance(function.get("name"), str)
    ):
        raise CurationValidationError("agent_protocol_failed")
    name = function["name"]
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return call_id, name, None
    if not isinstance(arguments, dict):
        return call_id, name, None
    return call_id, name, arguments


def _tool_message(call_id: str, result: Mapping[str, Any]) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
