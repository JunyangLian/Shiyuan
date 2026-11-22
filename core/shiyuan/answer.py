"""M4 问答与引用：先用本地检索收集证据，再交给"本地摘录"或可选模型组织回答。

硬约束：
- 没有证据就拒答，不凭文件名校验或常识补全；
- 云端调用必须显式授权，只上传入选的证据片段，不整库上传；
- 回答必须带 [n] 引用，引用越界或缺失会在界面上明确提示。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import embeddings, families, indexer, query, retrieval, secrets_store, store

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
    if settings.get("api_key"):
        try:
            settings["api_key"] = secrets_store.unprotect(settings["api_key"])
        except secrets_store.SecretError:
            settings["api_key"] = ""
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
        settings["api_key"] = secrets_store.protect(str(payload["api_key"]).strip())
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
    family_id: str | None = None
    family_size: int = 1
    family_title: str = ""
    version_label: str = ""
    sources: list[dict] = field(default_factory=list)

    def label(self) -> str:
        name = f"文件：{self.name}"
        if self.page:
            name += f"（第 {self.page} 页）"
        if self.sheet:
            name += f"（工作表 {self.sheet}）"
        if self.ocr:
            name += "（OCR 识别）"
        if self.family_size > 1:
            name += f"（同一文档 {self.family_size} 个版本）"
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
            "asset_id": self.asset_id,
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
            "family_id": self.family_id,
            "family_size": self.family_size,
            "family_title": self.family_title,
            "version_label": self.version_label,
            "sources": self.sources,
        }


def _snippet(text: str, focus: list[str] | None) -> str:
    """追问时把片段截到关键词附近，避免长文档的目录占了开头。"""
    if focus:
        lowered = (text or "").lower()
        for term in focus:
            index = lowered.find(term)
            if index > 200:
                return "…" + text[max(0, index - 160):]
    return (text or "")[:MAX_EVIDENCE_CHARS]


def retrieve(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
             ext: str | None = None, limit: int = 6,
             focus: list[str] | None = None,
             path_only: bool = False) -> list[Evidence]:
    analysis = query.analyze(question, synonyms=query.synonym_map(conn))
    if not analysis["variants"]:
        return []
    evidence: list[Evidence] = []
    per_asset: dict[str, int] = {}
    per_family: dict[str, int] = {}

    def take(hit_asset: str, family_id: str | None) -> bool:
        """限制同一文件/同一家族占用的证据位，避免多个版本挤掉其他文件。"""
        key = family_id or f"asset:{hit_asset}"
        if per_asset.get(hit_asset, 0) >= 1:
            return False
        if per_family.get(key, 0) >= 2:
            return False
        per_asset[hit_asset] = per_asset.get(hit_asset, 0) + 1
        per_family[key] = per_family.get(key, 0) + 1
        return True

    for hit in retrieval.find_chunks(conn, question, analysis=analysis,
                                     source_id=source_id, ext=ext, focus=focus,
                                     path_only=path_only):
        if not take(hit.asset_id, hit.family_id):
            continue
        name = hit.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        evidence.append(Evidence(
            chunk_id=hit.chunk_id,
            asset_id=hit.asset_id,
            location_id=hit.location_id,
            path=hit.path,
            name=name,
            text=_snippet(hit.text, focus),
            page=hit.page,
            sheet=hit.sheet,
            matches=hit.concepts,
            ocr=hit.ocr,
            version_id=hit.version_id,
            mtime=_iso_mtime(hit.mtime_ns),
            bbox=hit.bbox,
            family_id=hit.family_id,
            family_size=hit.family_size,
            family_title=hit.family_title,
            version_label=hit.version_label,
            sources=hit.sources,
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
        semantic_hits = families.dedupe_semantic_hits(conn, semantic_hits)
        for hit in semantic_hits:
            if len(evidence) >= limit:
                break
            if hit["chunk_id"] in seen or not take(hit["asset_id"], hit.get("family_id")):
                continue
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
                family_id=hit.get("family_id"),
                family_size=hit.get("family_size", 1),
                family_title=hit.get("family_title", ""),
                version_label=hit.get("version_label", ""),
                sources=hit.get("sources", []),
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


def _messages(question: str, evidence: list[Evidence],
              previous: str | None = None) -> list[dict]:
    system = (
        "你是本地知识库的检索助手，只能依据用户消息中的<证据>回答。"
        "禁止补充证据之外的事实或推测。每个事实句末用 [数字] 标注对应证据。"
        f"今天是 {datetime.now().strftime('%Y-%m-%d')}（本地时区）。"
        "遇到「上个月/最近/今年」等相对时间，先按今天换算成具体年月再判断证据，"
        "并在回答里说明采用的日期范围。"
        "证据标题带有文件的更新时间；若问题涉及时间条件（如最近、去年、某个日期前后），"
        "要用这些时间判断取舍，并在回答里说明采用的版本时间。"
        "证据可能来自同一份文档的多个版本。不要判断或宣称哪个版本最新、最权威、"
        "最值得采用；不要替用户选定版本。若不同版本内容有差异，请并列说明各自来自"
        "哪个文件或版本标记，不要把它们混成一份内容。"
        "若证据之间数值或结论不一致，请并列呈现，并说明各自来自哪个文件/时间条件。"
        "证据只是本次检索命中的少量片段，不代表知识库的全部文件或全部内容；"
        "不要根据证据条数推断文件总数、知识库规模或用户拥有的资料范围。"
        "若证据不足，直接回答：根据现有资料无法确定。"
    )
    user = f"<问题>\n{question}"
    if previous:
        user += f"\n（对话追问，上一轮问题：{previous}）"
    user += f"\n\n<证据>\n{_evidence_block(evidence)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _post_json(url: str, payload: dict, headers: dict, timeout: float | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    if timeout is None:
        timeout = float(os.environ.get("SHIYUAN_MODEL_TIMEOUT", "60"))
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


def _openai_answer(settings: dict, question: str, evidence: list[Evidence],
                   previous: str | None = None) -> str:
    if not settings.get("allow_cloud"):
        raise AnswerError("云端回答未获授权：请先在对话设置里允许上传证据片段")
    if not settings.get("api_key") or not settings.get("model"):
        raise AnswerError("云端回答尚未配置模型名称或 API Key")
    base = (settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "messages": _messages(question, evidence, previous),
    }
    data = _post_json(f"{base}/chat/completions", payload,
                      {"Authorization": f"Bearer {settings['api_key']}"})
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AnswerError("模型接口返回结构不符合 OpenAI 兼容格式") from exc


def _ollama_answer(settings: dict, question: str, evidence: list[Evidence],
                   previous: str | None = None) -> str:
    if not settings.get("model"):
        raise AnswerError("Ollama 回答尚未填写模型名称")
    base = (settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    payload = {"model": settings["model"], "stream": False,
               "messages": _messages(question, evidence, previous)}
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


_OVERVIEW_PATTERNS = (
    re.compile(
        r"(知识库|资料库|库里|拾源|你)(里|中)?(现在|目前|都|一共|总共)?"
        r"(有|包含|收录|存了)(什么|哪些)(文件|资料|文档)?[？?。！!\s]*$"
    ),
    re.compile(r"(有多少|总共多少|一共多少)(个|份)?(文件|资料|文档)"),
    re.compile(r"(文件|资料|文档)(总数|数量|概况)"),
    re.compile(r"(知识库|资料库|库里)(概况|概览|统计)"),
)


def _is_library_overview(text: str) -> bool:
    """识别“知识库里有什么/有多少文件”这类元问题，避免用 6 条证据冒充全库。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 40:
        return False
    return any(pattern.search(compact) for pattern in _OVERVIEW_PATTERNS)


