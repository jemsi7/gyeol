from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SERVER_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "measure_crypto_triage",
    SERVER_ROOT / "scripts" / "measure_crypto_triage.py",
)
measure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = measure
SPEC.loader.exec_module(measure)


def _complete_database_rows(fixture):
    rows = []
    for index, row in enumerate(fixture):
        on_topic = bool(row["on_topic"])
        rows.append(
            {
                "article_id": row["article_id"],
                "title": row["title"],
                "domain": row["domain"],
                "document_type": (
                    "governance_proposal"
                    if row["subtype"] == "document"
                    else None
                ),
                "document_source_name": row["source_name"],
                "triage_score": 100 - index if on_topic else index,
                "triage_model": "test/crypto-model",
                "triage_signals": {
                    "fallback": False,
                    "profile": "crypto",
                    "profile_version": measure.CRYPTO_TRIAGE_PROFILE_VERSION,
                    "prompt_version": measure.CRYPTO_TRIAGE_PROMPT_VERSION,
                    "relevance": "on_topic" if on_topic else "off_topic",
                    "reasons": (
                        ["ticker_title"]
                        if row["ticker_penalty_expected"]
                        else []
                    ),
                },
            }
        )
    return rows


def _pass_report(*, digest: str | None = None):
    fixture = measure.load_fixture()
    rows, mismatches = measure.merge_measurement_rows(
        fixture,
        _complete_database_rows(fixture),
        selected_article_ids=[
            row["article_id"] for row in fixture if row["subtype"] == "article"
        ],
        selected_document_ids=[
            row["article_id"] for row in fixture if row["subtype"] == "document"
        ],
    )
    return measure.build_report(
        rows,
        sample_digest=measure.fixture_digest(),
        mismatches=mismatches,
        content_manifest_sha256=digest,
    )


def test_fixed_fixture_is_exact_43_7_hash_ordered_and_digest_locked():
    fixture = measure.load_fixture()

    assert len(fixture) == 50
    assert sum(row["subtype"] == "article" for row in fixture) == 43
    assert sum(row["subtype"] == "document" for row in fixture) == 7
    assert measure.fixture_digest() == (
        "da93279c1d2b292a8d72381dc90b8f3b47f989e7fb23b006d78a9fc8082a952d"
    )


def test_perfect_provider_decisions_build_a_pass_receipt_without_source_text():
    fixture = measure.load_fixture()
    article_ids = [
        row["article_id"] for row in fixture if row["subtype"] == "article"
    ]
    document_ids = [
        row["article_id"] for row in fixture if row["subtype"] == "document"
    ]
    rows, mismatches = measure.merge_measurement_rows(
        fixture,
        _complete_database_rows(fixture),
        selected_article_ids=article_ids,
        selected_document_ids=document_ids,
    )

    report = measure.build_report(
        rows,
        sample_digest=measure.fixture_digest(),
        mismatches=mismatches,
    )

    assert mismatches == []
    assert report["gate"] == "pass"
    assert report["receipt_mode"] == "reference"
    assert "receipt_kind" not in report
    assert "ok" not in report
    assert report["model"] == "test/crypto-model"
    assert report["metrics"]["document_false_positives"] == 0
    encoded = str(report)
    assert fixture[0]["title"] not in encoded
    assert "postgresql://" not in encoded


def test_selection_drift_forces_an_inconclusive_receipt():
    fixture = measure.load_fixture()
    article_ids = [
        row["article_id"] for row in fixture if row["subtype"] == "article"
    ]
    document_ids = [
        row["article_id"] for row in fixture if row["subtype"] == "document"
    ]
    rows, mismatches = measure.merge_measurement_rows(
        fixture,
        _complete_database_rows(fixture),
        selected_article_ids=list(reversed(article_ids)),
        selected_document_ids=document_ids,
    )

    report = measure.build_report(
        rows,
        sample_digest=measure.fixture_digest(),
        mismatches=mismatches,
    )

    # DR-13243: the fixture defines the sample, so a re-evaluated selection
    # query no longer decides the verdict. Its window moves whenever the corpus
    # grows or retention runs, which reported drift for changes that never
    # touched the sample.
    assert mismatches == []
    assert report["gate"] == "pass"
    assert "article_selection_changed" not in report["metrics"]["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("manifest_path", "digest"),
    [
        (Path("manifest.json"), None),
        (None, "a" * 64),
    ],
)
async def test_manifest_pair_is_required_before_database_touch(
    monkeypatch, tmp_path, manifest_path, digest
):
    class ForbiddenDatabase:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("incomplete manifest pair must not touch DB")

    monkeypatch.setattr(measure, "Database", ForbiddenDatabase)
    with pytest.raises(
        measure.ContentManifestPreflightError,
        match="content_manifest_arguments_incomplete",
    ):
        await measure.measure(
            fixture_path=measure.CRYPTO_TRIAGE_FIXTURE,
            report_path=tmp_path / "report.json",
            manifest_path=manifest_path,
            content_manifest_sha256=digest,
        )


