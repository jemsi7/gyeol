"""Translated quotes kept in a verified pair (I-13201-b, DR-13207, DR-13260).

DR-13202 would have emptied the quote in translated lanes; DR-13207 cancelled
that and keeps the reading device, on the condition that the machine control
survives. So the exact-substring invariant moves off the display string and
onto ``quote_source``, and three deterministic gates stand between a source
span and its rendering. A model grading its own translation would turn the
control into a declaration, so none of the three asks a model anything.

DR-13260 then removed the last retyping surface: the writer no longer types
``quote_source`` at all — it names a claim-evidence address in
``quote_evidence_ref`` and the server fills the exact span, so the substring
invariant holds by construction and the validator keeps it as regression
defence only.
"""
from __future__ import annotations

import pytest

from app.editorial_pipeline import (
    _translated_scale_atoms,
    compile_scene_copies,
    quote_translation_errors,
    scene_output_is_translated,
    validate_scene_batch,
)
from app.map_output_schemas import scene_batch_output_schema


PLANNED = {
    "id": "S-01",
    "role": "evidence",
    "label": "label",
    "claim_refs": ["CL-001"],
    "image_ref": None,
    "chart_ref": None,
    "target_chars": 400,
}
SOURCE_SPAN = "Ethereum TVL fell to $48 billion, the SEC filing said"


LEDGER_EVIDENCE_REF = "CL-001/1"


def _ledger(evidence: str = SOURCE_SPAN) -> dict:
    return {
        "language": "ko",
        "claims": [
            {
                "id": "CL-001",
                "kind": "fact",
                "importance": "essential",
                "text": "480",
                "evidence": [{"paragraph_id": "P-001", "quote": evidence}],
            }
        ],
        "media": [],
        "visualizations": [],
    }


def _scene(**overrides) -> dict:
    scene = {
        "id": "S-01",
        "role": "evidence",
        "label": "label",
        "title": "제목",
        "summary": "",
        "body": "이더리움 TVL은 480억 달러로 내려갔다.",  # carries claim atom 480
        "secondary": "",
        "quote": "",
        "quote_source": "",
        "image_ref": None,
        "chart_ref": None,
        "claim_refs": ["CL-001"],
    }
    scene.update(overrides)
    return scene


def _errors(scene: dict, *, translated: bool = True) -> list[str]:
    return validate_scene_batch(
        [scene], [PLANNED], ledger=_ledger(), translated=translated
    )


def test_translation_is_decided_by_the_requested_language():
    assert scene_output_is_translated({"output_language": "ko"}) is True
    for value in ("auto", "AUTO", "", "  "):
        assert scene_output_is_translated({"output_language": value}) is False


def test_untranslated_output_mirrors_the_pair():
    compiled = compile_scene_copies(
        {"scenes": [{"scene_id": "S-01", "title": "t", "body": "b", "quote": "q"}]},
        [PLANNED],
        translated=False,
    )
    # Existing Korean-source lanes satisfy the source-span invariant by
    # construction, so their stored Maps are unchanged.
    assert compiled[0]["quote"] == "q"
    assert compiled[0]["quote_source"] == "q"


def test_translated_output_resolves_the_named_evidence_span():
    compiled = compile_scene_copies(
        {
            "scenes": [
                {
                    "scene_id": "S-01",
                    "title": "t",
                    "body": "b",
                    "quote": "번역",
                    "quote_evidence_ref": LEDGER_EVIDENCE_REF,
                }
            ]
        },
        [PLANNED],
        translated=True,
        ledger=_ledger(),
    )
    # The server, not the writer, owns the exact source span (DR-13260).
    assert compiled[0]["quote"] == "번역"
    assert compiled[0]["quote_source"] == SOURCE_SPAN
    assert compiled[0]["quote_evidence_ref"] == LEDGER_EVIDENCE_REF


def test_translated_output_ignores_a_retyped_source_span():
    compiled = compile_scene_copies(
        {
            "scenes": [
                {
                    "scene_id": "S-01",
                    "title": "t",
                    "body": "b",
                    "quote": "번역",
                    "quote_source": "Ethereum TVL rose to $99 billion",
                    "quote_evidence_ref": "",
                }
            ]
        },
        [PLANNED],
        translated=True,
        ledger=_ledger(),
    )
    # A model-typed span can no longer reach the invariant surface.
    assert compiled[0]["quote_source"] == ""


@pytest.mark.parametrize(
    "bad_ref",
    ["CL-999/1", "CL-001/2", "CL-001/0", "CL-001", "CL-001/x"],
)
def test_unresolvable_evidence_refs_resolve_to_empty(bad_ref):
    compiled = compile_scene_copies(
        {
            "scenes": [
                {
                    "scene_id": "S-01",
                    "title": "t",
                    "body": "b",
                    "quote": "번역",
                    "quote_evidence_ref": bad_ref,
                }
            ]
        },
        [PLANNED],
        translated=True,
        ledger=_ledger(),
    )
    assert compiled[0]["quote_source"] == ""


