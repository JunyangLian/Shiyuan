from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import (answer, db, embeddings, external, indexer, projects, scanner,
                     search, store)
from shiyuan.mcp_server import MCPServer


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "projects.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _index(conn, root, files: dict[str, str]) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return source


def _call(server: MCPServer, name: str, arguments: dict):
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    result = response["result"]
    payload = None if result["isError"] else json.loads(result["content"][0]["text"])
    return result, payload


def test_create_project_links_existing_assets_without_copying(conn, tmp_path):
    source = _index(conn, tmp_path / "资料", {
        "甲.md": "甲项目的样本量是 42。",
        "乙.md": "乙项目的内容。",
    })
    before = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    project = projects.create_project(conn, "黄斑蓝子鱼项目", description="实验与论文")
    linked = projects.add_source_path(conn, project["project_id"], source["root_uri"])

    assert linked["created"] is False
    assert linked["assets_linked"] == 2
    data = projects.get_project(conn, project["project_id"])
    assert data["stats"]["primary"] == 2
    assert data["stats"]["sources"] == 1
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == before


def test_project_subfolder_only_links_that_folder(conn, tmp_path):
    root = tmp_path / "大目录"
    (root / "甲组").mkdir(parents=True)
    (root / "乙组").mkdir(parents=True)
    (root / "甲组" / "记录.md").write_text("甲组记录 样本量 42", encoding="utf-8")
    (root / "乙组" / "记录.md").write_text("乙组记录 样本量 99", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    project = projects.create_project(conn, "甲组项目")
    projects.add_source_path(conn, project["project_id"], root / "甲组")

    listing = projects.list_assets(conn, project["project_id"])
    assert listing["total"] == 1
    assert "甲组" in listing["items"][0]["path"]


def test_one_asset_in_ten_projects_keeps_single_copy(conn, tmp_path):
    source = _index(conn, tmp_path / "资料", {"论文.md": "同一篇论文 样本量 42"})
    chunks_before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    vectors_before = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    asset_id = conn.execute("SELECT asset_id FROM asset_locations").fetchone()["asset_id"]

    for index in range(10):
        project = projects.create_project(conn, f"项目 {index}")
        projects.add_source_path(conn, project["project_id"], source["root_uri"])

    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == chunks_before
    assert conn.execute(
        "SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == vectors_before
    assert conn.execute(
        "SELECT COUNT(*) FROM project_assets WHERE asset_id = ?",
        (asset_id,)).fetchone()[0] == 10


def test_scopes_and_exclusion_do_not_leak_across_projects(conn, tmp_path):
    source_a = _index(conn, tmp_path / "甲库", {"甲.md": "甲库专用 样本量 42"})
    source_b = _index(conn, tmp_path / "乙库", {"乙.md": "乙库专用 样本量 99"})
    asset_a = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE source_id = ?",
        (source_a["source_id"],)).fetchone()["asset_id"]
    asset_b = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE source_id = ?",
        (source_b["source_id"],)).fetchone()["asset_id"]

    project = projects.create_project(conn, "甲项目")
    pid = project["project_id"]
    projects.add_source_path(conn, pid, source_a["root_uri"])
    projects.set_membership(conn, pid, [asset_b], "linked")

    allowed, excluded = projects.asset_filter(conn, pid, "project")
    hits = search.search(conn, "专用", allowed_assets=allowed, excluded_assets=excluded)
    assert hits["items"] and all("甲库" in item["path"] for item in hits["items"])

    allowed, excluded = projects.asset_filter(conn, pid, "expanded")
    hits = search.search(conn, "专用", allowed_assets=allowed, excluded_assets=excluded)
    paths = {item["path"] for item in hits["items"]}
    assert any("甲库" in path for path in paths)
    assert any("乙库" in path for path in paths)

    projects.set_membership(conn, pid, [asset_a], "excluded")
    allowed, excluded = projects.asset_filter(conn, pid, "project")
    assert allowed == set()
    hits = search.search(conn, "专用", allowed_assets=allowed, excluded_assets=excluded)
    assert hits["items"] == []

    allowed, excluded = projects.asset_filter(conn, pid, "global")
    hits = search.search(conn, "专用", allowed_assets=allowed, excluded_assets=excluded)
    assert hits["items"] and all("乙库" in item["path"] for item in hits["items"])
    # 其他项目/全局资产不受本项目排除影响
    assert projects.asset_filter(conn, None, "global") == (None, set())


