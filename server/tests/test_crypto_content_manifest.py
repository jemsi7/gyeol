from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_crypto_content_manifest",
    WORKSPACE_ROOT / "scripts" / "build_crypto_content_manifest.py",
)
manifest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manifest
SPEC.loader.exec_module(manifest)


def _scaffold(root: Path, *, reverse: bool = False) -> None:
    files = {
        "server/app/main.py": b"app\n",
        "server/scripts/operator.py": b"operator\n",
        "server/tests/test_main.py": b"test\n",
        "server/Dockerfile": b"FROM python\n",
        "server/railway.toml": b"[build]\n",
        "server/Procfile": b"web: run\n",
        "server/.railwayignore": b".env\n",
        "server/.dockerignore": b".env\n",
        "server/requirements.txt": b"pytest\n",
        "server/jobs.yaml": b"jobs: []\n",
        "scripts/build.py": b"build\n",
        "ios/ArticleFeedKit/Package.swift": b"// package\n",
        "ios/ArticleFeedKit/Sources/Feed.swift": b"source\n",
        "ios/ArticleFeedKit/Tests/FeedTests.swift": b"test\n",
        "ios/TextShortsApp/TextShortsApp/App.swift": b"app\n",
        "ios/TextShortsApp/TextShortsAppTests/AppTests.swift": b"test\n",
        "ios/TextShortsApp/TextShortsAppUITests/AppUITests.swift": b"ui\n",
        "ios/TextShortsApp/TextShortsApp.xcodeproj/project.pbxproj": b"pbx\n",
        "docs/maps/01_concept_map.md": b"# map\n",
    }
    items = list(files.items())
    if reverse:
        items.reverse()
    for relative, payload in items:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _create(root: Path, output: Path) -> tuple[dict, str]:
    value = manifest.build_content_manifest(workspace_root=root)
    payload = manifest.canonical_manifest_bytes(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    return value, manifest.manifest_digest(value)


def _receipt_payload(kind: str, digest: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "content_manifest_sha256": digest,
        "ok": True,
        "receipt_kind": kind,
        "receipt_mode": "final",
    }
    if kind == "t12_carried":
        payload.update(
            gate="pass",
            relevance_path_identical=True,
            carried_from_content_manifest_sha256="a" * 64,
            article_family_precision=0.95,
            document_false_positives=0,
            remeasurement_blocked_reason="fixture_rows_deleted_by_retention",
        )
    elif kind == "t26":
        payload["crypto"] = {
            "article_map_limit": 0,
            "daily_map_limit": 7,
            "document_map_limit": 7,
            "reader_exposed": False,
        }
        payload.update(
            held_document_maps=2,
            reader_visible_crypto_maps=0,
        )
    elif kind == "t09b":
        payload["crypto"] = {
            "article_map_limit": 13,
            "daily_map_limit": 20,
            "document_map_limit": 7,
            "reader_exposed": False,
        }
    elif kind == "f1_staging":
        payload.update(
            gate="pass",
            fail_open_cases={
                name: {
                    "reader_status": 200,
                    "body_preserved": True,
                    "error_ui": False,
                }
                for name in manifest.F1_FAIL_OPEN_CASES
            },
        )
    elif kind == "g2":
        payload.update(
            gate="pass",
            t19_t21_green=True,
            three_day_article_visual_check=True,
        )
    elif kind == "scope_waiver":
        payload.update({fact: True for fact in manifest.SCOPE_WAIVER_FACTS})
    elif kind in {"server_regression", "ios_regression"}:
        payload.update(status="passed", failed=0)
    else:  # pragma: no cover - helper guard
        raise AssertionError(kind)
    return payload


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _receipt_paths(root: Path, digest: str) -> dict[str, Path]:
    receipts: dict[str, Path] = {}
    for kind in manifest.REQUIRED_PROMOTION_RECEIPT_KINDS:
        path = root / "tmp" / "crypto_promotion" / "receipts" / f"{kind}.json"
        _write_receipt(path, _receipt_payload(kind, digest))
        receipts[kind] = path
    return receipts


def _binding(
    root: Path,
    manifest_path: Path,
    digest: str,
    receipts: dict[str, Path],
) -> Path:
    output = root / "tmp/crypto_promotion/receipt_binding.v1.json"
    manifest.bind_promotion_receipts(
        workspace_root=root,
        manifest_path=manifest_path,
        expected_digest=digest,
        receipts=receipts,
        output_path=output,
    )
    return output


def _bundle_fixture(root: Path) -> tuple[dict, str, Path, Path, Path]:
    _scaffold(root)
    manifest_path = root / "tmp/crypto_promotion/content_manifest.v1.json"
    value, digest = _create(root, manifest_path)
    receipts = _receipt_paths(root, digest)
    binding_path = _binding(root, manifest_path, digest, receipts)
    output_root = root / "tmp/crypto_promotion/bundle"
    manifest.build_promotion_bundle(
        workspace_root=root,
        manifest_path=manifest_path,
        expected_digest=digest,
        binding_path=binding_path,
        output_root=output_root,
    )
    return value, digest, manifest_path, binding_path, output_root


def test_manifest_is_stable_across_walk_order(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _scaffold(first)
    _scaffold(second, reverse=True)

    one = manifest.build_content_manifest(workspace_root=first)
    two = manifest.build_content_manifest(workspace_root=second)

    assert manifest.canonical_manifest_bytes(one) == manifest.canonical_manifest_bytes(two)
    assert manifest.manifest_digest(one) == manifest.manifest_digest(two)


def test_manifest_contains_server_ios_maps_configs_and_tests(tmp_path):
    _scaffold(tmp_path)
    value = manifest.build_content_manifest(workspace_root=tmp_path)
    paths = {item["path"] for item in value["files"]}

    assert {
        "server/app/main.py",
        "server/scripts/operator.py",
        "server/tests/test_main.py",
        "server/Dockerfile",
        "server/railway.toml",
        "server/Procfile",
        "server/.railwayignore",
        "server/.dockerignore",
        "server/requirements.txt",
        "server/jobs.yaml",
        "scripts/build.py",
        "ios/ArticleFeedKit/Package.swift",
        "ios/ArticleFeedKit/Sources/Feed.swift",
        "ios/ArticleFeedKit/Tests/FeedTests.swift",
        "ios/TextShortsApp/TextShortsApp/App.swift",
        "ios/TextShortsApp/TextShortsAppTests/AppTests.swift",
        "ios/TextShortsApp/TextShortsAppUITests/AppUITests.swift",
        "ios/TextShortsApp/TextShortsApp.xcodeproj/project.pbxproj",
        "docs/maps/01_concept_map.md",
    } <= paths


def test_manifest_contains_railway_procfile_and_ignore_configs(tmp_path):
    _scaffold(tmp_path)

    paths = {
        item["path"]
        for item in manifest.build_content_manifest(workspace_root=tmp_path)[
            "files"
        ]
    }

    assert {
        "server/railway.toml",
        "server/Procfile",
        "server/.railwayignore",
        "server/.dockerignore",
    } <= paths


def test_manifest_excludes_self_generated_cache_website_and_secrets(tmp_path):
    _scaffold(tmp_path)
    excluded = {
        "server/app/__pycache__/main.pyc": b"cache",
        "server/app/.env": b"SECRET=value",
        "server/app/secrets/key.txt": b"secret",
        "docs/maps/render.bin": b"generated",
        "website/index.html": b"website",
        "tmp/crypto_promotion/content_manifest.v1.json": b"self",
    }
    for relative, payload in excluded.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    value = manifest.build_content_manifest(workspace_root=tmp_path)
    paths = {item["path"] for item in value["files"]}

    assert not paths.intersection(excluded)
    encoded = manifest.canonical_manifest_bytes(value)
    assert b"SECRET=value" not in encoded
    assert not encoded.endswith(b"\n")


def test_manifest_rejects_symlink_escape(tmp_path):
    _scaffold(tmp_path)
    outside = tmp_path.parent / "outside-manifest-fixture.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "server/app/escape.py").symlink_to(outside)

    with pytest.raises(manifest.ManifestError, match="symlink_in_scope"):
        manifest.build_content_manifest(workspace_root=tmp_path)


def test_manifest_rejects_include_root_symlink_escape(tmp_path):
    _scaffold(tmp_path)
    include_root = tmp_path / "server/app"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-app"
    include_root.rename(outside)
    include_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(manifest.ManifestError, match="symlink_in_scope"):
        manifest.build_content_manifest(workspace_root=tmp_path)


def test_manifest_rejects_excluded_directory_symlink(tmp_path):
    _scaffold(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secrets"
    outside.mkdir()
    (outside / "key.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "server/app/secrets").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(manifest.ManifestError, match="symlink_in_scope"):
        manifest.build_content_manifest(workspace_root=tmp_path)


def test_manifest_rejects_nfc_normalized_path_collision(tmp_path):
    paths: dict[str, Path] = {}
    raw_paths: dict[str, str] = {}
    composed = tmp_path / "server/app/caf\N{LATIN SMALL LETTER E WITH ACUTE}.py"
    decomposed = tmp_path / "server/app/cafe\N{COMBINING ACUTE ACCENT}.py"

    manifest._register_scope_path(
        paths,
        raw_paths,
        root=tmp_path,
        path=composed,
    )
    with pytest.raises(manifest.ManifestError, match="normalized_path_collision"):
        manifest._register_scope_path(
            paths,
            raw_paths,
            root=tmp_path,
            path=decomposed,
        )


def test_manifest_detects_added_missing_size_and_hash_mismatch(tmp_path):
    _scaffold(tmp_path)
    output = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, output)
    assert manifest.verify_content_manifest(
        workspace_root=tmp_path,
        manifest_path=output,
        expected_digest=digest,
    ) == digest

    (tmp_path / "server/app/main.py").write_bytes(b"changed\n")
    with pytest.raises(manifest.ManifestError, match="workspace_content_mismatch"):
        manifest.verify_content_manifest(
            workspace_root=tmp_path,
            manifest_path=output,
            expected_digest=digest,
        )

    (tmp_path / "server/app/main.py").write_bytes(b"app\n")
    (tmp_path / "server/app/added.py").write_bytes(b"added\n")
    with pytest.raises(manifest.ManifestError, match="workspace_content_mismatch"):
        manifest.verify_content_manifest(
            workspace_root=tmp_path,
            manifest_path=output,
            expected_digest=digest,
        )

    (tmp_path / "server/app/added.py").unlink()
    (tmp_path / "server/app/main.py").unlink()
    with pytest.raises(manifest.ManifestError, match="workspace_content_mismatch"):
        manifest.verify_content_manifest(
            workspace_root=tmp_path,
            manifest_path=output,
            expected_digest=digest,
        )

    (tmp_path / "server/app/main.py").write_bytes(b"app\n")
    stored = json.loads(output.read_text())
    stored["files"][0]["sha256"] = "0" * 64
    output.write_bytes(manifest.canonical_manifest_bytes(stored))
    with pytest.raises(manifest.ManifestError, match="manifest_digest_mismatch"):
        manifest.verify_content_manifest(
            workspace_root=tmp_path,
            manifest_path=output,
            expected_digest=digest,
        )


def test_promotion_refuses_receipt_or_preupload_digest_mismatch():
    digest = "a" * 64
    manifest.verify_receipt_digests(
        expected_digest=digest,
        receipts=[{"content_manifest_sha256": digest}],
    )
    with pytest.raises(
        manifest.ManifestError, match="promotion_receipt_digest_mismatch"
    ):
        manifest.verify_receipt_digests(
            expected_digest=digest,
            receipts=[{"content_manifest_sha256": "b" * 64}],
        )


def test_cli_uses_content_manifest_sha256_for_create_verify_and_bind(
    tmp_path,
    monkeypatch,
    capsys,
):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_crypto_content_manifest.py",
            "create",
            "--workspace-root",
            str(tmp_path),
            "--output",
            str(manifest_path),
        ],
    )
    assert manifest.main() == 0
    created = json.loads(capsys.readouterr().out)
    digest = created["content_manifest_sha256"]
    assert "manifest_digest" not in created

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_crypto_content_manifest.py",
            "verify",
            "--workspace-root",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--expected-digest",
            digest,
        ],
    )
    assert manifest.main() == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["content_manifest_sha256"] == digest
    assert "manifest_digest" not in verified

    receipts = _receipt_paths(tmp_path, digest)
    binding_path = tmp_path / "tmp/crypto_promotion/receipt_binding.v1.json"
    receipt_arguments = [
        value
        for kind in manifest.REQUIRED_PROMOTION_RECEIPT_KINDS
        for value in ("--receipt", f"{kind}={receipts[kind]}")
    ]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_crypto_content_manifest.py",
            "bind-receipts",
            "--workspace-root",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--expected-digest",
            digest,
            *receipt_arguments,
            "--output",
            str(binding_path),
        ],
    )
    assert manifest.main() == 0
    bound = json.loads(capsys.readouterr().out)
    assert bound["content_manifest_sha256"] == digest
    assert "manifest_digest" not in bound


