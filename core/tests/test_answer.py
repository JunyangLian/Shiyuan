from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import answer, db, indexer, query, scanner, store


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
