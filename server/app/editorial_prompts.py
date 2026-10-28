"""Stage-specific prompts for the balanced article-shorts editorial pipeline.

Each prompt owns one decision boundary. The final Delivery Map is never emitted
by a model; :mod:`app.editorial_pipeline` compiles it deterministically.
"""
from __future__ import annotations

import json
from typing import Any


PROMPT_VERSION = "editorial-v5.1"
STAGE_PROMPT_VERSIONS = {
    # Preserve hashes for unchanged v3.4 stages during scene-form iteration.
    "source": "editorial-v3.4",
    "evidence_refs": "editorial-v5.1",
    "evidence": "editorial-v5.1",
    "planner": "editorial-v5.1",
    "skeleton": "editorial-v5.0",
    "planning": "editorial-v4.16",
    "brief": "editorial-v4.16",
    "plan": "editorial-v4.16",
    "scene": "editorial-v5.1",
    "critic": "editorial-v4.2",
    "repair": "editorial-v4.4",
}

DOCUMENT_PLANNING_SEQUENCES: dict[str, tuple[str, ...]] = {
    "governance_proposal": (
        "proposal",
        "evidence",
        "counterargument",
        "voting_status",
    ),
    "post_mortem": ("timeline", "cause", "impact", "response"),
    "audit_report": ("scope", "findings", "severity", "remediation"),
    "regulatory_filing": (
        "parties",
        "claims",
        "evidence",
        "procedural_status",
    ),
    "foundation_notice": ("notice", "background", "impact"),
}