def test_translated_compilation_requires_the_ledger():
    with pytest.raises(ValueError, match="evidence ledger"):
        compile_scene_copies({"scenes": []}, [PLANNED], translated=True)


def test_schema_asks_for_the_evidence_address_only_when_translating():
    translated = scene_batch_output_schema(
        [PLANNED], translated=True, ledger=_ledger()
    )
    plain = scene_batch_output_schema([PLANNED], translated=False)
    scene_schema = translated.schema["properties"]["scenes"]["items"]
    # The writer chooses among server-enumerated addresses; it never types
    # the span itself (DR-13260).
    assert scene_schema["properties"]["quote_evidence_ref"]["enum"] == [
        "",
        LEDGER_EVIDENCE_REF,
    ]
    assert "quote_source" not in str(translated.schema)
    # The existing lanes keep a byte-identical schema.
    assert "quote_source" not in str(plain.schema)
    assert "quote_evidence_ref" not in str(plain.schema)


def test_translated_schema_requires_the_ledger():
    with pytest.raises(ValueError, match="evidence ledger"):
        scene_batch_output_schema([PLANNED], translated=True)


def test_repair_schema_keeps_the_evidence_address():
    repair = scene_batch_output_schema(
        [PLANNED], repair=True, translated=True, ledger=_ledger()
    )
    assert "quote_evidence_ref" in str(repair.schema)
    assert "quote_source" not in str(repair.schema)


def test_source_span_must_be_exact_claim_evidence():
    errors = _errors(
        _scene(quote="번역", quote_source="Ethereum TVL rose to $99 billion")
    )
    assert any("quote_source is not an exact claim evidence substring" in e
               for e in errors)


def test_exact_source_span_passes():
    scene = _scene(
        quote="이더리움 TVL이 480억 달러로 내려갔다고 SEC 제출서류가 밝혔다",
        quote_source=SOURCE_SPAN,
    )
    assert _errors(scene) == []


def test_quote_without_a_source_is_rejected():
    errors = _errors(_scene(quote="번역", quote_source=""))
    assert any(
        "quote requires quote_evidence_ref naming one assigned claim "
        "evidence span" in e
        for e in errors
    )


def test_untranslated_orphan_quote_keeps_its_original_error():
    errors = _errors(
        _scene(quote="원문 그대로", quote_source=""), translated=False
    )
    assert any("has no quote_source to verify against" in e for e in errors)


def test_both_empty_is_a_normal_scene():
    assert _errors(_scene()) == []


def test_quote_may_not_invent_numbers():
    assert quote_translation_errors("990억 달러였다고 SEC와 TVL이", SOURCE_SPAN) == [
        "quote introduces numeric atoms absent from quote_source: 990"
    ]


def test_korean_myriad_rendering_is_not_an_invention():
    # "$48 billion" is faithfully "480억 달러": the digits change because the
    # grouping changes, and rejecting that would fail every honest rendering.
    assert _translated_scale_atoms("fell to $48 billion") == {"480", "4800000"}
    assert (
        quote_translation_errors(
            "TVL이 480억 달러로 내려갔다고 SEC 제출서류가 밝혔다", SOURCE_SPAN
        )
        == []
    )


def test_rounding_is_still_an_invention():
    # 50 is not a regrouping of 48; only exact myriad splits are admitted.
    assert "50" not in _translated_scale_atoms("fell to $48 billion")


def test_quote_must_keep_source_acronyms():
    errors = quote_translation_errors("이더리움 TVL이 480억 달러로 내려갔다", SOURCE_SPAN)
    assert errors == ["quote drops source acronyms: SEC"]


def test_acronym_report_lists_every_dropped_token():
    errors = quote_translation_errors("아무 말도 없다", "SEC and CFTC agreed")
    assert errors == ["quote drops source acronyms: CFTC, SEC"]


def test_acronym_rule_ignores_ordinary_capitalised_words():
    # "Ethereum" is capitalised but not an acronym; requiring it verbatim would
    # fail every honest Korean rendering.
    assert quote_translation_errors("이더리움이 하락했다", "Ethereum declined") == []


def test_quote_may_not_outgrow_its_source():
    long_quote = "가" * 200
    errors = quote_translation_errors(long_quote, "short span")
    assert errors == ["quote is too long to be a translation of quote_source"]


def test_untranslated_lane_skips_the_translation_gates():
    # Mirrored pairs are identical, so the gates are vacuous there; running
    # them anyway would be harmless but the flag keeps the intent explicit.
    scene = _scene(quote=SOURCE_SPAN, quote_source=SOURCE_SPAN)
    assert _errors(scene, translated=False) == []


def test_delivered_map_never_carries_the_source_span():
    import inspect

    from app.editorial_pipeline import compile_delivery_map

    body = inspect.getsource(compile_delivery_map)
    assert '("quote_source", "quote_evidence_ref")' in body