def test_ask_respects_project_scope_and_expands_when_empty(conn, tmp_path):
    _index(conn, tmp_path / "甲库", {"甲.md": "样本量是 42 例。"})
    _index(conn, tmp_path / "乙库", {"乙.md": "样本量是 99 例。"})

    project = projects.create_project(conn, "甲项目")
    pid = project["project_id"]
    source = conn.execute(
        "SELECT source_id, root_uri FROM sources WHERE root_uri LIKE '%甲库%'"
    ).fetchone()
    projects.add_source_path(conn, pid, source["root_uri"])

    result = answer.ask(conn, "样本量是多少", project_id=pid, scope="project")
    assert result["evidence_count"] >= 1
    assert all("甲库" in item["path"] for item in result["citations"])
    assert result["scope"]["used"] == "project"
    assert result["scope"]["expanded"] is False

    empty = projects.create_project(conn, "空项目")
    result = answer.ask(conn, "样本量是多少", project_id=empty["project_id"])
    assert result["evidence_count"] >= 1
    assert result["scope"]["requested"] == "project"
    assert result["scope"]["used"] == "global"
    assert result["scope"]["expanded"] is True


def test_mcp_exposes_projects_and_enforces_privacy(conn, tmp_path):
    _index(conn, tmp_path / "甲库", {"甲.md": "甲库专用 样本量 42"})
    _index(conn, tmp_path / "乙库", {"乙.md": "乙库专用 样本量 99"})
    source = conn.execute(
        "SELECT source_id, root_uri FROM sources WHERE root_uri LIKE '%甲库%'"
    ).fetchone()
    project = projects.create_project(conn, "甲项目")
    pid = project["project_id"]
    projects.add_source_path(conn, pid, source["root_uri"])

    server = MCPServer(conn)
    result, payload = _call(server, "list_projects", {})
    assert result["isError"] is False
    assert payload["total"] == 2
    names = {item["name"] for item in payload["items"]}
    assert {"全局项目", "甲项目"} <= names

    result, payload = _call(server, "search_knowledge",
                            {"query": "专用", "project_id": pid})
    assert result["isError"] is False
    assert payload["scope"]["used"] == "project"
    assert payload["items"] and all("甲库" in item["path"] for item in payload["items"])

    projects.update_project(conn, pid, privacy_policy="local_only")
    result, _ = _call(server, "get_project", {"project_id": pid})
    assert result["isError"] is True
    assert "仅允许本地" in result["content"][0]["text"]
    result, payload = _call(server, "list_projects", {})
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "全局项目"
    result, _ = _call(server, "search_knowledge",
                      {"query": "专用", "project_id": pid})
    assert result["isError"] is True


def test_path_only_recall_stays_inside_project(conn, tmp_path):
    """文件名只命中时，也不能把项目外同名的文档带进结果。"""
    source_a = _index(conn, tmp_path / "甲库", {"预算表.md": "甲库数据 42"})
    _index(conn, tmp_path / "乙库", {"预算表.md": "乙库数据 99"})
    project = projects.create_project(conn, "甲项目")
    pid = project["project_id"]
    projects.add_source_path(conn, pid, source_a["root_uri"])

    allowed, excluded = projects.asset_filter(conn, pid, "project")
    hits = search.search(conn, "预算表", allowed_assets=allowed,
                         excluded_assets=excluded)

    assert hits["items"]
    assert all("甲库" in item["path"] for item in hits["items"])


def test_semantic_recall_filters_scope_before_top_k(conn, tmp_path, monkeypatch):
    """项目范围必须先于 Top-K 生效，否则小项目会被全局高相似片段挤空。"""
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-scope"})

    def fake_vectors(settings, texts):
        return [[0.0, 1.0] if "深海" in text else [0.6, 0.8] for text in texts]

    monkeypatch.setattr(embeddings, "embed_texts", fake_vectors)
    source_a = _index(conn, tmp_path / "甲库", {"甲.md": "样本量 42"})
    _index(conn, tmp_path / "乙库", {"乙.md": "深海鱼调查"})
    embeddings.index_pending(conn)
    asset_a = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE source_id = ?",
        (source_a["source_id"],)).fetchone()["asset_id"]

    hits = embeddings.semantic_chunks(
        conn, "深海", top_k=1, allowed_assets={asset_a})

    assert hits and all(hit["asset_id"] == asset_a for hit in hits)
    assert embeddings.semantic_chunks(
        conn, "深海", top_k=1, allowed_assets=set()) == []


