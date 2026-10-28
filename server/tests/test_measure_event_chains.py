"""Pair scoring and gate arithmetic for the chain measurement (I-13209-b, G3).

The measurement decides whether F-4 keeps going, so the arithmetic that
produces the verdict is itself tested. The database and the vector index are
not: this file covers the counting and the refusals, not the retrieval.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "measure_event_chains",
    SERVER_ROOT / "scripts" / "measure_event_chains.py",
)
measure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = measure
SPEC.loader.exec_module(measure)

DAY_ZERO = datetime(2026, 7, 1, tzinfo=UTC)
SHA = "a" * 64


def _fixture_row(index: int, article_id: str, **overrides) -> dict:
    row = {
        "sample_id": f"e1-a{index:02d}",
        "article_id": article_id,
        "event_id": "bridge-exploit-2026-07",
        "event_role": "followup",
        "title_sha256": SHA,
        "published_at": "2026-07-30T04:12:00Z",
    }
    row.update(overrides)
    return row


def _write_fixture(tmp_path: Path, rows) -> Path:
    path = tmp_path / "events_v1.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _article(article_id: str, *, day: int, entities, title: str = "") -> dict:
    # Titles default to empty so the entity sets under test are exactly the
    # tagged subjects; a shared generic word in a headline would otherwise
    # count toward the overlap the join gate depends on.
    return {
        "article_id": article_id,
        "title": title,
        "tagged_atoms": [{"subject_id": entity} for entity in entities],
        "published_at": DAY_ZERO + timedelta(days=day),
        "source_hash": "0" * 64,
        "domain": "example.com",
    }


def _edge(source: str, target: str, similarity: float = 0.95) -> dict:
    return {"source": source, "target": target, "similarity": similarity}


def _passing_inputs():
    """Twenty labelled events of two articles, each chained exactly right."""
    labels = {}
    chains = {}
    sample_ids = {}
    for index in range(20):
        first, second = f"a{index:02d}-1", f"a{index:02d}-2"
        labels[first] = labels[second] = f"event-{index:02d}"
        chains[f"c{index:02d}"] = (first, second)
        sample_ids[first] = f"e1-a{index:02d}a"
        sample_ids[second] = f"e1-a{index:02d}b"
    return labels, chains, sample_ids


def _passing_report(**overrides) -> dict:
    labels, chains, sample_ids = _passing_inputs()
    assignment = {
        member: chain for chain, members in chains.items() for member in members
    }
    simulation = measure.ChainSimulation(
        chains=chains,
        assignment=assignment,
        joined=20,
        opened=20,
        considered=len(labels),
    )
    payload = {
        "simulation": simulation,
        "tally": measure.score_pairs(chains, labels),
        "labels": labels,
        "sample_ids": sample_ids,
        "sample_digest": "b" * 64,
        "missing_embeddings": 0,
        "content_manifest_sha256": "c" * 64,
        "measured_at": DAY_ZERO,
    }
    payload.update(overrides)
    return measure.build_report(**payload)


# --- fixture contract ------------------------------------------------------


def test_fixture_requires_sorted_unique_ids(tmp_path):
    path = _write_fixture(
        tmp_path,
        [_fixture_row(1, "b" * 64), _fixture_row(2, "a" * 64)],
    )
    with pytest.raises(ValueError, match="fixture_article_ids_not_sorted"):
        measure.load_fixture(path)


def test_fixture_rejects_duplicate_sample_ids(tmp_path):
    path = _write_fixture(
        tmp_path,
        [
            _fixture_row(1, "a" * 64),
            _fixture_row(1, "b" * 64),
        ],
    )
    with pytest.raises(ValueError, match="fixture_sample_id_not_unique"):
        measure.load_fixture(path)


def test_fixture_rejects_malformed_row(tmp_path):
    path = _write_fixture(tmp_path, [_fixture_row(1, "a" * 64, title_sha256="a" * 63)])
    with pytest.raises(ValueError, match="fixture_title_sha256_invalid"):
        measure.load_fixture(path)


def test_fixture_rejects_unknown_event_role(tmp_path):
    path = _write_fixture(tmp_path, [_fixture_row(1, "a" * 64, event_role="maybe")])
    with pytest.raises(ValueError, match="fixture_event_role_unknown"):
        measure.load_fixture(path)


def test_fixture_loads_and_counts_only_events_with_a_pair(tmp_path):
    rows = [
        _fixture_row(1, "a" * 64, event_id="one"),
        _fixture_row(2, "b" * 64, event_id="one"),
        _fixture_row(3, "c" * 64, event_id="lonely"),
        _fixture_row(4, "d" * 64, event_id=None, event_role="unrelated_near_miss"),
    ]
    loaded = measure.load_fixture(_write_fixture(tmp_path, rows))
    assert len(loaded) == 4
    # A single-article event yields no pair, so it is not a labelled event.
    assert measure.labelled_event_count(measure.labels_of(loaded)) == 1


# --- pair scoring ----------------------------------------------------------


def test_perfect_chains_score_one():
    labels = {"a1": "e1", "a2": "e1", "a3": "e1"}
    tally = measure.score_pairs({"c1": ("a1", "a2", "a3")}, labels)
    assert (tally.true_positive, tally.false_positive, tally.missed) == (3, 0, 0)
    assert tally.precision == 1.0
    assert tally.recall == 1.0


def test_false_join_lowers_precision():
    labels = {"a1": "e1", "a2": "e1", "b1": "e2", "b2": "e2"}
    tally = measure.score_pairs({"c1": ("a1", "a2", "b1")}, labels)
    assert tally.true_positive == 1
    assert tally.false_positive == 2
    assert tally.precision == pytest.approx(1 / 3)
    assert tally.false_join_pairs == (("a1", "b1"), ("a2", "b1"))


def test_unlabelled_article_join_is_false_positive():
    labels = {"a1": "e1", "a2": "e1", "x1": None}
    tally = measure.score_pairs({"c1": ("a1", "a2", "x1")}, labels)
    # An article that belongs to no event is exactly what must not be pulled in.
    assert tally.false_positive == 2
    assert tally.precision == pytest.approx(1 / 3)


def test_missed_pair_lowers_recall_only():
    labels = {"a1": "e1", "a2": "e1", "a3": "e1"}
    tally = measure.score_pairs({"c1": ("a1", "a2")}, labels)
    assert tally.precision == 1.0
    assert tally.recall == pytest.approx(1 / 3)
    assert tally.missed == 2


# --- simulation ------------------------------------------------------------


def test_same_incident_reports_form_one_chain():
    articles = [
        _article("a1", day=0, entities=["RONIN", "AXIE"]),
        _article("a2", day=1, entities=["RONIN", "AXIE"]),
        _article("a3", day=2, entities=["RONIN", "AXIE"]),
    ]
    edges = [_edge("a1", "a2"), _edge("a1", "a3"), _edge("a2", "a3")]
    simulation = measure.simulate_chains(articles, edges)
    assert len(simulation.chains) == 1
    assert set(next(iter(simulation.chains.values()))) == {"a1", "a2", "a3"}
    assert simulation.unchained == 0


def test_entity_gate_blocks_join_in_simulation():
    articles = [
        _article("a1", day=0, entities=["RONIN", "AXIE"]),
        _article("a2", day=1, entities=["RONIN", "WORMHOLE"]),
    ]
    simulation = measure.simulate_chains(articles, [_edge("a1", "a2", 0.95)])
    # High similarity with one shared name is the "another bridge exploit" case.
    assert simulation.chains == {}
    assert simulation.unchained == 2


def test_span_limit_excludes_old_neighbour():
    articles = [
        _article("a1", day=0, entities=["RONIN", "AXIE"]),
        _article("a2", day=60, entities=["RONIN", "AXIE"]),
    ]
    simulation = measure.simulate_chains(articles, [_edge("a1", "a2", 0.99)])
    assert simulation.chains == {}


def test_article_never_lands_in_two_chains():
    articles = [
        _article("a1", day=0, entities=["RONIN", "AXIE"]),
        _article("a2", day=1, entities=["RONIN", "AXIE"]),
        _article("b1", day=2, entities=["WORMHOLE", "SOL"]),
        _article("b2", day=3, entities=["WORMHOLE", "SOL"]),
        _article("c1", day=4, entities=["RONIN", "AXIE", "WORMHOLE", "SOL"]),
    ]
    edges = [
        _edge("a1", "a2"),
        _edge("b1", "b2"),
        _edge("c1", "a1"),
        _edge("c1", "b1"),
    ]
    simulation = measure.simulate_chains(articles, edges)
    assert len(simulation.chains) == 2
    # Two chains both look right, so the bridging article joins neither.
    assert "c1" not in simulation.assignment
    seen = [member for members in simulation.chains.values() for member in members]
    assert len(seen) == len(set(seen))


def test_syndicated_reprints_do_not_become_a_measured_chain():
    # The real staging case: five outlets republished one headline, and those
    # ten pairs were the only ones that reached the entity gate. The harness
    # calls plan_assignments, so it inherits the dedupe rule (DR-13233).
    headline = "Diese Entwicklung in Japan könnte Bitcoin belasten"
    articles = [
        _article(f"copy{index}", day=index, entities=["RONIN", "AXIE"], title=headline)
        for index in range(5)
    ]
    edges = [
        _edge(left["article_id"], right["article_id"], 0.99)
        for index, left in enumerate(articles)
        for right in articles[index + 1 :]
    ]
    simulation = measure.simulate_chains(articles, edges)
    assert simulation.chains == {}
    assert simulation.unchained == 5


def test_simulation_is_order_stable():
    articles = [
        _article("a1", day=0, entities=["RONIN", "AXIE"]),
        _article("a2", day=1, entities=["RONIN", "AXIE"]),
        _article("a3", day=2, entities=["RONIN", "AXIE"]),
    ]
    edges = [_edge("a1", "a2"), _edge("a2", "a3"), _edge("a1", "a3")]
    first = measure.simulate_chains(articles, edges)
    second = measure.simulate_chains(list(reversed(articles)), edges)
    assert first.chains == second.chains


# --- verdict ---------------------------------------------------------------


def _verdict(tally, **overrides):
    payload = {
        "labelled_events": 20,
        "missing_ratio": 0.0,
        "mismatches": (),
        "parameters_are_defaults": True,
    }
    payload.update(overrides)
    return measure.verdict(tally, **payload)


def test_gate_passes_at_threshold():
    tally = measure.PairTally(true_positive=9, false_positive=1)
    assert tally.precision == pytest.approx(0.90)
    assert _verdict(tally)["gate"] == "pass"


def test_gate_retunes_below_threshold_with_defaults():
    tally = measure.PairTally(true_positive=8, false_positive=2)
    assert _verdict(tally)["gate"] == "retune_and_remeasure"


def test_gate_stops_after_retune():
    tally = measure.PairTally(true_positive=8, false_positive=2)
    verdict = _verdict(tally, parameters_are_defaults=False)
    assert verdict["gate"] == "stop_and_ask"


def test_few_events_is_inconclusive():
    tally = measure.PairTally(true_positive=9, false_positive=1)
    verdict = _verdict(tally, labelled_events=19)
    assert verdict["gate"] == "inconclusive"
    assert "labelled_events_below_minimum" in verdict["reasons"]


def test_zero_predicted_pairs_is_inconclusive():
    verdict = _verdict(measure.PairTally(missed=20))
    # Chaining nothing is trivially precise and proves nothing.
    assert verdict["gate"] == "inconclusive"
    assert "no_predicted_joins" in verdict["reasons"]


def test_missing_embeddings_is_inconclusive():
    tally = measure.PairTally(true_positive=9, false_positive=1)
    verdict = _verdict(tally, missing_ratio=0.2)
    assert verdict["gate"] == "inconclusive"
    assert "embedding_coverage_below_bound" in verdict["reasons"]


# --- sample guard and report ----------------------------------------------


def test_metadata_mismatch_is_inconclusive():
    fixture = [
        _fixture_row(
            1,
            "a" * 64,
            title_sha256=(
                "3973e022e93220f9212c18d0d0c543ae7c309e46640da93a4a0314de"
                "999f5112"
            ),
        )
    ]
    rows = [
        {
            "article_id": "a" * 64,
            "title": "a different title",
            "domain": "example.com",
            "coverage_domain": "crypto",
            "map_status": "held_ok",
            "published_at": DAY_ZERO,
            "source_hash": "0" * 64,
            "tagged_atoms": [],
        }
    ]
    articles, mismatches, missing = measure.merge_sample_rows(fixture, rows)
    assert mismatches == ["sample_metadata_changed"]
    assert len(articles) == 1 and missing == 0
    report = _passing_report(mismatches=mismatches)
    assert report["gate"] == "inconclusive"
    assert "sample_metadata_changed" in report["reasons"]


def test_sample_outside_chainable_population_is_rejected():
    fixture = [_fixture_row(1, "a" * 64)]
    rows = [
        {
            "article_id": "a" * 64,
            "title": "",
            "domain": "example.com",
            "coverage_domain": "crypto",
            "map_status": "failed",
            "published_at": DAY_ZERO,
            "source_hash": "0" * 64,
            "tagged_atoms": [],
        }
    ]
    fixture[0]["title_sha256"] = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    _, mismatches, _ = measure.merge_sample_rows(fixture, rows)
    assert "sample_outside_chainable_population" in mismatches


def test_missing_source_hash_counts_as_missing_embedding():
    fixture = [
        _fixture_row(
            1,
            "a" * 64,
            title_sha256=(
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7"
                "852b855"
            ),
        )
    ]
    rows = [
        {
            "article_id": "a" * 64,
            "title": "",
            "domain": "example.com",
            "coverage_domain": "crypto",
            "map_status": "ok",
            "published_at": DAY_ZERO,
            "source_hash": None,
            "tagged_atoms": [],
        }
    ]
    _, mismatches, missing = measure.merge_sample_rows(fixture, rows)
    assert mismatches == []
    assert missing == 1


def test_report_names_false_joins_by_opaque_sample_id():
    labels = {"a1": "e1", "a2": "e1", "b1": "e2", "b2": "e2"}
    chains = {"c1": ("a1", "a2", "b1")}
    simulation = measure.ChainSimulation(
        chains=chains,
        assignment={member: "c1" for member in chains["c1"]},
        joined=1,
        opened=1,
        considered=4,
    )
    report = _passing_report(
        simulation=simulation,
        tally=measure.score_pairs(chains, labels),
        labels=labels,
        sample_ids={"a1": "e1-a01", "a2": "e1-a02", "b1": "e1-a03", "b2": "e1-a04"},
    )
    assert report["false_join_samples"] == [["e1-a01", "e1-a03"], ["e1-a02", "e1-a03"]]


def test_report_contains_no_source_text():
    articles = [
        _article(
            "a1",
            day=0,
            entities=["RONIN"],
            title="Ronin bridge drained in https://example.com/exploit report",
        ),
        _article("a2", day=1, entities=["RONIN"], title="Follow-up on the bridge"),
    ]
    simulation = measure.simulate_chains(articles, [_edge("a1", "a2")])
    report = _passing_report(simulation=simulation)
    serialized = json.dumps(report, ensure_ascii=False)
    assert "Ronin" not in serialized
    assert "http" not in serialized
    assert "bridge" not in serialized


def test_report_records_the_parameters_it_measured():
    report = _passing_report()
    assert report["parameters"] == measure.DEFAULT_PARAMETERS
    assert report["parameters_are_defaults"] is True
    assert report["gate"] == "pass"
    assert report["metrics"]["precision"] == 1.0
    assert report["sample"]["labelled_events"] == 20


# --- final receipt ---------------------------------------------------------


def test_final_receipt_requires_strict_pass():
    assert measure.final_receipt(_passing_report(content_manifest_sha256=None)) is None
    assert measure.final_receipt(_passing_report(missing_embeddings=40)) is None
    inconclusive = _passing_report(mismatches=["sample_metadata_changed"])
    assert measure.final_receipt(inconclusive) is None


def test_manifest_arguments_must_come_in_pairs():
    with pytest.raises(measure.ContentManifestPreflightError) as error:
        measure.verify_requested_content_manifest(
            manifest_path=None, content_manifest_sha256="c" * 64
        )
    assert error.value.code == "content_manifest_arguments_incomplete"


def test_missing_database_url_is_a_coded_refusal(monkeypatch, tmp_path):
    import asyncio

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(measure.MeasurementError) as error:
        asyncio.run(
            measure.measure(
                fixture_path=tmp_path / "events_v1.jsonl",
                report_path=tmp_path / "report.json",
            )
        )
    # The refusal lands before the fixture is even read, so nothing is written.
    assert error.value.code == "DATABASE_URL_required"
    assert not (tmp_path / "report.json").exists()


def test_final_receipt_shape():
    receipt = measure.final_receipt(_passing_report())
    assert receipt is not None
    assert receipt["receipt_kind"] == "g3"
    assert receipt["receipt_mode"] == "final"
    assert receipt["ok"] is True
    assert receipt["content_manifest_sha256"] == "c" * 64
    assert receipt["database"] == measure.STAGING_DATABASE
