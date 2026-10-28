from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.geo_data import GEO_RESOURCE_PATH, country_scopes


EXPECTED_SOURCE_RESOURCE_SHA256 = (
    "8ba410a3cf64303dcf894618490eab1f230ce1da5db284955fba0de3f3995fc7"
)


def test_bundled_geo_registry_is_server_owned_and_bound_to_canonical_resource():
    payload = json.loads(GEO_RESOURCE_PATH.read_text(encoding="utf-8"))
    server_root = Path(__file__).resolve().parents[1]
    assert GEO_RESOURCE_PATH.is_relative_to(server_root)
    assert payload["source_resource_sha256"] == EXPECTED_SOURCE_RESOURCE_SHA256
    assert len(payload["countries"]) == 173
    assert GEO_RESOURCE_PATH.stat().st_size < 20_000
    assert all(set(country) == {"code", "scope"} for country in payload["countries"])
    assert hashlib.sha256(GEO_RESOURCE_PATH.read_bytes()).hexdigest() == (
        "da3da3d20dca747618bd97f1ce044b1150796446ad1bdf90f11986dd85ba4ea3"
    )


def test_country_registry_covers_six_scopes_and_known_codes():
    scopes = country_scopes()
    assert scopes["KR"] == "asia"
    assert scopes["US"] == "north_america"
    assert scopes["FR"] == "europe"
    assert set(scopes.values()) == {
        "africa", "asia", "europe", "north_america", "south_america", "oceania"
    }


def test_registry_codes_are_unique_uppercase_alpha_two():
    payload = json.loads(GEO_RESOURCE_PATH.read_text(encoding="utf-8"))
    codes = [country["code"] for country in payload["countries"]]
    assert len(codes) == len(set(codes))
    assert all(len(code) == 2 and code.isascii() and code.isupper() for code in codes)
