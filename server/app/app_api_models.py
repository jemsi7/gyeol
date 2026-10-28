"""Strict wire models for the iOS/iPadOS app API."""
from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeviceInfo(StrictModel):
    installation_id: UUID
    vendor_id: UUID | None = None
    platform: Literal["ios", "ipados"]
    model: str = Field(min_length=1, max_length=64)
    os_version: str = Field(min_length=1, max_length=64)
    app_version: str = Field(min_length=1, max_length=64)
    build_number: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=1, max_length=64)

    @field_validator(
        "model", "os_version", "app_version", "build_number", "locale"
    )
    @classmethod
    def no_control_lines(cls, value: str) -> str:
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("control characters are not allowed")
        return value


class ChallengeRequest(StrictModel):
    purpose: Literal["attestation", "assertion"]
    key_id: str = Field(min_length=16, max_length=256)
    device: DeviceInfo


class ChallengeResponse(StrictModel):
    challenge_id: UUID
    challenge: str
    expires_at: datetime


class RegisterRequest(StrictModel):
    challenge_id: UUID
    key_id: str = Field(min_length=16, max_length=256)
    attestation_object: str = Field(min_length=16, max_length=262_144)
    device: DeviceInfo


class RegisterResponse(StrictModel):
    registered: bool
    key_id: str


class FeedMode(StrEnum):
    RECENT = "recent"
    RANDOM = "random"
    LATEST = "latest"


class FeedPayload(StrictModel):
    device: DeviceInfo
    limit: int = Field(default=5, ge=1, le=5)
    cursor: str | None = Field(default=None, max_length=1024)
    mode: FeedMode = FeedMode.RECENT
    exclude_article_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("exclude_article_ids")
    @classmethod
    def validate_exclude_article_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("exclude_article_ids must be unique")
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("exclude_article_ids must contain lowercase sha256 ids")
        return values

    @model_validator(mode="after")
    def validate_feed_mode_fields(self):
        if self.mode in {FeedMode.RANDOM, FeedMode.LATEST} and self.cursor is not None:
            raise ValueError(f"{self.mode.value} feed does not accept a cursor")
        if self.mode is FeedMode.RECENT and self.exclude_article_ids:
            raise ValueError("recent feed does not accept exclusions")
        return self


class AssertionEnvelope(StrictModel):
    challenge_id: UUID
    key_id: str = Field(min_length=16, max_length=256)
    assertion: str = Field(min_length=16, max_length=131_072)
    issued_at: int = Field(gt=0)


class AuthenticatedFeedRequest(StrictModel):
    payload: FeedPayload
    auth: AssertionEnvelope


class FeedItem(StrictModel):
    article_id: str
    source_url: str | None = None
    domain: str | None = None
    published_at: datetime | None = None
    source_language: str | None = None
    source_country: str | None = None
    document_type: str = ""
    source_publisher: str = ""
    status: Literal["ok", "degraded"]
    map: dict[str, Any]


class FeedResponse(StrictModel):
    format_version: Literal[1] = 1
    items: list[FeedItem]
    next_cursor: str | None = None
    cooldown_seconds: float


class AccountRecommendationSurface(StrEnum):
    HOME = "home"
    ARTICLE_BOUNDARY = "article_boundary"


class RecommendationTimeContext(StrictModel):
    occurred_at: datetime
    time_zone: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_+./-]+$",
    )
    utc_offset_minutes: int = Field(ge=-720, le=840)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class AccountRecommendationRequest(StrictModel):
    surface: AccountRecommendationSurface
    context_article_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    exclude_article_ids: list[str] = Field(default_factory=list, max_length=64)
    time_context: RecommendationTimeContext
    limit: int = Field(default=5, ge=1, le=5)
    cursor: str | None = Field(default=None, max_length=2048)

    @field_validator("exclude_article_ids")
    @classmethod
    def valid_exclude_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("exclude_article_ids must be unique")
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values):
            raise ValueError("exclude_article_ids must contain lowercase sha256 ids")
        return values

    @model_validator(mode="after")
    def surface_fields_match(self):
        if self.surface is AccountRecommendationSurface.ARTICLE_BOUNDARY:
            if (
                self.context_article_id is None
                or self.limit != 3
                or self.cursor is not None
            ):
                raise ValueError(
                    "article_boundary requires context, limit 3, and no cursor"
                )
        return self


class AccountRecommendationResponse(StrictModel):
    format_version: Literal[1] = 1
    surface: AccountRecommendationSurface
    strategy: Literal["personalized", "blended_cold_start"]
    ranking_version: Literal["content-v1"]
    generated_at: datetime
    items: list[FeedItem] = Field(max_length=5)
    next_cursor: str | None = Field(default=None, max_length=2048)


