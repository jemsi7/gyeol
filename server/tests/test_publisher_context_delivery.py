"""Publisher context on the wire (I-13202-c, DR-13203).

Two things must hold all the way to the app: the factual sentence reaches the
reader, and the basis codes -- the operator-facing half -- never do. The second
is enforced structurally: ``resolve_map`` is an allowlist projection, so a key
nobody added cannot leak because someone forgot to strip it.
"""
from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from app.map_loader import resolve_map
from app.publisher_context import build_publisher_context
from app.publishers import PublisherRecord, PublisherRegistry


IOS_MODELS = (
    Path(__file__).resolve().parents[2]
    / "ios/ArticleFeedKit/Sources/ArticleFeedKit/Models.swift"
)
IOS_CARD = (
    Path(__file__).resolve().parents[2]
    / "ios/TextShortsApp/TextShortsApp/Screens/HomeScreen.swift"
)


def _map(**overrides) -> dict:
    data = {
        "format_version": 3,
        "language": "ko",
        "headline": "제목",
        "summary": "요약",
        "keywords": [],
        "body_markdown": "본문",
        "images": [],
        "scenes": [],
        "publisher_context": "예시거래소이(가) 발행했고, 자사에 상장된 자산을 다룹니다.",
        "publisher_context_basis": ["exchange_listed", "sponsored_path"],
    }
    data.update(overrides)
    return data


def _resolved(**overrides) -> dict:
    return resolve_map(_map(**overrides), lambda image_id: f"https://img/{image_id}")


def test_sentence_reaches_the_app():
    assert _resolved()["publisher_context"].startswith("예시거래소")


def test_basis_never_reaches_the_app():
    assert "publisher_context_basis" not in _resolved()


def test_projection_is_an_allowlist_not_a_blocklist():
    # The guarantee above must come from resolve_map only naming what it emits,
    # not from a delete of this one key that a future field could miss.
    body = inspect.getsource(resolve_map)
    emitted = [
        line
        for line in body.split("return {")[1].splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not any("publisher_context_basis" in line for line in emitted)
    # No delete/pop anywhere: the projection never has the key to begin with.
    assert "pop(" not in body
    assert "del " not in body


def test_absent_field_becomes_an_empty_string():
    data = _map()
    del data["publisher_context"]
    resolved = resolve_map(data, lambda image_id: "")
    # Maps written before DR-13203 must decode, not fail.
    assert resolved["publisher_context"] == ""


def test_delivery_map_carries_both_halves():
    from app.editorial_pipeline import compile_delivery_map

    body = inspect.getsource(compile_delivery_map)
    assert '"publisher_context": publisher_context' in body
    assert '"publisher_context_basis": list(publisher_context_basis)' in body


def test_pipeline_builds_the_context_from_the_source_packet():
    from app.editorial_pipeline import EditorialPipeline

    body = inspect.getsource(EditorialPipeline)
    assert "build_publisher_context(" in body
    assert "subject_assets_from_ledger(" in body
    # Loaded once per pipeline, not once per article.
    assert "PublisherRegistry.load(settings.publishers_file)" in body


def test_ios_decodes_the_sentence_and_defaults_it_empty():
    swift = IOS_MODELS.read_text(encoding="utf-8")
    assert 'case publisherContext = "publisher_context"' in swift
    assert "decodeIfPresent(\n            String.self,\n            forKey: .publisherContext\n        ) ?? \"\"" in swift
    # The basis codes have no Swift representation at all.
    assert "publisher_context_basis" not in swift


def test_ios_renders_nothing_when_the_sentence_is_empty():
    swift = IOS_CARD.read_text(encoding="utf-8")
    assert "if !article.publisherContext.isEmpty {" in swift
    # Rendered with the existing muted caption style on the existing card, so
    # no new component, tab or panel is introduced.
    assert "Text(article.publisherContext)" in swift
    assert "sheet" not in swift.split("article.publisherContext")[1][:400]


@pytest.mark.parametrize(
    ("publisher_type", "assets", "expected_fragment"),
    [
        ("exchange", {"BTC"}, "자사에 상장된"),
        ("project", {"BTC"}, "자사 프로토콜"),
    ],
)
def test_end_to_end_shape_for_a_registered_publisher(
    publisher_type, assets, expected_fragment
):
    field = "listed_assets" if publisher_type == "exchange" else "protocol_assets"
    record = PublisherRecord(
        domain="example.com",
        publisher_type=publisher_type,
        owner="예시",
        source_url="https://example.com",
        verified_on=date(2026, 8, 9),
        **{field: frozenset(assets)},
    )
    context = build_publisher_context(
        url="https://example.com/a",
        domain="example.com",
        subject_assets=frozenset(assets),
        registry=PublisherRegistry((record,)),
    )
    resolved = _resolved(
        publisher_context=context.sentence,
        publisher_context_basis=list(context.basis),
    )
    assert expected_fragment in resolved["publisher_context"]
    assert "publisher_context_basis" not in resolved
