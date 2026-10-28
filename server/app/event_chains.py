"""Which reports are about the same incident (C-13209, PP-4).

Crypto incidents unfold: a hack, then the fund tracing, then an exchange
freeze, then a post-mortem. Early reports are almost always wrong, and Gyeol's
archive is what preserves them, so the chain exists to let a later correction
find the scene a reader saved.

Joining two reports that are not the same incident is worse than joining
nothing: it makes one article appear to correct another it has no relation to.
So similarity alone never joins. A shared-entity gate stands behind it --
embeddings happily rate "another bridge exploit" as near-identical to a
different bridge exploit -- and any ambiguity leaves the article unchained,
which is a normal outcome rather than a failure.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Mapping, Sequence

from .triage import normalized_title

log = logging.getLogger("gdelt.event_chains")

# Higher than day-clustering uses. That groups a day's coverage by topic; this
# claims two reports are the same event, and the cost of being wrong differs.
JOIN_SIMILARITY: Final[float] = 0.86
# Embedding similarity cannot tell one bridge exploit from another, but the
# names involved can. This gate is the main defence against a false join.
MIN_SHARED_ENTITIES: Final[int] = 2
# An incident that resurfaces after six weeks is usually a retrospective, not
# the same story continuing.
MAX_CHAIN_SPAN: Final[timedelta] = timedelta(days=45)
MAX_NEIGHBOURS: Final[int] = 10

_ACRONYM_RE: Final = re.compile(r"\b[A-Z]{2,6}\b")
_PROPER_RE: Final = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")
# Words that look like names but identify nothing in this domain.
_STOP_ENTITIES: Final[frozenset[str]] = frozenset(
    {
        "The", "This", "That", "These", "Those", "There", "Their",
        "After", "Before", "During", "Following", "According",
        "Crypto", "Bitcoin", "Blockchain", "Exchange", "Protocol",
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday", "January", "February", "March", "April",
        "June", "July", "August", "September", "October", "November",
        "December",
    }
)


@dataclass(frozen=True, slots=True)
class ChainCandidate:
    """One neighbour a new article might belong with."""

    article_id: str
    similarity: float
    chain_id: str | None = None


def article_entities(
    *, title: str = "", tagged_atoms: Sequence[Mapping[str, Any]] = ()
) -> frozenset[str]:
    """The names that identify which incident this is.

    Deliberately narrow: tagged subjects are already machine-verified, and
    title acronyms are the tickers, agencies and protocol names that separate
    one exploit from another. Generic capitalised words identify nothing.
    """
    entities: set[str] = set()
    for atom in tagged_atoms or ():
        if isinstance(atom, Mapping):
            subject = str(atom.get("subject_id") or "").strip()
            if subject:
                entities.add(subject.upper())
    text = title or ""
    entities.update(token.upper() for token in _ACRONYM_RE.findall(text))
    # In a Title Case headline every word is capitalised, so capitalisation
    # says nothing about which words are names -- "Said" and "Fell" would
    # count as entities and inflate the overlap the join gate depends on.
    if not _is_title_case(text):
        entities.update(
            token.upper()
            for token in _PROPER_RE.findall(text)
            if token not in _STOP_ENTITIES
        )
    return frozenset(entities)


def _is_title_case(text: str) -> bool:
    """True when capitalisation is a house style rather than a signal."""
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", text or "") if len(word) > 3]
    if len(words) < 4:
        return False
    capitalised = sum(1 for word in words if word[0].isupper())
    return capitalised / len(words) >= 0.7


def shared_entity_count(left: frozenset[str], right: frozenset[str]) -> int:
    return len(left & right)


def story_key(title: str) -> str:
    """The key syndicated copies of one story share.

    Deliberately the same fold triage already uses for its duplicate penalty:
    two definitions of "the same headline" would eventually disagree, and this
    one is the one the pipeline has been measured against.
    """
    return normalized_title(title)


def deduplicate_neighbours(
    neighbours: Sequence[ChainCandidate],
    *,
    story_key: str,
    neighbour_story_keys: Mapping[str, str],
) -> tuple[tuple[ChainCandidate, ...], frozenset[str]]:
    """Drop reprints from the neighbours, and name the chains that hold one.

    A reprint of this same headline is not a later report -- it is this report
    again -- so it can neither justify a join nor become a chain member. In the
    isolated staging corpus every pair that reached the entity gate was a
    syndicated copy, which is what this exists to stop (DR-13233).
    """
    seen = {story_key} if story_key else set()
    kept: list[ChainCandidate] = []
    reprint_chains: set[str] = set()
    for candidate in neighbours:
        key = neighbour_story_keys.get(candidate.article_id, "")
        if not key:
            # Nothing to compare; an untitled neighbour is judged on its merits.
            kept.append(candidate)
            continue
        if key in seen:
            if key == story_key and candidate.chain_id:
                # Joining that chain would put this story in it twice.
                reprint_chains.add(candidate.chain_id)
            continue
        seen.add(key)
        kept.append(candidate)
    return tuple(kept), frozenset(reprint_chains)


def chain_decision(
    neighbours: Sequence[ChainCandidate],
    *,
    entities: frozenset[str],
    neighbour_entities: Mapping[str, frozenset[str]],
) -> str | None:
    """The chain this article joins, ``None`` to stay unchained.

    ``None`` is returned far more often than a chain id, and every one of
    those cases is a deliberate refusal rather than a missing feature.
    """
    if not entities:
        return None
    qualified: list[ChainCandidate] = []
    for neighbour in neighbours[:MAX_NEIGHBOURS]:
        if neighbour.similarity < JOIN_SIMILARITY:
            continue
        shared = shared_entity_count(
            entities, neighbour_entities.get(neighbour.article_id, frozenset())
        )
        if shared < MIN_SHARED_ENTITIES:
            continue
        qualified.append(neighbour)
    if not qualified:
        return None

    chains = {item.chain_id for item in qualified if item.chain_id}
    if len(chains) > 1:
        # Two established chains both look right, which means at least one is
        # wrong. Merging them is the failure this unit exists to avoid.
        log.info("event chain join skipped: %d candidate chains", len(chains))
        return None
    if chains:
        return next(iter(chains))
    # Every qualified neighbour is itself unchained, so this is a new event --
    # but a single article is not one, and a lone report starts nothing.
    return None


def should_open_chain(
    neighbours: Sequence[ChainCandidate],
    *,
    entities: frozenset[str],
    neighbour_entities: Mapping[str, frozenset[str]],
) -> tuple[str, ...]:
    """Article ids that should form a new chain together with this one.

    Empty when there is nothing to open: a chain of one is not an event.
    """
    if not entities:
        return ()
    members = [
        neighbour.article_id
        for neighbour in neighbours[:MAX_NEIGHBOURS]
        if neighbour.chain_id is None
        and neighbour.similarity >= JOIN_SIMILARITY
        and shared_entity_count(
            entities, neighbour_entities.get(neighbour.article_id, frozenset())
        )
        >= MIN_SHARED_ENTITIES
    ]
    if any(neighbour.chain_id for neighbour in neighbours[:MAX_NEIGHBOURS]):
        # An existing chain is in range; joining it is chain_decision's call,
        # not an excuse to start a competing one.
        return ()
    return tuple(dict.fromkeys(members))