def test_bind_receipts_requires_exact_named_set_and_unique_paths(tmp_path):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    binding_path = _binding(tmp_path, manifest_path, digest, receipts)

    raw = binding_path.read_bytes()
    value = json.loads(raw)
    assert raw == manifest.canonical_manifest_bytes(value)
    assert value["content_manifest_sha256"] == digest
    assert [item["kind"] for item in value["receipts"]] == list(
        manifest.REQUIRED_PROMOTION_RECEIPT_KINDS
    )
    assert all(not Path(item["path"]).is_absolute() for item in value["receipts"])

    duplicate_path = dict(receipts)
    duplicate_path["scope_waiver"] = duplicate_path["g2"]
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_path_duplicate",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=duplicate_path,
            output_path=binding_path,
        )

    missing_kind = dict(receipts)
    missing_kind.pop("scope_waiver")
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_kind_missing",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=missing_kind,
            output_path=binding_path,
        )

    missing_file = dict(receipts)
    missing_file["scope_waiver"] = tmp_path / "tmp/crypto_promotion/receipts/missing.json"
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_missing",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=missing_file,
            output_path=binding_path,
        )

    mismatched = dict(receipts)
    _write_receipt(
        mismatched["scope_waiver"],
        _receipt_payload("scope_waiver", "b" * 64),
    )
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_digest_mismatch",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=mismatched,
            output_path=binding_path,
        )


