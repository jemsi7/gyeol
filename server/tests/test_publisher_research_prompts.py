"""Tool schemas and prompt contract for publisher-interest research (I-13212-b)."""
from __future__ import annotations

import pytest

from app.publisher_research_prompts import (
    CONFIDENCE_VALUES,
    PUBLISHER_RESEARCH_SYSTEM_PROMPT,
    RELATIONS,
    build_research_messages,
    build_research_tools,
)


def _tools():
    return build_research_tools(max_search_results=5, max_search_uses=4)


def test_tools_include_the_server_search_tool():
    search = _tools()[0]
    assert search["type"] == "openrouter:web_search"
    assert search["parameters"] == {"max_results": 5, "max_uses": 4}
    # It carries no "function" key: OpenRouter runs it, we never execute it.
    assert "function" not in search


def test_search_bounds_are_configurable_and_floored():
    search = build_research_tools(max_search_results=0, max_search_uses=0)[0]
    assert search["parameters"] == {"max_results": 1, "max_uses": 1}


def test_function_tools_are_strict_objects():
    for tool in _tools()[1:]:
        schema = tool["function"]["parameters"]
        assert tool["type"] == "function"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_submit_enum_matches_relations():
    submit = _tools()[2]["function"]["parameters"]["properties"]
    assert submit["relation"]["enum"] == list(RELATIONS)
    assert submit["confidence"]["enum"] == list(CONFIDENCE_VALUES)
    assert submit["evidence"]["maxItems"] == 4
    assert set(submit["evidence"]["items"]["required"]) == {"url", "quote"}


def test_tool_definitions_are_copies():
    first, second = _tools()[1], _tools()[1]
    first["function"]["name"] = "mutated"
    assert second["function"]["name"] == "fetch_page"


def test_prompt_forbids_unvisited_urls():
    prompt = PUBLISHER_RESEARCH_SYSTEM_PROMPT
    assert "Cite only URLs you actually opened" in prompt
    assert "never write a url from memory" in prompt.casefold()
    assert "discarded whole" in prompt


def test_prompt_allows_none_and_uncertain():
    prompt = PUBLISHER_RESEARCH_SYSTEM_PROMPT
    assert "correct, expected answers" in prompt
    assert '"none"' in prompt or "relation \"none\"" in prompt
    assert "uncertain" in prompt


def test_prompt_is_not_a_quality_judgement():
    assert "not judging the article" in PUBLISHER_RESEARCH_SYSTEM_PROMPT


@pytest.mark.parametrize("assets", [("BTC",), ("eth", "BTC")])
def test_messages_carry_domain_symbols_and_no_article_body(assets):
    messages = build_research_messages(
        domain="research.example.com",
        title="Example lists a new asset",
        assets=assets,
    )
    assert [message["role"] for message in messages] == ["system", "user"]
    task = messages[1]["content"]
    assert "research.example.com" in task
    assert "Example lists a new asset" in task
    for symbol in assets:
        assert symbol.upper() in task
