"""Additive PostgreSQL namespace and least-privilege role for the app staging API.

This module never runs from application startup.  An operator invokes the
separate provisioning command with the production database-owner connection,
and the staging service receives only the resulting restricted runtime DSN.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import os
from typing import Any, Final

from pydantic import SecretStr

STAGING_SCHEMA: Final[str] = "staging_app"
STAGING_SCHEMA_VERSION: Final[int] = 12
STAGING_SCHEMA_V1_CHECKSUM: Final[str] = (
    "8ac05aeba8ba83bfb850f593ceafbaec191617206f1ab0a17ca1d1c0a1e11f0b"
)
STAGING_SCHEMA_V2_CHECKSUM: Final[str] = (
    "06b1a4351fe4d6ccbf71b990250c7bc5552da0e480f5feda7c7003e2d9c7b137"
)
STAGING_SCHEMA_V3_CHECKSUM: Final[str] = (
    "df5334177dd6d1364b796c7812776d453cb6849b1121e2aac8627e6340c2e976"
)
STAGING_SCHEMA_V4_CHECKSUM: Final[str] = (
    "7c20c388a64d62f9e857873a3ef41ae113fcbda5eb93f54d9544d701719ee742"
)
STAGING_SCHEMA_V5_CHECKSUM: Final[str] = (
    "25fcc714483083556082c126c2dd66e498cb6f018548577530c0eea87d85fc5f"
)
STAGING_SCHEMA_V6_CHECKSUM: Final[str] = (
    "c7024d96fd23aac61d1f10414710eb469ae43b9ae730984ec7e7473f0ec6e8d1"
)
STAGING_SCHEMA_V7_CHECKSUM: Final[str] = (
    "bc4e73fee9cd251749ef8182853f0e248271b0d7e26e61c9930905b39f82abee"
)
STAGING_SCHEMA_V8_CHECKSUM: Final[str] = (
    "08433a77c7164e7c3aff33933a2422d1af77ca0f43f6123d3cdf9f0446c00dec"
)
STAGING_SCHEMA_V9_CHECKSUM: Final[str] = (
    "feaa1f51641ffd223d9b6ae4a86ca03a1436a756efcc89436a5c33159bc9b85f"
)
STAGING_SCHEMA_V10_CHECKSUM: Final[str] = (
    "47d6daa6f9803de0964b8cc3de110070aa5fb78e03aca8e5f215f023a8cb67aa"
)
STAGING_SCHEMA_V11_CHECKSUM: Final[str] = (
    "6c991edb1345121f526786c199d6a5eab35a494601272e03833afdbd01d1dd53"
)
STAGING_MIGRATION_TABLE: Final[str] = "staging_schema_migrations"
STAGING_MIGRATOR_ROLE: Final[str] = "gyeol_staging_migrator"
STAGING_RUNTIME_ROLE: Final[str] = "gyeol_staging_runtime"
STAGING_CONFIRMATION: Final[str] = (
    "devoted-commitment/production/staging_app"
)
STAGING_ADVISORY_LOCK_KEY: Final[int] = 0x4759454F4C535447

STAGING_V6_SHARED_VIEWS: tuple[str, ...] = (
    "explore_timeline_catalogs",
    "explore_timelines",
    "explore_timeline_nodes",
)

SHARED_READ_VIEWS: tuple[str, ...] = (
    "articles",
    "article_images",
    "article_image_links",
    "article_maps",
    "article_recommendation_features",
    "editorial_revisions",
    "editorial_publications",
    "article_curation_catalogs",
    "article_curation_routes",
    "article_curation_items",
    *STAGING_V6_SHARED_VIEWS,
)

STAGING_STATE_TABLES: tuple[str, ...] = (
    "article_view_events",
    "article_view_counts",
    "article_feed_impression_counts",
    "app_attest_challenges",
    "app_attest_keys",
    "app_privacy_consents",
    "app_accounts",
    "app_privacy_consent_accounts",
    "app_account_sessions",
    "app_account_session_activity_days",
    "app_guest_session_activity_days",
    "app_article_feedback",
    "app_account_reading_states",
    "article_bookmark_events",
    "app_account_activity_events",
    "app_recommendation_exposures",
    "app_article_list_exposures",
    "app_push_registrations",
    "app_timeline_subscriptions",
    "app_timeline_subscription_events",
    "editorial_push_notifications",
    "editorial_push_deliveries",
    "explore_timeline_push_notifications",
    "explore_timeline_push_deliveries",
    "app_request_cooldowns",
    "trending_search_snapshots",
)

STAGING_V5_STATE_TABLES: tuple[str, ...] = (
    "article_feed_impression_counts",
    "app_account_session_activity_days",
    "app_article_list_exposures",
)

STAGING_V7_STATE_TABLES: tuple[str, ...] = (
    "app_timeline_subscriptions",
)

STAGING_V8_STATE_TABLES: tuple[str, ...] = (
    "app_timeline_subscription_events",
)

STAGING_V9_STATE_TABLES: tuple[str, ...] = (
    "app_guest_session_activity_days",
)

STAGING_V10_STATE_TABLES: tuple[str, ...] = (
    "explore_timeline_push_notifications",
    "explore_timeline_push_deliveries",
)

REQUIRED_RELATION_COLUMNS: dict[str, tuple[str, ...]] = {
    "articles": (
        "id",
        "url",
        "title",
        "seendate",
        "domain",
        "language",
        "sourcecountry",
        "url_mobile",
        "socialimage_url",
        "source_query",
        "image_id",
        "raw",
        "first_seen_at",
        "updated_at",
        "coverage_domain",
        "document_type",
        "document_source_name",
    ),
    "article_images": (
        "id",
        "source_url",
        "content_type",
        "byte_size",
        "backend",
        "storage_ref",
        "data",
        "downloaded_at",
    ),
    "article_image_links": (
        "article_id",
        "ref_index",
        "image_id",
        "role",
        "source_url",
    ),
    "article_maps": (
        "article_id",
        "format_version",
        "model",
        "status",
        "data",
        "error",
        "created_at",
        "updated_at",
    ),
    "article_recommendation_features": (
        "article_id",
        "feature_version",
        "terms",
        "term_weights",
        "representative_topic",
        "domain",
        "published_at",
        "quality_score",
        "updated_at",
    ),
    "editorial_revisions": (
        "id",
        "document",
    ),
    "editorial_publications": (
        "draft_id",
        "article_id",
        "revision_id",
        "state",
        "published_at",
        "published_by",
        "withdrawn_at",
        "withdrawn_by",
        "display_order",
        "is_featured",
        "updated_at",
    ),
    "article_curation_catalogs": (
        "id",
        "editorial_day",
        "input_hash",
        "status",
        "candidate_count",
        "model",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "lease_expires_at",
        "error_code",
        "search_suggestions",
        "generated_at",
        "valid_until",
        "created_at",
        "updated_at",
    ),
    "article_curation_routes": (
        "id",
        "catalog_id",
        "rank",
        "slug",
        "title",
        "summary",
        "topic",
        "is_representative",
        "created_at",
    ),
    "article_curation_items": (
        "route_id",
        "position",
        "article_id",
        "role",
        "reason",
        "lasting_value",
        "quality",
    ),
    "explore_timeline_catalogs": (
        "id",
        "input_hash",
        "cluster_snapshot_id",
        "cluster_slot",
        "candidate_slot",
        "status",
        "candidate_count",
        "model",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "lease_expires_at",
        "error_code",
        "generated_at",
        "valid_until",
        "created_at",
        "updated_at",
    ),
    "explore_timelines": (
        "id",
        "catalog_id",
        "stable_key",
        "cluster_key",
        "rank",
        "title",
        "summary",
        "topic",
        "started_at",
        "ended_at",
        "created_at",
    ),
    "explore_timeline_nodes": (
        "row_id",
        "node_id",
        "timeline_id",
        "article_id",
        "anchor_node_id",
        "lane",
        "display_order",
        "update_kind",
        "relevance_score",
        "published_at_snapshot",
        "date_source",
        "created_at",
    ),
    "staging_schema_migrations": (
        "version",
        "checksum",
        "applied_at",
    ),
    "article_view_events": (
        "id",
        "article_id",
        "received_at",
    ),
    "article_view_counts": (
        "article_id",
        "view_count",
        "first_viewed_at",
        "last_viewed_at",
        "updated_at",
    ),
    "article_feed_impression_counts": (
        "article_id",
        "impression_count",
        "first_impressed_at",
        "last_impressed_at",
        "updated_at",
    ),
    "app_attest_challenges": (
        "id",
        "installation_id",
        "key_id",
        "purpose",
        "challenge",
        "expires_at",
        "used_at",
        "created_at",
    ),
    "app_attest_keys": (
        "key_id",
        "installation_id",
        "public_key_pem",
        "receipt",
        "environment",
        "sign_count",
        "device",
        "validation_category",
        "bundle_version",
        "status",
        "last_feed_at",
        "created_at",
        "updated_at",
        "last_seen_at",
    ),
    "app_privacy_consents": (
        "id",
        "installation_id",
        "key_id",
        "policy_version",
        "policy_url",
        "app_version",
        "build_number",
        "accepted_at",
    ),
    "app_accounts": (
        "id",
        "apple_subject",
        "email",
        "given_name",
        "family_name",
        "apple_refresh_token",
        "created_at",
        "updated_at",
        "last_signed_in_at",
    ),
    "app_privacy_consent_accounts": (
        "consent_id",
        "account_id",
        "linked_at",
    ),
    "app_account_sessions": (
        "id",
        "account_id",
        "token_hash",
        "expires_at",
        "created_at",
        "last_seen_at",
    ),
    "app_account_session_activity_days": (
        "account_id",
        "active_date",
        "first_seen_at",
        "last_seen_at",
    ),
    "app_guest_session_activity_days": (
        "installation_hash",
        "active_date",
        "first_seen_at",
        "last_seen_at",
    ),
    "app_article_feedback": (
        "account_id",
        "article_id",
        "reason",
        "created_at",
        "updated_at",
    ),
    "app_account_reading_states": (
        "account_id",
        "revision",
        "saved_articles",
        "scene_progress",
        "last_article_id",
        "settings",
        "created_at",
        "updated_at",
    ),
    "article_bookmark_events": (
        "id",
        "account_id",
        "article_id",
        "event_type",
        "received_at",
    ),
    "app_account_activity_events": (
        "id",
        "account_id",
        "event_type",
        "occurred_at",
        "time_zone",
        "utc_offset_minutes",
        "local_date",
        "local_hour",
        "query",
        "result_count",
        "article_id",
        "scene_index",
        "scene_count",
        "received_at",
    ),
    "app_recommendation_exposures": (
        "request_id",
        "account_id",
        "article_id",
        "ranking_version",
        "surface",
        "page_kind",
        "position",
        "lane",
        "served_at",
        "selected_at",
    ),
    "app_article_list_exposures": (
        "response_id",
        "position",
        "account_id",
        "article_id",
        "surface",
        "served_at",
    ),
    "trending_search_snapshots": (
        "bucket_started_at",
        "category_keywords",
        "keyword_pool",
        "suggestions",
        "generated_at",
    ),
    "app_push_registrations": (
        "id",
        "account_id",
        "session_id",
        "installation_id",
        "key_id",
        "token_hash",
        "token_ciphertext",
        "environment",
        "editorial_enabled",
        "weekly_insight_enabled",
        "timeline_updates_enabled",
        "timeline_refreshes_enabled",
        "timeline_additions_enabled",
        "status",
        "last_error",
        "invalidated_at",
        "created_at",
        "updated_at",
        "last_seen_at",
    ),
    "app_timeline_subscriptions": (
        "account_id",
        "timeline_stable_key",
        "created_at",
        "updated_at",
    ),
    "app_timeline_subscription_events": (
        "id",
        "account_id",
        "timeline_stable_key",
        "event_type",
        "occurred_at",
    ),
    "editorial_push_notifications": (
        "id",
        "draft_id",
        "article_id",
        "revision_id",
        "publication_kind",
        "headline",
        "body",
        "created_at",
    ),
    "editorial_push_deliveries": (
        "notification_id",
        "registration_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "lease_expires_at",
        "apns_id",
        "last_error",
        "delivered_at",
        "created_at",
        "updated_at",
    ),
    "explore_timeline_push_notifications": (
        "id",
        "catalog_id",
        "timeline_id",
        "timeline_stable_key",
        "node_id",
        "update_kind",
        "created_at",
    ),
    "explore_timeline_push_deliveries": (
        "notification_id",
        "registration_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "lease_expires_at",
        "apns_id",
        "last_error",
        "delivered_at",
        "created_at",
        "updated_at",
    ),
    "app_request_cooldowns": (
        "key_id",
        "scope",
        "last_allowed_at",
    ),
}

SCHEMA_DDL: Final[str] = (
    "CREATE SCHEMA staging_app AUTHORIZATION CURRENT_USER"
)

EDITORIAL_REVISIONS_VIEW_DDL: Final[str] = """
CREATE VIEW staging_app.editorial_revisions
    WITH (security_barrier = true) AS
