from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from shiyuan import db, indexer, ingest, inventory, scanner, store, updates


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "inventory.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_classify_key_categories(tmp_path):
    jar = tmp_path / "lib.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        archive.writestr("META-INF/maven/demo/pom.properties", "groupId=demo\n")
        archive.writestr("demo/Example.class", b"\xca\xfe\xba\xbe")

    assert inventory.classify("A.class")["content_policy"] == "exclude_content"
    assert inventory.classify("A.class")["skip_reason"] == "non_knowledge_compile_artifact"
    assert inventory.classify(jar)["subcategory"] == "jar_library"
    assert inventory.classify(jar)["content_policy"] == "metadata_only"
    assert inventory.classify(jar)["jar"]["class_count"] == 1
    assert inventory.classify("photo.CR2")["category"] == "photo"
    assert inventory.classify("photo.CR2")["content_policy"] == "metadata_only"
    assert inventory.classify("design.psd")["category"] == "design"
    assert inventory.classify("design.psd")["content_policy"] == "metadata_only"
    assert inventory.classify("config.properties")["content_policy"] == "metadata_only"
    assert inventory.classify("tool.exe")["category"] == "software"
    assert inventory.classify("tool.exe")["content_policy"] == "metadata_only"
    assert inventory.classify("shot.png")["content_policy"] == "deep_parse"
    assert inventory.classify("anim.gif")["content_policy"] == "metadata_only"
    assert inventory.classify("notes.md")["content_policy"] == "standard_parse"
    assert inventory.classify("~$lock.docx")["skip_reason"] == "non_knowledge_temp_file"
    assert inventory.classify("project/.metadata/settings.properties")["category"] == "ide_metadata"
    assert inventory.classify("project/.metadata/settings.properties")["content_policy"] == "exclude_content"


def test_config_and_subtitle_default_to_metadata_only():
    """工具配置与字幕属于非知识内容，默认不进正文索引。"""
    assert inventory.classify("app.properties")["content_policy"] == "metadata_only"
    assert inventory.classify("movie.ass")["content_policy"] == "metadata_only"


def test_declared_parse_formats_are_really_parseable():
    """声明「标准解析」的扩展名必须真的能被解析器处理，防止能力清单漂移。"""
    from shiyuan import extractors, ocr

    declared: set[str] = set()
    for name in ("_DOCUMENT_EXT", "_SHEET_EXT", "_PRESENTATION_EXT",
                 "_TEXT_EXT", "_CODE_EXT", "_EMAIL_EXT", "_CAD_EXT"):
        declared |= getattr(inventory, name)
    unsupported = sorted(
        ext for ext in declared
        if ext not in extractors.SUPPORTED_EXTS and ext not in ocr.OCR_EXTS)
    assert unsupported == []


def test_unknown_extension_is_metadata_only(tmp_path):
    """未知后缀默认只登记不解析：支持哪些格式是显式契约，不做内容嗅探。"""
    text_file = tmp_path / "JL1_groups.assembly"
    text_file.write_text(">ptg000001l 1 12345678\n", encoding="utf-8")
    info = inventory.classify(text_file, text_file.stat().st_size)
    assert info["content_policy"] == "metadata_only"
    assert info["skip_reason"] == "unknown_format"


def test_non_knowledge_extensions_default_to_metadata_only():
    """网页/前端脚本/日志/结构化数据/压缩包默认只登记，不进入正文索引。"""
    for name in ("page.html", "style.css", "data.json", "index.xml",
                 "app.js", "view.tsx", "run.log", "pack.zip", "bundle.7z"):
        info = inventory.classify(name)
        assert info["content_policy"] == "metadata_only", name
        assert info["skip_reason"] == "non_knowledge_content", name
    # 真正的文档与源码项目仍按标准解析。
    assert inventory.classify("notes.md")["content_policy"] == "standard_parse"
    assert inventory.classify("analysis.py")["content_policy"] == "standard_parse"
    assert inventory.classify("paper.pdf")["content_policy"] == "standard_parse"


