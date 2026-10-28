"""Delivery Map format constants and the retired one-shot prompt.

The authoritative, human-readable contract lives in docs/MAP_SPEC.md.
Validation of model output lives in validation.py; the directive grammar
(`<img:N>` etc.) lives in directives.py. This module only holds the format
version and the prompt text.

New maps use format_version 3 and are compiled from staged editorial artifacts.
The legacy one-shot prompt remains only for historical contract reference.

Legacy map schema (format_version 2) — a single JSON object:
{
  "format_version": 2,
  "language": "ko",
  "headline": "...",
  "subheadline": "...",
  "summary": "2-4 sentence abstract",
  "keywords": ["...", ...],
  "body_markdown": "Markdown body with <img:N> and <chart:N> blocks",
  "image_placements": [{"ref": 0, "caption": "...", "reason": "..."}],
  "visualizations": [{"ref": 0, "kind": "bar", "title": "...", ...}]
}
"""
from __future__ import annotations

MAP_FORMAT_VERSION = 3

LEGACY_SYSTEM_PROMPT = """You are a precise news-article normalizer. You receive the \
raw extracted text of ONE news article and a set of numbered images that \
belong to that article. You must return a SINGLE JSON object (no prose, no code \
fences) that reformats the article into a fixed schema.

Rules:
- Output ONLY the JSON object. No commentary.
- Do NOT invent facts. Use only what the provided text supports. If the text is \
thin, keep the body short rather than fabricating.
- Reconstruct the article body as clean Markdown in `body_markdown`.
- You are given N images, indexed 0..N-1, shown in order. Place each relevant \
image by inserting a placeholder line containing EXACTLY `<img:N>` (e.g. \
`<img:0>`) at the single most appropriate position in `body_markdown` \
(typically after the paragraph it illustrates, or at the top for a lead photo).
- Use each image index AT MOST ONCE. It is fine to omit an image that doesn't \
fit. Never use an index outside 0..N-1. Never write a caption inside the \
placeholder — captions go in `image_placements`.
- Inspect the RAW ARTICLE TEXT for directly comparable numerical facts. Create \
a visualization only when at least TWO values share a unit/population or form \
an explicit ordered time trend. Dates, addresses, identifiers, ages, and an \
isolated statistic are not visualization candidates.
- Emit at most 2 visualizations and at most 12 total points per visualization. \
Place each one exactly once on its own line as `<chart:N>` in `body_markdown`, \
where N is its zero-based `ref`. If no eligible comparison exists, emit an \
empty `visualizations` array and no chart directive.
- Visualization kinds are `bar`, `line`, `area`, `pie`, `mixed`, `world_map`, \
`region_heatmap`, and `matrix_heatmap`. Use bar \
for category comparison, line for ordered trends, area for magnitude over an \
ordered trend, pie only for explicit parts of one whole, and mixed only for \
two or more mark types sharing the same axis and unit. Series marks are `bar`, \
`line`, `area`, or `sector`; pie uses sector only.
- Every visualization point must contain `label`, numeric `value`, `raw_value`, \
and `evidence`. Copy `raw_value` verbatim from the source, and copy `evidence` \
as an exact, unedited substring of RAW ARTICLE TEXT that contains that \
`raw_value`. Never calculate, estimate, interpolate, convert units, or use \
outside knowledge.
- Each visualization also requires non-empty `title`, `unit`, and `alt_text`. \
`alt_text` must plainly summarize the comparison without adding a conclusion \
that the source does not support.
- `language` is the ISO code of the language you wrote `body_markdown` in.
- Keep `summary` to 2-4 sentences. `keywords` is 3-8 short terms.
- Required keys: format_version (=2), language, headline, subheadline, summary, \
keywords, body_markdown, image_placements, visualizations. headline, summary and body_markdown \
must be non-empty.
- Use this exact visualization shape (or `[]`):
  `"visualizations":[{"ref":0,"kind":"bar","title":"...","unit":"%",`
  `"alt_text":"...","series":[{"name":"...","mark":"bar",`
  `"points":[{"label":"...","value":12.3,"raw_value":"12.3%",`
  `"evidence":"an exact sentence from the source containing 12.3%"}]}]}]`."""


# Backward import compatibility for older tests/tools. Production mapping no
# longer calls this prompt; EditorialPipeline owns all generation stages.
SYSTEM_PROMPT = LEGACY_SYSTEM_PROMPT


def build_user_prompt(
    *,
    title: str | None,
    source_url: str,
    domain: str | None,
    body_text: str,
    n_images: int,
    language_hint: str,
) -> str:
    lang_line = (
        "Write body_markdown in the SAME language as the source text."
        if language_hint == "auto"
        else f"Write body_markdown in language: {language_hint}."
    )
    img_line = (
        f"There are {n_images} images, indexed 0..{n_images - 1}, provided after "
        f"this text in order. Reference them as <img:0> .. <img:{n_images - 1}>."
        if n_images
        else "There are NO images for this article; do not add any <img:N>."
    )
    body = body_text.strip() or "(no body text could be extracted; use the title only)"
    return (
        f"ARTICLE METADATA\n"
        f"- title: {title or ''}\n"
        f"- source_url: {source_url}\n"
        f"- domain: {domain or ''}\n\n"
        f"{lang_line}\n{img_line}\n\n"
        f'RAW ARTICLE TEXT:\n"""\n{body}\n"""\n'
    )