def test_mcp_related_documents_respects_project_scope(conn, tmp_path):
    source = _index(conn, tmp_path / "甲库", {
        "甲.md": "甲项目样本量 42",
        "乙.md": "甲项目补充材料",
    })
    _index(conn, tmp_path / "乙库", {"丙.md": "乙库内容"})
    project = projects.create_project(conn, "甲项目")
    pid = project["project_id"]
    projects.add_source_path(conn, pid, source["root_uri"])
    asset_id = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE path LIKE '%甲.md'").fetchone()["asset_id"]

    server = MCPServer(conn)
    result, payload = _call(server, "get_related_documents",
                            {"asset_id": asset_id, "project_id": pid})
    assert result["isError"] is False
    related = payload["related"]
    assert related and all(item["reason"] == "same_project" for item in related)
    assert all("乙库" not in (item["path"] or "") for item in related)

    result, _ = _call(server, "get_related_documents",
                      {"asset_id": asset_id, "project_id": "prj_missing"})
    assert result["isError"] is True

    projects.update_project(conn, pid, privacy_policy="local_only")
    result, _ = _call(server, "get_related_documents",
                      {"asset_id": asset_id, "project_id": pid})
    assert result["isError"] is True


def test_global_project_includes_all_sources_and_cannot_be_deleted(conn, tmp_path):
    _index(conn, tmp_path / "甲库", {"甲.md": "甲库专用 样本量 42"})
    _index(conn, tmp_path / "乙库", {"乙.md": "乙库专用 样本量 99"})

    global_project = projects.get_project(conn, projects.GLOBAL_PROJECT_ID)
    assert global_project["kind"] == "global"
    assert global_project["stats"]["assets"] == 2
    assert {source["label"] for source in global_project["sources"]} == {"甲库", "乙库"}

    listing = projects.list_assets(conn, projects.GLOBAL_PROJECT_ID)
    assert listing["total"] == 2
    assert all(item["membership_type"] == "primary" for item in listing["items"])

    allowed, excluded = projects.asset_filter(
        conn, projects.GLOBAL_PROJECT_ID, "project")
    assert allowed is None and excluded == set()

    with pytest.raises(ValueError):
        projects.delete_project(conn, projects.GLOBAL_PROJECT_ID)
    with pytest.raises(ValueError):
        projects.set_membership(
            conn, projects.GLOBAL_PROJECT_ID, [listing["items"][0]["asset_id"]],
            "excluded")


def test_external_token_is_bound_to_one_project(conn, tmp_path):
    source_a = _index(conn, tmp_path / "甲库", {"甲.md": "甲库专用 样本量 42"})
    source_b = _index(conn, tmp_path / "乙库", {"乙.md": "乙库专用 样本量 99"})
    project = projects.create_project(conn, "甲项目")
    pid = project["project_id"]
    projects.add_source_path(conn, pid, source_a["root_uri"])

    client = external.create_client(conn, preset="codex", project_id=pid)
    assert client["project_id"] == pid
    assert client["project_name"] == "甲项目"
    assert len(external.list_clients(conn, project_id=pid)) == 1
    assert external.list_clients(conn, project_id=projects.GLOBAL_PROJECT_ID) == []

    bound = MCPServer(conn, pid)
    result, payload = _call(bound, "list_projects", {})
    assert result["isError"] is False
    assert payload["total"] == 1
    assert payload["items"][0]["project_id"] == pid

    result, payload = _call(bound, "search_knowledge", {"query": "专用"})
    assert result["isError"] is False
    assert payload["scope"]["used"] == "expanded"
    assert payload["items"] and all("甲库" in item["path"] for item in payload["items"])

    result, _ = _call(bound, "search_knowledge",
                      {"query": "专用", "project_id": "prj_other"})
    assert result["isError"] is True

    result, payload = _call(bound, "search_knowledge",
                            {"query": "专用", "scope": "global"})
    assert payload["scope"]["used"] == "expanded"
    assert all("甲库" in item["path"] for item in payload["items"])

    location_b = conn.execute(
        "SELECT location_id FROM asset_locations WHERE source_id = ?",
        (source_b["source_id"],)).fetchone()["location_id"]
    result, _ = _call(bound, "get_document_text", {"location_id": location_b})
    assert result["isError"] is True
    assert "分享范围" in result["content"][0]["text"]

    global_client = external.create_client(
        conn, preset="codex", project_id=projects.GLOBAL_PROJECT_ID)
    assert global_client["project_id"] == projects.GLOBAL_PROJECT_ID
    global_server = MCPServer(conn, projects.GLOBAL_PROJECT_ID)
    result, payload = _call(global_server, "search_knowledge", {"query": "专用"})
    assert result["isError"] is False
    assert payload["scope"]["used"] == "global"
    assert any("乙库" in item["path"] for item in payload["items"])

    # 删除项目时绑定它的外部令牌一并失效，避免留下无主接口。
    assert projects.delete_project(conn, pid)["clients_revoked"] == 1
    assert external.get_client(conn, client["client_id"]) is None


