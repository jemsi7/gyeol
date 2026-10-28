from __future__ import annotations

import inspect

from app import api
from app.config import Settings
from app.feed import build_feed_item
from app.repository import Repository


def _row(*, document_type: str | None, source_name: str | None) -> dict:
    return {
        "article_id": "a" * 64,
        "source_url": "https://governance.example/proposal/1",
        "domain": "governance.example",
        "published_at": None,
        "source_language": "English",
        "sourcecountry": "US",
        "document_type": document_type,
        "document_source_name": source_name,
        "status": "ok",
        "data": {
            "format_version": 3,
            "language": "ko",
            "headline": "제안",
            "summary": "요약",
            "keywords": [],
            "body_markdown": "본문",
            "images": [],
            "scenes": [],
        },
    }


def _item(row: dict) -> dict:
    item = build_feed_item(
        row,
        settings=Settings(_env_file=None, PUBLIC_BASE_URL="https://api.example"),
        request_base_url="https://request.example/",
    )
    assert item is not None
    return item


def test_route_mixes_articles_and_documents() -> None:
    route = [
        _item(_row(document_type=None, source_name=None)),
        _item(
            _row(
                document_type="governance_proposal",
                source_name="Aave Governance",
            )
        ),
    ]

    assert [item["document_type"] for item in route] == ["", "governance_proposal"]


def test_document_exposes_same_source_fields() -> None:
    item = _item(
        _row(document_type="audit_report", source_name="Protocol Foundation")
    )

    assert item["source_url"] == "https://governance.example/proposal/1"
    assert item["domain"] == "governance.example"
    assert item["source_publisher"] == "Protocol Foundation"


def test_unsupported_document_type_is_hidden() -> None:
    item = _item(_row(document_type="whitepaper", source_name="Unknown"))

    assert item["document_type"] == ""
    assert item["source_publisher"] == ""


def test_reader_queries_and_existing_routes_carry_document_metadata() -> None:
    repository_source = inspect.getsource(Repository)
    api_source = inspect.getsource(api)

    for method_name in (
        "recommendation_feed_rows",
        "account_saved_article_maps",
        "curation_candidate_rows",
        "current_curation_catalog",
        "current_explore_timeline_catalog",
        "search_article_maps",
        "published_article_maps_by_ids",
        "published_editorial_maps",
        "recent_article_maps",
        "latest_article_maps",
        "random_article_maps",
    ):
        method_source = inspect.getsource(getattr(Repository, method_name))
        assert "document_type" in method_source
        assert "document_source_name" in method_source

    assert '"document_type": raw_item.get("document_type")' in api_source
    assert '"document_type": raw_node.get("document_type")' in api_source
    assert "document" in repository_source


def test_no_separate_document_tab() -> None:
    api_source = inspect.getsource(api)
    assert '"document_routes"' not in api_source
    assert '"document_tab"' not in api_source