_SOURCE_CUE = re.compile(
    r"(记得|授权|数据源|那个|这个|之前|上次|在哪|位于|是什么|有什么)"
)


def _bigrams(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value or "").lower()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _match_source(conn: sqlite3.Connection, text: str,
                  source_id: str | None = None) -> sqlite3.Row | None:
    """识别“你还记得那个 XX 吗”这类对已授权数据源的追问。"""
    compact = re.sub(r"\s+", "", text or "").lower()
    if not compact or len(compact) > 60 or not _SOURCE_CUE.search(compact):
        return None
    query_grams = _bigrams(compact)
    if len(query_grams) < 2:
        return None

    best: sqlite3.Row | None = None
    best_score = 0
    for row in store.list_sources(conn):
        if source_id and row["source_id"] != source_id:
            continue
        for name in (row["label"] or "", Path(row["root_uri"]).name):
            candidate = re.sub(r"\s+", "", name).lower()
            if not candidate:
                continue
            shared = query_grams & _bigrams(candidate)
            if len(shared) < 2:
                continue
            score = len(shared) + (4 if candidate in compact else 0)
            if score > best_score:
                best_score = score
                best = row
    return best


def _source_recall_answer(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    stats = store.source_stats(conn, row["source_id"])
    available = int(stats["available"] or 0)
    parsed = conn.execute(
        "SELECT COUNT(DISTINCT c.asset_id) FROM chunks c"
        " JOIN asset_locations l ON l.asset_id = c.asset_id"
        " WHERE l.source_id = ? AND l.availability = 'AVAILABLE'",
        (row["source_id"],),
    ).fetchone()[0]
    label = row["label"] or Path(row["root_uri"]).name
    lines = [
        f"记得。你授权的数据源「{label}」在 `{row['root_uri']}`。",
        f"当前有 {available} 个可检索文件，其中 {parsed} 个已解析正文"
        + (f"，{available - parsed} 个待解析。" if available > parsed else "。"),
    ]
    if row["state"] != "active":
        lines.append("这个数据源目前处于暂停状态。")
    lines.append("你可以继续问这个数据源里的具体内容。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 文件夹概览

# 「大三学了哪些课程」「pilot_batches 里有什么」这类问题问的是目录结构，
# 用零散正文片段回答反而更差，直接按文件夹汇总。
_FOLDER_INTENT = (
    re.compile(
        r"(?:哪些|什么|几门|几个)"
        r"(?:课程|科目|课|文件夹|子目录|目录|资料|文件|项目|批次|分析|实验|作业|报告|版本)"
    ),
    re.compile(
        r"(?:课程|科目|课|文件夹|子目录|目录|资料|文件|项目|批次|实验|作业|报告|版本)"
        r"(?:里面?|中|内)?(?:都)?(?:有|是|包括|包含|放|存|收录)(?:了)?(?:些)?(?:什么|哪些|啥)"
    ),
    re.compile(r"^(?=.*(?:里面?|中|内))(?:.*?)(?:有|包括|包含)(?:什么|哪些)"),
)

# 「放在哪个文件夹 / 存在哪里 / 课程表在哪」问的是位置，只按路径命中最稳妥。
_PATH_INTENT = re.compile(
    r"哪(?:个)?(?:文件夹|目录|盘)"
    r"|(?:放在|存放|保存在|存在|放到|存到)(?:了)?哪"
    r"|(?:文件|资料|表格|图片|课件|报告|作业|试卷|单据|记录|数据|课程表|截图|照片|发票|收据|合同)"
    r"[^。？?]{0,6}?哪(?:里|儿)?"
)


def _is_path_question(text: str) -> bool:
    return bool(_PATH_INTENT.search(text or ""))


def _is_folder_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 40:
        return False
    return any(pattern.search(compact) for pattern in _FOLDER_INTENT)


def _like_prefix(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _best_folder(conn: sqlite3.Connection, analysis: dict, *,
                 source_id: str | None, ext: str | None) -> retrieval.PathMatch | None:
    matches = retrieval.path_matches(conn, analysis, source_id=source_id, ext=ext)
    core = re.sub(r"\s+", "", (analysis.get("core") or "")).lower()
    best: retrieval.PathMatch | None = None
    best_key: tuple[int, int, int, int] | None = None
    for match in matches.values():
        if not match.specific_folder_path or not match.specific_folder:
            continue
        folder = (match.specific_folder or "").lower()
        hit_len = 0
        for concept in analysis["concepts"]:
            for variant in concept["variants"]:
                if (len(variant) >= 2 and variant in folder
                        and not retrieval.is_generic_term(variant)):
                    hit_len = max(hit_len, len(variant))
        # 「大二有哪些课程」应回到「大二」，而不是名字里恰好连着「大二课程」的子目录。
        in_query = 1 if folder and folder in core else 0
        key = (in_query, hit_len, -len(folder), match.score)
        if best_key is None or key > best_key:
            best_key = key
            best = match
    return best


def _folder_overview(conn: sqlite3.Connection, match: retrieval.PathMatch) -> str | None:
    folder = (match.specific_folder_path or match.folder_path or "").rstrip("\\/")
    if not folder:
        return None
    prefix = folder + "\\"
    rows = conn.execute(
        "SELECT l.path FROM asset_locations l JOIN sources s ON s.source_id = l.source_id"
        " WHERE l.availability = 'AVAILABLE' AND s.state = 'active'"
        " AND lower(l.path) LIKE lower(?) ESCAPE '\\'",
        (_like_prefix(prefix),),
    ).fetchall()
    total = len(rows)
    if total == 0:
        return None
    children: dict[str, int] = {}
    direct = 0
    for row in rows:
        relative = row["path"][len(prefix):]
        head, separator, _rest = relative.partition("\\")
        if separator:
            children[head] = children.get(head, 0) + 1
        else:
            direct += 1
    label = Path(folder).name or folder
    lines = [f"「{label}」位于 `{folder}`，共有 {total} 个可检索文件。"]
    if children:
        ordered = sorted(children.items(), key=lambda item: (-item[1], item[0]))
        lines.append(f"包含 {len(children)} 个子目录：")
        for name, count in ordered[:15]:
            lines.append(f"- {name}（{count} 个文件）")
        if len(ordered) > 15:
            lines.append(f"- 另有 {len(ordered) - 15} 个子目录未列出")
    if direct:
        lines.append(f"该目录下还有 {direct} 个直属文件。")
    lines.append("以上为本地索引统计，可在检索页点开具体文件查看内容。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 多轮追问

_FOLLOWUP = re.compile(
    r"^(?:那|那么|然后|还有|另外|接着|所以|它|它们|这个|那个|该|上述|上面|刚才|前面|之前|继续)"
    r"|第[一二三四五六七八九十0-9]+(?:章|节|部分|页)"
    r"|呢[？?]?$"
)


def _context_base(history: list[str]) -> str:
    """最近一条不是追问的问题，作为追问的上下文锚点。"""
    for previous in history:
        compact = re.sub(r"\s+", "", previous or "")
        if compact and not _FOLLOWUP.search(compact):
            return previous
    return history[0]


def _contextual_query(text: str, history: list[str] | None) -> str:
    """把「那第三章呢」这类追问补上前一轮问题，仅用于检索。"""
    if not history:
        return text
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 16:
        return text
    if not _FOLLOWUP.search(compact):
        return text
    return _context_base(history).strip() + " " + text


def _focus_terms(text: str) -> list[str]:
    """追问里本轮问题自己的关键词，排序时优先。"""
    analysis = query.analyze(text)
    return [term for term in analysis["variants"]
            if len(term) >= 2 and not retrieval.is_generic_term(term)]


def _library_overview(conn: sqlite3.Connection, *, source_id: str | None,
                      ext: str | None) -> str:
    clauses = ["l.availability = 'AVAILABLE'", "s.state = 'active'"]
    params: list = []
    if source_id:
        clauses.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        clauses.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    where = " AND ".join(clauses)
    total = conn.execute(
        "SELECT COUNT(*) FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        f" WHERE {where}", params,
    ).fetchone()[0]
    by_ext = conn.execute(
        "SELECT l.ext AS ext, COUNT(*) AS n FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        f" WHERE {where} GROUP BY l.ext ORDER BY n DESC LIMIT 8", params,
    ).fetchall()
    source_rows = conn.execute(
        "SELECT s.label,"
        " (SELECT COUNT(*) FROM asset_locations l WHERE l.source_id = s.source_id"
        "  AND l.availability = 'AVAILABLE') AS n"
        " FROM sources s WHERE s.state = 'active' ORDER BY s.created_at"
    ).fetchall()
    coverage = indexer.coverage(conn)
    filtered = bool(source_id or ext)
    lines = [f"当前知识库共有 {total} 个可检索文件{'（按当前筛选）' if filtered else ''}。"]
    if not filtered and source_rows:
        lines.append("数据源：" + "；".join(
            f"{row['label']}（{row['n']} 个）" for row in source_rows
        ))
    if by_ext:
        lines.append("文件类型：" + "、".join(
            f"{row['ext'] or '无扩展名'} {row['n']}" for row in by_ext
        ))
    lines.append(
        f"正文索引：已解析 {coverage['indexed']}，待解析 {coverage['pending']}，"
        f"无文本层 {coverage['empty']}，失败 {coverage['failed']}，"
        f"暂不支持 {coverage['unsupported']}。"
    )
    lines.append(f"OCR：已识别 {coverage['ocr_indexed']}，待 OCR {coverage['ocr_pending']}。")
    if filtered:
        lines.append("正文与 OCR 数字为全库统计。")
    lines.append("这是本地索引统计，不是本次检索到的证据条数。")
    return "\n".join(lines)


def ask(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
        ext: str | None = None, top_k: int | None = None,
        history: list[str] | None = None,
        anchor_sources: list[str] | None = None) -> dict:
    text = (question or "").strip()
    if not text:
        raise ValueError("问题不能为空")
    settings = load_settings(conn)
    limit = top_k or settings["top_k"]
    coverage = _coverage(conn)
    provider = settings["provider"]
    context_text = _contextual_query(text, history)
    previous = _context_base(history) if history and context_text != text else None
    # 追问时如果上一轮只涉及一个数据源，就把检索收窄到它，避免跨资料串味。
    effective_source = source_id
    if (previous and not source_id and anchor_sources
            and len(anchor_sources) == 1):
        effective_source = anchor_sources[0]

    if _is_library_overview(text):
        return {
            "question": text,
            "answer": _library_overview(conn, source_id=source_id, ext=ext),
            "refused": False,
            "reason": "library_overview",
            "provider": "library",
            "model": None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
        }

    matched_source = _match_source(conn, text, source_id=source_id)
    if matched_source is not None:
        return {
            "question": text,
            "answer": _source_recall_answer(conn, matched_source),
            "refused": False,
            "reason": "source_recall",
            "provider": "library",
            "model": None,
            "citations": [],
            "evidence_count": 0,
            "warning": None,
            "coverage": coverage,
        }

    if _is_folder_question(text):
        analysis = query.analyze(context_text, synonyms=query.synonym_map(conn))
        folder_match = _best_folder(conn, analysis, source_id=effective_source, ext=ext)
        if folder_match is not None:
            overview = _folder_overview(conn, folder_match)
            if overview:
                return {
                    "question": text,
                    "answer": overview,
                    "refused": False,
                    "reason": "folder_overview",
                    "provider": "library",
                    "model": None,
                    "citations": [],
                    "evidence_count": 0,
                    "warning": None,
                    "coverage": coverage,
                    "context_used": previous,
                }

    evidence = retrieve(conn, context_text, source_id=effective_source, ext=ext, limit=limit,
                        focus=_focus_terms(text) if previous else None,
                        path_only=_is_path_question(text))

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
        answer = _openai_answer(settings, text, evidence, previous)
        warning = _citation_warning(answer, len(evidence))
    else:
        answer = _ollama_answer(settings, text, evidence, previous)
        warning = _citation_warning(answer, len(evidence))

    if not answer:
        raise AnswerError("模型没有返回任何内容")
    citations = [item.citation(index) for index, item in enumerate(evidence, start=1)]
    return {
        "question": text,
        "answer": answer,
        "refused": False,
        "reason": None,
        "provider": provider,
        "model": settings.get("model") or None,
        "citations": citations,
        "evidence_count": len(evidence),
        "warning": warning,
        "coverage": coverage,
        "context_used": previous,
    }
