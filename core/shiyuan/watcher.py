"""实时监听：watchdog 事件 → 去抖合并 → 增量同步；周期对账兜底。

设计取舍：
- 事件只触发"涉及路径"的同步，不重扫整个数据源；
- 目录级事件（新建/删除/改名目录）无法枚举其内部文件，直接回退为一次完整对账；
- 周期对账兜住监听丢失、系统休眠、网络盘事件缺失；
- 同步任务与手动扫描共用 `scanner.request_scan`，同一数据源不会并发写库。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import config, db, indexer, scanner, store

DEBOUNCE_SECONDS = 1.5
MAX_DELAY_SECONDS = 10.0
TICK_SECONDS = 0.4


class PendingBatch:
    """单个数据源的待处理路径：安静一段时间后再统一处理，避免频繁小事务。"""

    def __init__(self) -> None:
        self.paths: set[str] = set()
        self.full = False
        self.first_at = 0.0
        self.last_at = 0.0

    def add(self, path: str | None, now: float, *, full: bool = False) -> None:
        if not self.paths and not self.full:
            self.first_at = now
        self.last_at = now
        if full:
            self.full = True
        if path:
            self.paths.add(path)

    def ready(self, now: float, debounce: float = DEBOUNCE_SECONDS,
              max_delay: float = MAX_DELAY_SECONDS) -> bool:
        if not self.paths and not self.full:
            return False
        return now - self.last_at >= debounce or now - self.first_at >= max_delay

    def take(self) -> tuple[list[str], bool]:
        paths = sorted(self.paths)
        full = self.full
        self.paths = set()
        self.full = False
        self.first_at = self.last_at = 0.0
        return paths, full


class _Handler(FileSystemEventHandler):
    def __init__(self, manager: "WatcherManager", source_id: str) -> None:
        self.manager = manager
        self.source_id = source_id

    def on_created(self, event) -> None:
        self._handle(event)

    def on_modified(self, event) -> None:
        self._handle(event)

    def on_deleted(self, event) -> None:
        self._handle(event)

    def on_moved(self, event) -> None:
        self._handle(event)
        self.manager.enqueue(self.source_id, None if event.is_directory else event.dest_path,
                             full=event.is_directory)

    def _handle(self, event) -> None:
        # 目录事件看不到其内部文件，交给完整对账处理。
        self.manager.enqueue(self.source_id, None if event.is_directory else event.src_path,
                             full=event.is_directory)


class WatcherManager:
    def __init__(self, store_path: Path | str | None = None, *,
                 debounce: float = DEBOUNCE_SECONDS,
                 max_delay: float = MAX_DELAY_SECONDS) -> None:
        self.store_path = store_path
        self.debounce = debounce
        self.max_delay = max_delay
        self._lock = threading.Lock()
        self._pending: dict[str, PendingBatch] = {}
        self._stop = threading.Event()
        self._observer = Observer()
        self._watches: dict[str, object] = {}
        self._threads: list[threading.Thread] = []
        self._started = False

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._observer.start()
        for target, name in ((self._dispatch_loop, "watch-dispatch"),
                             (self._reconcile_loop, "watch-reconcile")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self._log("watch_started", None, "实时监听已启动", "ok")
        self.sync_all()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._stop.set()
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:  # 观察器已在异常状态下退出时不再阻塞关闭流程
            pass
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
        self._watches.clear()
        self._log("watch_stopped", None, "实时监听已停止", "ok")

    # ---------------------------------------------------------------- 订阅管理

    def schedule(self, source_id: str, root_uri: str) -> None:
        if not self._started or source_id in self._watches:
            return
        root = Path(root_uri)
        if not root.is_dir():
            return
        try:
            watch = self._observer.schedule(_Handler(self, source_id), str(root), recursive=True)
        except OSError as exc:
            self._log("watch_failed", source_id, f"{root}: {exc}", "error")
            return
        self._watches[source_id] = watch

    def unschedule(self, source_id: str) -> None:
        watch = self._watches.pop(source_id, None)
        with self._lock:
            self._pending.pop(source_id, None)
        if watch is None:
            return
        try:
            self._observer.unschedule(watch)
        except Exception:
            pass

    def sync_all(self) -> None:
        """按数据库里 active 且可访问的数据源重建订阅。"""
        conn = db.connect(self.store_path)
        try:
            rows = store.list_sources(conn)
        finally:
            conn.close()
        active = {row["source_id"] for row in rows if row["state"] == "active"}
        for source_id in list(self._watches):
            if source_id not in active:
                self.unschedule(source_id)
        for row in rows:
            if row["state"] == "active":
                self.schedule(row["source_id"], row["root_uri"])

    # ---------------------------------------------------------------- 事件入口

    def enqueue(self, source_id: str, path: str | None, *, full: bool = False) -> None:
        if not self._started or source_id not in self._watches:
            return
        now = time.monotonic()
        with self._lock:
            batch = self._pending.get(source_id)
            if batch is None:
                batch = PendingBatch()
                self._pending[source_id] = batch
            batch.add(path, now, full=full)

    # ---------------------------------------------------------------- 后台循环

    def _dispatch_loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            due: list[tuple[str, list[str], bool]] = []
            now = time.monotonic()
            with self._lock:
                for source_id, batch in list(self._pending.items()):
                    if batch.ready(now, self.debounce, self.max_delay):
                        paths, full = batch.take()
                        due.append((source_id, paths, full))
            for source_id, paths, full in due:
                self._run_sync(source_id, paths, full)

    def _reconcile_loop(self) -> None:
        interval = config.reconcile_seconds()
        while not self._stop.wait(interval):
            conn = db.connect(self.store_path)
            try:
                rows = store.list_sources(conn)
            finally:
                conn.close()
            for row in rows:
                if row["state"] == "active":
                    scanner.scan_in_background(row["source_id"])

    def _run_sync(self, source_id: str, paths: list[str], full: bool) -> None:
        if not scanner.request_scan(source_id):
            # 已有一轮在跑：request_scan 已记录 follow-up，结束后会自动补扫。
            return
        try:
            conn = db.connect(self.store_path)
            try:
                if full:
                    scanner.scan_source(conn, source_id)
                else:
                    # 先标记 STALE：同步完成前，检索不再把旧内容当作当前事实。
                    store.mark_stale(conn, source_id, paths)
                    conn.commit()
                    scanner.sync_paths(conn, source_id, paths)
            finally:
                conn.close()
            indexer.index_incremental_in_new_connection(source_id, self.store_path)
        except Exception as exc:
            self._log("sync_failed", source_id, str(exc)[:300], "error")
        finally:
            scanner.release_scan(source_id)

    def _log(self, kind: str, source_id: str | None, detail: str, outcome: str) -> None:
        try:
            conn = db.connect(self.store_path)
            try:
                store.log_event(conn, kind, source_id, detail=detail, outcome=outcome)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            # 维护日志本身失败不应影响监听主流程。
            pass
