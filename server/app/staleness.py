"""What a number said then, and what it says now (C-13205, PP-1).

Gyeol is built for reading later: saved articles, resumed scenes, offline. In
AI coverage that delay is harmless. In crypto it is the whole problem -- a
three-day-old TVL figure reads as a current one -- so this module exists to
close a hazard the product's own design creates.

It is written to say almost nothing. A note appears only when the article is
old enough for the gap to matter, the current value is actually known, and the
change is large enough to be worth a reader's attention. Anything else renders
as nothing at all, because annotating every number would turn the reader into
a dashboard, which is exactly what this coverage is not.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Mapping, Sequence

log = logging.getLogger("gdelt.staleness")

# Below a day the "then vs now" framing is noise: the article still reads as
# current, and a 10% intraday move is not a correction of anything.
MIN_ARTICLE_AGE: Final[timedelta] = timedelta(days=1)
# Per-metric, because supply moves in fractions of a percent while a price
# routinely moves ten. One shared threshold would either flood prices or hide
# every supply change worth noticing.
CHANGE_THRESHOLDS: Final[Mapping[str, float]] = {
    "price": 0.10,
    "marketcap": 0.10,
    "tvl": 0.10,
    "supply": 0.02,
}
MAX_NOTES_PER_ARTICLE: Final[int] = 3

# Scale words in the languages this lane collects, plus the Korean myriad
# groupings a translated figure actually uses.
SCALE_WORDS: Final[Mapping[str, float]] = {
    "thousand": 1e3,
    "million": 1e6,
    "billion": 1e9,
    "trillion": 1e12,
    "만": 1e4,
    "억": 1e8,
    "조": 1e12,
}
_NUMBER_RE: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SCALE_RE: Final = re.compile(
    r"\s*(" + "|".join(sorted(SCALE_WORDS, key=len, reverse=True)) + r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class StalenessNote:
    claim_id: str
    atom: str
    tag: str
    as_of_text: str
    current_text: str
    relative_change: float
    attribution: str = ""

    @property
    def direction(self) -> str:
        return "증가" if self.relative_change > 0 else "감소"

    def sentence(self) -> str:
        """The fixed rendering. One line, no evaluation, no advice."""
        line = (
            f"기사 시점 {self.as_of_text} · 지금 {self.current_text}"
            f" · {abs(self.relative_change) * 100:.1f}% {self.direction}"
        )
        return f"{line} · {self.attribution}" if self.attribution else line


def parse_scaled_number(text: str) -> tuple[float, str] | None:
    """Return the value a printed figure denotes, and the scale word used.

    ``480억`` and ``$48 billion`` are the same quantity written two ways, and
    the comparison has to be against the quantity rather than the digits.
    """
    match = _NUMBER_RE.search(text or "")
    if match is None:
        return None
    # A leading minus would otherwise be dropped silently, turning a negative
    # figure into a positive one of the same size.
    if match.start() and (text or "")[match.start() - 1] in "-−":
        return None
    try:
        base = float(match.group().replace(",", ""))
    except ValueError:
        return None
    if base <= 0:
        return None
    scale_match = _SCALE_RE.match(text, match.end())
    if scale_match is None:
        return base, ""
    word = scale_match.group(1)
    return base * SCALE_WORDS[word.casefold()], word


def format_like(value: float, template: str) -> str | None:
    """Render ``value`` in the same units the article used.

    Changing units between the two halves would leave a reader comparing
    numbers that are not comparable at a glance, which defeats the note.
    """
    parsed = parse_scaled_number(template)
    if parsed is None:
        return None
    _base, word = parsed
    factor = SCALE_WORDS[word.casefold()] if word else 1.0
    scaled = value / factor
    if scaled >= 100:
        digits = 0
    elif scaled >= 10:
        digits = 1
    else:
        digits = 2
    match = _NUMBER_RE.search(template or "")
    if match is None:
        return None
    number = f"{scaled:,.{digits}f}"
    # Keep whatever surrounded the original figure -- a leading currency symbol
    # as much as a trailing unit -- so the two halves read as the same kind of
    # quantity rather than a bare number next to a priced one.
    prefix = (template or "")[: match.start()]
    suffix = (template or "")[match.end() :]
    return f"{prefix}{number}{suffix}".strip()


def _as_of(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def build_staleness_notes(
    *,
    tagged_atoms: Sequence[Mapping[str, Any]],
    values: Mapping[tuple[str, str, str], Any],
    now: datetime,
) -> tuple[StalenessNote, ...]:
    """Only the notes that clear age, availability and size."""
    notes: list[StalenessNote] = []
    seen: set[tuple[str, str]] = set()
    for atom in tagged_atoms or ():
        if not isinstance(atom, Mapping):
            continue
        claim_id = str(atom.get("claim_id") or "")
        printed = str(atom.get("atom") or "")
        tag = str(atom.get("tag") or "")
        key = (claim_id, printed)
        if not claim_id or not printed or key in seen:
            continue
        as_of = _as_of(atom.get("as_of"))
        if as_of is None or now - as_of < MIN_ARTICLE_AGE:
            continue
        current = values.get(
            (tag, str(atom.get("subject_kind") or ""), str(atom.get("subject_id") or ""))
        )
        if current is None:
            continue
        # DR-13236 makes "show nothing" the release invariant, so the guard is
        # structural here rather than a convention upstream: MarketDataClient
        # converges every failure to None today, but a value that is not a
        # usable reading must skip the note, never raise into the reader.
        current_value = getattr(current, "value", None)
        if not isinstance(current_value, (int, float)) or isinstance(
            current_value, bool
        ):
            continue
        parsed = parse_scaled_number(printed)
        if parsed is None:
            continue
        original, _word = parsed
        if original <= 0:
            continue
        change = (float(current_value) - original) / original
        threshold = CHANGE_THRESHOLDS.get(tag)
        if threshold is None or abs(change) < threshold:
            continue
        current_text = format_like(float(current_value), printed)
        if current_text is None:
            continue
        seen.add(key)
        notes.append(
            StalenessNote(
                claim_id=claim_id,
                atom=printed,
                tag=tag,
                as_of_text=printed,
                current_text=current_text,
                relative_change=change,
                attribution=str(getattr(current, "attribution", "") or ""),
            )
        )
    # Annotating everything would make the reader a dashboard; keep the moves
    # that actually changed the most.
    notes.sort(key=lambda note: abs(note.relative_change), reverse=True)
    if len(notes) > MAX_NOTES_PER_ARTICLE:
        log.info("staleness notes truncated kept=%d", MAX_NOTES_PER_ARTICLE)
    return tuple(notes[:MAX_NOTES_PER_ARTICLE])

def collect_staleness_subjects(
    items: Sequence[Mapping[str, Any]]
) -> list[tuple[str, str, str]]:
    """Every subject the given app items might annotate, deduplicated.

    Collected across the whole response so one bulk lookup serves it; a
    per-article lookup would multiply a read-time network call by the page.
    """
    subjects: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items or ():
        payload = (item or {}).get("map") if isinstance(item, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        for atom in payload.get("tagged_atoms") or ():
            if not isinstance(atom, Mapping):
                continue
            key = (
                str(atom.get("tag") or ""),
                str(atom.get("subject_kind") or ""),
                str(atom.get("subject_id") or ""),
            )
            if all(key) and key not in seen:
                seen.add(key)
                subjects.append(key)
    return subjects


def attach_staleness_notes(
    items: Sequence[Mapping[str, Any]],
    values: Mapping[tuple[str, str, str], Any],
    *,
    now: datetime,
) -> int:
    """Write each note onto the first scene that carries its claim.

    Returns how many notes were attached. Scene prose is never touched: the
    annotation is an extra field beside it, so the article still reads exactly
    as it was written.
    """
    attached = 0
    for item in items or ():
        payload = (item or {}).get("map") if isinstance(item, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        scenes = payload.get("scenes")
        if not isinstance(scenes, list):
            continue
        for scene in scenes:
            if isinstance(scene, dict):
                scene.setdefault("staleness_note", "")
        notes = build_staleness_notes(
            tagged_atoms=payload.get("tagged_atoms") or (),
            values=values,
            now=now,
        )
        for note in notes:
            for scene in scenes:
                if not isinstance(scene, dict) or scene.get("staleness_note"):
                    continue
                if note.claim_id in (scene.get("claim_refs") or []):
                    scene["staleness_note"] = note.sentence()
                    attached += 1
                    break
    return attached