SELECT revision.id,
       jsonb_build_object(
           'publication_kind',
           revision.document -> 'publication_kind'
       ) AS document
FROM public.editorial_revisions AS revision
WHERE EXISTS (
    SELECT 1
    FROM public.editorial_publications AS publication
    WHERE publication.revision_id = revision.id
      AND publication.state = 'published'
)
"""

VIEW_DDL_STATEMENTS: tuple[str, ...] = tuple(
    EDITORIAL_REVISIONS_VIEW_DDL
    if name == "editorial_revisions"
    else (
        f"CREATE VIEW {STAGING_SCHEMA}.{name} AS "
        f"SELECT source.* FROM public.{name} AS source"
    )
    for name in SHARED_READ_VIEWS
)
ARTICLES_VIEW_V11_DDL: Final[str] = (
    "CREATE OR REPLACE VIEW staging_app.articles AS "
    "SELECT source.* FROM public.articles AS source"
)
ARTICLES_VIEW_V12_DDL: Final[str] = (
    "CREATE OR REPLACE VIEW staging_app.articles AS "
    "SELECT source.* FROM public.articles AS source"
)
EDITORIAL_REVISIONS_GRANT_DDL: tuple[str, ...] = (
    "REVOKE ALL ON TABLE staging_app.editorial_revisions FROM PUBLIC",
    (
        "GRANT SELECT ON TABLE staging_app.editorial_revisions "
        "TO gyeol_staging_runtime"
    ),
)

RECOMMENDATION_EXPOSURE_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.app_recommendation_exposures (
    request_id      UUID NOT NULL,
    account_id      UUID NOT NULL
                    REFERENCES staging_app.app_accounts(id)
                    ON DELETE CASCADE,
    article_id      TEXT NOT NULL,
    ranking_version TEXT NOT NULL
                    CHECK (char_length(ranking_version) BETWEEN 1 AND 32),
    surface         TEXT NOT NULL
                    CHECK (surface IN ('home', 'article_boundary')),
    page_kind       TEXT NOT NULL
                    CHECK (page_kind IN ('initial', 'pagination', 'boundary')),
    position        SMALLINT NOT NULL CHECK (position BETWEEN 0 AND 4),
    lane            TEXT NOT NULL
                    CHECK (lane IN ('core', 'now', 'adjacent', 'surprise')),
    served_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    selected_at     TIMESTAMPTZ,
    PRIMARY KEY (request_id, article_id)
)
"""

ARTICLE_FEED_IMPRESSION_COUNT_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.article_feed_impression_counts (
    article_id         TEXT PRIMARY KEY,
    impression_count   BIGINT NOT NULL DEFAULT 0
                       CHECK (impression_count >= 0),
    first_impressed_at TIMESTAMPTZ,
    last_impressed_at  TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (
            impression_count = 0
            AND first_impressed_at IS NULL
            AND last_impressed_at IS NULL
        )
        OR (
            impression_count > 0
            AND first_impressed_at IS NOT NULL
            AND last_impressed_at IS NOT NULL
            AND first_impressed_at <= last_impressed_at
        )
    )
)
"""

ARTICLE_LIST_EXPOSURE_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.app_article_list_exposures (
    response_id UUID NOT NULL,
    position    SMALLINT NOT NULL CHECK (position BETWEEN 0 AND 99),
    account_id  UUID NOT NULL
                REFERENCES staging_app.app_accounts(id) ON DELETE CASCADE,
    article_id  TEXT NOT NULL,
    surface     TEXT NOT NULL CHECK (surface IN ('library')),
    served_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (response_id, position)
)
"""

SESSION_ACTIVITY_DAY_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.app_account_session_activity_days (
    account_id    UUID NOT NULL
                  REFERENCES staging_app.app_accounts(id) ON DELETE CASCADE,
    active_date   DATE NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, active_date),
    CHECK (first_seen_at <= last_seen_at)
)
"""

SESSION_ACTIVITY_DAY_INDEX_DDL: Final[str] = """
CREATE INDEX idx_stg_account_session_activity_date
    ON staging_app.app_account_session_activity_days (active_date, account_id)
"""

GUEST_SESSION_ACTIVITY_DAY_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.app_guest_session_activity_days (
    installation_hash BYTEA NOT NULL CHECK (octet_length(installation_hash) = 32),
    active_date       DATE NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (installation_hash, active_date),
    CHECK (first_seen_at <= last_seen_at)
)
"""

GUEST_SESSION_ACTIVITY_DAY_INDEX_DDL: Final[str] = """
CREATE INDEX idx_stg_guest_session_activity_date
    ON staging_app.app_guest_session_activity_days (active_date, installation_hash)
"""

TIMELINE_SUBSCRIPTION_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.app_timeline_subscriptions (
    account_id          UUID NOT NULL
                        REFERENCES staging_app.app_accounts(id)
                        ON DELETE CASCADE,
    timeline_stable_key TEXT NOT NULL
                        CHECK (char_length(timeline_stable_key) BETWEEN 1 AND 128),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, timeline_stable_key)
)
"""

TIMELINE_SUBSCRIPTION_INDEX_DDL: Final[str] = """
CREATE INDEX idx_stg_timeline_subscriptions_key
    ON staging_app.app_timeline_subscriptions
       (timeline_stable_key, account_id)
"""

TIMELINE_SUBSCRIPTION_EVENT_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.app_timeline_subscription_events (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL
                        REFERENCES staging_app.app_accounts(id)
                        ON DELETE CASCADE,
    timeline_stable_key TEXT NOT NULL
                        CHECK (char_length(timeline_stable_key) BETWEEN 1 AND 128),
    event_type          TEXT NOT NULL
                        CHECK (event_type IN ('subscribe','unsubscribe')),
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

TIMELINE_SUBSCRIPTION_EVENT_INDEX_DDL: Final[str] = """
CREATE INDEX idx_stg_timeline_subscription_events_account
    ON staging_app.app_timeline_subscription_events
       (account_id, occurred_at DESC, id DESC)
"""

TIMELINE_PUSH_STATE_TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE staging_app.explore_timeline_push_notifications (
        id                  UUID PRIMARY KEY,
        catalog_id          UUID NOT NULL,
        timeline_id         UUID NOT NULL,
        timeline_stable_key TEXT NOT NULL
                            CHECK (
                                char_length(timeline_stable_key) BETWEEN 1 AND 128
                            ),
        node_id             UUID NOT NULL,
        update_kind         TEXT NOT NULL
                            CHECK (update_kind IN ('inserted', 'occurred')),
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (catalog_id, timeline_stable_key, update_kind)
    )
    """,
    """
    CREATE TABLE staging_app.explore_timeline_push_deliveries (
        notification_id UUID NOT NULL
                        REFERENCES staging_app.explore_timeline_push_notifications(id)
                        ON DELETE CASCADE,
        registration_id UUID NOT NULL
                        REFERENCES staging_app.app_push_registrations(id)
                        ON DELETE CASCADE,
        status           TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN (
                             'queued',
                             'processing',
                             'sent',
                             'failed',
                             'cancelled'
                         )),
        attempt_count    INTEGER NOT NULL DEFAULT 0
                         CHECK (attempt_count >= 0),
        next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_expires_at TIMESTAMPTZ,
        apns_id          UUID,
        last_error       TEXT,
        delivered_at     TIMESTAMPTZ,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (notification_id, registration_id)
    )
    """,
)

TIMELINE_PUSH_STATE_INDEX_DDL: Final[str] = """
CREATE INDEX idx_stg_timeline_push_deliveries_due
    ON staging_app.explore_timeline_push_deliveries
       (next_attempt_at, created_at, notification_id, registration_id)
    WHERE status IN ('queued', 'processing')
"""

