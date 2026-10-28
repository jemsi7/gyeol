"""Publisher registry loading and lookup (I-13202-a, DR-13203).

The registry is the factual half of publisher context. It fails closed on a
malformed entry -- a wrong claim about who owns what is worse than no sentence
at all -- but treats an absent file as "say nothing", which is the documented
default rather than an error.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from app.publishers import PublisherRegistry, PublisherRecord


REGISTRY_FILE = Path(__file__).resolve().parents[1] / "publishers.yaml"


def _write(tmp_path, publishers) -> str:
    path = tmp_path / "publishers.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "publishers": publishers}, allow_unicode=True),
        encoding="utf-8",
    )
    return str(path)


def _entry(**overrides) -> dict:
    entry = {
        "domain": "research.example.com",
        "publisher_type": "exchange",
        "owner": "Example Exchange",
        "listed_assets": ["btc", "ETH"],
        "source_url": "https://example.com/listings",
        "verified_on": date(2026, 8, 9),
    }
    entry.update(overrides)
    return entry


def test_missing_file_yields_an_empty_registry(tmp_path):
    registry = PublisherRegistry.load(str(tmp_path / "absent.yaml"))
    assert len(registry) == 0
    assert registry.lookup("example.com") is None


def test_symbols_are_folded_to_uppercase(tmp_path):
    registry = PublisherRegistry.load(_write(tmp_path, [_entry()]))
    record = registry.lookup("research.example.com")
    assert record is not None
    assert record.listed_assets == frozenset({"BTC", "ETH"})


def test_duplicate_domain_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        PublisherRegistry.load(_write(tmp_path, [_entry(), _entry()]))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"publisher_type": "blogger"}, "publisher_type"),
        ({"owner": ""}, "owner"),
        ({"owner": "x" * 81}, "exceeds"),
        ({"source_url": "example.com/listings"}, "source_url"),
        ({"verified_on": "2026-08-09"}, "verified_on"),
        ({"listed_assets": []}, "must list listed_assets"),
        ({"listed_assets": ["BTC", " "]}, "blank symbol"),
    ],
)
def test_malformed_entries_fail_closed(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        PublisherRegistry.load(_write(tmp_path, [_entry(**overrides)]))


def test_interest_types_must_carry_their_facts(tmp_path):
    # A project entry with no protocol_assets would claim a relationship the
    # registry cannot back up when the sentence is built.
    with pytest.raises(ValueError, match="must list protocol_assets"):
        PublisherRegistry.load(
            _write(
                tmp_path,
                [
                    _entry(
                        publisher_type="project",
                        listed_assets=None,
                        protocol_assets=None,
                    )
                ],
            )
        )


def test_independent_needs_no_owner(tmp_path):
    path = _write(
        tmp_path,
        [
            _entry(
                publisher_type="independent",
                owner="",
                listed_assets=None,
            )
        ],
    )
    assert PublisherRegistry.load(path).lookup("research.example.com") is not None


def test_subdomain_matches_and_the_longest_entry_wins(tmp_path):
    path = _write(
        tmp_path,
        [
            _entry(domain="example.com"),
            _entry(
                domain="research.example.com",
                owner="Example Research",
                listed_assets=["SOL"],
            ),
        ],
    )
    registry = PublisherRegistry.load(path)
    assert registry.lookup("news.example.com").owner == "Example Exchange"
    assert registry.lookup("research.example.com").owner == "Example Research"
    assert registry.lookup("deep.research.example.com").owner == "Example Research"


def test_lookup_folds_www_and_case(tmp_path):
    registry = PublisherRegistry.load(_write(tmp_path, [_entry(domain="example.com")]))
    assert registry.lookup("WWW.Example.COM") is not None
    assert registry.lookup("") is None
    assert registry.lookup("unrelated.test") is None


def test_interest_in_is_empty_for_non_interest_types():
    wire = PublisherRecord(
        domain="prnewswire.com",
        publisher_type="wire",
        owner="PR Newswire",
        source_url="https://example.com",
        verified_on=date(2026, 8, 9),
    )
    assert wire.interest_in(frozenset({"BTC"})) == frozenset()


def test_shipped_registry_loads_and_records_its_sources():
    registry = PublisherRegistry.load(str(REGISTRY_FILE))
    assert len(registry) >= 20
    raw = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8"))["publishers"]
    # Every factual claim must point at where it was checked.
    assert all(item["source_url"].startswith("https://") for item in raw)
    assert all(isinstance(item["verified_on"], date) for item in raw)


def test_shipped_registry_covers_the_triage_wire_domains():
    from app.triage import WIRE_DOMAINS

    registry = PublisherRegistry.load(str(REGISTRY_FILE))
    # Not every wire needs an entry, but the ones that have one must agree with
    # the triage list rather than contradict it.
    for domain in WIRE_DOMAINS:
        record = registry.lookup(domain)
        if record is not None:
            assert record.publisher_type == "wire"
