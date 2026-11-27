from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import answer, db, embeddings, families, indexer, query, retrieval, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "recall.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _reset_vector_cache():
    """向量缓存是模块级状态，测试之间必须清空，避免不同数据库之间串数据。"""
    with embeddings._VECTOR_CACHE_LOCK:
        embeddings._VECTOR_CACHE.clear()
        embeddings._VECTOR_CACHE.update(embeddings._new_cache())
    yield
    deadline = time.time() + 3
    while time.time() < deadline and embeddings._VECTOR_REBUILD["running"]:
        time.sleep(0.02)
    with embeddings._VECTOR_CACHE_LOCK:
        embeddings._VECTOR_CACHE.clear()
        embeddings._VECTOR_CACHE.update(embeddings._new_cache())


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


def test_two_letter_tokens_only_match_standalone_words(conn, tmp_path):
    _source(conn, tmp_path, {
        "different.md": "different coffee filter notes without any model\n",
        "回归.md": "POOL FE RE 三种面板回归模型，FE 与 RE 通过 Hausman 检验。",
    })

    hits = search.search(conn, "FE RE")

    names = [item["name"] for item in hits["items"]]
    assert "回归.md" in names
    assert "different.md" not in names


def test_short_gram_in_filename_does_not_hijack_topic_query(conn, tmp_path):
    _source(conn, tmp_path, {
        "染色体水平基因组组装.md": "褐蓝子鱼染色体水平基因组组装，统计 Chr01 至 Chr16。",
        "性染色体说明.md": "性染色体是控制性别分化的核心区域，包含 XY 与 ZW 两种系统。",
    })

    hits = search.search(conn, "性染色体")

    assert hits["items"][0]["name"] == "性染色体说明.md"


def test_path_followup_prefers_topic_result_file(conn, tmp_path):
    root = tmp_path / "黄姑鱼小课题"
    bsa = root / "bsa"
    bsa.mkdir(parents=True)
    (bsa / "BSA_Result_Strict.txt").write_text(
        "Chrom\tPos\tRef\tAlt\nSuperscaffold1\t73571\tC\tG\n", encoding="utf-8")
    (root / "draw_bsa.py").write_text("print('画图')\n", encoding="utf-8")
    other = tmp_path / "其他项目"
    other.mkdir()
    (other / "machine_results.csv").write_text("result,output\n1,2\n", encoding="utf-8")
    source = store.add_source(conn, root.parent)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    hits = answer.retrieve(conn, "黄姑鱼 结果文件放在哪个目录", limit=3, path_only=True)

    assert hits
    assert hits[0].name == "BSA_Result_Strict.txt"


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


def test_long_run_3gram_does_not_shadow_core_word():
    """长句切出的 3-gram 是跨词噪声，不能把真正的 2 字核心词挤出语义单元。"""
    analysis = query.analyze("引物设计产生了多少对引物")
    terms = [concept["term"] for concept in query.maximal_terms(analysis)]

    assert "引物" in terms
    assert "引物设" not in terms


def test_generic_design_is_stopped_but_report_stays_alive():
    stopped = query.analyze("设计")
    alive = query.analyze("报告有哪些版本")

    assert "设计" not in [c["term"] for c in stopped["concepts"]]
    # 报告/实验/论文/项目 是常见问题主体词，停用会让「报告有哪些版本」搜不到。
    assert "报告" in [c["term"] for c in alive["concepts"]]


def test_synonym_only_hit_passes_evidence_gate(conn, tmp_path):
    """答案块只命中英文同义词（primer）时也要能通过证据门槛被召回。"""
    _source(conn, tmp_path, {"结果.md": "Designed primer pairs: 115,407 and 19,867.\n"})

    hits = retrieval.find_chunks(conn, "引物设计产生了多少对引物")

    assert any("115,407" in hit.text for hit in hits)


def test_code_files_demoted_for_non_code_query(conn, tmp_path):
    """非代码查询时，正文撞上同义词的代码文件（package sample）要沉底。"""
    _source(conn, tmp_path, {
        "a.md": "样本 采集 分开出现，各一次。\n",
        "helper.java": "package sample; import sample.util; // 样本采集\n",
    })

    hits = retrieval.find_chunks(conn, "样本采集自哪里")

    assert hits, "应至少召回 md 文件"
    assert hits[0].path.endswith("a.md")


