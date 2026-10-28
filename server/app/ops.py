"""Operations dashboard aggregation helpers.

The database supplies one authoritative ``now`` value plus process availability
and slot activity intervals. This module keeps the overlap math pure so crash,
stale-heartbeat and multi-process behavior can be tested without PostgreSQL.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable


WORKER_KINDS = (
    ("ingest", "기사 수집"),
    ("image", "이미지 처리"),
    ("llm_map", "LLM 매핑"),
    ("retry_drain", "재시도 처리"),
)


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _capacities(value: Any) -> dict[str, int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw in value.items():
        try:
            out[str(key)] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def build_worker_metrics(
    *,
    now: datetime,
    instances: Iterable[Any],
    activities: Iterable[Any],
    window_minutes: int = 15,
    stale_after_s: float = 20.0,
) -> list[dict]:
    """Return current slot load and rolling utilization for each worker kind."""
    window_start = now - timedelta(minutes=max(1, window_minutes))
    stale_cutoff = now - timedelta(seconds=max(1.0, stale_after_s))
    instance_map: dict[str, dict] = {}

    for raw in instances:
        row = _as_dict(raw)
        instance_id = str(row.get("instance_id") or "")
        started_at = row.get("started_at")
        last_seen_at = row.get("last_seen_at")
        stopped_at = row.get("stopped_at")
        if not instance_id or not isinstance(started_at, datetime) or not isinstance(
            last_seen_at, datetime
        ):
            continue
        live = stopped_at is None and last_seen_at >= stale_cutoff
        available_end = now if live else min(stopped_at or last_seen_at, now)
        instance_map[instance_id] = {
            "started_at": started_at,
            "available_end": available_end,
            "live": live,
            "capacities": _capacities(row.get("capacities")),
        }

    accum = {
        kind: {
            "kind": kind,
            "label": label,
            "active_slots": 0,
            "capacity": 0,
            "busy_seconds": 0.0,
            "capacity_seconds": 0.0,
        }
        for kind, label in WORKER_KINDS
    }

    for instance in instance_map.values():
        overlap_start = max(instance["started_at"], window_start)
        overlap_end = min(instance["available_end"], now)
        available_seconds = _seconds(overlap_start, overlap_end)
        for kind, _label in WORKER_KINDS:
            capacity = instance["capacities"].get(kind, 0)
            accum[kind]["capacity_seconds"] += available_seconds * capacity
            if instance["live"]:
                accum[kind]["capacity"] += capacity

    for raw in activities:
        row = _as_dict(raw)
        kind = row.get("worker_kind")
        instance = instance_map.get(str(row.get("instance_id") or ""))
        started_at = row.get("started_at")
        finished_at = row.get("finished_at")
        if kind not in accum or instance is None or not isinstance(started_at, datetime):
            continue
        activity_end = finished_at or instance["available_end"]
        if not isinstance(activity_end, datetime):
            continue
        overlap_start = max(started_at, window_start)
        overlap_end = min(activity_end, instance["available_end"], now)
        accum[kind]["busy_seconds"] += _seconds(overlap_start, overlap_end)
        if finished_at is None and instance["live"] and started_at <= now:
            accum[kind]["active_slots"] += 1

    result: list[dict] = []
    for kind, _label in WORKER_KINDS:
        item = accum[kind]
        current = (
            item["active_slots"] / item["capacity"] * 100
            if item["capacity"]
            else 0.0
        )
        utilization = (
            item["busy_seconds"] / item["capacity_seconds"] * 100
            if item["capacity_seconds"]
            else 0.0
        )
        result.append(
            {
                "kind": item["kind"],
                "label": item["label"],
                "active_slots": item["active_slots"],
                "capacity": item["capacity"],
                "current_percent": round(min(100.0, max(0.0, current)), 1),
                "utilization_percent": round(
                    min(100.0, max(0.0, utilization)), 1
                ),
                "busy_seconds": round(item["busy_seconds"], 1),
                "capacity_seconds": round(item["capacity_seconds"], 1),
            }
        )
    return result
