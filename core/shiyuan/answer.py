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

from . import (embeddings, families, indexer, planner, query, rerank,
               retrieval, secrets_store, store)

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


def _shingles(text: str, size: int = 4) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    if len(compact) <= size:
        return {compact} if compact else set()
    return {compact[index:index + size] for index in range(len(compact) - size + 1)}


def _duplicate_of(signature: set[str], chosen: list[set[str]],
                  threshold: float = 0.88) -> bool:
    """短片段被长片段包含时也算重复，避免同一段落换个版本再占一个证据位。"""
    if not signature:
        return False
    for other in chosen:
        if not other:
            continue
        overlap = len(signature & other) / max(1, min(len(signature), len(other)))
        if overlap >= threshold:
            return True
    return False


def retrieve(conn: sqlite3.Connection, question: str, *, source_id: str | None = None,
             ext: str | None = None, limit: int = 6,
             focus: list[str] | None = None,
             path_only: bool = False,
             plan: planner.Plan | None = None,
             stats: dict | None = None) -> list[Evidence]:
    analysis = query.analyze(question, synonyms=query.synonym_map(conn))
    if not analysis["variants"]:
        return []
    per_asset_limit = plan.mode.per_asset if plan else 1
    per_family_limit = plan.mode.per_family if plan else 2

    evidence: list[Evidence] = []
    chosen_sigs: list[set[str]] = []
    used_chunks: set[str] = set()
    per_asset_count: dict[str, int] = {}
    per_family_count: dict[str, int] = {}

    def family_key(asset_id: str, family_id: str | None) -> str:
        return family_id or f"asset:{asset_id}"

    def take(asset_id: str, family_id: str | None, text: str,
             *, allow_second: bool) -> bool:
        """按计划控制同文件/同家族配额，并跳过与已选内容高度重合的片段。"""
        key = family_key(asset_id, family_id)
        if per_asset_count.get(asset_id, 0) >= per_asset_limit:
            return False
        cap = per_family_limit if allow_second else 1
        if per_family_count.get(key, 0) >= cap:
            return False
        signature = _shingles(text)
        if _duplicate_of(signature, chosen_sigs):
            return False
        per_asset_count[asset_id] = per_asset_count.get(asset_id, 0) + 1
        per_family_count[key] = per_family_count.get(key, 0) + 1
        chosen_sigs.append(signature)
        return True

    def build(hit: retrieval.ChunkHit) -> Evidence:
        name = hit.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return Evidence(
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
        )

    candidates = retrieval.find_chunks(conn, question, analysis=analysis,
                                       source_id=source_id, ext=ext, focus=focus,
                                       path_only=path_only)
    candidate_total = len(candidates)
    # 候选明显多于预算时，先让已授权的生成模型按「能否直接回答」筛选并排序；
    # 未配置、失败或超时都保持本地顺序，不影响问答。
    answer_settings = load_settings(conn)
    rerank_stats: dict = {}
    # 精确查找要保住少量高置信证据，历史追溯要保住多个版本；
    # 只有解释与综合探索容易被同主题噪声淹没，值得多花一次模型调用。
    if (plan is not None and plan.mode.key in {"explain", "comprehensive"}
            and rerank.enabled(answer_settings) and not path_only
            and len(candidates) > limit):
        candidates, rerank_stats = rerank.rerank(
            answer_settings, question, candidates)

    # 先按家族取样，凑够计划要求的最小覆盖面；再回头补同家族的第二条。
    min_families = plan.mode.min_families if plan else 1
    if min_families > 1:
        families_seen: set[str] = set()
        for hit in candidates:
            if len(evidence) >= limit or len(families_seen) >= min_families:
                break
            key = family_key(hit.asset_id, hit.family_id)
            if key in families_seen:
                continue
            if take(hit.asset_id, hit.family_id, hit.text, allow_second=False):
                evidence.append(build(hit))
                used_chunks.add(hit.chunk_id)
                families_seen.add(key)

    for hit in candidates:
        if len(evidence) >= limit:
            break
        if hit.chunk_id in used_chunks:
            continue
        if take(hit.asset_id, hit.family_id, hit.text, allow_second=True):
            evidence.append(build(hit))
            used_chunks.add(hit.chunk_id)

    embed_settings = embeddings.load_settings(conn)
    if embeddings.enabled(embed_settings) and len(evidence) < limit:
        try:
            semantic_hits = embeddings.semantic_chunks(
                conn, analysis["core"] or question, source_id=source_id, ext=ext,
                top_k=max(10, embed_settings["top_k"] * 3))
        except embeddings.EmbeddingError:
            semantic_hits = []
        semantic_hits = families.dedupe_semantic_hits(conn, semantic_hits)
        for hit in semantic_hits:
            if len(evidence) >= limit:
                break
            if hit["chunk_id"] in used_chunks:
                continue
            if not take(hit["asset_id"], hit.get("family_id"), hit["text"],
                        allow_second=True):
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
            used_chunks.add(hit["chunk_id"])

    if stats is not None:
        blob = [f"{item.name}\n{item.text}".lower() for item in evidence]
        facets = [
            {"name": facet, "covered": any(facet.lower() in text for text in blob)}
            for facet in (plan.facets if plan else [])
        ]
        stats.update({
            "candidates": candidate_total,
            "used": len(evidence),
            "families": len({item.family_id or item.asset_id for item in evidence}),
            "facets": facets,
            "missing": [item["name"] for item in facets if not item["covered"]],
            "reranked": bool(rerank_stats.get("reranked")),
            "rerank_kept": rerank_stats.get("kept", candidate_total),
            "rerank_empty": bool(rerank_stats.get("empty")),
            "rerank_error": rerank_stats.get("error"),
        })
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