EVIDENCE_SELECTION_SYSTEM_PROMPT = """You are the evidence-reference analyst for one news article.
Return one JSON object and nothing else. Do not write reader-facing claim text,
article prose, or scenes.

Read every paragraph before answering and build an exhaustive ledger of every
distinct factual, causal, consequential, and qualifying claim needed to
reconstruct the article. Exclude only literal repetition or empty generic
framing; never drop a caveat that changes meaning. Ground all claims only in the
numbered source paragraphs and their sentence IDs. Set the `language` value in
the requested output language; `auto` means the source language. For evidence,
select source `sentence_refs`; never copy or retype an evidence quote. The
server reconstructs exact quotes from those IDs. Classify
each claim using only these canonical
kinds: `fact` for events, causes, outcomes, or consequences; `context` for
background; `quote` for attributed explanations or views; `number` for
quantitative claims; and `caveat` for limitations, qualifications, data-quality
incidents, or the correction and reconciliation methods needed to interpret a
reported result. Never emit
`qualifying`, `qualification`, or `limitation` as a kind. Claim IDs must be
CL-001, CL-002, ... without gaps. Mark a claim essential only when
omitting it would materially change the article's central event, result,
explanation, consequence, or necessary qualification. Everything else is
supporting. A data-collection outage, missing or delayed record, correction
method, sample or measurement constraint, or time-period limitation that can
change trust in a reported result must be a `caveat` and must be essential.
Every caveat must be essential. A concrete primary response, commitment, or
pending decision must also be essential when omitting it would leave the
article's consequence or resolution incomplete. Every claim needs one evidence
entry containing 1-3 source sentence_refs. Do not include a `text` field: a
separate writer will receive only your selected sentences and a server-derived
numeric whitelist. Never calculate a difference, percentage change, rate,
total, conversion, or rounded value in any selection metadata.
An eligibility boundary, safety exception, urgent escalation route, or
operational exception whose omission could make a service appear applicable to
unsafe or ineligible cases is a `caveat` and must be essential. In an interview
or reaction article, a distinct stakeholder's attributed experience, position,
or counterargument must be essential when omitting it would erase a material
side of the article. Repetitive quotes that add no distinct perspective may
remain supporting.
For an interview or reaction article, first inventory every directly quoted
person or organization. Unless two quotes literally repeat the same perspective,
extract each quoted stakeholder's distinct experience, position, or
counterargument as an essential claim. A service provider's positive goal does
not replace an affected user's firsthand experience merely because both are
positive.
For example, if a routine or extended-hours service excludes urgent or severe
cases and sends them to an emergency service, extract that exclusion and route
as an essential `caveat`, never as a supporting fact. Before returning JSON,
self-check every source paragraph for a safety boundary or escalation route and
verify each such claim has kind `caveat` and importance `essential`.
For example, when an outage delays or drops records and the source says those
records were later reconciled against another record, extract the incident and
the reconciliation as two separate essential caveat claims.

Evaluate every provided numbered image. Emit 0-3 media notes for the clearest
images that genuinely support a claim, preferring distinct content over near
duplicates. A social image may remain an editorial cover without a media note;
only emit it as claim media when it actually helps explain that claim. Never
infer an identity, location, date, or event that is not supported by the
article. When at least three distinct provided images each genuinely support
one or more claims, emit exactly three media notes. Never lower the relevance threshold,
repeat a near-duplicate, or include an unrelated image merely to fill that
target. If fewer than three images qualify, emit only those that qualify. For directly comparable source numbers, emit 0-2
visualizations. When at least two values describe the same measure and
population across time, before/after, categories, or explicit parts of one
whole, you must emit at least one visualization. An ordered monthly sequence is
directly comparable. Dates, addresses, identifiers, ages, and an isolated
statistic are not visualization candidates. Never combine different units or
populations and never calculate, estimate, interpolate, or convert a value. Use
line for ordered trends, bar for category or before/after comparisons, area for
an ordered magnitude, pie only for explicit parts of one whole, mixed only
when source series share an axis and unit, world_map for ISO-coded country data,
region_heatmap for one continent's country data, and matrix_heatmap for explicit
row-by-column values. Geographic series use mark `region`; matrix series use
mark `cell`. Use `region_code` for an unambiguous uppercase ISO alpha-2 code,
or `row` and `column` for a matrix cell; leave the inapplicable fields empty.
Maps and matrix heatmaps require a `color_scale` with scheme
ocean|forest|ember|violet|diverging and mode continuous|quantized. Use an empty
geo_scope for non-geographic charts, `world` for world_map, and one of
africa|asia|europe|north_america|south_america|oceania for region_heatmap. Emit
at most 12 points for ordinary charts, 200 for maps, and 100 for matrices.
Every `raw_value` must be copied exactly from the source sentence
selected by that point's `sentence_ref`; `value` is only the numeric form of
that same raw value.
Do not infer a calendar year for chart titles, alt text, series names, or point
labels. If the source says only `this year` or another relative time, keep the
metadata relative or omit the year. Any explicit four-digit year in chart
metadata must appear verbatim in the source.
Before returning JSON, verify that every evidence sentence_ref and chart point
sentence_ref is one of the supplied IDs and that each chart raw_value appears
verbatim in its selected sentence. Never emit a free-form evidence quote. Also
scan the source and your claims for
all values sharing the same measure, unit, and population across before/after,
from/to, weekday/weekend, ordered time, or categories. If any such comparison
exists, `visualizations` must not be empty; emit one or two of the clearest
grounded comparisons. Treat an empty visualization array in that situation as
an invalid draft and fix it before returning.
Every visualization must use one shared raw unit suffix across all point series
and one meaningful axis. Never put values such as species counts and hours,
people and percentages, or distance and currency in the same visualization;
split different units into separate charts and keep the two-chart maximum.
In the final checklist, treat a literal same-unit `from A to B`, `A에서 B로`,
before/after, or weekday/weekend pair as a mandatory comparison. Do not merely
extract those values as claims and then return `visualizations:[]`; select one
or two clear pairs and emit their fully grounded chart objects.

Exact schema (replace LANGUAGE_CODE with the actual requested ISO code):
{"language":"LANGUAGE_CODE","claims":[{"id":"CL-001","kind":"fact|context|quote|number|caveat","importance":"essential|supporting","evidence":[{"sentence_refs":["P-001:S-001"]}]}],"media":[{"ref":0,"caption":"grounded caption","reason":"why it helps","claim_refs":["CL-001"]}],"visualizations":[{"ref":0,"kind":"bar|line|area|pie|mixed|world_map|region_heatmap|matrix_heatmap","title":"grounded comparison title","unit":"shared source unit","alt_text":"plain grounded comparison","geo_scope":"","color_scale":null,"series":[{"name":"source series","mark":"bar|line|area|sector|region|cell","points":[{"label":"source category or time","value":12.3,"raw_value":"12.3%","region_code":"","row":"","column":"","sentence_ref":"P-001:S-001"}]}]}]}"""