BOOKMARK_EVENT_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.article_bookmark_events (
    id          UUID PRIMARY KEY,
    account_id  UUID NOT NULL
                REFERENCES staging_app.app_accounts(id) ON DELETE CASCADE,
    article_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL CHECK (event_type IN ('saved','unsaved')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

TRENDING_SNAPSHOT_TABLE_DDL: Final[str] = """
CREATE TABLE staging_app.trending_search_snapshots (
    bucket_started_at TIMESTAMPTZ PRIMARY KEY,
    category_keywords JSONB NOT NULL
                      CHECK (jsonb_typeof(category_keywords) = 'object'),
    keyword_pool      JSONB NOT NULL
                      CHECK (
                          jsonb_typeof(keyword_pool) = 'array'
                          AND jsonb_array_length(keyword_pool) <= 9
                      ),
    suggestions       JSONB NOT NULL
                      CHECK (
                          jsonb_typeof(suggestions) = 'array'
                          AND jsonb_array_length(suggestions) <= 3
                      ),
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

TRENDING_STATE_TABLE_DDL: tuple[str, ...] = (
    BOOKMARK_EVENT_TABLE_DDL,
    TRENDING_SNAPSHOT_TABLE_DDL,
)

TABLE_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE staging_app.staging_schema_migrations (
        version    INTEGER PRIMARY KEY,
        checksum   TEXT NOT NULL CHECK (char_length(checksum) = 64),
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.article_view_events (
        id          UUID PRIMARY KEY,
        article_id  TEXT NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.article_view_counts (
        article_id      TEXT PRIMARY KEY,
        view_count      BIGINT NOT NULL DEFAULT 0 CHECK (view_count >= 0),
        first_viewed_at TIMESTAMPTZ,
        last_viewed_at  TIMESTAMPTZ,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (
            (
                view_count = 0
                AND first_viewed_at IS NULL
                AND last_viewed_at IS NULL
            )
            OR (
                view_count > 0
                AND first_viewed_at IS NOT NULL
                AND last_viewed_at IS NOT NULL
                AND first_viewed_at <= last_viewed_at
            )
        )
    )
    """,
    """
    CREATE TABLE staging_app.app_attest_challenges (
        id              UUID PRIMARY KEY,
        installation_id TEXT NOT NULL,
        key_id          TEXT NOT NULL,
        purpose         TEXT NOT NULL
                        CHECK (purpose IN ('attestation', 'assertion')),
        challenge       BYTEA NOT NULL CHECK (octet_length(challenge) = 32),
        expires_at      TIMESTAMPTZ NOT NULL,
        used_at         TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.app_attest_keys (
        key_id              TEXT PRIMARY KEY,
        installation_id     TEXT NOT NULL,
        public_key_pem      BYTEA NOT NULL,
        receipt             BYTEA NOT NULL,
        environment         TEXT NOT NULL
                            CHECK (environment IN ('development', 'production')),
        sign_count          BIGINT NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
        device              JSONB NOT NULL,
        validation_category INTEGER,
        bundle_version      TEXT,
        status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked')),
        last_feed_at        TIMESTAMPTZ,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.app_privacy_consents (
        id              UUID PRIMARY KEY,
        installation_id TEXT NOT NULL,
        key_id          TEXT NOT NULL
                        REFERENCES staging_app.app_attest_keys(key_id)
                        ON DELETE CASCADE,
        policy_version  TEXT NOT NULL
                        CHECK (char_length(policy_version) BETWEEN 5 AND 32),
        policy_url      TEXT NOT NULL
                        CHECK (char_length(policy_url) BETWEEN 12 AND 2048),
        app_version     TEXT NOT NULL
                        CHECK (char_length(app_version) BETWEEN 1 AND 64),
        build_number    TEXT NOT NULL
                        CHECK (char_length(build_number) BETWEEN 1 AND 64),
        accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (installation_id, policy_version)
    )
    """,
    """
    CREATE TABLE staging_app.app_accounts (
        id                  UUID PRIMARY KEY,
        apple_subject       TEXT NOT NULL UNIQUE,
        email               TEXT,
        given_name          TEXT,
        family_name         TEXT,
        apple_refresh_token BYTEA NOT NULL,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_signed_in_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.app_privacy_consent_accounts (
        consent_id UUID NOT NULL
                   REFERENCES staging_app.app_privacy_consents(id)
                   ON DELETE CASCADE,
        account_id UUID NOT NULL
                   REFERENCES staging_app.app_accounts(id)
                   ON DELETE CASCADE,
        linked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (consent_id, account_id)
    )
    """,
    """
    CREATE TABLE staging_app.app_account_sessions (
        id           UUID PRIMARY KEY,
        account_id   UUID NOT NULL
                     REFERENCES staging_app.app_accounts(id)
                     ON DELETE CASCADE,
        token_hash   BYTEA NOT NULL UNIQUE
                     CHECK (octet_length(token_hash) = 32),
        expires_at   TIMESTAMPTZ NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    SESSION_ACTIVITY_DAY_TABLE_DDL,
    GUEST_SESSION_ACTIVITY_DAY_TABLE_DDL,
    """
    CREATE TABLE staging_app.app_article_feedback (
        account_id UUID NOT NULL
                   REFERENCES staging_app.app_accounts(id)
                   ON DELETE CASCADE,
        article_id TEXT NOT NULL,
        reason     TEXT NOT NULL CHECK (reason IN (
                       'not_relevant',
                       'too_simple',
                       'too_difficult',
                       'seen_similar',
                       'distrust_source',
                       'other'
                   )),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (account_id, article_id)
    )
    """,
    """
    CREATE TABLE staging_app.app_account_reading_states (
        account_id      UUID PRIMARY KEY
                        REFERENCES staging_app.app_accounts(id)
                        ON DELETE CASCADE,
        revision        BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
        saved_articles  JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(saved_articles) = 'object'),
        scene_progress  JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(scene_progress) = 'object'),
        last_article_id TEXT,
        settings        JSONB NOT NULL DEFAULT '{
            "type_size":"standard",
            "leading":"comfortable",
            "theme":"light",
            "motion_enabled":true,
            "media_saver":false,
            "offline_cache":true
        }'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    BOOKMARK_EVENT_TABLE_DDL,
    r"""
    CREATE TABLE staging_app.app_account_activity_events (
        id                 UUID PRIMARY KEY,
        account_id         UUID NOT NULL
                           REFERENCES staging_app.app_accounts(id)
                           ON DELETE CASCADE,
        event_type         TEXT NOT NULL CHECK (event_type IN (
                               'search',
                               'article_opened',
                               'scene_viewed',
                               'article_completed'
                           )),
        occurred_at        TIMESTAMPTZ NOT NULL,
        time_zone          TEXT NOT NULL CHECK (
                               char_length(time_zone) BETWEEN 1 AND 64
                               AND time_zone ~ '^[A-Za-z0-9_+./-]+$'
                           ),
        utc_offset_minutes SMALLINT NOT NULL
                           CHECK (utc_offset_minutes BETWEEN -720 AND 840),
        local_date         DATE NOT NULL,
        local_hour         SMALLINT NOT NULL
                           CHECK (local_hour BETWEEN 0 AND 23),
        query              TEXT CHECK (
                               query IS NULL OR (
                                   char_length(query) BETWEEN 2 AND 80
                                   AND position(E'\n' in query) = 0
                                   AND position(E'\r' in query) = 0
                               )
                           ),
        result_count       SMALLINT CHECK (
                               result_count IS NULL
                               OR result_count BETWEEN 0 AND 1000
                           ),
        article_id         TEXT CHECK (
                               article_id IS NULL OR (
                                   char_length(article_id) BETWEEN 1 AND 128
                                   AND position(E'\n' in article_id) = 0
                                   AND position(E'\r' in article_id) = 0
                               )
                           ),
        scene_index        SMALLINT CHECK (
                               scene_index IS NULL
                               OR scene_index BETWEEN 0 AND 999
                           ),
        scene_count        SMALLINT CHECK (
                               scene_count IS NULL
                               OR scene_count BETWEEN 1 AND 1000
                           ),
        received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (
            (
                event_type = 'search'
                AND query IS NOT NULL
                AND result_count IS NOT NULL
                AND article_id IS NULL
                AND scene_index IS NULL
                AND scene_count IS NULL
            )
            OR (
                event_type IN (
                    'article_opened',
                    'scene_viewed',
                    'article_completed'
                )
                AND query IS NULL
                AND result_count IS NULL
                AND article_id IS NOT NULL
                AND scene_index IS NOT NULL
                AND scene_count IS NOT NULL
                AND scene_index < scene_count
                AND (
                    event_type <> 'article_completed'
                    OR scene_index = scene_count - 1
                )
            )
        )
    )
    """,
    """
    CREATE TABLE staging_app.app_push_registrations (
        id                     UUID PRIMARY KEY,
        account_id             UUID NOT NULL
                               REFERENCES staging_app.app_accounts(id)
                               ON DELETE CASCADE,
        session_id             UUID NOT NULL
                               REFERENCES staging_app.app_account_sessions(id)
                               ON DELETE CASCADE,
        installation_id        TEXT NOT NULL UNIQUE,
        key_id                 TEXT NOT NULL
                               REFERENCES staging_app.app_attest_keys(key_id)
                               ON DELETE CASCADE,
        token_hash             BYTEA NOT NULL UNIQUE
                               CHECK (octet_length(token_hash) = 32),
        token_ciphertext       BYTEA NOT NULL,
        environment            TEXT NOT NULL
                               CHECK (environment IN (
                                   'development',
                                   'production'
                               )),
        editorial_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
        weekly_insight_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        timeline_updates_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        timeline_refreshes_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        timeline_additions_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        status                 TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'invalid')),
        last_error             TEXT,
        invalidated_at         TIMESTAMPTZ,
        created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.editorial_push_notifications (
        id               UUID PRIMARY KEY,
        draft_id         UUID NOT NULL UNIQUE,
        article_id       TEXT NOT NULL,
        revision_id      UUID NOT NULL,
        publication_kind TEXT NOT NULL DEFAULT 'editorial'
                         CHECK (publication_kind IN (
                             'editorial',
                             'weekly_insight'
                         )),
        headline         TEXT NOT NULL
                         CHECK (char_length(headline) BETWEEN 1 AND 200),
        body             TEXT NOT NULL CHECK (char_length(body) <= 240),
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE staging_app.editorial_push_deliveries (
        notification_id UUID NOT NULL
                        REFERENCES staging_app.editorial_push_notifications(id)
                        ON DELETE CASCADE,
        registration_id UUID NOT NULL
                        REFERENCES staging_app.app_push_registrations(id)
                        ON DELETE CASCADE,
        status           TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN (
                             'queued',
                             'processing',
                             'sent',
                             'failed',
                             'cancelled'
                         )),
        attempt_count    INTEGER NOT NULL DEFAULT 0
                         CHECK (attempt_count >= 0),
        next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_expires_at TIMESTAMPTZ,
        apns_id          UUID,
        last_error       TEXT,
        delivered_at     TIMESTAMPTZ,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (notification_id, registration_id)
    )
    """,
    *TIMELINE_PUSH_STATE_TABLE_DDL,
    """
    CREATE TABLE staging_app.app_request_cooldowns (
        key_id          TEXT NOT NULL
                        REFERENCES staging_app.app_attest_keys(key_id)
                        ON DELETE CASCADE,
        scope           TEXT NOT NULL
                        CHECK (char_length(scope) BETWEEN 1 AND 64),
        last_allowed_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (key_id, scope)
    )
    """,
    TRENDING_SNAPSHOT_TABLE_DDL,
    RECOMMENDATION_EXPOSURE_TABLE_DDL,
    ARTICLE_FEED_IMPRESSION_COUNT_TABLE_DDL,
    ARTICLE_LIST_EXPOSURE_TABLE_DDL,
    TIMELINE_SUBSCRIPTION_TABLE_DDL,
    TIMELINE_SUBSCRIPTION_EVENT_TABLE_DDL,
)