def test_bind_receipts_rejects_duplicate_json_keys_kind_mismatch_and_false_ok(
    tmp_path,
):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    output = tmp_path / "tmp/crypto_promotion/receipt_binding.v1.json"

    receipts["t12_carried"].write_text(
        "{"
        f'"content_manifest_sha256":"{digest}",'
        '"gate":"pass","nested":{"value":1,"value":2},'
        '"ok":true,"receipt_kind":"t12_carried"}',
        encoding="utf-8",
    )
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_json_duplicate_key",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=receipts,
            output_path=output,
        )

    wrong_kind = _receipt_payload("t12_carried", digest)
    wrong_kind["receipt_kind"] = "f1_staging"
    _write_receipt(receipts["t12_carried"], wrong_kind)
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_kind_mismatch",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=receipts,
            output_path=output,
        )

    false_ok = _receipt_payload("t12_carried", digest)
    false_ok["ok"] = False
    _write_receipt(receipts["t12_carried"], false_ok)
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_not_ok",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=receipts,
            output_path=output,
        )


@pytest.mark.parametrize("mode", [None, "reference", True])
def test_reference_receipt_cannot_bind_even_when_ok_true(tmp_path, mode):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    payload = _receipt_payload("t12_carried", digest)
    if mode is None:
        payload.pop("receipt_mode")
    else:
        payload["receipt_mode"] = mode
    _write_receipt(receipts["t12_carried"], payload)

    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_mode_mismatch",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=receipts,
            output_path=tmp_path / "tmp/crypto_promotion/receipt_binding.v1.json",
        )


