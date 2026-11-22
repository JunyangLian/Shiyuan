from __future__ import annotations

import time
from pathlib import Path

import pytest

from shiyuan import db, indexer, scanner, search, store, watcher


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "watch.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _prepare(conn, tmp_path: Path):
    root = tmp_path / "资料"
    root.mkdir()
    target = root / "记录.md"
    target.write_text("旧内容 alpha\n", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return root, target, source["source_id"]


def test_scan_followup_is_triggered_after_busy_release(monkeypatch):
    source_id = "src_followup_test"
    started: list[str] = []
    monkeypatch.setattr(scanner, "scan_in_background", lambda sid: started.append(sid))

    assert scanner.request_scan(source_id) is True
    assert scanner.request_scan(source_id) is False
    assert scanner.is_running(source_id) is True

    scanner.release_scan(source_id)
    assert started == [source_id]
    assert scanner.is_running(source_id) is False

    scanner.release_scan(source_id)
    assert started == [source_id]


def test_release_without_pending_does_not_rescan(monkeypatch):
    source_id = "src_no_followup_test"
    started: list[str] = []
    monkeypatch.setattr(scanner, "scan_in_background", lambda sid: started.append(sid))

    assert scanner.request_scan(source_id) is True
    scanner.release_scan(source_id)
    assert started == []
    assert scanner.is_running(source_id) is False


def test_sync_paths_updates_content_and_reindexes(conn, tmp_path):
    root, target, source_id = _prepare(conn, tmp_path)
    time.sleep(0.01)
    target.write_text("新内容 beta\n", encoding="utf-8")

    result = scanner.sync_paths(conn, source_id, [str(target)])
    indexer.index_pending(conn, source_id)

    assert result["updated"] == 1
    assert search.search(conn, "beta")["total"] == 1
    assert search.search(conn, "alpha")["total"] == 0


def test_sync_paths_marks_deleted_missing(conn, tmp_path):
    root, target, source_id = _prepare(conn, tmp_path)
    target.unlink()

    result = scanner.sync_paths(conn, source_id, [str(target)])

    assert result["missing"] == 1
    assert search.search(conn, "alpha")["total"] == 0
    status = conn.execute("SELECT availability FROM asset_locations WHERE path = ?",
                          (str(target),)).fetchone()
    assert status["availability"] == "MISSING"


def test_stale_hides_content_until_sync_restores_it(conn, tmp_path):
    root, target, source_id = _prepare(conn, tmp_path)
    conn.execute("UPDATE asset_locations SET availability = 'STALE' WHERE path = ?", (str(target),))
    conn.commit()
    assert search.search(conn, "alpha")["total"] == 0

    versions_before = conn.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0]
    scanner.sync_paths(conn, source_id, [str(target)])

    assert conn.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0] == versions_before
    assert search.search(conn, "alpha")["total"] == 1


def test_sync_paths_detects_same_disk_move(conn, tmp_path):
    root, target, source_id = _prepare(conn, tmp_path)
    asset_id = conn.execute("SELECT asset_id FROM asset_locations WHERE path = ?",
                            (str(target),)).fetchone()["asset_id"]
    moved = root / "改名后的记录.md"
    target.rename(moved)

    result = scanner.sync_paths(conn, source_id, [str(target), str(moved)])

    assert result["moved"] == 1
    row = conn.execute("SELECT asset_id, availability FROM asset_locations WHERE path = ?",
                       (str(moved),)).fetchone()
    assert row["asset_id"] == asset_id
    assert row["availability"] == "AVAILABLE"


def test_pending_batch_coalesces_paths():
    batch = watcher.PendingBatch()
    batch.add("a", 100.0)
    batch.add("b", 101.0)

    assert not batch.ready(102.2, debounce=1.5, max_delay=10)
    assert batch.ready(102.6, debounce=1.5, max_delay=10)
    paths, full = batch.take()
    assert paths == ["a", "b"]
    assert not full
    assert not batch.ready(200.0)


def test_pending_batch_max_delay_and_directory_fallback():
    batch = watcher.PendingBatch()
    batch.add("a", 0.0)
    for tick in range(1, 11):
        batch.add(None, float(tick))
    assert batch.ready(10.0, debounce=100, max_delay=10)
    paths, full = batch.take()
    assert paths == ["a"]
    assert not full

    batch.add(None, 20.0, full=True)
    assert batch.ready(21.6, debounce=1.5, max_delay=10)
    _, full = batch.take()
    assert full


def test_watcher_detects_new_file_end_to_end(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SHIYUAN_HOME", str(home))
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("初始内容\n", encoding="utf-8")

    connection = db.connect(home / "shiyuan.db")
    db.init_db(connection)
    source = store.add_source(connection, root)
    scanner.scan_source(connection, source["source_id"])
    connection.close()

    manager = watcher.WatcherManager(home / "shiyuan.db", debounce=0.2, max_delay=1.0)
    manager.start()
    try:
        (root / "新文件.md").write_text("关键词 独角兽\n", encoding="utf-8")
        deadline = time.time() + 15
        found = False
        while time.time() < deadline:
            check = db.connect(home / "shiyuan.db")
            try:
                found = search.search(check, "独角兽")["total"] == 1
            finally:
                check.close()
            if found:
                break
            time.sleep(0.2)
        assert found, "watchdog 事件未在 15 秒内完成增量同步"
    finally:
        manager.stop()
