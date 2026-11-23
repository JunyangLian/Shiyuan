from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import answer, db, indexer, planner, query, rerank, scanner, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "answer.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def indexed(conn, tmp_path):
    root = tmp_path / "科研资料"
    root.mkdir()
    (root / "实验记录.md").write_text("2025-03 本次样本量 42 例，测序平台 NovaSeq 6000\n", encoding="utf-8")
    (root / "项目报告.md").write_text("项目A 的最终样本量为 48 例\n", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return root, source["source_id"]


def test_library_overview_uses_corpus_stats(conn, indexed):
    assert answer._is_library_overview("你现在有什么") is True
    assert answer._is_library_overview("知识库里有多少文件") is True
    assert answer._is_library_overview("项目A 的最终样本量是多少") is False

    result = answer.ask(conn, "你现在有什么")

    assert result["provider"] == "library"
    assert result["citations"] == []
    assert "2 个可检索文件" in result["answer"]
    assert "文件类型：md 2" in result["answer"]


def test_extractive_answer_cites_evidence(conn, indexed):
    result = answer.ask(conn, "样本量是多少")

    assert result["refused"] is False
    assert result["provider"] == "extractive"
    assert result["evidence_count"] >= 1
    assert "[1]" in result["answer"]
    citation = result["citations"][0]
    assert citation["name"].endswith(".md")
    assert citation["snippet"]
    assert result["warning"] is None


def test_multi_file_evidence_keeps_both_sources(conn, indexed):
    result = answer.ask(conn, "样本量")
    names = {citation["name"] for citation in result["citations"]}
    assert {"实验记录.md", "项目报告.md"} <= names


def test_no_evidence_refuses(conn, indexed):
    result = answer.ask(conn, "量子纠缠实验结论")

    assert result["refused"] is True
    assert result["reason"] == "no_evidence"
    assert result["citations"] == []
    assert "没有找到足够证据" in result["answer"]
    assert result["coverage"]["content_indexed"] >= 2


def test_source_recall_answers_authorized_source_question(conn, tmp_path):
    root = tmp_path / "小微商户月度单据整理助手"
    root.mkdir()
    (root / "单据处理报告.md").write_text("本月处理单据 42 张。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "你还记得那个单据助手吗")

    assert result["provider"] == "library"
    assert result["reason"] == "source_recall"
    assert "小微商户月度单据整理助手" in result["answer"]
    assert "1 个可检索文件" in result["answer"]
    assert result["citations"] == []


def test_conversational_fillers_are_not_search_terms():
    analysis = query.analyze("你还记得那个单据助手吗")

    assert "单据" in analysis["variants"]
    assert "助手" in analysis["variants"]
    assert "记得" not in analysis["variants"]
    assert "那个" not in analysis["variants"]


def test_question_words_in_the_middle_are_removed():
    analysis = query.analyze("MISA 跑了什么参数")

    assert analysis["core"] == "misa 参数"
    assert "跑" not in analysis["variants"]
    assert "跑了" not in analysis["variants"]
    assert "什么" not in analysis["variants"]

    assert query.analyze("专利里写了什么")["core"] == "专利"
    assert query.analyze("我毕业论文写了什么")["core"] == "我毕业论文"


def test_planner_classifies_four_modes():
    assert planner.make("论文中 BUSCO 的完整度是多少").mode.key == "precision"
    assert planner.make("介绍一下群体基因组分析").mode.key == "explain"
    assert planner.make("实验室之前做过哪些群体基因组研究").mode.key == "comprehensive"
    assert planner.make("论文修改了哪些内容").mode.key == "history"


def test_comprehensive_plan_covers_multiple_families(conn, tmp_path):
    root = tmp_path / "研究"
    root.mkdir()
    for index in range(6):
        (root / f"研究主题{index}.md").write_text(
            f"实验室研究主题 {index}：鱼类基因组分析，样本与测序方案各不相同。",
            encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "实验室做过哪些鱼类研究")

    plan = result["plan"]
    assert plan["intent"] == "comprehensive"
    assert plan["used"] >= 5
    assert plan["families"] >= 5
    assert plan["candidates"] >= plan["used"]


def test_precision_plan_keeps_evidence_small(conn, tmp_path):
    root = tmp_path / "参数"
    root.mkdir()
    for index in range(6):
        (root / f"参数记录{index}.md").write_text(
            f"第 {index} 次运行：测序参数为 -t 8，接头阈值 0.2。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "测序参数是多少")

    assert result["plan"]["intent"] == "precision"
    assert 0 < result["plan"]["used"] <= 4


def test_plan_reports_missing_coverage(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "蓝子鱼.md").write_text(
        "蓝子鱼样本采集于 2024 年，共 30 尾，用于群体基因组分析。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "蓝子鱼的毒素基因有哪些")

    plan = result["plan"]
    assert plan["facets"]
    assert "毒素基因" in plan["missing"]


def test_rerank_orders_candidates_by_model(monkeypatch):
    from types import SimpleNamespace

    hits = [SimpleNamespace(path=f"D:\\候选{index}.md", text=f"内容{index}")
            for index in range(3)]
    settings = {"provider": "openai", "model": "gpt-test", "api_key": "sk-test",
                "allow_cloud": True}
    monkeypatch.setattr(rerank, "_post_json", lambda *args, **kwargs: {
        "choices": [{"message": {"content": '{"relevant": [3, 1]}'}}]})

    ranked, stats = rerank.rerank(settings, "问题", hits)

    assert [hit.text for hit in ranked] == ["内容2", "内容0"]
    assert stats["reranked"] is True
    assert stats["kept"] == 2
    assert stats["error"] is None


def test_rerank_falls_back_to_local_order(monkeypatch):
    from types import SimpleNamespace

    hits = [SimpleNamespace(path=f"D:\\候选{index}.md", text=f"内容{index}")
            for index in range(3)]
    settings = {"provider": "openai", "model": "gpt-test", "api_key": "sk-test",
                "allow_cloud": True}

    def boom(*args, **kwargs):
        raise rerank.RerankError("上游超时")

    monkeypatch.setattr(rerank, "_post_json", boom)

    ranked, stats = rerank.rerank(settings, "问题", hits)

    assert ranked == hits
    assert stats["reranked"] is False
    assert "上游超时" in stats["error"]


def test_rerank_empty_keeps_local_order(monkeypatch):
    from types import SimpleNamespace

    hits = [SimpleNamespace(path=f"D:\\候选{index}.md", text=f"内容{index}")
            for index in range(3)]
    settings = {"provider": "openai", "model": "gpt-test", "api_key": "sk-test",
                "allow_cloud": True}
    monkeypatch.setattr(rerank, "_post_json", lambda *args, **kwargs: {
        "choices": [{"message": {"content": '{"relevant": []}'}}]})

    ranked, stats = rerank.rerank(settings, "问题", hits)

    assert ranked == hits
    assert stats["reranked"] is False
    assert stats["empty"] is True
    assert stats["error"] is None


def test_retrieve_reranks_before_selecting_evidence(conn, tmp_path, monkeypatch):
    root = tmp_path / "重排"
    root.mkdir()
    topics = ["斑马鱼", "蓝子鱼", "草鱼", "鲫鱼", "鲤鱼"]
    for index, topic in enumerate(topics):
        (root / f"记录{index}.md").write_text(
            f"{topic}的样本量是 {index + 1} 尾，采样地点编号 {index * 7}，"
            f"测序批次 {topic} 批次，分析流程与其它批次不同。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    answer.update_settings(conn, {"provider": "openai", "model": "gpt-test",
                                  "api_key": "sk-test", "allow_cloud": True})

    def fake_rerank(settings, question, hits, **kwargs):
        kept = list(reversed(hits))[:2]
        return kept, {"reranked": True, "model": settings["model"],
                      "considered": len(hits), "kept": len(kept), "error": None}

    monkeypatch.setattr(answer.rerank, "rerank", fake_rerank)
    stats: dict = {}
    plan = planner.make("介绍一下样本量")
    evidence = answer.retrieve(conn, "样本量是多少", limit=2, plan=plan, stats=stats)

    assert stats["candidates"] > 2
    assert stats["reranked"] is True
    assert stats["rerank_kept"] == 2
    assert stats["rerank_error"] is None
    assert 0 < stats["used"] <= 2
    assert evidence
    assert evidence[0].name == "记录4.md"


def test_duplicate_check_report_is_demoted(conn, tmp_path):
    root = tmp_path / "论文"
    root.mkdir()
    (root / "专利说明.md").write_text(
        "本发明公开了一种基于混池测序的性别决定区定位方法，创新点在于全局比对策略。",
        encoding="utf-8")
    (root / "专利查重报告.md").write_text(
        "专利 创新点 混池测序 全局比对策略 重复率 12% 比对报告 片段对照 原文对照",
        encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "专利的创新点是什么")

    assert result["citations"][0]["name"] == "专利说明.md"

    explicit = answer.ask(conn, "专利的查重报告重复率多少")
    assert explicit["citations"]
    assert explicit["citations"][0]["name"] == "专利查重报告.md"


def test_stat_question_lists_tables_instead_of_summing(conn, tmp_path):
    root = tmp_path / "单据"
    root.mkdir()
    rows = ["单据编号,日期,金额"] + [f"D{i},2026-10-0{i % 9 + 1},100" for i in range(1, 20)]
    (root / "单据明细.csv").write_text("\n".join(rows), encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "上个月单据金额合计多少")

    assert result["reason"] == "table_stats_hint"
    assert result["citations"][0]["name"] == "单据明细.csv"
    assert "不会替你求和" in result["answer"]
    assert "1900" not in result["answer"]

    chapters = answer.ask(conn, "论文一共几章")
    assert chapters["reason"] != "table_stats_hint"

    spoken = answer.ask(conn, "这个月单据花了多少")
    assert spoken["reason"] == "table_stats_hint"


def test_folder_overview_expands_second_level(conn, tmp_path):
    root = tmp_path / "大学"
    (root / "大三" / "系统工程").mkdir(parents=True)
    (root / "大三" / "云计算").mkdir(parents=True)
    (root / "大三" / "系统工程" / "课件.md").write_text("系统工程课件", encoding="utf-8")
    (root / "大三" / "云计算" / "笔记.md").write_text("云计算笔记", encoding="utf-8")
    (root / "大三" / "课程说明.md").write_text("大三课程说明", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "大三有哪些课程")

    assert result["reason"] == "folder_overview"
    assert "系统工程" in result["answer"]
    assert "文件：课件.md" in result["answer"]
    assert "课程说明.md" in result["answer"]


def test_folder_question_answers_with_subfolder_list(conn, tmp_path):
    root = tmp_path / "大学"
    (root / "大三" / "系统工程").mkdir(parents=True)
    (root / "大三" / "云计算").mkdir(parents=True)
    (root / "大三" / "系统工程" / "课件.md").write_text("系统工程课件", encoding="utf-8")
    (root / "大三" / "云计算" / "笔记.md").write_text("云计算笔记", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "大三学了哪些课程")

    assert result["reason"] == "folder_overview"
    assert result["citations"] == []
    assert "系统工程" in result["answer"]
    assert "云计算" in result["answer"]
    assert "2 个可检索文件" in result["answer"]


def test_folder_question_prefers_folder_named_in_query(conn, tmp_path):
    root = tmp_path / "大学"
    (root / "大二" / "课程A").mkdir(parents=True)
    (root / "大一" / "1大二课程设计").mkdir(parents=True)
    (root / "大二" / "课程A" / "笔记.md").write_text("课程A笔记", encoding="utf-8")
    (root / "大一" / "1大二课程设计" / "代码.java").write_text("class A {}", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "大二有哪些课程")

    assert result["reason"] == "folder_overview"
    assert "「大二」" in result["answer"]
    assert "1大二课程设计" not in result["answer"]


def test_where_is_file_question_returns_path_hit(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "课程表.md").write_text("周一 高等数学；周二 大学物理。", encoding="utf-8")
    (root / "笔记.md").write_text("随手记：今天下雨，课程表还没改。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "我的课程表在哪")

    assert result["citations"][0]["name"] == "课程表.md"


def test_howto_question_prefers_readme(conn, tmp_path):
    root = tmp_path / "工具"
    root.mkdir()
    (root / "README.md").write_text(
        "# 单据助手\n\n中文名：单据助手。\n\n## 安装\npip install -r requirements.txt\n",
        encoding="utf-8")
    rows = ["单据编号,金额"] + [f"单据{i},100" for i in range(200)]
    (root / "data.csv").write_text("\n".join(rows), encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "单据助手怎么用")

    assert result["citations"][0]["name"] == "README.md"


def test_intro_question_prefers_readme(conn, tmp_path):
    root = tmp_path / "工具"
    root.mkdir()
    (root / "README.md").write_text(
        "# 单据助手\n\n这是一个整理单据的小工具。", encoding="utf-8")
    rows = ["单据编号,金额"] + [f"单据{i},100" for i in range(200)]
    (root / "data.csv").write_text("\n".join(rows), encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "单据助手是做什么的")

    assert result["citations"][0]["name"] == "README.md"


def test_followup_question_uses_previous_topic(conn, tmp_path):
    root = tmp_path / "资料"
    (root / "论文").mkdir(parents=True)
    (root / "其他").mkdir(parents=True)
    (root / "论文" / "初稿.md").write_text(
        "论文题目：本地知识检索。\n\n第三章 研究方法\n本文采用问卷调查与访谈相结合的方法。",
        encoding="utf-8")
    (root / "其他" / "笔记.md").write_text(
        "阅读笔记：第三章读起来有点晦涩，准备下周再读。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "那第三章呢", history=["我想看看论文写了什么"])

    assert result["context_used"] == "我想看看论文写了什么"
    assert result["citations"][0]["name"] == "初稿.md"


def test_followup_stays_in_previous_source(conn, tmp_path):
    tool_root = tmp_path / "工具资料"
    tool_root.mkdir()
    (tool_root / "README.md").write_text(
        "# 单据助手\n\n## 配置\n\n在 .env 中设置 OPENAI_API_KEY 与 OPENAI_BASE_URL。",
        encoding="utf-8")
    course_root = tmp_path / "课程资料"
    course_root.mkdir()
    (course_root / "笔记.md").write_text(
        "课程笔记：环境变量是操作系统提供的键值对。", encoding="utf-8")
    tool = store.add_source(conn, tool_root)
    course = store.add_source(conn, course_root)
    for source in (tool, course):
        scanner.scan_source(conn, source["source_id"])
        indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "那环境变量怎么配",
                        history=["单据助手怎么用"],
                        anchor_sources=[tool["source_id"]])

    assert result["citations"]
    assert all(item["path"].startswith(str(tool_root)) for item in result["citations"])


def test_cloud_answer_requires_explicit_consent(conn, indexed):
    answer.update_settings(conn, {"provider": "openai", "model": "gpt-test", "api_key": "sk-test"})

    with pytest.raises(answer.AnswerError, match="未获授权"):
        answer.ask(conn, "样本量是多少")


def test_cloud_answer_validates_citations(conn, indexed, monkeypatch):
    answer.update_settings(conn, {
        "provider": "openai", "model": "gpt-test", "api_key": "sk-test", "allow_cloud": True,
    })

    def fake_post(url, payload, headers, timeout=30.0):
        assert headers["Authorization"] == "Bearer sk-test"
        return {"choices": [{"message": {"content": "样本量为 42 例 [1]，另见 [9]。[1]"}}]}

    monkeypatch.setattr(answer, "_post_json", fake_post)
    result = answer.ask(conn, "样本量是多少")

    assert result["refused"] is False
    assert result["provider"] == "openai"
    assert "[1]" in result["answer"]
    assert result["warning"] and "[9]" in result["warning"]


def test_cloud_answer_without_citation_warns(conn, indexed, monkeypatch):
    answer.update_settings(conn, {
        "provider": "openai", "model": "gpt-test", "api_key": "sk-test", "allow_cloud": True,
    })
    monkeypatch.setattr(answer, "_post_json",
                        lambda url, payload, headers, timeout=30.0: {
                            "choices": [{"message": {"content": "样本量应该是 42 例。"}}]})
    result = answer.ask(conn, "样本量是多少")

    assert result["warning"] and "没有引用任何证据" in result["warning"]


def test_ollama_provider_uses_local_endpoint(conn, indexed, monkeypatch):
    answer.update_settings(conn, {"provider": "ollama", "model": "qwen2.5:7b"})
    seen = {}

    def fake_post(url, payload, headers, timeout=30.0):
        seen["url"] = url
        return {"message": {"content": "样本量 42 例 [1]"}}

    monkeypatch.setattr(answer, "_post_json", fake_post)
    result = answer.ask(conn, "样本量是多少")

    assert seen["url"].startswith("http://127.0.0.1:11434")
    assert result["provider"] == "ollama"
    assert result["warning"] is None


def test_settings_never_echo_api_key(conn):
    saved = answer.update_settings(conn, {"provider": "openai", "api_key": "sk-secret"})
    assert saved["has_key"] is True
    assert "api_key" not in saved

    cleared = answer.update_settings(conn, {"clear_key": True})
    assert cleared["has_key"] is False


def test_rejects_unknown_provider(conn):
    with pytest.raises(ValueError):
        answer.update_settings(conn, {"provider": "magic"})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_path_question_requires_a_path_hit(conn, tmp_path):
    root = tmp_path / "资料"
    (root / "数据").mkdir(parents=True)
    (root / "数据" / "笔记.md").write_text(
        "这里记录了一些普通的实验数据，与测序无关。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "转录组数据放在哪个文件夹")

    assert result["refused"] is True
    assert result["citations"] == []


def test_filename_subject_outranks_generic_content(conn, tmp_path):
    root = tmp_path / "实验"
    root.mkdir()
    (root / "专利说明.md").write_text(
        "本发明公开了一种基于全基因组混池测序的性别决定区定位方法。",
        encoding="utf-8")
    (root / "论文笔记.md").write_text(
        "创新点：本文提出了三个创新点，创新点一是指标体系，创新点二是模型。"
        "创新点分析还需要继续打磨。",
        encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = answer.ask(conn, "专利的创新点是什么")

    assert result["citations"][0]["name"] == "专利说明.md"


def test_api_ask_and_settings(client, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42 例\n", encoding="utf-8")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    assert client.post(f"/api/sources/{source_id}/scan").status_code == 202

    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    # 初次扫描只做 L0；确认策略后才会继续 L1/L2。
    confirmed = client.put("/api/inventory/policies", json={
        "source_id": source_id, "policies": {"md": "standard_parse"},
    })
    assert confirmed.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    created = client.post("/api/ask", json={"question": "样本量是多少"})
    assert created.status_code == 200
    body = created.json()
    assert body["refused"] is False
    assert body["citations"][0]["name"] == "记录.md"

    settings = client.get("/api/settings/answer").json()
    assert settings["provider"] == "extractive"
    assert settings["has_key"] is False

    updated = client.put("/api/settings/answer", json={"top_k": 4, "model": "gpt-test"})
    assert updated.status_code == 200
    assert updated.json()["top_k"] == 4

    refused = client.post("/api/ask", json={"question": "量子引力实验结论"})
    assert refused.status_code == 200
    assert refused.json()["refused"] is True

    bad = client.put("/api/settings/answer", json={"provider": "nope"})
    assert bad.status_code == 400


def test_api_followup_question_stays_in_previous_source(client, tmp_path):
    tool_root = tmp_path / "工具"
    tool_root.mkdir()
    (tool_root / "README.md").write_text(
        "# 单据助手\n\n这是一个整理单据的小工具。\n\n## 环境变量\n"
        "在 .env 里配置 OPENAI_API_KEY 与 OPENAI_BASE_URL。",
        encoding="utf-8")
    course_root = tmp_path / "课程"
    course_root.mkdir()
    (course_root / "笔记.md").write_text(
        "课程笔记：环境变量是操作系统提供的键值对，可用于配置程序。", encoding="utf-8")

    for root in (tool_root, course_root):
        source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
        client.post(f"/api/sources/{source_id}/scan")
        client.put("/api/inventory/policies", json={
            "source_id": source_id, "policies": {"md": "standard_parse"},
        })
        deadline = time.time() + 10
        while time.time() < deadline:
            if not client.get("/api/sources").json()["items"][0]["scanning"]:
                break
            time.sleep(0.05)

    session = client.post("/api/chat/sessions", json={"title": "追问"}).json()
    first = client.post("/api/ask", json={
        "question": "单据助手怎么用", "session_id": session["session_id"],
    }).json()
    assert first["citations"][0]["name"] == "README.md"

    second = client.post("/api/ask", json={
        "question": "那环境变量怎么配", "session_id": session["session_id"],
    }).json()
    assert second["context_used"] == "单据助手怎么用"
    assert second["citations"]
    assert all(item["path"].startswith(str(tool_root)) for item in second["citations"])
