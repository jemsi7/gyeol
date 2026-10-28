"""The then-versus-now annotation (I-13205-a, PP-1).

The product is built for reading later, which in crypto turns a saved article
into a stale one. This closes that hazard -- and the way it closes it is by
staying quiet: nearly every test here asserts that no note appears.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.market_data import MarketValue
from app.staleness import (
    CHANGE_THRESHOLDS,
    MAX_NOTES_PER_ARTICLE,
    MIN_ARTICLE_AGE,
    build_staleness_notes,
    format_like,
    parse_scaled_number,
)


NOW = datetime(2026, 8, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=3)


def _atom(**overrides) -> dict:
    atom = {
        "claim_id": "CL-001",
        "atom": "480억 달러",
        "tag": "tvl",
        "subject_kind": "chain",
        "subject_id": "ethereum",
        "as_of": OLD.isoformat(),
    }
    atom.update(overrides)
    return atom


def _value(value: float, attribution: str = "DefiLlama") -> MarketValue:
    return MarketValue(
        value=value,
        unit="USD",
        observed_at=NOW,
        provider="defillama",
        attribution=attribution,
    )


def _notes(atoms, values, *, now=NOW):
    return build_staleness_notes(tagged_atoms=atoms, values=values, now=now)


ETH_TVL = ("tvl", "chain", "ethereum")


# --- parsing quantities, not digits ------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("480억 달러", 4.8e10),
        ("$48 billion", 4.8e10),
        ("48 trillion", 4.8e13),
        ("1,200", 1200.0),
        ("19,700,000", 19_700_000.0),
        ("1.5 million", 1_500_000.0),
    ],
)
def test_scaled_figures_resolve_to_a_quantity(text, expected):
    # "480억" and "$48 billion" are the same amount written two ways.
    assert parse_scaled_number(text)[0] == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "no digits", "0", "-5"])
def test_unusable_figures_have_no_quantity(text):
    assert parse_scaled_number(text) is None


def test_the_current_value_keeps_the_articles_units():
    assert format_like(3.91e10, "480억 달러") == "391억 달러"
    assert format_like(3.91e10, "$48 billion") == "$39.1 billion"
    assert format_like(1234.0, "1,200") == "1,234"


def test_formatting_preserves_currency_prefixes():
    # A bare number next to a priced one is not a comparison a reader can make
    # at a glance.
    assert format_like(3.91e10, "₩48 billion").startswith("₩")


# --- when a note appears -----------------------------------------------------

def test_a_material_change_on_an_old_article_is_annotated():
    notes = _notes([_atom()], {ETH_TVL: _value(3.91e10)})

    assert len(notes) == 1
    assert notes[0].sentence() == (
        "기사 시점 480억 달러 · 지금 391억 달러 · 18.5% 감소 · DefiLlama"
    )


def test_an_increase_reads_as_an_increase():
    notes = _notes([_atom()], {ETH_TVL: _value(6.0e10)})
    assert notes[0].direction == "증가"
    assert "증가" in notes[0].sentence()


def test_attribution_is_carried_when_the_provider_requires_it():
    notes = _notes([_atom()], {ETH_TVL: _value(3.91e10, attribution="")})
    assert notes[0].sentence().endswith("감소")


# --- when it stays quiet -----------------------------------------------------

def test_a_fresh_article_is_never_annotated():
    # Under a day the article still reads as current; "then vs now" is noise.
    fresh = _atom(as_of=(NOW - MIN_ARTICLE_AGE + timedelta(minutes=1)).isoformat())
    assert _notes([fresh], {ETH_TVL: _value(3.91e10)}) == ()


def test_a_small_change_is_never_annotated():
    below = 4.8e10 * (1 - CHANGE_THRESHOLDS["tvl"] / 2)
    assert _notes([_atom()], {ETH_TVL: _value(below)}) == ()


def test_the_threshold_boundary_is_annotated():
    at = 4.8e10 * (1 - CHANGE_THRESHOLDS["tvl"])
    assert len(_notes([_atom()], {ETH_TVL: _value(at)})) == 1


def test_supply_uses_its_own_tighter_threshold():
    # A 3% supply move matters; a 3% price move does not.
    supply = _atom(tag="supply", atom="19,700,000", subject_kind="asset", subject_id="BTC")
    key = ("supply", "asset", "BTC")
    assert len(_notes([supply], {key: _value(19_700_000 * 1.03)})) == 1
    price = _atom(tag="price", atom="19,700,000", subject_kind="asset", subject_id="BTC")
    assert _notes([price], {("price", "asset", "BTC"): _value(19_700_000 * 1.03)}) == ()


def test_an_unavailable_value_is_never_annotated():
    # A failed lookup shows the article's own number and nothing else.
    assert _notes([_atom()], {}) == ()


def test_an_unsupported_metric_is_never_annotated():
    assert _notes([_atom(tag="hashrate")], {("hashrate", "chain", "ethereum"): _value(1)}) == ()


@pytest.mark.parametrize(
    "override",
    [
        {"claim_id": ""},
        {"atom": ""},
        {"atom": "no digits here"},
        {"as_of": "not a date"},
        {"as_of": None},
    ],
)
def test_malformed_atoms_are_skipped(override):
    assert _notes([_atom(**override)], {ETH_TVL: _value(3.91e10)}) == ()


def test_non_mapping_entries_are_skipped():
    assert _notes(["nope", None], {ETH_TVL: _value(3.91e10)}) == ()


def test_no_atoms_yields_no_notes():
    assert _notes([], {ETH_TVL: _value(3.91e10)}) == ()


# --- volume ------------------------------------------------------------------

def test_the_same_figure_is_annotated_once():
    assert len(_notes([_atom(), _atom()], {ETH_TVL: _value(3.91e10)})) == 1


def test_notes_are_capped_and_keep_the_largest_moves():
    atoms, values = [], {}
    for index, multiplier in enumerate([1.2, 3.0, 1.5, 5.0, 2.0]):
        subject = f"chain-{index}"
        atoms.append(
            _atom(claim_id=f"CL-{index:03d}", atom="100 billion", subject_id=subject)
        )
        values[("tvl", "chain", subject)] = _value(1e11 * multiplier)

    notes = _notes(atoms, values)

    # Annotating every number would turn the reader into a dashboard.
    assert len(notes) == MAX_NOTES_PER_ARTICLE
    changes = [round(note.relative_change, 1) for note in notes]
    assert changes == sorted(changes, reverse=True)
    assert changes[0] == pytest.approx(4.0)


def test_thresholds_cover_exactly_the_supported_metrics():
    from app.market_data import SUPPORTED_TAGS

    assert set(CHANGE_THRESHOLDS) == SUPPORTED_TAGS


def test_unusable_reading_skips_note_instead_of_raising():
    """DR-13236: a value object with no usable reading must render nothing.

    MarketDataClient converges real failures to None, but the fail-open
    invariant is a release condition, so the guard lives here rather than
    depending on that upstream convention holding forever.
    """

    class _FailedReading:
        value = None
        attribution = ""

    atoms = [
        {
            "claim_id": "C-1",
            "atom": "480억 달러",
            "tag": "tvl",
            "subject_kind": "protocol",
            "subject_id": "ethereum",
            "as_of": "2026-07-14T00:00:00+00:00",
        }
    ]
    now = datetime(2026, 8, 17, tzinfo=UTC)
    for broken in (_FailedReading(), object(), "not-a-value"):
        notes = build_staleness_notes(
            tagged_atoms=atoms,
            values={("tvl", "protocol", "ethereum"): broken},
            now=now,
        )
        assert notes == ()