def test_code_file_named_after_topic_is_not_demoted(conn, tmp_path):
    """文件名强命中主题的代码文件不降权：答案就在这个文件里。"""
    _source(conn, tmp_path, {
        "receipt_sorter.py": "# receipt_sorter 入口\nSUPPORTED_EXTENSIONS = ['.jpg', '.png']\n",
        "手册.md": "receipt_sorter 支持的格式与安装说明。\n",
    })

    hits = retrieval.find_chunks(conn, "receipt_sorter 支持什么格式")

    assert hits[0].path.endswith("receipt_sorter.py")


def test_code_file_substring_name_match_still_demoted(conn, tmp_path):
    """primer 撞上 PrimeRing.java 属于子串巧合，不构成豁免。"""
    _source(conn, tmp_path, {
        "引物.md": "引物设计结果：共 300 对。\n",
        "PrimeRing.java": "// PrimeRing 数据结构课程作业\n",
    })

    hits = retrieval.find_chunks(conn, "引物设计产生了多少对引物")

    assert hits[0].path.endswith("引物.md")


def test_three_letter_acronym_matches_whole_word_only(conn, tmp_path):
    """三字母缩写（ONT）做子串匹配会撞上 Context/front，必须整词匹配。"""
    _source(conn, tmp_path, {
        "ui.md": "Context menu and front-end widgets.\n",
        "platform.md": "ONT 与 Illumina 双平台测序。\n",
    })

    hits = retrieval.find_chunks(conn, "ONT 测序平台")

    names = [hit.path for hit in hits]
    assert any(p.endswith("platform.md") for p in names)
    assert not any(p.endswith("ui.md") for p in names)


def test_count_query_bonus_skips_generic_report_names(conn, tmp_path):
    """「多少」问题给统计/结果命名的文件加分，但不给课程/实习报告加分。"""
    _source(conn, tmp_path, {
        "实习报告.md": "工作中产生了大量连接记录。\n",
        "统计结果.md": "满足阈值的连接共 93 个。\n",
    })

    hits = retrieval.find_chunks(conn, "满足阈值的连接有多少个")

    assert hits[0].path.endswith("统计结果.md")
    assert any("93" in hit.text for hit in hits[:3])


def test_phrase_hit_outranks_scattered_words(conn, tmp_path):
    """命中完整短语（样本采集自）的块应排在只散落单词的块之前。"""
    _source(conn, tmp_path, {
        "大纲.md": "样本采集自北部湾海域。\n",
        "清单.md": "样本清单与采集工具、采集点分布说明。\n",
    })

    hits = retrieval.find_chunks(conn, "样本采集自哪里")

    assert hits[0].path.endswith("大纲.md")


