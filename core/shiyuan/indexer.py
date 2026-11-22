"""L1 索引流水线：把当前内容版本解析成 chunks 并写入 FTS。

缓存键是 version_id + parser_signature：内容没变就复用产物，解析器升级或
文件变化才重做。解析结果按版本清理，旧版本的块不会继续参与检索。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import budget, db, extractors, ocr, store

# 解析器与切块策略的版本签名；改动提取逻辑或分块参数时必须同步升级。
PARSER_SIGNATURE = (
    "l1.v4:pdf=pymupdf+fallback,docx=python-docx+header+comment,"
    "pptx=python-pptx,xlsx=openpyxl+formula,office=libreoffice,"
    "archive=zip/7z/rar,email=eml/msg,cad=dxf/dwg,av=metadata,chunk=800/100"
)
OCR_SIGNATURES = {
    "windows": f"l2.v2:windows-ocr:maxpages={ocr.MAX_PDF_PAGES}:mixed=pages",
    "rapidocr": f"l2.v2:rapidocr-v6-small:maxpages={ocr.MAX_PDF_PAGES}:mixed=pages",
}
# PPTX 内嵌图片单独签名，避免升级后把已有 PDF / 图片 OCR 结果全部作废。
PPTX_OCR_SIGNATURES = {
    "windows": "l2.v1:windows-ocr:pptx=images",
    "rapidocr": "l2.v1:rapidocr-v6-small:pptx=images",
}
OCR_SIGNATURE = OCR_SIGNATURES["windows"]

OCR_BATCH = 50
OCR_RUN_LIMIT = 200
MAX_ATTEMPTS = 3

CHUNK_CHARS = 800
CHUNK_OVERLAP = 100


@dataclass
class Chunk:
    text: str
    page: int | None
    sheet: str | None
    start: int
    end: int
    boxes: list[list[int]] | None = None


def chunk_sections(sections) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in sections:
        text = section.text
        line_boxes: list[tuple[int, int, list[int]]] = []
        boxes = getattr(section, "boxes", None)
        if boxes:
            offset = 0
            for index, line in enumerate(text.split("\n")):
                if index < len(boxes):
                    line_boxes.append((offset, offset + len(line), boxes[index]))
                offset += len(line) + 1
        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            if end < len(text):
                cut = text.rfind("\n", start + CHUNK_CHARS // 2, end)
                if cut > 0:
                    end = cut + 1
            piece = text[start:end]
            if piece.strip():
                keep = [box for (box_start, box_end, box) in line_boxes
                        if box_end > start and box_start < end]
                chunks.append(Chunk(piece, section.page, section.sheet, start, end,
                                    keep or None))
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _purge_old_versions(conn: sqlite3.Connection, asset_id: str, keep_version: str) -> None:
    rows = conn.execute(
        "SELECT version_id FROM content_versions WHERE asset_id = ? AND version_id != ?",
        (asset_id, keep_version),
    ).fetchall()
    for row in rows:
        version_id = row["version_id"]
        conn.execute("DELETE FROM chunk_fts WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM chunks WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM parse_artifacts WHERE version_id = ?", (version_id,))


def _write_artifact(conn: sqlite3.Connection, row: sqlite3.Row, *, state: str, quality: str,
                    sections, message: str | None, chunks: list[Chunk],
                    signature: str = PARSER_SIGNATURE,
                    page_count: int = 0, text_pages: int = 0,
                    image_count: int = 0) -> str:
    artifact_id = store.new_id("art")
    char_count = sum(len(section.text) for section in sections)
    conn.execute(
        "INSERT INTO parse_artifacts (artifact_id, version_id, parser_signature, state, quality,"
        " char_count, page_count, text_pages, image_count, message, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact_id, row["version_id"], signature, state, quality, char_count,
         page_count, text_pages, image_count, message, store.now_iso()),
    )
    for ordinal, chunk in enumerate(chunks):
        chunk_id = store.new_id("chk")
        bbox = json.dumps(chunk.boxes, ensure_ascii=False) if chunk.boxes else None
        cursor = conn.execute(
            "INSERT INTO chunks (chunk_id, artifact_id, version_id, asset_id, ordinal, text,"
            " page, sheet, bbox, char_start, char_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, artifact_id, row["version_id"], row["asset_id"], ordinal, chunk.text,
             chunk.page, chunk.sheet, bbox, chunk.start, chunk.end),
        )
        conn.execute(
            "INSERT INTO chunk_fts (rowid, text, chunk_id, version_id) VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, chunk.text, chunk_id, row["version_id"]),
        )
    return state


def _job_update(conn: sqlite3.Connection, row: sqlite3.Row, stage: str, status: str,
                error: str | None = None, count_attempt: bool = False) -> None:
    """记录/更新任务账本；attempts 只在实际开始解析时加一。"""
    conn.execute(
        "INSERT INTO index_jobs (job_id, source_id, version_id, asset_id, stage, status,"
        " attempts, error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(version_id, stage) DO UPDATE SET"
        " status = excluded.status,"
        " attempts = index_jobs.attempts + excluded.attempts,"
        " error = excluded.error, updated_at = excluded.updated_at",
        (store.new_id("job"), row["source_id"], row["version_id"], row["asset_id"], stage,
         status, 1 if count_attempt else 0, error, store.now_iso()),
    )


def _requeue_failed(conn: sqlite3.Connection, stage: str, signature: str,
                    source_id: str | None = None) -> int:
    """清理未达重试上限的失败产物，让它们在本轮重新排队。"""
    sql = [
        "SELECT a.artifact_id FROM parse_artifacts a",
        "JOIN content_versions v ON v.version_id = a.version_id",
        "JOIN asset_locations l ON l.asset_id = v.asset_id",
        "LEFT JOIN index_jobs j ON j.version_id = a.version_id AND j.stage = ?",
        "WHERE a.parser_signature = ? AND a.state = 'failed' AND v.is_current = 1",
        "AND l.availability = 'AVAILABLE' AND COALESCE(j.attempts, 0) < ?",
    ]
    params: list = [stage, signature, MAX_ATTEMPTS]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    artifacts = [row["artifact_id"] for row in conn.execute(" ".join(sql), params)]
    for artifact_id in artifacts:
        conn.execute(
            "DELETE FROM chunk_fts WHERE rowid IN"
            " (SELECT rowid FROM chunks WHERE artifact_id = ?)", (artifact_id,))
        conn.execute("DELETE FROM chunks WHERE artifact_id = ?", (artifact_id,))
        conn.execute("DELETE FROM parse_artifacts WHERE artifact_id = ?", (artifact_id,))
    return len(artifacts)


def index_version(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """解析单个内容版本，返回 ok / empty / failed / skipped。"""
    existing = conn.execute(
        "SELECT state FROM parse_artifacts WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], PARSER_SIGNATURE),
    ).fetchone()
    if existing is not None:
        return existing["state"]
    if not extractors.is_supported(row["ext"]):
        return "skipped"

    _job_update(conn, row, "l1", "running", count_attempt=True)
    sections: list[extractors.Section] = []
    chunks: list[Chunk] = []
    page_count = 0
    text_pages = 0
    image_count = 0
    try:
        sections = extractors.extract(row["path"], row["ext"])
        quality = extractors.quality_of(sections)
        if row["ext"] == "pdf":
            page_count = extractors.pdf_page_count(row["path"])
            text_pages = len({section.page for section in sections if section.page})
        elif row["ext"] == "pptx":
            image_count = extractors.pptx_image_count(row["path"])
        if quality == "empty":
            state, message = "empty", "未提取到文本层（可能是扫描件或空文档）"
        else:
            state, message = "ok", None
            chunks = chunk_sections(sections)
    except extractors.UnsupportedType:
        return "skipped"
    except Exception as exc:
        state, quality, message = "failed", "empty", str(exc)[:400]

    _purge_old_versions(conn, row["asset_id"], row["version_id"])
    _write_artifact(conn, row, state=state, quality=quality, sections=sections,
                    message=message, chunks=chunks,
                    page_count=page_count, text_pages=text_pages,
                    image_count=image_count)
    _job_update(conn, row, "l1", "done" if state != "failed" else "failed", error=message)
    return state


def pending_rows(conn: sqlite3.Connection, source_id: str | None = None,
                 limit: int = 500) -> list[sqlite3.Row]:
    exts = sorted(extractors.SUPPORTED_EXTS)
    placeholders = ",".join("?" for _ in exts)
    sql = [
        "SELECT v.version_id, v.asset_id, l.path, l.ext, l.source_id",
        "FROM content_versions v",
        "JOIN asset_locations l ON l.asset_id = v.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        f"AND l.ext IN ({placeholders})",
        "AND NOT EXISTS (SELECT 1 FROM parse_artifacts a"
        " WHERE a.version_id = v.version_id AND a.parser_signature = ?)",
    ]
    params: list[str] = [*exts, PARSER_SIGNATURE]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    sql.append("ORDER BY v.observed_at DESC")
    sql.append(f"LIMIT {limit}")
    return conn.execute(" ".join(sql), params).fetchall()


def index_pending(conn: sqlite3.Connection, source_id: str | None = None,
                  batch: int = 200) -> dict:
    """按批处理待解析版本，受每日预算限制；超出的留给下一次运行。"""
    counters = {"indexed": 0, "empty": 0, "failed": 0, "budget_exhausted": False}
    failures: list[str] = []
    _requeue_failed(conn, "l1", PARSER_SIGNATURE, source_id)
    conn.commit()

    allow = budget.remaining(conn, "l1")
    processed = 0
    while processed < allow:
        rows = pending_rows(conn, source_id, min(batch, allow - processed))
        if not rows:
            break
        for row in rows:
            state = index_version(conn, row)
            counters[{"ok": "indexed"}.get(state, state)] += 1
            processed += 1
            if state == "failed":
                failures.append(f"{row['path']}: 解析失败")
        conn.commit()
    budget.add(conn, "l1", processed)
    if allow == 0 and pending_rows(conn, source_id, 1):
        counters["budget_exhausted"] = True

    if any(counters[key] for key in ("indexed", "empty", "failed")) or counters["budget_exhausted"]:
        store.log_event(
            conn, "index_completed", source_id,
            detail=json.dumps(counters, ensure_ascii=False), outcome="ok",
        )
        if failures:
            store.log_event(conn, "index_failed", source_id,
                            detail="; ".join(failures[:5]), outcome="partial")
        conn.commit()
    return counters


def index_pending_in_new_connection(source_id: str | None = None,
                                    store_path: str | None = None) -> dict:
    conn = db.connect(store_path)
    try:
        return index_pending(conn, source_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- L2 OCR

def _route_engine(ext: str, text_pages: int, plan: dict) -> str | None:
    """按策略选择 OCR 引擎；返回 None 表示当前文件没有可用引擎。"""
    if plan["policy"] == "windows":
        return "windows" if plan["windows"] else None
    if plan["policy"] == "rapidocr":
        if plan["rapidocr"]:
            return "rapidocr"
        return "windows" if plan["windows"] else None
    # auto：有 Windows 时，整份扫描 PDF 走 PP-OCRv6，其余走 Windows；
    # 没有 Windows 时全部交给随包的 PP-OCRv6 兜底。
    if plan["windows"]:
        if ext == "pdf" and (text_pages or 0) == 0 and plan["rapidocr"]:
            return "rapidocr"
        return "windows"
    return "rapidocr" if plan["rapidocr"] else None


def _ocr_plan_sql(conn: sqlite3.Connection) -> tuple[dict, str | None, str | None, str, list]:
    """返回（策略, 引擎表达式, 签名表达式, 候选文件条件, 参数）。

    候选条件只覆盖“OCR 有意义的文件”：图片，或文本层为空/质量低/有缺页的 PDF。
    签名表达式与引擎表达式使用同一套路由，保证待处理判断与执行引擎一致。
    """
    plan = ocr.plan_engines(conn)
    images = sorted(ocr.IMAGE_EXTS)
    placeholders = ",".join("?" for _ in images)
    scanned = "(l.ext = 'pdf' AND COALESCE(l1.text_pages, 0) = 0)"
    pdf_gap = ("(l.ext = 'pdf' AND (l1.state IN ('empty', 'ok')"
               " AND (l1.state = 'empty' OR l1.quality = 'low'"
               "  OR l1.page_count > l1.text_pages)))")
    image_part = f"l.ext IN ({placeholders})"
    pptx_images = "(l.ext = 'pptx' AND COALESCE(l1.image_count, 0) > 0)"
    params: list[str] = []

    if plan["policy"] == "windows":
        if not plan["windows"]:
            return plan, None, None, "0", []
        engine_expr = "'windows'"
        sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                    f" ELSE '{OCR_SIGNATURES['windows']}' END")
    elif plan["policy"] == "rapidocr":
        if plan["rapidocr"]:
            engine_expr = "'rapidocr'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['rapidocr']}'"
                        f" ELSE '{OCR_SIGNATURES['rapidocr']}' END")
        elif plan["windows"]:
            engine_expr = "'windows'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                        f" ELSE '{OCR_SIGNATURES['windows']}' END")
        else:
            return plan, None, None, "0", []
    else:  # auto
        if plan["windows"] and plan["rapidocr"]:
            engine_expr = f"CASE WHEN {scanned} THEN 'rapidocr' ELSE 'windows' END"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                        f" WHEN {scanned} THEN '{OCR_SIGNATURES['rapidocr']}'"
                        f" ELSE '{OCR_SIGNATURES['windows']}' END")
        elif plan["windows"]:
            engine_expr = "'windows'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                        f" ELSE '{OCR_SIGNATURES['windows']}' END")
        elif plan["rapidocr"]:
            # 没有 Windows OCR 时，auto 用随包的 RapidOCR 兜底图片与混合 PDF。
            engine_expr = "'rapidocr'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['rapidocr']}'"
                        f" ELSE '{OCR_SIGNATURES['rapidocr']}' END")
        else:
            return plan, None, None, "0", []

    if not (plan["windows"] or plan["rapidocr"]):
        return plan, None, None, "0", []
    eligible = f"({image_part} OR {pdf_gap} OR {pptx_images})"
    return plan, engine_expr, sig_expr, eligible, [*images]


def _ocr_query(conn: sqlite3.Connection, source_id: str | None,
               select: str, tail: str) -> tuple[list[sqlite3.Row], dict]:
    plan, engine_expr, sig_expr, eligible, image_params = _ocr_plan_sql(conn)
    if engine_expr is None or sig_expr is None:
        return [], plan
    where = [
        "v.is_current = 1",
        "l.availability = 'AVAILABLE'",
        "s.state = 'active'",
        eligible,
    ]
    params: list = [PARSER_SIGNATURE, *image_params]
    if source_id:
        where.append("l.source_id = ?")
        params.append(source_id)
    sql = (
        f"{select} FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN parse_artifacts l1 ON l1.version_id = v.version_id"
        "  AND l1.parser_signature = ?"
        f" WHERE {' AND '.join(where)}"
        f" AND NOT EXISTS (SELECT 1 FROM parse_artifacts b"
        f"   WHERE b.version_id = v.version_id AND b.parser_signature = {sig_expr})"
        f" {tail}"
    )
    return conn.execute(sql, params).fetchall(), plan


def ocr_candidate_rows(conn: sqlite3.Connection, source_id: str | None = None,
                       limit: int = OCR_BATCH) -> list[sqlite3.Row]:
    rows, _ = _ocr_query(
        conn, source_id,
        "SELECT v.version_id, v.asset_id, l.path, l.ext, l.source_id",
        f"ORDER BY v.observed_at DESC LIMIT {limit}",
    )
    if not rows:
        return []
    # 引擎表达式按行计算后再补进结果，避免在 SQL 里重复 CASE。
    _, engine_expr, _, _, _ = _ocr_plan_sql(conn)
    engine_sql = (
        "SELECT v.version_id, "
        f"{engine_expr} AS engine FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN parse_artifacts l1 ON l1.version_id = v.version_id"
        "  AND l1.parser_signature = ?"
        f" WHERE v.version_id IN ({','.join('?' for _ in rows)})"
    )
    engine_rows = conn.execute(engine_sql, [PARSER_SIGNATURE, *[r["version_id"] for r in rows]]).fetchall()
    engines = {row["version_id"]: row["engine"] for row in engine_rows}
    return [{**dict(row), "engine": engines.get(row["version_id"])} for row in rows]


def ocr_pending_count(conn: sqlite3.Connection, source_id: str | None = None) -> int:
    rows, _ = _ocr_query(conn, source_id, "SELECT COUNT(*) AS n", "")
    return rows[0]["n"] if rows else 0


def _l1_pages(conn: sqlite3.Connection, version_id: str) -> set[int]:
    """该版本已经有可用文本层的页码；OCR 只补这些页之外的页面。"""
    rows = conn.execute(
        "SELECT DISTINCT c.page FROM chunks c"
        " JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
        " WHERE c.version_id = ? AND c.page IS NOT NULL AND a.state = 'ok'",
        (version_id,),
    ).fetchall()
    return {row["page"] for row in rows if row["page"]}


def _row_engine(conn: sqlite3.Connection, row) -> str | None:
    if isinstance(row, dict) and row.get("engine"):
        return row["engine"]
    if not isinstance(row, dict):
        keys = row.keys()
        if "engine" in keys and row["engine"]:
            return row["engine"]
    l1 = conn.execute(
        "SELECT text_pages FROM parse_artifacts"
        " WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], PARSER_SIGNATURE),
    ).fetchone()
    text_pages = l1["text_pages"] if l1 is not None else 0
    return _route_engine(row["ext"], text_pages, ocr.plan_engines(conn))


def index_ocr_version(conn: sqlite3.Connection, row, engine: str | None = None) -> str:
    """按策略选择引擎，对图片或 PDF 缺失文本层的页做 OCR。"""
    engine = engine or _row_engine(conn, row)
    if engine is None:
        raise ocr.OcrUnavailable("当前没有可用的 OCR 引擎")
    signatures = PPTX_OCR_SIGNATURES if row["ext"] == "pptx" else OCR_SIGNATURES
    signature = signatures[engine]
    existing = conn.execute(
        "SELECT state FROM parse_artifacts WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], signature),
    ).fetchone()
    if existing is not None:
        return existing["state"]

    _job_update(conn, row, "l2", "running", count_attempt=True)
    sections: list[ocr.Section] = []
    chunks: list[Chunk] = []
    try:
        if row["ext"] == "pdf":
            skip_pages: set[int] = set()
            l1 = conn.execute(
                "SELECT state, quality FROM parse_artifacts"
                " WHERE version_id = ? AND parser_signature = ?",
                (row["version_id"], PARSER_SIGNATURE),
            ).fetchone()
            if l1 is not None and l1["state"] == "ok" and l1["quality"] != "low":
                skip_pages = _l1_pages(conn, row["version_id"])
            if engine == "rapidocr":
                sections = ocr.extract_pdf_rapid(row["path"], skip_pages=skip_pages)
            else:
                sections = ocr.extract_pdf(row["path"], skip_pages=skip_pages)
        elif row["ext"] == "pptx":
            sections = ocr.extract_pptx(row["path"], rapid=(engine == "rapidocr"))
        elif engine == "rapidocr":
            sections = ocr.extract_image_rapid(row["path"])
        else:
            sections = ocr.extract_image(row["path"])
        sections = [section for section in sections if section.text.strip()]
        quality = ocr.quality_of(sections)
        if quality == "empty":
            state, message = "empty", "OCR 未识别到文字（可能是空白页或图片质量过低）"
        else:
            state, message = "ok", None
            chunks = chunk_sections(sections)
    except ocr.OcrUnavailable:
        raise
    except Exception as exc:
        state, quality, message = "failed", "empty", str(exc)[:400]

    _purge_old_versions(conn, row["asset_id"], row["version_id"])
    _write_artifact(conn, row, state=state, quality=quality, sections=sections,
                    message=message, chunks=chunks, signature=signature)
    _job_update(conn, row, "l2", "done" if state != "failed" else "failed", error=message)
    return state


def index_ocr_pending(conn: sqlite3.Connection, source_id: str | None = None,
                      limit: int = OCR_RUN_LIMIT) -> dict:
    counters = {"available": True, "indexed": 0, "empty": 0, "failed": 0,
                "processed": 0, "remaining": 0, "budget_exhausted": False}
    plan = ocr.plan_engines(conn)
    windows_info = ocr.engine_info()
    available = plan["windows"]
    if not available and plan["rapidocr"] and plan["policy"] == "rapidocr":
        available = True
    if not available and plan["rapidocr"] and ocr_pending_count(conn, source_id):
        available = True
    if not available:
        return {**counters, "available": False,
                "message": windows_info["message"] or "没有可用的 OCR 引擎",
                "engine": windows_info["engine"], "languages": windows_info["languages"]}

    for signature in [*OCR_SIGNATURES.values(), *PPTX_OCR_SIGNATURES.values()]:
        _requeue_failed(conn, "l2", signature, source_id)
    conn.commit()

    allow = min(limit, budget.remaining(conn, "l2"))
    processed = 0
    while processed < allow:
        rows = ocr_candidate_rows(conn, source_id, min(OCR_BATCH, allow - processed))
        if not rows:
            break
        for row in rows:
            state = index_ocr_version(conn, row)
            counters[{"ok": "indexed"}.get(state, state)] += 1
            processed += 1
        conn.commit()

    budget.add(conn, "l2", processed)
    if allow == 0 and ocr_candidate_rows(conn, source_id, 1):
        counters["budget_exhausted"] = True
    counters["processed"] = processed
    counters["remaining"] = ocr_pending_count(conn, source_id)
    counters["engine"] = plan["policy"]
    counters["languages"] = (windows_info["languages"] if plan["windows"]
                             else ["zh-Hans", "en"])
    if processed:
        store.log_event(
            conn, "ocr_completed", source_id,
            detail=json.dumps({key: counters[key] for key in
                               ("indexed", "empty", "failed", "remaining",
                                "budget_exhausted")},
                              ensure_ascii=False),
            outcome="ok",
        )
        conn.commit()
    return counters


def index_ocr_in_new_connection(source_id: str | None = None,
                                store_path: str | None = None) -> dict:
    conn = db.connect(store_path)
    try:
        return index_ocr_pending(conn, source_id)
    finally:
        conn.close()


def list_jobs(conn: sqlite3.Connection, *, status: str | None = None,
              stage: str | None = None, limit: int = 50) -> list[dict]:
    sql = [
        "SELECT j.job_id, j.source_id, j.version_id, j.asset_id, j.stage, j.status,"
        " j.attempts, j.error, j.updated_at, l.path, l.ext",
        "FROM index_jobs j",
        "LEFT JOIN content_versions v ON v.version_id = j.version_id",
        "LEFT JOIN asset_locations l ON l.asset_id = v.asset_id",
        "WHERE 1 = 1",
    ]
    params: list = []
    if status:
        sql.append("AND j.status = ?")
        params.append(status)
    if stage:
        sql.append("AND j.stage = ?")
        params.append(stage)
    sql.append("ORDER BY CASE j.status WHEN 'failed' THEN 0 ELSE 1 END, j.updated_at DESC")
    sql.append(f"LIMIT {max(1, min(limit, 200))}")
    items = []
    for row in conn.execute(" ".join(sql), params):
        item = dict(row)
        path = item.get("path") or ""
        item["name"] = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if path else None
        items.append(item)
    return items


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)).fetchone()


def retry_job(conn: sqlite3.Connection, job_id: str) -> dict:
    """单文件重试：清掉该版本的失败产物并立刻重跑一次。"""
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    row = conn.execute(
        "SELECT v.version_id, v.asset_id, l.path, l.ext, l.source_id"
        " FROM content_versions v JOIN asset_locations l ON l.asset_id = v.asset_id"
        " WHERE v.version_id = ?", (job["version_id"],)).fetchone()
    if row is None:
        raise ValueError("该版本已不在资产目录中")

    engine = None
    if job["stage"] == "l1":
        signature = PARSER_SIGNATURE
    else:
        engine = _row_engine(conn, row)
        if engine is None:
            raise ValueError("当前没有可用的 OCR 引擎")
        signature = OCR_SIGNATURES[engine]
    conn.execute(
        "DELETE FROM chunk_fts WHERE rowid IN (SELECT c.rowid FROM chunks c"
        " JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
        " WHERE a.version_id = ? AND a.parser_signature = ?)",
        (row["version_id"], signature))
    conn.execute(
        "DELETE FROM chunks WHERE artifact_id IN (SELECT artifact_id FROM parse_artifacts"
        " WHERE version_id = ? AND parser_signature = ?)",
        (row["version_id"], signature))
    conn.execute(
        "DELETE FROM parse_artifacts WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], signature))
    conn.execute(
        "UPDATE index_jobs SET attempts = 0, status = 'pending', error = NULL"
        " WHERE job_id = ?", (job_id,))
    conn.commit()

    if job["stage"] == "l1":
        state = index_version(conn, row)
    else:
        state = index_ocr_version(conn, row, engine)
    budget.add(conn, "l1" if job["stage"] == "l1" else "l2", 1)
    store.log_event(
        conn, "job_retry", row["source_id"],
        detail=json.dumps({"job_id": job_id, "stage": job["stage"], "state": state},
                          ensure_ascii=False),
        outcome="ok" if state != "failed" else "partial",
    )
    conn.commit()
    return {"job_id": job_id, "stage": job["stage"], "state": state}


def coverage(conn: sqlite3.Connection) -> dict:
    """当前内容版本的解析覆盖：已解析 / 无文本层 / 失败 / 未支持 / 待处理。"""
    rows = conn.execute(
        "SELECT v.version_id, l.ext, a.state FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
        "  AND a.parser_signature = ?"
        " WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        (PARSER_SIGNATURE,),
    ).fetchall()
    counts = {"indexed": 0, "empty": 0, "failed": 0, "pending": 0, "unsupported": 0}
    for row in rows:
        if not extractors.is_supported(row["ext"]):
            # 图片与 PDF 由 L2 接管，不算"暂不支持"。
            if row["ext"] not in ocr.OCR_EXTS:
                counts["unsupported"] += 1
        elif row["state"] is None:
            counts["pending"] += 1
        elif row["state"] == "ok":
            counts["indexed"] += 1
        elif row["state"] == "empty":
            counts["empty"] += 1
        else:
            counts["failed"] += 1

    plan, engine_expr, sig_expr, eligible, image_params = _ocr_plan_sql(conn)
    ocr_states: dict = {}
    if sig_expr is not None:
        rows = conn.execute(
            "SELECT a.state, COUNT(*) AS n FROM content_versions v"
            " JOIN asset_locations l ON l.asset_id = v.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " LEFT JOIN parse_artifacts l1 ON l1.version_id = v.version_id"
            "  AND l1.parser_signature = ?"
            f" LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
            f"  AND a.parser_signature = {sig_expr}"
            " WHERE v.is_current = 1 AND l.availability = 'AVAILABLE'"
            f"  AND s.state = 'active' AND {eligible}"
            " GROUP BY a.state",
            [PARSER_SIGNATURE, *image_params],
        ).fetchall()
        ocr_states = {row["state"]: row["n"] for row in rows}
    counts["low_quality"] = conn.execute(
        "SELECT COUNT(*) FROM parse_artifacts a"
        " JOIN content_versions v ON v.version_id = a.version_id"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE a.parser_signature = ? AND a.quality = 'low' AND v.is_current = 1"
        "  AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        (PARSER_SIGNATURE,),
    ).fetchone()[0]
    info = ocr.engine_info()
    counts.update({
        "ocr_indexed": ocr_states.get("ok", 0),
        "ocr_empty": ocr_states.get("empty", 0),
        "ocr_failed": ocr_states.get("failed", 0),
        "ocr_pending": ocr_pending_count(conn),
        "mixed_pdf": conn.execute(
            "SELECT COUNT(*) FROM parse_artifacts a"
            " JOIN content_versions v ON v.version_id = a.version_id"
            " JOIN asset_locations l ON l.asset_id = v.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " WHERE a.parser_signature = ? AND l.ext = 'pdf' AND a.page_count > a.text_pages"
            "  AND a.text_pages > 0"
            "  AND v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'"
            "  AND NOT EXISTS (SELECT 1 FROM parse_artifacts b"
            "   WHERE b.version_id = a.version_id AND b.parser_signature IN (?, ?))",
            (PARSER_SIGNATURE, OCR_SIGNATURES["windows"], OCR_SIGNATURES["rapidocr"]),
        ).fetchone()[0],
        "ocr_available": info["available"],
        "ocr_engine": plan["policy"],
        "ocr_policy": plan["policy"],
        "ocr_languages": info["languages"],
        "ocr_message": info["message"],
    })
    return counts