@pytest.mark.parametrize(
    ("kind", "invalid_fields"),
    [
        ("t12_carried", {"gate": "fail"}),
        ("t26", {"held_document_maps": 1}),
        ("t26", {"reader_visible_crypto_maps": 1}),
        (
            "t26",
            {
                "crypto": {
                    "article_map_limit": 0,
                    "daily_map_limit": 8,
                    "document_map_limit": 7,
                    "reader_exposed": False,
                }
            },
        ),
        (
            "t26",
            {
                "crypto": {
                    "article_map_limit": 0,
                    "daily_map_limit": 7,
                    "document_map_limit": 6,
                    "reader_exposed": False,
                }
            },
        ),
        (
            "t26",
            {
                "crypto": {
                    "article_map_limit": 1,
                    "daily_map_limit": 7,
                    "document_map_limit": 7,
                    "reader_exposed": False,
                }
            },
        ),
        (
            "t26",
            {
                "crypto": {
                    "article_map_limit": 0,
                    "daily_map_limit": 7,
                    "document_map_limit": 7,
                    "reader_exposed": True,
                }
            },
        ),
        (
            "t09b",
            {
                "crypto": {
                    "article_map_limit": 12,
                    "daily_map_limit": 20,
                    "document_map_limit": 7,
                    "reader_exposed": False,
                }
            },
        ),
        (
            "f1_staging",
            {
                "fail_open_cases": {
                    "missing_field": {
                        "reader_status": 200,
                        "body_preserved": True,
                        "error_ui": False,
                    },
                    "null_value": {
                        "reader_status": 200,
                        "body_preserved": True,
                        "error_ui": False,
                    },
                    "lookup_failure": {
                        "reader_status": 500,
                        "body_preserved": True,
                        "error_ui": False,
                    },
                }
            },
        ),
        ("g2", {"three_day_article_visual_check": False}),
        ("scope_waiver", {"g1_waived_for_this_release": False}),
        ("server_regression", {"failed": 1}),
        ("ios_regression", {"status": "failed"}),
    ],
)
def test_bind_receipts_enforces_each_kind_success_predicate(
    tmp_path,
    kind,
    invalid_fields,
):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    payload = _receipt_payload(kind, digest)
    payload.update(invalid_fields)
    _write_receipt(receipts[kind], payload)

    with pytest.raises(
        manifest.ManifestError,
        match=f"promotion_receipt_{kind}_predicate_failed",
    ):
        manifest.bind_promotion_receipts(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            receipts=receipts,
            output_path=tmp_path / "tmp/crypto_promotion/receipt_binding.v1.json",
        )


