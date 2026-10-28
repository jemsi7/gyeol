"""Publisher registry: who issued an article and what they own (DR-13203).

Facts only. Each record states a relationship that was checked against a public
source, never a judgement about a publisher. The inferred axis -- the model's
read on how promotional a piece is -- stays in ``map_queue.triage_signals`` and
is not represented here at all, so no reader-facing sentence can be built from
it by accident.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

import yaml

log = logging.getLogger("gdelt.publishers")

PUBLISHER_TYPES: Final[frozenset[str]] = frozenset(
    {
        "exchange",
        "project",
        "investor_owned",
        "wire",
        "individual",
        "independent",
    }
)
# Types whose sentence names an interest in a specific asset, and the field
# that interest is recorded in.
INTEREST_FIELDS: Final[dict[str, str]] = {
    "exchange": "listed_assets",
    "project": "protocol_assets",
    "investor_owned": "portfolio_assets",
}
MAX_OWNER_CHARS: Final[int] = 80
MAX_ASSETS: Final[int] = 200


@dataclass(frozen=True, slots=True)
class PublisherRecord:
    domain: str
    publisher_type: str
    owner: str
    source_url: str
    verified_on: date
    listed_assets: frozenset[str] = field(default_factory=frozenset)
    portfolio_assets: frozenset[str] = field(default_factory=frozenset)
    protocol_assets: frozenset[str] = field(default_factory=frozenset)

    def interest_in(self, subject_assets: frozenset[str]) -> frozenset[str]:
        """Assets this publisher has a recorded interest in, of those named."""
        field_name = INTEREST_FIELDS.get(self.publisher_type)
        if field_name is None:
            return frozenset()
        return frozenset(getattr(self, field_name)) & subject_assets


def _assets(raw: Any, *, domain: str, key: str) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or len(raw) > MAX_ASSETS:
        raise ValueError(f"publisher {domain!r} {key} must be a list of symbols")
    symbols: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"publisher {domain!r} {key} has a blank symbol")
        symbols.add(item.strip().upper())
    return frozenset(symbols)


def _record(raw: Any) -> PublisherRecord:
    if not isinstance(raw, dict):
        raise ValueError("each publisher must be an object")
    domain = str(raw.get("domain") or "").strip().casefold().removeprefix("www.")
    if not domain:
        raise ValueError("publisher domain must be a non-empty string")
    publisher_type = str(raw.get("publisher_type") or "").strip()
    if publisher_type not in PUBLISHER_TYPES:
        allowed = ", ".join(sorted(PUBLISHER_TYPES))
        raise ValueError(
            f"publisher {domain!r} publisher_type must be one of: {allowed}"
        )
    owner = str(raw.get("owner") or "").strip()
    if publisher_type != "independent" and not owner:
        raise ValueError(f"publisher {domain!r} must name an owner")
    if len(owner) > MAX_OWNER_CHARS:
        raise ValueError(f"publisher {domain!r} owner exceeds {MAX_OWNER_CHARS} chars")
    source_url = str(raw.get("source_url") or "").strip()
    if not source_url.startswith(("http://", "https://")):
        raise ValueError(f"publisher {domain!r} needs an http(s) source_url")
    verified_on = raw.get("verified_on")
    if not isinstance(verified_on, date):
        raise ValueError(f"publisher {domain!r} needs a verified_on date")
    record = PublisherRecord(
        domain=domain,
        publisher_type=publisher_type,
        owner=owner,
        source_url=source_url,
        verified_on=verified_on,
        listed_assets=_assets(
            raw.get("listed_assets"), domain=domain, key="listed_assets"
        ),
        portfolio_assets=_assets(
            raw.get("portfolio_assets"), domain=domain, key="portfolio_assets"
        ),
        protocol_assets=_assets(
            raw.get("protocol_assets"), domain=domain, key="protocol_assets"
        ),
    )
    # A type whose sentence names an interest cannot be recorded without the
    # facts that sentence is made of: the entry would claim a relationship the
    # registry cannot back up.
    interest_field = INTEREST_FIELDS.get(publisher_type)
    if interest_field is not None and not getattr(record, interest_field):
        raise ValueError(
            f"publisher {domain!r} is {publisher_type} and must list {interest_field}"
        )
    return record


class PublisherRegistry:
    """Immutable domain -> record lookup with subdomain matching."""

    def __init__(self, records: tuple[PublisherRecord, ...] = ()):
        self._by_domain = {record.domain: record for record in records}

    def __len__(self) -> int:
        return len(self._by_domain)

    @classmethod
    def load(cls, path: str) -> "PublisherRegistry":
        if not os.path.exists(path):
            # An absent registry is not a failure: publisher context simply
            # stays empty, which is the DR-13203 default anyway.
            log.warning("publisher registry %s not found; context stays empty", path)
            return cls()
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        raw_list = data.get("publishers") or []
        if not isinstance(raw_list, list):
            raise ValueError("publishers must be a list")
        records: list[PublisherRecord] = []
        seen: set[str] = set()
        for raw in raw_list:
            record = _record(raw)
            if record.domain in seen:
                raise ValueError(f"duplicate publisher domain: {record.domain!r}")
            seen.add(record.domain)
            records.append(record)
        log.info("loaded %d publisher record(s) from %s", len(records), path)
        return cls(tuple(records))

    def lookup(self, domain: str) -> PublisherRecord | None:
        """Exact or subdomain match, using the triage host-folding rules."""
        host = (domain or "").strip().casefold().removeprefix("www.")
        if not host:
            return None
        record = self._by_domain.get(host)
        if record is not None:
            return record
        # Longest suffix wins so a specific subdomain entry beats its parent.
        best: PublisherRecord | None = None
        for candidate_domain, candidate in self._by_domain.items():
            if host.endswith(f".{candidate_domain}") and (
                best is None or len(candidate_domain) > len(best.domain)
            ):
                best = candidate
        return best
