"""MCP (Model Context Protocol) 服务：让本机 Agent 直接检索拾源知识库。

传输使用 stdio（每行一个 JSON-RPC 2.0 消息），只读访问与桌面端相同的 SQLite 数据库。
不扫描、不解析、不改原件；需要新内容时仍由桌面端完成扫描与索引。

启动：
    cd core
    python -m shiyuan.mcp_server

也可以从任意目录直接运行：
    python <repo>/core/mcp_server.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__, config, db, embeddings, external, search, store

SERVER_NAME = "shiyuan-mcp"
DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
DEFAULT_MAX_CHARS = 20_000
MAX_MAX_CHARS = 100_000


class ToolError(RuntimeError):
    """工具参数或数据状态导致的、可以原样返回给 Agent 的错误。"""


def _debug(message: str) -> None:
    if os.environ.get("SHIYUAN_MCP_DEBUG", "").strip().lower() in {"", "0", "false", "off"}:
        return
    line = f"[shiyuan-mcp] {message}\n".encode("utf-8", errors="replace")
    stream = getattr(sys.stderr, "buffer", None) if sys.stderr is not None else None
    try:
        if stream is not None:
            stream.write(line)
            stream.flush()
        else:
            os.write(2, line)
    except OSError:
        pass


def _binary_stream(name: str, fd: int, mode: str) -> BinaryIO:
    """冻结为 --noconsole 时 sys.stdin/stdout 可能为 None，但管道句柄仍然有效。"""
    stream = getattr(sys, name, None)
    buffer = getattr(stream, "buffer", None) if stream is not None else None
    if buffer is not None:
        return buffer
    try:
        return os.fdopen(fd, mode, buffering=0)
    except OSError:
        if os.name != "nt":
            raise
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint]
        handle = kernel32.GetStdHandle(-10 if name == "stdin" else -11)
        if handle in (None, 0, -1) or handle == 2 ** 64 - 1:
            raise RuntimeError(
                f"{name} 不可用；MCP 客户端必须用管道启动 shiyuan-core.exe --mcp"
            )
        flags = os.O_RDONLY if "r" in mode else os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode, buffering=0)


def _error(message_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _result(message_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": payload}


def _tool_text(payload: dict | list) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _string(args: dict, name: str, *, required: bool = True) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise ToolError(f"缺少参数 {name}")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise ToolError(f"参数 {name} 不能为空")
        return None
    return text


def _integer(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"参数 {name} 必须是整数") from exc
    return max(minimum, min(maximum, number))


def _stage_of(signature: str | None) -> str:
    return "L2" if (signature or "").startswith("l2.") else "L1"


class MCPServer:
    """把知识库能力映射成 MCP 的 tools 与 resources。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        message_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(message_id, -32600, "Invalid Request")
        if "id" not in message:  # JSON-RPC 通知不需要响应
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(message_id, -32602, "Invalid params")

        if method == "initialize":
            return _result(message_id, self._initialize(params))
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": self._tools()})
        if method == "tools/call":
            return _result(message_id, self._call_tool(params))
        if method == "resources/list":
            return _result(message_id, {"resources": self._resources()})
        if method == "resources/templates/list":
            return _result(message_id, {"resourceTemplates": []})
        if method == "resources/read":
            try:
                return _result(message_id, self._read_resource(params))
            except _ResourceNotFound as exc:
                return _error(message_id, -32002, str(exc))
        return _error(message_id, -32601, f"Method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {
            "protocolVersion": protocol,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": (
                "Read-only access to the local Shiyuan knowledge base. "
                "For ordinary content questions, use search_knowledge first, then "
                "get_document_text with the returned location_id for full context. "
                "For corpus-level questions (what files exist, how many files, coverage), "
                "call knowledge_stats and list_sources instead: search_knowledge returns "
                "only a limited top-k sample, and its item count is not the corpus total. "
                "Paths point to original files on this machine."
            ),
        }

    # ---------------------------------------------------------------- tools
    def _tools(self) -> list[dict]:
        readonly = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
        return [
            {
                "name": "search_knowledge",
                "title": "Search knowledge base",
                "description": (
                    "Search indexed file names, paths, parsed text and OCR content. "
                    "Returns ranked documents with snippets and page/sheet locators. "
                    "This is the recommended first step for content questions. "
                    "Results are a limited top-k sample, not the complete corpus; "
                    "use knowledge_stats for file counts and coverage."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords or a natural-language question.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_LIMIT,
                            "default": DEFAULT_LIMIT,
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 1000,
                            "default": 0,
                        },
                        "ext": {
                            "type": "string",
                            "description": "Optional extension filter, e.g. pdf, docx, md.",
                        },
                        "source_id": {
                            "type": "string",
                            "description": "Optional authorized source id from list_sources.",
                        },
                    },
                    "required": ["query"],
                },
                "annotations": readonly,
            },
            {
                "name": "get_document_text",
                "title": "Read document text",
                "description": (
                    "Read indexed text for one document by location_id or absolute path. "
                    "Use search_knowledge first. Optionally filter by page or sheet."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "location_id": {
                            "type": "string",
                            "description": "location_id returned by search_knowledge.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Absolute original file path, as an alternative.",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Only return chunks from this PDF/PPTX page.",
                        },
                        "sheet": {
                            "type": "string",
                            "description": "Only return chunks from this worksheet.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": MAX_MAX_CHARS,
                            "default": DEFAULT_MAX_CHARS,
                        },
                    },
                    "anyOf": [{"required": ["location_id"]}, {"required": ["path"]}],
                },
                "annotations": readonly,
            },
            {
                "name": "list_sources",
                "title": "List authorized sources",
                "description": (
                    "List authorized local folders with scan health and file counts. "
                    "Use source_id values to narrow search_knowledge."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": readonly,
            },
            {
                "name": "knowledge_stats",
                "title": "Knowledge base stats",
                "description": (
                    "Return corpus coverage, file-type distribution, OCR status and "
                    "maintenance counters for the local knowledge base."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": readonly,
            },
        ]

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _tool_error("无效的工具调用参数")
        started = time.monotonic()
        try:
            if name == "search_knowledge":
                payload = self._search(arguments)
            elif name == "get_document_text":
                payload = self._document_text(arguments)
            elif name == "list_sources":
                payload = self._list_sources()
            elif name == "knowledge_stats":
                payload = self._stats()
            else:
                return _tool_error(f"未知工具：{name}")
        except ToolError as exc:
            return _tool_error(str(exc))
        except Exception as exc:  # 工具执行失败不应终止整个 MCP 进程
            _debug(f"tool {name} failed: {exc}\n{traceback.format_exc()}")
            return _tool_error(f"工具执行失败：{exc}")
        finally:
            _debug(f"tool {name} took {time.monotonic() - started:.3f}s")
        return _tool_text(payload)

    # ---------------------------------------------------------------- tool impl
    def _search(self, args: dict) -> dict:
        query = _string(args, "query") or ""
        limit = _integer(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _integer(args, "offset", 0, 0, 1000)
        ext = _string(args, "ext", required=False)
        source_id = _string(args, "source_id", required=False)
        result = search.search(
            self.conn, query, limit=limit, offset=offset,
            source_id=source_id, ext=ext,
        )
        items = [
            {
                "location_id": item["location_id"],
                "asset_id": item["asset_id"],
                "name": item["name"],
                "path": item["path"],
                "directory": item["directory"],
                "rel_path": item["rel_path"],
                "ext": item["ext"],
                "size": item["size"],
                "mtime": item["mtime"],
                "page": item["page"],
                "sheet": item["sheet"],
                "index_level": item["index_level"],
                "match": item["match"],
                "semantic": item["semantic"],
                "snippet": item["snippet"],
                "source_id": item["source_id"],
                "source_label": item["source_label"],
                "family_id": item.get("family_id"),
                "family_size": item.get("family_size"),
                "family_title": item.get("family_title"),
                "version_label": item.get("version_label"),
                "family_versions": item.get("family_versions"),
            }
            for item in result["items"]
        ]
        return {
            "query": result["query"],
            "total": result["total"],
            "returned": len(items),
            "offset": offset,
            "rewritten": result.get("rewritten"),
            "vector_used": result.get("vector_used", False),
            "vector_error": result.get("vector_error"),
            "items": items,
        }

    def _resolve_location(self, args: dict) -> sqlite3.Row:
        location_id = _string(args, "location_id", required=False)
        path = _string(args, "path", required=False)
        base = (
            "SELECT l.*, s.label AS source_label, s.state AS source_state,"
            " s.root_uri AS source_root FROM asset_locations l"
            " JOIN sources s ON s.source_id = l.source_id"
        )
        row = None
        if location_id:
            row = self.conn.execute(
                f"{base} WHERE l.location_id = ?", (location_id,)
            ).fetchone()
        elif path:
            row = self.conn.execute(f"{base} WHERE l.path = ?", (path,)).fetchone()
            if row is None:
                row = self.conn.execute(
                    f"{base} WHERE lower(l.path) = lower(?) LIMIT 1", (path,)
                ).fetchone()
        else:
            raise ToolError("请提供 location_id 或 path")
        if row is None:
            raise ToolError("未找到该文件；请先用 search_knowledge 获取 location_id")
        if row["availability"] != "AVAILABLE":
            raise ToolError(f"文件当前不可检索：{row['availability']}")
        if row["source_state"] != "active":
            raise ToolError("所属数据源已暂停，恢复后才能读取")
        return row

    def _document_text(self, args: dict) -> dict:
        location = self._resolve_location(args)
        page = args.get("page")
        if page is not None:
            try:
                page = int(page)
            except (TypeError, ValueError) as exc:
                raise ToolError("参数 page 必须是整数") from exc
            if page < 1:
                raise ToolError("参数 page 必须大于 0")
        sheet = _string(args, "sheet", required=False)
        max_chars = _integer(args, "max_chars", DEFAULT_MAX_CHARS, 100, MAX_MAX_CHARS)

        version = self.conn.execute(
            "SELECT * FROM content_versions WHERE asset_id = ? AND is_current = 1"
            " ORDER BY observed_at DESC LIMIT 1",
            (location["asset_id"],),
        ).fetchone()
        if version is None:
            raise ToolError("该文件还没有内容版本，请先在桌面端扫描")

        conditions = ["c.version_id = ?"]
        params: list[Any] = [version["version_id"]]
        if page is not None:
            conditions.append("c.page = ?")
            params.append(page)
        if sheet:
            conditions.append("c.sheet = ?")
            params.append(sheet)
        rows = self.conn.execute(
            "SELECT c.page, c.sheet, c.ordinal, c.text, a.parser_signature"
            " FROM chunks c JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
            f" WHERE {' AND '.join(conditions)}",
            params,
        ).fetchall()

        chunks = [
            {
                "page": row["page"],
                "sheet": row["sheet"],
                "ordinal": row["ordinal"],
                "text": row["text"],
                "stage": _stage_of(row["parser_signature"]),
            }
            for row in rows
        ]
        chunks.sort(
            key=lambda item: (
                item["page"] is None,
                item["page"] or 0,
                item["sheet"] or "",
                item["ordinal"],
            )
        )
        selected = self._select_chunks(location["ext"], chunks)

        sections: list[dict] = []
        for chunk in selected:
            key = (chunk["page"], chunk["sheet"])
            if sections and sections[-1]["_key"] == key:
                sections[-1]["parts"].append(chunk["text"])
                sections[-1]["stages"].add(chunk["stage"])
            else:
                sections.append(
                    {"_key": key, "parts": [chunk["text"]], "stages": {chunk["stage"]}}
                )

        artifacts = [
            {
                "stage": _stage_of(row["parser_signature"]),
                "state": row["state"],
                "quality": row["quality"],
                "message": row["message"],
            }
            for row in self.conn.execute(
                "SELECT parser_signature, state, quality, message FROM parse_artifacts"
                " WHERE version_id = ? ORDER BY created_at",
                (version["version_id"],),
            )
        ]

        remaining = max_chars
        total_chars = 0
        output_sections: list[dict] = []
        truncated = False
        for section in sections:
            page_number, sheet_name = section["_key"]
            text = "\n".join(section["parts"])
            header_parts = []
            if page_number:
                header_parts.append(f"第 {page_number} 页")
            if sheet_name:
                header_parts.append(f"工作表 {sheet_name}")
            header = f"[{' · '.join(header_parts)}]\n" if header_parts else ""
            body = header + text
            total_chars += len(text)
            if remaining <= 0:
                truncated = True
                break
            if len(body) > remaining:
                body = body[:remaining]
                truncated = True
            remaining -= len(body)
            output_sections.append(
                {
                    "page": page_number,
                    "sheet": sheet_name,
                    "stage": "+".join(sorted(section["stages"])),
                    "text": body,
                }
            )
        return {
            "location": {
                "location_id": location["location_id"],
                "asset_id": location["asset_id"],
                "name": Path(location["path"]).name,
                "path": location["path"],
                "rel_path": location["rel_path"],
                "ext": location["ext"],
                "size": location["size"],
                "mtime": (
                    datetime.fromtimestamp(location["mtime_ns"] / 1e9)
                    .astimezone()
                    .isoformat(timespec="seconds")
                    if location["mtime_ns"] else None
                ),
                "source_id": location["source_id"],
                "source_label": location["source_label"],
                "source_root": location["source_root"],
            },
            "version_id": version["version_id"],
            "observed_at": version["observed_at"],
            "sections": output_sections,
            "text": "\n".join(section["text"] for section in output_sections),
            "total_chars": total_chars,
            "truncated": truncated,
            "artifacts": artifacts,
        }

    @staticmethod
    def _select_chunks(ext: str, chunks: list[dict]) -> list[dict]:
        """PDF 优先使用 OCR 补页，PPTX 合并正文与图片 OCR，其余优先 L1。"""
        if not chunks:
            return []
        if ext == "pptx":
            return sorted(
                chunks,
                key=lambda item: (
                    item["page"] is None,
                    item["page"] or 0,
                    item["stage"] == "L2",
                    item["ordinal"],
                ),
            )
        if ext == "pdf":
            groups: dict[tuple, dict[str, list[dict]]] = {}
            for chunk in chunks:
                key = (chunk["page"], chunk["sheet"])
                groups.setdefault(key, {"L1": [], "L2": []})[chunk["stage"]].append(chunk)
            selected: list[dict] = []
            for key in sorted(groups, key=lambda item: (item[0] is None, item[0] or 0)):
                bucket = groups[key]
                selected.extend(bucket["L2"] or bucket["L1"])
            return selected
        l1 = [chunk for chunk in chunks if chunk["stage"] == "L1"]
        return l1 or [chunk for chunk in chunks if chunk["stage"] == "L2"]

    def _list_sources(self) -> dict:
        items = []
        for row in store.list_sources(self.conn):
            stats = store.source_stats(self.conn, row["source_id"])
            items.append(
                {
                    "source_id": row["source_id"],
                    "label": row["label"],
                    "root_uri": row["root_uri"],
                    "state": row["state"],
                    "health": row["health"],
                    "last_scan_at": row["last_scan_at"],
                    "available": stats["available"],
                    "stale": stats["stale"],
                    "missing": stats["missing"],
                    "offline": stats["offline"],
                    "last_run": stats["last_run"],
                }
            )
        return {"total": len(items), "items": items}

    def _stats(self) -> dict:
        return search.stats(self.conn)

    # ---------------------------------------------------------------- resources
    def _resources(self) -> list[dict]:
        return [
            {
                "uri": "shiyuan://sources",
                "name": "Authorized sources",
                "description": "Authorized folders with scan health and file counts.",
                "mimeType": "application/json",
            },
            {
                "uri": "shiyuan://stats",
                "name": "Knowledge base stats",
                "description": "Coverage, file types, OCR status and maintenance counters.",
                "mimeType": "application/json",
            },
        ]

    def _read_resource(self, params: dict) -> dict:
        uri = params.get("uri")
        if uri == "shiyuan://sources":
            payload: Any = self._list_sources()
        elif uri == "shiyuan://stats":
            payload = self._stats()
        else:
            raise _ResourceNotFound(f"Unknown resource: {uri}")
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }


class _ResourceNotFound(RuntimeError):
    """resources/read 找不到 URI 时转为 JSON-RPC 错误。"""


def serve(
    conn: sqlite3.Connection,
    reader: BinaryIO | None = None,
    writer: BinaryIO | None = None,
    *,
    once: bool = False,
) -> None:
    """按行读写 JSON-RPC；stdout 只允许协议消息。"""
    server = MCPServer(conn)
    reader = reader or _binary_stream("stdin", 0, "rb")
    writer = writer or _binary_stream("stdout", 1, "wb")
    for raw in reader:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            payload = _error(None, -32700, "Parse error")
        else:
            payload = server.handle(message)
        if payload is not None:
            writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            writer.flush()
        if once:
            break


def server_command() -> list[str]:
    """MCP 启动命令：源码模式走 python -m，安装版走 shiyuan-core.exe --mcp。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mcp"]
    return [sys.executable, "-m", "shiyuan.mcp_server"]


def config_snippet(token: str | None = None) -> dict:
    env = {"SHIYUAN_HOME": str(config.app_home())}
    if token:
        env["SHIYUAN_EXT_TOKEN"] = token
    if getattr(sys, "frozen", False):
        server: dict[str, Any] = {
            "command": sys.executable, "args": ["--mcp"], "env": env,
        }
    else:
        server = {
            "command": sys.executable,
            "args": ["-m", "shiyuan.mcp_server"],
            "cwd": str(Path(__file__).resolve().parents[1]),
            "env": env,
        }
    return {"mcpServers": {"shiyuan": server}}


def _verify_token(path: Path, token: str) -> str | None:
    """校验外部连接令牌；返回 client_id，非法令牌直接终止进程。"""
    conn = db.connect(path)
    try:
        db.init_db(conn)
        row = external.match_token(conn, token)
        if row is None:
            line = "Shiyuan MCP: invalid external token\n"
            if sys.stderr is not None:
                sys.stderr.write(line)
            else:
                os.write(2, line.encode("utf-8"))
            raise SystemExit(2)
        external.record_usage(conn, row["client_id"])
        return row["client_id"]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Shiyuan MCP server (stdio, read-only)")
    parser.add_argument("--db", help="SQLite path; defaults to the desktop app database")
    parser.add_argument("--token", help="external client token (or SHIYUAN_EXT_TOKEN)")
    parser.add_argument(
        "--print-config", action="store_true",
        help="print an MCP client JSON snippet and exit",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="handle a single JSON-RPC message and exit (used by connection test)",
    )
    args = parser.parse_args(argv)

    token = (args.token or os.environ.get("SHIYUAN_EXT_TOKEN", "")).strip()

    if args.print_config:
        # 控制台编码因系统而异；Unicode 转义保证中文路径在任何客户端复制后仍然有效。
        text = json.dumps(config_snippet(token or None), ensure_ascii=True, indent=2)
        if sys.stdout is not None:
            print(text)
        else:
            os.write(1, (text + "\n").encode("ascii"))
        return

    path = Path(args.db) if args.db else db.db_path()
    if token:
        _verify_token(path, token)
    conn = db.connect(path)
    try:
        db.init_db(conn)
        conn.execute("PRAGMA query_only = ON")
        # 后台预热向量矩阵与本地模型，避免 Agent 的第一次检索等冷启动。
        embeddings.warm_cache_async()
        serve(conn, once=args.once)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