def test_bind_cli_rejects_duplicate_and_unknown_receipt_kinds():
    receipt = "tmp/crypto_promotion/receipt.json"
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_kind_duplicate",
    ):
        manifest._parse_receipt_arguments(
            [
                *(f"{kind}={receipt}" for kind in manifest.REQUIRED_PROMOTION_RECEIPT_KINDS),
                f"t12_carried={receipt}",
            ]
        )
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_receipt_kind_unknown",
    ):
        manifest._parse_receipt_arguments(
            [
                *(f"{kind}={receipt}" for kind in manifest.REQUIRED_PROMOTION_RECEIPT_KINDS),
                f"unknown={receipt}",
            ]
        )


def test_server_upload_component_strips_prefix_and_records_exact_mapping(tmp_path):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    binding_path = _binding(tmp_path, manifest_path, digest, receipts)
    output_root = tmp_path / "tmp/crypto_promotion/bundle"

    receipt = manifest.build_promotion_bundle(
        workspace_root=tmp_path,
        manifest_path=manifest_path,
        expected_digest=digest,
        binding_path=binding_path,
        output_root=output_root,
    )

    expected_mappings = [
        {
            "manifest_path": item["path"],
            "sha256": item["sha256"],
            "size": item["size"],
            "upload_path": f'server-upload/{item["path"][len("server/"):]}',
        }
        for item in value["files"]
        if item["path"].startswith("server/")
    ]
    expected = {item["upload_path"]: item for item in expected_mappings}
    actual = {
        path.relative_to(output_root).as_posix(): path
        for path in output_root.rglob("*")
        if path.is_file()
    }
    assert set(actual) == set(expected)
    for relative, path in actual.items():
        raw = path.read_bytes()
        assert len(raw) == expected[relative]["size"]
        assert hashlib.sha256(raw).hexdigest() == expected[relative]["sha256"]
    assert receipt["content_manifest_sha256"] == digest
    assert receipt["promotion_binding_sha256"] == hashlib.sha256(
        binding_path.read_bytes()
    ).hexdigest()
    assert receipt["components"]["server"] == {
        "files": len(expected_mappings),
        "mappings": expected_mappings,
        "source_prefix": "server/",
        "upload_root": "server-upload/",
    }
    assert not any(path.startswith("server-upload/server/") for path in actual)
    assert not any(path.startswith("ios/") for path in actual)
    persisted = json.loads(
        manifest.bundle_receipt_path(output_root).read_text(encoding="utf-8")
    )
    assert persisted == receipt
    assert manifest.bundle_receipt_path(output_root).parent == output_root.parent


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("extra", "bundle_file_set_mismatch"),
        ("missing", "bundle_file_set_mismatch"),
        ("mutated", "bundle_content_mismatch"),
        ("binding", "bundle_binding_mismatch"),
        ("root_symlink", "bundle_symlink"),
    ],
)
def test_verify_bundle_rejects_extra_missing_mutated_and_binding_mismatch(
    tmp_path,
    mutation,
    error_code,
):
    _value, digest, manifest_path, binding_path, output_root = _bundle_fixture(
        tmp_path
    )
    receipt_path = manifest.bundle_receipt_path(output_root)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    first_upload = output_root / receipt["components"]["server"]["mappings"][0][
        "upload_path"
    ]

    if mutation == "extra":
        extra = output_root / "server-upload/unlisted.py"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"unlisted\n")
    elif mutation == "missing":
        first_upload.unlink()
    elif mutation == "mutated":
        first_upload.write_bytes(b"mutated\n")
    elif mutation == "binding":
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["receipts"][0]["sha256"] = "f" * 64
        binding_path.write_bytes(manifest.canonical_manifest_bytes(binding))
    else:
        real_root = output_root.with_name("real-bundle")
        output_root.rename(real_root)
        output_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(manifest.ManifestError, match=error_code):
        manifest.verify_promotion_bundle(
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=binding_path,
            bundle_root=output_root,
            bundle_receipt_path=receipt_path,
        )


