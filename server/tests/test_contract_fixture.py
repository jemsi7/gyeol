"""The checked-in server fixture must remain a valid FeedResponse."""
from pathlib import Path

from app.app_api_models import FeedResponse
from app.validation import validate_map


def test_feed_contract_fixture_roundtrips():
    path = Path(__file__).parent / "fixtures" / "feed_response.json"
    response = FeedResponse.model_validate_json(path.read_text(encoding="utf-8"))
    assert response.format_version == 1
    assert len(response.items) == 1
    assert [image["ref"] for image in response.items[0].map["images"]] == [0, 1]
    assert response.items[0].map["body_markdown"].count("<img:") == 2
    assert response.items[0].map["format_version"] == 3
    assert response.items[0].map["body_markdown"].count("<chart:") == 1
    assert response.items[0].map["visualizations"][0]["kind"] == "bar"
    assert response.items[0].map["used_chart_refs"] == [0]
    assert [scene["id"] for scene in response.items[0].map["scenes"]] == [
        "S-01", "S-02", "S-03", "S-04"
    ]
    assert response.items[0].map["required_claim_refs"] == ["CL-001", "CL-002"]
    assert response.items[0].map["editorial"]["critic_verdict"] == "pass"
    map_data = response.items[0].map
    source_text = "\n".join(
        point["evidence"]
        for chart in map_data["visualizations"]
        for series in chart["series"]
        for point in series["points"]
    )
    validated = validate_map(map_data, n_images=2, source_text=source_text)
    assert validated.ok, validated.errors