def _plan_note(plan: planner.Plan, stats: dict) -> str:
    """把检索计划与覆盖情况交给模型，避免它对缺失信息装作完整。"""
    note = (
        f"检索模式：{plan.mode.label}（{plan.reason}）。"
        f"候选 {stats.get('candidates', 0)} 条，采用 {stats.get('used', 0)} 条，"
        f"覆盖 {stats.get('families', 0)} 个文档家族。"
    )
    missing = stats.get("missing") or []
    if missing:
        note += f"以下关键词没有可用证据：{'、'.join(missing)}；请在回答里明确说明这部分资料不足。"
    return note


def _messages(question: str, evidence: list[Evidence],
              previous: str | None = None,
              plan_note: str | None = None) -> list[dict]:
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
        "不要自行做跨行汇总或算术计算（求和、平均、总计），也不要根据片段推算数字；"
        "若问题需要这类统计，说明拾源当前不做表格汇总，并指出相关文件。"
        "若证据不足，直接回答：根据现有资料无法确定。"
    )
    user = f"<问题>\n{question}"
    if previous:
        user += f"\n（对话追问，上一轮问题：{previous}）"
    if plan_note:
        user += f"\n\n<检索规划>\n{plan_note}"
    user += f"\n\n<证据>\n{_evidence_block(evidence)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _post_json(url: str, payload: dict, headers: dict, timeout: float | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    if timeout is None:
        timeout = float(os.environ.get("SHIYUAN_MODEL_TIMEOUT", "90"))
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
                   previous: str | None = None,
                   plan_note: str | None = None) -> str:
    if not settings.get("allow_cloud"):
        raise AnswerError("云端回答未获授权：请先在对话设置里允许上传证据片段")
    if not settings.get("api_key") or not settings.get("model"):
        raise AnswerError("云端回答尚未配置模型名称或 API Key")
    base = (settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "messages": _messages(question, evidence, previous, plan_note),
    }
    data = _post_json(f"{base}/chat/completions", payload,
                      {"Authorization": f"Bearer {settings['api_key']}"})
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise AnswerError("模型接口返回结构不符合 OpenAI 兼容格式") from exc