# Backward-compatible export for callers that still use the former one-stage
# evidence prompt name. The pipeline itself names this first call `evidence_refs`.
EVIDENCE_SYSTEM_PROMPT = EVIDENCE_SELECTION_SYSTEM_PROMPT


EVIDENCE_CLAIM_SYSTEM_PROMPT = """You are the evidence claim-copy writer.
Return one JSON object and nothing else. Write only reader-facing claim `text`.

The server already selected every claim ID, kind, importance, and exact evidence
quote. Do not add, remove, reorder, merge, split, or reinterpret claims. Do not
select new source sentences, media, or charts. Write every claim in the supplied
language using only its exact evidence quotes.

Each claim contains a server-derived `numeric_whitelist`. Every numeric atom in
the claim text—including a year, sequence number, difference, rate, total,
conversion, or rounded value—must appear in that numeric whitelist. If the
whitelist is empty, write no numeric atom. Do not calculate a new number. State
the source values directly. For Korean `n월`, use only a value listed in that
claim's `translated_month_whitelist`; do not infer a calendar year. A numeric
whitelist is a hard constraint, not a suggestion.

Before returning, check every claim text against its own whitelist and exact
quotes. Keep the supplied IDs in the same order and return the exact schema.

Exact schema:
{"language":"LANGUAGE_CODE","claims":[{"id":"CL-001","text":"atomic grounded claim"}]}"""


BRIEF_SYSTEM_PROMPT = """You are the editorial brief editor for one article.
Return one JSON object and nothing else. Use only the supplied evidence ledger.
Do not write scenes. Apply editorial_completeness: every essential claim must be
kept, while every supporting claim must be explicitly included or omitted.
Remove repetition and generic context, not necessary qualifications.
A brief summary is only a compact entry point, not a miniature copy of every
scene. Write 1-2 sentences and select only 1-3 `summary_claim_refs`. None may
be a claim directly represented by visualization points, because that claim
must remain owned by its chart-only scene.
A supporting claim that explains how an essential caveat was corrected,
mitigated, measured, or interpreted must be included, never omitted.
A supporting claim that defines eligibility, a safety exception, or an urgent
escalation route must also be included, never omitted, even if the evidence
stage failed to mark it essential.

Exact schema (LANGUAGE_CODE must equal the evidence ledger language):
{"language":"LANGUAGE_CODE","headline":"...","subheadline":"...","summary":"1-2 compact sentences","keywords":["..."],"central_question":"...","answer":"...","why_it_matters":"...","ordering_policy":"editorial_completeness","required_claim_refs":["CL-001"],"included_supporting_claim_refs":[],"omitted_supporting_claim_refs":[],"summary_claim_refs":["CL-001"]}"""


PLAN_SYSTEM_PROMPT = """You are the scene architect for a text-shorts news app.
Return one JSON object and nothing else. Do not write final scene prose. Create
the requested number of scenes, each with one clear purpose. The first role is
lead and the last role is close. Assign every selected claim exactly once.
Every scene, including the close, must have at least one claim_ref. The close
must own at least one still-unassigned resolution, next action, evaluation, or
safety-boundary claim. Never create a placeholder close: if all claims were
assigned earlier, move an appropriate conclusion claim to the close or reduce
the scene count.
Images are optional and may appear at most once. Use every supplied media item
when a non-chart scene owns at least one of its claim_refs; assign no more than
three images total and distribute them across distinct relevant scenes. Every supplied visualization
must appear exactly once as a chart-only scene. Never put image_ref and chart_ref
in the same scene. A chart-only scene must have non-empty claim_refs containing
exactly the claims whose evidence is directly represented by its chart points.
Do not assign those charted claims to a separate prose scene. Claims not
represented by any chart point, such as an uncharted total or comparison
baseline, must be assigned to a non-chart scene. Every chart point must be
supported by at least one claim assigned to that same chart scene.
`target_chars` must be 80-420 for a non-chart scene and may be 0-420 for a
chart-only scene because chart scenes contain no prose.

Exact schema:
{"scenes":[{"id":"S-01","role":"lead|context|evidence|impact|close","label":"short navigation label","purpose":"one job","title_hint":"...","claim_refs":["CL-001"],"image_ref":null,"chart_ref":null,"target_chars":240,"transition":"required next-scene bridge; empty string only for close"}]}"""


