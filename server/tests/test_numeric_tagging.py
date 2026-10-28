"""Numeric atom semantic tagging (I-13203-a, C-13203).

The largest unknown in the extension. A wrong subject is not a mislabel, it is
wrong information a reader will act on, so every rule here is written to
produce fewer tags rather than more. Most of these tests assert that something
is *not* tagged.
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from app.numeric_tagging import (
    MAX_CANDIDATES,
    AtomCandidate,
    candidate_atoms,
    normalize_tagging_response,
    numeric_tagging_output_schema,
)


AS_OF = datetime(2026, 7, 14, tzinfo=UTC)


def _ledger(*quotes: str, claim_id: str = "CL-001") -> dict:
    return {
        "claims": [
            {
                "id": claim_id,
                "evidence": [{"paragraph_id": "P-001", "quote": quote}],
            }
            for quote in quotes
        ]
    }


def _atoms(quote: str) -> list[str]:
    return [item.atom for item in candidate_atoms(_ledger(quote))]


def _reply(*decisions: dict) -> dict:
    return {"tags": list(decisions)}


def _decision(**overrides) -> dict:
    decision = {
        "ref": "1",
        "tag": "tvl",
        "subject_kind": "chain",
        "subject_id": "ethereum",
        "confidence": 0.9,
    }
    decision.update(overrides)
    return decision


# --- deterministic candidate selection --------------------------------------

def test_only_numbers_with_an_adjacent_unit_are_asked_about():
    # The scale word belongs to 48; 3 is a count that happens to sit near it.
    assert _atoms("TVL fell to $48 billion while 3 people watched") == ["48"]


def test_percentages_are_never_candidates():
    # A percentage is a change, and there is no current value to compare it to.
    assert _atoms("TVL rose 18.5% this week") == []
    assert _atoms("유통량이 12퍼센트 늘었다") == []


def test_bare_counts_and_identifiers_are_not_candidates():
    assert _atoms("The proposal received 412 votes in block 21000000") == []
    assert _atoms("2024년부터 3개 프로토콜이 참여했다") == []


def test_korean_units_are_recognised():
    assert _atoms("이더리움 TVL은 480억 달러로 내려갔다") == ["480"]


def test_metric_word_before_the_number_qualifies_it():
    assert _atoms("hashrate reached 700 EH/s") == ["700"]
    assert _atoms("시총 1200억 원을 기록했다") == ["1200"]


def test_candidates_are_deterministic():
    ledger = _ledger("TVL fell to $48 billion and fees hit 12 gwei")
    first = candidate_atoms(ledger)
    for _ in range(50):
        assert candidate_atoms(ledger) == first


def test_duplicate_atoms_within_a_claim_are_asked_once():
    ledger = _ledger("$48 billion today, $48 billion yesterday")
    assert [item.atom for item in candidate_atoms(ledger)] == ["48"]


def test_candidate_count_is_capped():
    quote = " ".join(f"${index} billion" for index in range(1, MAX_CANDIDATES + 20))
    assert len(candidate_atoms(_ledger(quote))) == MAX_CANDIDATES


def test_no_candidates_means_no_schema_is_built():
    assert candidate_atoms(_ledger("no numbers here")) == ()
    with pytest.raises(ValueError):
        numeric_tagging_output_schema(())


def test_malformed_ledger_entries_are_skipped():
    ledger = {"claims": ["nope", {"evidence": [{"quote": "$1 billion"}]}, {"id": ""}]}
    # The second claim has no id, so nothing can be attributed to it.
    assert candidate_atoms(ledger) == ()


# --- response normalisation --------------------------------------------------

def _normalize(reply, quote="TVL fell to $48 billion", **kwargs):
    candidates = candidate_atoms(_ledger(quote))
    return normalize_tagging_response(reply, candidates, as_of=AS_OF, **kwargs)


def test_a_confident_decision_is_kept():
    tags = _normalize(_reply(_decision()))
    assert [item.to_dict()["subject_id"] for item in tags] == ["ethereum"]
    assert tags[0].as_of == AS_OF


def test_low_confidence_is_dropped():
    assert _normalize(_reply(_decision(confidence=0.5))) == ()


def test_confidence_threshold_is_configurable():
    assert _normalize(_reply(_decision(confidence=0.5)), min_confidence=0.4) != ()


@pytest.mark.parametrize(
    "override",
    [
        {"ref": "9"},
        {"tag": "vibes"},
        {"subject_kind": "person"},
        {"subject_id": ""},
        {"subject_id": "has spaces"},
        {"confidence": "high"},
        {"confidence": True},
        {"confidence": 1.5},
    ],
)
def test_decisions_outside_the_contract_are_dropped(override):
    assert _normalize(_reply(_decision(**override))) == ()


def test_two_readings_of_the_same_number_drop_both():
    # Ambiguity is the failure mode this unit exists to avoid; resolving it by
    # picking one would be guessing.
    reply = _reply(
        _decision(),
        _decision(subject_id="aave-v3", subject_kind="protocol"),
    )
    assert _normalize(reply) == ()


def test_a_repeated_identical_decision_is_not_a_conflict():
    assert len(_normalize(_reply(_decision(), _decision()))) == 1


def test_a_malformed_reply_yields_no_tags():
    for reply in (None, {}, {"tags": "nope"}, {"tags": [None]}, []):
        assert _normalize(reply) == ()


def test_normalisation_without_candidates_is_empty():
    assert normalize_tagging_response(_reply(_decision()), (), as_of=AS_OF) == ()


# --- pipeline integration ----------------------------------------------------

def test_pipeline_stage_fails_open_and_costs_at_most_one_call():
    from app.editorial_pipeline import EditorialPipeline

    body = inspect.getsource(EditorialPipeline._tag_numeric_atoms)
    # Exactly one provider call, and nothing that could repeat it.
    assert body.count("complete_json(") == 1
    assert "while " not in body
    assert "retry" not in body.lower().replace("retryable", "")
    # Any failure returns [] rather than raising into the Map pipeline.
    assert "except Exception" in body
    assert body.count("return []") == 2


def test_delivery_map_and_projection_carry_the_tags():
    from app.editorial_pipeline import compile_delivery_map
    from app.map_loader import resolve_map

    assert '"tagged_atoms": list(tagged_atoms)' in inspect.getsource(
        compile_delivery_map
    )
    resolved = resolve_map(
        {"scenes": [], "tagged_atoms": [{"atom": "48"}]},
        lambda image_id: "",
    )
    assert resolved["tagged_atoms"] == [{"atom": "48"}]
    # Absent on every Map written before this unit.
    assert resolve_map({"scenes": []}, lambda image_id: "")["tagged_atoms"] == []


def test_schema_addresses_candidates_by_batch_local_ref():
    candidates = (
        AtomCandidate(claim_id="CL-001", atom="48", context="ctx"),
        AtomCandidate(claim_id="CL-002", atom="99", context="ctx"),
    )
    schema = numeric_tagging_output_schema(candidates)
    rendered = str(schema.schema)
    # Claim IDs never travel to the model; refs do.
    assert "CL-001" not in rendered
    assert "'1', '2'" in rendered or '"1", "2"' in rendered