RECOMMENDATION_EXPOSURE_INDEX_DDL: tuple[str, ...] = (
    """
    CREATE INDEX idx_stg_recommendation_exposure_account_time
        ON staging_app.app_recommendation_exposures
        (account_id, served_at DESC)
    """,
    """
    CREATE INDEX idx_stg_recommendation_exposure_account_article
        ON staging_app.app_recommendation_exposures
        (account_id, article_id, served_at DESC)
    """,
)

ARTICLE_LIST_EXPOSURE_INDEX_DDL: tuple[str, ...] = (
    """
    CREATE INDEX idx_stg_article_list_exposure_account_time
        ON staging_app.app_article_list_exposures
        (account_id, served_at DESC)
    """,
    """
    CREATE INDEX idx_stg_article_list_exposure_account_article
        ON staging_app.app_article_list_exposures
        (account_id, article_id, served_at DESC)
    """,
)

TRENDING_STATE_INDEX_DDL: tuple[str, ...] = (
    """
    CREATE INDEX idx_stg_article_bookmark_events_article_time
        ON staging_app.article_bookmark_events
        (article_id, received_at DESC)
    """,
    """
    CREATE INDEX idx_stg_article_bookmark_events_account_time
        ON staging_app.article_bookmark_events
        (account_id, received_at DESC)
    """,
)

INDEX_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE INDEX idx_stg_article_view_events_article
        ON staging_app.article_view_events (article_id, received_at DESC)
    """,
    """
    CREATE INDEX idx_stg_attest_challenge_lookup
        ON staging_app.app_attest_challenges
        (installation_id, key_id, purpose, expires_at DESC)
    """,
    """
    CREATE UNIQUE INDEX idx_stg_attest_one_active_installation
        ON staging_app.app_attest_keys (installation_id)
        WHERE status = 'active'
    """,
    """
    CREATE INDEX idx_stg_attest_key_installation
        ON staging_app.app_attest_keys (installation_id, status)
    """,
    """
    CREATE INDEX idx_stg_privacy_consents_accepted
        ON staging_app.app_privacy_consents (accepted_at DESC)
    """,
    """
    CREATE INDEX idx_stg_privacy_consent_accounts_account
        ON staging_app.app_privacy_consent_accounts
        (account_id, linked_at DESC)
    """,
    """
    CREATE INDEX idx_stg_account_sessions_account
        ON staging_app.app_account_sessions (account_id, expires_at DESC)
    """,
    """
    CREATE INDEX idx_stg_account_sessions_expiry
        ON staging_app.app_account_sessions (expires_at)
    """,
    SESSION_ACTIVITY_DAY_INDEX_DDL,
    GUEST_SESSION_ACTIVITY_DAY_INDEX_DDL,
    """
    CREATE INDEX idx_stg_article_feedback_account_recent
        ON staging_app.app_article_feedback (account_id, updated_at DESC)
    """,
    """
    CREATE INDEX idx_stg_account_activity_account_time
        ON staging_app.app_account_activity_events
        (account_id, occurred_at DESC)
    """,
    """
    CREATE INDEX idx_stg_account_activity_type_local_time
        ON staging_app.app_account_activity_events
        (event_type, local_date, local_hour)
    """,
    """
    CREATE INDEX idx_stg_account_activity_account_type_time
        ON staging_app.app_account_activity_events
        (account_id, event_type, occurred_at DESC)
    """,
    """
    CREATE INDEX idx_stg_push_registrations_target
        ON staging_app.app_push_registrations
        (status, environment, last_seen_at DESC)
    """,
    """
    CREATE INDEX idx_stg_push_registrations_session
        ON staging_app.app_push_registrations (session_id)
    """,
    """
    CREATE INDEX idx_stg_editorial_push_deliveries_due
        ON staging_app.editorial_push_deliveries
        (next_attempt_at, created_at, notification_id, registration_id)
        WHERE status IN ('queued', 'processing')
    """,
    TIMELINE_PUSH_STATE_INDEX_DDL,
    """
    CREATE INDEX idx_stg_app_request_cooldowns_last
        ON staging_app.app_request_cooldowns (last_allowed_at DESC)
    """,
    *TRENDING_STATE_INDEX_DDL,
    *RECOMMENDATION_EXPOSURE_INDEX_DDL,
    *ARTICLE_LIST_EXPOSURE_INDEX_DDL,
    TIMELINE_SUBSCRIPTION_INDEX_DDL,
    TIMELINE_SUBSCRIPTION_EVENT_INDEX_DDL,
)

MIGRATOR_ROLE_DDL: Final[str] = """
CREATE ROLE gyeol_staging_migrator
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS
"""

RUNTIME_ROLE_POLICY: Final[str] = """
CREATE ROLE gyeol_staging_runtime
    LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS PASSWORD <SCRAM-SHA-256>
"""

SCHEMA_GRANT_DDL_STATEMENTS: tuple[str, ...] = (
    "REVOKE ALL ON SCHEMA staging_app FROM PUBLIC",
    "GRANT USAGE ON SCHEMA staging_app TO gyeol_staging_migrator",
    "GRANT USAGE ON SCHEMA staging_app TO gyeol_staging_runtime",
)

VIEW_GRANT_DDL_STATEMENTS: tuple[str, ...] = tuple(
    statement
    for name in SHARED_READ_VIEWS
    for statement in (
        f"REVOKE ALL ON TABLE {STAGING_SCHEMA}.{name} FROM PUBLIC",
        (
            f"GRANT SELECT ON TABLE {STAGING_SCHEMA}.{name} "
            f"TO {STAGING_RUNTIME_ROLE}"
        ),
    )
)

TABLE_GRANT_DDL_STATEMENTS: tuple[str, ...] = (
    *tuple(
        statement
        for name in STAGING_STATE_TABLES
        for statement in (
            f"REVOKE ALL ON TABLE {STAGING_SCHEMA}.{name} FROM PUBLIC",
            (
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                f"{STAGING_SCHEMA}.{name} TO {STAGING_RUNTIME_ROLE}"
            ),
        )
    ),
    (
        f"REVOKE ALL ON TABLE {STAGING_SCHEMA}.{STAGING_MIGRATION_TABLE} "
        "FROM PUBLIC"
    ),
    (
        f"GRANT SELECT ON TABLE {STAGING_SCHEMA}.{STAGING_MIGRATION_TABLE} "
        f"TO {STAGING_RUNTIME_ROLE}"
    ),
)

OWNER_DDL_STATEMENTS: tuple[str, ...] = tuple(
    (
        f"ALTER TABLE {STAGING_SCHEMA}.{name} "
        f"OWNER TO {STAGING_MIGRATOR_ROLE}"
    )
    for name in (*STAGING_STATE_TABLES, STAGING_MIGRATION_TABLE)
)

ROLE_SETTING_DDL_STATEMENTS: tuple[str, ...] = (
    (
        f"ALTER ROLE {STAGING_RUNTIME_ROLE} "
        f"SET search_path = {STAGING_SCHEMA}, pg_catalog"
    ),
)

OBJECT_DDL_STATEMENTS: tuple[str, ...] = (
    SCHEMA_DDL,
    *VIEW_DDL_STATEMENTS,
    *TABLE_DDL_STATEMENTS,
    *INDEX_DDL_STATEMENTS,
    *SCHEMA_GRANT_DDL_STATEMENTS,
    *VIEW_GRANT_DDL_STATEMENTS,
    *TABLE_GRANT_DDL_STATEMENTS,
    *OWNER_DDL_STATEMENTS,
    *ROLE_SETTING_DDL_STATEMENTS,
)

_CHECKSUM_SOURCE = "\n-- statement --\n".join(
    (
        f"schema-version={STAGING_SCHEMA_VERSION}",
        MIGRATOR_ROLE_DDL,
        RUNTIME_ROLE_POLICY,
        *OBJECT_DDL_STATEMENTS,
    )
)
STAGING_SCHEMA_CHECKSUM: Final[str] = hashlib.sha256(
    _CHECKSUM_SOURCE.encode("utf-8")
).hexdigest()

SCHEMA_INSPECTION_SQL: Final[str] = """
SELECT /* staging:schema */
       current_user::text AS current_user,
       (
           SELECT pg_get_userbyid(namespace.nspowner)
           FROM pg_catalog.pg_namespace AS namespace
           WHERE namespace.nspname = 'staging_app'
       ) AS schema_owner
"""

ROLE_INSPECTION_SQL: Final[str] = """
SELECT /* staging:roles */
       rolname,
       rolsuper,
       rolinherit,
       rolcreaterole,
       rolcreatedb,
       rolcanlogin,
       rolreplication,
       rolbypassrls
FROM pg_catalog.pg_roles
WHERE rolname = ANY($1::text[])
ORDER BY rolname
"""

RELATION_INSPECTION_SQL: Final[str] = """
SELECT /* staging:relations */
       relation.relname,
       relation.relkind,
       pg_get_userbyid(relation.relowner) AS owner
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'staging_app'
  AND relation.relkind IN ('r', 'v')
ORDER BY relation.relname
"""

MIGRATION_INSPECTION_SQL: Final[str] = """
SELECT /* staging:migration */
       version,
       checksum,
       count(*) OVER ()::integer AS migration_count
FROM staging_app.staging_schema_migrations
ORDER BY version DESC
LIMIT 1
"""

RUNTIME_GRANT_INSPECTION_SQL: Final[str] = """
SELECT /* staging:runtime-grants */
       table_name,
       privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'staging_app'
  AND grantee = 'gyeol_staging_runtime'
ORDER BY table_name, privilege_type
"""

SCHEMA_PRIVILEGE_INSPECTION_SQL: Final[str] = """
SELECT /* staging:schema-privileges */
       has_schema_privilege(
           'gyeol_staging_runtime',
           'staging_app',
           'USAGE'
       ) AS runtime_usage,
       has_schema_privilege(
           'gyeol_staging_runtime',
           'staging_app',
           'CREATE'
       ) AS runtime_create,
       has_schema_privilege(
           'gyeol_staging_migrator',
           'staging_app',
           'USAGE'
       ) AS migrator_usage,
       has_schema_privilege(
           'gyeol_staging_migrator',
           'staging_app',
           'CREATE'
       ) AS migrator_create