def test_deploy_verifies_same_bundle_before_and_after_upload(
    tmp_path,
    monkeypatch,
    capsys,
):
    _value, digest, manifest_path, binding_path, output_root = _bundle_fixture(
        tmp_path
    )
    receipt_path = manifest.bundle_receipt_path(output_root)
    argv = [
        "build_crypto_content_manifest.py",
        "verify-bundle",
        "--manifest",
        str(manifest_path),
        "--expected-digest",
        digest,
        "--binding",
        str(binding_path),
        "--bundle-root",
        str(output_root),
        "--bundle-receipt",
        str(receipt_path),
    ]

    monkeypatch.setattr(sys, "argv", argv)
    assert manifest.main() == 0
    before = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys, "argv", argv)
    assert manifest.main() == 0
    after = json.loads(capsys.readouterr().out)
    assert before["content_manifest_sha256"] == digest
    assert after == before

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    first_upload = output_root / receipt["components"]["server"]["mappings"][0][
        "upload_path"
    ]
    first_upload.write_bytes(b"changed after upload\n")
    monkeypatch.setattr(sys, "argv", argv)
    assert manifest.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "error_type": "bundle_content_mismatch",
        "ok": False,
    }


def test_atomic_writers_use_random_same_directory_exclusive_nofollow_replace(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "artifacts/content_manifest.v1.json"
    target.parent.mkdir(parents=True)
    predictable = target.with_suffix(f"{target.suffix}.tmp")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")
    predictable.symlink_to(outside)
    open_calls: list[tuple[Path, int, int]] = []
    replace_calls: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []
    real_open = manifest.os.open
    real_replace = manifest.os.replace
    real_fsync = manifest.os.fsync

    def recording_open(path, flags, mode=0o777):
        open_calls.append((Path(path), flags, mode))
        return real_open(path, flags, mode)

    def recording_replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    def recording_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(manifest.os, "open", recording_open)
    monkeypatch.setattr(manifest.os, "replace", recording_replace)
    monkeypatch.setattr(manifest.os, "fsync", recording_fsync)

    manifest._write_manifest(target, b"canonical")

    exclusive = [call for call in open_calls if call[1] & os.O_CREAT]
    assert len(exclusive) == 1
    temporary, flags, mode = exclusive[0]
    assert temporary.parent == target.parent
    assert temporary != predictable
    assert flags & (os.O_CREAT | os.O_EXCL | os.O_WRONLY) == (
        os.O_CREAT | os.O_EXCL | os.O_WRONLY
    )
    if hasattr(os, "O_NOFOLLOW"):
        assert flags & os.O_NOFOLLOW
    assert mode == 0o600
    assert replace_calls == [(temporary, target)]
    assert len(fsync_calls) >= 2
    assert target.read_bytes() == b"canonical"
    assert outside.read_bytes() == b"outside"

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "symlink-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(
        manifest.ManifestError,
        match="atomic_output_parent_symlink",
    ):
        manifest._write_manifest(symlink_parent / "binding.json", b"binding")