def test_same_name_tsv_copies_form_one_family(conn, tmp_path):
    """版本目录里同名 tsv 的副本要聚成一个家族，避免重复吃掉证据位。"""
    root = tmp_path / "修订"
    (root / "v1").mkdir(parents=True)
    (root / "v2").mkdir(parents=True)
    table = "join_id\tchr\tposition\nJ001\tChr01\t12345\n"
    (root / "v1" / "Supplementary_Table_S6.tsv").write_text(table, encoding="utf-8")
    (root / "v2" / "Supplementary_Table_S6.tsv").write_text(table, encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    families.rebuild(conn)
    mapping = families.family_map(conn)

    ids = [info["family_id"] for info in mapping.values() if info["family_id"]]
    assert len(ids) == 2
    assert len(set(ids)) == 1


def test_identifier_token_outranks_topic_words(conn, tmp_path):
    """查询带编号（JL1）时，含该编号的数据文件要压过只命中中文主题词的旧文档。"""
    _source(conn, tmp_path, {
        "褐篮子鱼基因组组装统计.md":
            "染色体水平基因组组装统计，scaffold N50 与总长度见附表。" * 3,
        "Genome_Assembly_Stats.csv":
            "Sample,Type,N50,Total_length\nJL1,Scaffold,30500180,3103712985\n",
    })
    question = "JL1 染色体级组装的 scaffold N50 是多少"

    chunks = retrieval.find_chunks(conn, question)
    assert chunks[0].path.endswith("Genome_Assembly_Stats.csv")

    results = search.search(conn, question)
    assert results["items"][0]["name"] == "Genome_Assembly_Stats.csv"


def test_generic_metric_names_are_not_identifiers():
    """N50 这类通用指标名不算编号，否则所有组装统计都会被同等加权。"""
    assert query.query_identifiers("scaffold N50 是多少") == []
    assert query.query_identifiers("JL1 hap1 的 N50") == ["jl1", "hap1"]


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


def test_embeddable_heuristic_skips_numeric_tables():
    assert embeddings._embeddable("样本量 42 例，测序深度 30x")[0]
    numeric, reason = embeddings._embeddable("1\t2\t3\n" * 60)
    assert numeric is False and reason
    assert embeddings._embeddable("ACGTACGT" * 60)[0] is False


def test_index_pending_records_skipped_chunks(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {"数值.txt": "1\t2\t3\n" * 60, "说明.md": "样本量 42\n"})

    result = embeddings.index_pending(conn)

    assert result["indexed"] >= 1
    assert result["skipped"] >= 1
    assert embeddings.skipped_count(conn) >= 1
    pending = embeddings._pending_chunks(conn, "fake-embed", None, 50)
    assert all("数值" not in row["text"] for row in pending)


def test_tsv_is_not_scheduled_for_vectors(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {"数据.tsv": "a\tb\n1\t2\n" * 60})

    result = embeddings.index_pending(conn)

    assert result["indexed"] == 0
    assert result["skipped"] == 0
    assert embeddings.pending_count(conn, "fake-embed") == 0


def test_vector_cache_appends_new_rows_without_rebuild(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    root, source_id = _source(conn, tmp_path, {"a.md": "样本量 42\n"})
    embeddings.index_pending(conn)

    builds = []
    original = embeddings._build_cache
    monkeypatch.setattr(
        embeddings, "_build_cache",
        lambda c, m: builds.append(m) or original(c, m))

    embeddings.semantic_chunks(conn, "样本量", top_k=5)
    assert builds == ["fake-embed"]
    before = embeddings._vector_cache_key(conn, "fake-embed")

    (root / "b.md").write_text("深海鱼调查记录\n", encoding="utf-8")
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)
    embeddings.index_pending(conn)
    hits = embeddings.semantic_chunks(conn, "海洋生物", top_k=5)

    # 新向量只追加，没有再次整库重建。
    assert builds == ["fake-embed"]
    assert embeddings._VECTOR_CACHE["rows"] == 2
    assert any("深海鱼" in hit["text"] for hit in hits)
    after = embeddings._vector_cache_key(conn, "fake-embed")
    assert after[1] == before[1] + 1
    assert after[2] > before[2]


def test_vector_cache_resets_when_all_vectors_removed(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {"a.md": "样本量 42\n"})
    embeddings.index_pending(conn)
    assert embeddings.semantic_chunks(conn, "样本量", top_k=5)

    embeddings.clear(conn)

    assert embeddings.semantic_chunks(conn, "样本量", top_k=5) == []
    assert embeddings._VECTOR_CACHE["rows"] == 0


def test_vector_cache_compacts_after_mass_delete(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts", lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {
        "a.md": "样本量 42\n",
        "b.md": "深海鱼调查\n",
        "c.md": "样本量 48\n",
    })
    embeddings.index_pending(conn)
    assert embeddings._VECTOR_CACHE["rows"] == 0

    embeddings.semantic_chunks(conn, "样本量", top_k=5)
    assert embeddings._VECTOR_CACHE["rows"] == 3

    monkeypatch.setattr(embeddings, "_STALE_MIN", 0)
    monkeypatch.setattr(embeddings, "_STALE_RATIO", 0.0)
    conn.execute(
        "DELETE FROM chunk_embeddings WHERE rowid IN"
        " (SELECT rowid FROM chunk_embeddings ORDER BY rowid LIMIT 2)")
    conn.commit()

    assert embeddings.semantic_chunks(conn, "样本量", top_k=5)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not embeddings._VECTOR_REBUILD["running"] and embeddings._VECTOR_CACHE["rows"] == 1:
            break
        time.sleep(0.02)

    assert embeddings._VECTOR_CACHE["rows"] == 1
    assert embeddings._VECTOR_CACHE["count"] == 1


def test_result_query_boosts_report_name():
    analysis = query.analyze("测试客户A的处理结果", synonyms={})

    assert retrieval.name_prior("D:\\资料\\processing_report.md", analysis) >= 200


def test_cross_language_synonym_matches_english_filename(conn, tmp_path):
    _source(conn, tmp_path, {
        "All_Perfect_Markers.txt": "marker,chr,pos\n",
        "实验说明.md": "样本量 42\n",
    })

    hits = search.search(conn, "完美标记")

    assert hits["items"][0]["name"] == "All_Perfect_Markers.txt"


