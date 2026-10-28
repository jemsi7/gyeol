"""Bootstrap and verify the user-reset production PostgreSQL schema.

This command creates application schema only. It deliberately contains no
restore, bytea migration, data copy, or destructive cleanup behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass

from .config import get_settings
from .db import Database

REQUIRED_TABLES = (
    "article_images",
    "articles",
    "article_view_events",
    "article_view_counts",
    "article_feed_impression_counts",
    "article_image_links",
    "article_maps",
    "article_recommendation_features",
    "article_pipeline_artifacts",
    "app_attest_challenges",
    "app_attest_keys",
    "app_accounts",
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
    "editor_operators",
    "editorial_drafts",
    "editorial_revisions",
    "editorial_publications",
    "editorial_schedules",
    "editorial_audit_log",
    "map_queue",
    "map_budget_settings",
    "map_budget_changes",
    "map_budget_ledger",
    "map_budget_domain_settings",
    "market_value_cache",
    "event_chains",
    "event_chain_members",
    "article_curation_catalogs",
    "article_curation_routes",
    "article_curation_items",
    "explore_timeline_catalogs",
    "explore_timelines",
    "explore_timeline_nodes",
    "explore_timeline_push_notifications",
    "explore_timeline_push_deliveries",
    "trending_search_snapshots",
    "app_request_cooldowns",
    "load_runs",
    "worker_instances",
    "worker_activity",
)

REQUIRED_INDEXES = (
    "idx_articles_seendate",
    "idx_articles_domain",
    "idx_articles_country",
    "idx_articles_query",
    "idx_articles_image",
    "idx_articles_document_type",
    "idx_article_view_events_article",
    "idx_link_article",
    "idx_link_image",
    "idx_maps_status",
    "idx_article_recommendation_terms",
    "idx_article_recommendation_fresh",
    "idx_pipeline_artifact_status",
    "idx_attest_challenge_lookup",
    "idx_attest_one_active_installation",
    "idx_attest_key_installation",
    "idx_account_sessions_account",
    "idx_account_sessions_expiry",
    "idx_account_session_activity_date",
    "idx_guest_session_activity_date",
    "idx_article_feedback_account_recent",
    "idx_article_bookmark_events_article_time",
    "idx_article_bookmark_events_account_time",
    "idx_account_activity_account_time",
    "idx_account_activity_type_local_time",
    "idx_account_activity_account_type_time",
    "idx_recommendation_exposure_account_time",
    "idx_recommendation_exposure_account_article",
    "idx_article_list_exposure_account_time",
    "idx_article_list_exposure_account_article",
    "idx_push_registrations_target",
    "idx_push_registrations_session",
    "idx_timeline_subscriptions_key",
    "idx_editor_operators_status",
    "idx_editorial_drafts_updated",
    "idx_editorial_drafts_deleted",
    "idx_editorial_revisions_draft",
    "idx_editorial_publications_feed",
    "idx_editorial_schedules_active_draft",
    "idx_editorial_schedules_due",
    "idx_editorial_schedules_updated",
    "idx_editorial_audit_created",
    "idx_queue_due",
    "idx_queue_fifo",
    "idx_queue_lease",
    "idx_queue_triage_pending",
    "idx_queue_triage_rank",
    "idx_articles_title_key",
    "idx_map_budget_changes_recent",
    "idx_map_budget_claims_domain",
    "idx_curation_catalog_due",
    "idx_curation_catalog_current",
    "idx_curation_one_processing",
    "idx_curation_routes_catalog",
    "idx_curation_one_representative",
    "idx_curation_items_article",
    "idx_explore_timeline_catalog_due",
    "idx_explore_timeline_catalog_current",
    "idx_explore_timeline_one_processing",
    "idx_explore_timelines_catalog",
    "idx_explore_timeline_nodes_timeline",
    "idx_explore_timeline_nodes_article",
    "idx_timeline_push_notifications_key",
    "idx_timeline_push_deliveries_due",
    "idx_app_request_cooldowns_last",
    "idx_load_runs_started",
    "idx_worker_instances_seen",
    "idx_worker_activity_window",
    "idx_worker_activity_open",
)

_COUNT_SQL = """
SELECT
    (SELECT count(*) FROM article_images) AS article_images,
    (SELECT count(*) FROM articles) AS articles,
    (SELECT count(*) FROM article_view_events) AS article_view_events,
    (SELECT count(*) FROM article_view_counts) AS article_view_counts,
    (SELECT count(*) FROM article_feed_impression_counts)
        AS article_feed_impression_counts,
    (SELECT count(*) FROM article_image_links) AS article_image_links,
    (SELECT count(*) FROM article_maps) AS article_maps,
    (SELECT count(*) FROM article_recommendation_features)
        AS article_recommendation_features,
    (SELECT count(*) FROM article_pipeline_artifacts) AS article_pipeline_artifacts,
    (SELECT count(*) FROM app_attest_challenges) AS app_attest_challenges,
    (SELECT count(*) FROM app_attest_keys) AS app_attest_keys,
    (SELECT count(*) FROM app_accounts) AS app_accounts,
    (SELECT count(*) FROM app_account_sessions) AS app_account_sessions,
    (SELECT count(*) FROM app_account_session_activity_days)
        AS app_account_session_activity_days,
    (SELECT count(*) FROM app_guest_session_activity_days)
        AS app_guest_session_activity_days,
    (SELECT count(*) FROM app_article_feedback) AS app_article_feedback,
    (SELECT count(*) FROM app_account_reading_states) AS app_account_reading_states,
    (SELECT count(*) FROM article_bookmark_events) AS article_bookmark_events,
    (SELECT count(*) FROM app_account_activity_events) AS app_account_activity_events,
    (SELECT count(*) FROM app_recommendation_exposures)
        AS app_recommendation_exposures,
    (SELECT count(*) FROM app_article_list_exposures)
        AS app_article_list_exposures,
    (SELECT count(*) FROM app_push_registrations) AS app_push_registrations,
    (SELECT count(*) FROM app_timeline_subscriptions)
        AS app_timeline_subscriptions,
    (SELECT count(*) FROM editor_operators) AS editor_operators,
    (SELECT count(*) FROM editorial_drafts) AS editorial_drafts,
    (SELECT count(*) FROM editorial_revisions) AS editorial_revisions,
    (SELECT count(*) FROM editorial_publications) AS editorial_publications,
    (SELECT count(*) FROM editorial_schedules) AS editorial_schedules,
    (SELECT count(*) FROM editorial_audit_log) AS editorial_audit_log,
    (SELECT count(*) FROM map_queue) AS map_queue,
    (SELECT count(*) FROM article_curation_catalogs) AS article_curation_catalogs,
    (SELECT count(*) FROM article_curation_routes) AS article_curation_routes,
    (SELECT count(*) FROM article_curation_items) AS article_curation_items,
    (SELECT count(*) FROM explore_timeline_catalogs) AS explore_timeline_catalogs,
    (SELECT count(*) FROM explore_timelines) AS explore_timelines,
    (SELECT count(*) FROM explore_timeline_nodes) AS explore_timeline_nodes,
    (SELECT count(*) FROM explore_timeline_push_notifications)
        AS explore_timeline_push_notifications,
    (SELECT count(*) FROM explore_timeline_push_deliveries)
        AS explore_timeline_push_deliveries,
    (SELECT count(*) FROM trending_search_snapshots) AS trending_search_snapshots,
    (SELECT count(*) FROM app_request_cooldowns) AS app_request_cooldowns,
    (SELECT count(*) FROM load_runs) AS load_runs,
    (SELECT count(*) FROM worker_instances) AS worker_instances,
    (SELECT count(*) FROM worker_activity) AS worker_activity
