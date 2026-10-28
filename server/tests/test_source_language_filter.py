"""Per-lane source language filtering (DR-13228).

The crypto lane pins its output language to Korean, so it may only collect
sources it can render faithfully. Every other lane keeps collecting in every
language: in production 55.6% of successfully mapped articles are non-English,
and the same filter there would delete a third of the working coverage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.gdelt_client import _augment_query
from app.jobs import Job, load_jobs


JOBS_FILE = Path(__file__).resolve().parents[1] / "jobs.yaml"


def _job(**overrides) -> Job:
    payload = {
        "name": "lane",
        "provider": "gdelt",
        "coverage_domain": "crypto",
        "query": "(bitcoin)",
        "window_hours": 24,
    }
    payload.update(overrides)
    return Job.from_dict(payload)


# --- query construction ------------------------------------------------------

def test_a_single_language_stays_a_bare_operator():
    assert _augment_query("(a)", "english", None) == "(a) sourcelang:english"


def test_several_languages_become_an_or_group():
    # Space is AND in DOC 2.0, so a bare list would match nothing at all.
    assert _augment_query("(a)", ["english", "korean"], None) == (
        "(a) (sourcelang:english OR sourcelang:korean)"
    )


def test_a_one_item_list_needs_no_group():
    assert _augment_query("(a)", ["english"], None) == "(a) sourcelang:english"


def test_no_language_leaves_the_query_untouched():
    assert _augment_query("(a)", None, None) == "(a)"
    assert _augment_query("(a)", [], None) == "(a)"


def test_language_and_country_compose():
    assert _augment_query("(a)", ["english", "korean"], "southkorea") == (
        "(a) (sourcelang:english OR sourcelang:korean) sourcecountry:southkorea"
    )


# --- job parsing -------------------------------------------------------------

def test_a_list_is_normalised_and_deduplicated():
    assert _job(source_lang=["English", "korean", "ENGLISH"]).source_lang == [
        "english",
        "korean",
    ]


def test_a_plain_string_stays_a_string():
    assert _job(source_lang="English").source_lang == "english"


@pytest.mark.parametrize(
    "value",
    [
        [],
        [""],
        ["english", "  "],
        ["english OR korean"],   # would silently rewrite the whole query
        ["source lang"],
        [123],
    ],
)
def test_unusable_language_values_are_rejected(value):
    # The value is interpolated straight into a query operator, so anything
    # that is not a bare alphabetic token has to fail loudly.
    with pytest.raises(ValueError):
        _job(source_lang=value)


def test_absent_language_stays_none():
    assert _job().source_lang is None


# --- the shipped configuration ----------------------------------------------

def test_crypto_lane_collects_only_english_and_korean():
    jobs = {job.name: job for job in load_jobs(str(JOBS_FILE))}
    assert jobs["blockchain-crypto"].source_lang == ["english", "korean"]


def test_other_lanes_keep_collecting_every_language():
    jobs = {job.name: job for job in load_jobs(str(JOBS_FILE))}
    # DR-13228 deliberately leaves these alone; narrowing them would drop the
    # majority of what currently maps successfully in production.
    assert jobs["ai-technology"].source_lang is None
    for name, job in jobs.items():
        if job.coverage_domain != "crypto":
            assert job.source_lang is None, name


def test_crypto_query_carries_the_or_group():
    jobs = {job.name: job for job in load_jobs(str(JOBS_FILE))}
    crypto = jobs["blockchain-crypto"]
    augmented = _augment_query(crypto.query, crypto.source_lang, crypto.source_country)
    assert augmented.endswith("(sourcelang:english OR sourcelang:korean)")
    assert crypto.query in augmented