def test_lookup_query_skips_semantic(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    called = []
    monkeypatch.setattr(
        embeddings, "semantic_chunks",
        lambda *args, **kwargs: called.append(1) or [])
    _source(conn, tmp_path, {"TRX50记录.md": "TRX50 样本量 42\n"})

    hits = search.search(conn, "TRX50")

    assert hits["vector_used"] is False
    assert called == []
    assert hits["items"][0]["name"] == "TRX50记录.md"


def test_search_marks_weak_results(conn, tmp_path):
    files = {f"报告{i}.md": "样本量 42\n" for i in range(3)}
    files["总览.md"] = "样本量 42\n" * 3
    _source(conn, tmp_path, files)

    result = search.search(conn, "样本量")

    assert 1 <= result["strong"] <= result["total"]
    flags = [item["weak"] for item in result["items"]]
    assert flags == sorted(flags)


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


def test_english_stopwords_are_not_search_terms():
    """英文虚词不做检索词，避免 to 这类子串命中 photo/photoshop。"""
    analysis = query.analyze("how to evaluate high quality economic development")

    assert "to" not in analysis["variants"]
    assert "how" not in analysis["variants"]
    assert "quality" in analysis["variants"]
    assert "development" in analysis["variants"]


def test_generic_english_filename_term_is_not_specific(conn, tmp_path):
    """通用英文词命中文件名不算主题命中，避免工具/教程文件压过语义结果。"""
    _source(conn, tmp_path, {
        "LifecycleMappingPage.txt": "mapping page tutorial\n",
        "黄姑鱼性别决定区.txt": "黄姑鱼性别决定区定位分析\n",
    })
    analysis = query.analyze("mapping")

    matches = retrieval.path_matches(conn, analysis)
    target = next(match for match in matches.values()
                  if "LifecycleMappingPage" in match.path)
    assert target.name_matched is True
    assert target.specific is False
    assert retrieval.is_generic_term("mapping") is True


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


def test_api_ask_stream_emits_status_and_done(client, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42 例\n", encoding="utf-8")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    client.post(f"/api/sources/{source_id}/scan")
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    with client.stream("POST", "/api/ask/stream", json={"question": "样本量是多少"}) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = [
        json.loads(block[5:].strip())
        for block in body.split("\n\n")
        if block.startswith("data: ")
    ]
    types = [event["type"] for event in events]
    assert types[0] == "status"
    assert "done" in types
    done = next(event for event in events if event["type"] == "done")["result"]
    assert done["answer"]


def test_index_all_pending_rebuilds_all_chunk_vectors(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {f"doc{i}.md": f"样本量 {i} 记录\n" for i in range(3)})

    result = embeddings.index_all_pending(conn)

    assert result["available"] is True
    assert result["indexed"] == embeddings.count(conn)
    assert result["remaining"] == 0


def test_embed_failure_is_recorded_and_visible(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    _source(conn, tmp_path, {"记录.md": "样本量 42\n"})

    def broken(settings, texts):
        raise embeddings.EmbeddingError("向量接口返回 400：code 1210")

    monkeypatch.setattr(embeddings, "embed_texts", broken)

    with pytest.raises(embeddings.EmbeddingError):
        embeddings.index_pending(conn)

    public = embeddings.public_settings(conn)
    assert public["last_error"] and "400" in public["last_error"]
    assert "code 1210" in public["last_error"]

    # 修改设置后旧错误清除（未做同步探测）。
    embeddings.update_settings(conn, {"model": "another-model"})
    assert embeddings.public_settings(conn)["last_error"] is None


def test_successful_vector_query_clears_stale_error(conn, tmp_path, monkeypatch):
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda settings, texts: _fake_vectors(texts))
    _source(conn, tmp_path, {"记录.md": "样本量 42 例\n重复测量记录\n"})
    embeddings.index_pending(conn)
    embeddings._set_last_error(conn, "无法连接向量接口")
    assert embeddings.public_settings(conn)["last_error"]

    hits = embeddings.semantic_chunks(conn, "样本量", top_k=5)

    assert hits
    assert embeddings.public_settings(conn)["last_error"] is None


def test_query_vector_cache_reuses_embedding(monkeypatch):
    calls = []

    def fake_embed(settings, texts):
        calls.append(list(texts))
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    with embeddings._QUERY_CACHE_LOCK:
        embeddings._QUERY_CACHE.clear()
    settings = {"model": "cache-test-model"}
    text = "查询缓存唯一语句 42"
    first = embeddings._query_vector(settings, text)
    second = embeddings._query_vector(settings, text)
    assert first == second == [0.1, 0.2, 0.3]
    assert len(calls) == 1
