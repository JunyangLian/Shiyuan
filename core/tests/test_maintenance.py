from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import db, indexer, maintenance, ocr, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "maintenance.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _source(conn, tmp_path, files: dict[str, bytes | str]):
    root = tmp_path / "资料"
    root.mkdir(exist_ok=True)
    for name, content in files.items():
        target = root / name
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    source = store.add_source(conn, root)
    return root, source["source_id"]


def _job(conn, stage: str, version_id: str | None = None):
    if version_id:
        return conn.execute(
            "SELECT * FROM index_jobs WHERE stage = ? AND version_id = ?",
            (stage, version_id)).fetchone()
    return conn.execute("SELECT * FROM index_jobs WHERE stage = ?", (stage,)).fetchone()


def test_successful_index_records_job(conn, tmp_path):
    _, source_id = _source(conn, tmp_path, {"记录.md": "样本量 42\n"})
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)

    job = _job(conn, "l1")
    assert job["status"] == "done"
    assert job["attempts"] == 1
    assert job["source_id"] == source_id
    assert search.stats(conn)["maintenance"]["jobs"]["l1:done"] == 1


def test_failed_job_stops_after_three_attempts(conn, tmp_path):
    _, source_id = _source(conn, tmp_path, {"坏文档.docx": b"PK\x03\x04fake"})
    scanner.scan_source(conn, source_id)

    for _ in range(4):
        indexer.index_pending(conn, source_id)

    job = _job(conn, "l1")
    assert job["status"] == "failed"
    assert job["attempts"] == indexer.MAX_ATTEMPTS
    assert job["error"]

    again = indexer.index_pending(conn, source_id)
    assert again["failed"] == 0


def test_retry_resets_attempts_and_reruns(conn, tmp_path):
    _, source_id = _source(conn, tmp_path, {"坏文档.docx": b"PK\x03\x04fake"})
    scanner.scan_source(conn, source_id)
    for _ in range(3):
        indexer.index_pending(conn, source_id)
    assert _job(conn, "l1")["attempts"] == indexer.MAX_ATTEMPTS

    result = maintenance._apply(conn, "retry", source_id)
    assert result["requeued"] == 1
    assert _job(conn, "l1")["attempts"] == 1
    assert _job(conn, "l1")["status"] == "failed"


def test_reindex_rebuilds_content_cache(conn, tmp_path):
    _, source_id = _source(conn, tmp_path, {"记录.md": "样本量 42\n"})
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)
    before = conn.execute("SELECT artifact_id FROM parse_artifacts").fetchone()["artifact_id"]

    result = maintenance._apply(conn, "reindex", source_id)

    after = conn.execute("SELECT artifact_id FROM parse_artifacts").fetchone()["artifact_id"]
    assert result["artifacts"] == 1
    assert result["indexed"] == 1
    assert after != before
    assert search.search(conn, "样本量")["total"] == 1


def test_cleanup_removes_noncurrent_versions(conn, tmp_path):
    root, source_id = _source(conn, tmp_path, {"记录.md": "样本量 42\n"})
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)
    target = root / "记录.md"
    time.sleep(0.01)
    target.write_text("样本量 48\n", encoding="utf-8")
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)

    old_version = conn.execute(
        "SELECT version_id, asset_id FROM content_versions WHERE is_current = 0").fetchone()
    conn.execute(
        "INSERT INTO parse_artifacts (artifact_id, version_id, parser_signature, state,"
        " quality, char_count, message, created_at) VALUES ('art_old', ?, 'old', 'ok',"
        " 'good', 10, NULL, '2026-01-01')", (old_version["version_id"],))
    conn.execute(
        "INSERT INTO chunks (chunk_id, artifact_id, version_id, asset_id, ordinal, text,"
        " page, sheet, char_start, char_end) VALUES ('chk_old', 'art_old', ?, ?, 0,"
        " '旧版本内容', NULL, NULL, 0, 6)", (old_version["version_id"], old_version["asset_id"]))
    conn.commit()

    result = maintenance._apply(conn, "cleanup", source_id)

    assert result["artifacts"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE chunk_id = 'chk_old'").fetchone()[0] == 0


def test_ocr_stage_job_is_recorded(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "engine_info", lambda: {
        "available": True, "engine": "windows-ocr", "languages": ["zh-Hans-CN"], "message": None})
    monkeypatch.setattr(ocr, "extract_image",
                        lambda path: [ocr.Section(text="图片里的文字", page=None)])
    _, source_id = _source(conn, tmp_path, {"图.png": b"\x89PNG\r\n\x1a\n" + b"0" * 16})
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)
    indexer.index_ocr_pending(conn, source_id)

    job = _job(conn, "l2")
    assert job["status"] == "done"
    assert job["attempts"] == 1


def test_stats_reports_cache_and_db_size(conn, tmp_path):
    _, source_id = _source(conn, tmp_path, {"记录.md": "样本量 42\n"})
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)

    info = search.stats(conn)["maintenance"]
    assert info["chunks"] >= 1
    assert info["artifacts"] == 1
    assert info["db_bytes"] > 0
    assert info["running"]["running"] is False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def _wait_maintenance(client, seconds: float = 10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        info = client.get("/api/stats").json()["maintenance"]["running"]
        if not info["running"]:
            return info
        time.sleep(0.05)
    raise AssertionError("维护任务没有在预期时间内结束")


def test_api_maintenance_actions(client, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42\n", encoding="utf-8")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    client.post(f"/api/sources/{source_id}/scan")
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    assert client.post("/api/maintenance/reindex").status_code == 202
    _wait_maintenance(client)
    assert client.get("/api/search", params={"q": "样本量"}).json()["total"] == 1

    assert client.post("/api/maintenance/retry", params={"source_id": source_id}).status_code == 202
    _wait_maintenance(client)

    assert client.post("/api/maintenance/cleanup").status_code == 202
    _wait_maintenance(client)

    events = client.get("/api/events", params={"limit": 20}).json()["items"]
    assert any(event["type"].startswith("maintenance_") for event in events)

    assert client.post("/api/maintenance/reindex", params={"source_id": "missing"}).status_code == 404
