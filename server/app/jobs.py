"""Named source jobs and their provider-specific fetch options.

Jobs are loaded from a YAML file (JOBS_FILE, default jobs.yaml) so you can add
new topics without touching code. Legacy jobs default to the GDELT provider.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal, Optional

import yaml

log = logging.getLogger("source.jobs")

# Coverage domains a job may collect into (DR-13200, DR-13208). Only "core"
# articles reach map_queue; every other domain is quarantined from Map
# generation until its own daily budget axis exists.
COVERAGE_DOMAINS: frozenset[str] = frozenset({"core", "crypto"})


def _normalized_source_lang(
    raw: Any, *, name: str
) -> Optional[str | list[str]]:
    """Accept one language or a list, rejecting anything unusable as a filter."""
    if raw is None:
        return None
    values = raw if isinstance(raw, list) else [raw]
    if not values:
        raise ValueError(f"job {name!r} source_lang must not be an empty list")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"job {name!r} source_lang entries must be non-empty strings")
        language = value.strip().casefold()
        # The value is interpolated straight into a GDELT query operator, so a
        # token with whitespace or an operator character would silently change
        # the whole query rather than fail.
        if not language.isalpha():
            raise ValueError(f"job {name!r} source_lang {value!r} must be alphabetic")
        if language not in cleaned:
            cleaned.append(language)
    return cleaned[0] if len(cleaned) == 1 and not isinstance(raw, list) else cleaned


@dataclass(slots=True)
class Job:
    name: str
    query: str
    provider: Literal["gdelt", "newsapi"] = "gdelt"
    # Either a simple timespan ("1d", "6h") ...
    timespan: Optional[str] = "1d"
    # ... or a multi-slice window for higher volume.
    window_hours: Optional[int] = None
    slice_hours: int = 6
    max_records: int = 250
    # One language, or several for a lane whose output language is pinned
    # (DR-13228). A list becomes an OR group in the GDELT query.
    source_lang: Optional[str | list[str]] = None
    source_country: Optional[str] = None
    # NewsAPI /v2/everything options.
    language: Optional[str] = None
    search_in: Optional[str] = None
    sort_by: Optional[str] = None
    # Which coverage domain this job's articles belong to. Non-core domains are
    # collected but kept out of map_queue entirely (DR-13208).
    coverage_domain: str = "core"
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        provider = d.get("provider", "gdelt")
        if provider not in {"gdelt", "newsapi"}:
            raise ValueError(f"unsupported job provider: {provider!r}")
        if not isinstance(d.get("name"), str) or not d["name"].strip():
            raise ValueError("job name must be a non-empty string")
        if not isinstance(d.get("query"), str) or not d["query"].strip():
            raise ValueError(f"job {d['name']!r} query must be a non-empty string")

        job = cls(
            name=d["name"].strip(),
            query=d["query"].strip(),
            provider=provider,
            timespan=d.get("timespan", "1d" if provider == "gdelt" else None),
            window_hours=d.get("window_hours"),
            slice_hours=int(d.get("slice_hours", 6)),
            max_records=int(d.get("max_records", 250)),
            source_lang=_normalized_source_lang(d.get("source_lang"), name=d["name"]),
            source_country=d.get("source_country"),
            language=d.get("language"),
            search_in=d.get("search_in"),
            sort_by=d.get("sort_by"),
            coverage_domain=d.get("coverage_domain", "core"),
            enabled=bool(d.get("enabled", True)),
        )
        job._validate(source=d)
        return job

    def _validate(self, *, source: dict[str, Any]) -> None:
        if self.slice_hours <= 0:
            raise ValueError(f"job {self.name!r} slice_hours must be positive")
        if self.max_records <= 0:
            raise ValueError(f"job {self.name!r} max_records must be positive")
        # Never normalize an unexpected domain into a default: a typo that
        # silently resolved to "core" would let a quarantined lane spend the
        # shared Map budget, which is exactly what the quarantine prevents.
        if self.coverage_domain not in COVERAGE_DOMAINS:
            allowed = ", ".join(sorted(COVERAGE_DOMAINS))
            raise ValueError(
                f"job {self.name!r} coverage_domain must be one of: {allowed}"
            )

        if self.provider == "newsapi":
            if not self.name.startswith("newsapi:"):
                raise ValueError("NewsAPI job names must start with 'newsapi:'")
            if source.get("timespan") is not None:
                raise ValueError(f"NewsAPI job {self.name!r} does not accept timespan")
            if self.window_hours != 24:
                raise ValueError(f"NewsAPI job {self.name!r} window_hours must be 24")
            if not 1 <= self.max_records <= 100:
                raise ValueError(f"NewsAPI job {self.name!r} max_records must be 1..100")
            if len(self.query) > 500:
                raise ValueError(f"NewsAPI job {self.name!r} query exceeds 500 characters")
            if self.language != "en":
                raise ValueError(f"NewsAPI job {self.name!r} language must be 'en'")
            if self.search_in != "title,description":
                raise ValueError(
                    f"NewsAPI job {self.name!r} search_in must be 'title,description'"
                )
            if self.sort_by != "publishedAt":
                raise ValueError(
                    f"NewsAPI job {self.name!r} sort_by must be 'publishedAt'"
                )
            if self.source_lang is not None or self.source_country is not None:
                raise ValueError(
                    f"NewsAPI job {self.name!r} does not accept GDELT source filters"
                )
            return

        for field_name in ("language", "search_in", "sort_by"):
            if source.get(field_name) is not None:
                raise ValueError(
                    f"GDELT job {self.name!r} does not accept {field_name}"
                )
        if self.max_records > 250:
            raise ValueError(f"GDELT job {self.name!r} max_records must be 1..250")


def load_jobs(path: str) -> list[Job]:
    if not os.path.exists(path):
        log.warning("jobs file %s not found; using built-in default", path)
        return _default_jobs()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("jobs") or []
    if not isinstance(raw, list):
        raise ValueError("jobs must be a list")
    seen_names: set[str] = set()
    jobs: list[Job] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each job must be an object")
        raw_name = item.get("name")
        if isinstance(raw_name, str) and raw_name in seen_names:
            raise ValueError(f"duplicate job name: {raw_name!r}")
        if isinstance(raw_name, str):
            seen_names.add(raw_name)
        if item.get("enabled", True):
            jobs.append(Job.from_dict(item))
    log.info("Loaded %d enabled job(s) from %s", len(jobs), path)
    return jobs or _default_jobs()


def _default_jobs() -> list[Job]:
    return [Job(name="breaking-news", query="breaking news", timespan="1d")]
