"""Central configuration, loaded from environment variables.

All knobs are env-driven so the same image runs unchanged on Railway,
locally, or in CI. Nothing here reaches out to the network or DB.
"""
from __future__ import annotations

from functools import lru_cache
import re
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

NEWSAPI_MAX_ATTEMPTS: Final[int] = 4
PRODUCTION_PUBLIC_BASE_URL: Final[str] = (
    "https://example.invalid"
)
POSTGRES_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z_][a-z0-9_]*$"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # ---- Database -------------------------------------------------------
    # Railway injects DATABASE_URL automatically when you attach Postgres.
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/gdelt",
        alias="DATABASE_URL",
    )
    db_pool_min: int = Field(default=1, alias="DB_POOL_MIN")
    db_pool_max: int = Field(default=10, alias="DB_POOL_MAX")
    # Production remains the default. The staging profile is deliberately
    # narrow: it reads production content through staging-owned projections
    # and writes only to the isolated staging_app namespace.
    runtime_profile: Literal["production", "staging_app"] = Field(
        default="production", alias="APP_RUNTIME_PROFILE"
    )
    database_read_schema: str = Field(
        default="public", alias="DATABASE_READ_SCHEMA"
    )
    database_write_schema: str = Field(
        default="public", alias="DATABASE_WRITE_SCHEMA"
    )

    # ---- GDELT client ---------------------------------------------------
    gdelt_base_url: str = Field(
        default="https://api.gdeltproject.org/api/v2/doc/doc",
        alias="GDELT_BASE_URL",
    )
    # DOC 2.0 caps a single response at 250 records.
    gdelt_max_records: int = Field(default=250, alias="GDELT_MAX_RECORDS")
    gdelt_timeout_s: float = Field(default=30.0, alias="GDELT_TIMEOUT_S")
    gdelt_user_agent: str = Field(
        default="gdelt-loader/1.0 (+https://github.com/)", alias="GDELT_USER_AGENT"
    )
    # Be polite: minimum seconds between calls to the GDELT endpoint.
    gdelt_min_interval_s: float = Field(default=5.0, alias="GDELT_MIN_INTERVAL_S")

    # ---- NewsAPI complementary client ----------------------------------
    # This provider is opt-in and keeps its credential out of URLs/logs.
    newsapi_enabled: bool = Field(default=False, alias="NEWSAPI_ENABLED")
    newsapi_api_key: SecretStr = Field(
        default=SecretStr(""), alias="NEWSAPI_API_KEY"
    )
    newsapi_base_url: str = Field(
        default="https://newsapi.org/v2/everything", alias="NEWSAPI_BASE_URL"
    )
    newsapi_timeout_s: float = Field(
        default=30.0, ge=1.0, le=60.0, alias="NEWSAPI_TIMEOUT_S"
    )
    newsapi_user_agent: str = Field(
        default="text-shorts-newsapi-adapter/1.0", alias="NEWSAPI_USER_AGENT"
    )
    # The user-provided account limit is 1,000/24h. Keep a 10% hard margin.
    newsapi_request_budget_24h: int = Field(
        default=900, ge=NEWSAPI_MAX_ATTEMPTS, le=900,
        alias="NEWSAPI_REQUEST_BUDGET_24H",
    )

    # ---- Image ingestion ------------------------------------------------
    fetch_images: bool = Field(default=True, alias="FETCH_IMAGES")
    # db  -> store bytes in Postgres (durable on Railway, default)
    # s3  -> upload to an S3-compatible bucket, keep only the key in PG
    # local -> write to IMAGE_LOCAL_DIR (ephemeral on Railway!)
    # none -> keep the URL only, never download bytes
    image_backend: Literal["db", "s3", "local", "none"] = Field(
        default="db", alias="IMAGE_BACKEND"
    )
    image_timeout_s: float = Field(default=20.0, alias="IMAGE_TIMEOUT_S")
    image_max_bytes: int = Field(default=8 * 1024 * 1024, alias="IMAGE_MAX_BYTES")
    image_concurrency: int = Field(default=6, alias="IMAGE_CONCURRENCY")
    image_local_dir: str = Field(default="./_images", alias="IMAGE_LOCAL_DIR")

    # Private S3-compatible storage. Railway exposes the AWS_* aliases while
    # S3_* keeps the same application contract portable to other providers.
    s3_bucket: str = Field(
        default="",
        validation_alias=AliasChoices("S3_BUCKET", "AWS_S3_BUCKET_NAME"),
    )
    s3_endpoint_url: str = Field(
        default="",
        validation_alias=AliasChoices("S3_ENDPOINT_URL", "AWS_ENDPOINT_URL"),
    )
    s3_region: str = Field(
        default="",
        validation_alias=AliasChoices("S3_REGION", "AWS_DEFAULT_REGION"),
    )
    s3_access_key_id: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    )
    s3_secret_access_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"
        ),
    )
    s3_url_style: Literal["auto", "virtual", "path"] = Field(
        default="virtual",
        validation_alias=AliasChoices("S3_URL_STYLE", "AWS_S3_URL_STYLE"),
    )
    s3_presign_ttl_s: int = Field(
        default=900, ge=60, le=7 * 24 * 60 * 60, alias="S3_PRESIGN_TTL_S"
    )
    s3_redirect_cache_s: int = Field(
        default=60, ge=0, le=24 * 60 * 60, alias="S3_REDIRECT_CACHE_S"
    )

    # ---- Article body extraction ---------------------------------------
    extract_timeout_s: float = Field(default=25.0, alias="EXTRACT_TIMEOUT_S")
    extract_max_html_bytes: int = Field(
        default=4 * 1024 * 1024, alias="EXTRACT_MAX_HTML_BYTES"
    )
    # Max images (social + in-body) collected & offered to the model per article.
    max_images_per_article: int = Field(default=6, alias="MAX_IMAGES_PER_ARTICLE")

    # ---- LLM mapping provider selection -------------------------------
    enable_mapping: bool = Field(default=True, alias="ENABLE_MAPPING")
    llm_provider: Literal["auto", "openrouter", "cerebras"] = Field(
        default="auto",
        alias="LLM_PROVIDER",
    )

    @field_validator("llm_provider", mode="before")
    @classmethod
    def normalize_llm_provider(cls, value: Any) -> Any:
        """Normalize operator spelling while keeping one provider identity."""
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        return "cerebras" if normalized == "celebras" else normalized

    # ---- LLM mapping (OpenRouter) --------------------------------------
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1/chat/completions",
        alias="OPENROUTER_BASE_URL",
    )
    # Environment-overridable; the default must support text and image inputs.
    openrouter_model: str = Field(default="x-ai/grok-4.3", alias="OPENROUTER_MODEL")
    openrouter_timeout_s: float = Field(default=90.0, alias="OPENROUTER_TIMEOUT_S")
    openrouter_max_tokens: int = Field(default=4000, alias="OPENROUTER_MAX_TOKENS")
    openrouter_temperature: float = Field(default=0.2, alias="OPENROUTER_TEMPERATURE")
    openrouter_structured_outputs_enabled: bool = Field(
        default=True,
        alias="OPENROUTER_STRUCTURED_OUTPUTS_ENABLED",
    )
    # Optional attribution headers OpenRouter recommends.
    openrouter_referer: str = Field(default="", alias="OPENROUTER_REFERER")
    openrouter_title: str = Field(default="gdelt-loader", alias="OPENROUTER_TITLE")

    # ---- LLM mapping (Cerebras) ---------------------------------------
    # The official key name is preferred. CELEBRAS_API intentionally keeps
    # compatibility with the spelling already used by the operator.
    cerebras_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CEREBRAS_API_KEY", "CELEBRAS_API"),
    )
    cerebras_base_url: str = Field(
        default="https://api.cerebras.ai/v1/chat/completions",
        alias="CEREBRAS_BASE_URL",
    )
    # The shared Cerebras endpoint currently exposes this vision-capable model.
    cerebras_model: str = Field(
        default="gemma-4-31b",
        min_length=1,
        alias="CEREBRAS_MODEL",
    )
    cerebras_timeout_s: float = Field(
        default=90.0,
        ge=1.0,
        alias="CEREBRAS_TIMEOUT_S",
    )
    cerebras_max_completion_tokens: int = Field(
        default=4000,
        ge=1,
        alias="CEREBRAS_MAX_COMPLETION_TOKENS",
    )
    cerebras_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        alias="CEREBRAS_TEMPERATURE",
    )
    cerebras_structured_outputs_enabled: bool = Field(
        default=True,
        alias="CEREBRAS_STRUCTURED_OUTPUTS_ENABLED",
    )
    # Free Trial shared endpoints accept at most two images per request.
    cerebras_max_images: Literal[2] = Field(
        default=2,
        alias="CEREBRAS_MAX_IMAGES",
    )
    # Separate from OpenRouter pacing because the providers have independent
    # account limits. Zero is unlimited.
    cerebras_requests_per_minute: int = Field(
        default=0,
        ge=0,
        alias="CEREBRAS_REQUESTS_PER_MINUTE",
    )
    # Images actually sent to the vision model (subset of stored, to bound cost).
    llm_max_images: int = Field(default=4, alias="LLM_MAX_IMAGES")

    # ---- Cross-modal image RAG -----------------------------------------
    # This is a derived, rebuildable search store. DATABASE_URL remains the
    # sole source of truth for articles, maps, image metadata and links.
    vector_database_url: str = Field(default="", alias="VECTOR_DATABASE_URL")
    image_rag_enabled: bool = Field(default=True, alias="IMAGE_RAG_ENABLED")
    image_rag_model: Literal["google/gemini-embedding-2"] = Field(
        default="google/gemini-embedding-2", alias="IMAGE_RAG_MODEL"
    )
    image_rag_dimensions: int = Field(
        default=768, ge=768, le=768, alias="IMAGE_RAG_DIMENSIONS"
    )
    image_rag_sync_limit: int = Field(
        default=50, ge=1, le=50, alias="IMAGE_RAG_SYNC_LIMIT"
    )
    image_rag_embed_batch_size: int = Field(
        default=6, ge=1, le=6, alias="IMAGE_RAG_EMBED_BATCH_SIZE"
    )
    image_rag_max_attempts: int = Field(
        default=5, ge=1, le=10, alias="IMAGE_RAG_MAX_ATTEMPTS"
    )
    image_rag_min_similarity: float = Field(
        default=0.75, ge=0.0, le=1.0, alias="IMAGE_RAG_MIN_SIMILARITY"
    )
    image_rag_candidates_per_scene: int = Field(
        default=3, ge=1, le=6, alias="IMAGE_RAG_CANDIDATES_PER_SCENE"
    )
    image_rag_max_candidates: int = Field(
        default=18, ge=1, le=18, alias="IMAGE_RAG_MAX_CANDIDATES"
    )
    image_rag_vision_batch_size: int = Field(
        default=4, ge=1, le=4, alias="IMAGE_RAG_VISION_BATCH_SIZE"
    )
    # Article text embeddings share the same fixed model/vector service but
    # have an independent bounded background lifecycle and cost limiter.
    article_embedding_enabled: bool = Field(
        default=True, alias="ARTICLE_EMBEDDING_ENABLED"
    )
    article_embedding_poll_seconds: float = Field(
        default=5.0, ge=1.0, le=300.0, alias="ARTICLE_EMBEDDING_POLL_SECONDS"
    )
    article_embedding_sync_limit: int = Field(
        default=50, ge=1, le=50, alias="ARTICLE_EMBEDDING_SYNC_LIMIT"
    )
    article_embedding_batch_size: int = Field(
        default=16, ge=1, le=16, alias="ARTICLE_EMBEDDING_BATCH_SIZE"
    )
    article_embedding_max_attempts: int = Field(
        default=5, ge=1, le=10, alias="ARTICLE_EMBEDDING_MAX_ATTEMPTS"
    )
    article_embedding_requests_per_minute: int = Field(
        default=12, ge=1, le=60, alias="ARTICLE_EMBEDDING_REQUESTS_PER_MINUTE"
    )
    article_embedding_source_max_chars: int = Field(
        default=12_000,
        ge=1_000,
        le=12_000,
        alias="ARTICLE_EMBEDDING_SOURCE_MAX_CHARS",
    )
    # Request-time semantic discovery reuses the article document namespace.
    # It is strictly additive: every failure falls back to keyword-only search.
    article_search_rag_enabled: bool = Field(
        default=True,
        alias="ARTICLE_SEARCH_RAG_ENABLED",
    )
    article_search_rag_limit: int = Field(
        default=5,
        ge=1,
        le=5,
        alias="ARTICLE_SEARCH_RAG_LIMIT",
    )
    article_search_rag_min_similarity: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        alias="ARTICLE_SEARCH_RAG_MIN_SIMILARITY",
    )
    article_search_rag_timeout_seconds: float = Field(
        default=1.2,
        ge=0.05,
        le=5.0,
        alias="ARTICLE_SEARCH_RAG_TIMEOUT_SECONDS",
    )
    # Bounded article-level parallelism. PostgreSQL admission and the local
    # semaphore both enforce this ceiling across automatic/manual Map work.
    map_concurrency: int = Field(
        default=3, ge=1, le=7, alias="MAP_CONCURRENCY"
    )
    # Pace every actual OpenRouter HTTP attempt made by Map pipelines,
    # including provider retries and vision verification. Zero is unlimited.
    map_requests_per_minute: int = Field(
        default=0, ge=0, alias="MAP_REQUESTS_PER_MINUTE"
    )
    # Target output language hint for the model ("auto" keeps source language).
    map_language: str = Field(default="auto", alias="MAP_LANGUAGE")
    # Crypto sources are overwhelmingly English while the product stays a
    # Korean reader (DR-13201), so this domain pins its output language rather
    # than following the source. Kept as a setting so it can be reverted to
    # "auto" without a deploy if the Korean path has to be paused.
    map_language_crypto: str = Field(default="ko", alias="MAP_LANGUAGE_CRYPTO")
    # Empty = inherit OPENROUTER_MODEL. Set only when the global Map model
    # cannot typeset this domain; see map_model_for.
    map_model_crypto: str = Field(default="", alias="MAP_MODEL_CRYPTO")
    # Below this the subject of a number is not settled by its context, and an
    # unsettled subject shown to a reader is misinformation (C-13203).
    numeric_tag_min_confidence: float = Field(
        default=0.7, ge=0.0, le=1.0, alias="NUMERIC_TAG_MIN_CONFIDENCE"
    )
    # Primary-source PDFs (DR-13205). Oversize is a refusal rather than a cut:
    # a truncated PDF loses pages silently instead of failing loudly.
    extract_max_pdf_bytes: int = Field(
        default=4 * 1024 * 1024, ge=0, alias="EXTRACT_MAX_PDF_BYTES"
    )
    extract_max_pdf_pages: int = Field(
        default=200, ge=1, le=2000, alias="EXTRACT_MAX_PDF_PAGES"
    )
    map_format_version: Literal[3] = Field(default=3, alias="MAP_FORMAT_VERSION")

    def map_model_for(self, coverage_domain: str) -> str:
        """Map typesetting model for one coverage domain.

        Empty means "use the global model", so core is untouched unless an
        operator sets the override deliberately. Crypto needs its own because
        the model production runs globally could not produce the evidence_refs
        envelope the Map pipeline requires (DR-13224 saw the same failure on
        staging and split the model there).
        """
        if (coverage_domain or "").strip().casefold() == "crypto":
            override = self.map_model_crypto.strip()
            if override:
                return override
        return self.openrouter_model

    def map_language_for(self, coverage_domain: str) -> str:
        """Output language for one coverage domain.

        Unknown domains take the global value: a domain that has not declared
        a language policy must not silently inherit another domain's.
        """
        if (coverage_domain or "").strip().casefold() == "crypto":
            return self.map_language_crypto
        return self.map_language


    @field_validator("map_format_version", mode="before")
    @classmethod
    def normalize_map_format_version(cls, value: Any) -> Any:
        """Accept the string representation supplied by env/dotenv, only for v3."""
        return 3 if value == "3" else value

    # Editorial pipeline gates and balanced generation profile.
    map_min_source_chars: int = Field(
        default=300, ge=100, le=5000, alias="MAP_MIN_SOURCE_CHARS"
    )
    map_scene_batch_size: int = Field(
        default=3, ge=2, le=3, alias="MAP_SCENE_BATCH_SIZE"
    )
    map_min_scenes: int = Field(default=3, ge=3, le=9, alias="MAP_MIN_SCENES")
    map_max_scenes: int = Field(default=9, ge=3, le=9, alias="MAP_MAX_SCENES")
    map_max_critic_rounds: int = Field(
        default=2, ge=0, le=4, alias="MAP_MAX_CRITIC_ROUNDS"
    )
    # Persistent Map queue: failed validation / LLM errors use backoff.
    map_max_attempts: int = Field(default=3, alias="MAP_MAX_ATTEMPTS")
    map_retry_base_delay_s: float = Field(default=120.0, alias="MAP_RETRY_BASE_DELAY_S")
    # A crashed processing row becomes eligible again after this lease.
    map_processing_lease_s: float = Field(
        default=1800.0,
        ge=300.0,
        le=21600.0,
        alias="MAP_PROCESSING_LEASE_S",
    )
    # How many queued items one drain call processes. The independent Map
    # worker immediately opens another batch while backlog remains.
    queue_drain_limit: int = Field(default=50, alias="QUEUE_DRAIN_LIMIT")

    # ---- Daily Map budget and candidate triage --------------------------
    # Ingestion stays unbounded; these bound how many candidates per Seoul
    # editorial day actually reach the Map pipeline, and which ones go first.
    map_budget_enabled: bool = Field(default=True, alias="MAP_BUDGET_ENABLED")
    # First-run defaults only. The live values live in map_budget_settings and
    # are edited from /ops, so a redeploy never silently resets operator intent.
    map_daily_limit_default: int = Field(
        default=60, ge=0, le=1000, alias="MAP_DAILY_LIMIT_DEFAULT"
    )
    map_fresh_share_default: int = Field(
        default=75, ge=0, le=100, alias="MAP_FRESH_SHARE_DEFAULT"
    )
    map_daily_run_count_default: int = Field(
        default=1, ge=1, le=24, alias="MAP_DAILY_RUN_COUNT_DEFAULT"
    )
    # Candidates never selected within this window are deleted outright.
    map_candidate_ttl_days: int = Field(
        default=3, ge=1, le=30, alias="MAP_CANDIDATE_TTL_DAYS"
    )
    map_candidate_purge_limit: int = Field(
        default=500, ge=1, le=5000, alias="MAP_CANDIDATE_PURGE_LIMIT"
    )
    map_candidate_purge_interval_s: float = Field(
        default=3600.0, ge=60.0, le=86400.0, alias="MAP_CANDIDATE_PURGE_INTERVAL_S"
    )
    # Retention runs on the worker's first tick and ignores TRIAGE_MODE, so a
    # read-only inspection surface (the isolated staging `serve`) had no way to
    # keep it from deleting candidate rows. Defaults to on: core behaviour is
    # unchanged and only an explicitly read-only profile turns it off.
    map_candidate_purge_enabled: bool = Field(
        default=True, alias="MAP_CANDIDATE_PURGE_ENABLED"
    )
    # F-4 is deferred (DR-13235) and must not reach a reader in this release.
    # The chain annotations write revision_note / revision_target_article_id /
    # chain_progress_note onto feed items, and FeedItem forbids extras, so
    # leaving them on does not just leak a deferred feature -- it fails the
    # whole response. Defaults off; flipping it on requires the fields to be
    # part of the client contract first.
    event_chain_annotations_enabled: bool = Field(
        default=False, alias="EVENT_CHAIN_ANNOTATIONS_ENABLED"
    )
    # off: no scoring at all. shadow: score but keep FIFO claim order.
    # enforce: claim the highest scoring candidate first.
    triage_mode: Literal["off", "shadow", "enforce"] = Field(
        default="shadow", alias="TRIAGE_MODE"
    )
    # Empty falls back to OPENROUTER_MODEL; a cheap model is the point here.
    triage_model: str = Field(default="", alias="TRIAGE_MODEL")
    triage_batch_size: int = Field(
        default=30, ge=5, le=50, alias="TRIAGE_BATCH_SIZE"
    )
    triage_poll_seconds: float = Field(
        default=30.0, ge=1.0, le=600.0, alias="TRIAGE_POLL_SECONDS"
    )
    triage_max_attempts: int = Field(
        default=3, ge=1, le=10, alias="TRIAGE_MAX_ATTEMPTS"
    )
    # An unscored candidate becomes claimable at the neutral score after this,
    # so a broken scorer degrades ranking instead of stopping Map work.
    triage_stale_hours: float = Field(
        default=6.0, ge=0.5, le=72.0, alias="TRIAGE_STALE_HOURS"
    )

    @property
    def triage_scoring_model(self) -> str:
        """The model used for candidate triage, falling back to the Map model."""
        return self.triage_model.strip() or self.openrouter_model

    # ---- Scheduler ------------------------------------------------------
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")
    # Cron for the recurring refresh (default: every 30 min).
    refresh_cron: str = Field(default="*/30 * * * *", alias="REFRESH_CRON")
    jobs_file: str = Field(default="jobs.yaml", alias="JOBS_FILE")
    primary_documents_file: str = Field(
        default="primary_documents.yaml", alias="PRIMARY_DOCUMENTS_FILE"
    )
    primary_document_scan_limit: int = Field(
        default=50, ge=1, le=200, alias="PRIMARY_DOCUMENT_SCAN_LIMIT"
    )
    primary_document_insert_limit: int = Field(
        default=7, ge=1, le=50, alias="PRIMARY_DOCUMENT_INSERT_LIMIT"
    )
    primary_document_outstanding_limit: int = Field(
        default=28, ge=1, le=500, alias="PRIMARY_DOCUMENT_OUTSTANDING_LIMIT"
    )
    primary_document_timeout_seconds: float = Field(
        default=20.0,
        ge=1.0,
        le=60.0,
        alias="PRIMARY_DOCUMENT_TIMEOUT_SECONDS",
    )
    # Factual publisher relationships behind the DR-13203 context line. An
    # absent file simply leaves every context empty.
    publishers_file: str = Field(default="publishers.yaml", alias="PUBLISHERS_FILE")
    # Web research into publishers the registry does not know (DR-13245~48).
    # off: never runs. shadow: researches and stores, shows nothing.
    # on: shows the hedged possibility sentence as well. Default off so a
    # deployment opts in; 1.3.0 sets PUBLISHER_RESEARCH_MODE=on.
    publisher_research_mode: Literal["off", "shadow", "on"] = Field(
        default="off", alias="PUBLISHER_RESEARCH_MODE"
    )
    # The cost ceiling: each run buys provider search plus page reads.
    publisher_research_daily_limit: int = Field(
        default=20, ge=0, le=200, alias="PUBLISHER_RESEARCH_DAILY_LIMIT"
    )
    publisher_research_cache_days: int = Field(
        default=30, ge=1, le=365, alias="PUBLISHER_RESEARCH_CACHE_DAYS"
    )
    publisher_research_timeout_s: float = Field(
        default=60.0, ge=5.0, le=180.0, alias="PUBLISHER_RESEARCH_TIMEOUT_S"
    )
    publisher_research_fetch_timeout_s: float = Field(
        default=8.0, ge=1.0, le=30.0, alias="PUBLISHER_RESEARCH_FETCH_TIMEOUT_S"
    )
    publisher_research_max_search_results: int = Field(
        default=5, ge=1, le=10, alias="PUBLISHER_RESEARCH_MAX_SEARCH_RESULTS"
    )
    publisher_research_max_search_uses: int = Field(
        default=4, ge=1, le=8, alias="PUBLISHER_RESEARCH_MAX_SEARCH_USES"
    )
    # Ticker -> provider id for the staleness comparison. An absent file simply
    # leaves every price lookup unresolved.
    market_data_ids_file: str = Field(
        default="market_data_ids.yaml", alias="MARKET_DATA_IDS_FILE"
    )

    # Human-approved editorial publication reservations run independently
    # from the 30-minute ingestion scheduler.
    publication_worker_enabled: bool = Field(
        default=True, alias="PUBLICATION_WORKER_ENABLED"
    )
    publication_poll_seconds: float = Field(
        default=15.0, ge=1.0, le=60.0, alias="PUBLICATION_POLL_SECONDS"
    )
    publication_max_attempts: int = Field(
        default=5, ge=1, le=10, alias="PUBLICATION_MAX_ATTEMPTS"
    )

    # Shared automatic discovery catalog. Polling is cheap; a provider call is
    # admitted only for a changed candidate hash after the six-hour DB gate.
    curation_worker_enabled: bool = Field(
        default=True, alias="CURATION_WORKER_ENABLED"
    )
    curation_agent_model: str = Field(
        default="deepseek/deepseek-v4-flash", alias="CURATION_AGENT_MODEL"
    )
    curation_poll_seconds: float = Field(
        default=300.0, ge=5.0, le=1800.0, alias="CURATION_POLL_SECONDS"
    )
    curation_refresh_min_seconds: float = Field(
        default=21600.0,
        ge=3600.0,
        le=86400.0,
        alias="CURATION_REFRESH_MIN_SECONDS",
    )
    curation_candidate_days: int = Field(
        default=30, ge=1, le=90, alias="CURATION_CANDIDATE_DAYS"
    )
    curation_candidate_limit: int = Field(
        default=48, ge=8, le=64, alias="CURATION_CANDIDATE_LIMIT"
    )
    curation_min_routes: Literal[2] = Field(
        default=2, alias="CURATION_MIN_ROUTES"
    )
    curation_max_routes: int = Field(
        default=4, ge=2, le=4, alias="CURATION_MAX_ROUTES"
    )
    curation_max_attempts: int = Field(
        default=3, ge=1, le=5, alias="CURATION_MAX_ATTEMPTS"
    )
    curation_retry_base_delay_s: float = Field(
        default=300.0,
        ge=30.0,
        le=3600.0,
        alias="CURATION_RETRY_BASE_DELAY_SECONDS",
    )
    curation_processing_lease_s: float = Field(
        default=600.0,
        ge=120.0,
        le=3600.0,
        alias="CURATION_PROCESSING_LEASE_SECONDS",
    )
    curation_valid_hours: int = Field(
        default=36, ge=24, le=72, alias="CURATION_VALID_HOURS"
    )

    @field_validator("curation_agent_model")
    @classmethod
    def validate_curation_agent_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("CURATION_AGENT_MODEL must not be blank")
        return value

    # Precomputed Explore timelines. Full article clustering runs every six
    # hours; candidates are reranked every four hours (six times per day).
    timeline_worker_enabled: bool = Field(
        default=True, alias="TIMELINE_WORKER_ENABLED"
    )
    timeline_poll_seconds: float = Field(
        default=300.0, ge=30.0, le=1800.0, alias="TIMELINE_POLL_SECONDS"
    )
    timeline_cluster_refresh_seconds: float = Field(
        default=21600.0,
        ge=21600.0,
        le=21600.0,
        alias="TIMELINE_CLUSTER_REFRESH_SECONDS",
    )
    timeline_candidate_refresh_seconds: float = Field(
        default=14400.0,
        ge=14400.0,
        le=14400.0,
        alias="TIMELINE_CANDIDATE_REFRESH_SECONDS",
    )
    timeline_candidate_days: int = Field(
        default=30, ge=1, le=90, alias="TIMELINE_CANDIDATE_DAYS"
    )
    timeline_candidate_limit: int = Field(
        default=500, ge=64, le=500, alias="TIMELINE_CANDIDATE_LIMIT"
    )
    # The candidate pool is stratified across this many recent KST days so a
    # cluster can span the several distinct dates a main lane requires.
    timeline_candidate_day_span: int = Field(
        default=14, ge=3, le=30, alias="TIMELINE_CANDIDATE_DAY_SPAN"
    )
    timeline_knn_neighbors: Literal[8] = Field(
        default=8, alias="TIMELINE_KNN_NEIGHBORS"
    )
    timeline_cluster_min_similarity: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        alias="TIMELINE_CLUSTER_MIN_SIMILARITY",
    )
    timeline_assignment_min_similarity: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        alias="TIMELINE_ASSIGNMENT_MIN_SIMILARITY",
    )
    timeline_min_vector_coverage: float = Field(
        default=0.8,
        ge=0.5,
        le=1.0,
        alias="TIMELINE_MIN_VECTOR_COVERAGE",
    )
    timeline_min_timelines: Literal[4] = Field(
        default=4, alias="TIMELINE_MIN_TIMELINES"
    )
    timeline_max_timelines: Literal[8] = Field(
        default=8, alias="TIMELINE_MAX_TIMELINES"
    )
    timeline_min_nodes: Literal[4] = Field(
        default=4, alias="TIMELINE_MIN_NODES"
    )
    timeline_max_nodes: Literal[8] = Field(
        default=8, alias="TIMELINE_MAX_NODES"
    )
    timeline_rerank_model: Literal["cohere/rerank-4-pro"] = Field(
        default="cohere/rerank-4-pro", alias="TIMELINE_RERANK_MODEL"
    )
    timeline_rerank_document_limit: int = Field(
        default=20, ge=8, le=20, alias="TIMELINE_RERANK_DOCUMENT_LIMIT"
    )
    timeline_max_attempts: int = Field(
        default=3, ge=1, le=5, alias="TIMELINE_MAX_ATTEMPTS"
    )
    timeline_retry_base_delay_s: float = Field(
        default=300.0,
        ge=30.0,
        le=3600.0,
        alias="TIMELINE_RETRY_BASE_DELAY_SECONDS",
    )
    timeline_processing_lease_s: float = Field(
        default=900.0,
        ge=120.0,
        le=3600.0,
        alias="TIMELINE_PROCESSING_LEASE_SECONDS",
    )
    timeline_valid_hours: int = Field(
        default=36, ge=24, le=72, alias="TIMELINE_VALID_HOURS"
    )
    timeline_stale_hours: int = Field(
        default=72, ge=36, le=168, alias="TIMELINE_STALE_HOURS"
    )

    # ---- App ------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Public origin used to turn stored image paths into absolute app URLs.
    # Set this explicitly behind a reverse proxy (for example Railway).
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    # Optional shared secret to protect the manual /refresh trigger.
    api_token: str = Field(default="", alias="API_TOKEN")

    # ---- Operations dashboard / worker telemetry ----------------------
    ops_window_minutes: int = Field(
        default=15, ge=1, le=1440, alias="OPS_WINDOW_MINUTES"
    )
    ops_heartbeat_seconds: float = Field(
        default=5.0, ge=1.0, le=60.0, alias="OPS_HEARTBEAT_SECONDS"
    )
    ops_stale_after_seconds: float = Field(
        default=20.0, ge=5.0, le=300.0, alias="OPS_STALE_AFTER_SECONDS"
    )
    ops_telemetry_retention_hours: int = Field(
        default=24, ge=1, le=24 * 30, alias="OPS_TELEMETRY_RETENTION_HOURS"
    )

    # ---- Apple App Attest -----------------------------------------------
    # App-facing endpoints fail closed until this is enabled and the App ID
    # components are configured. Existing loader/admin endpoints keep working.
    app_attest_enabled: bool = Field(default=False, alias="APP_ATTEST_ENABLED")
    app_attest_app_id_prefix: str = Field(default="", alias="APP_ATTEST_APP_ID_PREFIX")
    app_attest_bundle_id: str = Field(default="", alias="APP_ATTEST_BUNDLE_ID")
    app_attest_environment: Literal["development", "production"] = Field(
        default="development", alias="APP_ATTEST_ENVIRONMENT"
    )
    app_attest_challenge_ttl_s: int = Field(
        default=120, ge=10, le=600, alias="APP_ATTEST_CHALLENGE_TTL_S"
    )
    app_attest_clock_skew_s: int = Field(
        default=30, ge=0, le=300, alias="APP_ATTEST_CLOCK_SKEW_S"
    )
    app_feed_cooldown_s: float = Field(
        default=1.0, ge=0.25, le=30.0, alias="APP_FEED_COOLDOWN_S"
    )
    app_feed_continuation_cooldown_s: float = Field(
        default=1.0,
        ge=0.25,
        le=30.0,
        alias="APP_FEED_CONTINUATION_COOLDOWN_S",
    )
    app_feed_random_cooldown_s: float = Field(
        default=1.0,
        ge=0.25,
        le=30.0,
        alias="APP_FEED_RANDOM_COOLDOWN_S",
    )
    app_discovery_cooldown_s: float = Field(
        default=300.0, ge=60.0, le=3600.0, alias="APP_DISCOVERY_COOLDOWN_S"
    )
    app_discovery_search_cooldown_s: float = Field(
        default=1.0,
        ge=0.25,
        le=30.0,
        alias="APP_DISCOVERY_SEARCH_COOLDOWN_S",
    )
    app_attest_allowed_bundle_versions: str = Field(
        default="", alias="APP_ATTEST_ALLOWED_BUNDLE_VERSIONS"
    )
    app_attest_require_extended_attributes: bool = Field(
        default=False, alias="APP_ATTEST_REQUIRE_EXTENDED_ATTRIBUTES"
    )

    # ---- Versioned privacy consent -------------------------------------
    # The server controls the document identity recorded in the consent
    # ledger. Clients may only request the current exact version.
    privacy_policy_version: str = Field(
        default="1.5.0", alias="PRIVACY_POLICY_VERSION"
    )
    privacy_policy_url: str = Field(
        default="https://example.invalid/privacy",
        alias="PRIVACY_POLICY_URL",
    )

    # ---- Sign in with Apple accounts ------------------------------------
    # Account routes fail closed until every secret is configured. This is
    # intentionally separate from App Attest, which proves the app instance.
    apple_sign_in_enabled: bool = Field(
        default=False, alias="APPLE_SIGN_IN_ENABLED"
    )
    apple_sign_in_client_id: str = Field(
        default="", alias="APPLE_SIGN_IN_CLIENT_ID"
    )
    apple_sign_in_team_id: str = Field(
        default="", alias="APPLE_SIGN_IN_TEAM_ID"
    )
    apple_sign_in_key_id: str = Field(
        default="", alias="APPLE_SIGN_IN_KEY_ID"
    )
    apple_sign_in_private_key: SecretStr = Field(
        default=SecretStr(""), alias="APPLE_SIGN_IN_PRIVATE_KEY"
    )
    apple_token_encryption_key: SecretStr = Field(
        default=SecretStr(""), alias="APPLE_TOKEN_ENCRYPTION_KEY"
    )
    apple_sign_in_jwks_url: str = Field(
        default="https://appleid.apple.com/auth/keys",
        alias="APPLE_SIGN_IN_JWKS_URL",
    )
    apple_sign_in_token_url: str = Field(
        default="https://appleid.apple.com/auth/token",
        alias="APPLE_SIGN_IN_TOKEN_URL",
    )
    apple_sign_in_revoke_url: str = Field(
        default="https://appleid.apple.com/auth/revoke",
        alias="APPLE_SIGN_IN_REVOKE_URL",
    )
    apple_sign_in_http_timeout_s: float = Field(
        default=10.0, ge=1.0, le=30.0, alias="APPLE_SIGN_IN_HTTP_TIMEOUT_S"
    )
    account_session_ttl_days: int = Field(
        default=30, ge=1, le=365, alias="ACCOUNT_SESSION_TTL_DAYS"
    )

    # ---- Editorial APNs delivery ---------------------------------------
    # Direct provider delivery is opt-in. Disabled startup creates neither an
    # APNs HTTP client nor a worker task.
    push_notifications_enabled: bool = Field(
        default=False, alias="PUSH_NOTIFICATIONS_ENABLED"
    )
    apns_team_id: str = Field(default="", alias="APNS_TEAM_ID")
    apns_key_id: str = Field(default="", alias="APNS_KEY_ID")
    apns_private_key: SecretStr = Field(
        default=SecretStr(""), alias="APNS_PRIVATE_KEY"
    )
    apns_topic: str = Field(default="", alias="APNS_TOPIC")
    apns_timeout_s: float = Field(
        default=10.0, ge=1.0, le=30.0, alias="APNS_TIMEOUT_S"
    )
    push_poll_seconds: float = Field(
        default=5.0, ge=1.0, le=60.0, alias="PUSH_POLL_SECONDS"
    )
    push_max_attempts: int = Field(
        default=5, ge=1, le=10, alias="PUSH_MAX_ATTEMPTS"
    )
    push_lease_seconds: float = Field(
        default=60.0, ge=10.0, le=300.0, alias="PUSH_LEASE_SECONDS"
    )

    @field_validator("privacy_policy_version")
    @classmethod
    def validate_privacy_policy_version(cls, value: str) -> str:
        parts = value.strip().split(".")
        if len(parts) != 3 or any(not part.isdigit() for part in parts):
            raise ValueError("PRIVACY_POLICY_VERSION must use major.minor.patch")
        return ".".join(str(int(part)) for part in parts)

    @field_validator("privacy_policy_url")
    @classmethod
    def validate_privacy_policy_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("https://") or value.endswith("/"):
            raise ValueError("PRIVACY_POLICY_URL must be an HTTPS document URL")
        return value

    def app_attest_app_id(self) -> str:
        prefix = self.app_attest_app_id_prefix.strip().rstrip(".")
        bundle_id = self.app_attest_bundle_id.strip().lstrip(".")
        return f"{prefix}.{bundle_id}" if prefix and bundle_id else ""

    def app_attest_bundle_version_set(self) -> set[str]:
        return {
            value.strip()
            for value in self.app_attest_allowed_bundle_versions.split(",")
            if value.strip()
        }

    def validate_app_attest(self) -> None:
        if not self.app_attest_enabled:
            raise ValueError("App Attest is disabled")
        if not self.app_attest_app_id():
            raise ValueError(
                "APP_ATTEST_APP_ID_PREFIX and APP_ATTEST_BUNDLE_ID are required"
            )

    def validate_apple_sign_in(self) -> None:
        if not self.apple_sign_in_enabled:
            raise ValueError("Sign in with Apple is disabled")
        required = {
            "APPLE_SIGN_IN_CLIENT_ID": self.apple_sign_in_client_id.strip(),
            "APPLE_SIGN_IN_TEAM_ID": self.apple_sign_in_team_id.strip(),
            "APPLE_SIGN_IN_KEY_ID": self.apple_sign_in_key_id.strip(),
            "APPLE_SIGN_IN_PRIVATE_KEY": self.apple_sign_in_private_key.get_secret_value().strip(),
            "APPLE_TOKEN_ENCRYPTION_KEY": self.apple_token_encryption_key.get_secret_value().strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "incomplete Sign in with Apple configuration: "
                + ", ".join(sorted(missing))
            )

    def validate_apns(self) -> None:
        if not self.push_notifications_enabled:
            raise ValueError("push notifications are disabled")
        required = {
            "APNS_TEAM_ID": self.apns_team_id.strip(),
            "APNS_KEY_ID": self.apns_key_id.strip(),
            "APNS_PRIVATE_KEY": self.apns_private_key.get_secret_value().strip(),
            "APNS_TOPIC": self.apns_topic.strip(),
            "APPLE_TOKEN_ENCRYPTION_KEY": self.apple_token_encryption_key.get_secret_value().strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "incomplete APNs configuration: " + ", ".join(sorted(missing))
            )

    def validate_s3(self) -> None:
        """Reject partially configured private object storage before startup."""
        if self.image_backend != "s3":
            return
        required = {
            "S3_BUCKET": self.s3_bucket.strip(),
            "S3_ENDPOINT_URL": self.s3_endpoint_url.strip(),
            "S3_REGION": self.s3_region.strip(),
            "S3_ACCESS_KEY_ID": self.s3_access_key_id.get_secret_value().strip(),
            "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key.get_secret_value().strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "incomplete S3 configuration: " + ", ".join(sorted(missing))
            )
        if self.s3_redirect_cache_s >= self.s3_presign_ttl_s:
            raise ValueError("S3_REDIRECT_CACHE_S must be less than S3_PRESIGN_TTL_S")

    @model_validator(mode="after")
    def validate_runtime_database_boundary(self) -> "Settings":
        if (
            self.enable_mapping
            and len(self.configured_map_provider_names) > 1
            and self.map_concurrency < 2
        ):
            raise ValueError(
                "MAP_CONCURRENCY must be at least 2 when OpenRouter and "
                "Cerebras Map providers are both configured"
            )

        for field_name, value in (
            ("DATABASE_READ_SCHEMA", self.database_read_schema),
            ("DATABASE_WRITE_SCHEMA", self.database_write_schema),
        ):
            if POSTGRES_IDENTIFIER_RE.fullmatch(value) is None:
                raise ValueError(
                    f"{field_name} must be a lowercase PostgreSQL identifier"
                )

        if self.is_staging_app:
            if (
                self.database_read_schema != "public"
                or self.database_write_schema != "staging_app"
            ):
                raise ValueError(
                    "staging_app requires DATABASE_READ_SCHEMA=public and "
                    "DATABASE_WRITE_SCHEMA=staging_app"
                )

            origin = self.public_base_url
            parsed = urlsplit(origin)
            if (
                origin != PRODUCTION_PUBLIC_BASE_URL
                or parsed.scheme != "https"
                or parsed.hostname != "server-production-bf47.up.railway.app"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "staging_app PUBLIC_BASE_URL must be the canonical "
                    "production image origin"
                )
            if self.app_attest_enabled and (
                self.app_attest_bundle_id.strip()
                != "com.example.gyeol.staging"
                or self.app_attest_environment != "development"
            ):
                raise ValueError(
                    "staging_app App Attest requires the staging bundle "
                    "and development environment"
                )
            if (
                self.apple_sign_in_enabled
                and self.apple_sign_in_client_id.strip()
                != "com.example.gyeol.staging"
            ):
                raise ValueError(
                    "staging_app Sign in with Apple requires the staging "
                    "client identifier"
                )
        elif (
            self.database_read_schema != "public"
            or self.database_write_schema != "public"
        ):
            raise ValueError(
                "production requires DATABASE_READ_SCHEMA=public and "
                "DATABASE_WRITE_SCHEMA=public"
            )

        return self

    @property
    def is_staging_app(self) -> bool:
        return self.runtime_profile == "staging_app"

    @property
    def configured_map_provider_names(self) -> tuple[str, ...]:
        """Return keyed Map providers allowed by the routing selection."""
        candidates = (
            ("openrouter", "cerebras")
            if self.llm_provider == "auto"
            else (self.llm_provider,)
        )
        keys = {
            "openrouter": self.openrouter_api_key,
            "cerebras": self.cerebras_api_key,
        }
        return tuple(name for name in candidates if keys[name].strip())

    @property
    def has_map_provider_key(self) -> bool:
        return bool(self.configured_map_provider_names)

    def database_search_path(self) -> str:
        """Return a fixed, non-user-composable PostgreSQL namespace path."""
        if self.is_staging_app:
            return "staging_app,pg_catalog"
        return "public,pg_catalog"

    def asyncpg_dsn(self) -> str:
        """asyncpg wants a plain postgres:// DSN without the +driver suffix."""
        dsn = self.database_url
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        # Railway sometimes hands out postgres:// which asyncpg also accepts.
        return dsn

    def vector_asyncpg_dsn(self) -> str:
        """Return the separate derived-vector database DSN for asyncpg."""
        dsn = self.vector_database_url.strip()
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        return dsn

    def newsapi_logical_run_limit(self) -> int:
        """Logical 24h runs after reserving every possible retry attempt."""
        return self.newsapi_request_budget_24h // NEWSAPI_MAX_ATTEMPTS


@lru_cache
def get_settings() -> Settings:
    return Settings()
