"""One-shot CLI runner. Useful for cron-style containers or local testing.

    python -m app.cli                 # run all jobs from jobs.yaml once
    python -m app.cli --job newsapi:ai-models-agents  # one named provider job
    python -m app.cli --query "AI" --timespan 6h   # ad-hoc query
"""
from __future__ import annotations

import argparse
import asyncio
import json

from .config import get_settings
from .db import Database
from .engine import LoaderEngine
from .image_rag import ImageRagService
from .jobs import Job, load_jobs
from .logging_conf import configure_logging
from .object_storage import S3ObjectStore
from .repository import Repository
from .telemetry import WorkerTelemetry


async def _main() -> None:
    parser = argparse.ArgumentParser(description="multi-source news loader runner")
    parser.add_argument("--job", help="run a single GDELT or NewsAPI job by name")
    parser.add_argument("--query", help="ad-hoc GDELT query")
    parser.add_argument("--timespan", default="1d")
    parser.add_argument("--max-records", type=int, default=250)
    parser.add_argument(
        "--map-pending", type=int, metavar="N", default=None,
        help="skip fetching; reconcile articles then process N Map jobs in FIFO order",
    )
    parser.add_argument(
        "--drain-queue", type=int, metavar="N", default=None,
        help="skip fetching; process up to N due persistent Map jobs in FIFO order",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)

    db = Database(settings)
    await db.connect()
    await db.ensure_schema()
    repo = Repository(db)
    await repo.reconcile_map_queue(max_attempts=settings.map_max_attempts)
    telemetry = WorkerTelemetry(settings, repo)
    await telemetry.start()
    object_store = S3ObjectStore(settings) if settings.image_backend == "s3" else None
    image_rag = ImageRagService(settings, repo, object_store)
    await image_rag.start()
    engine = LoaderEngine(settings, repo, telemetry, image_rag=image_rag)

    try:
        await engine.sync_image_rag()
        if args.drain_queue is not None:
            res = await engine.process_queue(args.drain_queue)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        if args.map_pending is not None:
            res = await engine.map_pending(args.map_pending)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        if args.query:
            results = [
                await engine.run_job(
                    Job(name="adhoc", query=args.query,
                        timespan=args.timespan, max_records=args.max_records)
                )
            ]
        else:
            jobs = load_jobs(settings.jobs_file)
            if args.job:
                jobs = [j for j in jobs if j.name == args.job]
            results = await engine.run_all(jobs)
        print(json.dumps([r.as_dict() for r in results], ensure_ascii=False, indent=2))
    finally:
        await image_rag.close()
        await telemetry.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