def test_atomic_json_publish_rolls_back_parent_fsync_failure_and_retries(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "artifacts/binding.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous")
    original_fsync_directory = manifest._fsync_directory
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected parent fsync failure")
        return original_fsync_directory(path)

    monkeypatch.setattr(manifest, "_fsync_directory", fail_once)
    with pytest.raises(manifest.ManifestError, match="atomic_write_failed"):
        manifest._write_manifest(target, b"replacement")
    assert target.read_bytes() == b"previous"

    monkeypatch.setattr(manifest, "_fsync_directory", original_fsync_directory)
    manifest._write_manifest(target, b"replacement")
    assert target.read_bytes() == b"replacement"

    new_target = tmp_path / "artifacts/new-receipt.json"
    calls = 0
    monkeypatch.setattr(manifest, "_fsync_directory", fail_once)
    with pytest.raises(manifest.ManifestError, match="atomic_write_failed"):
        manifest._write_manifest(new_target, b"new")
    assert not new_target.exists()


def test_atomic_bundle_publish_rejects_symlink_existing_output_and_partial_tree(
    tmp_path,
    monkeypatch,
):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    binding_path = _binding(tmp_path, manifest_path, digest, receipts)

    outside = tmp_path / "outside-bundle"
    outside.mkdir()
    symlink_output = tmp_path / "tmp/crypto_promotion/symlink-bundle"
    symlink_output.symlink_to(outside, target_is_directory=True)
    with pytest.raises(manifest.ManifestError, match="bundle_output_symlink"):
        manifest.build_promotion_bundle(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=binding_path,
            output_root=symlink_output,
        )

    existing_output = tmp_path / "tmp/crypto_promotion/existing-bundle"
    existing_output.mkdir()
    with pytest.raises(manifest.ManifestError, match="bundle_output_exists"):
        manifest.build_promotion_bundle(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=binding_path,
            output_root=existing_output,
        )

    partial_output = tmp_path / "tmp/crypto_promotion/partial-bundle"
    before = set(partial_output.parent.iterdir())
    original_write = manifest._write_exclusive_file
    calls = 0

    def fail_after_first(path, payload):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("injected partial write")
        return original_write(path, payload)

    monkeypatch.setattr(manifest, "_write_exclusive_file", fail_after_first)
    with pytest.raises(manifest.ManifestError, match="bundle_write_failed"):
        manifest.build_promotion_bundle(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=binding_path,
            output_root=partial_output,
        )
    assert not partial_output.exists()
    assert set(partial_output.parent.iterdir()) == before


