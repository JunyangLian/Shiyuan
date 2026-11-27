from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shiyuan import config, db, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "科研资料"
    (root / "2025" / "项目A").mkdir(parents=True)
    (root / "2025" / "BLAST结果表.xlsx").write_bytes(b"blast,result\n" * 10)
    (root / "2025" / "项目A" / "实验记录.md").write_text("# 实验记录\n样本量 42\n", encoding="utf-8")
    (root / "旧的草稿.docx").write_bytes(b"PK\x03\x04fake")
    return root


def _scan(conn, source_id):
    return scanner.scan_source(conn, source_id)


def test_l0_scan_and_search_by_name(conn, tree):
    source = store.add_source(conn, tree)
    result = _scan(conn, source["source_id"])
    assert result["seen"] == 3
    assert result["new"] == 3

    hits = search.search(conn, "blast")
    assert hits["total"] == 1
    assert hits["items"][0]["name"] == "BLAST结果表.xlsx"
    assert hits["items"][0]["index_level"] == "L0"


def test_scan_drops_legacy_office_lock_rows(conn, tmp_path):
    root = tmp_path / "大学"
    root.mkdir()
    (root / "笔记.md").write_text("正常内容，样本量 42\n", encoding="utf-8")
    (root / "~$论文.docx").write_bytes(b"owner-lock")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    assert conn.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE path LIKE '%~$%'").fetchone()[0] == 0

    # 模拟旧版本遗留的锁文件记录：它不应在下次扫描时变成 MISSING。
    note = conn.execute("SELECT * FROM asset_locations LIMIT 1").fetchone()
    conn.execute(
        "INSERT INTO asset_locations (location_id, asset_id, source_id, path, rel_path,"
        " native_file_id, availability, size, mtime_ns, ext, first_seen_at, last_seen_at)"
        " VALUES ('loc_legacy_lock', ?, ?, ?, '~$论文.docx', NULL, 'AVAILABLE', 10, 1,"
        " 'docx', ?, ?)",
        (note["asset_id"], source["source_id"],
         str(root / "~$论文.docx"), note["first_seen_at"], note["last_seen_at"]),
    )
    conn.commit()

    result = scanner.scan_source(conn, source["source_id"])
    assert result["missing"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE location_id = 'loc_legacy_lock'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE availability = 'MISSING'").fetchone()[0] == 0


def test_search_supports_cjk_tokens_and_path(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    hits = search.search(conn, "blast 结果")
    assert [item["name"] for item in hits["items"]] == ["BLAST结果表.xlsx"]

    by_dir = search.search(conn, "项目A")
    assert by_dir["total"] == 1
    assert by_dir["items"][0]["rel_path"] == "2025/项目A/实验记录.md"


def test_modified_file_creates_new_version(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    target = tree / "2025" / "项目A" / "实验记录.md"
    target.write_text("# 实验记录\n样本量 48\n", encoding="utf-8")

    result = _scan(conn, source["source_id"])
    assert result["updated"] == 1
    assert result["new"] == 0

    versions = conn.execute(
        "SELECT hash, is_current FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id WHERE l.path = ?"
        " ORDER BY observed_at",
        (str(target),),
    ).fetchall()
    assert len(versions) == 2
    assert sum(row["is_current"] for row in versions) == 1
    assert versions[0]["hash"] != versions[1]["hash"]


def test_rename_is_tracked_as_move_with_stable_asset(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    before = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE path = ?",
        (str(tree / "旧的草稿.docx"),),
    ).fetchone()["asset_id"]

    (tree / "旧的草稿.docx").rename(tree / "2025" / "最终草稿.docx")
    result = _scan(conn, source["source_id"])

    assert result["moved"] == 1
    assert result["new"] == 0
    assert result["missing"] == 0
    row = conn.execute(
        "SELECT asset_id, availability FROM asset_locations WHERE path = ?",
        (str(tree / "2025" / "最终草稿.docx"),),
    ).fetchone()
    assert row["asset_id"] == before
    assert row["availability"] == "AVAILABLE"
    assert search.search(conn, "最终草稿")["total"] == 1


def test_deleted_file_becomes_missing_and_leaves_results(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    (tree / "2025" / "BLAST结果表.xlsx").unlink()

    result = _scan(conn, source["source_id"])
    assert result["missing"] == 1
    assert search.search(conn, "blast")["total"] == 0

    status = conn.execute(
        "SELECT availability FROM asset_locations WHERE path LIKE '%BLAST%'"
    ).fetchone()
    assert status["availability"] == "MISSING"


def test_paused_source_is_not_searchable(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    assert search.search(conn, "blast")["total"] == 1

    store.set_source_state(conn, source["source_id"], "paused")
    assert search.search(conn, "blast")["total"] == 0


def test_remove_source_clears_derived_records(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    cleared = store.remove_source(conn, source["source_id"])
    assert cleared["assets_cleared"] == 3
    assert conn.execute("SELECT COUNT(*) FROM asset_locations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert search.search(conn, "blast")["total"] == 0


def test_offline_source_marks_locations_offline(conn, tmp_path):
    root = tmp_path / "移动硬盘"
    root.mkdir()
    (root / "论文.pdf").write_bytes(b"%PDF-1.4")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])

    root.rename(tmp_path / "未挂载")
    result = _scan(conn, source["source_id"])

    assert result["state"] == "failed"
    row = conn.execute("SELECT availability FROM asset_locations").fetchone()
    assert row["availability"] == "OFFLINE"
    assert search.search(conn, "论文")["total"] == 0


def test_rejects_nested_and_duplicate_sources(conn, tree):
    store.add_source(conn, tree)
    with pytest.raises(ValueError):
        store.add_source(conn, tree)
    with pytest.raises(ValueError):
        store.add_source(conn, tree / "2025")


def test_stats_reports_only_l0_coverage(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    data = search.stats(conn)
    assert data["files"]["available"] == 3
    assert data["coverage"]["directory_level"] == "L0"
    assert data["coverage"]["content_indexed"] == 0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    # 接口测试不启动文件监听，避免后台线程与临时目录清理互相干扰。
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_api_flow(client, tree):
    created = client.post("/api/sources", json={"root": str(tree)})
    assert created.status_code == 201
    source_id = created.json()["source_id"]

    assert client.post(f"/api/sources/{source_id}/scan").status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        items = client.get("/api/sources").json()["items"]
        if items and not items[0]["scanning"]:
            break
        time.sleep(0.05)

    hits = client.get("/api/search", params={"q": "blast"}).json()
    assert hits["total"] == 1

    progress = client.get("/api/sources").json()["items"][0]["parse"]
    assert progress["total"] == 3
    assert sum(progress[key] for key in (
        "parsed", "failed", "empty", "pending", "locked", "unsupported",
        "metadata_only")) == 3

    again = client.post("/api/sources", json={"root": str(tree)})
    assert again.status_code == 201
    assert again.json()["created"] is False
    assert again.json()["source_id"] == source_id
    assert client.delete(f"/api/sources/{source_id}").status_code == 200
    assert client.get("/api/search", params={"q": "blast"}).json()["total"] == 0


def test_api_pause_and_events(client, tree):
    source_id = client.post("/api/sources", json={"root": str(tree)}).json()["source_id"]
    assert client.post(f"/api/sources/{source_id}/state", params={"state": "paused"}).status_code == 200
    events = client.get("/api/events").json()["items"]
    assert events[0]["type"] == "source_state"
    assert client.post(f"/api/sources/{source_id}/state", params={"state": "bogus"}).status_code == 422


def test_app_home_prefers_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "home"))
    assert config.app_home() == tmp_path / "home"


def test_app_home_follows_install_dir_when_frozen(tmp_path, monkeypatch):
    """安装版把数据写在 <安装根>\\data，跟随安装位置而不是固定到 LOCALAPPDATA。"""
    monkeypatch.delenv("SHIYUAN_HOME", raising=False)
    install = tmp_path / "SourceLoom"
    exe = install / "binaries" / "shiyuan-core" / "shiyuan-core.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert config.app_home() == install / "data"
    assert (install / "data").is_dir()


def test_app_home_uses_localappdata_for_source_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIYUAN_HOME", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.app_home() == tmp_path / "local" / config.APP_DIR_NAME
