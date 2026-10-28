"""Primary source typing and section selection (I-13206-b/c, DR-13205).

Both decisions are deterministic by design. The type comes from a curated list
rather than inference, because a wrong type produces a wrong reading of a
document nobody else covers. The section order comes from a keyword table
rather than a model, because the same filing must always read the same way.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.primary_documents import (
    DEFAULT_MAX_SECTION_CHARS,
    SECTION_KEYWORDS,
    document_type_for,
    load_primary_sources,
    select_sections,
)


SOURCES_FILE = Path(__file__).resolve().parents[1] / "primary_documents.yaml"

PROPOSAL = """EXAMPLE PROPOSAL

Voting
Quorum reached on March 3.

Abstract
This proposal raises the cap.

Appendix B
Boilerplate nobody reads.

Motivation
The current cap blocks growth.

Risks and drawbacks
Liquidity may fragment.
"""


def _write(tmp_path, sources) -> str:
    path = tmp_path / "primary_documents.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "sources": sources}, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def _source(**overrides) -> dict:
    source = {
        "name": "example-forum",
        "document_type": "governance_proposal",
        "discovery": "rss",
        "url": "https://forum.example.org/c/proposals.rss",
        "allow_url_prefixes": ["https://forum.example.org/t/"],
    }
    source.update(overrides)
    return source


# --- source list -------------------------------------------------------------

def test_missing_file_leaves_the_lane_idle(tmp_path):
    assert load_primary_sources(str(tmp_path / "absent.yaml")) == ()


def test_disabled_sources_are_skipped(tmp_path):
    path = _write(tmp_path, [_source(enabled=False)])
    assert load_primary_sources(path) == ()


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"document_type": "blog_post"}, "document_type"),
        ({"discovery": "scrape"}, "discovery"),
        ({"url": "forum.example.org"}, "http"),
        ({"allow_url_prefixes": []}, "allow_url_prefix"),
        ({"allow_url_prefixes": ["/t/"]}, "non-http"),
        ({"name": ""}, "name"),
    ],
)
def test_malformed_sources_fail_closed(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        load_primary_sources(_write(tmp_path, [_source(**overrides)]))


def test_duplicate_names_and_ambiguous_prefixes_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        load_primary_sources(_write(tmp_path, [_source(), _source()]))
    with pytest.raises(ValueError, match="ambiguous"):
        load_primary_sources(
            _write(tmp_path, [_source(), _source(name="other-forum")])
        )


def test_longest_prefix_decides_the_type(tmp_path):
    path = _write(
        tmp_path,
        [
            _source(allow_url_prefixes=["https://forum.example.org/"]),
            _source(
                name="incidents",
                document_type="post_mortem",
                allow_url_prefixes=["https://forum.example.org/t/incident-"],
            ),
        ],
    )
    sources = load_primary_sources(path)
    assert document_type_for("https://forum.example.org/t/a", sources) == (
        "governance_proposal"
    )
    assert document_type_for(
        "https://forum.example.org/t/incident-42", sources
    ) == "post_mortem"


def test_an_unlisted_url_is_not_collected(tmp_path):
    sources = load_primary_sources(_write(tmp_path, [_source()]))
    assert document_type_for("https://elsewhere.test/t/a", sources) is None
    assert document_type_for("", sources) is None


def test_shipped_source_list_covers_the_two_required_types():
    sources = load_primary_sources(str(SOURCES_FILE))
    types = [source.document_type for source in sources]
    # DR-13205 completion criterion 2 asks for these two specifically.
    assert types.count("governance_proposal") >= 2
    assert types.count("post_mortem") >= 2
    # Route diversity needs more than one publisher behind the lane.
    assert len({source.name for source in sources}) >= 4


# --- section selection -------------------------------------------------------

def test_sections_are_reordered_into_the_layout_the_type_needs():
    result = select_sections(PROPOSAL, document_type="governance_proposal")
    # The document lists voting first; the reading order puts it last.
    assert result.selected_headings == (
        "Abstract",
        "Motivation",
        "Risks and drawbacks",
        "Voting",
    )
    assert result.used_fallback is False


def test_unmatched_sections_are_dropped_and_recorded():
    result = select_sections(PROPOSAL, document_type="governance_proposal")
    assert "Appendix B" in result.dropped_headings
    assert "Boilerplate" not in result.body_text


def test_selection_is_deterministic():
    first = select_sections(PROPOSAL, document_type="governance_proposal")
    for _ in range(50):
        assert select_sections(PROPOSAL, document_type="governance_proposal") == first


def test_a_section_that_does_not_fit_is_dropped_whole():
    result = select_sections(
        PROPOSAL, document_type="governance_proposal", max_chars=60
    )
    assert result.truncated is True
    # Never a half section: a cut section reads as a complete one.
    for heading in result.selected_headings:
        assert heading in result.body_text
    assert len(result.body_text) <= 60


def test_a_document_without_matching_headings_uses_the_fallback():
    result = select_sections(
        "Nothing here matches any keyword group at all.\nJust prose.",
        document_type="audit_report",
    )
    assert result.used_fallback is True
    assert "Just prose." in result.body_text


def test_an_empty_selection_never_returns_an_empty_body():
    result = select_sections(
        "Appendix\nBoilerplate only.", document_type="governance_proposal"
    )
    assert result.used_fallback is True
    assert result.body_text


def test_fallback_truncates_at_the_cap():
    result = select_sections("가" * 100, document_type="post_mortem", max_chars=40)
    assert result.used_fallback is True
    assert result.truncated is True
    assert len(result.body_text) == 40


def test_audit_findings_are_selected():
    document = """Scope
Contracts in scope are listed below.

Findings
Three issues were identified.

Legal notice
Boilerplate.
"""
    result = select_sections(document, document_type="audit_report")
    assert "Findings" in result.selected_headings
    assert "Legal notice" in result.dropped_headings


def test_every_type_has_an_ordered_keyword_table():
    from app.primary_documents import DOCUMENT_TYPES

    assert set(SECTION_KEYWORDS) == set(DOCUMENT_TYPES)
    for groups in SECTION_KEYWORDS.values():
        assert len(groups) >= 3
        assert all(keywords for keywords in groups)


def test_default_cap_is_bounded():
    assert DEFAULT_MAX_SECTION_CHARS == 40_000