def test_no_extension_sniffing(tmp_path):
    text = tmp_path / "README"
    text.write_text("这是说明文本\n", encoding="utf-8")
    binary = tmp_path / "BIN"
    binary.write_bytes(b"\x00\x01\x02\x03")
    empty = tmp_path / "EMPTY"
    empty.write_bytes(b"")

    assert inventory.classify(text)["subcategory"] == "no_extension"
    assert inventory.classify(binary)["subcategory"] == "no_extension_binary"
    assert inventory.classify(empty)["subcategory"] == "empty"


def test_file_types_and_policy_roundtrip(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "报告.md").write_text("正文", encoding="utf-8")
    (root / "A.class").write_bytes(b"\xca\xfe\xba\xbe")
    (root / "截图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    data = inventory.file_types(conn)
    items = {item["ext"]: item for item in data["items"]}
    assert items["md"]["recommended_policy"] == "standard_parse"
    assert items["class"]["recommended_policy"] == "exclude_content"
    assert items["png"]["recommended_policy"] == "deep_parse"

    saved = inventory.save_policies(conn, {"md": "metadata_only", "class": "exclude_content"})
    assert saved["md"] == "metadata_only"
    assert inventory.load_policies(conn)["md"] == "metadata_only"

    refreshed = inventory.file_types(conn)
    assert refreshed["policies"]["md"] == "metadata_only"

    with pytest.raises(ValueError):
        inventory.save_policies(conn, {"md": "unknown_policy"})


def test_policies_are_scoped_to_source(conn, tmp_path):
    roots = []
    for name in ("甲", "乙"):
        root = tmp_path / name
        root.mkdir()
        (root / "笔记.md").write_text("正文", encoding="utf-8")
        (root / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        roots.append(root)

    first = store.add_source(conn, roots[0])
    second = store.add_source(conn, roots[1])
    for source in (first, second):
        scanner.scan_source(conn, source["source_id"])

    assert inventory.load_policies(conn, first["source_id"]) == {}
    assert inventory.is_confirmed(conn, first["source_id"]) is False
    assert inventory.allowed_exts(conn, first["source_id"]) is None

    inventory.save_policies(conn, {"md": "standard_parse"}, source_id=first["source_id"])
    assert inventory.is_confirmed(conn, first["source_id"]) is True
    assert inventory.allowed_exts(conn, first["source_id"]) == {"md"}
    assert inventory.load_policies(conn, second["source_id"]) == {}
    assert inventory.allowed_exts(conn, second["source_id"]) is None

    inventory.save_policies(
        conn, {"md": "exclude_content", "png": "metadata_only"},
        source_id=second["source_id"],
    )
    assert inventory.allowed_exts(conn, second["source_id"]) == set()

    first_data = inventory.file_types(conn, source_id=first["source_id"])
    second_data = inventory.file_types(conn, source_id=second["source_id"])
    assert {item["ext"] for item in first_data["items"]} == {"md", "png"}
    assert first_data["confirmed"] is True
    assert second_data["confirmed"] is True
    assert first_data["policies"] == {"md": "standard_parse"}
    assert second_data["policies"]["png"] == "metadata_only"


def test_allowed_exts_adds_newly_supported_code(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "笔记.md").write_text("正文", encoding="utf-8")
    (root / "脚本.pl").write_text("print 1;\n", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    inventory.save_policies(
        conn, {"md": "standard_parse"}, source_id=source["source_id"])

    allowed = inventory.allowed_exts(conn, source["source_id"])

    # 策略快照里没有 pl，但源里实际存在，且 pl 是后来新增支持的解析类型。
    assert "pl" in allowed
    assert "md" in allowed


def test_allowed_exts_filters_parse_queues(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "笔记.md").write_text("正文", encoding="utf-8")
    (root / "数据.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    source = store.add_source(conn, root)
    source_id = source["source_id"]
    scanner.scan_source(conn, source_id)
    inventory.save_policies(
        conn, {"md": "standard_parse", "csv": "metadata_only", "png": "exclude_content"},
        source_id=source_id,
    )

    rows = indexer.pending_rows(conn, source_id, allowed_exts={"md"})
    assert {row["ext"] for row in rows} == {"md"}
    assert indexer.pending_count(conn, source_id, allowed_exts={"md"}) == 1
    assert indexer.pending_count(conn, source_id, allowed_exts=set()) == 0
    assert indexer.ocr_pending_count(conn, source_id, allowed_exts=set()) == 0


def test_parse_results_lists_success_and_failure(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "好.md").write_text("正文内容", encoding="utf-8")
    (root / "坏.docx").write_bytes(b"PK\x03\x04fake")
    (root / "未知.xyz").write_bytes(b"data")

    source = store.add_source(conn, root)
    source_id = source["source_id"]
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)

    report = indexer.parse_results(conn, source_id)
    items = {item["name"]: item for item in report["items"]}
    assert items["好.md"]["status"] == "ok"
    assert items["好.md"]["stage"] == "L1"
    assert items["好.md"]["chars"] > 0
    assert items["坏.docx"]["status"] == "failed"
    assert items["坏.docx"]["error"]
    assert items["未知.xyz"]["status"] == "unsupported"
    assert report["summary"]["parsed"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["unsupported"] == 1

    failed = indexer.parse_results(conn, source_id, status="failed")
    assert failed["total"] == 1
    assert failed["items"][0]["name"] == "坏.docx"

    progress = indexer.parse_progress(conn, source_id)
    assert progress == {"total": 3, "parsed": 1, "failed": 1, "empty": 0,
                        "pending": 0, "locked": 0, "unsupported": 1,
                        "metadata_only": 0}


def test_metadata_only_files_are_not_pending(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "说明.md").write_text("正文内容", encoding="utf-8")
    (root / "资料包.zip").write_bytes(b"PK\x03\x04fake")

    source = store.add_source(conn, root)
    source_id = source["source_id"]
    inventory.save_policies(conn, {"md": "standard_parse", "zip": "metadata_only"},
                            source_id=source_id)
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id,
                          allowed_exts=inventory.allowed_exts(conn, source_id))

    report = indexer.parse_results(conn, source_id)
    items = {item["name"]: item for item in report["items"]}
    assert items["资料包.zip"]["status"] == "metadata_only"
    assert items["说明.md"]["status"] == "ok"
    assert report["summary"]["metadata_only"] == 1
    assert report["summary"]["pending"] == 0

    progress = indexer.parse_progress(conn, source_id)
    assert progress["metadata_only"] == 1
    assert progress["pending"] == 0

    filtered = indexer.parse_results(conn, source_id, status="metadata_only")
    assert filtered["total"] == 1
    assert filtered["items"][0]["name"] == "资料包.zip"


def test_incremental_parse_only_new_or_modified(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    note = root / "笔记.md"
    note.write_text("第一版", encoding="utf-8")

    source = store.add_source(conn, root)
    source_id = source["source_id"]
    scanner.scan_source(conn, source_id)
    inventory.save_policies(conn, {"md": "standard_parse"}, source_id=source_id)

    first = indexer.index_incremental_in_new_connection(
        source_id, tmp_path / "inventory.db")
    assert first["skipped"] is False
    assert first["l1"]["indexed"] == 1

    again = indexer.index_incremental_in_new_connection(
        source_id, tmp_path / "inventory.db")
    assert again["l1"]["indexed"] == 0

    note.write_text("第二版，内容变长了一些", encoding="utf-8")
    scanner.scan_source(conn, source_id)
    changed = indexer.index_incremental_in_new_connection(
        source_id, tmp_path / "inventory.db")
    assert changed["l1"]["indexed"] == 1

    ingest.update(conn, auto_parse=False)
    note.write_text("第三版", encoding="utf-8")
    scanner.scan_source(conn, source_id)
    skipped = indexer.index_incremental_in_new_connection(
        source_id, tmp_path / "inventory.db")
    assert skipped["skipped"] is True
    assert skipped["reason"] == "auto_parse_off"
    assert indexer.pending_count(conn, source_id, allowed_exts={"md"}) == 1


def test_updates_overview_reports_activity(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "报告.md").write_text("正文内容", encoding="utf-8")

    source = store.add_source(conn, root)
    source_id = source["source_id"]
    scanner.scan_source(conn, source_id)
    indexer.index_pending(conn, source_id)

    report = updates.overview(conn, watcher_running=True, watch_enabled=True)
    assert report["auto_parse"] is True
    assert report["watcher_running"] is True
    assert report["summary"]["parsed_24h"] >= 1
    assert report["summary"]["pending_l1"] == 0
    assert any(item["name"] == "报告.md" for item in report["recent"])
    assert report["recent"][0]["stage"] in {"L1", "L2"}


def test_first_ingest_only_l0_until_policy_confirmed(conn, tmp_path, monkeypatch):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "报告.md").write_text("正文", encoding="utf-8")

    source = store.add_source(conn, root)
    source_id = source["source_id"]
    conn.commit()

    original_connect = db.connect
    monkeypatch.setattr(scanner.db, "connect", lambda *args, **kwargs: conn)
    calls: list[dict] = []

    def fake_index_pending(*args, **kwargs):
        calls.append(kwargs)
        return {"paused": False, "indexed": 0, "remaining": 1}

    monkeypatch.setattr(scanner.indexer, "index_pending", fake_index_pending)

    scanner._run_pipeline(source_id)

    assert calls == []
    assert scanner.is_running(source_id) is False

    check = original_connect(tmp_path / "inventory.db")
    try:
        row = store.get_source(check, source_id)
        assert row["l0_completed_at"] is not None
        assert row["l1_completed_at"] is None
        events = {
            item["type"] for item in check.execute(
                "SELECT type FROM maintenance_events WHERE source_id = ?", (source_id,)
            ).fetchall()
        }
        assert "inventory_ready" in events
        inventory.save_policies(check, {"md": "standard_parse"}, source_id=source_id)
    finally:
        check.close()

    reopened = original_connect(tmp_path / "inventory.db")
    monkeypatch.setattr(scanner.db, "connect", lambda *args, **kwargs: reopened)
    scanner._run_pipeline(source_id)

    assert calls and calls[0]["allowed_exts"] == {"md"}
    assert calls[0]["ignore_budget"] is True


def test_dry_run_aggregates_without_writing(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "报告.md").write_text("正文", encoding="utf-8")
    (root / "数据.xlsx").write_bytes(b"not a real xlsx")
    (root / "截图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (root / "照片.CR2").write_bytes(b"raw")
    (root / "设计.psd").write_bytes(b"psd")
    (root / "工具.exe").write_bytes(b"MZ" + b"0" * 16)
    (root / "A.class").write_bytes(b"\xca\xfe\xba\xbe")
    (root / "README").write_text("说明", encoding="utf-8")
    metadata = root / "project" / ".metadata"
    metadata.mkdir(parents=True)
    (metadata / "settings.properties").write_text("key=value\n", encoding="utf-8")

    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    report = inventory.dry_run(conn, sample_limit=3)

    assert report["total_files"] == 9
    assert report["rules_version"] == inventory.RULES_VERSION
    assert report["policies"]["standard_parse"]["count"] >= 2
    assert report["policies"]["deep_parse"]["count"] == 1
    assert report["policies"]["metadata_only"]["count"] >= 4
    assert report["policies"]["exclude_content"]["count"] >= 2
    assert report["skip_reasons"]["non_knowledge_compile_artifact"]["count"] == 1
    assert report["skip_reasons"]["non_knowledge_ide_cache"]["count"] >= 1
    assert report["skip_reasons"]["non_knowledge_runtime"]["count"] == 1
    assert report["categories"]
    assert report["conflicts"] == []
