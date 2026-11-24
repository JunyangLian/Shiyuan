"""知识库维护：重建正文索引、重试失败任务、重建 OCR、清理旧版本缓存。

所有操作都在后台线程串行执行，并与扫描共用 `scanner.request_scan`，
避免同一数据源被两个写入方同时修改。任何操作都会写维护日志。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import budget, db, embeddings, indexer, knowledge, scanner, store

ACTIONS = {"reindex", "retry", "ocr", "cleanup", "embed", "knowledge"}

_state = {"running": False, "kind": None, "source_id": None,
          "message": None, "started_at": None}
_lock = threading.Lock()


def running_info() -> dict:
    with _lock:
        return dict(_state)


def start(kind: str, source_id: str | None = None,
          store_path: str | Path | None = None) -> bool:
    if kind not in ACTIONS:
        raise ValueError("未知的维护操作")
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, kind=kind, source_id=source_id,
                      message=None, started_at=store.now_iso())
    threading.Thread(target=_run, args=(kind, source_id, store_path),
                     name=f"maintenance-{kind}", daemon=True).start()
    return True


def _run(kind: str, source_id: str | None, store_path) -> None:
    conn = db.connect(store_path)
    try:
        rows = store.list_sources(conn)
        if source_id and not any(row["source_id"] == source_id for row in rows):
            raise ValueError("数据源不存在")
        targets = [source_id] if source_id else [
            row["source_id"] for row in rows if row["state"] == "active"
        ]
        if kind == "knowledge":
            # 衍生知识队列不按数据源划分，直接跑一轮。
            result = knowledge.run_round(conn)
            store.log_event(conn, "maintenance_knowledge", source_id,
                            detail=json.dumps(result, ensure_ascii=False),
                            outcome="ok")
            conn.commit()
            return
        totals = {"sources": 0, "artifacts": 0, "chunks": 0, "requeued": 0,
                  "indexed": 0, "failed": 0, "skipped": 0}
        for target in targets:
            if not scanner.request_scan(target):
                totals["skipped"] += 1
                continue
            try:
                partial = _apply(conn, kind, target)
            except Exception as exc:
                totals["failed"] += 1
                store.log_event(conn, f"maintenance_{kind}_failed", target,
                                detail=str(exc)[:300], outcome="error")
                conn.commit()
                continue
            finally:
                scanner.release_scan(target)
            totals["sources"] += 1
            for key in ("artifacts", "chunks", "requeued", "indexed"):
                totals[key] += partial.get(key, 0)
            conn.commit()
        store.log_event(conn, f"maintenance_{kind}", source_id,
                        detail=json.dumps(totals, ensure_ascii=False), outcome="ok")
        conn.commit()
    except Exception as exc:
        try:
            store.log_event(conn, f"maintenance_{kind}_failed", source_id,
                            detail=str(exc)[:300], outcome="error")
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()
        with _lock:
            _state.update(running=False, kind=None, source_id=None,
                          message=None, started_at=None)


def _artifact_ids(conn: sqlite3.Connection, source_id: str, *,
                  signature: str | None = None, state: str | None = None,
                  current_only: bool = False) -> list[str]:
    sql = [
        "SELECT a.artifact_id FROM parse_artifacts a",
        "JOIN content_versions v ON v.version_id = a.version_id",
        "JOIN asset_locations l ON l.asset_id = v.asset_id",
        "WHERE l.source_id = ?",
    ]
    params: list = [source_id]
    if signature:
        sql.append("AND a.parser_signature = ?")
        params.append(signature)
    if state:
        sql.append("AND a.state = ?")
        params.append(state)
    if current_only:
        sql.append("AND v.is_current = 1")
    return [row["artifact_id"] for row in conn.execute(" ".join(sql), params)]


def _delete_artifacts(conn: sqlite3.Connection, artifact_ids: list[str]) -> int:
    for artifact_id in artifact_ids:
        conn.execute(
            "DELETE FROM chunk_fts WHERE rowid IN"
            " (SELECT rowid FROM chunks WHERE artifact_id = ?)", (artifact_id,))
        conn.execute("DELETE FROM chunks WHERE artifact_id = ?", (artifact_id,))
        conn.execute("DELETE FROM parse_artifacts WHERE artifact_id = ?", (artifact_id,))
    return len(artifact_ids)


def _clear_stage(conn: sqlite3.Connection, source_id: str, stage: str,
                 signature: str) -> int:
    removed = _delete_artifacts(conn, _artifact_ids(conn, source_id, signature=signature))
    conn.execute("DELETE FROM index_jobs WHERE source_id = ? AND stage = ?",
                 (source_id, stage))
    conn.commit()
    return removed


def _apply(conn: sqlite3.Connection, kind: str, source_id: str) -> dict:
    if kind == "reindex":
        removed = _clear_stage(conn, source_id, "l1", indexer.PARSER_SIGNATURE)
        result = indexer.index_pending(conn, source_id)
        return {"artifacts": removed, "indexed": result["indexed"] + result["empty"]}
    if kind == "ocr":
        removed = 0
        for signature in indexer.OCR_SIGNATURES.values():
            removed += _clear_stage(conn, source_id, "l2", signature)
        result = indexer.index_ocr_pending(conn, source_id)
        return {"artifacts": removed, "indexed": result["indexed"] + result["empty"]}
    if kind == "retry":
        failed = _artifact_ids(conn, source_id, state="failed")
        removed = _delete_artifacts(conn, failed)
        conn.execute(
            "UPDATE index_jobs SET attempts = 0, status = 'pending', error = NULL"
            " WHERE source_id = ? AND status = 'failed'", (source_id,))
        conn.commit()
        l1 = indexer.index_pending(conn, source_id)
        l2 = indexer.index_ocr_pending(conn, source_id)
        return {"artifacts": removed, "requeued": removed,
                "indexed": l1["indexed"] + l2["indexed"]}
    if kind == "embed":
        result = embeddings.index_all_pending(conn, source_id)
        if not result.get("available"):
            return {"indexed": 0, "skipped": 1}
        return {"indexed": result["indexed"]}
    # cleanup：只清理不再属于当前版本的缓存
    ids = []
    for artifact_id in _artifact_ids(conn, source_id):
        row = conn.execute(
            "SELECT v.is_current FROM parse_artifacts a"
            " JOIN content_versions v ON v.version_id = a.version_id"
            " WHERE a.artifact_id = ?", (artifact_id,)).fetchone()
        if row is not None and not row["is_current"]:
            ids.append(artifact_id)
    removed = _delete_artifacts(conn, ids)
    conn.execute(
        "DELETE FROM chunks WHERE version_id NOT IN (SELECT version_id FROM content_versions)")
    conn.execute(
        "DELETE FROM index_jobs WHERE version_id NOT IN"
        " (SELECT version_id FROM content_versions)")
    conn.execute(
        "DELETE FROM chunk_embeddings WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
    conn.execute(
        "DELETE FROM chunk_embedding_skips"
        " WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)")
    conn.commit()
    knowledge_removed = knowledge.cleanup_orphans(conn)
    return {"artifacts": removed, **knowledge_removed}


def stats(conn: sqlite3.Connection) -> dict:
    jobs = {
        f"{row['stage']}:{row['status']}": row["n"]
        for row in conn.execute(
            "SELECT stage, status, COUNT(*) AS n FROM index_jobs GROUP BY stage, status")
    }
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    artifacts = conn.execute("SELECT COUNT(*) FROM parse_artifacts").fetchone()[0]
    file_path = None
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            file_path = row["file"]
    size = 0
    if file_path:
        path = Path(file_path)
        for candidate in (path, path.with_name(path.name + "-wal")):
            if candidate.exists():
                size += candidate.stat().st_size
    return {
        "jobs": jobs,
        "chunks": chunks,
        "artifacts": artifacts,
        "db_bytes": size,
        "running": running_info(),
        "budget": budget.info(conn),
        "vectors": embeddings.count(conn),
        "vector_skipped": embeddings.skipped_count(conn),
        "embedding": embeddings.public_settings(conn),
        "knowledge": knowledge.stats(conn),
    }
