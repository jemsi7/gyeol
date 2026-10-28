from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from verify_editorial_push_vertical import (  # noqa: E402
    ARTICLE_ID,
    STAGE_NAMES,
    build_manifest,
    run_editorial_push_vertical,
    validate_manifest,
)


@pytest.mark.asyncio
async def test_signed_in_first_publish_fake_apns_and_tap_contract_are_one_vertical():
    result = await run_editorial_push_vertical()

    assert [stage["name"] for stage in result.stages] == list(STAGE_NAMES)
    assert all(stage["status"] == "passed" for stage in result.stages)
    assert result.article_id == ARTICLE_ID
    assert result.delivery_count == 1
    assert result.external_network_calls == 0
    assert result.production_mutations == 0


@pytest.mark.asyncio
async def test_vertical_manifest_is_bounded_secret_free_and_keeps_live_gate_explicit():
    result = await run_editorial_push_vertical()
    manifest = build_manifest(result, {"server": 1, "package": 1, "app": 1})

    assert validate_manifest(manifest) == []
    assert len(json.dumps(manifest).encode("utf-8")) < 100 * 1024
    assert manifest["live_gate"]["status"] == "not-run"

    leaked = dict(manifest)
    leaked["token_ciphertext"] = "should-never-appear"
    assert "secret_material_detected" in validate_manifest(leaked)