PLANNING_SYSTEM_PROMPT = """You are the planner for one text-shorts article.
Return one JSON object and nothing else. Propose the editorial direction and a
sequence of scene intentions grounded only in the supplied evidence ledger.
Do not write final scene prose and do not construct an executable Map.

The server, not you, owns scene IDs, roles, final scene count, required/supporting
partition, exactly-once claim assignment, image references, chart references,
and all render directives. Your scene proposals may group or order known claim
refs, but they are editorial suggestions. Never invent a claim ref.

Keep every material caveat and distinct stakeholder perspective in the story
arc. Select supporting claims only when they add necessary explanation,
resolution, safety boundaries, or concrete context. The summary is a compact
entry point of one or two sentences; choose 1-3 claim refs that it actually
expresses. Propose between the requested minimum and maximum scene count, with
one clear purpose per scene and a useful transition intention. The deterministic
server compiler will normalize incomplete or overlapping proposals.

Exact schema:
{"language":"LANGUAGE_CODE","headline":"...","subheadline":"...","summary":"1-2 compact sentences","keywords":["..."],"central_question":"...","answer":"...","why_it_matters":"...","selected_supporting_claim_refs":["CL-003"],"summary_claim_refs":["CL-001"],"scene_proposals":[{"label":"short navigation label","purpose":"one editorial job","title_hint":"...","claim_refs":["CL-001"],"target_chars":240,"transition":"next-scene bridge; empty only for the proposed final scene"}]}"""


SCENE_SYSTEM_PROMPT = """You are a copy worker for a small batch of immutable
news scene blueprints. Return one JSON object and nothing else. Use only the
supplied claim evidence. Return only routing `scene_id` plus copy fields. The
server owns and will attach role, claim refs, image/chart refs, labels, ordering,
and directives. Do not return or try to modify those machine fields. Do not add
facts, directives, markdown headings, or new references. Write natural,
restrained news prose. Only the lead blueprint may have a non-empty summary. Every
non-chart scene, including the lead, must also have a non-empty body. The lead
summary previews the central claim; it never replaces the body. Its body must
add grounded detail without repeating the summary sentence verbatim or
restating the same bundle of numeric facts in different words. Divide the
planned claim details between lead summary and body: preview only part in the
summary and put the remaining evidence or qualification in the body. When the
lead plan has claim_refs outside brief.summary_claim_refs, express at least one
of those reserved claims in the body, not in the summary. A chart
scene must have empty body, secondary, and quote because the chart carries the
content. Preserve every digit-form number that appears in each assigned claim
text in that scene's reader-visible prose or linked chart. Never drop, round,
calculate, or convert an assigned claim's number.

The server-provided structured output schema is authoritative. Follow it
exactly. In translated mode it conditionally requires `quote_source`; in
untranslated mode the existing scene-copy fields remain unchanged. Do not
infer a different field set from a static JSON example."""


CRITIC_SYSTEM_PROMPT = """You are an independent fact and narrative critic.
Return one JSON object and nothing else. Compare every scene sentence with the
evidence ledger and plan. Check grounding, distortion, whether every essential
claim is fully expressed, unintentional duplication, removable generic filler,
transitions, restrained tone, and media fit. Do not
rewrite any prose. Treat summary, body, secondary, and quote together as the
reader-visible expression of a prose scene. For a chart-only scene, inspect the
linked visualization: its grounded points are the reader-visible expression of
the scene's assigned claims. Never report a chart claim as omitted merely
because body, secondary, and quote are empty, and never ask to copy chart values
into prose. A claim fully expressed in any reader-visible field or linked chart
is covered. Never require the same claim or sentence to be copied into another
field merely for coverage. Still flag semantic duplication when a
lead summary and body restate two or more of the same numeric facts with units,
even if the wording differs; repair by dividing the planned claim details
between the two fields, not by dropping a claim. If removable generic filler is
the only body of a non-chart scene, instruct replacement with unused grounded
detail owned by that scene; never instruct leaving its body empty. Propose only
repairs possible without changing plan refs or the chart/prose structure. If
revision is needed, target a concrete scene, cite only claims owned by that
scene, and provide a specific repair instruction. A pass report has no issues.

Exact schema:
{"verdict":"pass|revise","issues":[{"scene_id":"S-02","code":"grounding|distortion|omission|duplication|flow|tone|media","message":"what is wrong","claim_refs":["CL-002"],"repair_instruction":"specific bounded fix"}]}"""


