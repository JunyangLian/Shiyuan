"""可选模型重排：把初步召回的候选交给已授权的生成模型按相关性排序。

只做排序，不新增证据，也不替换候选集合；模型未配置、超时或返回格式异常时
保持原顺序，问答流程不受影响。上传内容与后续生成回答相同，仅为候选文件名
与短片段，不上传原文件。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

MAX_CANDIDATES = 50
SNIPPET_CHARS = 200
DEFAULT_TIMEOUT = 40.0

SYSTEM = (
    "你是检索过滤器。根据用户问题，判断每条候选片段能否直接回答问题。"
    "招生名单、课程作业、模板、与问题主题不同的材料都不要列出。\n"
    "只输出 JSON，格式为 {\"relevant\": [3, 1, 2]}：数字是候选编号，"
    "按相关性从高到低，只包含真正相关的候选，至少 1 个。"
    "不要输出解释，不要补充候选之外的信息。"
)


class RerankError(RuntimeError):
    pass


def enabled(settings: dict) -> bool:
    """与回答模型共用配置；本地摘录模式不重排。"""
    provider = settings.get("provider")
    if provider == "openai":
        return bool(settings.get("allow_cloud") and settings.get("api_key")
                    and settings.get("model"))
    if provider == "ollama":
        return bool(settings.get("model"))
    return False


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RerankError(f"重排接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RerankError(f"无法连接重排接口：{exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise RerankError("重排接口返回了非 JSON 内容") from exc


def _candidate_block(hits: list) -> str:
    lines = []
    for index, hit in enumerate(hits, start=1):
        name = hit.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        snippet = " ".join((hit.text or "").split())[:SNIPPET_CHARS]
        lines.append(f"[{index}] {name}｜{snippet}")
    return "\n".join(lines)


def _parse_ids(content: str, count: int) -> list[int]:
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    data = json.loads(text)
    if isinstance(data, dict):
        items = data.get("relevant", data.get("order"))
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError("重排结果缺少 relevant 数组")
    ranked: list[int] = []
    for item in items:
        if isinstance(item, str) and item.strip().isdigit():
            item = int(item.strip())
        if not isinstance(item, int) or isinstance(item, bool):
            continue
        if 1 <= item <= count and item not in ranked:
            ranked.append(item)
    return ranked


def _answer_content(settings: dict, data: dict) -> str:
    if settings.get("provider") == "ollama":
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise RerankError("Ollama 重排返回结构不符合预期") from exc
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RerankError("重排接口返回结构不符合 OpenAI 兼容格式") from exc


def _request(settings: dict, question: str, hits: list, timeout: float) -> str:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"<问题>\n{question}\n\n<候选>\n{_candidate_block(hits)}"},
    ]
    if settings.get("provider") == "ollama":
        base = (settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        data = _post_json(f"{base}/api/chat", {
            "model": settings["model"], "stream": False, "format": "json",
            "options": {"temperature": 0}, "messages": messages,
        }, {}, timeout)
    else:
        base = (settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        data = _post_json(f"{base}/chat/completions", {
            "model": settings["model"], "temperature": 0, "max_tokens": 2000,
            "messages": messages,
        }, {"Authorization": f"Bearer {settings['api_key']}"}, timeout)
    return _answer_content(settings, data)


def rerank(settings: dict, question: str, hits: list,
           limit: int = MAX_CANDIDATES,
           timeout: float | None = None) -> tuple[list, dict]:
    """返回（重排后的候选, 统计）。任何异常都回退为原顺序。"""
    considered = min(len(hits), max(1, limit))
    stats = {"reranked": False, "model": settings.get("model") or None,
             "considered": considered, "kept": len(hits), "empty": False,
             "error": None}
    if considered < 2:
        return hits, stats
    if timeout is None:
        timeout = float(os.environ.get("SHIYUAN_RERANK_TIMEOUT", DEFAULT_TIMEOUT))
    try:
        content = _request(settings, question, hits[:considered], timeout)
        ids = _parse_ids(content, considered)
    except (RerankError, ValueError, KeyError, TypeError) as exc:
        stats["error"] = str(exc)[:200]
        return hits, stats
    if not ids:
        # 模型认为候选里没有明确相关的材料：保留本地排序，不据此清空证据。
        stats["empty"] = True
        return hits, stats
    ranked = [hits[index - 1] for index in ids]
    stats["reranked"] = True
    stats["kept"] = len(ranked)
    return ranked, stats