"""

_INVARIANT_SQL = """
SELECT
    (SELECT count(*) FROM article_images WHERE data IS NOT NULL)
        AS non_null_image_data,
    (SELECT count(*)
       FROM articles a
       LEFT JOIN article_images i ON i.id = a.image_id
      WHERE a.image_id IS NOT NULL AND i.id IS NULL)
        AS dangling_article_images,
    (SELECT count(*)
       FROM article_image_links l
       LEFT JOIN articles a ON a.id = l.article_id
       LEFT JOIN article_images i ON i.id = l.image_id
      WHERE a.id IS NULL OR i.id IS NULL)
        AS dangling_image_links
"""


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    tables: dict[str, bool]
    indexes: dict[str, bool]
    counts: dict[str, int]
    non_null_image_data: int
    dangling_article_images: int
    dangling_image_links: int
    legacy_data_absent: bool
    ok: bool


async def verify_empty_database(db: Database) -> BootstrapReport:
    if db.pool is None:
        raise RuntimeError("database is not connected")

    table_rows = await db.pool.fetch(
        """
        SELECT tablename
        FROM pg_catalog.pg_tables
        WHERE schemaname = 'public' AND tablename = ANY($1::text[])
        """,
        list(REQUIRED_TABLES),
    )
    index_rows = await db.pool.fetch(
        """
        SELECT indexname
        FROM pg_catalog.pg_indexes
        WHERE schemaname = 'public' AND indexname = ANY($1::text[])
        """,
        list(REQUIRED_INDEXES),
    )
    present_tables = {str(row["tablename"]) for row in table_rows}
    present_indexes = {str(row["indexname"]) for row in index_rows}
    tables = {name: name in present_tables for name in REQUIRED_TABLES}
    indexes = {name: name in present_indexes for name in REQUIRED_INDEXES}

    if not all(tables.values()):
        return BootstrapReport(
            tables=tables,
            indexes=indexes,
            counts={},
            non_null_image_data=-1,
            dangling_article_images=-1,
            dangling_image_links=-1,
            legacy_data_absent=False,
            ok=False,
        )

    count_row = await db.pool.fetchrow(_COUNT_SQL)
    invariant_row = await db.pool.fetchrow(_INVARIANT_SQL)
    if count_row is None or invariant_row is None:
        raise RuntimeError("database verification query returned no row")
    counts = {name: int(value) for name, value in dict(count_row).items()}
    non_null_image_data = int(invariant_row["non_null_image_data"])
    dangling_article_images = int(invariant_row["dangling_article_images"])
    dangling_image_links = int(invariant_row["dangling_image_links"])
    legacy_data_absent = (
        all(value == 0 for value in counts.values())
        and non_null_image_data == 0
        and dangling_article_images == 0
        and dangling_image_links == 0
    )
    ok = all(indexes.values()) and legacy_data_absent
    return BootstrapReport(
        tables=tables,
        indexes=indexes,
        counts=counts,
        non_null_image_data=non_null_image_data,
        dangling_article_images=dangling_article_images,
        dangling_image_links=dangling_image_links,
        legacy_data_absent=legacy_data_absent,
        ok=ok,
    )


async def bootstrap_database(db: Database, *, ensure: bool) -> BootstrapReport:
    if ensure:
        await db.ensure_schema()
    return await verify_empty_database(db)


async def _run(action: str) -> int:
    db = Database(get_settings())
    try:
        await db.connect()
        report = await bootstrap_database(db, ensure=action == "ensure")
    except Exception:
        print(json.dumps({"ok": False, "error": "db_bootstrap_failed"}))
        return 1
    finally:
        await db.close()
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "verify"))
    args = parser.parse_args()
    return asyncio.run(_run(args.action))


if __name__ == "__main__":
    raise SystemExit(main())