class DiscoveryPayload(StrictModel):
    device: DeviceInfo
    query: str | None = Field(default=None, min_length=2, max_length=80)
    collection: Literal["editorial", "weekly_insight"] | None = None
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=12, ge=1, le=20)

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        value = " ".join(value.split())
        return value or None

    @model_validator(mode="after")
    def valid_discovery_mode(self):
        if self.query is not None and self.collection is not None:
            raise ValueError("query and collection are mutually exclusive")
        if self.cursor is not None and self.collection is None:
            raise ValueError("cursor requires a publication collection")
        return self


class AuthenticatedDiscoveryRequest(StrictModel):
    payload: DiscoveryPayload
    auth: AssertionEnvelope


class PrivacyConsentPayload(StrictModel):
    policy_version: str = Field(min_length=5, max_length=32)
    device: DeviceInfo

    @field_validator("policy_version")
    @classmethod
    def version_has_no_controls(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("control characters are not allowed")
        return value


class AuthenticatedPrivacyConsentRequest(StrictModel):
    payload: PrivacyConsentPayload
    auth: AssertionEnvelope


class PrivacyConsentResponse(StrictModel):
    consent_id: UUID
    policy_version: str
    policy_url: str
    accepted_at: datetime


class AccountPrivacyConsentLink(StrictModel):
    consent_id: UUID
    account_id: UUID
    policy_version: str
    policy_url: str
    accepted_at: datetime
    linked_at: datetime


class ArticleViewPayload(StrictModel):
    event_id: UUID
    article_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    device: DeviceInfo


class AuthenticatedArticleViewRequest(StrictModel):
    payload: ArticleViewPayload
    auth: AssertionEnvelope


class ArticleViewResponse(StrictModel):
    article_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    counted: bool
    view_count: int = Field(ge=0)


class DiscoveryRouteItem(StrictModel):
    position: int = Field(ge=1, le=4)
    role: Literal["start", "context", "evidence", "counterpoint"]
    reason: str = Field(min_length=20, max_length=180)
    lasting_value: str = Field(min_length=20, max_length=180)
    article: FeedItem


class DiscoveryRoute(StrictModel):
    id: UUID
    rank: int = Field(ge=1, le=8)
    slug: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=4, max_length=100)
    summary: str = Field(min_length=20, max_length=300)
    topic: str = Field(min_length=2, max_length=60)
    is_representative: bool
    items: list[DiscoveryRouteItem] = Field(min_length=4, max_length=4)


class DiscoverySearchResult(StrictModel):
    depth: Literal["quick", "deep"]
    retrieval: Literal["keyword", "rag"] = "keyword"
    article: FeedItem


class FeaturedTimelineNode(StrictModel):
    id: UUID
    lane: Literal["main", "branch"]
    anchor_node_id: UUID | None = None
    update_kind: Literal["existing", "inserted", "occurred"]
    published_at: datetime
    date_source: Literal["provider", "first_seen"]
    article: FeedItem

    @model_validator(mode="after")
    def anchor_matches_lane(self):
        if (self.lane == "main") != (self.anchor_node_id is None):
            raise ValueError("timeline branch anchor does not match lane")
        return self


