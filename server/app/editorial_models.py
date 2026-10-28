"""Strict wire models shared by the editorial server and macOS desk."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .primary_documents import DOCUMENT_TYPES


EditorRole = Literal["admin", "editor"]
PublicationKind = Literal["editorial", "weekly_insight"]


class EditorialStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EditorOperatorCreateRequest(EditorialStrictModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: EditorRole = "editor"

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("control characters are not allowed")
        return value


class EditorOperatorResponse(EditorialStrictModel):
    id: UUID
    display_name: str
    role: EditorRole
    status: Literal["active", "revoked"]
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class EditorOperatorCreatedResponse(EditorOperatorResponse):
    token: str = Field(min_length=32, max_length=128)


ShortText = Annotated[str, Field(max_length=10_000)]


class EditorialImageInput(EditorialStrictModel):
    image_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    caption: str = Field(default="", max_length=500)
    source_url: str = Field(default="", max_length=2048)


class EditorialChartPointInput(EditorialStrictModel):
    label: str = Field(default="", max_length=500)
    value: float = 0
    raw_value: str = Field(default="", max_length=500)
    evidence: str = Field(default="", max_length=10_000)
    region_code: str = Field(default="", max_length=2)
    row: str = Field(default="", max_length=120)
    column: str = Field(default="", max_length=120)


class EditorialChartSeriesInput(EditorialStrictModel):
    name: str = Field(default="", max_length=500)
    mark: Literal["bar", "line", "area", "sector", "region", "cell"] = "bar"
    points: list[EditorialChartPointInput] = Field(default_factory=list, max_length=200)


class EditorialColorScaleInput(EditorialStrictModel):
    scheme: Literal["ocean", "forest", "ember", "violet", "diverging"] = "ocean"
    mode: Literal["continuous", "quantized"] = "continuous"


class EditorialVisualizationInput(EditorialStrictModel):
    kind: Literal[
        "bar",
        "line",
        "area",
        "pie",
        "mixed",
        "world_map",
        "region_heatmap",
        "matrix_heatmap",
    ] = "bar"
    title: str = Field(default="", max_length=1000)
    unit: str = Field(default="", max_length=100)
    alt_text: str = Field(default="", max_length=5000)
    geo_scope: str = Field(default="", max_length=32)
    color_scale: EditorialColorScaleInput | None = None
    series: list[EditorialChartSeriesInput] = Field(default_factory=list, max_length=4)


class EditorialSceneInput(EditorialStrictModel):
    role: Literal["lead", "context", "evidence", "impact", "close"] = "context"
    label: str = Field(default="", max_length=1000)
    title: str = Field(default="", max_length=5000)
    summary: ShortText = ""
    body: ShortText = ""
    secondary: ShortText = ""
    quote: ShortText = ""
    image_ref: int | None = Field(default=None, ge=0, le=11)
    chart_ref: int | None = Field(default=None, ge=0, le=1)


class EditorialDocument(EditorialStrictModel):
    format_version: Literal[3] = 3
    publication_kind: PublicationKind = "editorial"
    source_name: str = Field(default="결 편집부", max_length=200)
    language: str = Field(default="ko", max_length=32)
    headline: str = Field(default="", max_length=10_000)
    subheadline: str = Field(default="", max_length=10_000)
    summary: str = Field(default="", max_length=10_000)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    images: list[EditorialImageInput] = Field(default_factory=list, max_length=12)
    cover_image_ref: int | None = Field(default=None, ge=0, le=11)
    visualizations: list[EditorialVisualizationInput] = Field(
        default_factory=list, max_length=2
    )
    scenes: list[EditorialSceneInput] = Field(default_factory=list, max_length=9)
    # F-3 (DR-13254). Empty is an ordinary article, which is the default and
    # what every existing draft is. Setting it promotes the publication to a
    # primary document, which is exactly what feed.py already projects as
    # `document_type` + `source_publisher`.
    document_type: str = Field(default="", max_length=64)
    document_source_name: str = Field(default="", max_length=200)

    @field_validator("document_type")
    @classmethod
    def known_document_type(cls, value: str) -> str:
        """Reject unknown types here rather than let feed.py drop them.

        feed.py filters on a whitelist, so a typo would publish silently as an
        ordinary article and the editor would never learn why.
        """
        cleaned = value.strip()
        if cleaned and cleaned not in DOCUMENT_TYPES:
            allowed = ", ".join(sorted(DOCUMENT_TYPES))
            raise ValueError(f"document_type must be empty or one of: {allowed}")
        return cleaned

    @field_validator("keywords")
    @classmethod
    def bounded_keywords(cls, values: list[str]) -> list[str]:
        if any(len(value) > 120 for value in values):
            raise ValueError("keywords must be at most 120 characters")
        return values


class EditorialDraftCreateRequest(EditorialStrictModel):
    document: EditorialDocument


class EditorialDraftUpdateRequest(EditorialStrictModel):
    expected_version: int = Field(ge=1)
    document: EditorialDocument


class EditorialDraftVersionRequest(EditorialStrictModel):
    expected_version: int = Field(ge=1)


class EditorialDraftResponse(EditorialStrictModel):
    id: UUID
    status: Literal["draft", "published", "withdrawn"]
    document: EditorialDocument
    version: int
    created_by: UUID
    updated_by: UUID
    deleted_at: datetime | None = None
    deleted_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class EditorialValidationResponse(EditorialStrictModel):
    valid: bool
    errors: list[str]
    map: dict


class EditorialAssetResponse(EditorialStrictModel):
    image_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    byte_size: int = Field(gt=0)
    url: str


class EditorialVersionRequest(EditorialStrictModel):
    expected_version: int | None = Field(default=None, ge=1)


class EditorialScheduleUpsertRequest(EditorialStrictModel):
    expected_draft_version: int = Field(ge=1)
    expected_schedule_version: int | None = Field(default=None, ge=1)
    scheduled_for: datetime

    @field_validator("scheduled_for")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_for must include a timezone offset")
        return value


class EditorialScheduleVersionRequest(EditorialStrictModel):
    expected_schedule_version: int = Field(ge=1)


EditorialScheduleState = Literal[
    "scheduled", "processing", "published", "cancelled", "failed"
]


class EditorialScheduleResponse(EditorialStrictModel):
    id: UUID
    draft_id: UUID
    version: int = Field(gt=0)
    state: EditorialScheduleState
    scheduled_for: datetime
    snapshot_draft_version: int = Field(gt=0)
    created_by: UUID
    updated_by: UUID
    attempt_count: int = Field(ge=0)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None = None
    published_at: datetime | None = None


class EditorialRevisionResponse(EditorialStrictModel):
    id: UUID
    draft_id: UUID
    revision_number: int = Field(gt=0)
    document: EditorialDocument
    map_data: dict
    created_by: UUID
    created_at: datetime


class EditorialPublicationResponse(EditorialStrictModel):
    draft_id: UUID
    article_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_id: UUID
    state: Literal["published", "withdrawn"]
    published_at: datetime
    published_by: UUID
    withdrawn_at: datetime | None = None
    withdrawn_by: UUID | None = None
    display_order: int | None = Field(default=None, ge=0)
    is_featured: bool = False
    updated_at: datetime
    draft_version: int = Field(gt=0)


class EditorialPublishResponse(EditorialStrictModel):
    publication: EditorialPublicationResponse
    revision: EditorialRevisionResponse
    map: dict


class EditorialTodayOrderRequest(EditorialStrictModel):
    ordered_article_ids: list[str] = Field(default_factory=list, max_length=100)
    featured_article_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("ordered_article_ids")
    @classmethod
    def valid_unique_article_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("ordered_article_ids must be unique")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("ordered_article_ids must be sha256 article IDs")
        return values


class EditorialTodayItem(EditorialStrictModel):
    draft_id: UUID
    article_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    headline: str
    published_at: datetime
    display_order: int | None = Field(default=None, ge=0)
    is_featured: bool


class EditorialTodayResponse(EditorialStrictModel):
    editorial_day: str
    items: list[EditorialTodayItem]


@dataclass(frozen=True, slots=True)
class EditorPrincipal:
    id: UUID
    display_name: str
    role: EditorRole
    created_at: datetime
    last_used_at: datetime | None