"""

PUBLIC_ACL_FINGERPRINT_SQL: Final[str] = """
SELECT /* staging:public-acl-fingerprint */
       jsonb_build_object(
           'schema',
           (
               SELECT jsonb_build_array(
                   namespace.nspname,
                   pg_get_userbyid(namespace.nspowner),
                   COALESCE(namespace.nspacl::text, '')
               )
               FROM pg_catalog.pg_namespace AS namespace
               WHERE namespace.nspname = 'public'
           ),
           'relations',
           COALESCE(
               (
                   SELECT jsonb_agg(
                       jsonb_build_array(
                           relation.relname,
                           relation.relkind::text,
                           pg_get_userbyid(relation.relowner),
                           COALESCE(relation.relacl::text, '')
                       )
                       ORDER BY relation.relname, relation.relkind::text
                   )
                   FROM pg_catalog.pg_class AS relation
                   JOIN pg_catalog.pg_namespace AS namespace
                     ON namespace.oid = relation.relnamespace
                   WHERE namespace.nspname = 'public'
                     AND relation.relkind IN (
                         'r',
                         'p',
                         'v',
                         'm',
                         'S',
                         'f'
                     )
               ),
               '[]'::jsonb
           )
       )::text AS snapshot
"""

INSPECTION_SQL: tuple[str, ...] = (
    SCHEMA_INSPECTION_SQL,
    ROLE_INSPECTION_SQL,
    RELATION_INSPECTION_SQL,
    MIGRATION_INSPECTION_SQL,
    RUNTIME_GRANT_INSPECTION_SQL,
    SCHEMA_PRIVILEGE_INSPECTION_SQL,
    PUBLIC_ACL_FINGERPRINT_SQL,
)

RUNTIME_CONTRACT_SQL: Final[str] = """
SELECT /* staging:runtime-contract */
       current_user::text AS current_user,
       current_setting('search_path') AS search_path,
       (
           SELECT pg_get_userbyid(namespace.nspowner)
           FROM pg_catalog.pg_namespace AS namespace
           WHERE namespace.nspname = 'staging_app'
       ) AS schema_owner,
       role.rolsuper,
       role.rolinherit,
       role.rolcreaterole,
       role.rolcreatedb,
       role.rolcanlogin,
       role.rolreplication,
       role.rolbypassrls,
       has_schema_privilege(
           current_user,
           'staging_app',
           'USAGE'
       ) AS runtime_usage,
       has_schema_privilege(
           current_user,
           'staging_app',
           'CREATE'
       ) AS runtime_create,
       has_schema_privilege(
           'gyeol_staging_migrator',
           'staging_app',
           'USAGE'
       ) AS migrator_usage,
       has_schema_privilege(
           'gyeol_staging_migrator',
           'staging_app',
           'CREATE'
       ) AS migrator_create,
       (
           SELECT COALESCE(
               jsonb_object_agg(
                   relation.relname,
                   jsonb_build_object(
                       'kind',
                       relation.relkind,
                       'owner',
                       pg_get_userbyid(relation.relowner)
                   )
               ),
               '{}'::jsonb
           )
           FROM pg_catalog.pg_class AS relation
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'staging_app'
             AND relation.relkind IN ('r', 'v')
       ) AS relations,
       (
           SELECT COALESCE(
               jsonb_object_agg(columns.table_name, columns.names),
               '{}'::jsonb
           )
           FROM (
               SELECT
                   table_name,
                   jsonb_agg(column_name ORDER BY ordinal_position) AS names
               FROM information_schema.columns
               WHERE table_schema = 'staging_app'
               GROUP BY table_name
           ) AS columns
       ) AS columns,
       (
           SELECT COALESCE(
               array_agg(
                   table_name || ':' || privilege_type
                   ORDER BY table_name, privilege_type
               ),
               ARRAY[]::text[]
           )
           FROM information_schema.role_table_grants
           WHERE table_schema = 'staging_app'
             AND grantee = current_user
       ) AS grants,
       (
           SELECT version
           FROM staging_app.staging_schema_migrations
           ORDER BY version DESC
           LIMIT 1
       ) AS migration_version,
       (
           SELECT checksum
           FROM staging_app.staging_schema_migrations
           ORDER BY version DESC
           LIMIT 1
       ) AS migration_checksum,
       (
           SELECT count(*)::integer
           FROM staging_app.staging_schema_migrations
       ) AS migration_count,
       (
           SELECT count(*)::integer
           FROM pg_catalog.pg_class AS relation
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'public'
             AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
             AND (
                 has_table_privilege(
                     current_user,
                     relation.oid,
                     'SELECT'
                 )
                 OR has_table_privilege(
                     current_user,
                     relation.oid,
                     'INSERT'
                 )
                 OR has_table_privilege(
                     current_user,
                     relation.oid,
                     'UPDATE'
                 )
                 OR has_table_privilege(
                     current_user,
                     relation.oid,
                     'DELETE'
                 )
                 OR has_table_privilege(
                     current_user,
                     relation.oid,
                     'TRUNCATE'
                 )
                 OR has_table_privilege(
                     current_user,
                     relation.oid,
                     'REFERENCES'
                 )
                 OR has_table_privilege(
                     current_user,
                     relation.oid,
                     'TRIGGER'
                 )
             )
       ) AS public_privilege_count
FROM pg_catalog.pg_roles AS role
WHERE role.rolname = current_user
"""


class StagingProvisionError(RuntimeError):
    """A stable, credential-free provisioning failure code."""

    def __init__(self, code: str, *, details: tuple[str, ...] = ()):
        self.code = code
        self.details = details
        super().__init__(code)


class StagingRuntimeContractError(RuntimeError):
    """A stable runtime-validation failure with no catalog or DSN details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class StagingProvisionPlan:
    schema: str
    schema_version: int
    checksum: str
    roles: tuple[str, ...]
    views: tuple[str, ...]
    state_tables: tuple[str, ...]
    migration_table: str
    confirmation: str


@dataclass(frozen=True, slots=True)
class StagingProvisionReport:
    current_user: str
    schema_exists: bool
    schema_owner: str | None
    roles: tuple[str, ...]
    relations: tuple[str, ...]
    migration_version: int | None
    migration_checksum: str | None
    migration_count: int
    runtime_grants: tuple[str, ...]
    runtime_schema_usage: bool
    runtime_schema_create: bool
    migrator_schema_usage: bool
    migrator_schema_create: bool
    public_acl_sha256: str
    pristine: bool
    upgradeable: bool
    ready: bool
    applied: bool
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StagingRuntimeReport:
    profile: str
    schema_version: int | None
    views: dict[str, bool]
    tables: dict[str, bool]
    privileges_ok: bool
    current_user_is_runtime: bool
    search_path_ok: bool
    owners_ok: bool
    columns_ok: bool
    checksum_ok: bool
    public_relations_denied: bool
    valid: bool


def staging_plan() -> StagingProvisionPlan:
    """Return the static, secret-free additive object plan."""
    return StagingProvisionPlan(
        schema=STAGING_SCHEMA,
        schema_version=STAGING_SCHEMA_VERSION,
        checksum=STAGING_SCHEMA_CHECKSUM,
        roles=(STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE),
        views=SHARED_READ_VIEWS,
        state_tables=STAGING_STATE_TABLES,
        migration_table=STAGING_MIGRATION_TABLE,
        confirmation=STAGING_CONFIRMATION,
    )


def _scram_verifier(password: str) -> str:
    salt = os.urandom(16)
    iterations = 4096
    salted_password = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    client_key = hmac.new(
        salted_password,
        b"Client Key",
        hashlib.sha256,
    ).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(
        salted_password,
        b"Server Key",
        hashlib.sha256,
    ).digest()
    encode = lambda value: base64.b64encode(value).decode("ascii")
    return (
        f"SCRAM-SHA-256${iterations}:{encode(salt)}$"
        f"{encode(stored_key)}:{encode(server_key)}"
    )


def runtime_role_ddl(runtime_password: SecretStr) -> str:
    """Build role DDL containing a salted verifier, never the plaintext."""
    verifier = _runtime_password_verifier(runtime_password)
    return (
        f"CREATE ROLE {STAGING_RUNTIME_ROLE} "
        "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        f"NOREPLICATION NOBYPASSRLS PASSWORD '{verifier}'"
    )


def _runtime_password_verifier(runtime_password: SecretStr) -> str:
    password = runtime_password.get_secret_value()
    if not password or "\n" in password or "\r" in password or "\0" in password:
        raise StagingProvisionError("runtime_password_invalid")
    return _scram_verifier(password)


def runtime_role_password_ddl(runtime_password: SecretStr) -> str:
    """Build secret-redacted DDL for rotating only the staging login."""
    verifier = _runtime_password_verifier(runtime_password)
    return (
        f"ALTER ROLE {STAGING_RUNTIME_ROLE} "
        f"PASSWORD '{verifier}'"
    )


