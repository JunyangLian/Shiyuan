from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import answer, db, embeddings, indexer, query, retrieval, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "recall.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _source(conn, tmp_path, files: dict[str, str]):
    root = tmp_path / "资料"
    root.mkdir(exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return root, source["source_id"]


def _fake_vectors(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "海洋" in lowered or "深海" in lowered:
            vectors.append([0.0, 1.0])
        elif "样本" in lowered:
            vectors.append([1.0, 0.0])
        else:
            vectors.append([0.3, 0.3])
    return vectors


def test_query_rewrite_strips_question_and_expands_synonyms():
    analysis = query.analyze("请问项目A 的最终样本数是多少？")

    assert analysis["core"] == "项目a 的最终样本数"
    assert "样本数" in analysis["variants"]
    assert "样本量" in analysis["variants"]


def test_synonym_recall_for_content(conn, tmp_path):
    _source(conn, tmp_path, {"记录.md": "本次样本量 42 例\n"})

    hits = search.search(conn, "样本数")

    assert hits["total"] == 1
    assert hits["items"][0]["name"] == "记录.md"


def test_filename_subject_outranks_dense_content(conn, tmp_path):
    dense = "本文讨论专利 创新点、创新点分析、专利技术与创新点归纳。" * 20
    _source(conn, tmp_path, {
        "专利说明.md": "本发明公开了一种性别决定区定位方法，创新点在于混池测序与全局比对。",
        "论文笔记.md": dense,
    })

    hits = search.search(conn, "专利的创新点")

    assert hits["items"][0]["name"] == "专利说明.md"


def test_question_tail_does_not_block_content_search(conn, tmp_path):
    _source(conn, tmp_path, {"记录.md": "本次样本量 42 例\n"})

    hits = search.search(conn, "样本量是多少？")

    assert hits["total"] == 1
    assert hits["rewritten"] == "样本量"


def test_search_matches_folder_and_file_names(conn, tmp_path):
    root = tmp_path / "资料"
    batch = root / "pilot_batches" / "2026-07"
    batch.mkdir(parents=True)
    (batch / "处理结果.md").write_text("本月共有 12 张单据。", encoding="utf-8")
    (root / "无关.md").write_text("大学语文复习资料。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    hits = search.search(conn, "pilot_batches 里有什么")
    assert hits["items"]
    assert hits["items"][0]["name"] == "处理结果.md"
    assert hits["items"][0]["matched_folder"] == "pilot_batches"

    by_name = search.search(conn, "处理结果")
    assert by_name["items"][0]["name"] == "处理结果.md"

    # 正文不含“pilot_batches”，但文件夹名命中时仍应进入问答召回。
    chunks = retrieval.find_chunks(conn, "pilot_batches 里有什么")
    assert any(hit.path.endswith("处理结果.md") for hit in chunks)


def test_custom_synonyms_are_used(conn, tmp_path):
    _source(conn, tmp_path, {"记录.md": "招募人数 42 人\n"})

    assert search.search(conn, "入组")["total"] == 0

    saved = query.update_settings(conn, {"custom": [["入组", "招募"], ["单独"]]})
    assert saved["custom"] == [["入组", "招募"]]

    hits = search.search(conn, "入组")
    assert hits["total"] == 1
    assert hits["items"][0]["name"] == "记录.md"


def test_answer_citation_includes_version_and_mtime(conn, tmp_path):
    _source(conn, tmp_path, {"记录.md": "本次样本量 42 例\n"})

    evidence = answer.retrieve(conn, "样本量")
    assert "更新于" in evidence[0].label()

    result = answer.ask(conn, "样本量")
    citation = result["citations"][0]
    assert citation["version_id"].startswith("ver_")
    assert citation["mtime"]


def test_answer_uses_filename_hint_when_content_lacks_term(conn, tmp_path):
    _source(conn, tmp_path, {"毒鳍记录.md": "物种：Acanthurus nigricans\n登录号：GCA_988281415.1\n"})

    asked = answer.ask(conn, "毒鳍表里有哪些物种？")
    assert asked["refused"] is False
    assert any(item["name"] == "毒鳍记录.md" for item in asked["citations"])


def test_embedding_settings_require_consent_and_hide_key(conn):
    assert embeddings.enabled(embeddings.load_settings(conn)) is False

    saved = embeddings.update_settings(conn, {
        "provider": "openai", "model": "text-embedding-3-small", "api_key": "sk-test",
    })
    assert saved["has_key"] is True
    assert "api_key" not in saved
    assert embeddings.enabled(embeddings.load_settings(conn)) is False

    embeddings.update_settings(conn, {"allow_cloud": True})
    assert embeddings.enabled(embeddings.load_settings(conn)) is True


def test_vector_index_builds_and_counts(conn, tmp_path, monkeypatch):
    _source(conn, tmp_path, {"a.md": "样本量 42\n", "b.md": "深海鱼调查\n"})
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))

    result = embeddings.index_pending(conn)

    assert result["indexed"] == 2
    assert result["remaining"] == 0
    assert embeddings.count(conn) == 2


def test_semantic_only_hit_is_merged_into_search(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {"a.md": "样本量 42\n", "b.md": "深海鱼调查\n"})
    embeddings.index_pending(conn)

    hits = search.search(conn, "海洋生物")

    assert hits["vector_used"] is True
    assert hits["vector_model"] == "fake-embed"
    assert hits["total"] == 1
    item = hits["items"][0]
    assert item["name"] == "b.md"
    assert item["semantic"] is True
    assert item["match"] == "semantic"


def test_answer_uses_semantic_evidence(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {"b.md": "深海鱼调查记录\n"})
    embeddings.index_pending(conn)

    result = answer.ask(conn, "海洋生物")

    assert result["refused"] is False
    assert result["citations"][0]["name"] == "b.md"
    assert result["citations"][0]["semantic"] is True


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_api_embedding_settings_and_build(client, tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    root = tmp_path / "资料"
    root.mkdir()
    (root / "a.md").write_text("样本量 42\n", encoding="utf-8")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    client.post(f"/api/sources/{source_id}/scan")
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    confirmed = client.put("/api/inventory/policies", json={
        "source_id": source_id, "policies": {"md": "standard_parse"},
    })
    assert confirmed.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    settings = client.get("/api/settings/embedding").json()
    assert settings["provider"] == "none"
    assert settings["has_key"] is False

    saved = client.put("/api/settings/embedding", json={
        "provider": "ollama", "model": "fake-embed", "top_k": 6,
    })
    assert saved.status_code == 200
    assert saved.json()["provider"] == "ollama"

    assert client.post("/api/maintenance/embed").status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/stats").json()["maintenance"]["running"]["running"]:
            break
        time.sleep(0.05)

    info = client.get("/api/stats").json()["maintenance"]
    assert info["vectors"] == 1
    assert info["embedding"]["model"] == "fake-embed"


def test_api_embed_rejects_when_disabled(client):
    assert client.post("/api/maintenance/embed").status_code == 400


def test_api_query_settings_validation(client):
    initial = client.get("/api/settings/query").json()
    assert initial["custom"] == []
    assert initial["builtin_groups"] > 0

    saved = client.put("/api/settings/query", json={"custom": [["入组", "招募"], ["单独"]]})
    assert saved.status_code == 200
    assert saved.json()["custom"] == [["入组", "招募"]]

    assert client.put("/api/settings/query", json={"custom": "bad"}).status_code == 422
    too_long = [["长" * 40, "短"]]
    assert client.put("/api/settings/query", json={"custom": too_long}).status_code == 400
