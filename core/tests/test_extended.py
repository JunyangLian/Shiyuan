from __future__ import annotations

import wave
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest
from PIL import Image

from shiyuan import db, extractors, indexer, ocr, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "extended.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _scan_and_index(conn, root: Path) -> str:
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return source["source_id"]


def _fake_engine(monkeypatch, available: bool = True):
    info = {"available": available, "engine": "windows-ocr", "label": "Windows OCR",
            "languages": ["zh-Hans-CN"], "message": None if available else "无可用语言包"}
    monkeypatch.setattr(ocr, "engine_info", lambda *args, **kwargs: info)
    return info


def test_zip_indexes_inner_text_nested_archives_and_dedupes(conn, tmp_path):
    import py7zr

    root = tmp_path / "资料"
    root.mkdir()
    nested = tmp_path / "嵌套.7z"
    with py7zr.SevenZipFile(nested, "w") as archive:
        archive.writestr("嵌套内容关键词 雪松\n", "inner/nested.md")
    with zipfile.ZipFile(root / "资料包.zip", "w") as archive:
        archive.writestr("docs/说明.md", "压缩包正文关键词 梧桐\n")
        archive.writestr("docs/说明副本.md", "压缩包正文关键词 梧桐\n")
        archive.write(nested, "inner/nested.7z")
        archive.writestr("../逃逸.md", "逃逸路径内容不应该出现\n")

    _scan_and_index(conn, root)

    assert search.search(conn, "梧桐")["total"] == 1
    assert search.search(conn, "雪松")["total"] == 1
    assert search.search(conn, "逃逸路径")["total"] == 0
    text = "\n".join(row["text"] for row in conn.execute("SELECT text FROM chunks"))
    assert "【docs/说明.md】" in text
    assert "【inner/nested.7z/inner/nested.md】" in text


def test_standalone_7z_is_indexed(conn, tmp_path):
    import py7zr

    root = tmp_path / "资料"
    root.mkdir()
    with py7zr.SevenZipFile(root / "单包.7z", "w") as archive:
        archive.writestr("七压关键词 银杏\n", "笔记.md")

    _scan_and_index(conn, root)
    assert search.search(conn, "银杏")["total"] == 1


def test_eml_indexes_body_attachment_and_nested_message(conn, tmp_path):
    root = tmp_path / "邮件"
    root.mkdir()
    message = EmailMessage()
    message["Subject"] = "邮件主题 银杏"
    message["From"] = "a@example.com"
    message["To"] = "b@example.com"
    message.set_content("邮件正文关键词 银杏叶\n")
    message.add_attachment(
        "附件正文关键词 枫树\n".encode("utf-8"),
        maintype="text", subtype="plain", filename="附件.md",
    )
    nested = EmailMessage()
    nested["Subject"] = "嵌套邮件 松果"
    nested.set_content("嵌套正文 松针\n")
    message.add_attachment(nested)
    (root / "邮件.eml").write_bytes(message.as_bytes())

    _scan_and_index(conn, root)

    assert search.search(conn, "银杏叶")["total"] == 1
    assert search.search(conn, "枫树")["total"] == 1
    assert search.search(conn, "松果")["total"] == 1
    assert search.search(conn, "松针")["total"] == 1


def test_dxf_indexes_text_layers_and_block_attributes(conn, tmp_path):
    import ezdxf

    root = tmp_path / "图纸"
    root.mkdir()
    document = ezdxf.new()
    modelspace = document.modelspace()
    modelspace.add_text("CAD文字关键词 橡树", dxfattribs={"layer": "标注"})
    modelspace.add_mtext("多行文字关键词 榕树", dxfattribs={"layer": "说明"})
    block = document.blocks.new(name="标题栏")
    block.add_attdef("项目名称", dxfattribs={"height": 2.5})
    insert = modelspace.add_blockref("标题栏", (0, 0))
    insert.add_auto_attribs({"项目名称": "白桦林工程"})
    document.saveas(root / "图纸.dxf")

    _scan_and_index(conn, root)

    assert search.search(conn, "橡树")["total"] == 1
    assert search.search(conn, "榕树")["total"] == 1
    assert search.search(conn, "白桦林工程")["total"] == 1
    assert search.search(conn, "标注")["total"] == 1


def test_audio_metadata_is_indexed_without_transcription(conn, tmp_path):
    root = tmp_path / "录音"
    root.mkdir()
    with wave.open(str(root / "会议.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8000)

    _scan_and_index(conn, root)

    hits = search.search(conn, "采样率")
    assert hits["total"] == 1
    text = "\n".join(row["text"] for row in conn.execute("SELECT text FROM chunks"))
    assert "时长：" in text
    assert "WAVE" in text


@pytest.mark.skipif(extractors.soffice_path() is None, reason="未安装 LibreOffice")
def test_xls_falls_back_to_xlrd_when_conversion_fails(conn, tmp_path, monkeypatch):
    import openpyxl

    root = tmp_path / "旧表"
    root.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "汇总"
    sheet.append(["项目", "数值"])
    sheet.append(["橡树", 12])
    modern = staging / "modern.xlsx"
    workbook.save(str(modern))
    produced = extractors.convert_legacy(modern, "xls", staging)
    produced.rename(root / "旧表.xls")

    def broken_conversion(*args, **kwargs):
        raise extractors.ExtractError("模拟转换失败")

    monkeypatch.setattr(extractors, "convert_legacy", broken_conversion)
    _scan_and_index(conn, root)

    hits = search.search(conn, "橡树")
    assert hits["total"] == 1
    assert hits["items"][0]["sheet"] == "汇总"


def test_pptx_embedded_images_enter_ocr(conn, tmp_path, monkeypatch):
    from pptx import Presentation
    from pptx.util import Inches

    _fake_engine(monkeypatch)
    monkeypatch.setattr(
        ocr, "extract_image",
        lambda path: [ocr.Section(text="图片文字 ZEBRA", page=None)],
    )
    root = tmp_path / "汇报"
    root.mkdir()
    image_path = tmp_path / "scan.png"
    Image.new("RGB", (320, 120), "white").save(image_path)
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "图片页"
    slide.shapes.add_picture(str(image_path), Inches(1), Inches(1.5), width=Inches(4))
    presentation.save(str(root / "图片汇报.pptx"))

    source_id = _scan_and_index(conn, root)
    artifact = conn.execute(
        "SELECT image_count FROM parse_artifacts WHERE parser_signature = ?",
        (indexer.PARSER_SIGNATURE,),
    ).fetchone()
    assert artifact["image_count"] == 1
    assert indexer.ocr_pending_count(conn, source_id) == 1

    result = indexer.index_ocr_pending(conn, source_id)
    assert result["indexed"] == 1
    hits = search.search(conn, "ZEBRA")
    assert hits["total"] == 1
    assert hits["items"][0]["page"] == 1
    signatures = {
        row["parser_signature"]
        for row in conn.execute("SELECT parser_signature FROM parse_artifacts")
    }
    assert indexer.PPTX_OCR_SIGNATURES["windows"] in signatures