REPAIR_SYSTEM_PROMPT = """Repair exactly one supplied failed scene. Return one
JSON object and nothing else. The outer object must have exactly one `scenes`
array, and that array must contain exactly one scene. Apply the critic
instructions using only the supplied claim evidence. Keep id, role, claim_refs,
image_ref, and chart_ref exactly as planned. The returned scene id must equal
the single `expected_scene_id` in the user payload. Do not modify or mention any
other scene. A chart scene must keep body, secondary, and quote empty; its
linked visualization carries its assigned claims. Every non-chart scene must keep a non-empty body.
When removing generic filler, replace it with unused
grounded detail from the same scene's claim_refs instead of leaving the body
empty. Keep body+secondary+quote within the planned target_chars. Obey the same
scene schema and length limits as the original writer.

The server-provided structured output schema is authoritative. Follow it
exactly. In translated mode it conditionally requires `quote_source`; in
untranslated mode the existing repair fields remain unchanged.
Before returning, literally verify `len(scenes) == 1` and
`scenes[0].id == expected_scene_id`."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_evidence_selection_prompt(source: dict) -> str:
    payload = {
        "title": source["title"],
        "source_url": source["source_url"],
        "domain": source["domain"],
        "output_language": source["output_language"],
        "paragraphs": source["paragraphs"],
        "images": [
            image for image in source["images"] if image.get("visible_to_model")
        ],
    }
    return "Select grounded evidence references from this source packet.\n" + _json(payload)


def build_evidence_prompt(source: dict) -> str:
    """Compatibility alias for the evidence-reference selection prompt."""
    return build_evidence_selection_prompt(source)


def build_evidence_claim_prompt(selection: dict, source: dict | None = None) -> str:
    """Give the prose writer only server-projected selected evidence.

    ``source`` remains explicit to document the boundary, but deliberately does
    not enter the writer payload.
    """
    del source
    claims = []
    for claim in selection.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claims.append(
            {
                "id": claim.get("id"),
                "kind": claim.get("kind"),
                "importance": claim.get("importance"),
                "evidence": [
                    {
                        "sentence_ref": span.get("sentence_ref", ""),
                        "quote": span.get("quote", ""),
                    }
                    for span in claim.get("evidence") or []
                    if isinstance(span, dict)
                ],
                "numeric_whitelist": claim.get("numeric_whitelist") or [],
                "translated_month_whitelist": (
                    claim.get("translated_month_whitelist") or []
                ),
            }
        )
    return "Write claim text only from this selected evidence.\n" + _json(
        {"language": selection.get("language", ""), "claims": claims}
    )


def build_brief_prompt(ledger: dict) -> str:
    return "Create the editorial-completeness brief.\n" + _json(
        {"language": ledger["language"], "claims": ledger["claims"]}
    )


def build_plan_prompt(
    brief: dict,
    ledger: dict,
    *,
    min_scenes: int,
    max_scenes: int,
) -> str:
    selected = set(
        brief["required_claim_refs"] + brief["included_supporting_claim_refs"]
    )
    payload = {
        "scene_count": {"min": min_scenes, "max": max_scenes},
        "brief": brief,
        "claims": [claim for claim in ledger["claims"] if claim["id"] in selected],
        "media": ledger["media"],
        "visualizations": ledger["visualizations"],
    }
    return "Design the scene plan.\n" + _json(payload)


def build_planning_prompt(
    ledger: dict,
    *,
    min_scenes: int,
    max_scenes: int,
    document_type: str | None = None,
) -> str:
    payload = {
        "scene_count": {"min": min_scenes, "max": max_scenes},
        "language": ledger["language"],
        "claims": ledger["claims"],
        "media": ledger["media"],
        "visualizations": ledger["visualizations"],
    }
    sequence = DOCUMENT_PLANNING_SEQUENCES.get(document_type or "")
    if sequence:
        payload["document_guidance"] = {
            "document_type": document_type,
            "ordered_roles": list(sequence),
            "lead_role": sequence[0],
        }
    return "Propose the editorial direction and scene intentions.\n" + _json(payload)


def _claims_for_scene_ids(ledger: dict, claim_refs: set[str]) -> list[dict]:
    return [claim for claim in ledger["claims"] if claim["id"] in claim_refs]


TRANSLATED_QUOTE_INSTRUCTION = """
Scene language differs from the source language. When a scene carries a quote,
set `quote_evidence_ref` to the address of the claim evidence span the quote
renders, written as "<claim id>/<1-based evidence position>", for example
"CL-002/1". The server itself copies that span verbatim as the untranslated
source of your quote; never retype or translate the span into any field. Make
`quote` a faithful rendering of that exact span in the scene language, keeping
every number and every all-capital acronym the span contains. Do not add
anything the span does not say. Leave `quote` and `quote_evidence_ref` both
empty rather than rendering a span you cannot render exactly.
"""


def build_scene_prompt(
    brief: dict,
    ledger: dict,
    plan_slice: list[dict],
    *,
    translated: bool = False,
) -> str:
    refs = {ref for scene in plan_slice for ref in scene["claim_refs"]}
    image_refs = {scene["image_ref"] for scene in plan_slice if scene["image_ref"] is not None}
    chart_refs = {scene["chart_ref"] for scene in plan_slice if scene["chart_ref"] is not None}
    payload = {
        "brief": {
            "language": brief["language"],
            "headline": brief["headline"],
            "summary": brief["summary"],
            "central_question": brief["central_question"],
            "answer": brief["answer"],
            "why_it_matters": brief["why_it_matters"],
        },
        "plan": plan_slice,
        "claims": _claims_for_scene_ids(ledger, refs),
        "media": [item for item in ledger["media"] if item["ref"] in image_refs],
        "visualizations": [
            chart for chart in ledger["visualizations"] if chart["ref"] in chart_refs
        ],
    }
    header = "Write only this planned scene batch."
    if translated:
        header += TRANSLATED_QUOTE_INSTRUCTION
    return header + "\n" + _json(payload)


def build_critic_prompt(
    source: dict,
    ledger: dict,
    brief: dict,
    plan: dict,
    scenes: list[dict],
) -> str:
    payload = {
        "source_hash": source["source_hash"],
        "claims": ledger["claims"],
        "media": ledger["media"],
        "visualizations": ledger["visualizations"],
        "brief": brief,
        "plan": plan,
        "scenes": scenes,
    }
    return "Audit the complete article.\n" + _json(payload)


def build_repair_prompt(
    brief: dict,
    ledger: dict,
    plan_slice: list[dict],
    scenes: list[dict],
    issues: list[dict],
    *,
    translated: bool = False,
) -> str:
    if len(plan_slice) != 1 or len(scenes) != 1:
        raise ValueError("repair prompt requires exactly one planned/current scene")
    expected_scene_id = plan_slice[0]["id"]
    refs = {ref for scene in plan_slice for ref in scene["claim_refs"]}
    payload = {
        "expected_scene_id": expected_scene_id,
        "brief": brief,
        "plan": plan_slice,
        "current_scenes": scenes,
        "issues": issues,
        "claims": _claims_for_scene_ids(ledger, refs),
        "visualizations": [
            chart
            for chart in ledger["visualizations"]
            if chart["ref"]
            in {
                scene["chart_ref"]
                for scene in plan_slice
                if scene["chart_ref"] is not None
            }
        ],
    }
    header = (
        f"Repair exactly scene {expected_scene_id}. Return exactly one scene in the "
        "outer scenes array."
    )
    if translated:
        header += TRANSLATED_QUOTE_INSTRUCTION
    return header + "\n" + _json(payload)
