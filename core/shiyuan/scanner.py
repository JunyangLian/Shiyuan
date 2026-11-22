"""L0 目录层：完整对账与事件驱动的单文件增量同步。

两条路径共用同一套归因逻辑（`_plan_ops`）：
- 完整对账：枚举授权根目录，发现新增 / 修改 / 移动 / 失联，兜住监听丢失与休眠；
- 增量同步：watchdog 事件去抖后只处理涉及的路径，避免每次保存都全盘重扫。

STALE 语义：事件到达时先把相关位置标记为 STALE（检索立即停止把它当作当前事实），
同步成功后内容未变则恢复 AVAILABLE，内容变化则写入新版本。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Iterator

from . import db, embeddings, indexer, store

MAX_HASH_BYTES = 32 * 1024 * 1024

DEFAULT_EXCLUDES = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "$recycle.bin",
    "system volume information",
    "node_modules",
    ".venv",
    "venv",
    ".idea",
}

_running: set[str] = set()
_followup: dict[str, bool] = {}
_running_lock = threading.Lock()


class ScanError(RuntimeError):
    pass


def is_running(source_id: str) -> bool:
    with _running_lock:
        return source_id in _running


def request_scan(source_id: str) -> bool:
    """占用某数据源的同步权；已有任务在跑时记录 follow-up，结束后自动补扫。"""
    with _running_lock:
        if source_id in _running:
            _followup[source_id] = True
            return False
        _running.add(source_id)
        return True


def release_scan(source_id: str) -> None:
    with _running_lock:
        _running.discard(source_id)
        pending = _followup.pop(source_id, False)
    if pending:
        # 扫描期间到达的文件事件可能晚于本轮枚举；结束前再补一轮增量。
        scan_in_background(source_id)


def scan_in_background(source_id: str) -> bool:
    """目录对账 + 内容解析放到后台线程；接口与监听器共用这一个入口。"""
    if not request_scan(source_id):
        return False
    threading.Thread(target=_run_pipeline, args=(source_id,),
                     name=f"sync-{source_id}", daemon=True).start()
    return True


def _run_pipeline(source_id: str) -> None:
    try:
        conn = db.connect()
        try:
            scan_source(conn, source_id)
        finally:
            conn.close()
        indexer.index_pending_in_new_connection(source_id)
        # 配置了语义检索时顺手补建向量；失败不影响目录/正文结果。
        try:
            embeddings.index_in_new_connection(source_id)
        except Exception:
            pass
    finally:
        release_scan(source_id)


def _hash_file(path: Path, size: int, mtime_ns: int) -> str:
    """大文件只记录元数据指纹，明确标注它不足以证明内容相同。"""
    if size > MAX_HASH_BYTES:
        return f"meta:size={size};mtime_ns={mtime_ns}"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _native_id(st: os.stat_result) -> str | None:
    # Windows 上 st_ino 是文件索引，同盘改名后可保持不变；部分文件系统返回 0，必须放弃该线索。
    if not st.st_ino:
        return None
    return f"{st.st_dev}:{st.st_ino}"


def _walk(root: Path, excludes: set[str], errors: list[str]) -> Iterator[tuple[Path, os.stat_result]]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            errors.append(f"{current}: {exc}")
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    # 符号链接 / junction 一律跳过，防止越过授权范围。
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in excludes:
                        continue
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if entry.name.lower() in excludes:
                        continue
                    # Windows 上 DirEntry.stat() 不填充文件索引，必须用 os.stat 取 stable ID。
                    yield Path(entry.path), os.stat(entry.path, follow_symlinks=False)
            except OSError as exc:
                errors.append(f"{entry.path}: {exc}")


def _record(root: Path, path: Path, st: os.stat_result) -> dict:
    return {
        "path": str(path),
        "rel_path": path.relative_to(root).as_posix(),
        "native_id": _native_id(st),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ext": path.suffix.lower().lstrip("."),
    }


def _collect_records(root: Path, excludes: set[str], errors: list[str]) -> list[dict]:
    records = []
    for path, st in _walk(root, excludes, errors):
        try:
            records.append(_record(root, path, st))
        except ValueError:
            errors.append(f"{path}: 路径越界")
    return records


def _plan_ops(records: list[dict], existing: list[sqlite3.Row], counters: dict,
              errors: list[str]) -> tuple[list[tuple[str, sqlite3.Row | None, dict, str | None]], set[str]]:
    """把"当前磁盘记录"与"目录里已有的位置"对齐，产出待执行操作。

    kind 取值：insert / update / move / restore。restore 表示内容没变，
    只是之前被标记为 STALE（或离线后重新可见），恢复即可，不应产生新版本。
    """
    by_path = {row["path"].lower(): row for row in existing}
    by_native: dict[str, list[sqlite3.Row]] = {}
    for row in existing:
        if row["native_file_id"]:
            by_native.setdefault(row["native_file_id"], []).append(row)

    seen: set[str] = set()
    ops: list[tuple[str, sqlite3.Row | None, dict, str | None]] = []
    for record in records:
        location = by_path.get(record["path"].lower())
        if location is not None:
            seen.add(location["location_id"])
            unchanged = (
                record["size"] == location["size"]
                and record["mtime_ns"] == location["mtime_ns"]
                and record["native_id"] == location["native_file_id"]
            )
            if unchanged:
                if location["availability"] == "AVAILABLE":
                    continue
                ops.append(("restore", location, record, None))
                continue
            kind = "update"
            counters["updated"] += 1
        else:
            candidate = next(
                (row for row in by_native.get(record["native_id"] or "", [])
                 if row["location_id"] not in seen),
                None,
            )
            if candidate is not None:
                seen.add(candidate["location_id"])
                location = candidate
                kind = "move"
                counters["moved"] += 1
            else:
                kind = "insert"
                counters["new"] += 1
        try:
            digest = _hash_file(Path(record["path"]), record["size"], record["mtime_ns"])
        except OSError as exc:
            counters["errors"] += 1
            errors.append(f"{record['path']}: {exc}")
            counters[{"insert": "new", "update": "updated", "move": "moved"}[kind]] -= 1
            continue
        ops.append((kind, location, record, digest))
    return ops, seen


def _apply_ops(conn: sqlite3.Connection, source_id: str,
               ops: list[tuple[str, sqlite3.Row | None, dict, str | None]]) -> None:
    pending = 0
    for kind, location, record, digest in ops:
        if kind == "restore":
            store.restore_location(conn, location["location_id"])
        elif kind == "insert":
            store.insert_location(
                conn,
                source_id=source_id,
                path=record["path"],
                rel_path=record["rel_path"],
                native_file_id=record["native_id"],
                size=record["size"],
                mtime_ns=record["mtime_ns"],
                ext=record["ext"],
                content_hash=digest,
            )
        else:
            store.update_location(
                conn, location,
                path=record["path"],
                rel_path=record["rel_path"],
                native_file_id=record["native_id"],
                size=record["size"],
                mtime_ns=record["mtime_ns"],
                ext=record["ext"],
                content_hash=digest,
            )
        pending += 1
        if pending % 500 == 0:
            conn.commit()


def _start_run(conn: sqlite3.Connection, source_id: str) -> str:
    run_id = store.new_id("run")
    conn.execute(
        "INSERT INTO source_scan_runs (run_id, source_id, state, started_at) VALUES (?, ?, 'running', ?)",
        (run_id, source_id, store.now_iso()),
    )
    conn.commit()
    return run_id


def _finish_run(conn: sqlite3.Connection, run_id: str, state: str, counters: dict, message: str) -> None:
    conn.execute(
        "UPDATE source_scan_runs SET state = ?, files_seen = ?, files_new = ?, files_updated = ?,"
        " files_moved = ?, files_missing = ?, errors = ?, message = ?, finished_at = ?"
        " WHERE run_id = ?",
        (state, counters["seen"], counters["new"], counters["updated"], counters["moved"],
         counters["missing"], counters["errors"], message, store.now_iso(), run_id),
    )
    conn.commit()


def scan_source(conn: sqlite3.Connection, source_id: str) -> dict:
    source = store.get_source(conn, source_id)
    if source is None:
        raise ScanError("数据源不存在")
    counters = {"seen": 0, "new": 0, "updated": 0, "moved": 0, "missing": 0, "errors": 0}
    errors: list[str] = []
    root = Path(source["root_uri"])
    if not root.is_dir():
        run_id = _start_run(conn, source_id)
        offline = store.mark_source_offline(conn, source_id)
        conn.commit()
        _finish_run(conn, run_id, "failed", counters, f"数据源不可访问：{root}")
        store.log_event(conn, "scan_failed", source_id, detail=str(root), outcome=f"offline={offline}")
        conn.commit()
        return {"run_id": run_id, "state": "failed", "offline": offline, **counters}

    run_id = _start_run(conn, source_id)
    excludes = DEFAULT_EXCLUDES | {name.lower() for name in json.loads(source["excludes"] or "[]")}
    records = _collect_records(root, excludes, errors)
    counters["seen"] = len(records)

    if not records and errors:
        offline = store.mark_source_offline(conn, source_id)
        conn.commit()
        _finish_run(conn, run_id, "failed", counters, errors[0])
        store.log_event(conn, "scan_failed", source_id, detail=errors[0], outcome=f"offline={offline}")
        conn.commit()
        return {"run_id": run_id, "state": "failed", "offline": offline, **counters}

    existing = store.list_locations(conn, source_id)
    ops, seen = _plan_ops(records, existing, counters, errors)
    _apply_ops(conn, source_id, ops)
    counters["errors"] = len(errors)

    counters["missing"] = store.mark_missing(
        conn, [row["location_id"] for row in existing if row["location_id"] not in seen]
    )
    conn.execute(
        "UPDATE sources SET health = 'ok', last_scan_at = ? WHERE source_id = ?",
        (store.now_iso(), source_id),
    )
    store.log_event(
        conn, "scan_completed", source_id,
        detail=json.dumps(counters, ensure_ascii=False),
        outcome="ok",
    )
    conn.commit()
    _finish_run(conn, run_id, "done", counters,
                errors[0] if errors else "")
    return {"run_id": run_id, "state": "done", **counters, "errors_detail": errors[:20]}


def sync_paths(conn: sqlite3.Connection, source_id: str, paths: Iterable[str]) -> dict:
    """事件驱动的增量同步：只处理给定路径，不做全盘枚举。"""
    counters = {"seen": 0, "new": 0, "updated": 0, "moved": 0, "missing": 0, "errors": 0}
    source = store.get_source(conn, source_id)
    if source is None or source["state"] != "active":
        return {**counters, "skipped": True}
    root = Path(source["root_uri"])
    if not root.is_dir():
        # 数据源暂时不可达：事件不能当成删除证据。
        return {**counters, "skipped": True, "offline": True}

    excludes = DEFAULT_EXCLUDES | {name.lower() for name in json.loads(source["excludes"] or "[]")}
    errors: list[str] = []
    deleted: list[str] = []
    records: list[dict] = []
    for raw in paths:
        path = Path(raw)
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part.lower() in excludes for part in rel_parts) or path.is_symlink():
            continue
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            deleted.append(str(path))
            continue
        if path.is_dir():
            continue
        try:
            records.append(_record(root, path, st))
        except ValueError:
            continue

    if deleted:
        counters["missing"] = store.mark_paths_missing(conn, source_id, deleted)
    existing = store.list_locations(conn, source_id)
    ops, _ = _plan_ops(records, existing, counters, errors)
    _apply_ops(conn, source_id, ops)
    counters["seen"] = len(records)
    counters["errors"] = len(errors)
    conn.execute("UPDATE sources SET health = 'ok', last_scan_at = ? WHERE source_id = ?",
                 (store.now_iso(), source_id))
    store.log_event(
        conn, "sync_completed", source_id,
        detail=json.dumps({**counters, "paths": len(records) + len(deleted)}, ensure_ascii=False),
        outcome="ok",
    )
    conn.commit()
    return counters


def scan_source_in_new_connection(source_id: str, store_path: str | None = None) -> dict:
    conn = db.connect(store_path)
    try:
        return scan_source(conn, source_id)
    finally:
        conn.close()