def expected_runtime_grants() -> set[str]:
    """Return the exact table-level privilege contract for the login role."""
    grants = {f"{name}:SELECT" for name in SHARED_READ_VIEWS}
    for name in STAGING_STATE_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def production_runtime_report() -> StagingRuntimeReport:
    """Return the production no-op contract without touching PostgreSQL."""
    return StagingRuntimeReport(
        profile="production",
        schema_version=None,
        views={},
        tables={},
        privileges_ok=True,
        current_user_is_runtime=True,
        search_path_ok=True,
        owners_ok=True,
        columns_ok=True,
        checksum_ok=True,
        public_relations_denied=True,
        valid=True,
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise TypeError("expected JSON object")
    return {str(key): item for key, item in decoded.items()}


async def validate_staging_runtime(conn: Any) -> StagingRuntimeReport:
    """Validate the restricted login using catalog SELECT statements only."""
    try:
        row = await conn.fetchrow(RUNTIME_CONTRACT_SQL)
        if row is None:
            raise TypeError("runtime contract query returned no row")
        relations = _json_object(row["relations"])
        columns = _json_object(row["columns"])
        grants = {str(value) for value in row["grants"]}
    except Exception:
        raise StagingRuntimeContractError(
            "staging_runtime_contract_query_failed"
        ) from None

    current_user_is_runtime = (
        str(row["current_user"]) == STAGING_RUNTIME_ROLE
    )
    search_path_ok = (
        "".join(str(row["search_path"]).split())
        == f"{STAGING_SCHEMA},pg_catalog"
    )
    schema_owner_value = row["schema_owner"]
    schema_owner = (
        str(schema_owner_value) if schema_owner_value is not None else None
    )
    owner_role_is_safe = schema_owner not in (
        None,
        STAGING_RUNTIME_ROLE,
        STAGING_MIGRATOR_ROLE,
    )

    expected_relations = {
        *SHARED_READ_VIEWS,
        *STAGING_STATE_TABLES,
        STAGING_MIGRATION_TABLE,
    }
    relation_catalog_exact = set(relations) == expected_relations
    views: dict[str, bool] = {}
    for name in SHARED_READ_VIEWS:
        relation = relations.get(name)
        views[name] = (
            isinstance(relation, dict)
            and str(relation.get("kind")) == "v"
            and str(relation.get("owner")) == schema_owner
        )
    tables: dict[str, bool] = {}
    for name in (*STAGING_STATE_TABLES, STAGING_MIGRATION_TABLE):
        relation = relations.get(name)
        tables[name] = (
            isinstance(relation, dict)
            and str(relation.get("kind")) == "r"
            and str(relation.get("owner")) == STAGING_MIGRATOR_ROLE
        )
    owners_ok = (
        owner_role_is_safe
        and relation_catalog_exact
        and all(views.values())
        and all(tables.values())
    )

    column_results: dict[str, bool] = {}
    for name, expected in REQUIRED_RELATION_COLUMNS.items():
        actual = columns.get(name)
        column_results[name] = (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and {str(value) for value in actual} == set(expected)
        )
    columns_ok = (
        set(columns) == set(REQUIRED_RELATION_COLUMNS)
        and all(column_results.values())
    )

    role_is_restricted = (
        current_user_is_runtime
        and bool(row["rolsuper"]) is False
        and bool(row["rolinherit"]) is False
        and bool(row["rolcreaterole"]) is False
        and bool(row["rolcreatedb"]) is False
        and bool(row["rolcanlogin"]) is True
        and bool(row["rolreplication"]) is False
        and bool(row["rolbypassrls"]) is False
    )
    public_relations_denied = int(row["public_privilege_count"]) == 0
    privileges_ok = (
        role_is_restricted
        and bool(row["runtime_usage"]) is True
        and bool(row["runtime_create"]) is False
        and bool(row["migrator_usage"]) is True
        and bool(row["migrator_create"]) is False
        and grants == expected_runtime_grants()
        and public_relations_denied
    )
    migration_version_value = row["migration_version"]
    migration_version = (
        int(migration_version_value)
        if migration_version_value is not None
        else None
    )
    checksum_ok = (
        migration_version == STAGING_SCHEMA_VERSION
        and int(row["migration_count"]) == STAGING_SCHEMA_VERSION
        and str(row["migration_checksum"]) == STAGING_SCHEMA_CHECKSUM
    )
    valid = (
        current_user_is_runtime
        and search_path_ok
        and owners_ok
        and columns_ok
        and privileges_ok
        and checksum_ok
    )
    report = StagingRuntimeReport(
        profile="staging_app",
        schema_version=migration_version,
        views=views,
        tables=tables,
        privileges_ok=privileges_ok,
        current_user_is_runtime=current_user_is_runtime,
        search_path_ok=search_path_ok,
        owners_ok=owners_ok,
        columns_ok=columns_ok,
        checksum_ok=checksum_ok,
        public_relations_denied=public_relations_denied,
        valid=valid,
    )
    if not valid:
        raise StagingRuntimeContractError(
            "staging_runtime_contract_invalid"
        )
    return report


def _role_is_expected(name: str, row: Any) -> bool:
    return (
        bool(row["rolsuper"]) is False
        and bool(row["rolinherit"]) is False
        and bool(row["rolcreaterole"]) is False
        and bool(row["rolcreatedb"]) is False
        and bool(row["rolreplication"]) is False
        and bool(row.get("rolbypassrls", False)) is False
        and bool(row["rolcanlogin"]) is (name == STAGING_RUNTIME_ROLE)
    )


async def public_acl_fingerprint(conn: Any) -> str:
    """Hash only the public schema/relation ownership and ACL catalog."""
    row = await conn.fetchrow(PUBLIC_ACL_FINGERPRINT_SQL)
    if row is None or not isinstance(row["snapshot"], str):
        raise StagingProvisionError("public_acl_inspection_failed")
    return hashlib.sha256(row["snapshot"].encode("utf-8")).hexdigest()


async def inspect_staging_database(conn: Any) -> StagingProvisionReport:
    """Read only PostgreSQL catalogs and classify pristine, ready, or drifted."""
    public_acl_sha256 = await public_acl_fingerprint(conn)
    schema_row = await conn.fetchrow(SCHEMA_INSPECTION_SQL)
    if schema_row is None:
        raise StagingProvisionError("catalog_inspection_failed")
    current_user = str(schema_row["current_user"])
    schema_owner_value = schema_row["schema_owner"]
    schema_owner = (
        str(schema_owner_value) if schema_owner_value is not None else None
    )

    role_rows = await conn.fetch(
        ROLE_INSPECTION_SQL,
        [STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE],
    )
    roles_by_name = {str(row["rolname"]): row for row in role_rows}

    relation_rows = await conn.fetch(RELATION_INSPECTION_SQL)
    relations_by_name: dict[str, tuple[str, str]] = {}
    for row in relation_rows:
        raw_kind = row["relkind"]
        kind = (
            raw_kind.decode("ascii")
            if isinstance(raw_kind, bytes)
            else str(raw_kind)
        )
        relations_by_name[str(row["relname"])] = (
            kind,
            str(row["owner"]),
        )

    migration_version: int | None = None
    migration_checksum: str | None = None
    migration_count = 0
    if STAGING_MIGRATION_TABLE in relations_by_name:
        migration_row = await conn.fetchrow(MIGRATION_INSPECTION_SQL)
        if migration_row is not None:
            migration_version = int(migration_row["version"])
            migration_checksum = str(migration_row["checksum"])
            migration_count = int(migration_row.get("migration_count", 1))

    runtime_grants: set[str] = set()
    runtime_schema_usage = False
    runtime_schema_create = False
    migrator_schema_usage = False
    migrator_schema_create = False
    if (
        schema_owner is not None
        and STAGING_RUNTIME_ROLE in roles_by_name
    ):
        grant_rows = await conn.fetch(RUNTIME_GRANT_INSPECTION_SQL)
        runtime_grants = {
            f"{row['table_name']}:{row['privilege_type']}"
            for row in grant_rows
        }
        privilege_row = await conn.fetchrow(
            SCHEMA_PRIVILEGE_INSPECTION_SQL
        )
        if privilege_row is not None:
            runtime_schema_usage = bool(privilege_row["runtime_usage"])
            runtime_schema_create = bool(privilege_row["runtime_create"])
            migrator_schema_usage = bool(privilege_row["migrator_usage"])
            migrator_schema_create = bool(privilege_row["migrator_create"])

    pristine = (
        schema_owner is None
        and not roles_by_name
        and not relations_by_name
    )
    conflicts: list[str] = []
    expected_roles = {STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE}
    expected_relations = {
        *SHARED_READ_VIEWS,
        *STAGING_STATE_TABLES,
        STAGING_MIGRATION_TABLE,
    }

    if not pristine:
        if set(roles_by_name) != expected_roles:
            conflicts.append("role_catalog")
        else:
            for role_name in sorted(expected_roles):
                if not _role_is_expected(
                    role_name,
                    roles_by_name[role_name],
                ):
                    conflicts.append(f"role_attributes:{role_name}")

        if schema_owner != current_user:
            conflicts.append("schema_owner")
        if set(relations_by_name) != expected_relations:
            conflicts.append("relation_catalog")
        for view in SHARED_READ_VIEWS:
            if view in relations_by_name and relations_by_name[view] != (
                "v",
                current_user,
            ):
                conflicts.append(f"view_owner:{view}")
        for table in (*STAGING_STATE_TABLES, STAGING_MIGRATION_TABLE):
            if table in relations_by_name and relations_by_name[table] != (
                "r",
                STAGING_MIGRATOR_ROLE,
            ):
                conflicts.append(f"table_owner:{table}")

        if (
            migration_count != STAGING_SCHEMA_VERSION
            or migration_version != STAGING_SCHEMA_VERSION
            or migration_checksum != STAGING_SCHEMA_CHECKSUM
        ):
            conflicts.append("migration_checksum")
        if runtime_grants != expected_runtime_grants():
            conflicts.append("runtime_grants")
        if (
            not runtime_schema_usage
            or runtime_schema_create
            or not migrator_schema_usage
            or migrator_schema_create
        ):
            conflicts.append("runtime_schema_privileges")

    ready = not pristine and not conflicts
    report = StagingProvisionReport(
        current_user=current_user,
        schema_exists=schema_owner is not None,
        schema_owner=schema_owner,
        roles=tuple(sorted(roles_by_name)),
        relations=tuple(sorted(relations_by_name)),
        migration_version=migration_version,
        migration_checksum=migration_checksum,
        migration_count=migration_count,
        runtime_grants=tuple(sorted(runtime_grants)),
        runtime_schema_usage=runtime_schema_usage,
        runtime_schema_create=runtime_schema_create,
        migrator_schema_usage=migrator_schema_usage,
        migrator_schema_create=migrator_schema_create,
        public_acl_sha256=public_acl_sha256,
        pristine=pristine,
        upgradeable=False,
        ready=ready,
        applied=False,
        conflicts=tuple(conflicts),
    )
    if (
        _is_exact_v1(report)
        or _is_exact_v2(report)
        or _is_exact_v3(report)
        or _is_exact_v4(report)
        or _is_exact_v5(report)
        or _is_exact_v6(report)
        or _is_exact_v7(report)
        or _is_exact_v8(report)
        or _is_exact_v9(report)
        or _is_exact_v10(report)
        or _is_exact_v11(report)
    ):
        return replace(report, upgradeable=True, conflicts=())
    return report


def _expected_v1_runtime_grants() -> set[str]:
    grants = {
        f"{name}:SELECT"
        for name in SHARED_READ_VIEWS
        if name != "editorial_revisions" and name not in STAGING_V6_SHARED_VIEWS
    }
    for name in STAGING_STATE_TABLES:
        if name in {
            "app_recommendation_exposures",
            "article_bookmark_events",
            "trending_search_snapshots",
            *STAGING_V5_STATE_TABLES,
            *STAGING_V7_STATE_TABLES,
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        }:
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v1(report: StagingProvisionReport) -> bool:
    post_v1_tables = {
        "app_recommendation_exposures",
        "article_bookmark_events",
        "trending_search_snapshots",
        *STAGING_V5_STATE_TABLES,
        *STAGING_V7_STATE_TABLES,
        *STAGING_V8_STATE_TABLES,
        *STAGING_V9_STATE_TABLES,
        *STAGING_V10_STATE_TABLES,
    }
    expected_relations = {
        *(
            name
            for name in SHARED_READ_VIEWS
            if name != "editorial_revisions" and name not in STAGING_V6_SHARED_VIEWS
        ),
        *(
            name
            for name in STAGING_STATE_TABLES
            if name not in post_v1_tables
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 1
        and report.migration_checksum == STAGING_SCHEMA_V1_CHECKSUM
        and report.migration_count == 1
        and set(report.runtime_grants) == _expected_v1_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v2_runtime_grants() -> set[str]:
    grants = {
        f"{name}:SELECT"
        for name in SHARED_READ_VIEWS
        if name not in STAGING_V6_SHARED_VIEWS
    }
    for name in STAGING_STATE_TABLES:
        if name in {
            "app_recommendation_exposures",
            "article_bookmark_events",
            "trending_search_snapshots",
            *STAGING_V5_STATE_TABLES,
            *STAGING_V7_STATE_TABLES,
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        }:
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v2(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *(name for name in SHARED_READ_VIEWS if name not in STAGING_V6_SHARED_VIEWS),
        *(
            name
            for name in STAGING_STATE_TABLES
            if name not in {
                "app_recommendation_exposures",
                "article_bookmark_events",
                "trending_search_snapshots",
                *STAGING_V5_STATE_TABLES,
                *STAGING_V7_STATE_TABLES,
                *STAGING_V8_STATE_TABLES,
                *STAGING_V9_STATE_TABLES,
                *STAGING_V10_STATE_TABLES,
            }
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 2
        and report.migration_checksum == STAGING_SCHEMA_V2_CHECKSUM
        and report.migration_count == 2
        and set(report.runtime_grants) == _expected_v2_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v3_runtime_grants() -> set[str]:
    grants = {
        f"{name}:SELECT"
        for name in SHARED_READ_VIEWS
        if name not in STAGING_V6_SHARED_VIEWS
    }
    for name in STAGING_STATE_TABLES:
        if name in {
            "article_bookmark_events",
            "trending_search_snapshots",
            *STAGING_V5_STATE_TABLES,
            *STAGING_V7_STATE_TABLES,
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        }:
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v3(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *(name for name in SHARED_READ_VIEWS if name not in STAGING_V6_SHARED_VIEWS),
        *(
            name
            for name in STAGING_STATE_TABLES
            if name not in {
                "article_bookmark_events",
                "trending_search_snapshots",
                *STAGING_V5_STATE_TABLES,
                *STAGING_V7_STATE_TABLES,
                *STAGING_V8_STATE_TABLES,
                *STAGING_V9_STATE_TABLES,
                *STAGING_V10_STATE_TABLES,
            }
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 3
        and report.migration_checksum == STAGING_SCHEMA_V3_CHECKSUM
        and report.migration_count == 3
        and set(report.runtime_grants) == _expected_v3_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v4_runtime_grants() -> set[str]:
    grants = {
        f"{name}:SELECT"
        for name in SHARED_READ_VIEWS
        if name not in STAGING_V6_SHARED_VIEWS
    }
    for name in STAGING_STATE_TABLES:
        if name in (
            *STAGING_V5_STATE_TABLES,
            *STAGING_V7_STATE_TABLES,
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        ):
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v4(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *(name for name in SHARED_READ_VIEWS if name not in STAGING_V6_SHARED_VIEWS),
        *(
            name
            for name in STAGING_STATE_TABLES
            if name not in (
                *STAGING_V5_STATE_TABLES,
                *STAGING_V7_STATE_TABLES,
                *STAGING_V8_STATE_TABLES,
                *STAGING_V9_STATE_TABLES,
                *STAGING_V10_STATE_TABLES,
            )
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 4
        and report.migration_checksum == STAGING_SCHEMA_V4_CHECKSUM
        and report.migration_count == 4
        and set(report.runtime_grants) == _expected_v4_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v5_runtime_grants() -> set[str]:
    grants = {
        f"{name}:SELECT"
        for name in SHARED_READ_VIEWS
        if name not in STAGING_V6_SHARED_VIEWS
    }
    for name in STAGING_STATE_TABLES:
        if name in (
            *STAGING_V7_STATE_TABLES,
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        ):
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v5(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *(name for name in SHARED_READ_VIEWS if name not in STAGING_V6_SHARED_VIEWS),
        *(
            name for name in STAGING_STATE_TABLES
            if name not in (
                *STAGING_V7_STATE_TABLES,
                *STAGING_V8_STATE_TABLES,
                *STAGING_V9_STATE_TABLES,
                *STAGING_V10_STATE_TABLES,
            )
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 5
        and report.migration_checksum == STAGING_SCHEMA_V5_CHECKSUM
        and report.migration_count == 5
        and set(report.runtime_grants) == _expected_v5_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v6_runtime_grants() -> set[str]:
    grants = {f"{name}:SELECT" for name in SHARED_READ_VIEWS}
    for name in STAGING_STATE_TABLES:
        if name in (
            *STAGING_V7_STATE_TABLES,
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        ):
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v6(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *SHARED_READ_VIEWS,
        *(
            name for name in STAGING_STATE_TABLES
            if name not in (
                *STAGING_V7_STATE_TABLES,
                *STAGING_V8_STATE_TABLES,
                *STAGING_V9_STATE_TABLES,
                *STAGING_V10_STATE_TABLES,
            )
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 6
        and report.migration_checksum == STAGING_SCHEMA_V6_CHECKSUM
        and report.migration_count == 6
        and set(report.runtime_grants) == _expected_v6_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v7_runtime_grants() -> set[str]:
    grants = {f"{name}:SELECT" for name in SHARED_READ_VIEWS}
    for name in STAGING_STATE_TABLES:
        if name in (
            *STAGING_V8_STATE_TABLES,
            *STAGING_V9_STATE_TABLES,
            *STAGING_V10_STATE_TABLES,
        ):
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v7(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *SHARED_READ_VIEWS,
        *(
            name
            for name in STAGING_STATE_TABLES
            if name not in (
                *STAGING_V8_STATE_TABLES,
                *STAGING_V9_STATE_TABLES,
                *STAGING_V10_STATE_TABLES,
            )
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 7
        and report.migration_checksum == STAGING_SCHEMA_V7_CHECKSUM
        and report.migration_count == 7
        and set(report.runtime_grants) == _expected_v7_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {
            "relation_catalog",
            "relation_columns",
            "migration_checksum",
            "runtime_grants",
        }
    )


def _expected_v8_runtime_grants() -> set[str]:
    grants = {f"{name}:SELECT" for name in SHARED_READ_VIEWS}
    for name in STAGING_STATE_TABLES:
        if name in (*STAGING_V9_STATE_TABLES, *STAGING_V10_STATE_TABLES):
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v8(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *SHARED_READ_VIEWS,
        *(
            name
            for name in STAGING_STATE_TABLES
            if name not in (*STAGING_V9_STATE_TABLES, *STAGING_V10_STATE_TABLES)
        ),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 8
        and report.migration_checksum == STAGING_SCHEMA_V8_CHECKSUM
        and report.migration_count == 8
        and set(report.runtime_grants) == _expected_v8_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {"relation_catalog", "migration_checksum", "runtime_grants"}
    )


def _expected_v9_runtime_grants() -> set[str]:
    grants = {f"{name}:SELECT" for name in SHARED_READ_VIEWS}
    for name in STAGING_STATE_TABLES:
        if name in STAGING_V10_STATE_TABLES:
            continue
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            grants.add(f"{name}:{privilege}")
    grants.add(f"{STAGING_MIGRATION_TABLE}:SELECT")
    return grants


def _is_exact_v9(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *SHARED_READ_VIEWS,
        *(name for name in STAGING_STATE_TABLES if name not in STAGING_V10_STATE_TABLES),
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 9
        and report.migration_checksum == STAGING_SCHEMA_V9_CHECKSUM
        and report.migration_count == 9
        and set(report.runtime_grants) == _expected_v9_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts)
        == {
            "relation_catalog",
            "migration_checksum",
            "runtime_grants",
        }
    )


def _is_exact_v10(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *SHARED_READ_VIEWS,
        *STAGING_STATE_TABLES,
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 10
        and report.migration_checksum == STAGING_SCHEMA_V10_CHECKSUM
        and report.migration_count == 10
        and set(report.runtime_grants) == expected_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts) == {"migration_checksum"}
    )


def _is_exact_v11(report: StagingProvisionReport) -> bool:
    expected_relations = {
        *SHARED_READ_VIEWS,
        *STAGING_STATE_TABLES,
        STAGING_MIGRATION_TABLE,
    }
    return (
        not report.pristine
        and not report.ready
        and report.schema_owner == report.current_user
        and report.roles
        == tuple(sorted((STAGING_MIGRATOR_ROLE, STAGING_RUNTIME_ROLE)))
        and set(report.relations) == expected_relations
        and report.migration_version == 11
        and report.migration_checksum == STAGING_SCHEMA_V11_CHECKSUM
        and report.migration_count == 11
        and set(report.runtime_grants) == expected_runtime_grants()
        and report.runtime_schema_usage
        and not report.runtime_schema_create
        and report.migrator_schema_usage
        and not report.migrator_schema_create
        and set(report.conflicts) == {"migration_checksum"}
    )


async def apply_staging_database(
    conn: Any,
    *,
    runtime_password: SecretStr,
    confirm: str,
) -> StagingProvisionReport:
    """Transactionally create only the additive staging roles and namespace."""
    if confirm != STAGING_CONFIRMATION:
        raise StagingProvisionError("confirmation_mismatch")
    runtime_sql = runtime_role_ddl(runtime_password)

    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                STAGING_ADVISORY_LOCK_KEY,
            )
            before = await inspect_staging_database(conn)
            if before.ready:
                return before
            if before.upgradeable:
                if before.migration_version == 1:
                    await conn.execute(EDITORIAL_REVISIONS_VIEW_DDL)
                    for statement in EDITORIAL_REVISIONS_GRANT_DDL:
                        await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        2,
                        STAGING_SCHEMA_V2_CHECKSUM,
                    )
                if before.migration_version <= 2:
                    await conn.execute(RECOMMENDATION_EXPOSURE_TABLE_DDL)
                    for statement in RECOMMENDATION_EXPOSURE_INDEX_DDL:
                        await conn.execute(statement)
                    for statement in (
                        (
                            "REVOKE ALL ON TABLE "
                            "staging_app.app_recommendation_exposures FROM PUBLIC"
                        ),
                        (
                            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                            "staging_app.app_recommendation_exposures "
                            "TO gyeol_staging_runtime"
                        ),
                        (
                            "ALTER TABLE staging_app.app_recommendation_exposures "
                            "OWNER TO gyeol_staging_migrator"
                        ),
                    ):
                        await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        3,
                        STAGING_SCHEMA_V3_CHECKSUM,
                    )
                if before.migration_version <= 3:
                    for statement in TRENDING_STATE_TABLE_DDL:
                        await conn.execute(statement)
                    for statement in TRENDING_STATE_INDEX_DDL:
                        await conn.execute(statement)
                    for table_name in (
                        "article_bookmark_events",
                        "trending_search_snapshots",
                    ):
                        for statement in (
                            (
                                f"REVOKE ALL ON TABLE staging_app.{table_name} "
                                "FROM PUBLIC"
                            ),
                            (
                                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                                f"staging_app.{table_name} TO gyeol_staging_runtime"
                            ),
                            (
                                f"ALTER TABLE staging_app.{table_name} "
                                "OWNER TO gyeol_staging_migrator"
                            ),
                        ):
                            await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        4,
                        STAGING_SCHEMA_V4_CHECKSUM,
                    )
                if before.migration_version <= 4:
                    for statement in (
                        ARTICLE_FEED_IMPRESSION_COUNT_TABLE_DDL,
                        SESSION_ACTIVITY_DAY_TABLE_DDL,
                        ARTICLE_LIST_EXPOSURE_TABLE_DDL,
                        SESSION_ACTIVITY_DAY_INDEX_DDL,
                        *ARTICLE_LIST_EXPOSURE_INDEX_DDL,
                    ):
                        await conn.execute(statement)
                    for table_name in STAGING_V5_STATE_TABLES:
                        for statement in (
                            (
                                f"REVOKE ALL ON TABLE staging_app.{table_name} "
                                "FROM PUBLIC"
                            ),
                            (
                                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                                f"staging_app.{table_name} TO gyeol_staging_runtime"
                            ),
                            (
                                f"ALTER TABLE staging_app.{table_name} "
                                "OWNER TO gyeol_staging_migrator"
                            ),
                        ):
                            await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        5,
                        STAGING_SCHEMA_V5_CHECKSUM,
                    )
                if before.migration_version <= 5:
                    view_ddl_by_name = dict(
                        zip(SHARED_READ_VIEWS, VIEW_DDL_STATEMENTS)
                    )
                    for view_name in STAGING_V6_SHARED_VIEWS:
                        await conn.execute(view_ddl_by_name[view_name])
                        await conn.execute(
                            f"REVOKE ALL ON TABLE staging_app.{view_name} FROM PUBLIC"
                        )
                        await conn.execute(
                            "GRANT SELECT ON TABLE "
                            f"staging_app.{view_name} TO gyeol_staging_runtime"
                        )
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        6,
                        STAGING_SCHEMA_V6_CHECKSUM,
                    )
                if before.migration_version <= 6:
                    await conn.execute(
                        """
                        ALTER TABLE staging_app.app_push_registrations
                        ADD COLUMN timeline_updates_enabled BOOLEAN
                        NOT NULL DEFAULT FALSE
                        """
                    )
                    await conn.execute(TIMELINE_SUBSCRIPTION_TABLE_DDL)
                    await conn.execute(TIMELINE_SUBSCRIPTION_INDEX_DDL)
                    for statement in (
                        "REVOKE ALL ON TABLE "
                        "staging_app.app_timeline_subscriptions FROM PUBLIC",
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                        "staging_app.app_timeline_subscriptions "
                        "TO gyeol_staging_runtime",
                        "ALTER TABLE staging_app.app_timeline_subscriptions "
                        "OWNER TO gyeol_staging_migrator",
                    ):
                        await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        7,
                        STAGING_SCHEMA_V7_CHECKSUM,
                    )
                if before.migration_version <= 7:
                    for statement in (
                        "ALTER TABLE staging_app.app_push_registrations "
                        "ADD COLUMN timeline_refreshes_enabled BOOLEAN "
                        "NOT NULL DEFAULT FALSE",
                        "ALTER TABLE staging_app.app_push_registrations "
                        "ADD COLUMN timeline_additions_enabled BOOLEAN "
                        "NOT NULL DEFAULT FALSE",
                        "UPDATE staging_app.app_push_registrations "
                        "SET timeline_refreshes_enabled=TRUE, "
                        "timeline_additions_enabled=TRUE "
                        "WHERE timeline_updates_enabled",
                        TIMELINE_SUBSCRIPTION_EVENT_TABLE_DDL,
                        TIMELINE_SUBSCRIPTION_EVENT_INDEX_DDL,
                        "REVOKE ALL ON TABLE "
                        "staging_app.app_timeline_subscription_events FROM PUBLIC",
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                        "staging_app.app_timeline_subscription_events "
                        "TO gyeol_staging_runtime",
                        "ALTER TABLE staging_app.app_timeline_subscription_events "
                        "OWNER TO gyeol_staging_migrator",
                    ):
                        await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        8,
                        STAGING_SCHEMA_V8_CHECKSUM,
                    )
                if before.migration_version <= 8:
                    for statement in (
                        GUEST_SESSION_ACTIVITY_DAY_TABLE_DDL,
                        GUEST_SESSION_ACTIVITY_DAY_INDEX_DDL,
                        "REVOKE ALL ON TABLE "
                        "staging_app.app_guest_session_activity_days FROM PUBLIC",
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                        "staging_app.app_guest_session_activity_days "
                        "TO gyeol_staging_runtime",
                        "ALTER TABLE staging_app.app_guest_session_activity_days "
                        "OWNER TO gyeol_staging_migrator",
                    ):
                        await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        9,
                        STAGING_SCHEMA_V9_CHECKSUM,
                    )
                if before.migration_version <= 9:
                    for statement in (
                        *TIMELINE_PUSH_STATE_TABLE_DDL,
                        TIMELINE_PUSH_STATE_INDEX_DDL,
                    ):
                        await conn.execute(statement)
                    for table_name in STAGING_V10_STATE_TABLES:
                        for statement in (
                            (
                                f"REVOKE ALL ON TABLE staging_app.{table_name} "
                                "FROM PUBLIC"
                            ),
                            (
                                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                                f"staging_app.{table_name} TO gyeol_staging_runtime"
                            ),
                            (
                                f"ALTER TABLE staging_app.{table_name} "
                                "OWNER TO gyeol_staging_migrator"
                            ),
                        ):
                            await conn.execute(statement)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        10,
                        STAGING_SCHEMA_V10_CHECKSUM,
                    )
                if before.migration_version <= 10:
                    await conn.execute(ARTICLES_VIEW_V11_DDL)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        11,
                        STAGING_SCHEMA_V11_CHECKSUM,
                    )
                if before.migration_version <= 11:
                    await conn.execute(ARTICLES_VIEW_V12_DDL)
                    await conn.execute(
                        """
                        INSERT INTO staging_app.staging_schema_migrations(
                            version,
                            checksum
                        )
                        VALUES ($1, $2)
                        """,
                        STAGING_SCHEMA_VERSION,
                        STAGING_SCHEMA_CHECKSUM,
                    )
                after = await inspect_staging_database(conn)
                if not after.ready:
                    raise StagingProvisionError(
                        "post_apply_validation_failed",
                        details=after.conflicts,
                    )
                if after.public_acl_sha256 != before.public_acl_sha256:
                    raise StagingProvisionError("public_acl_changed")
                return replace(after, applied=True)
            if not before.pristine:
                raise StagingProvisionError("staging_objects_conflict")

            await conn.execute(MIGRATOR_ROLE_DDL)
            await conn.execute(runtime_sql)
            await conn.execute(SCHEMA_DDL)
            for statement in VIEW_DDL_STATEMENTS:
                await conn.execute(statement)
            for statement in TABLE_DDL_STATEMENTS:
                await conn.execute(statement)
            for statement in INDEX_DDL_STATEMENTS:
                await conn.execute(statement)
            for version, checksum in (
                (1, STAGING_SCHEMA_V1_CHECKSUM),
                (2, STAGING_SCHEMA_V2_CHECKSUM),
                (3, STAGING_SCHEMA_V3_CHECKSUM),
                (4, STAGING_SCHEMA_V4_CHECKSUM),
                (5, STAGING_SCHEMA_V5_CHECKSUM),
                (6, STAGING_SCHEMA_V6_CHECKSUM),
                (7, STAGING_SCHEMA_V7_CHECKSUM),
                (8, STAGING_SCHEMA_V8_CHECKSUM),
                (9, STAGING_SCHEMA_V9_CHECKSUM),
                (10, STAGING_SCHEMA_V10_CHECKSUM),
                (11, STAGING_SCHEMA_V11_CHECKSUM),
                (STAGING_SCHEMA_VERSION, STAGING_SCHEMA_CHECKSUM),
            ):
                await conn.execute(
                    """
                    INSERT INTO staging_app.staging_schema_migrations(
                        version,
                        checksum
                    )
                    VALUES ($1, $2)
                    """,
                    version,
                    checksum,
                )
            for statement in (
                *SCHEMA_GRANT_DDL_STATEMENTS,
                *VIEW_GRANT_DDL_STATEMENTS,
                *TABLE_GRANT_DDL_STATEMENTS,
                *OWNER_DDL_STATEMENTS,
                *ROLE_SETTING_DDL_STATEMENTS,
            ):
                await conn.execute(statement)

            after = await inspect_staging_database(conn)
            if not after.ready:
                raise StagingProvisionError(
                    "post_apply_validation_failed",
                    details=after.conflicts,
                )
            if after.public_acl_sha256 != before.public_acl_sha256:
                raise StagingProvisionError("public_acl_changed")
            return replace(after, applied=True)
    except StagingProvisionError:
        raise
    except Exception:
        raise StagingProvisionError("staging_apply_failed") from None


async def rotate_staging_runtime_password(
    conn: Any,
    *,
    runtime_password: SecretStr,
    confirm: str,
) -> StagingProvisionReport:
    """Rotate only the exact-ready staging runtime role credential."""
    if confirm != STAGING_CONFIRMATION:
        raise StagingProvisionError("confirmation_mismatch")
    rotation_sql = runtime_role_password_ddl(runtime_password)

    try:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                STAGING_ADVISORY_LOCK_KEY,
            )
            before = await inspect_staging_database(conn)
            if not before.ready:
                raise StagingProvisionError(
                    "staging_runtime_rotation_requires_ready"
                )

            await conn.execute(rotation_sql)

            after = await inspect_staging_database(conn)
            if not after.ready:
                raise StagingProvisionError(
                    "post_rotation_validation_failed",
                    details=after.conflicts,
                )
            if after.public_acl_sha256 != before.public_acl_sha256:
                raise StagingProvisionError("public_acl_changed")
            return replace(after, applied=True)
    except StagingProvisionError:
        raise
    except Exception:
        raise StagingProvisionError(
            "staging_runtime_rotation_failed"
        ) from None
