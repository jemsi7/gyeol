from __future__ import annotations

from pathlib import Path

from app.app_api_models import DiscoveryResponse


FIXTURE = Path(__file__).parent / "fixtures" / "discovery_response.json"


def test_canonical_discovery_fixture_matches_wire_and_catalog_contract():
    response = DiscoveryResponse.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    assert response.format_version == 1
    assert len(response.routes) == 2
    assert response.representative_route_id == response.routes[0].id
    assert [route.is_representative for route in response.routes] == [True, False]
    assert [len(route.items) for route in response.routes] == [4, 4]
    assert [item.role for item in response.routes[0].items] == [
        "start",
        "context",
        "evidence",
        "counterpoint",
    ]

    article_ids = [
        item.article.article_id
        for route in response.routes
        for item in route.items
    ]
    assert len(article_ids) == len(set(article_ids)) == 8
    assert [result.depth for result in response.results] == ["quick", "deep"]
    assert len(response.search_suggestions) == 4


def test_canonical_discovery_fixture_roundtrips_without_unknown_fields():
    response = DiscoveryResponse.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    encoded = response.model_dump_json()
    decoded = DiscoveryResponse.model_validate_json(encoded)

    assert decoded == response