def _ollama_answer(settings: dict, question: str, evidence: list[Evidence],
                   previous: str | None = None,
                   plan_note: str | None = None) -> str:
    if not settings.get("model"):
        raise AnswerError("Ollama 回答尚未填写模型名称")
    base = (settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    payload = {"model": settings["model"], "stream": False,
               "messages": _messages(question, evidence, previous, plan_note)}
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
    child_dirs: dict[str, set[str]] = {}
    child_files: dict[str, list[str]] = {}
    direct_names: list[str] = []
    for row in rows:
        relative = row["path"][len(prefix):]
        head, separator, rest = relative.partition("\\")
        if not separator:
            direct_names.append(head)
            continue
        children[head] = children.get(head, 0) + 1
        sub, sub_separator, _tail = rest.partition("\\")
        if sub_separator:
            child_dirs.setdefault(head, set()).add(sub)
        else:
            child_files.setdefault(head, []).append(sub)
    label = Path(folder).name or folder
    lines = [f"「{label}」位于 `{folder}`，共有 {total} 个可检索文件。"]
    if children:
        ordered = sorted(children.items(), key=lambda item: (-item[1], item[0]))
        lines.append(f"包含 {len(children)} 个子目录：")
        # 子目录很少时补一层内容，避免用户再追问一次「里面有什么」。
        expand = len(ordered) <= 8
        for name, count in ordered[:15]:
            lines.append(f"- {name}（{count} 个文件）")
            if not expand:
                continue
            dirs = sorted(child_dirs.get(name, ()))
            if dirs:
                shown = "、".join(dirs[:6])
                more = " 等" if len(dirs) > 6 else ""
                lines.append(f"  - 目录：{shown}{more}")
            files = sorted(child_files.get(name, ()))
            if files:
                shown = "、".join(files[:5])
                more = " 等" if len(files) > 5 else ""
                lines.append(f"  - 文件：{shown}{more}")
        if len(ordered) > 15:
            lines.append(f"- 另有 {len(ordered) - 15} 个子目录未列出")
    if direct_names:
        ordered_names = sorted(direct_names)
        shown = "、".join(ordered_names[:8])
        more = "…" if len(ordered_names) > 8 else ""
        lines.append(f"该目录下还有 {len(ordered_names)} 个直属文件：{shown}{more}")
    lines.append("以上为本地索引统计，可在检索页点开具体文件查看内容。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 表格统计

_AGG_INTENT = re.compile(
    r"(合计|总计|总额|总和|加总|求和|汇总|一共|总共|平均|均值|最大值|最小值"
    r"|花了|花销|花费|多少钱)")
_TABLE_ENTITY = re.compile(
    r"(单据|收据|发票|账单|明细|台账|流水|报表|报销|金额|支出|收入|成本|工资|费用|成绩|分数)")
_TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}


def _is_stat_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 30:
        return False
    return bool(_AGG_INTENT.search(compact)) and bool(_TABLE_ENTITY.search(compact))


def _sheet_names(conn: sqlite3.Connection, asset_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT sheet FROM chunks"
        " WHERE asset_id = ? AND sheet IS NOT NULL ORDER BY sheet",
        (asset_id,),
    ).fetchall()
    return [row["sheet"] for row in rows if row["sheet"]]


def _stats_answer(conn: sqlite3.Connection, question: str, *,
                  source_id: str | None, ext: str | None) -> dict | None:
    """表格统计问句：给出命中的表格与工作表，明确说明不做汇总。"""
    analysis = query.analyze(question, synonyms=query.synonym_map(conn))
    matches = retrieval.path_matches(conn, analysis, source_id=source_id, ext=ext)
    tables: list[retrieval.PathMatch] = []
    seen: set[str] = set()
    for match in sorted(matches.values(), key=lambda item: -item.score):
        if Path(match.path).suffix.lower() not in _TABLE_EXTS:
            continue
        key = Path(match.path).name.lower()
        if key in seen:
            continue
        seen.add(key)
        tables.append(match)
        if len(tables) >= 5:
            break
    if not tables:
        return None

    evidence: list[Evidence] = []
    lines = [
        "这是需要跨行汇总的统计问题（合计 / 平均 / 总额）。",
        "拾源目前只做检索与摘录，不会替你求和或求平均，以免给出错误数字。",
        "",
        "命中的表格：",
    ]
    for match in tables:
        name = Path(match.path).name
        sheets = _sheet_names(conn, match.asset_id)
        detail = f"（工作表 {'、'.join(sheets[:3])}）" if sheets else ""
        lines.append(f"- {name}{detail}")
        row = conn.execute(
            "SELECT c.chunk_id, c.text, c.page, c.sheet, c.version_id, l.mtime_ns"
            " FROM chunks c JOIN asset_locations l ON l.asset_id = c.asset_id"
            " WHERE c.asset_id = ? LIMIT 1",
            (match.asset_id,),
        ).fetchone()
        if row is None:
            continue
        evidence.append(Evidence(
            chunk_id=row["chunk_id"],
            asset_id=match.asset_id,
            location_id=match.location_id,
            path=match.path,
            name=name,
            text=(row["text"] or "")[:200],
            page=row["page"],
            sheet=row["sheet"],
            matches=0,
            version_id=row["version_id"] or "",
            mtime=_iso_mtime(row["mtime_ns"]),
        ))
    lines += [
        "",
        "建议：打开表格核对，或换个问法，例如「…里有哪些字段」「哪些…需要人工复核」。",
    ]
    return {
        "answer": "\n".join(lines),
        "citations": [item.citation(index) for index, item in enumerate(evidence, 1)],
    }


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

    if _is_stat_question(text):
        stats_hint = _stats_answer(conn, context_text, source_id=effective_source, ext=ext)
        if stats_hint is not None:
            return {
                "question": text,
                "answer": stats_hint["answer"],
                "refused": False,
                "reason": "table_stats_hint",
                "provider": "library",
                "model": None,
                "citations": stats_hint["citations"],
                "evidence_count": len(stats_hint["citations"]),
                "warning": None,
                "coverage": coverage,
                "context_used": previous,
            }

    plan = planner.make(context_text)
    plan_stats: dict = {}
    budget = limit if top_k else plan.mode.budget
    evidence = retrieve(conn, context_text, source_id=effective_source, ext=ext,
                        limit=budget,
                        focus=_focus_terms(text) if previous else None,
                        path_only=_is_path_question(text),
                        plan=plan, stats=plan_stats)
    plan_payload = {**plan.as_dict(), **plan_stats}
    plan_note = _plan_note(plan, plan_stats)

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
            "plan": plan_payload,
        }

    if provider == "extractive":
        answer = _extractive_answer(evidence)
        warning = None
    elif provider == "openai":
        answer = _openai_answer(settings, text, evidence, previous, plan_note)
        warning = _citation_warning(answer, len(evidence))
    else:
        answer = _ollama_answer(settings, text, evidence, previous, plan_note)
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
        "plan": plan_payload,
    }
