"""M4 问答与引用：先用本地检索收集证据，再交给"本地摘录"或可选模型组织回答。

硬约束：
- 没有证据就拒答，不凭文件名校验或常识补全；
- 云端调用必须显式授权，只上传入选的证据片段，不整库上传；
- 回答必须带 [n] 引用，引用越界或缺失会在界面上明确提示。
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from . import embeddings, indexer, query, retrieval, store

SETTINGS_KEY = "answer"
PROVIDERS = {"extractive", "openai", "ollama"}
MAX_EVIDENCE_CHARS = 900
MAX_PROMPT_CHARS = 8000

DEFAULT_SETTINGS = {
    "provider": "extractive",
    "base_url": "",
    "model": "",
    "api_key": "",
    "allow_cloud": False,
    "top_k": 6,
}


class AnswerError(RuntimeError):
    pass


# ---------------------------------------------------------------- 设置

def load_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            settings.update({key: stored[key] for key in DEFAULT_SETTINGS if key in stored})
    if settings["provider"] not in PROVIDERS:
        settings["provider"] = "extractive"
    try:
        settings["top_k"] = max(1, min(int(settings["top_k"]), 12))
    except (TypeError, ValueError):
        settings["top_k"] = 6
    settings["allow_cloud"] = bool(settings["allow_cloud"])
    return settings


def public_settings(conn: sqlite3.Connection) -> dict:
    settings = load_settings(conn)
    has_key = bool(settings.pop("api_key", ""))
    settings["has_key"] = has_key
    return settings


def update_settings(conn: sqlite3.Connection, payload: dict) -> dict:
    settings = load_settings(conn)
    provider = payload.get("provider")
    if provider is not None:
        if provider not in PROVIDERS:
            raise ValueError("未知的回答提供商")
        settings["provider"] = provider
    for key in ("base_url", "model"):
        if key in payload and payload[key] is not None:
            settings[key] = str(payload[key]).strip()
    if payload.get("allow_cloud") is not None:
        settings["allow_cloud"] = bool(payload["allow_cloud"])
    if payload.get("top_k") is not None:
        try:
            settings["top_k"] = max(1, min(int(payload["top_k"]), 12))
        except (TypeError, ValueError) as exc:
            raise ValueError("top_k 必须是数字") from exc
    if payload.get("clear_key"):
        settings["api_key"] = ""
    elif payload.get("api_key"):
        settings["api_key"] = str(payload["api_key"]).strip()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(settings, ensure_ascii=False)),
    )
    conn.commit()
    return public_settings(conn)


# ---------------------------------------------------------------- 检索

@dataclass
class Evidence:
    chunk_id: str
    asset_id: str
    location_id: str
    path: str
    name: str
    text: str
    page: int | None
    sheet: str | None
    matches: int
    ocr: bool = False
    semantic: bool = False
    version_id: str = ""
    mtime: str | None = None
    bbox: list[list[int]] | None = None

    def label(self) -> str:
        name = f"文件：{self.name}"
        if self.page:
            name += f"（第 {self.page} 页）"
        if self.sheet:
            name += f"（工作表 {self.sheet}）"
        if self.ocr:
            name += "（OCR 识别）"
        if self.mtime:
            name += f"（更新于 {self.mtime.replace('T', ' ')[:16]}）"
        return name

    def citation(self, number: int) -> dict:
        snippet = " ".join(self.text.split())
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        return {
            "n": number,
            "chunk_id": self.chunk_id,
            "location_id": self.location_id,
            "name": self.name,
            "path": self.path,
            "page": self.page,
            "sheet": self.sheet,
            "snippet": snippet,
            "ocr": self.ocr,
            "semantic": self.semantic,
            "version_id": self.version_id,
            "mtime": self.mtime,
            "bbox": self.bbox,
        }


def retrieve(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
             ext: str | None = None, limit: int = 6) -> list[Evidence]:
    analysis = query.analyze(question, synonyms=query.synonym_map(conn))
    if not analysis["variants"]:
        return []
    evidence: list[Evidence] = []
    per_asset: dict[str, int] = {}
    for hit in retrieval.find_chunks(conn, question, analysis=analysis,
                                     source_id=source_id, ext=ext):
        if per_asset.get(hit.asset_id, 0) >= 2:
            continue
        per_asset[hit.asset_id] = per_asset.get(hit.asset_id, 0) + 1
        name = hit.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        evidence.append(Evidence(
            chunk_id=hit.chunk_id,
            asset_id=hit.asset_id,
            location_id=hit.location_id,
            path=hit.path,
            name=name,
            text=hit.text[:MAX_EVIDENCE_CHARS],
            page=hit.page,
            sheet=hit.sheet,
            matches=hit.concepts,
            ocr=hit.ocr,
            version_id=hit.version_id,
            mtime=_iso_mtime(hit.mtime_ns),
            bbox=hit.bbox,
        ))
        if len(evidence) >= limit:
            break

    settings = embeddings.load_settings(conn)
    if embeddings.enabled(settings) and len(evidence) < limit:
        seen = {item.chunk_id for item in evidence}
        try:
            semantic_hits = embeddings.semantic_chunks(
                conn, analysis["core"] or question, source_id=source_id, ext=ext,
                top_k=max(10, settings["top_k"] * 3))
        except embeddings.EmbeddingError:
            semantic_hits = []
        for hit in semantic_hits:
            if len(evidence) >= limit:
                break
            if hit["chunk_id"] in seen or per_asset.get(hit["asset_id"], 0) >= 2:
                continue
            per_asset[hit["asset_id"]] = per_asset.get(hit["asset_id"], 0) + 1
            name = hit["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            evidence.append(Evidence(
                chunk_id=hit["chunk_id"],
                asset_id=hit["asset_id"],
                location_id=hit["location_id"],
                path=hit["path"],
                name=name,
                text=hit["text"][:MAX_EVIDENCE_CHARS],
                page=hit["page"],
                sheet=hit["sheet"],
                matches=0,
                ocr=hit["ocr"],
                semantic=True,
                version_id=hit["version_id"],
                mtime=_iso_mtime(hit["mtime_ns"]),
                bbox=hit.get("bbox"),
            ))
    return evidence


def _iso_mtime(mtime_ns: int) -> str | None:
    if not mtime_ns:
        return None
    return datetime.fromtimestamp(mtime_ns / 1e9).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 回答

def _evidence_block(evidence: list[Evidence]) -> str:
    parts = []
    total = 0
    for index, item in enumerate(evidence, start=1):
        piece = f"[{index}] {item.label()}\n{item.text}"
        if total + len(piece) > MAX_PROMPT_CHARS:
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def _messages(question: str, evidence: list[Evidence]) -> list[dict]:
    system = (
        "你是本地知识库的检索助手，只能依据用户消息中的<证据>回答。"
        "禁止补充证据之外的事实或推测。每个事实句末用 [数字] 标注对应证据。"
        "证据标题带有文件的更新时间；若问题涉及时间条件（如最近、去年、某个日期前后），"
        "要用这些时间判断取舍，并在回答里说明采用的版本时间。"
        "若证据之间数值或结论不一致，请并列呈现，并说明各自来自哪个文件/时间条件。"
        "若证据不足，直接回答：根据现有资料无法确定。"
    )
    user = f"<问题>\n{question}\n\n<证据>\n{_evidence_block(evidence)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _post_json(url: str, payload: dict, headers: dict, timeout: float = 30.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AnswerError(f"模型接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AnswerError(f"无法连接模型接口：{exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise AnswerError("模型接口返回了非 JSON 内容") from exc


def _openai_answer(settings: dict, question: str, evidence: list[Evidence]) -> str:
    if not settings.get("allow_cloud"):
        raise AnswerError("云端回答未获授权：请先在对话设置里允许上传证据片段")
    if not settings.get("api_key") or not settings.get("model"):
        raise AnswerError("云端回答尚未配置模型名称或 API Key")
    base = (settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "messages": _messages(question, evidence),
    }
    data = _post_json(f"{base}/chat/completions", payload,
                      {"Authorization": f"Bearer {settings['api_key']}"})
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AnswerError("模型接口返回结构不符合 OpenAI 兼容格式") from exc


def _ollama_answer(settings: dict, question: str, evidence: list[Evidence]) -> str:
    if not settings.get("model"):
        raise AnswerError("Ollama 回答尚未填写模型名称")
    base = (settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    payload = {"model": settings["model"], "stream": False,
               "messages": _messages(question, evidence)}
    data = _post_json(f"{base}/api/chat", payload, {})
    try:
        return str(data["message"]["content"]).strip()
    except (KeyError, TypeError) as exc:
        raise AnswerError("Ollama 返回结构不符合预期") from exc


def _extractive_answer(evidence: list[Evidence]) -> str:
    lines = ["以下是与问题最相关的证据摘录（本地模式，未调用生成模型）："]
    for index, item in enumerate(evidence, start=1):
        snippet = " ".join(item.text.split())
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        lines.append(f"[{index}] {item.label()}：{snippet}")
    return "\n\n".join(lines)


def _citation_warning(text: str, evidence_count: int) -> str | None:
    numbers = {int(value) for value in re.findall(r"\[(\d+)\]", text)}
    valid = {value for value in numbers if 1 <= value <= evidence_count}
    if not valid:
        return "回答没有引用任何证据，请结合下方来源自行核对，不要直接采信。"
    if numbers - valid:
        extra = "、".join(f"[{value}]" for value in sorted(numbers - valid))
        return f"回答包含不存在的引用 {extra}，已忽略这些编号。"
    return None


def _coverage(conn: sqlite3.Connection) -> dict:
    counts = indexer.coverage(conn)
    return {"content_indexed": counts["indexed"], "content_pending": counts["pending"],
            "content_empty": counts["empty"], "content_failed": counts["failed"],
            "ocr_indexed": counts["ocr_indexed"], "ocr_pending": counts["ocr_pending"]}


def ask(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
        ext: str | None = None, top_k: int | None = None) -> dict:
    text = (question or "").strip()
    if not text:
        raise ValueError("问题不能为空")
    settings = load_settings(conn)
    limit = top_k or settings["top_k"]
    evidence = retrieve(conn, text, source_id=source_id, ext=ext, limit=limit)
    coverage = _coverage(conn)
    provider = settings["provider"]

    if not evidence:
        indexed = coverage["content_indexed"]
        pending = coverage["content_pending"]
        answer = f"没有找到足够证据回答这个问题。当前正文索引覆盖 {indexed} 个文件"
        if pending:
            answer += f"，另有 {pending} 个文件待解析"
        answer += "；扫描件需要 OCR（L2）后才能检索。"
        return {
            "question": text,
            "answer": answer,
            "refused": True,
            "reason": "no_evidence",
            "provider": provider,
            "model": settings.get("model") or None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
        }

    if provider == "extractive":
        answer = _extractive_answer(evidence)
        warning = None
    elif provider == "openai":
        answer = _openai_answer(settings, text, evidence)
        warning = _citation_warning(answer, len(evidence))
    else:
        answer = _ollama_answer(settings, text, evidence)
        warning = _citation_warning(answer, len(evidence))

    if not answer:
        raise AnswerError("模型没有返回任何内容")
    return {
        "question": text,
        "answer": answer,
        "refused": False,
        "reason": None,
        "provider": provider,
        "model": settings.get("model") or None,
        "citations": [item.citation(index) for index, item in enumerate(evidence, start=1)],
        "evidence_count": len(evidence),
        "warning": warning,
        "coverage": coverage,
    }
