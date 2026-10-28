"""asyncpg connection pool + idempotent schema bootstrap.

The schema is created on startup with `CREATE ... IF NOT EXISTS`, so there is
no separate migration step to run on Railway — deploy and go. If you later
adopt Alembic, drop `ensure_schema` and point it at your migrations instead.
"""
from __future__ import annotations

import logging
from typing import Optional

import asyncpg

from .config import Settings
from .staging_database import (
    StagingRuntimeReport,
    production_runtime_report,
    validate_staging_runtime,
)

log = logging.getLogger("gdelt.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS article_images (
    id           TEXT PRIMARY KEY,          -- sha256 of image bytes (dedup key)
    source_url   TEXT NOT NULL,
    content_type TEXT,
    byte_size    INTEGER NOT NULL DEFAULT 0,
    backend      TEXT NOT NULL,             -- db | s3 | local
    storage_ref  TEXT,                      -- s3 key / local path (NULL for db)
    data         BYTEA,                     -- populated only for backend='db'
    downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS articles (
    id             TEXT PRIMARY KEY,        -- sha256(url)
    url            TEXT NOT NULL UNIQUE,
    title          TEXT,
    seendate       TIMESTAMPTZ,
    domain         TEXT,
    language       TEXT,
    sourcecountry  TEXT,
    url_mobile     TEXT,
    socialimage_url TEXT,
    source_query   TEXT,
    image_id       TEXT REFERENCES article_images(id) ON DELETE SET NULL,
    raw            JSONB,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_articles_seendate ON articles (seendate DESC);
CREATE INDEX IF NOT EXISTS idx_articles_domain   ON articles (domain);
CREATE INDEX IF NOT EXISTS idx_articles_country  ON articles (sourcecountry);
CREATE INDEX IF NOT EXISTS idx_articles_query    ON articles (source_query);
CREATE INDEX IF NOT EXISTS idx_articles_image    ON articles (image_id);

-- Coverage domain of the collecting job (DR-13200, DR-13208). Only 'core'
-- articles enter map_queue; other domains are collected and stored but never
-- mapped, so they cannot consume the shared daily Map budget. Existing rows
-- become 'core' because every currently active job is a core-domain job --
-- that is a fact about those rows, not a fallback for an undecidable case.
-- Allowed values are enforced in Job._validate: the values all originate from
-- jobs.yaml, and a CHECK here would scan the whole articles table.
ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS coverage_domain TEXT NOT NULL DEFAULT 'core';
CREATE INDEX IF NOT EXISTS idx_articles_coverage_domain
    ON articles (coverage_domain);

-- Curated primary-source documents share the article/Map lifecycle but keep
-- their source type for deterministic section selection and strict subtype
-- budgeting (DR-13217). NULL is the ordinary article lane.
ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS document_type TEXT;
ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS document_source_name TEXT;
CREATE INDEX IF NOT EXISTS idx_articles_document_type
    ON articles (document_type)
    WHERE document_type IS NOT NULL;

-- Anonymous, idempotent receipts and a separate aggregate counter for explicit
-- Reader entries. Neither table stores an account or installation identifier.
CREATE TABLE IF NOT EXISTS article_view_events (
    id          UUID PRIMARY KEY,
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_view_events_article
    ON article_view_events (article_id, received_at DESC);

CREATE TABLE IF NOT EXISTS article_view_counts (
    article_id      TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
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
);

-- Anonymous aggregate of cards included in successful consumer article-list
-- responses. Unlike Reader views, this measures server-served impressions.
CREATE TABLE IF NOT EXISTS article_feed_impression_counts (
    article_id          TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    impression_count    BIGINT NOT NULL DEFAULT 0 CHECK (impression_count >= 0),
    first_impressed_at  TIMESTAMPTZ,
    last_impressed_at   TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
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
);

-- Many-to-many: an article can have several images, each with a stable
-- per-article ref index that the LLM map references as <img:N>.
CREATE TABLE IF NOT EXISTS article_image_links (
    article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    ref_index  INTEGER NOT NULL,                 -- the N in <img:N>
    image_id   TEXT NOT NULL REFERENCES article_images(id) ON DELETE CASCADE,
    role       TEXT,                             -- social | og | content
    source_url TEXT,
    PRIMARY KEY (article_id, ref_index)
);
CREATE INDEX IF NOT EXISTS idx_link_article ON article_image_links (article_id);
CREATE INDEX IF NOT EXISTS idx_link_image   ON article_image_links (image_id);

-- The processed "map": one per article. body_markdown lives inside `data`
-- and carries <img:N> placeholders resolvable via article_image_links.
CREATE TABLE IF NOT EXISTS article_maps (
    article_id     TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    format_version INTEGER NOT NULL DEFAULT 1,
    model          TEXT,
    status         TEXT NOT NULL DEFAULT 'ok',   -- ok | degraded | failed
    data           JSONB,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_maps_status ON article_maps (status);

-- Shared, non-personal article terms used to build bounded recommendation
-- candidate pools. Per-account profiles and served-order histories are never
-- materialized in this table.
CREATE TABLE IF NOT EXISTS article_recommendation_features (
    article_id           TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    feature_version      SMALLINT NOT NULL DEFAULT 1 CHECK (feature_version = 1),
    terms                TEXT[] NOT NULL CHECK (cardinality(terms) <= 64),
    term_weights         JSONB NOT NULL CHECK (jsonb_typeof(term_weights) = 'object'),
    representative_topic TEXT,
    domain                TEXT,
    published_at          TIMESTAMPTZ,
    quality_score         DOUBLE PRECISION NOT NULL
                          CHECK (quality_score BETWEEN 0 AND 1),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_recommendation_terms
    ON article_recommendation_features USING GIN (terms);
CREATE INDEX IF NOT EXISTS idx_article_recommendation_fresh
    ON article_recommendation_features (published_at DESC, article_id DESC);

-- First-party server operators. Plaintext bearer credentials are returned
-- exactly once; only their SHA-256 digests are persisted.
CREATE TABLE IF NOT EXISTS editor_operators (
    id            UUID PRIMARY KEY,
    display_name  TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 100),
    role          TEXT NOT NULL CHECK (role IN ('admin', 'editor')),
    token_hash    BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    token_prefix  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_editor_operators_status
    ON editor_operators (status, created_at DESC);

CREATE TABLE IF NOT EXISTS editorial_drafts (
    id          UUID PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'published', 'withdrawn')),
    document    JSONB NOT NULL,
    version     BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by  UUID NOT NULL REFERENCES editor_operators(id),
    updated_by  UUID NOT NULL REFERENCES editor_operators(id),
    deleted_at  TIMESTAMPTZ,
    deleted_by  UUID REFERENCES editor_operators(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Added later; safe to run repeatedly for existing production databases.
ALTER TABLE editorial_drafts
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE editorial_drafts
    ADD COLUMN IF NOT EXISTS deleted_by UUID REFERENCES editor_operators(id);
CREATE INDEX IF NOT EXISTS idx_editorial_drafts_updated
    ON editorial_drafts (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_editorial_drafts_deleted
    ON editorial_drafts (deleted_at DESC, id DESC)
    WHERE deleted_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS editorial_revisions (
    id              UUID PRIMARY KEY,
    draft_id        UUID NOT NULL REFERENCES editorial_drafts(id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    document        JSONB NOT NULL,
    map_data        JSONB NOT NULL,
    created_by      UUID NOT NULL REFERENCES editor_operators(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_editorial_revisions_draft
    ON editorial_revisions (draft_id, revision_number DESC);

CREATE TABLE IF NOT EXISTS editorial_publications (
    draft_id          UUID PRIMARY KEY REFERENCES editorial_drafts(id),
    article_id        TEXT NOT NULL UNIQUE REFERENCES articles(id),
    revision_id       UUID NOT NULL REFERENCES editorial_revisions(id),
    state             TEXT NOT NULL CHECK (state IN ('published', 'withdrawn')),
    published_at      TIMESTAMPTZ NOT NULL,
    published_by      UUID NOT NULL REFERENCES editor_operators(id),
    withdrawn_at      TIMESTAMPTZ,
    withdrawn_by      UUID REFERENCES editor_operators(id),
    display_order     INTEGER CHECK (display_order IS NULL OR display_order >= 0),
    is_featured       BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_editorial_publications_feed
    ON editorial_publications (state, published_at DESC, is_featured DESC, display_order);

-- Durable, human-approved publication reservations. The validated document,
-- compiled map and image links are frozen here so later draft edits cannot
-- silently change what will be published.
CREATE TABLE IF NOT EXISTS editorial_schedules (
    id                      UUID PRIMARY KEY,
    draft_id                UUID NOT NULL REFERENCES editorial_drafts(id),
    version                 BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    state                   TEXT NOT NULL
                            CHECK (state IN ('scheduled','processing','published','cancelled','failed')),
    scheduled_for           TIMESTAMPTZ NOT NULL,
    snapshot_draft_version  BIGINT NOT NULL CHECK (snapshot_draft_version > 0),
    document                JSONB NOT NULL,
    map_data                JSONB NOT NULL,
    image_links             JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by              UUID NOT NULL REFERENCES editor_operators(id),
    updated_by              UUID NOT NULL REFERENCES editor_operators(id),
    attempt_count           INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at         TIMESTAMPTZ NOT NULL,
    lease_expires_at        TIMESTAMPTZ,
    last_error              TEXT,
    publication_revision_id UUID REFERENCES editorial_revisions(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at            TIMESTAMPTZ,
    published_at            TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_editorial_schedules_active_draft
    ON editorial_schedules (draft_id)
    WHERE state IN ('scheduled', 'processing');
CREATE INDEX IF NOT EXISTS idx_editorial_schedules_due
    ON editorial_schedules (next_attempt_at, scheduled_for, created_at)
    WHERE state IN ('scheduled', 'processing');
CREATE INDEX IF NOT EXISTS idx_editorial_schedules_updated
    ON editorial_schedules (updated_at DESC);

CREATE TABLE IF NOT EXISTS editorial_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    operator_id UUID NOT NULL REFERENCES editor_operators(id),
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_editorial_audit_created
    ON editorial_audit_log (created_at DESC);

-- Checkpoints for the multi-stage editorial pipeline. One latest artifact is
-- kept per article/stage; input_hash prevents stale reuse after source, prompt,
-- model, or upstream-artifact changes.
CREATE TABLE IF NOT EXISTS article_pipeline_artifacts (
    article_id       TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    stage            TEXT NOT NULL,
    input_hash       TEXT NOT NULL,
    artifact_version INTEGER NOT NULL DEFAULT 1,
    model            TEXT,
    status           TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    data             JSONB,
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (article_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_artifact_status
    ON article_pipeline_artifacts (status, updated_at DESC);

-- One-time challenges for App Attest registration and request assertions.
CREATE TABLE IF NOT EXISTS app_attest_challenges (
    id              UUID PRIMARY KEY,
    installation_id TEXT NOT NULL,
    key_id          TEXT NOT NULL,
    purpose         TEXT NOT NULL CHECK (purpose IN ('attestation', 'assertion')),
    challenge       BYTEA NOT NULL CHECK (octet_length(challenge) = 32),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_attest_challenge_lookup
    ON app_attest_challenges (installation_id, key_id, purpose, expires_at DESC);

-- Apple-certified public key and replay/cooldown state for one app install.
CREATE TABLE IF NOT EXISTS app_attest_keys (
    key_id              TEXT PRIMARY KEY,
    installation_id     TEXT NOT NULL,
    public_key_pem      BYTEA NOT NULL,
    receipt             BYTEA NOT NULL,
    environment         TEXT NOT NULL CHECK (environment IN ('development', 'production')),
    sign_count          BIGINT NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
    device              JSONB NOT NULL,
    validation_category INTEGER,
    bundle_version      TEXT,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    last_feed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attest_one_active_installation
    ON app_attest_keys (installation_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_attest_key_installation
    ON app_attest_keys (installation_id, status);

-- One server-confirmed acceptance per attested installation and policy
-- version. The receipt is safe to retry and contains no account or token PII.
CREATE TABLE IF NOT EXISTS app_privacy_consents (
    id              UUID PRIMARY KEY,
    installation_id TEXT NOT NULL,
    key_id          TEXT NOT NULL REFERENCES app_attest_keys(key_id) ON DELETE CASCADE,
    policy_version  TEXT NOT NULL CHECK (char_length(policy_version) BETWEEN 5 AND 32),
    policy_url      TEXT NOT NULL CHECK (char_length(policy_url) BETWEEN 12 AND 2048),
    app_version     TEXT NOT NULL CHECK (char_length(app_version) BETWEEN 1 AND 64),
    build_number    TEXT NOT NULL CHECK (char_length(build_number) BETWEEN 1 AND 64),
    accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (installation_id, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_privacy_consents_accepted
    ON app_privacy_consents (accepted_at DESC);

-- Server-verified Sign in with Apple accounts. Apple refresh tokens are
-- encrypted by the application before reaching this table.
CREATE TABLE IF NOT EXISTS app_accounts (
    id                      UUID PRIMARY KEY,
    apple_subject           TEXT NOT NULL UNIQUE,
    email                   TEXT,
    given_name              TEXT,
    family_name             TEXT,
    apple_refresh_token     BYTEA NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_signed_in_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A pre-login installation receipt can later be associated with each Apple
-- account that authenticates on that installation. Account deletion removes
-- only the association; the installation receipt follows its App Attest key.
CREATE TABLE IF NOT EXISTS app_privacy_consent_accounts (
    consent_id UUID NOT NULL REFERENCES app_privacy_consents(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    linked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consent_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_privacy_consent_accounts_account
    ON app_privacy_consent_accounts (account_id, linked_at DESC);

-- Opaque app sessions. Only SHA-256 digests are persisted; bearer plaintext
-- is returned to the client exactly once at creation time.
CREATE TABLE IF NOT EXISTS app_account_sessions (
    id              UUID PRIMARY KEY,
    account_id      UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    token_hash      BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_sessions_account
    ON app_account_sessions (account_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_sessions_expiry
    ON app_account_sessions (expires_at);

-- One privacy-minimal proof that an authenticated account refreshed a valid
-- session on a given Seoul calendar day.  Session rows themselves expire or
-- are deleted on logout, so this bounded ledger is the DAU/MAU source.
CREATE TABLE IF NOT EXISTS app_account_session_activity_days (
    account_id    UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    active_date   DATE NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, active_date),
    CHECK (first_seen_at <= last_seen_at)
);
CREATE INDEX IF NOT EXISTS idx_account_session_activity_date
    ON app_account_session_activity_days (active_date, account_id);

-- Privacy-minimal daily activity for an attested guest installation. The
-- installation identifier is SHA-256 hashed before it reaches this ledger.
CREATE TABLE IF NOT EXISTS app_guest_session_activity_days (
    installation_hash BYTEA NOT NULL CHECK (octet_length(installation_hash) = 32),
    active_date       DATE NOT NULL,
    first_seen_at     TIMESTAMPTZ NOT NULL,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (installation_hash, active_date),
    CHECK (first_seen_at <= last_seen_at)
);
CREATE INDEX IF NOT EXISTS idx_guest_session_activity_date
    ON app_guest_session_activity_days (active_date, installation_hash);

-- One current recommendation signal per signed-in account and article.
-- Re-submission replaces the reason instead of amplifying duplicate events.
CREATE TABLE IF NOT EXISTS app_article_feedback (
    account_id  UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL CHECK (reason IN (
                    'not_relevant',
                    'too_simple',
                    'too_difficult',
                    'seen_similar',
                    'distrust_source',
                    'other'
                )),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_article_feedback_account_recent
    ON app_article_feedback (account_id, updated_at DESC);

-- Account-scoped current reading state. Only IDs, progress and preferences are
-- stored; article maps and media remain in the shared article tables.
CREATE TABLE IF NOT EXISTS app_account_reading_states (
    account_id       UUID PRIMARY KEY REFERENCES app_accounts(id) ON DELETE CASCADE,
    revision         BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    saved_articles   JSONB NOT NULL DEFAULT '{}'::jsonb
                     CHECK (jsonb_typeof(saved_articles) = 'object'),
    scene_progress   JSONB NOT NULL DEFAULT '{}'::jsonb
                     CHECK (jsonb_typeof(scene_progress) = 'object'),
    last_article_id  TEXT REFERENCES articles(id) ON DELETE SET NULL,
    settings         JSONB NOT NULL DEFAULT '{
        "type_size":"standard",
        "leading":"comfortable",
        "theme":"light",
        "motion_enabled":true,
        "media_saver":false,
        "offline_cache":true
    }'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exact server-applied bookmark transitions. Current reading state remains the
-- source of truth; this bounded ledger preserves removals for future rate work.
CREATE TABLE IF NOT EXISTS article_bookmark_events (
    id          UUID PRIMARY KEY,
    account_id  UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL CHECK (event_type IN ('saved','unsaved')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_article_bookmark_events_article_time
    ON article_bookmark_events (article_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_article_bookmark_events_account_time
    ON article_bookmark_events (account_id, received_at DESC);

-- Append-only account activity used for time-of-day behavior analysis.
-- Client UUIDs make offline retry idempotent. Article IDs deliberately stay
-- opaque so a later article purge does not erase the account's retained event.
CREATE TABLE IF NOT EXISTS app_account_activity_events (
    id                 UUID PRIMARY KEY,
    account_id         UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    event_type         TEXT NOT NULL CHECK (event_type IN (
                           'search',
                           'article_opened',
                           'scene_viewed',
                           'article_completed'
                       )),
    occurred_at         TIMESTAMPTZ NOT NULL,
    time_zone           TEXT NOT NULL CHECK (
                           char_length(time_zone) BETWEEN 1 AND 64
                           AND time_zone ~ '^[A-Za-z0-9_+./-]+$'
                       ),
    utc_offset_minutes  SMALLINT NOT NULL CHECK (
                           utc_offset_minutes BETWEEN -720 AND 840
                       ),
    local_date          DATE NOT NULL,
    local_hour          SMALLINT NOT NULL CHECK (local_hour BETWEEN 0 AND 23),
    query               TEXT CHECK (
                           query IS NULL OR (
                               char_length(query) BETWEEN 2 AND 80
                               AND position(E'\n' in query) = 0
                               AND position(E'\r' in query) = 0
                           )
                       ),
    result_count        SMALLINT CHECK (
                           result_count IS NULL
                           OR result_count BETWEEN 0 AND 1000
                       ),
    article_id          TEXT CHECK (
                           article_id IS NULL OR (
                               char_length(article_id) BETWEEN 1 AND 128
                               AND position(E'\n' in article_id) = 0
                               AND position(E'\r' in article_id) = 0
                           )
                       ),
    scene_index         SMALLINT CHECK (
                           scene_index IS NULL OR scene_index BETWEEN 0 AND 999
                       ),
    scene_count         SMALLINT CHECK (
                           scene_count IS NULL OR scene_count BETWEEN 1 AND 1000
                       ),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
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
            event_type IN ('article_opened', 'scene_viewed', 'article_completed')
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
);
CREATE INDEX IF NOT EXISTS idx_account_activity_account_time
    ON app_account_activity_events (account_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_account_activity_type_local_time
    ON app_account_activity_events (event_type, local_date, local_hour);
CREATE INDEX IF NOT EXISTS idx_account_activity_account_type_time
    ON app_account_activity_events (account_id, event_type, occurred_at DESC);

-- Bounded account-scoped recommendation exposure ledger. It stores only
-- article identifiers and ranking metadata, never raw queries or profile
-- vectors. The latest initial Home slate is used to rotate at least three of
-- five cards on a normal revisit, while cursor pages exclude every prior item.
CREATE TABLE IF NOT EXISTS app_recommendation_exposures (
    request_id      UUID NOT NULL,
    account_id      UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    article_id      TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    ranking_version TEXT NOT NULL CHECK (
                        char_length(ranking_version) BETWEEN 1 AND 32
                    ),
    surface         TEXT NOT NULL CHECK (
                        surface IN ('home', 'article_boundary')
                    ),
    page_kind       TEXT NOT NULL CHECK (
                        page_kind IN ('initial', 'pagination', 'boundary')
                    ),
    position        SMALLINT NOT NULL CHECK (position BETWEEN 0 AND 4),
    lane            TEXT NOT NULL CHECK (
                        lane IN ('core', 'now', 'adjacent', 'surprise')
                    ),
    served_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    selected_at     TIMESTAMPTZ,
    PRIMARY KEY (request_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_recommendation_exposure_account_time
    ON app_recommendation_exposures (account_id, served_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendation_exposure_account_article
    ON app_recommendation_exposures
       (account_id, article_id, served_at DESC);

-- Account-linked list impressions for authenticated non-recommendation
-- surfaces. Anonymous App Attest feed/discovery requests never enter here.
CREATE TABLE IF NOT EXISTS app_article_list_exposures (
    response_id UUID NOT NULL,
    position    SMALLINT NOT NULL CHECK (position BETWEEN 0 AND 99),
    account_id  UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    surface     TEXT NOT NULL CHECK (surface IN ('library')),
    served_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (response_id, position)
);
CREATE INDEX IF NOT EXISTS idx_article_list_exposure_account_time
    ON app_article_list_exposures (account_id, served_at DESC);
CREATE INDEX IF NOT EXISTS idx_article_list_exposure_account_article
    ON app_article_list_exposures (account_id, article_id, served_at DESC);

-- Seed aggregate counters once from the signed-in activity ledger that
-- predates anonymous view receipts. Existing counter rows always win, making
-- repeated schema bootstrap safe and preventing later activity from re-adding
-- historical views.
INSERT INTO article_view_counts(
    article_id,
    view_count,
    first_viewed_at,
    last_viewed_at,
    updated_at
)
SELECT
    event.article_id,
    count(*)::BIGINT,
    min(event.received_at),
    max(event.received_at),
    max(event.received_at)
FROM app_account_activity_events event
JOIN articles article ON article.id = event.article_id
WHERE event.event_type = 'article_opened'
GROUP BY event.article_id
ON CONFLICT (article_id) DO NOTHING;

-- One APNs address per attested app installation and active account session.
-- The opaque token must be recoverable for delivery, so only ciphertext and a
-- deduplication digest are persisted. Session/account logout cascades remove it.
CREATE TABLE IF NOT EXISTS app_push_registrations (
    id                UUID PRIMARY KEY,
    account_id        UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    session_id        UUID NOT NULL REFERENCES app_account_sessions(id) ON DELETE CASCADE,
    installation_id   TEXT NOT NULL UNIQUE,
    key_id            TEXT NOT NULL REFERENCES app_attest_keys(key_id) ON DELETE CASCADE,
    token_hash        BYTEA NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    token_ciphertext  BYTEA NOT NULL,
    environment       TEXT NOT NULL CHECK (environment IN ('development','production')),
    editorial_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    weekly_insight_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    timeline_updates_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    timeline_refreshes_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    timeline_additions_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','invalid')),
    last_error        TEXT,
    invalidated_at    TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_push_registrations
    ADD COLUMN IF NOT EXISTS editorial_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE app_push_registrations
    ADD COLUMN IF NOT EXISTS weekly_insight_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app_push_registrations
    ADD COLUMN IF NOT EXISTS timeline_updates_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app_push_registrations
    ADD COLUMN IF NOT EXISTS timeline_refreshes_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE app_push_registrations
    ADD COLUMN IF NOT EXISTS timeline_additions_enabled BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE app_push_registrations
SET timeline_refreshes_enabled=TRUE,
    timeline_additions_enabled=TRUE
WHERE timeline_updates_enabled
  AND NOT timeline_refreshes_enabled
  AND NOT timeline_additions_enabled;
CREATE INDEX IF NOT EXISTS idx_push_registrations_target
    ON app_push_registrations (status, environment, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_push_registrations_session
    ON app_push_registrations (session_id);

CREATE TABLE IF NOT EXISTS app_timeline_subscriptions (
    account_id          UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    timeline_stable_key TEXT NOT NULL
                        CHECK (char_length(timeline_stable_key) BETWEEN 1 AND 128),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, timeline_stable_key)
);
CREATE INDEX IF NOT EXISTS idx_timeline_subscriptions_key
    ON app_timeline_subscriptions (timeline_stable_key, account_id);

CREATE TABLE IF NOT EXISTS app_timeline_subscription_events (
    id                  UUID PRIMARY KEY,
    account_id          UUID NOT NULL REFERENCES app_accounts(id) ON DELETE CASCADE,
    timeline_stable_key TEXT NOT NULL
                        CHECK (char_length(timeline_stable_key) BETWEEN 1 AND 128),
    event_type          TEXT NOT NULL CHECK (event_type IN ('subscribe','unsubscribe')),
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_timeline_subscription_events_account
    ON app_timeline_subscription_events (account_id, occurred_at DESC, id DESC);

-- One immutable notification per manually authored editorial's first
-- publication. Recipient rows are snapshotted in the same transaction as the
-- revision so publication and notification intent cannot diverge.
CREATE TABLE IF NOT EXISTS editorial_push_notifications (
    id          UUID PRIMARY KEY,
    draft_id    UUID NOT NULL UNIQUE REFERENCES editorial_drafts(id) ON DELETE CASCADE,
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    revision_id UUID NOT NULL REFERENCES editorial_revisions(id) ON DELETE CASCADE,
    publication_kind TEXT NOT NULL DEFAULT 'editorial'
                     CHECK (publication_kind IN ('editorial','weekly_insight')),
    headline    TEXT NOT NULL CHECK (char_length(headline) BETWEEN 1 AND 200),
    body        TEXT NOT NULL CHECK (char_length(body) <= 240),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE editorial_push_notifications
    ADD COLUMN IF NOT EXISTS publication_kind TEXT NOT NULL DEFAULT 'editorial'
    CHECK (publication_kind IN ('editorial','weekly_insight'));

CREATE TABLE IF NOT EXISTS editorial_push_deliveries (
    notification_id UUID NOT NULL REFERENCES editorial_push_notifications(id) ON DELETE CASCADE,
    registration_id UUID NOT NULL REFERENCES app_push_registrations(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','processing','sent','failed','cancelled')),
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    apns_id          UUID,
    last_error       TEXT,
    delivered_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (notification_id, registration_id)
);
CREATE INDEX IF NOT EXISTS idx_editorial_push_deliveries_due
    ON editorial_push_deliveries (next_attempt_at, created_at, notification_id, registration_id)
    WHERE status IN ('queued','processing');

-- Persistent lifecycle for every article's LLM Map work. New articles start
-- queued; workers claim one processing lease at a time and finish done, or
-- requeue with exponential backoff until terminal dead.
CREATE TABLE IF NOT EXISTS map_queue (
    article_id      TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued|processing|done|dead
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    lease_owner     TEXT,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_queue_due ON map_queue (status, next_attempt_at);
-- Added later; safe to run repeatedly for existing production databases.
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS lease_owner TEXT;
CREATE INDEX IF NOT EXISTS idx_queue_fifo
    ON map_queue (status, next_attempt_at, created_at, article_id);
CREATE INDEX IF NOT EXISTS idx_queue_lease
    ON map_queue (status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_queue_live_owner
    ON map_queue (status, lease_owner)
    WHERE status = 'processing' AND lease_owner IS NOT NULL;
-- Candidate triage. Ingestion still enqueues every article; these columns rank
-- which candidates the bounded daily Map budget actually spends itself on.
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS triage_score INTEGER
    CHECK (triage_score IS NULL OR triage_score BETWEEN 0 AND 100);
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS triage_signals JSONB;
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS triage_model TEXT;
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS triage_scored_at TIMESTAMPTZ;
ALTER TABLE map_queue ADD COLUMN IF NOT EXISTS triage_attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_queue_triage_pending
    ON map_queue (status, triage_scored_at, created_at);
CREATE INDEX IF NOT EXISTS idx_queue_triage_rank
    ON map_queue (status, triage_score DESC, next_attempt_at);
-- Syndicated copies share an exact headline; this makes the duplicate lookup
-- an index probe instead of a scan over every stored article.
CREATE INDEX IF NOT EXISTS idx_articles_title_key
    ON articles (lower(btrim(title)));

-- Daily Map conversion budget. Ingestion volume no longer decides provider
-- spend: one editable settings row bounds it, and the ledger records what each
-- Seoul editorial day actually consumed per candidate bucket.
CREATE TABLE IF NOT EXISTS map_budget_settings (
    id                  SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    daily_map_limit     INTEGER NOT NULL CHECK (daily_map_limit BETWEEN 0 AND 1000),
    fresh_share_percent INTEGER NOT NULL CHECK (fresh_share_percent BETWEEN 0 AND 100),
    daily_run_count     INTEGER NOT NULL DEFAULT 1
                        CHECK (daily_run_count BETWEEN 1 AND 24),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE map_budget_settings
    ADD COLUMN IF NOT EXISTS daily_run_count INTEGER NOT NULL DEFAULT 1;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='map_budget_settings'::regclass
          AND conname='map_budget_settings_daily_run_count_check'
    ) THEN
        ALTER TABLE map_budget_settings
            ADD CONSTRAINT map_budget_settings_daily_run_count_check
            CHECK (daily_run_count BETWEEN 1 AND 24);
    END IF;
END $$;
-- Existing databases retain the original generated 0..500 CHECK because
-- CREATE TABLE IF NOT EXISTS does not update constraints. Widen it once.
DO $$
DECLARE
    daily_limit_definition TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid)
      INTO daily_limit_definition
      FROM pg_constraint
     WHERE conrelid='map_budget_settings'::regclass
       AND conname='map_budget_settings_daily_map_limit_check';
    IF daily_limit_definition IS NOT NULL
       AND position('1000' in daily_limit_definition) = 0 THEN
        ALTER TABLE map_budget_settings
            DROP CONSTRAINT map_budget_settings_daily_map_limit_check;
        ALTER TABLE map_budget_settings
            ADD CONSTRAINT map_budget_settings_daily_map_limit_check
            CHECK (daily_map_limit BETWEEN 0 AND 1000);
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS map_budget_changes (
    id                  BIGSERIAL PRIMARY KEY,
    previous_limit      INTEGER,
    previous_share      INTEGER,
    previous_run_count  INTEGER,
    daily_map_limit     INTEGER NOT NULL,
    fresh_share_percent INTEGER NOT NULL,
    daily_run_count     INTEGER NOT NULL DEFAULT 1,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE map_budget_changes
    ADD COLUMN IF NOT EXISTS previous_run_count INTEGER;
ALTER TABLE map_budget_changes
    ADD COLUMN IF NOT EXISTS daily_run_count INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_map_budget_changes_recent
    ON map_budget_changes (changed_at DESC);
CREATE TABLE IF NOT EXISTS map_budget_ledger (
    editorial_day DATE NOT NULL,
    bucket        TEXT NOT NULL CHECK (bucket IN ('fresh','backlog')),
    consumed      INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (editorial_day, bucket)
);
CREATE TABLE IF NOT EXISTS map_budget_claims (
    id             BIGSERIAL PRIMARY KEY,
    article_id     TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    editorial_day  DATE NOT NULL,
    bucket         TEXT NOT NULL CHECK (bucket IN ('fresh','backlog')),
    status         TEXT NOT NULL DEFAULT 'reserved'
                   CHECK (status IN ('reserved','committed','released')),
    release_reason TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_map_budget_claims_reserved_article
    ON map_budget_claims (article_id)
    WHERE status = 'reserved';
CREATE INDEX IF NOT EXISTS idx_map_budget_claims_day_status
    ON map_budget_claims (editorial_day, status, created_at DESC);

-- Per-domain daily Map allowance (DR-13206). map_budget_settings keeps the
-- global pacing (fresh share, run count) and its daily_map_limit now means the
-- ceiling across domains; this table splits that ceiling. Unused allowance
-- never carries across domains, so each domain needs its own ledger axis.
CREATE TABLE IF NOT EXISTS map_budget_domain_settings (
    coverage_domain TEXT PRIMARY KEY,
    daily_map_limit INTEGER NOT NULL
                    CHECK (daily_map_limit BETWEEN 0 AND 1000),
    document_map_limit INTEGER NOT NULL DEFAULT 0
                    CHECK (document_map_limit BETWEEN 0 AND 1000),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Seed once. DO NOTHING keeps operator edits from being reset on every boot.
-- reader_exposed is the DR-13209 gate. A domain can accumulate Maps for
-- internal measurement while staying out of every reader surface, because the
-- Map is stored with a held_* status that no reader query matches.
ALTER TABLE map_budget_domain_settings
    ADD COLUMN IF NOT EXISTS reader_exposed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE map_budget_domain_settings
    ADD COLUMN IF NOT EXISTS document_map_limit INTEGER NOT NULL DEFAULT 0;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'map_budget_domain_settings'::regclass
          AND conname = 'map_budget_domain_settings_document_map_limit_check'
    ) THEN
        ALTER TABLE map_budget_domain_settings
            ADD CONSTRAINT map_budget_domain_settings_document_map_limit_check
            CHECK (document_map_limit BETWEEN 0 AND 1000);
    END IF;
END $$;
-- The DR-13206 split. crypto held at 0 until the Korean output path existed,
-- because has_map blocks re-mapping and English Maps would have been permanent;
-- that path shipped with DR-13201/DR-13207, so a fresh database now seeds the
-- full 20. reader_exposed stays FALSE: crypto accumulates held_* Maps for the
-- tagging-accuracy measurement and reaches no reader until DR-13209 opens.
-- An already-seeded database keeps whatever it has -- DO NOTHING is deliberate,
-- since a boot must never reset an operator's limit -- so raising an existing
-- deployment from 0 to 20 is an explicit /ops action, not a migration.
INSERT INTO map_budget_domain_settings
    (coverage_domain, daily_map_limit, reader_exposed, document_map_limit)
VALUES ('core', 40, TRUE, 0), ('crypto', 20, FALSE, 7)
ON CONFLICT (coverage_domain) DO NOTHING;

-- 'core' is the correct historical value for rows written before this axis
-- existed: every consumed Map until now came from a core-domain article.
ALTER TABLE map_budget_ledger
    ADD COLUMN IF NOT EXISTS coverage_domain TEXT NOT NULL DEFAULT 'core';
ALTER TABLE map_budget_claims
    ADD COLUMN IF NOT EXISTS coverage_domain TEXT NOT NULL DEFAULT 'core';

-- Widen the ledger key from (day,bucket) to (day,domain,bucket). The column is
-- added above first, so an in-flight editorial day keeps its consumed counts.
DO $$
DECLARE
    ledger_key      TEXT;
    ledger_key_cols INTEGER;
BEGIN
    SELECT conname, array_length(conkey, 1)
      INTO ledger_key, ledger_key_cols
      FROM pg_constraint
     WHERE conrelid = 'map_budget_ledger'::regclass
       AND contype = 'p';
    IF ledger_key IS NOT NULL AND ledger_key_cols = 2 THEN
        EXECUTE format(
            'ALTER TABLE map_budget_ledger DROP CONSTRAINT %I', ledger_key
        );
        ALTER TABLE map_budget_ledger
            ADD PRIMARY KEY (editorial_day, coverage_domain, bucket);
    END IF;
END $$;

-- Reports about one unfolding incident (C-13209). An article belongs to at
-- most one chain: two chains claiming the same article would let each appear
-- to correct the other.
CREATE TABLE IF NOT EXISTS event_chains (
    id              UUID PRIMARY KEY,
    coverage_domain TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS event_chain_members (
    chain_id   UUID NOT NULL REFERENCES event_chains(id) ON DELETE CASCADE,
    article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    similarity DOUBLE PRECISION NOT NULL
               CHECK (similarity BETWEEN 0.0 AND 1.0),
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, article_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_chain_member_article
    ON event_chain_members (article_id);
CREATE INDEX IF NOT EXISTS idx_event_chain_members_joined
    ON event_chain_members (chain_id, joined_at);

-- Read-time market values behind the staleness comparison (C-13204).
-- Cached in the database rather than in process memory because the lookup
-- happens while assembling a reader response, and the API and worker
-- processes are separate deployments that would otherwise never share a hit.
CREATE TABLE IF NOT EXISTS market_value_cache (
    tag           TEXT NOT NULL,
    subject_kind  TEXT NOT NULL,
    subject_id    TEXT NOT NULL,
    value         DOUBLE PRECISION NOT NULL CHECK (value > 0),
    unit          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    attribution   TEXT NOT NULL DEFAULT '',
    observed_at   TIMESTAMPTZ NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tag, subject_kind, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_market_value_fetched
    ON market_value_cache (fetched_at);

-- Web-researched publisher interests (C-13212, DR-13247, DR-13248). One row
-- per (publisher, asset set): the same outlet can have a different interest in
-- a different asset. Rows serve three jobs at once -- the 30-day cache that
-- keeps research off the hot path, the daily-spend ledger, and the queue an
-- operator reads before promoting a publisher into publishers.yaml, after
-- which the deterministic registry answers first and research stops running
-- for that domain. 'none' verdicts are stored too: knowing there is nothing
-- to say is worth exactly as much as knowing there is.
CREATE TABLE IF NOT EXISTS publisher_interest_findings (
    domain          TEXT NOT NULL,
    assets_key      TEXT NOT NULL,
    relation        TEXT NOT NULL
                    CHECK (relation IN ('exchange_listed','project_own',
                                        'investor_portfolio','none')),
    owner           TEXT NOT NULL DEFAULT '',
    asset           TEXT NOT NULL DEFAULT '',
    confidence      TEXT NOT NULL CHECK (confidence IN ('likely','uncertain')),
    sentence        TEXT NOT NULL DEFAULT '',
    evidence        JSONB NOT NULL DEFAULT '[]'::jsonb,
    article_id      TEXT,
    editorial_day   DATE NOT NULL,
    registry_status TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (registry_status IN ('proposed','approved','rejected')),
    researched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (domain, assets_key)
);
CREATE INDEX IF NOT EXISTS idx_publisher_interest_day
    ON publisher_interest_findings (editorial_day);
CREATE INDEX IF NOT EXISTS idx_publisher_interest_status
    ON publisher_interest_findings (registry_status, researched_at DESC);

CREATE INDEX IF NOT EXISTS idx_map_budget_claims_domain
    ON map_budget_claims (editorial_day, coverage_domain, status);

-- Append-only Map attempt evidence. Pipeline artifacts remain a latest-stage
-- cache; this ledger preserves every correction and terminal classification.
CREATE TABLE IF NOT EXISTS map_attempt_events (
    id                BIGSERIAL PRIMARY KEY,
    article_id        TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    stage             TEXT NOT NULL,
    outcome           TEXT NOT NULL
                      CHECK (outcome IN (
                          'started','cached','succeeded','failed',
                          'terminal','requeued'
                      )),
    error_code        TEXT,
    error_detail      TEXT,
    retryable         BOOLEAN NOT NULL DEFAULT FALSE,
    correction_index  INTEGER CHECK (
                          correction_index IS NULL OR correction_index >= 0
                      ),
    model              TEXT,
    structured_output BOOLEAN,
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_map_attempt_events_article_time
    ON map_attempt_events (article_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_map_attempt_events_stage_code_time
    ON map_attempt_events (stage, error_code, created_at DESC);

-- Automatic discovery curation. A catalog row is also the durable work
-- ledger; routes/items become visible only after one atomic publish.
CREATE TABLE IF NOT EXISTS article_curation_catalogs (
    id               UUID PRIMARY KEY,
    editorial_day    DATE NOT NULL,
    input_hash       TEXT NOT NULL UNIQUE CHECK (char_length(input_hash) = 64),
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','processing','published','failed','superseded')),
    candidate_count  INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    model            TEXT,
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts     INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    error_code       TEXT,
    search_suggestions JSONB NOT NULL DEFAULT '[]'::jsonb,
    generated_at     TIMESTAMPTZ,
    valid_until      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_curation_catalog_due
    ON article_curation_catalogs (status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_curation_catalog_current
    ON article_curation_catalogs (status, valid_until, generated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_curation_one_processing
    ON article_curation_catalogs ((status)) WHERE status='processing';

CREATE TABLE IF NOT EXISTS article_curation_routes (
    id                UUID PRIMARY KEY,
    catalog_id        UUID NOT NULL REFERENCES article_curation_catalogs(id) ON DELETE CASCADE,
    rank              INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 8),
    slug              TEXT NOT NULL CHECK (char_length(slug) BETWEEN 2 AND 64),
    title             TEXT NOT NULL CHECK (char_length(title) BETWEEN 4 AND 100),
    summary           TEXT NOT NULL CHECK (char_length(summary) BETWEEN 20 AND 300),
    topic             TEXT NOT NULL CHECK (char_length(topic) BETWEEN 2 AND 60),
    is_representative BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (catalog_id, rank),
    UNIQUE (catalog_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_curation_routes_catalog
    ON article_curation_routes (catalog_id, rank);
CREATE UNIQUE INDEX IF NOT EXISTS idx_curation_one_representative
    ON article_curation_routes (catalog_id) WHERE is_representative;

CREATE TABLE IF NOT EXISTS article_curation_items (
    route_id      UUID NOT NULL REFERENCES article_curation_routes(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL CHECK (position BETWEEN 1 AND 4),
    article_id    TEXT NOT NULL REFERENCES articles(id) ON DELETE RESTRICT,
    role          TEXT NOT NULL CHECK (role IN ('start','context','evidence','counterpoint')),
    reason        TEXT NOT NULL CHECK (char_length(reason) BETWEEN 20 AND 180),
    lasting_value TEXT NOT NULL CHECK (char_length(lasting_value) BETWEEN 20 AND 180),
    quality       JSONB NOT NULL,
    PRIMARY KEY (route_id, position),
    UNIQUE (route_id, article_id)
);
CREATE INDEX IF NOT EXISTS idx_curation_items_article
    ON article_curation_items (article_id);

-- Precomputed, globally shared Explore timelines.  Work rows and the public
-- tree publish atomically; failed refreshes never replace the last catalog.
CREATE TABLE IF NOT EXISTS explore_timeline_catalogs (
    id                  UUID PRIMARY KEY,
    input_hash          TEXT NOT NULL UNIQUE CHECK (char_length(input_hash) = 64),
    cluster_snapshot_id TEXT NOT NULL CHECK (char_length(cluster_snapshot_id) BETWEEN 1 AND 128),
    cluster_slot        TEXT NOT NULL CHECK (char_length(cluster_slot) BETWEEN 1 AND 64),
    candidate_slot      TEXT NOT NULL UNIQUE CHECK (char_length(candidate_slot) BETWEEN 1 AND 64),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','processing','published','failed','superseded')),
    candidate_count     INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    model               TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts        INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 5),
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at    TIMESTAMPTZ,
    error_code          TEXT,
    generated_at        TIMESTAMPTZ,
    valid_until         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_explore_timeline_catalog_due
    ON explore_timeline_catalogs (status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_explore_timeline_catalog_current
    ON explore_timeline_catalogs (status, valid_until, generated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_explore_timeline_one_processing
    ON explore_timeline_catalogs ((status)) WHERE status='processing';

CREATE TABLE IF NOT EXISTS explore_timelines (
    id          UUID PRIMARY KEY,
    catalog_id  UUID NOT NULL REFERENCES explore_timeline_catalogs(id) ON DELETE CASCADE,
    stable_key  TEXT NOT NULL CHECK (char_length(stable_key) BETWEEN 1 AND 128),
    cluster_key TEXT NOT NULL CHECK (char_length(cluster_key) BETWEEN 1 AND 128),
    rank        INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 8),
    title       TEXT NOT NULL CHECK (char_length(title) BETWEEN 4 AND 100),
    summary     TEXT NOT NULL CHECK (char_length(summary) BETWEEN 20 AND 240),
    topic       TEXT NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 100),
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (started_at <= ended_at),
    UNIQUE (catalog_id, rank),
    UNIQUE (catalog_id, stable_key),
    UNIQUE (catalog_id, cluster_key)
);
CREATE INDEX IF NOT EXISTS idx_explore_timelines_catalog
    ON explore_timelines (catalog_id, rank);

CREATE TABLE IF NOT EXISTS explore_timeline_nodes (
    row_id                UUID PRIMARY KEY,
    node_id               UUID NOT NULL,
    timeline_id           UUID NOT NULL REFERENCES explore_timelines(id) ON DELETE CASCADE,
    article_id            TEXT NOT NULL REFERENCES articles(id) ON DELETE RESTRICT,
    anchor_node_id        UUID,
    lane                  TEXT NOT NULL CHECK (lane IN ('main','branch')),
    display_order         INTEGER NOT NULL CHECK (display_order BETWEEN 1 AND 8),
    update_kind           TEXT NOT NULL CHECK (update_kind IN ('existing','inserted','occurred')),
    relevance_score       DOUBLE PRECISION NOT NULL CHECK (relevance_score BETWEEN 0.0 AND 1.0),
    published_at_snapshot TIMESTAMPTZ NOT NULL,
    date_source           TEXT NOT NULL CHECK (date_source IN ('provider','first_seen')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((lane='main' AND anchor_node_id IS NULL)
        OR (lane='branch' AND anchor_node_id IS NOT NULL)),
    UNIQUE (timeline_id, node_id),
    UNIQUE (timeline_id, display_order),
    UNIQUE (timeline_id, article_id),
    FOREIGN KEY (timeline_id, anchor_node_id)
        REFERENCES explore_timeline_nodes(timeline_id, node_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_explore_timeline_nodes_timeline
    ON explore_timeline_nodes (timeline_id, display_order);
CREATE INDEX IF NOT EXISTS idx_explore_timeline_nodes_article
    ON explore_timeline_nodes (article_id);

CREATE TABLE IF NOT EXISTS explore_timeline_push_notifications (
    id                  UUID PRIMARY KEY,
    catalog_id          UUID NOT NULL
                        REFERENCES explore_timeline_catalogs(id) ON DELETE CASCADE,
    timeline_id         UUID NOT NULL
                        REFERENCES explore_timelines(id) ON DELETE CASCADE,
    timeline_stable_key TEXT NOT NULL
                        CHECK (char_length(timeline_stable_key) BETWEEN 1 AND 128),
    node_id             UUID NOT NULL,
    update_kind         TEXT NOT NULL
                        CHECK (update_kind IN ('inserted','occurred')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (catalog_id,timeline_stable_key,update_kind),
    FOREIGN KEY (timeline_id,node_id)
        REFERENCES explore_timeline_nodes(timeline_id,node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_timeline_push_notifications_key
    ON explore_timeline_push_notifications (timeline_stable_key,created_at DESC);

CREATE TABLE IF NOT EXISTS explore_timeline_push_deliveries (
    notification_id UUID NOT NULL
                    REFERENCES explore_timeline_push_notifications(id)
                    ON DELETE CASCADE,
    registration_id UUID NOT NULL
                    REFERENCES app_push_registrations(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','processing','sent','failed','cancelled')),
    attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    apns_id          UUID,
    last_error       TEXT,
    delivered_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (notification_id,registration_id)
);
CREATE INDEX IF NOT EXISTS idx_timeline_push_deliveries_due
    ON explore_timeline_push_deliveries
       (next_attempt_at,created_at,notification_id,registration_id)
    WHERE status IN ('queued','processing');

-- Five-minute, aggregate-only discovery snapshots. No account or article IDs
-- are persisted here; the row only stabilizes the small keyword candidate set.
CREATE TABLE IF NOT EXISTS trending_search_snapshots (
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
);

-- Endpoint-specific request throttles keep discovery search independent from
-- request cooldown windows such as random feed and discovery catalog/search.
CREATE TABLE IF NOT EXISTS app_request_cooldowns (
    key_id         TEXT NOT NULL REFERENCES app_attest_keys(key_id) ON DELETE CASCADE,
    scope          TEXT NOT NULL CHECK (char_length(scope) BETWEEN 1 AND 64),
    last_allowed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (key_id, scope)
);
CREATE INDEX IF NOT EXISTS idx_app_request_cooldowns_last
    ON app_request_cooldowns (last_allowed_at DESC);

-- Audit trail: one row per loader run.
CREATE TABLE IF NOT EXISTS load_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_name      TEXT,
    query         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    fetched       INTEGER NOT NULL DEFAULT 0,
    inserted      INTEGER NOT NULL DEFAULT 0,
    updated       INTEGER NOT NULL DEFAULT 0,
    images_stored INTEGER NOT NULL DEFAULT 0,
    ok            BOOLEAN NOT NULL DEFAULT TRUE,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_load_runs_started ON load_runs (started_at DESC);
-- Added later; safe to run repeatedly.
ALTER TABLE load_runs ADD COLUMN IF NOT EXISTS mapped      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE load_runs ADD COLUMN IF NOT EXISTS maps_failed INTEGER NOT NULL DEFAULT 0;

-- Process heartbeats and slot-level activity intervals for the operations
-- dashboard. Capacities are a worker-kind -> integer JSON object so one
-- process can advertise every type it can execute.
CREATE TABLE IF NOT EXISTS worker_instances (
    instance_id TEXT PRIMARY KEY,
    capacities  JSONB NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_worker_instances_seen
    ON worker_instances (last_seen_at DESC);

CREATE TABLE IF NOT EXISTS worker_activity (
    id           BIGSERIAL PRIMARY KEY,
    instance_id  TEXT NOT NULL REFERENCES worker_instances(instance_id),
    worker_kind  TEXT NOT NULL CHECK (
        worker_kind IN (
            'ingest', 'image', 'llm_map', 'retry_drain', 'curation', 'triage'
        )
    ),
    work_key     TEXT,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    outcome      TEXT
);
CREATE INDEX IF NOT EXISTS idx_worker_activity_window
    ON worker_activity (worker_kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_worker_activity_open
    ON worker_activity (instance_id, finished_at) WHERE finished_at IS NULL;

-- Existing databases received narrower kind checks. Replace the constraint only
-- when it has not yet learned every additive worker kind.
DO $$
DECLARE
    worker_kind_definition TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid)
      INTO worker_kind_definition
      FROM pg_constraint
     WHERE conrelid='worker_activity'::regclass
       AND conname='worker_activity_worker_kind_check';
    IF worker_kind_definition IS NOT NULL
       AND (position('curation' in worker_kind_definition) = 0
            OR position('triage' in worker_kind_definition) = 0) THEN
        ALTER TABLE worker_activity
            DROP CONSTRAINT worker_activity_worker_kind_check;
        ALTER TABLE worker_activity
            ADD CONSTRAINT worker_activity_worker_kind_check CHECK (
                worker_kind IN (
                    'ingest','image','llm_map','retry_drain','curation','triage'
                )
            );
    END IF;
END $$;
"""


class Database:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        if self.pool is not None:
            return
        self.pool = await asyncpg.create_pool(
            dsn=self.s.asyncpg_dsn(),
            min_size=self.s.db_pool_min,
            max_size=self.s.db_pool_max,
            command_timeout=60,
            server_settings={
                "search_path": self.s.database_search_path(),
            },
        )
        log.info("Postgres pool ready (%s..%s)", self.s.db_pool_min, self.s.db_pool_max)

    async def ensure_schema(self) -> None:
        if self.s.is_staging_app:
            raise RuntimeError("staging_schema_bootstrap_forbidden")
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        log.info("Schema ensured")

    async def validate_runtime_contract(self) -> StagingRuntimeReport:
        if not self.s.is_staging_app:
            return production_runtime_report()
        if self.pool is None:
            raise RuntimeError("database is not connected")
        async with self.pool.acquire() as conn:
            return await validate_staging_runtime(conn)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
