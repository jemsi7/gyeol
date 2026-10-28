"""Directive grammar — the single source of truth for the inline tokens that
appear inside `body_markdown`.

A directive is written `<type:payload>` and marks a place where the future
parser/visualizer must insert non-text content (an image today; charts, graphs,
tables, timelines later). Keeping the grammar here means the validator
(`validation.py`), the resolver (`map_loader.py`) and the spec doc
(`docs/MAP_SPEC.md`) all agree.

To add a new directive type (e.g. `chart`), register it in DIRECTIVE_REGISTRY
with a payload parser, then document it in docs/MAP_SPEC.md and bump the map
format_version if the change is not backward compatible.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# Matches any `<type:payload>` token. type = lowercase identifier; payload has
# no angle brackets. Deliberately strict so malformed tokens are detectable.
DIRECTIVE_RE = re.compile(r"<([a-z][a-z0-9_]*):([^<>]*)>")

# Convenience regexes for block directives used by the resolver.
IMG_RE = re.compile(r"<img:(\d+)>")
CHART_RE = re.compile(r"<chart:(\d+)>")


@dataclass(slots=True)
class Directive:
    type: str
    payload_raw: str
    start: int
    end: int
    # Parsed value when the payload is well-formed (e.g. int ref for img).
    value: object = None
    ok: bool = False
    error: Optional[str] = None


def _parse_img(payload: str) -> tuple[bool, object, Optional[str]]:
    """img payload must be a non-negative integer ref index."""
    if re.fullmatch(r"\d+", payload):
        return True, int(payload), None
    return False, None, f"img payload must be an integer, got {payload!r}"


def _parse_chart(payload: str) -> tuple[bool, object, Optional[str]]:
    """chart payload must be a non-negative visualization ref index."""
    if re.fullmatch(r"\d+", payload):
        return True, int(payload), None
    return False, None, f"chart payload must be an integer, got {payload!r}"


# type -> payload parser. Extend this to add directive types.
DIRECTIVE_REGISTRY: dict[str, Callable[[str], tuple[bool, object, Optional[str]]]] = {
    "img": _parse_img,
    "chart": _parse_chart,
}


def parse_directives(body: str) -> list[Directive]:
    """Find and validate every `<type:payload>` token in the body."""
    out: list[Directive] = []
    for m in DIRECTIVE_RE.finditer(body or ""):
        dtype, payload = m.group(1), m.group(2)
        parser = DIRECTIVE_REGISTRY.get(dtype)
        if parser is None:
            out.append(
                Directive(dtype, payload, m.start(), m.end(), ok=False,
                          error=f"unknown directive type <{dtype}:...>")
            )
            continue
        ok, value, err = parser(payload)
        out.append(
            Directive(dtype, payload, m.start(), m.end(), value=value, ok=ok, error=err)
        )
    return out
