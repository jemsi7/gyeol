"""Read-only country registry derived from the native ArticleFeedKit resource."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


GEO_RESOURCE_PATH = Path(__file__).resolve().parent / "resources" / "world-country-scopes.json"


@lru_cache(maxsize=1)
def country_scopes() -> dict[str, str]:
    payload = json.loads(GEO_RESOURCE_PATH.read_text(encoding="utf-8"))
    return {
        str(item["code"]): str(item["scope"])
        for item in payload.get("countries", [])
        if isinstance(item, dict) and item.get("code") and item.get("scope")
    }
