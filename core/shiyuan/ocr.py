"""L2 深度解析：调用 Windows 本机 OCR 识别图片与无文本层 PDF。

只使用系统自带的 OCR（Windows.Media.Ocr），不联网、不打包第三方模型；
语言以系统已安装的 OCR 语言包为准，中文环境通常为 zh-Hans-CN。
识别结果只是"另一路文本来源"，按行保留像素坐标（x/y/w/h），PDF 页面定位到页码；
图片没有页码，但同样保留行坐标。混合版式 PDF 可以只识别缺失文本层的页。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"}
OCR_EXTS = IMAGE_EXTS | {"pdf"}
MAX_PDF_PAGES = 30
MAX_PPTX_IMAGES = 20
MAX_PPTX_IMAGE_BYTES = 20 * 1024 * 1024

PREFERRED_LANGUAGES = ("zh-hans", "zh", "en-us", "en")

SETTINGS_KEY = "ocr"
POLICIES = {"auto", "windows", "rapidocr"}
DEFAULT_SETTINGS = {"policy": "auto"}
ENGINE_LABELS = {
    "windows": "Windows OCR",
    "rapidocr": "PP-OCRv6 small",
}

_CJK = r"\u4e00-\u9fff"

_ENGINE_CACHE: tuple[float, dict] | None = None
_RAPID_AVAILABLE: bool | None = None
_RAPID_ENGINE = None
_RAPID_LOCK = threading.Lock()


@dataclass
class Section:
    text: str
    page: int | None = None
    sheet: str | None = None
    boxes: list[list[int]] | None = None  # 与 text 的分行一一对应：[x, y, w, h]


class OcrUnavailable(RuntimeError):
    """系统没有可用的 OCR 引擎或语言包。"""


def _language_tags() -> list[str]:
    from winsdk.windows.media.ocr import OcrEngine

    return [str(language.language_tag) for language in OcrEngine.available_recognizer_languages]


def rapidocr_available() -> bool:
    """RapidOCR/PP-OCRv6 是否随包可用；不可用时自动回退 Windows OCR。"""
    global _RAPID_AVAILABLE
    if _RAPID_AVAILABLE is None:
        try:
            import rapidocr  # noqa: F401
        except Exception:
            _RAPID_AVAILABLE = False
        else:
            _RAPID_AVAILABLE = True
    return _RAPID_AVAILABLE


def _windows_info() -> dict:
    global _ENGINE_CACHE
    now = time.monotonic()
    if _ENGINE_CACHE is not None and now - _ENGINE_CACHE[0] < 30:
        return _ENGINE_CACHE[1]
    try:
        tags = _language_tags()
    except Exception as exc:  # winsdk 缺失、系统版本过低等
        info = {"available": False, "engine": "windows-ocr", "label": ENGINE_LABELS["windows"],
                "languages": [], "message": f"Windows OCR 不可用：{exc}"}
    else:
        info = {
            "available": bool(tags),
            "engine": "windows-ocr",
            "label": ENGINE_LABELS["windows"],
            "languages": tags,
            "message": None if tags else "系统未安装 OCR 语言包（设置 → 时间和语言 → 语言 → 可选功能）",
        }
    _ENGINE_CACHE = (now, info)
    return info


def engine_info(engine: str = "windows") -> dict:
    if engine == "rapidocr":
        available = rapidocr_available()
        return {
            "available": available,
            "engine": "rapidocr",
            "label": ENGINE_LABELS["rapidocr"],
            "languages": ["zh-Hans", "en"] if available else [],
            "message": None if available else "未安装 RapidOCR（需要 rapidocr + onnxruntime 与 PP-OCRv6 模型）",
        }
    return _windows_info()


# ---------------------------------------------------------------- 策略设置

def load_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict) and stored.get("policy") in POLICIES:
            settings["policy"] = stored["policy"]
    return settings


def public_settings(conn: sqlite3.Connection) -> dict:
    settings = load_settings(conn)
    return {
        "policy": settings["policy"],
        "engines": [engine_info("windows"), engine_info("rapidocr")],
    }


def update_settings(conn: sqlite3.Connection, payload: dict) -> dict:
    policy = payload.get("policy")
    if policy is not None:
        if policy not in POLICIES:
            raise ValueError("未知的 OCR 策略")
        if policy == "rapidocr" and not rapidocr_available():
            raise ValueError("RapidOCR 不可用：未安装 rapidocr 或模型缺失")
    settings = load_settings(conn)
    if policy is not None:
        settings["policy"] = policy
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(settings, ensure_ascii=False)),
    )
    conn.commit()
    return public_settings(conn)


def plan_engines(conn: sqlite3.Connection) -> dict:
    """返回策略与可用性，供 indexer 做按文件路由。"""
    settings = load_settings(conn)
    return {
        "policy": settings["policy"],
        "windows": engine_info()["available"],
        "rapidocr": rapidocr_available(),
    }


def _create_engine():
    from winsdk.windows.globalization import Language
    from winsdk.windows.media.ocr import OcrEngine

    tags = _language_tags()
    for preferred in PREFERRED_LANGUAGES:
        for tag in tags:
            if tag.lower().startswith(preferred):
                engine = OcrEngine.try_create_from_language(Language(tag))
                if engine is not None:
                    return engine
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrUnavailable("系统未安装可用的 OCR 语言包")
    return engine


def normalize(text: str) -> str:
    """OCR 常在汉字之间、数字与指数之间插入空格，检索前先收紧。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", text)
    text = re.sub(r"(?<=\d)\s+(?=[eE][+-]?\d)", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _line_box(line) -> list[int] | None:
    rects = [word.bounding_rect for word in line.words]
    if not rects:
        return None
    left = min(rect.x for rect in rects)
    top = min(rect.y for rect in rects)
    right = max(rect.x + rect.width for rect in rects)
    bottom = max(rect.y + rect.height for rect in rects)
    return [int(round(left)), int(round(top)),
            int(round(right - left)), int(round(bottom - top))]


def _section(result, page: int | None) -> Section | None:
    """把识别结果转成「按行文本 + 行坐标」；坐标只在本次识别内有效。"""
    lines: list[str] = []
    boxes: list[list[int]] = []
    for line in result.lines:
        text = normalize(line.text)
        if not text:
            continue
        box = _line_box(line)
        lines.append(text)
        boxes.append(box if box is not None else [0, 0, 0, 0])
    text = "\n".join(lines)
    if not text.strip():
        return None
    return Section(text=text, page=page, boxes=boxes)


async def _recognize_image(engine, path: Path) -> Section | None:
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile

    file = await StorageFile.get_file_from_path_async(str(path))
    stream = await file.open_async(0)  # FileAccessMode.Read
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return _section(result, None)


async def _recognize_pdf(engine, path: Path, max_pages: int,
                         skip_pages: set[int] | None = None) -> list[Section]:
    from winsdk.windows.data.pdf import PdfDocument
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream

    skip = skip_pages or set()
    file = await StorageFile.get_file_from_path_async(str(path))
    document = await PdfDocument.load_from_file_async(file)
    sections: list[Section] = []
    for index in range(min(document.page_count, max_pages)):
        page_number = index + 1
        if page_number in skip:
            continue
        page = document.get_page(index)
        try:
            stream = InMemoryRandomAccessStream()
            await page.render_to_stream_async(stream)
            stream.seek(0)
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            result = await engine.recognize_async(bitmap)
            section = _section(result, page_number)
            if section is not None:
                sections.append(section)
        finally:
            page.close()
    return sections


def extract_image(path: str | Path) -> list[Section]:
    engine = _create_engine()
    section = asyncio.run(_recognize_image(engine, Path(path)))
    return [section] if section is not None else []


def extract_pdf(path: str | Path, max_pages: int = MAX_PDF_PAGES,
                skip_pages: set[int] | None = None) -> list[Section]:
    engine = _create_engine()
    return asyncio.run(_recognize_pdf(engine, Path(path), max_pages, skip_pages))


# ---------------------------------------------------------------- RapidOCR

def _rapid_engine():
    global _RAPID_ENGINE
    if _RAPID_ENGINE is None:
        from rapidocr import RapidOCR

        _RAPID_ENGINE = RapidOCR()
    return _RAPID_ENGINE


def _run_rapid(path: str | Path):
    with _RAPID_LOCK:
        return _rapid_engine()(str(path))


def order_rapid_items(items: list[tuple[str, list]]) -> list[tuple[str, list]]:
    """把 RapidOCR 的行按阅读顺序排序。

    RapidOCR 对倾斜长行可能拆成多段并按视觉位置返回；这里用所有行的平均角度
    把坐标旋转到“水平”坐标系，再按 y'、x' 排序，避免右段排到左段前面。
    """
    if not items:
        return []
    sum_dx = sum_dy = 0.0
    for _, poly in items:
        (x0, y0), (x1, y1) = poly[0], poly[1]
        sum_dx += x1 - x0
        sum_dy += y1 - y0
    theta = math.atan2(sum_dy, sum_dx) if (sum_dx or sum_dy) else 0.0
    sin_t, cos_t = math.sin(theta), math.cos(theta)

    decorated = []
    for text, poly in items:
        cx = sum(point[0] for point in poly) / len(poly)
        cy = sum(point[1] for point in poly) / len(poly)
        x_rot = cx * cos_t + cy * sin_t
        y_rot = -cx * sin_t + cy * cos_t
        decorated.append((y_rot, x_rot, text, poly))
    decorated.sort(key=lambda item: (item[0], item[1]))

    # 同一视觉行可能被拆成多段，y' 只差几个像素；先按基线聚类，再在行内按 x' 排序。
    grouped: list[list[tuple[float, float, str, list]]] = []
    for item in decorated:
        if grouped:
            mean_y = sum(entry[0] for entry in grouped[-1]) / len(grouped[-1])
        if not grouped or abs(item[0] - mean_y) > 8.0:
            grouped.append([item])
        else:
            grouped[-1].append(item)
    ordered = []
    for group in grouped:
        group.sort(key=lambda item: item[1])
        ordered.extend((text, poly) for _, _, text, poly in group)
    return ordered


def _poly_bbox(poly: list) -> list[int]:
    xs = [point[0] for point in poly]
    ys = [point[1] for point in poly]
    left, top = min(xs), min(ys)
    return [int(round(left)), int(round(top)),
            int(round(max(xs) - left)), int(round(max(ys) - top))]


def _rapid_section(result, page: int | None) -> Section | None:
    texts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    if texts is None or boxes is None:
        return None
    items: list[tuple[str, list]] = []
    for text, poly in zip(list(texts), list(boxes)):
        value = normalize(str(text))
        if value and len(poly):
            items.append((value, poly))
    ordered = order_rapid_items(items)
    if not ordered:
        return None
    return Section(
        text="\n".join(text for text, _ in ordered),
        page=page,
        boxes=[_poly_bbox(poly) for _, poly in ordered],
    )


def extract_image_rapid(path: str | Path) -> list[Section]:
    section = _rapid_section(_run_rapid(Path(path)), None)
    return [section] if section is not None else []


def extract_pdf_rapid(path: str | Path, max_pages: int = MAX_PDF_PAGES,
                      skip_pages: set[int] | None = None) -> list[Section]:
    import pypdfium2 as pdfium

    skip = skip_pages or set()
    document = pdfium.PdfDocument(str(path))
    sections: list[Section] = []
    try:
        for index in range(min(len(document), max_pages)):
            page_number = index + 1
            if page_number in skip:
                continue
            page = document[index]
            try:
                image = page.render(scale=2.0).to_pil()
            finally:
                page.close()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                temp_path = Path(handle.name)
            try:
                image.save(temp_path)
                section = _rapid_section(_run_rapid(temp_path), page_number)
                if section is not None:
                    sections.append(section)
            finally:
                temp_path.unlink(missing_ok=True)
    finally:
        document.close()
    return sections


def _iter_picture_shapes(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_picture_shapes(shape.shapes)
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            yield shape


def extract_pptx(path: str | Path, rapid: bool = False) -> list[Section]:
    """识别 PPTX 内嵌图片中的文字，页码对应幻灯片序号。"""
    from pptx import Presentation

    presentation = Presentation(str(path))
    sections: list[Section] = []
    used = 0
    for index, slide in enumerate(presentation.slides, start=1):
        texts: list[str] = []
        for shape in _iter_picture_shapes(slide.shapes):
            if used >= MAX_PPTX_IMAGES:
                break
            try:
                image = shape.image
                ext = (image.ext or "").lower()
                blob = image.blob
            except Exception:
                continue
            if ext not in IMAGE_EXTS or len(blob) > MAX_PPTX_IMAGE_BYTES:
                continue
            used += 1
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(blob)
            try:
                result = extract_image_rapid(temp_path) if rapid else extract_image(temp_path)
            except OcrUnavailable:
                raise
            except Exception:
                result = []
            finally:
                temp_path.unlink(missing_ok=True)
            text = "\n".join(section.text for section in result if section.text.strip()).strip()
            if text:
                texts.append(text)
        if texts:
            sections.append(Section(text="\n".join(texts), page=index))
    return sections


def quality_of(sections: list[Section]) -> str:
    text = "".join(section.text for section in sections)
    if not text.strip():
        return "empty"
    # OCR 结果里常见大量单字空格或替换字符；这里只做粗粒度判断。
    if text.count("\ufffd") > max(10, len(text) * 0.05):
        return "low"
    return "good"
