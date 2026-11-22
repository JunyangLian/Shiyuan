"""持续自动解析的状态汇总：开关、待处理队列、最近解析活动与失败记录。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import indexer, ingest, inventory, store

ACTIVITY_LIMIT = 12
FAILURE_LIMIT = 8


def _name(path: str | None) -> str | None:
    return Path(path).name if path else None


def overview(conn: sqlite3.Connection, *, watcher_running: bool,
             watch_enabled: bool) -> dict:
    settings = ingest.load(conn)
    sources = [row for row in store.list_sources(conn) if row["state"] == "active"]

    pending_l1 = 0
    pending_l2 = 0
    awaiting_policy = 0
    for row in sources:
        if (row["full_coverage_at"] is None
                and not inventory.is_confirmed(conn, row["source_id"])):
            awaiting_policy += 1
            continue
        allowed = inventory.allowed_exts(conn, row["source_id"])
        pending_l1 += indexer.pending_count(
            conn, row["source_id"], allowed_exts=allowed)
        pending_l2 += indexer.ocr_pending_count(
            conn, row["source_id"], allowed_exts=allowed)

    since = (datetime.now().astimezone() - timedelta(hours=24)).isoformat(timespec="seconds")
    parsed_24h = conn.execute(
        "SELECT COUNT(*) FROM parse_artifacts a"
        " JOIN content_versions v ON v.version_id = a.version_id AND v.is_current = 1"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE (a.parser_signature LIKE 'l1.%' OR a.parser_signature LIKE 'l2.%')"
        "  AND a.state = 'ok' AND a.created_at >= ?",
        (since,),
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM index_jobs j"
        " JOIN content_versions v ON v.version_id = j.version_id AND v.is_current = 1"
        " WHERE j.status = 'failed'",
    ).fetchone()[0]

    recent = []
    for row in conn.execute(
        "SELECT a.state, a.quality, a.char_count, a.created_at, a.parser_signature,"
        " l.path, l.source_id, s.label AS source_label"
        " FROM parse_artifacts a"
        " JOIN content_versions v ON v.version_id = a.version_id AND v.is_current = 1"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE (a.parser_signature LIKE 'l1.%' OR a.parser_signature LIKE 'l2.%')"
        " ORDER BY a.created_at DESC LIMIT ?",
        (ACTIVITY_LIMIT,),
    ):
        recent.append({
            "name": _name(row["path"]),
            "path": row["path"],
            "source_id": row["source_id"],
            "source_label": row["source_label"],
            "stage": "L2" if row["parser_signature"].startswith("l2.") else "L1",
            "state": row["state"],
            "quality": row["quality"],
            "chars": int(row["char_count"] or 0),
            "created_at": row["created_at"],
        })

    failures = []
    for row in conn.execute(
        "SELECT j.job_id, j.source_id, j.stage, j.attempts, j.error, j.updated_at,"
        " l.path, s.label AS source_label"
        " FROM index_jobs j"
        " JOIN content_versions v ON v.version_id = j.version_id AND v.is_current = 1"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE j.status = 'failed'"
        " ORDER BY j.updated_at DESC LIMIT ?",
        (FAILURE_LIMIT,),
    ):
        failures.append({
            "job_id": row["job_id"],
            "name": _name(row["path"]),
            "path": row["path"],
            "source_id": row["source_id"],
            "source_label": row["source_label"],
            "stage": row["stage"],
            "attempts": int(row["attempts"] or 0),
            "error": row["error"],
            "updated_at": row["updated_at"],
        })

    last_sync = conn.execute(
        "SELECT MAX(last_scan_at) FROM sources WHERE state = 'active'"
    ).fetchone()[0]
    return {
        "auto_parse": bool(settings.get("auto_parse", True)),
        "paused": bool(settings.get("paused", False)),
        "profile": settings.get("profile", "balanced"),
        "watcher_running": watcher_running,
        "watch_enabled": watch_enabled,
        "last_sync_at": last_sync,
        "summary": {
            "pending_l1": pending_l1,
            "pending_l2": pending_l2,
            "parsed_24h": int(parsed_24h),
            "failed": int(failed),
            "sources": len(sources),
            "awaiting_policy": awaiting_policy,
        },
        "recent": recent,
        "failures": failures,
        "generated_at": store.now_iso(),
    }