def test_migration_is_idempotent_and_backs_up(tmp_path):
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    db.init_db(conn)
    root = tmp_path / "资料"
    root.mkdir()
    store.add_source(conn, root)

    conn.execute("UPDATE app_settings SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    db.init_db(conn)

    assert conn.execute(
        "SELECT value FROM app_settings WHERE key = 'schema_version'").fetchone()[0] == "3"
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'projects'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM projects WHERE kind = 'global'").fetchone()[0] >= 1

    backups = list(tmp_path.glob("legacy.bak-*.db"))
    assert len(backups) == 1
    db.init_db(conn)
    assert len(list(tmp_path.glob("legacy.bak-*.db"))) == 1
    conn.close()


def test_removing_source_cleans_project_links(conn, tmp_path):
    source = _index(conn, tmp_path / "资料", {"记录.md": "样本量 42"})
    project = projects.create_project(conn, "临时项目")
    projects.add_source_path(conn, project["project_id"], source["root_uri"])
    assert conn.execute("SELECT COUNT(*) FROM project_assets").fetchone()[0] == 1

    store.remove_source(conn, source["source_id"])

    assert conn.execute("SELECT COUNT(*) FROM project_sources").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM project_assets").fetchone()[0] == 0
    assert projects.get_project(conn, project["project_id"]) is not None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def _wait_scans(client) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        items = client.get("/api/sources").json()["items"]
        if not any(item.get("scanning") for item in items):
            return
        time.sleep(0.05)


def test_project_api_create_search_ask_and_delete(client, tmp_path):
    for name, text in (("甲库", "样本量是 42 例，甲库专用。"), ("乙库", "样本量是 99 例，乙库专用。")):
        root = tmp_path / name
        root.mkdir()
        (root / "记录.md").write_text(text, encoding="utf-8")
        source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
        client.post(f"/api/sources/{source_id}/scan")
        client.put("/api/inventory/policies", json={
            "source_id": source_id, "policies": {"md": "standard_parse"},
        })
        _wait_scans(client)

    created = client.post("/api/projects", json={
        "name": "甲项目", "folders": [str(tmp_path / "甲库")],
    }).json()
    project_id = created["project"]["project_id"]
    assert created["project"]["stats"]["primary"] == 1

    listing = client.get(f"/api/projects/{project_id}/assets").json()
    assert listing["total"] == 1

    hits = client.get("/api/search", params={
        "q": "专用", "project_id": project_id, "scope": "project",
    }).json()
    assert hits["items"] and all("甲库" in item["path"] for item in hits["items"])

    result = client.post("/api/ask", json={
        "question": "样本量是多少", "project_id": project_id,
    }).json()
    assert result["evidence_count"] >= 1
    assert all("甲库" in item["path"] for item in result["citations"])
    assert result["scope"]["used"] in {"project", "expanded"}

    assert client.delete(f"/api/projects/{project_id}").status_code == 200
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    # 删除项目不影响全局文件库
    assert client.get("/api/search", params={"q": "专用"}).json()["total"] >= 2