@pytest.mark.asyncio
async def test_manifest_mismatch_fails_before_database_touch(monkeypatch, tmp_path):
    class ManifestMismatch(ValueError):
        code = "manifest_digest_mismatch"

    def fail_verify(**_kwargs):
        raise ManifestMismatch("manifest_digest_mismatch")

    class ForbiddenDatabase:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("manifest mismatch must not touch DB")

    monkeypatch.setattr(measure, "_verify_content_manifest", fail_verify)
    monkeypatch.setattr(measure, "Database", ForbiddenDatabase)
    with pytest.raises(ManifestMismatch):
        await measure.measure(
            fixture_path=measure.CRYPTO_TRIAGE_FIXTURE,
            report_path=tmp_path / "report.json",
            manifest_path=tmp_path / "manifest.json",
            content_manifest_sha256="a" * 64,
        )


def test_verified_manifest_digest_is_embedded_in_t12_receipt(monkeypatch, tmp_path):
    digest = "a" * 64
    calls = []

    def verified(**kwargs):
        calls.append(kwargs)
        return digest

    monkeypatch.setattr(measure, "_verify_content_manifest", verified)
    assert measure.verify_requested_content_manifest(
        manifest_path=tmp_path / "manifest.json",
        content_manifest_sha256=digest,
    ) == digest
    assert calls == [
        {
            "workspace_root": measure.WORKSPACE_ROOT,
            "manifest_path": tmp_path / "manifest.json",
            "expected_digest": digest,
        }
    ]

    fixture = measure.load_fixture()
    article_ids = [
        row["article_id"] for row in fixture if row["subtype"] == "article"
    ]
    document_ids = [
        row["article_id"] for row in fixture if row["subtype"] == "document"
    ]
    rows, mismatches = measure.merge_measurement_rows(
        fixture,
        _complete_database_rows(fixture),
        selected_article_ids=article_ids,
        selected_document_ids=document_ids,
    )
    report = measure.build_report(
        rows,
        sample_digest=measure.fixture_digest(),
        mismatches=mismatches,
        content_manifest_sha256=digest,
    )
    assert report[measure.CONTENT_MANIFEST_FIELD] == digest


def test_t12_final_receipt_has_fixed_kind_and_requires_pass():
    digest = "a" * 64
    reference = _pass_report(digest=digest)

    receipt = measure.final_receipt(reference)

    assert receipt is not None
    assert receipt["receipt_kind"] == "t12"
    assert receipt["receipt_mode"] == "final"
    assert receipt["ok"] is True
    assert receipt[measure.CONTENT_MANIFEST_FIELD] == digest

    failed = {**reference, "gate": "fail"}
    assert measure.final_receipt(failed) is None
    assert measure.final_receipt(_pass_report()) is None


def test_t12_final_cli_requires_complete_manifest_pair_and_rejects_kind_override(
    tmp_path,
):
    parse = measure.parser()
    with pytest.raises(SystemExit) as missing_digest:
        parse.parse_args(
            [
                "final-receipt",
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--out",
                str(tmp_path / "final.json"),
            ]
        )
    assert missing_digest.value.code == 2

    with pytest.raises(SystemExit) as override:
        parse.parse_args(
            [
                "final-receipt",
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--content-manifest-sha256",
                "a" * 64,
                "--out",
                str(tmp_path / "final.json"),
                "--receipt-kind",
                "g1",
            ]
        )
    assert override.value.code == 2


def test_t12_final_cli_writes_final_only_for_pass_and_preserves_it_on_failure(
    monkeypatch, tmp_path
):
    digest = "a" * 64
    manifest = tmp_path / "manifest.json"
    reference_path = tmp_path / "reference.json"
    final_path = tmp_path / "final.json"
    passing = _pass_report(digest=digest)

    async def pass_measure(**kwargs):
        measure.write_atomic_report(kwargs["report_path"], passing)
        return passing

    monkeypatch.setattr(measure, "measure", pass_measure)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "measure_crypto_triage.py",
            "final-receipt",
            "--manifest",
            str(manifest),
            "--content-manifest-sha256",
            digest,
            "--reference-report",
            str(reference_path),
            "--out",
            str(final_path),
        ],
    )

    assert measure.main() == 0
    assert json.loads(reference_path.read_text())["receipt_mode"] == "reference"
    assert json.loads(final_path.read_text()) == measure.final_receipt(passing)

    sentinel = b'{"sentinel":true}\n'
    final_path.write_bytes(sentinel)
    failed = {**passing, "gate": "fail", "metrics": {**passing["metrics"], "status": "fail"}}

    async def fail_measure(**kwargs):
        measure.write_atomic_report(kwargs["report_path"], failed)
        return failed

    monkeypatch.setattr(measure, "measure", fail_measure)
    assert measure.main() == 3
    assert final_path.read_bytes() == sentinel
    assert json.loads(reference_path.read_text())["gate"] == "fail"