class FeaturedTimeline(StrictModel):
    id: UUID
    rank: int = Field(ge=1, le=8)
    stable_key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=4, max_length=100)
    summary: str = Field(min_length=20, max_length=240)
    topic: str = Field(min_length=1, max_length=100)
    started_at: datetime
    ended_at: datetime
    nodes: list[FeaturedTimelineNode] = Field(min_length=4, max_length=8)

    @model_validator(mode="after")
    def valid_timeline_tree(self):
        if self.started_at > self.ended_at:
            raise ValueError("timeline period is reversed")
        node_ids = {node.id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("timeline node IDs must be unique")
        if any(
            node.anchor_node_id is not None and node.anchor_node_id not in node_ids
            for node in self.nodes
        ):
            raise ValueError("timeline branch anchor must exist")
        if sum(node.lane == "main" for node in self.nodes) < 3:
            raise ValueError("timeline requires at least three main nodes")
        return self


class DiscoveryResponse(StrictModel):
    format_version: Literal[1] = 1
    generated_at: datetime | None = None
    expires_at: datetime | None = None
    representative_route_id: UUID | None = None
    routes: list[DiscoveryRoute] = Field(default_factory=list, max_length=8)
    search_suggestions: list[str] = Field(default_factory=list, max_length=8)
    trending_search_suggestions: list[str] = Field(
        default_factory=list,
        max_length=3,
    )
    query: str | None = None
    collection: Literal["editorial", "weekly_insight"] | None = None
    results: list[DiscoverySearchResult] = Field(default_factory=list, max_length=25)
    timeline_status: Literal["fresh", "stale", "unavailable"] = "unavailable"
    timelines_generated_at: datetime | None = None
    timelines_refreshed_at: datetime | None = None
    featured_timelines: list[FeaturedTimeline] = Field(
        default_factory=list,
        max_length=8,
    )
    timeline_results: list[FeaturedTimeline] = Field(default_factory=list, max_length=8)
    next_cursor: str | None = None
    cooldown_seconds: float

    @field_validator("trending_search_suggestions")
    @classmethod
    def valid_trending_search_suggestions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        identities: set[str] = set()
        for value in values:
            if not 2 <= len(value) <= 60:
                raise ValueError("trending search suggestions must be 2..60 characters")
            if any(character in value for character in ("\n", "\r", "\x00")):
                raise ValueError("control characters are not allowed")
            identity = " ".join(value.casefold().split())
            if identity in identities:
                raise ValueError("trending search suggestions must be unique")
            identities.add(identity)
            normalized.append(value)
        return normalized


class AppleAccountSignInRequest(StrictModel):
    identity_token: str = Field(min_length=16, max_length=32_768)
    authorization_code: str = Field(min_length=8, max_length=16_384)
    nonce: str = Field(min_length=16, max_length=512)
    given_name: str | None = Field(default=None, max_length=100)
    family_name: str | None = Field(default=None, max_length=100)
    privacy_consent_id: UUID

    @field_validator("given_name", "family_name")
    @classmethod
    def safe_person_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("control characters are not allowed")
        return value


class AccountProfile(StrictModel):
    id: UUID
    display_name: str | None = None
    email: str | None = None


class AccountSessionResponse(StrictModel):
    session_token: str = Field(min_length=32, max_length=128)
    expires_at: datetime
    account: AccountProfile


class AccountReaderSettings(StrictModel):
    type_size: Literal["small", "standard", "large"] = "standard"
    leading: Literal["compact", "comfortable"] = "comfortable"
    theme: Literal["system", "light", "dark"] = "light"
    motion_enabled: bool = True
    media_saver: bool = False
    offline_cache: bool = True


class SavedArticleChange(StrictModel):
    article_id: str = Field(min_length=1, max_length=128)
    saved: bool
    saved_at: datetime | None = None

    @field_validator("article_id")
    @classmethod
    def valid_article_id(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def saved_at_matches_operation(self):
        if self.saved != (self.saved_at is not None):
            raise ValueError("saved_at must be present exactly when saved is true")
        if self.saved_at is not None and (
            self.saved_at.tzinfo is None or self.saved_at.utcoffset() is None
        ):
            raise ValueError("saved_at must include a timezone")
        return self


class ProgressChange(StrictModel):
    article_id: str = Field(min_length=1, max_length=128)
    scene_index: int = Field(ge=0, le=999)

    @field_validator("article_id")
    @classmethod
    def valid_article_id(cls, value: str) -> str:
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError("control characters are not allowed")
        return value


class AccountReadingStatePatch(StrictModel):
    saved_changes: list[SavedArticleChange] = Field(default_factory=list, max_length=100)
    progress_changes: list[ProgressChange] = Field(default_factory=list, max_length=500)
    settings: AccountReaderSettings | None = None
    update_last_article: bool = False
    last_article_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("last_article_id")
    @classmethod
    def valid_last_article_id(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\n", "\r", "\x00")
        ):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def valid_patch(self):
        saved_ids = [change.article_id for change in self.saved_changes]
        progress_ids = [change.article_id for change in self.progress_changes]
        if len(set(saved_ids)) != len(saved_ids):
            raise ValueError("saved article changes must be unique")
        if len(set(progress_ids)) != len(progress_ids):
            raise ValueError("progress changes must be unique")
        if not self.update_last_article and self.last_article_id is not None:
            raise ValueError("last_article_id requires update_last_article")
        if not (
            self.saved_changes
            or self.progress_changes
            or self.settings is not None
            or self.update_last_article
        ):
            raise ValueError("reading state patch must contain a change")
        return self


class AccountSavedArticle(StrictModel):
    saved_at: datetime
    article: FeedItem


class AccountReadingStateResponse(StrictModel):
    revision: int = Field(ge=0)
    saved_articles: list[AccountSavedArticle] = Field(default_factory=list, max_length=100)
    scene_progress: dict[str, int] = Field(default_factory=dict, max_length=500)
    last_article_id: str | None = None
    settings: AccountReaderSettings


class AccountActivityEventType(StrEnum):
    SEARCH = "search"
    ARTICLE_OPENED = "article_opened"
    SCENE_VIEWED = "scene_viewed"
    ARTICLE_COMPLETED = "article_completed"


class AccountActivityEvent(StrictModel):
    event_id: UUID
    event_type: AccountActivityEventType
    occurred_at: datetime
    time_zone: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_+./-]+$",
    )
    utc_offset_minutes: int = Field(ge=-720, le=840)
    query: str | None = Field(default=None, min_length=2, max_length=80)
    result_count: int | None = Field(default=None, ge=0, le=1000)
    article_id: str | None = Field(default=None, min_length=1, max_length=128)
    scene_index: int | None = Field(default=None, ge=0, le=999)
    scene_count: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("query", "article_id")
    @classmethod
    def no_activity_control_lines(cls, value: str | None) -> str | None:
        if value is not None and any(
            character in value for character in ("\n", "\r", "\x00")
        ):
            raise ValueError("control characters are not allowed")
        return value

    @model_validator(mode="after")
    def event_fields_match_type(self):
        if self.event_type is AccountActivityEventType.SEARCH:
            if (
                self.query is None
                or self.result_count is None
                or self.article_id is not None
                or self.scene_index is not None
                or self.scene_count is not None
            ):
                raise ValueError("search event fields do not match event_type")
            return self

        if (
            self.query is not None
            or self.result_count is not None
            or self.article_id is None
            or self.scene_index is None
            or self.scene_count is None
            or self.scene_index >= self.scene_count
        ):
            raise ValueError("article event fields do not match event_type")
        if (
            self.event_type is AccountActivityEventType.ARTICLE_COMPLETED
            and self.scene_index != self.scene_count - 1
        ):
            raise ValueError("completed event must reference the last scene")
        return self


