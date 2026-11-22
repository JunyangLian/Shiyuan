from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from shiyuan import answer, db, indexer, ocr, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "ocr.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _fake_engine(monkeypatch, available: bool = True):
    info = {"available": available, "engine": "windows-ocr", "label": "Windows OCR",
            "languages": ["zh-Hans-CN"], "message": None if available else "无可用语言包"}
    monkeypatch.setattr(ocr, "engine_info", lambda *args, **kwargs: info)
    return info


def _scan_and_index(conn, root: Path) -> str:
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return source["source_id"]


def test_image_is_pending_then_searchable_via_ocr(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "extract_image",
                        lambda path: [ocr.Section(text="图片里写着 海豚 项目甲", page=None)])
    root = tmp_path / "资料"
    root.mkdir()
    (root / "截图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    source_id = _scan_and_index(conn, root)

    coverage = search.stats(conn)["coverage"]
    assert coverage["ocr_pending"] == 1
    assert coverage["ocr_indexed"] == 0
    assert coverage["content_unsupported"] == 0

    result = indexer.index_ocr_pending(conn, source_id)
    assert result["indexed"] == 1
    assert result["remaining"] == 0

    hits = search.search(conn, "海豚")
    assert hits["total"] == 1
    assert hits["items"][0]["index_level"] == "L2"

    coverage = search.stats(conn)["coverage"]
    assert coverage["ocr_indexed"] == 1
    assert coverage["ocr_pending"] == 0


def test_scanned_pdf_uses_ocr_with_page_number(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "extract_pdf",
                        lambda path, **kwargs: [ocr.Section(text="扫描页内容 麒麟", page=1)])
    root = tmp_path / "扫描件"
    root.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with (root / "IMG_001.pdf").open("wb") as handle:
        writer.write(handle)
    source_id = _scan_and_index(conn, root)

    assert indexer.ocr_pending_count(conn, source_id) == 1
    assert search.stats(conn)["coverage"]["mixed_pdf"] == 0
    result = indexer.index_ocr_pending(conn, source_id)
    assert result["indexed"] == 1

    hits = search.search(conn, "麒麟")
    assert hits["total"] == 1
    assert hits["items"][0]["page"] == 1
    assert hits["items"][0]["index_level"] == "L2"

    asked = answer.ask(conn, "麒麟")
    assert asked["refused"] is False
    assert asked["citations"][0]["ocr"] is True


def test_pdf_with_text_layer_is_not_sent_to_ocr(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    called = {"pdf": 0}

    def fail_pdf(path, **kwargs):
        called["pdf"] += 1
        return []

    monkeypatch.setattr(ocr, "extract_pdf", fail_pdf)
    root = tmp_path / "文本层"
    root.mkdir()
    import docx  # noqa: F401  仅保证环境依赖不变
    import pypdf
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (has text layer) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with (root / "normal.pdf").open("wb") as handle:
        writer.write(handle)

    source_id = _scan_and_index(conn, root)
    assert indexer.ocr_pending_count(conn, source_id) == 0
    indexer.index_ocr_pending(conn, source_id)
    assert called["pdf"] == 0


def test_mixed_pdf_ocr_only_missing_pages_and_keeps_boxes(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    seen: dict[str, set[int] | None] = {}

    def fake_extract_pdf(path, **kwargs):
        seen["skip_pages"] = kwargs.get("skip_pages")
        return [ocr.Section(text="第二页 麒麟", page=2, boxes=[[10, 20, 30, 40]])]

    monkeypatch.setattr(ocr, "extract_pdf", fake_extract_pdf)
    root = tmp_path / "混合版式"
    root.mkdir()

    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (page one text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=300, height=200)
    with (root / "mixed.pdf").open("wb") as handle:
        writer.write(handle)

    source_id = _scan_and_index(conn, root)
    coverage = search.stats(conn)["coverage"]
    assert coverage["mixed_pdf"] == 1
    assert indexer.ocr_pending_count(conn, source_id) == 1

    result = indexer.index_ocr_pending(conn, source_id)
    assert result["indexed"] == 1
    assert seen["skip_pages"] == {1}

    hits = search.search(conn, "麒麟")
    assert hits["total"] == 1
    item = hits["items"][0]
    assert item["page"] == 2
    assert item["bbox"] == [[10, 20, 30, 40]]

    asked = answer.ask(conn, "麒麟")
    assert asked["citations"][0]["bbox"] == [[10, 20, 30, 40]]
    assert search.stats(conn)["coverage"]["mixed_pdf"] == 0
    signature = conn.execute(
        "SELECT parser_signature FROM parse_artifacts WHERE parser_signature LIKE 'l2.%'"
    ).fetchone()["parser_signature"]
    assert signature == indexer.OCR_SIGNATURES["windows"]


def test_chunk_sections_aligns_ocr_line_boxes():
    section = ocr.Section(text="第一行\n第二行", page=3,
                          boxes=[[0, 0, 10, 5], [0, 10, 20, 5]])

    chunks = indexer.chunk_sections([section])

    assert len(chunks) == 1
    assert chunks[0].page == 3
    assert chunks[0].boxes == [[0, 0, 10, 5], [0, 10, 20, 5]]


def test_ocr_marks_empty_when_nothing_recognized(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "extract_image", lambda path: [])
    root = tmp_path / "资料"
    root.mkdir()
    (root / "空白.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    source_id = _scan_and_index(conn, root)

    result = indexer.index_ocr_pending(conn, source_id)
    assert result["empty"] == 1
    coverage = search.stats(conn)["coverage"]
    assert coverage["ocr_empty"] == 1
    assert coverage["ocr_pending"] == 0


def test_ocr_failure_is_recorded_and_does_not_leak(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "extract_image", lambda path: (_ for _ in ()).throw(ValueError("decode error")))
    root = tmp_path / "资料"
    root.mkdir()
    (root / "坏图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    source_id = _scan_and_index(conn, root)

    result = indexer.index_ocr_pending(conn, source_id)
    assert result["failed"] == 1
    coverage = search.stats(conn)["coverage"]
    assert coverage["ocr_failed"] == 1
    assert search.search(conn, "decode")["total"] == 0


def test_ocr_unavailable_is_reported(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch, available=False)
    root = tmp_path / "资料"
    root.mkdir()
    (root / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    source_id = _scan_and_index(conn, root)

    result = indexer.index_ocr_pending(conn, source_id)
    assert result["available"] is False
    assert result["processed"] == 0
    assert search.stats(conn)["coverage"]["ocr_available"] is False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_api_deep_scan_runs_ocr(client, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "extract_image",
                        lambda path: [ocr.Section(text="报表里写着 火烈鸟", page=None)])
    root = tmp_path / "资料"
    root.mkdir()
    (root / "截图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    assert client.post(f"/api/sources/{source_id}/scan").status_code == 202

    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    started = client.post(f"/api/sources/{source_id}/ocr")
    assert started.status_code == 202
    assert started.json()["pending"] == 1

    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    hits = client.get("/api/search", params={"q": "火烈鸟"}).json()
    assert hits["total"] == 1
    assert hits["items"][0]["index_level"] == "L2"
    assert client.get("/api/stats").json()["coverage"]["ocr_indexed"] == 1


def test_api_ocr_rejects_when_engine_unavailable(client, tmp_path, monkeypatch):
    _fake_engine(monkeypatch, available=False)
    root = tmp_path / "资料"
    root.mkdir()
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    response = client.post(f"/api/sources/{source_id}/ocr")
    assert response.status_code == 400


def test_auto_routes_scanned_pdf_to_rapidocr(conn, tmp_path, monkeypatch):
    _fake_engine(monkeypatch)
    monkeypatch.setattr(ocr, "_RAPID_AVAILABLE", True)
    calls = {"windows": 0, "rapid": 0}

    def fake_windows(path, **kwargs):
        calls["windows"] += 1
        return []

    def fake_rapid(path, **kwargs):
        calls["rapid"] += 1
        return [ocr.Section(text="扫描件 麒麟", page=1, boxes=[[1, 2, 30, 12]])]

    monkeypatch.setattr(ocr, "extract_pdf", fake_windows)
    monkeypatch.setattr(ocr, "extract_pdf_rapid", fake_rapid)
    root = tmp_path / "扫描件"
    root.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with (root / "scan.pdf").open("wb") as handle:
        writer.write(handle)

    source_id = _scan_and_index(conn, root)
    assert indexer.ocr_pending_count(conn, source_id) == 1

    result = indexer.index_ocr_pending(conn, source_id)

    assert result["indexed"] == 1
    assert calls == {"windows": 0, "rapid": 1}
    signature = conn.execute(
        "SELECT parser_signature FROM parse_artifacts WHERE parser_signature LIKE 'l2.%'"
    ).fetchone()["parser_signature"]
    assert signature == indexer.OCR_SIGNATURES["rapidocr"]
    hits = search.search(conn, "麒麟")
    assert hits["items"][0]["index_level"] == "L2"


def test_rapid_reading_order_handles_skewed_line_segments():
    left = [[22, 138], [615, 76], [622, 144], [29, 206]]
    right = [[616, 77], [1017, 37], [1023, 103], [623, 143]]

    ordered = ocr.order_rapid_items([("右段", right), ("左段", left)])

    assert [text for text, _ in ordered] == ["左段", "右段"]


def test_ocr_policy_settings_default_and_validation(client, monkeypatch):
    initial = client.get("/api/settings/ocr").json()
    assert initial["policy"] == "auto"
    rapid = next(item for item in initial["engines"] if item["engine"] == "rapidocr")
    assert rapid["available"] is False

    assert client.put("/api/settings/ocr", json={"policy": "rapidocr"}).status_code == 400
    assert client.put("/api/settings/ocr", json={"policy": "bad"}).status_code == 400
    saved = client.put("/api/settings/ocr", json={"policy": "windows"})
    assert saved.status_code == 200
    assert saved.json()["policy"] == "windows"

    monkeypatch.setattr(ocr, "_RAPID_AVAILABLE", True)
    saved = client.put("/api/settings/ocr", json={"policy": "rapidocr"})
    assert saved.status_code == 200
    assert saved.json()["policy"] == "rapidocr"
