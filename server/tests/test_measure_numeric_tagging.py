"""Scoring and gate arithmetic for the tagging measurement (I-13203-b, G1).

The measurement decides whether F-1 and F-4 are built, so the arithmetic that
produces the verdict is itself tested. The provider call is not: this file
covers the counting, not the labelling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "measure_numeric_tagging",
    REPO_ROOT / "scripts" / "measure_numeric_tagging.py",
)
measure = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_numeric_tagging"] = measure
_SPEC.loader.exec_module(measure)


def _tagged(atom: str, subject_id: str = "ethereum", confidence: float = 0.9) -> dict:
    return {
        "atom": atom,
        "tag": "tvl",
        "subject_kind": "chain",
        "subject_id": subject_id,
        "confidence": confidence,
    }


def _expected(atom: str, subject_id: str = "ethereum") -> dict:
    return {
        "atom": atom,
        "tag": "tvl",
        "subject_kind": "chain",
        "subject_id": subject_id,
    }


def test_each_outcome_lands_in_its_own_bucket():
    tally = measure.Tally()
    measure.score_article(
        predicted=[
            _tagged("480"),                       # correct
            _tagged("99", subject_id="aave-v3"),  # right atom list, wrong subject
            _tagged("3"),                         # should not have been tagged
        ],
        expected=[_expected("480"), _expected("99"), _expected("7")],
        expected_untagged=["3", "2024"],
        tally=tally,
    )
    assert tally.correct == 1
    assert tally.wrong_subject == 1
    assert tally.spurious == 1
    assert tally.missed == 1              # 7 was expected and never offered
    assert tally.correctly_untagged == 1  # 2024 was left alone


def test_subject_error_rate_counts_only_what_was_tagged():
    tally = measure.Tally(correct=8, wrong_subject=1, spurious=1, missed=5)
    # A miss is a coverage problem, not a wrong statement to a reader, so it
    # does not enter the rate that gates the feature.
    assert tally.tagged == 10
    assert tally.subject_error_rate == pytest.approx(0.2)


def test_recall_counts_what_should_have_been_tagged():
    tally = measure.Tally(correct=8, wrong_subject=1, missed=1)
    assert tally.recall == pytest.approx(0.8)


def test_rates_are_none_when_nothing_was_tagged():
    tally = measure.Tally(correctly_untagged=12)
    assert tally.subject_error_rate is None
    assert tally.recall is None


def test_confidence_buckets_are_stable():
    tally = measure.Tally()
    measure.score_article(
        predicted=[_tagged("480", confidence=0.72), _tagged("99", confidence=1.0)],
        expected=[_expected("480"), _expected("99")],
        expected_untagged=[],
        tally=tally,
    )
    assert tally.confidence_buckets == {"0.7-0.8": 1, "0.9-1.0": 1}


@pytest.mark.parametrize(
    ("correct", "wrong", "expected_gate"),
    [
        (100, 4, "pass"),                  # 0.038
        (100, 6, "narrow_and_remeasure"),  # 0.057
        (100, 30, "stop_and_ask"),         # 0.231
    ],
)
def test_gate_thresholds(correct, wrong, expected_gate):
    tally = measure.Tally(correct=correct, wrong_subject=wrong)
    result = measure.verdict(tally, samples=50, failures=0)
    assert result["gate"] == expected_gate
    assert result["inconclusive"] is False


def test_a_small_sample_is_inconclusive_whatever_the_rate():
    tally = measure.Tally(correct=100)
    result = measure.verdict(tally, samples=measure.MIN_SAMPLE - 1, failures=0)
    assert result == {"inconclusive": True, "gate": "inconclusive"}


def test_too_many_failed_articles_is_inconclusive():
    tally = measure.Tally(correct=100)
    result = measure.verdict(tally, samples=50, failures=6)
    assert result["gate"] == "inconclusive"


def test_no_tags_at_all_is_inconclusive_not_a_pass():
    # Zero tagged means zero errors, which must never read as a clean pass.
    tally = measure.Tally(correctly_untagged=50)
    assert measure.verdict(tally, samples=50, failures=0)["gate"] == "inconclusive"


def test_thresholds_match_the_build_order():
    order = (REPO_ROOT / "docs/maps/07_crypto_build_order.md").read_text(
        encoding="utf-8"
    )
    assert "subject_error_rate <= 0.05" in order
    assert measure.PASS_ERROR_RATE == 0.05
    assert measure.NARROW_ERROR_RATE == 0.15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_args",
    [
        ["--manifest", "manifest.json"],
        ["--content-manifest-sha256", "a" * 64],
    ],
)
async def test_manifest_pair_is_required_before_provider_construction(
    monkeypatch, tmp_path, manifest_args
):
    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("incomplete manifest pair must not construct provider")

    monkeypatch.setattr(measure, "OpenRouterClient", ForbiddenClient)
    args = measure.parser().parse_args(
        ["--fixtures", str(tmp_path), *manifest_args]
    )
    with pytest.raises(
        measure.ContentManifestPreflightError,
        match="content_manifest_arguments_incomplete",
    ):
        await measure.run(args)


@pytest.mark.asyncio
async def test_manifest_mismatch_fails_before_provider_construction(
    monkeypatch, tmp_path
):
    class ManifestMismatch(ValueError):
        code = "manifest_digest_mismatch"

    def fail_verify(**_kwargs):
        raise ManifestMismatch("manifest_digest_mismatch")

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("manifest mismatch must not construct provider")

    monkeypatch.setattr(measure, "_verify_content_manifest", fail_verify)
    monkeypatch.setattr(measure, "OpenRouterClient", ForbiddenClient)
    args = measure.parser().parse_args(
        [
            "--fixtures",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--content-manifest-sha256",
            "a" * 64,
        ]
    )
    with pytest.raises(ManifestMismatch):
        await measure.run(args)


@pytest.mark.asyncio
async def test_verified_manifest_digest_is_embedded_in_g1_receipt(
    monkeypatch, tmp_path
):
    digest = "a" * 64
    calls = []

    def verified(**kwargs):
        calls.append(kwargs)
        return digest

    class EmptyClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(measure, "_verify_content_manifest", verified)
    monkeypatch.setattr(measure, "OpenRouterClient", EmptyClient)
    manifest_path = tmp_path / "manifest.json"
    args = measure.parser().parse_args(
        [
            "--fixtures",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--content-manifest-sha256",
            digest,
        ]
    )

    report = await measure.run(args)

    assert report[measure.CONTENT_MANIFEST_FIELD] == digest
    assert report["receipt_mode"] == "reference"
    assert report["failed_count"] == 0
    assert report["tagged_count"] == 0
    assert calls == [
        {
            "workspace_root": measure.REPO_ROOT,
            "manifest_path": manifest_path,
            "expected_digest": digest,
        }
    ]


def _passing_reference(digest: str = "a" * 64) -> dict:
    return {
        "receipt_mode": "reference",
        "samples": 50,
        "failed": [],
        "failed_count": 0,
        "tagged_count": 100,
        "min_confidence": 0.7,
        "model": "test/model",
        "subject_error_rate": 0.05,
        "inconclusive": False,
        "gate": "pass",
        measure.CONTENT_MANIFEST_FIELD: digest,
    }


def test_g1_final_receipt_has_fixed_kind_and_requires_pass():
    reference = _passing_reference()

    receipt = measure.final_receipt(reference)

    assert receipt == {
        "receipt_kind": "g1",
        "receipt_mode": "final",
        "ok": True,
        measure.CONTENT_MANIFEST_FIELD: "a" * 64,
        "gate": "pass",
        "samples": 50,
        "failed_count": 0,
        "max_failure_ratio": 0.1,
        "tagged_count": 100,
        "subject_error_rate": 0.05,
        "min_confidence": 0.7,
        "model": "test/model",
    }
    assert measure.final_receipt({**reference, "samples": 49}) is None
    assert measure.final_receipt({**reference, "failed_count": 6}) is None
    assert measure.final_receipt({**reference, "tagged_count": 0}) is None
    assert measure.final_receipt({**reference, "gate": "narrow_and_remeasure"}) is None


def test_g1_final_cli_requires_manifest_pair_and_rejects_kind_override(tmp_path):
    parse = measure.parser()
    with pytest.raises(SystemExit) as missing_digest:
        parse.parse_args(
            [
                "final-receipt",
                "--fixtures",
                str(tmp_path),
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
                "--fixtures",
                str(tmp_path),
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--content-manifest-sha256",
                "a" * 64,
                "--out",
                str(tmp_path / "final.json"),
                "--receipt-kind",
                "t12",
            ]
        )
    assert override.value.code == 2


def test_g1_final_cli_writes_final_only_for_pass_and_preserves_it_on_failure(
    monkeypatch, tmp_path
):
    digest = "a" * 64
    reference_path = tmp_path / "reference.json"
    final_path = tmp_path / "final.json"
    passing = _passing_reference(digest)

    async def pass_run(_args):
        return passing

    monkeypatch.setattr(measure, "run", pass_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "measure_numeric_tagging.py",
            "final-receipt",
            "--fixtures",
            str(tmp_path),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--content-manifest-sha256",
            digest,
            "--reference-out",
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
    failing = {**passing, "gate": "inconclusive", "inconclusive": True}

    async def fail_run(_args):
        return failing

    monkeypatch.setattr(measure, "run", fail_run)
    assert measure.main() == 3
    assert final_path.read_bytes() == sentinel
    assert json.loads(reference_path.read_text())["gate"] == "inconclusive"
