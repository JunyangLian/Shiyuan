"""HTTP 接口：桌面端通过 127.0.0.1 与 Knowledge Core 通信。

只监听回环地址；所有接口都只读用户文件，不提供移动/删除原件的操作。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import (__version__, answer, chat, config, db, embeddings, indexer, maintenance, ocr,
               query, scanner, search, store, watcher)
from .config import db_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    manager = None
    if config.watch_enabled():
        manager = watcher.WatcherManager(db_path())
        manager.start()
    app.state.watcher = manager
    try:
        yield
    finally:
        if manager is not None:
            manager.stop()


app = FastAPI(title="拾源 Knowledge Core", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(http://(localhost|127\.0\.0\.1):\d+|http://tauri\.localhost|https://tauri\.localhost)$",
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _watcher(request: Request):
    return getattr(request.app.state, "watcher", None)


class SourceCreate(BaseModel):
    root: str = Field(min_length=1)
    label: str | None = None
    excludes: list[str] = Field(default_factory=list)


class AskPayload(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    source_id: str | None = None
    ext: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=12)
    session_id: str | None = None


class AnswerSettingsPayload(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    clear_key: bool = False
    allow_cloud: bool | None = None
    top_k: int | None = Field(default=None, ge=1, le=12)


class QuerySettingsPayload(BaseModel):
    custom: list[list[str]] | None = None


class OcrSettingsPayload(BaseModel):
    policy: str | None = None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "db": str(db_path())}


@app.get("/api/sources")
def list_sources(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    items = []
    for row in store.list_sources(conn):
        items.append({
            "source_id": row["source_id"],
            "root_uri": row["root_uri"],
            "label": row["label"],
            "state": row["state"],
            "health": row["health"],
            "last_scan_at": row["last_scan_at"],
            "excludes": row["excludes"],
            "stats": store.source_stats(conn, row["source_id"]),
            "scanning": scanner.is_running(row["source_id"]),
        })
    return {"items": items}


@app.post("/api/sources", status_code=201)
def create_source(payload: SourceCreate, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        plan = store.plan_source_add(conn, payload.root)
        busy = [item for item in plan["merged"] if scanner.is_running(item["source_id"])]
        if busy:
            raise HTTPException(status_code=409,
                                detail="目录内的数据源正在扫描，请稍后再添加上级目录")
        result = store.add_source_merged(conn, payload.root, payload.label, payload.excludes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = result["source"]
    manager = _watcher(request)
    if manager is not None:
        for item in result["merged"]:
            manager.unschedule(item["source_id"])
        manager.schedule(row["source_id"], row["root_uri"])
    return {
        "source_id": row["source_id"],
        "root_uri": row["root_uri"],
        "label": row["label"],
        "created": result["created"],
        "covered_by": result["covered_by"],
        "merged": result["merged"],
    }


@app.post("/api/sources/{source_id}/state")
def change_state(source_id: str, request: Request, state: str = Query(pattern="^(active|paused)$"),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    row = store.set_source_state(conn, source_id, state)
    if row is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    manager = _watcher(request)
    if manager is not None:
        if state == "active":
            manager.schedule(source_id, row["root_uri"])
        else:
            manager.unschedule(source_id)
    return {"source_id": source_id, "state": row["state"]}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if scanner.is_running(source_id):
        raise HTTPException(status_code=409, detail="该数据源正在扫描，请稍后再撤销")
    manager = _watcher(request)
    if manager is not None:
        manager.unschedule(source_id)
    try:
        cleared = store.remove_source(conn, source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="数据源不存在") from exc
    return {"source_id": source_id, **cleared}


@app.post("/api/sources/{source_id}/scan", status_code=202)
def start_scan(source_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if not scanner.scan_in_background(source_id):
        raise HTTPException(status_code=409, detail="该数据源正在扫描")
    return {"source_id": source_id, "state": "running"}


def _run_ocr(source_id: str) -> None:
    try:
        indexer.index_ocr_in_new_connection(source_id)
    finally:
        scanner.release_scan(source_id)


@app.post("/api/sources/{source_id}/ocr", status_code=202)
def start_ocr(source_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """按策略做深度解析：文本型 PDF 用 Windows OCR，整份扫描 PDF 用 PP-OCRv6 small。"""
    if store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    plan = ocr.plan_engines(conn)
    if not plan["windows"] and not plan["rapidocr"]:
        raise HTTPException(status_code=400, detail="没有可用的 OCR 引擎")
    if not scanner.request_scan(source_id):
        raise HTTPException(status_code=409, detail="该数据源正在同步，请稍后再试")
    threading.Thread(target=_run_ocr, args=(source_id,), name=f"ocr-{source_id}",
                     daemon=True).start()
    info = ocr.engine_info()
    return {"source_id": source_id, "state": "running",
            "pending": indexer.ocr_pending_count(conn, source_id),
            "engine": plan["policy"], "languages": info["languages"]}


@app.get("/api/search")
def run_search(q: str = "", limit: int = 50, offset: int = 0,
               source_id: str | None = None, ext: str | None = None,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return search.search(conn, q, limit=limit, offset=offset, source_id=source_id, ext=ext)


@app.get("/api/stats")
def run_stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return search.stats(conn)


@app.post("/api/ask")
def run_ask(payload: AskPayload, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if payload.session_id and chat.get_session(conn, payload.session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        result = answer.ask(conn, payload.question, source_id=payload.source_id,
                            ext=payload.ext, top_k=payload.top_k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except answer.AnswerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if payload.session_id:
        chat.append_exchange(conn, payload.session_id, payload.question, result)
        result["session_id"] = payload.session_id
    return result


class ChatSessionPayload(BaseModel):
    title: str | None = None


class ChatRenamePayload(BaseModel):
    title: str = Field(min_length=1, max_length=60)


@app.get("/api/chat/sessions")
def list_chat_sessions(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"items": chat.list_sessions(conn)}


@app.post("/api/chat/sessions", status_code=201)
def create_chat_session(payload: ChatSessionPayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return chat.create_session(conn, payload.title)


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str,
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    session = chat.get_session(conn, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session": session, "messages": chat.list_messages(conn, session_id)}


@app.patch("/api/chat/sessions/{session_id}")
def rename_chat_session(session_id: str, payload: ChatRenamePayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        session = chat.rename_session(conn, session_id, payload.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@app.delete("/api/chat/sessions/{session_id}")
def delete_chat_session(session_id: str,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if not chat.delete_session(conn, session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "deleted": True}


@app.get("/api/settings/answer")
def get_answer_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return answer.public_settings(conn)


@app.put("/api/settings/answer")
def put_answer_settings(payload: AnswerSettingsPayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return answer.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/embedding")
def get_embedding_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return embeddings.public_settings(conn)


@app.put("/api/settings/embedding")
def put_embedding_settings(payload: AnswerSettingsPayload,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return embeddings.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/query")
def get_query_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return query.public_settings(conn)


@app.put("/api/settings/query")
def put_query_settings(payload: QuerySettingsPayload,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return query.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/ocr")
def get_ocr_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return ocr.public_settings(conn)


@app.put("/api/settings/ocr")
def put_ocr_settings(payload: OcrSettingsPayload,
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return ocr.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/events")
def list_events(limit: int = 20, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT event_id, type, source_id, detail, outcome, timestamp FROM maintenance_events"
        " ORDER BY event_id DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/jobs")
def list_index_jobs(status: str | None = None, stage: str | None = None, limit: int = 50,
                    conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"items": indexer.list_jobs(conn, status=status, stage=stage, limit=limit)}


@app.post("/api/jobs/{job_id}/retry")
def retry_index_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """单文件重试：只重跑这一条任务，不影响同数据源的其他文件。"""
    job = indexer.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["stage"] == "l2":
        plan = ocr.plan_engines(conn)
        if not plan["windows"] and not plan["rapidocr"]:
            raise HTTPException(status_code=400, detail="没有可用的 OCR 引擎")
    if not scanner.request_scan(job["source_id"]):
        raise HTTPException(status_code=409, detail="该数据源正在同步，请稍后再试")
    try:
        return indexer.retry_job(conn, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ocr.OcrUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        scanner.release_scan(job["source_id"])


def _start_maintenance(kind: str, source_id: str | None, conn: sqlite3.Connection) -> dict:
    if source_id and store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if not maintenance.start(kind, source_id, db_path()):
        raise HTTPException(status_code=409, detail="已有维护任务在运行")
    return {"kind": kind, "source_id": source_id, "state": "running"}


@app.post("/api/maintenance/reindex", status_code=202)
def maintenance_reindex(source_id: str | None = None,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """清空正文产物后按当前文件重建（OCR 产物保留）。"""
    return _start_maintenance("reindex", source_id, conn)


@app.post("/api/maintenance/retry", status_code=202)
def maintenance_retry(source_id: str | None = None,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """把失败任务的尝试次数清零并重新解析（正文与 OCR）。"""
    return _start_maintenance("retry", source_id, conn)


@app.post("/api/maintenance/ocr", status_code=202)
def maintenance_ocr(source_id: str | None = None,
                    conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """清空 OCR 产物后对图片与无文本层 PDF 重跑。"""
    plan = ocr.plan_engines(conn)
    if not plan["windows"] and not plan["rapidocr"]:
        raise HTTPException(status_code=400, detail="没有可用的 OCR 引擎")
    return _start_maintenance("ocr", source_id, conn)


@app.post("/api/maintenance/cleanup", status_code=202)
def maintenance_cleanup(source_id: str | None = None,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """清理非当前版本的缓存与孤立块，不动原文件。"""
    return _start_maintenance("cleanup", source_id, conn)


@app.post("/api/maintenance/embed", status_code=202)
def maintenance_embed(source_id: str | None = None,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """为尚未生成向量的块补建向量索引。"""
    settings = embeddings.load_settings(conn)
    if not embeddings.enabled(settings):
        raise HTTPException(status_code=400, detail="语义检索未启用或缺少配置")
    return _start_maintenance("embed", source_id, conn)