@pytest.mark.parametrize(
    "failure",
    ["parent_fsync", "receipt_write", "final_verify"],
)
def test_bundle_publish_rolls_back_late_failure_and_allows_retry(
    tmp_path,
    monkeypatch,
    failure,
):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    receipts = _receipt_paths(tmp_path, digest)
    binding_path = _binding(tmp_path, manifest_path, digest, receipts)
    output_root = tmp_path / "tmp/crypto_promotion/bundle"
    receipt_path = manifest.bundle_receipt_path(output_root)
    original_fsync_directory = manifest._fsync_directory
    original_write_manifest = manifest._write_manifest
    original_verify_bundle = manifest.verify_promotion_bundle

    if failure == "parent_fsync":
        failed = False

        def fail_publish_parent_once(path):
            nonlocal failed
            if path == output_root.parent and not failed:
                failed = True
                raise OSError("injected bundle parent fsync failure")
            return original_fsync_directory(path)

        monkeypatch.setattr(
            manifest,
            "_fsync_directory",
            fail_publish_parent_once,
        )
        error_code = "bundle_write_failed"
    elif failure == "receipt_write":

        def fail_receipt_write(path, payload):
            if path == receipt_path:
                raise manifest.ManifestError("injected_receipt_write_failure")
            return original_write_manifest(path, payload)

        monkeypatch.setattr(manifest, "_write_manifest", fail_receipt_write)
        error_code = "injected_receipt_write_failure"
    else:

        def fail_final_verify(**_kwargs):
            raise manifest.ManifestError("injected_final_verify_failure")

        monkeypatch.setattr(
            manifest,
            "verify_promotion_bundle",
            fail_final_verify,
        )
        error_code = "injected_final_verify_failure"

    with pytest.raises(manifest.ManifestError, match=error_code):
        manifest.build_promotion_bundle(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=binding_path,
            output_root=output_root,
        )
    assert not output_root.exists()
    assert not receipt_path.exists()

    monkeypatch.setattr(
        manifest,
        "_fsync_directory",
        original_fsync_directory,
    )
    monkeypatch.setattr(manifest, "_write_manifest", original_write_manifest)
    monkeypatch.setattr(
        manifest,
        "verify_promotion_bundle",
        original_verify_bundle,
    )
    manifest.build_promotion_bundle(
        workspace_root=tmp_path,
        manifest_path=manifest_path,
        expected_digest=digest,
        binding_path=binding_path,
        output_root=output_root,
    )
    assert output_root.is_dir()
    assert receipt_path.is_file()


def test_deploy_refuses_missing_or_mismatched_binding(tmp_path):
    _scaffold(tmp_path)
    manifest_path = tmp_path / "tmp/crypto_promotion/content_manifest.v1.json"
    _value, digest = _create(tmp_path, manifest_path)
    output_root = tmp_path / "tmp/crypto_promotion/bundle"

    with pytest.raises(manifest.ManifestError, match="promotion_binding_missing"):
        manifest.build_promotion_bundle(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=tmp_path / "tmp/crypto_promotion/missing-binding.json",
            output_root=output_root,
        )

    receipts = _receipt_paths(tmp_path, digest)
    binding_path = _binding(tmp_path, manifest_path, digest, receipts)
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["content_manifest_sha256"] = "b" * 64
    binding_path.write_bytes(manifest.canonical_manifest_bytes(binding))
    with pytest.raises(
        manifest.ManifestError,
        match="promotion_binding_digest_mismatch",
    ):
        manifest.build_promotion_bundle(
            workspace_root=tmp_path,
            manifest_path=manifest_path,
            expected_digest=digest,
            binding_path=binding_path,
            output_root=output_root,
        )
    assert not output_root.exists()
