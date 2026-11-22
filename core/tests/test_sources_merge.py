from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shiyuan import db, indexer, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "merge.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _index_child(conn, child):
    source = store.add_source(conn, child)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return source


def test_adding_parent_merges_existing_child(conn, tmp_path):
    root = tmp_path / "项目"
    child = root / "实验记录"
    child.mkdir(parents=True)
    (child / "记录.md").write_text("本次样本量 42 例\n", encoding="utf-8")
    child_source = _index_child(conn, child)

    result = store.add_source_merged(conn, root)

    assert result["created"] is True
    assert [item["source_id"] for item in result["merged"]] == [child_source["source_id"]]
    sources = store.list_sources(conn)
    assert len(sources) == 1
    assert sources[0]["root_uri"] == str(root.resolve())

    locations = conn.execute(
        "SELECT source_id, rel_path FROM asset_locations"
    ).fetchall()
    assert [row["source_id"] for row in locations] == [sources[0]["source_id"]]
    assert locations[0]["rel_path"] == "实验记录/记录.md"
    assert search.search(conn, "样本量")["total"] == 1


def test_adding_child_under_existing_parent_reuses_parent(conn, tmp_path):
    root = tmp_path / "项目"
    child = root / "实验记录"
    child.mkdir(parents=True)
    parent = store.add_source_merged(conn, root)

    result = store.add_source_merged(conn, child)

    assert result["created"] is False
    assert result["covered_by"] == parent["source"]["source_id"]
    assert result["source"]["source_id"] == parent["source"]["source_id"]
    assert len(store.list_sources(conn)) == 1


def test_siblings_are_not_merged(conn, tmp_path):
    tree = tmp_path / "项目"
    first = tree / "第一批"
    second = tree / "第二批"
    first.mkdir(parents=True)
    second.mkdir()

    store.add_source_merged(conn, first)
    second_result = store.add_source_merged(conn, second)

    assert second_result["created"] is True
    assert len(store.list_sources(conn)) == 2


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_api_reports_merge(client, tmp_path):
    root = tmp_path / "项目"
    child = root / "实验记录"
    child.mkdir(parents=True)
    created = client.post("/api/sources", json={"root": str(child)})
    assert created.status_code == 201

    merged = client.post("/api/sources", json={"root": str(root)})
    assert merged.status_code == 201
    body = merged.json()
    assert body["created"] is True
    assert [item["source_id"] for item in body["merged"]] == [created.json()["source_id"]]

    sources = client.get("/api/sources").json()["items"]
    assert len(sources) == 1
    assert sources[0]["root_uri"] == str(root.resolve())

    covered = client.post("/api/sources", json={"root": str(child)})
    assert covered.status_code == 201
    assert covered.json()["created"] is False
    assert covered.json()["covered_by"] == sources[0]["source_id"]