class AccountActivityEventBatch(StrictModel):
    events: list[AccountActivityEvent] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def event_ids_are_unique(self):
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event_id values must be unique within a batch")
        return self


class AccountActivityEventBatchResponse(StrictModel):
    accepted: int = Field(ge=0)
    duplicates: int = Field(ge=0)


class AccountDeletionResponse(StrictModel):
    deleted: Literal[True] = True
    apple_token_revoked: bool


class ArticleFeedbackReason(StrEnum):
    NOT_RELEVANT = "not_relevant"
    TOO_SIMPLE = "too_simple"
    TOO_DIFFICULT = "too_difficult"
    SEEN_SIMILAR = "seen_similar"
    DISTRUST_SOURCE = "distrust_source"
    OTHER = "other"


class ArticleFeedbackRequest(StrictModel):
    reason: ArticleFeedbackReason


class ArticleFeedbackResponse(StrictModel):
    article_id: str
    reason: ArticleFeedbackReason
    created_at: datetime
    updated_at: datetime


class PushPreferences(StrictModel):
    editorial: bool = True
    weekly_insight: bool = False
    timeline_updates: bool = False
    timeline_refreshes: bool | None = None
    timeline_additions: bool | None = None

    @model_validator(mode="after")
    def normalize_timeline_preferences(self):
        legacy = self.timeline_updates
        if self.timeline_refreshes is None:
            self.timeline_refreshes = legacy
        if self.timeline_additions is None:
            self.timeline_additions = legacy
        self.timeline_updates = bool(self.timeline_refreshes or self.timeline_additions)
        if not self.editorial and not self.weekly_insight and not self.timeline_updates:
            raise ValueError("at least one push preference must be enabled")
        return self


class PushRegistrationPayload(StrictModel):
    installation_id: UUID
    device_token: str = Field(min_length=16, max_length=512)
    environment: Literal["development", "production"]
    preferences: PushPreferences = Field(default_factory=PushPreferences)
    device: DeviceInfo


class AuthenticatedPushRegistrationRequest(StrictModel):
    payload: PushRegistrationPayload
    auth: AssertionEnvelope


class PushRegistrationResponse(StrictModel):
    active: Literal[True] = True
    installation_id: UUID
    environment: Literal["development", "production"]
    preferences: PushPreferences = Field(default_factory=PushPreferences)


class TimelineSubscriptionResponse(StrictModel):
    stable_key: str = Field(min_length=1, max_length=128)
    subscribed: Literal[True] = True
    subscribed_at: datetime


class TimelineSubscriptionDetail(StrictModel):
    stable_key: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=100)
    summary: str | None = Field(default=None, max_length=240)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    subscribed_at: datetime


class TimelineSubscriptionListResponse(StrictModel):
    stable_keys: list[str] = Field(default_factory=list, max_length=64)
    subscriptions: list[TimelineSubscriptionDetail] = Field(
        default_factory=list, max_length=64
    )


class TimelineSubscriptionHistoryEvent(StrictModel):
    id: UUID
    stable_key: str = Field(min_length=1, max_length=128)
    event_type: Literal["subscribe", "unsubscribe"]
    occurred_at: datetime
    is_currently_subscribed: bool
    title: str | None = Field(default=None, max_length=100)


class TimelineSubscriptionHistoryResponse(StrictModel):
    events: list[TimelineSubscriptionHistoryEvent] = Field(
        default_factory=list, max_length=100
    )


class APIErrorDetail(StrictModel):
    code: str
    message: str
    retry_after_seconds: float | None = None


class APIError(StrictModel):
    error: APIErrorDetail
