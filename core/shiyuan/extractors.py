"""L1 文本提取：把常见文档转成可检索的章节（含页码 / 工作表定位）。

只读取原文件，不做 OCR，也不改写原件。提取失败必须显式记录，
不能让"没提取到"看起来像"内容里确实没有"。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_BYTES = 64 * 1024 * 1024
# 纯文本按头部采样：几百 MB 的日志/表格只需要开头就能被检索到，
# 不再因为超过整文件上限直接判失败。
MAX_TEXT_SAMPLE_BYTES = 8 * 1024 * 1024
MAX_CHARS = 2_000_000

# 少于此长度的 PDF 页文本更可能是水印 / 页眉残片，留给 OCR 补全而不是当作正文。
PDF_MIN_PAGE_CHARS = 8

TEXT_EXTS = {
    "txt", "md", "markdown", "csv", "tsv", "log", "json", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "xml", "html", "htm", "css", "js", "jsx",
    "ts", "tsx", "py", "ipynb", "java", "c", "h", "cpp", "hpp", "cs", "go",
    "rs", "rb", "php", "sh", "bat", "ps1", "sql", "r", "m", "tex", "bib", "pl",
    # 盘点策略里声明为标准解析的纯文本/源码类型，必须与解析器能力一致，
    # 否则会显示「可解析」却永远解析失败。
    "rst", "cmd", "jsp", "kt", "kts", "scala", "lua", "svelte", "vue",
}
PDF_EXTS = {"pdf"}
DOCX_EXTS = {"docx"}
PPTX_EXTS = {"pptx"}
SHEET_EXTS = {"xlsx", "xlsm"}
# 旧版 Office 与开放文档格式统一由 LibreOffice 转成 OOXML 后再走同一套解析；
# 转换产物只写临时目录。未安装 LibreOffice 时明确报错，不静默当成无文本。
LEGACY_EXTS = {
    "doc": "docx", "ppt": "pptx", "xls": "xlsx",
    "odt": "docx", "rtf": "docx", "ods": "xlsx", "odp": "pptx", "pptm": "pptx",
}
ARCHIVE_EXTS = {"zip", "7z", "rar"}
EMAIL_EXTS = {"eml", "msg", "mbox"}
CAD_EXTS = {"dxf", "dwg"}
AUDIO_EXTS = {"mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma", "aiff"}
VIDEO_EXTS = {"mp4", "m4v", "mov", "wmv", "avi", "mkv", "webm", "flv"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

SUPPORTED_EXTS = (
    TEXT_EXTS | PDF_EXTS | DOCX_EXTS | PPTX_EXTS | SHEET_EXTS
    | set(LEGACY_EXTS) | ARCHIVE_EXTS | EMAIL_EXTS | CAD_EXTS | MEDIA_EXTS
)

MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 300
MAX_ARCHIVE_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_PARSE_ENTRIES = 20
MAX_ARCHIVE_DEPTH = 2
MAX_MAIL_ATTACHMENTS = 20
MAX_MAIL_DEPTH = 2

_SOFFICE_LOCK = threading.Lock()
_SOFFICE_PROFILE: str | None = None


class ExtractError(RuntimeError):
    """文件存在但无法解析（加密、损坏、缺文本层之外的解析异常）。"""


class UnsupportedType(RuntimeError):
    """当前版本不处理该类型。"""


@dataclass
class Section:
    text: str
    page: int | None = None
    sheet: str | None = None


def is_supported(ext: str) -> bool:
    return ext.lower().lstrip(".") in SUPPORTED_EXTS


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_text(path: Path) -> list[Section]:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size <= MAX_TEXT_SAMPLE_BYTES:
        return [Section(text=_decode(path.read_bytes()))]
    with path.open("rb") as handle:
        data = handle.read(MAX_TEXT_SAMPLE_BYTES)
    text = _decode(data)
    text += (f"\n\n[已截断] 原文件 {size / (1024 * 1024):.1f}MB，"
             f"仅解析前 {MAX_TEXT_SAMPLE_BYTES // (1024 * 1024)}MB；"
             "其余内容未进入索引。")
    return [Section(text=text)]


def _usable_pdf_text(text: str) -> bool:
    """判断 PDF 页文本是否值得作为正文保留；否则留空交给 L2 OCR。"""
    stripped = text.strip()
    if len(stripped) < PDF_MIN_PAGE_CHARS:
        return False
    visible = [char for char in stripped if not char.isspace()]
    if not visible:
        return False
    bad = sum(
        1 for char in visible
        if char == "\ufffd" or ord(char) < 32 or 0xE000 <= ord(char) <= 0xF8FF
    )
    return bad <= max(20, len(visible) * 0.05)


def _read_pdf_pypdf(path: Path) -> list[Section]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ExtractError("PDF 已加密，无法提取文本层")
    sections = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # pypdf 对个别畸形页会抛各种异常
            raise ExtractError(f"第 {index} 页解析失败：{exc}") from exc
        sections.append(Section(text=text if _usable_pdf_text(text) else "", page=index))
    return sections


def _read_pdf(path: Path) -> list[Section]:
    """优先用 PyMuPDF 提取文本；失败的页留空，由 OCR 补齐。"""
    try:
        import fitz
    except ImportError:
        return _read_pdf_pypdf(path)
    try:
        document = fitz.open(str(path))
    except Exception:
        return _read_pdf_pypdf(path)
    try:
        if document.needs_pass:
            raise ExtractError("PDF 已加密，无法提取文本层")
        sections = []
        for index, page in enumerate(document, start=1):
            text = page.get_text("text") or ""
            sections.append(Section(text=text if _usable_pdf_text(text) else "", page=index))
        return sections
    except ExtractError:
        raise
    except Exception:
        # 个别 PDF 的字体 / 结构会让 MuPDF 打开成功但抽取失败，退回 pypdf 再试一次。
        return _read_pdf_pypdf(path)
    finally:
        document.close()


def _paragraph_lines(paragraphs) -> list[str]:
    return [
        paragraph.text.strip()
        for paragraph in paragraphs
        if paragraph.text and paragraph.text.strip()
    ]


def _md_cell(value) -> str:
    """单元格文本：压平换行与制表符，转义竖线，避免破坏 markdown 表格列。"""
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|")


def _md_table(rows) -> list[str]:
    """把二维单元格转成 GitHub 风格 markdown 表格：首行为表头，补分隔行与空列。"""
    normalized = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            # 常见情况：整行带制表符的表格被粘进一个单元格，按制表符还原成多列。
            cells.extend(str(cell or "").split("\t"))
        cells = [_md_cell(cell) for cell in cells]
        if any(cells):
            normalized.append(cells)
    if not normalized:
        return []
    width = max(len(row) for row in normalized)
    normalized = [row + [""] * (width - len(row)) for row in normalized]
    header, *body = normalized
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return lines


def _table_lines(tables) -> list[str]:
    """多个表格之间留空行，每个表格都带表头与分隔行，便于 md 渲染与预览。"""
    lines: list[str] = []
    for table in tables:
        block = _md_table([[cell.text for cell in row.cells] for row in table.rows])
        if not block:
            continue
        if lines:
            lines.append("")
        lines.extend(block)
    return lines


def _read_docx(path: Path) -> list[Section]:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    # 按正文顺序读取段落与表格，避免把所有表格堆到文末、彼此首尾相接。
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, document).text
            if text.strip():
                lines.append(text)
        elif child.tag == qn("w:tbl"):
            block = _table_lines([Table(child, document)])
            if block:
                if lines:
                    lines.append("")
                lines.extend(block)
                lines.append("")

    for section in document.sections:
        for part in (section.header, section.footer):
            if part.is_linked_to_previous:
                continue
            lines.extend(_paragraph_lines(part.paragraphs))
            lines.extend(_table_lines(part.tables))

    for comment in document.comments:
        text = (comment.text or "").strip()
        if not text:
            continue
        author = (comment.author or "").strip()
        lines.append(f"[批注{' ' + author if author else ''}] {text.replace(chr(10), ' ')}")

    return [Section(text="\n".join(lines))]


def _shape_lines(shape) -> list[str]:
    """按文档流顺序取形状内文字：组合形状递归，表格按行拼接。"""
    if getattr(shape, "has_text_frame", False):
        lines = _paragraph_lines(shape.text_frame.paragraphs)
        if lines:
            return lines
    if getattr(shape, "has_table", False):
        return _table_lines([shape.table])
    if hasattr(shape, "shapes"):  # 组合形状
        lines = []
        for child in shape.shapes:
            lines.extend(_shape_lines(child))
        return lines
    return []


def _read_pptx(path: Path) -> list[Section]:
    from pptx import Presentation

    presentation = Presentation(str(path))
    sections = []
    for index, slide in enumerate(presentation.slides, start=1):
        # 每个形状自成一个块、块之间空行：表格不会被前后文字挤在一起。
        blocks: list[str] = []
        for shape in slide.shapes:
            chunk = "\n".join(_shape_lines(shape)).strip()
            if chunk and chunk not in blocks:
                blocks.append(chunk)
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                blocks.append(f"[备注] {notes.replace(chr(10), ' ')}")
        sections.append(Section(text="\n\n".join(blocks), page=index))
    return sections


_OCR_IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"}


def _iter_picture_shapes(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_picture_shapes(shape.shapes)
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            yield shape


def pptx_image_count(path: str | Path) -> int:
    """统计可交给 OCR 的内嵌位图数量；打不开时返回 0，不影响 L1 结果。"""
    try:
        from pptx import Presentation

        presentation = Presentation(str(path))
    except Exception:
        return 0
    count = 0
    for slide in presentation.slides:
        for shape in _iter_picture_shapes(slide.shapes):
            try:
                ext = (shape.image.ext or "").lower()
            except Exception:
                continue
            if ext in _OCR_IMAGE_EXTS:
                count += 1
    return count


def soffice_path() -> str | None:
    """定位 LibreOffice 命令行入口；支持 SHIYUAN_SOFFICE 覆盖。"""
    override = os.environ.get("SHIYUAN_SOFFICE")
    if override:
        candidate = Path(override)
        if candidate.exists():
            return str(candidate)
    for name in ("soffice.com", "soffice"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        try:
            import winreg
        except ImportError:
            pass
        else:
            views = (
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0),
            )
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in views:
                    try:
                        with winreg.OpenKey(
                            hive, r"SOFTWARE\LibreOffice\UNO\InstallPath", 0, view
                        ) as key:
                            folder = winreg.QueryValueEx(key, "")[0]
                    except OSError:
                        continue
                    for name in ("soffice.com", "soffice.exe"):
                        candidate = Path(folder) / name
                        if candidate.exists():
                            return str(candidate)
    for base in (r"C:\Program Files\LibreOffice\program",
                 r"C:\Program Files (x86)\LibreOffice\program"):
        for name in ("soffice.com", "soffice.exe"):
            candidate = Path(base) / name
            if candidate.exists():
                return str(candidate)
    return None


def _soffice_profile_uri() -> str:
    """独立的一次性用户配置目录，避免与用户正在使用的 LibreOffice 抢锁。"""
    global _SOFFICE_PROFILE
    if _SOFFICE_PROFILE is None:
        _SOFFICE_PROFILE = tempfile.mkdtemp(prefix="shiyuan-lo-profile-")
    return Path(_SOFFICE_PROFILE).as_uri()


def convert_legacy(path: Path, target_ext: str, outdir: Path) -> Path:
    """用 LibreOffice 把旧版 Office 文件转成 OOXML；只读取原件，输出写到 outdir。"""
    executable = soffice_path()
    if executable is None:
        raise ExtractError("未检测到 LibreOffice，无法解析旧版 Office 文件")
    command = [
        executable,
        "--headless", "--norestore", "--invisible", "--nologo",
        f"-env:UserInstallation={_soffice_profile_uri()}",
        "--convert-to", target_ext,
        "--outdir", str(outdir),
        str(path),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    timeout_seconds = 90
    stdout = ""
    stderr = ""
    try:
        with _SOFFICE_LOCK:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True, creationflags=creationflags,
                    )
                else:
                    process.kill()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise ExtractError(
                    f"LibreOffice 转换超时（超过 {timeout_seconds} 秒），已终止转换进程"
                )
    except OSError as exc:
        raise ExtractError(f"无法启动 LibreOffice：{exc}") from exc
    output = Path(outdir) / f"{path.stem}.{target_ext}"
    if not output.exists():
        matches = sorted(Path(outdir).glob(f"*.{target_ext}"))
        if matches:
            output = matches[0]
    if process.returncode != 0 or not output.exists():
        detail = (stderr or stdout or "").strip()
        raise ExtractError(f"LibreOffice 转换失败：{detail[:300] or '未生成输出文件'}")
    return output


def _read_xls(path: Path) -> list[Section]:
    """旧版 XLS 的备用解析器；LibreOffice 不可用或转换失败时直接读单元格。"""
    import xlrd

    try:
        book = xlrd.open_workbook(str(path))
    except Exception as exc:
        raise ExtractError(f"XLS 备用解析失败：{exc}") from exc
    sections = []
    for sheet in book.sheets():
        rows = []
        for row_index in range(sheet.nrows):
            cells = [
                _cell_text(sheet.cell_value(row_index, col))
                for col in range(sheet.ncols)
            ]
            if any(cells):
                rows.append(cells)
        sections.append(Section(text="\n".join(_md_table(rows)), sheet=sheet.name))
    return sections


def _read_legacy(path: Path, target_ext: str) -> list[Section]:
    try:
        with tempfile.TemporaryDirectory(prefix="shiyuan-office-") as outdir:
            output = convert_legacy(path, target_ext, Path(outdir))
            if target_ext == "docx":
                return _read_docx(output)
            if target_ext == "pptx":
                return _read_pptx(output)
            return _read_sheet(output)
    except ExtractError as exc:
        if target_ext != "xlsx":
            raise
        try:
            return _read_xls(path)
        except Exception as fallback_exc:
            raise ExtractError(
                f"LibreOffice 转换失败（{exc}），备用解析也失败：{fallback_exc}"
            ) from fallback_exc


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _collect_zip(path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    with zipfile.ZipFile(path) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ExtractError(f"压缩包条目超过 {MAX_ARCHIVE_ENTRIES}，已拒绝解析")
        members: list[tuple[str, bytes]] = []
        skipped: list[str] = []
        total = 0
        for info in infos:
            name = _safe_member_name(info.filename)
            if not name:
                continue
            if info.flag_bits & 0x1:
                raise ExtractError("压缩包已加密，无法解压")
            if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                skipped.append(name)
                continue
            with archive.open(info) as handle:
                data = handle.read(MAX_ARCHIVE_FILE_BYTES + 1)
            if len(data) > MAX_ARCHIVE_FILE_BYTES:
                skipped.append(name)
                continue
            total += len(data)
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ExtractError(
                    f"压缩包解压后超过 {MAX_ARCHIVE_TOTAL_BYTES // (1024 * 1024)}MB，已停止解析"
                )
            members.append((name, data))
    return members, skipped


def _collect_7z(path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    import py7zr

    try:
        with py7zr.SevenZipFile(path, mode="r") as archive:
            if archive.needs_password():
                raise ExtractError("压缩包已加密，无法解压")
            infos = [info for info in archive.list() if not info.is_directory]
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ExtractError(f"压缩包条目超过 {MAX_ARCHIVE_ENTRIES}，已拒绝解析")
            selected: list[tuple[str, str]] = []
            skipped: list[str] = []
            declared_total = 0
            for info in infos:
                name = _safe_member_name(info.filename)
                if not name or info.is_symlink:
                    continue
                if (info.uncompressed or 0) > MAX_ARCHIVE_FILE_BYTES:
                    skipped.append(name)
                    continue
                declared_total += info.uncompressed or 0
                if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ExtractError(
                        f"压缩包解压后超过 {MAX_ARCHIVE_TOTAL_BYTES // (1024 * 1024)}MB，已停止解析"
                    )
                selected.append((info.filename, name))
            with tempfile.TemporaryDirectory(prefix="shiyuan-7z-") as tmpdir:
                if selected:
                    archive.extract(path=tmpdir, targets=[raw for raw, _ in selected])
                root = Path(tmpdir).resolve()
                members: list[tuple[str, bytes]] = []
                total = 0
                for raw, name in selected:
                    target = (root / raw).resolve()
                    if target != root and root not in target.parents:
                        continue
                    if not target.is_file():
                        continue
                    if target.stat().st_size > MAX_ARCHIVE_FILE_BYTES:
                        skipped.append(name)
                        continue
                    data = target.read_bytes()
                    total += len(data)
                    if total > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ExtractError(
                            f"压缩包解压后超过 {MAX_ARCHIVE_TOTAL_BYTES // (1024 * 1024)}MB，已停止解析"
                        )
                    members.append((name, data))
            return members, skipped
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(f"7Z 解析失败：{exc}") from exc


def _collect_rar(path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    import rarfile

    tool = shutil.which("bsdtar") or shutil.which("tar")
    if tool:
        rarfile.BSDTAR_TOOL = tool
        rarfile.CURRENT_SETUP = None
    try:
        with rarfile.RarFile(str(path)) as archive:
            if archive.needs_password():
                raise ExtractError("压缩包已加密，无法解压")
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ExtractError(f"压缩包条目超过 {MAX_ARCHIVE_ENTRIES}，已拒绝解析")
            members: list[tuple[str, bytes]] = []
            skipped: list[str] = []
            total = 0
            for info in infos:
                name = _safe_member_name(info.filename)
                if not name:
                    continue
                if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                    skipped.append(name)
                    continue
                with archive.open(info) as handle:
                    data = handle.read(MAX_ARCHIVE_FILE_BYTES + 1)
                if len(data) > MAX_ARCHIVE_FILE_BYTES:
                    skipped.append(name)
                    continue
                total += len(data)
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ExtractError(
                        f"压缩包解压后超过 {MAX_ARCHIVE_TOTAL_BYTES // (1024 * 1024)}MB，已停止解析"
                    )
                members.append((name, data))
            return members, skipped
    except ExtractError:
        raise
    except rarfile.RarCannotExec as exc:
        raise ExtractError("未找到可用的 RAR 解压工具（需要 unrar 或 bsdtar）") from exc
    except Exception as exc:
        raise ExtractError(f"RAR 解析失败：{exc}") from exc


def _extract_blob(name: str, data: bytes, tempdir: Path, *, depth: int,
                  prefix: str, seen: set[str]) -> list[Section]:
    """解析压缩包/邮件里的一段字节；返回带来源标记的 Section。"""
    suffix = Path(name).suffix.lower().lstrip(".")
    if not suffix:
        return []
    if suffix in ARCHIVE_EXTS:
        if depth >= MAX_ARCHIVE_DEPTH:
            return [Section(text=f"[未解析] {prefix}{name}：嵌套超过 {MAX_ARCHIVE_DEPTH} 层")]
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            return []
        seen.add(digest)
        nested = tempdir / f"nested_{digest}.{suffix}"
        nested.write_bytes(data)
        return _read_archive(nested, suffix, depth=depth + 1,
                             prefix=f"{prefix}{name}/", seen=seen)
    if suffix not in SUPPORTED_EXTS:
        return []
    digest = hashlib.sha256(data).hexdigest()
    if digest in seen:
        return []
    seen.add(digest)
    item = tempdir / f"item_{digest}.{suffix}"
    item.write_bytes(data)
    try:
        sections = extract(item, suffix)
    except UnsupportedType:
        return []
    except Exception as exc:
        return [Section(text=f"[未解析] {prefix}{name}：{exc}")]
    return [
        Section(text=f"【{prefix}{name}】\n{section.text}", page=section.page,
                sheet=section.sheet)
        for section in sections
    ]


def _read_archive(path: str | Path, ext: str, depth: int = 0,
                  prefix: str = "", seen: set[str] | None = None) -> list[Section]:
    seen = seen if seen is not None else set()
    suffix = ext.lower().lstrip(".")
    if suffix == "zip":
        members, skipped = _collect_zip(Path(path))
    elif suffix == "7z":
        members, skipped = _collect_7z(Path(path))
    elif suffix == "rar":
        members, skipped = _collect_rar(Path(path))
    else:
        raise UnsupportedType(suffix)

    sections: list[Section] = [
        Section(text=f"[未解析] {prefix}{name}：超过单文件大小上限") for name in skipped
    ]
    parsed = 0
    with tempfile.TemporaryDirectory(prefix="shiyuan-archive-") as tmpdir:
        for name, data in members:
            inner_ext = Path(name).suffix.lower().lstrip(".")
            if inner_ext not in SUPPORTED_EXTS and inner_ext not in ARCHIVE_EXTS:
                continue
            if inner_ext not in ARCHIVE_EXTS:
                if parsed >= MAX_ARCHIVE_PARSE_ENTRIES:
                    sections.append(Section(
                        text=f"[未解析] {prefix}{name}：单包最多解析 {MAX_ARCHIVE_PARSE_ENTRIES} 个文件"
                    ))
                    continue
                parsed += 1
            sections.extend(_extract_blob(
                name, data, Path(tmpdir), depth=depth, prefix=prefix, seen=seen
            ))
    return sections


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skipping += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skipping:
            self.skipping -= 1

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _HtmlText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def _part_text(part) -> str:
    try:
        content = part.get_content()
    except Exception:
        content = _decode(part.get_payload(decode=True) or b"")
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        return _html_to_text(content)
    return content


def _mail_attachment_sections(part, tempdir: Path, *, prefix: str,
                              seen: set[str]) -> list[Section]:
    filename = part.get_filename() or "附件"
    data = part.get_payload(decode=True) or b""
    if len(data) > MAX_ARCHIVE_FILE_BYTES:
        return [Section(text=f"{prefix}[未解析] 附件 {filename}：超过 64MB")]
    return _extract_blob(
        f"附件/{filename}", data, tempdir, depth=0, prefix="", seen=seen
    )


def _email_sections(message, tempdir: Path, *, depth: int,
                    prefix: str, seen: set[str]) -> list[Section]:
    sections: list[Section] = []
    headers = []
    for label, key in (("主题", "Subject"), ("发件人", "From"),
                       ("收件人", "To"), ("抄送", "Cc"), ("时间", "Date")):
        value = message.get(key)
        if value:
            headers.append(f"{label}：{value}")
    plain: list[str] = []
    html: list[str] = []
    attachments = []
    nested = []

    def walk(part) -> None:
        content_type = part.get_content_type()
        if content_type == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                payload = payload[0] if payload else None
            if payload is not None:
                nested.append(payload)
            return
        if part.is_multipart():
            for child in part.iter_parts():
                walk(child)
            return
        filename = part.get_filename()
        if part.get_content_disposition() == "attachment" or filename:
            attachments.append(part)
            return
        if content_type == "text/plain":
            plain.append(_part_text(part))
        elif content_type == "text/html":
            html.append(_part_text(part))

    walk(message)
    body = "\n".join(text for text in plain if text.strip())
    if not body.strip():
        body = "\n".join(text for text in html if text.strip())
    head = "\n".join(headers)
    text = "\n".join(part for part in (head, body.strip()) if part.strip())
    if text.strip():
        sections.append(Section(text=f"{prefix}{text}"))

    for part in attachments[:MAX_MAIL_ATTACHMENTS]:
        sections.extend(_mail_attachment_sections(part, tempdir, prefix=prefix, seen=seen))
    if len(attachments) > MAX_MAIL_ATTACHMENTS:
        sections.append(Section(text=f"{prefix}[未解析] 其余 {len(attachments) - MAX_MAIL_ATTACHMENTS} 个附件未处理"))

    for item in nested:
        if depth >= MAX_MAIL_DEPTH:
            sections.append(Section(text=f"{prefix}[未解析] 嵌套邮件超过 {MAX_MAIL_DEPTH} 层"))
            continue
        sections.extend(_email_sections(
            item, tempdir, depth=depth + 1,
            prefix=f"{prefix}[嵌套邮件] ", seen=seen,
        ))
    return sections


def _read_eml(path: Path) -> list[Section]:
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    with tempfile.TemporaryDirectory(prefix="shiyuan-mail-") as tmpdir:
        return _email_sections(message, Path(tmpdir), depth=0, prefix="", seen=set())


def _msg_sections(message, tempdir: Path, *, depth: int,
                  prefix: str, seen: set[str]) -> list[Section]:
    sections: list[Section] = []
    headers = []
    for label, key in (("主题", "subject"), ("发件人", "sender"),
                       ("收件人", "to"), ("抄送", "cc"), ("时间", "date")):
        value = getattr(message, key, None)
        if value:
            headers.append(f"{label}：{value}")
    body = getattr(message, "body", "") or ""
    if not str(body).strip():
        html = getattr(message, "htmlBody", None)
        if isinstance(html, bytes):
            html = _decode(html)
        if html:
            body = _html_to_text(str(html))
    head = "\n".join(headers)
    text = "\n".join(part for part in (head, str(body).strip()) if part.strip())
    if text.strip():
        sections.append(Section(text=f"{prefix}{text}"))

    attachments = list(getattr(message, "attachments", None) or [])
    for attachment in attachments[:MAX_MAIL_ATTACHMENTS]:
        filename = (getattr(attachment, "longFilename", None)
                    or getattr(attachment, "shortFilename", None) or "附件")
        try:
            data = attachment.data
        except Exception:
            continue
        if hasattr(data, "subject"):  # 内嵌邮件
            if depth >= MAX_MAIL_DEPTH:
                sections.append(Section(text=f"{prefix}[未解析] 嵌套邮件超过 {MAX_MAIL_DEPTH} 层"))
                continue
            sections.extend(_msg_sections(
                data, tempdir, depth=depth + 1,
                prefix=f"{prefix}[嵌套邮件] ", seen=seen,
            ))
        elif isinstance(data, bytes):
            if len(data) > MAX_ARCHIVE_FILE_BYTES:
                sections.append(Section(text=f"{prefix}[未解析] 附件 {filename}：超过 64MB"))
                continue
            sections.extend(_extract_blob(
                f"附件/{filename}", data, tempdir, depth=0, prefix="", seen=seen
            ))
    if len(attachments) > MAX_MAIL_ATTACHMENTS:
        sections.append(Section(
            text=f"{prefix}[未解析] 其余 {len(attachments) - MAX_MAIL_ATTACHMENTS} 个附件未处理"
        ))
    return sections


def _read_msg(path: Path) -> list[Section]:
    import extract_msg

    message = extract_msg.openMsg(str(path))
    try:
        with tempfile.TemporaryDirectory(prefix="shiyuan-msg-") as tmpdir:
            return _msg_sections(message, Path(tmpdir), depth=0, prefix="", seen=set())
    finally:
        try:
            message.close()
        except Exception:
            pass


def _dxf_entity_lines(entities, prefix: str) -> list[str]:
    lines: list[str] = []
    for entity in entities:
        try:
            kind = entity.dxftype()
            layer = entity.dxf.get("layer", "0")
            if kind == "TEXT":
                text = entity.dxf.get("text", "")
            elif kind == "MTEXT":
                text = entity.plain_text()
            elif kind == "ATTRIB":
                text = f"{entity.dxf.get('tag', '')} {entity.dxf.get('text', '')}"
            elif kind == "DIMENSION":
                text = entity.dxf.get("text", "")
                if text == "<>":
                    text = ""
            elif kind == "INSERT":
                for attrib in entity.attribs:
                    value = attrib.dxf.get("text", "").strip()
                    if value:
                        lines.append(
                            f"{prefix}[{layer}] {attrib.dxf.get('tag', '')} {value}"
                        )
                continue
            else:
                continue
        except Exception:
            continue
        if text and text.strip():
            lines.append(f"{prefix}[{layer}] {text.strip()}")
    return lines


def _read_dxf(path: Path) -> list[Section]:
    import ezdxf

    try:
        document = ezdxf.readfile(str(path))
    except Exception as exc:
        raise ExtractError(f"DXF 解析失败：{exc}") from exc
    lines: list[str] = []
    version = document.header.get("$ACADVER", "")
    if version:
        lines.append(f"版本：{version}")
    lines.extend(_dxf_entity_lines(document.modelspace(), "[模型] "))
    for layout in document.layouts:
        if layout.name.lower() == "model":
            continue
        lines.extend(_dxf_entity_lines(layout, f"[布局 {layout.name}] "))
    for block in document.blocks:
        if block.name.startswith("*"):
            continue
        lines.extend(_dxf_entity_lines(block, f"[块 {block.name}] "))
    unique = list(dict.fromkeys(line for line in lines if line.strip()))
    return [Section(text="\n".join(unique))] if unique else []


def _read_dwg(path: Path) -> list[Section]:
    converter = os.environ.get("SHIYUAN_ODA_CONVERTER")
    if not converter or not Path(converter).exists():
        converter = next(
            (candidate for candidate in (
                r"C:\Program Files\ODA\ODAFileConverter\ODAFileConverter.exe",
                r"C:\Program Files (x86)\ODA\ODAFileConverter\ODAFileConverter.exe",
            ) if Path(candidate).exists()),
            None,
        )
    if not converter:
        raise ExtractError(
            "DWG 需要 ODA File Converter；安装后设置 SHIYUAN_ODA_CONVERTER 即可自动转换"
        )
    with tempfile.TemporaryDirectory(prefix="shiyuan-dwg-") as tmpdir:
        input_dir = Path(tmpdir) / "in"
        output_dir = Path(tmpdir) / "out"
        input_dir.mkdir()
        output_dir.mkdir()
        shutil.copy2(path, input_dir / path.name)
        command = [
            converter, str(input_dir), str(output_dir),
            "ACAD2018", "DXF", "0", "1", "*.dwg",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180, creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractError("DWG 转换超时（超过 180 秒）") from exc
        output = output_dir / f"{path.stem}.dxf"
        if completed.returncode != 0 or not output.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ExtractError(f"DWG 转换失败：{detail[:300] or '未生成 DXF'}")
        return _read_dxf(output)


TAG_LABELS = {
    "title": "标题", "artist": "作者", "author": "作者", "album": "专辑",
    "date": "日期", "comment": "备注", "genre": "类型", "tracknumber": "音轨",
}


def _read_media(path: Path) -> list[Section]:
    from mutagen import File as MutagenFile

    try:
        media = MutagenFile(str(path), easy=True)
    except Exception as exc:
        raise ExtractError(f"无法读取音视频元数据：{exc}") from exc
    if media is None:
        raise ExtractError("暂不支持该音视频容器")
    lines = [f"格式：{type(media).__name__}"]
    info = getattr(media, "info", None)
    if info is not None:
        length = getattr(info, "length", None)
        if length:
            lines.append(f"时长：{length:.1f} 秒")
        bitrate = getattr(info, "bitrate", None)
        if bitrate:
            lines.append(f"码率：{bitrate // 1000} kbps")
        sample_rate = getattr(info, "sample_rate", None)
        if sample_rate:
            lines.append(f"采样率：{sample_rate} Hz")
        width = getattr(info, "width", None)
        height = getattr(info, "height", None)
        if width and height:
            lines.append(f"分辨率：{width}x{height}")
    tags = getattr(media, "tags", None)
    if tags:
        for key in sorted(tags.keys(), key=str):
            label = TAG_LABELS.get(str(key).lower(), str(key))
            value = tags[key]
            text = "；".join(str(item) for item in value) if isinstance(
                value, (list, tuple)
            ) else str(value)
            if text.strip():
                lines.append(f"{label}：{text.strip()}")
    return [Section(text="\n".join(lines))]


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("\n", " ")


def _protected_sheet_names(path: Path) -> set[str]:
    """从 xlsx 的 sheet XML 读取受保护工作表；read_only 模式下 openpyxl 不暴露 protection。"""
    from xml.etree import ElementTree as ET

    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                rel.attrib.get("Id"): rel.attrib.get("Target")
                for rel in rels
            }
            protected: set[str] = set()
            for sheet in workbook.findall(f"{main_ns}sheets/{main_ns}sheet"):
                rid = sheet.attrib.get(rel_ns)
                target = targets.get(rid)
                if not target:
                    continue
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = f"xl/{target}"
                try:
                    payload = archive.read(target)
                except KeyError:
                    continue
                if b"<sheetProtection" in payload:
                    name = sheet.attrib.get("name")
                    if name:
                        protected.add(name)
            return protected
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
        return set()


def _read_sheet(path: Path) -> list[Section]:
    """同时读取计算值与公式原文；公式单元格输出为“值〔公式：...〕”。"""
    import openpyxl

    protected = _protected_sheet_names(path)
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        try:
            formulas = openpyxl.load_workbook(str(path), read_only=True, data_only=False)
        except Exception:
            formulas = None
        try:
            sections = []
            for sheet in workbook.worksheets:
                formula_sheet = None
                if formulas is not None:
                    try:
                        formula_sheet = formulas[sheet.title]
                    except KeyError:
                        formula_sheet = None
                formula_rows = (
                    formula_sheet.iter_rows(values_only=True) if formula_sheet is not None
                    else iter(())
                )
                lines = []
                if sheet.title in protected:
                    lines.append("[工作表受保护，当前仅建立只读索引]")
                rows = []
                for row in sheet.iter_rows(values_only=True):
                    formula_row = next(formula_rows, ())
                    cells = []
                    for index, value in enumerate(row):
                        text = _cell_text(value)
                        formula = _cell_text(
                            formula_row[index] if index < len(formula_row) else None
                        )
                        if formula.startswith("="):
                            if text and text != formula:
                                text = f"{text}〔公式：{formula}〕"
                            else:
                                text = f"〔公式：{formula}〕"
                        cells.append(text)
                    if any(cells):
                        rows.append(cells)
                if rows:
                    if lines:
                        lines.append("")
                    lines.extend(_md_table(rows))
                sections.append(Section(text="\n".join(lines), sheet=sheet.title))
            return sections
        finally:
            if formulas is not None:
                formulas.close()
    finally:
        workbook.close()


def pdf_page_count(path: str | Path) -> int:
    """PDF 总页数；用于判断哪些页缺少文本层。打开失败时返回 0。"""
    try:
        import fitz

        with fitz.open(str(path)) as document:
            return document.page_count
    except Exception:
        pass
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def extract(path: str | Path, ext: str) -> list[Section]:
    """返回按页 / 工作表切分的文本；空文本表示"无文本层"而非"无内容"。"""
    target = Path(path)
    suffix = ext.lower().lstrip(".")
    if suffix not in SUPPORTED_EXTS:
        raise UnsupportedType(suffix)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise ExtractError(f"无法读取文件：{exc}") from exc
    if suffix in MEDIA_EXTS:
        if size > MAX_MEDIA_BYTES:
            raise ExtractError(f"音视频超过 {MAX_MEDIA_BYTES // (1024 ** 3)}GB，跳过解析")
    elif size > MAX_FILE_BYTES and suffix not in TEXT_EXTS:
        raise ExtractError(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB，首版跳过解析")

    if suffix in TEXT_EXTS:
        sections = _read_text(target)
    elif suffix in PDF_EXTS:
        sections = _read_pdf(target)
    elif suffix in DOCX_EXTS:
        sections = _read_docx(target)
    elif suffix in PPTX_EXTS:
        sections = _read_pptx(target)
    elif suffix in LEGACY_EXTS:
        sections = _read_legacy(target, LEGACY_EXTS[suffix])
    elif suffix in ARCHIVE_EXTS:
        sections = _read_archive(target, suffix)
    elif suffix in EMAIL_EXTS:
        # mbox 是 RFC 822 文本归档，与 eml 同一套解析；只有 msg 走 Outlook 容器。
        sections = _read_eml(target) if suffix in {"eml", "mbox"} else _read_msg(target)
    elif suffix in CAD_EXTS:
        sections = _read_dxf(target) if suffix == "dxf" else _read_dwg(target)
    elif suffix in MEDIA_EXTS:
        sections = _read_media(target)
    else:
        sections = _read_sheet(target)

    total = 0
    limited = []
    for section in sections:
        text = section.text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            continue
        remaining = MAX_CHARS - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        total += len(text)
        limited.append(Section(text=text, page=section.page, sheet=section.sheet))
    return limited


def quality_of(sections: list[Section]) -> str:
    text = "".join(section.text for section in sections)
    if not text.strip():
        return "empty"
    # 乱码页常常替换字符占比很高；这只是启发式，后续应由用户反馈进一步校准。
    if text.count("\ufffd") > max(20, len(text) * 0.05):
        return "low"
    return "good"
