"""资产盘点与内容策略分类（P0）。

这一层只做轻量识别，不读取正文、不做 OCR、不做 Embedding：
- 扩展名、精确文件名、目录规则；
- Magic Bytes 冲突检测；
- JAR 包结构识别；
- 无扩展名文件的 4 KB 文本嗅探。

输出是 dry-run 报告，不写入数据库。真正应用策略前由用户确认。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

RULES_VERSION = "inventory.v1"
SAMPLE_LIMIT_DEFAULT = 5
POLICY_KEY = "inventory_policies"
# 盘点策略确认后才新增支持的可解析类型：老数据源只要实际存在这类文件，
# 就补进解析队列；用户显式设置过的类型仍以策略为准。
NEWLY_SUPPORTED_EXTS = {"pl"}

POLICIES = {"deep_parse", "standard_parse", "metadata_only", "exclude_content"}

_COMPILE_EXT = {
    "class", "pdb", "obj", "ilk", "idb", "exp",
}
_BUILD_EXT = {
    "tlog", "index", "prefs", "cache", "sf", "rsa", "launch",
    "lastbuildstate", "sublime-package", "sublime-session", "sublime-settings",
    "rcache", "lnk", "inf",
}
_SOFTWARE_EXT = {
    "exe", "dll", "sys", "msi", "msix", "appx", "cab",
    "iso", "img", "dmg", "pkg", "deb", "rpm", "apk", "ipa",
}
_DEPENDENCY_EXT = {
    "jar", "war", "ear", "pyc", "pyo", "pyd", "so", "dylib", "a", "lib",
}
_DESIGN_EXT = {"psd", "ai", "fla"}
_RAW_PHOTO_EXT = {"cr2", "arw", "dng", "nef", "orf", "rw2", "raf", "srw", "pef", "xmp"}
_OCR_IMAGE_EXT = {"png", "jpg", "jpeg", "jfif", "bmp", "tif", "tiff", "webp"}
_GIF_EXT = {"gif"}
_FONT_EXT = {"ttf", "otf", "woff", "woff2", "eot"}
_MEDIA_EXT = {
    "mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma", "aiff",
    "mp4", "m4v", "mov", "wmv", "avi", "mkv", "webm", "flv", "mid", "swf",
}
_RESEARCH_EXT = {
    "fasta", "fa", "fna", "gff", "gtf", "vcf", "fastq", "bam", "cram", "h5",
    "ssr", "stat",
}
_IDE_PROJECT_EXT = {"sln", "vcxproj", "filters", "iml"}
_ARCHIVE_OTHER_EXT = {"tar", "gz", "bz2", "xz"}

# 默认只登记、不解析的非知识文件：网页、前端脚本、日志、结构化数据与压缩包。
# 资料库里这类内容多为工具/教程产物，全量索引进正文会淹没真正的文档；
# 用户仍可在盘点策略里把个别类型改成 standard_parse 显式解析。
_NON_KNOWLEDGE_EXT = {
    "html", "htm", "css", "xml", "xsl", "xslt", "svg", "top", "json", "ipynb",
    "js", "jsx", "ts", "tsx", "log", "zip", "7z", "rar",
    # 工具/项目配置与字幕：默认只登记，避免把 IDE、Tomcat、字幕塞进正文索引。
    "properties", "ass",
}

_DOCUMENT_EXT = {"pdf", "doc", "docx", "odt", "rtf"}
_SHEET_EXT = {"xls", "xlsx", "xlsm", "csv", "tsv", "ods"}
_PRESENTATION_EXT = {"ppt", "pptx", "pptm", "odp"}
_TEXT_EXT = {
    "txt", "md", "markdown", "rst",
    "ini", "cfg", "conf", "toml", "yaml", "yml",
}
_CODE_EXT = {
    "py", "java", "c", "h", "cpp", "hpp", "cs",
    "go", "rs", "rb", "php", "sh", "bat", "cmd", "ps1", "sql", "r", "m", "tex",
    "bib", "jsp", "kt", "kts", "scala", "lua", "pl", "vue", "svelte",
}
_EMAIL_EXT = {"eml", "msg", "mbox"}
_CAD_EXT = {"dxf", "dwg"}

_EXACT_CACHE_NAMES = {
    "thumbs.db", "desktop.ini", ".ds_store",
}
_IDE_FILE_NAMES = {
    ".classpath", ".project", ".lastbuildstate",
}

_IDE_DIRS = {
    ".metadata", ".plugins", ".settings", ".vscode", ".vs",
    ".pytest_cache", ".mypy_cache", ".cache",
}
_DEPENDENCY_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv",
}
_CACHE_MARKERS = (
    ((".m2", "repository"), "maven_cache"),
    ((".gradle", "caches"), "gradle_cache"),
    ((".nuget",), "nuget_cache"),
    ((".git", "objects"), "git_objects"),
)


def _result(category: str, subcategory: str, policy: str, reason: str,
            confidence: str = "high", *, skip_reason: str | None = None,
            detected_format: str | None = None, conflict: bool = False,
            jar: dict | None = None, system_excluded: bool = False) -> dict:
    return {
        "category": category,
        "subcategory": subcategory,
        "content_policy": policy,
        "skip_reason": skip_reason,
        "reason": reason,
        "confidence": confidence,
        "detected_format": detected_format,
        "conflict": conflict,
        "jar": jar,
        "system_excluded": system_excluded,
    }


def _head_bytes(path: Path, size: int | None = None) -> bytes:
    if size == 0:
        return b""
    try:
        with path.open("rb") as handle:
            return handle.read(16)
    except OSError:
        return b""


def _detect_format(head: bytes) -> str | None:
    if not head:
        return "EMPTY"
    if head.startswith(b"MZ"):
        return "PE"
    if head.startswith(b"\x7fELF"):
        return "ELF"
    if head.startswith(b"\xca\xfe\xba\xbe"):
        return "JAVA_CLASS"
    if head.startswith(b"PK\x03\x04"):
        return "ZIP"
    if head.startswith(b"%PDF"):
        return "PDF"
    if head.startswith(b"\x89PNG"):
        return "PNG"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"GIF8"):
        return "GIF"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "OLE"
    return None


def _jar_info(path: Path) -> dict:
    info: dict[str, Any] = {
        "class_count": 0,
        "has_manifest": False,
        "has_maven_metadata": False,
        "sources": "sources" in path.name.lower(),
        "javadoc": "javadoc" in path.name.lower(),
        "error": None,
    }
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            info["has_manifest"] = any(
                name.upper() == "META-INF/MANIFEST.MF" for name in names
            )
            info["has_maven_metadata"] = any(
                name.startswith("META-INF/maven/") and name.endswith("pom.properties")
                for name in names
            )
            info["class_count"] = sum(1 for name in names if name.endswith(".class"))
    except Exception as exc:  # 损坏、被占用、不是 zip
        info["error"] = str(exc)[:160]
    return info


def _sniff_no_ext(path: Path) -> tuple[str, str]:
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError as exc:
        return "unreadable", str(exc)[:160]
    if not head:
        return "empty", "空文件"
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "text", "UTF-16 文本"
    if b"\x00" in head:
        return "binary", "包含 NUL 字节的二进制内容"
    for encoding in ("utf-8", "gb18030"):
        try:
            head.decode(encoding)
        except UnicodeDecodeError:
            continue
        return "text", f"{encoding} 文本"
    return "binary", "无法按文本解码"


def _path_rule(parts: list[str]) -> dict | None:
    lowered = [part.lower() for part in parts]
    if "eclipse" in lowered and any(
        part in {"configuration", "p2", "features", "plugins"} for part in lowered
    ):
        return _result(
            "ide_metadata", "eclipse_runtime", "exclude_content",
            "位于 Eclipse 安装/工作区元数据目录", "high",
            skip_reason="non_knowledge_ide_cache", system_excluded=True,
        )
    if any(part in _IDE_DIRS for part in lowered):
        return _result(
            "ide_metadata", "ide_directory", "exclude_content",
            "位于 IDE/工具元数据目录", "high",
            skip_reason="non_knowledge_ide_cache", system_excluded=True,
        )
    if any(part in _DEPENDENCY_DIRS for part in lowered):
        return _result(
            "dependency", "dependency_directory", "exclude_content",
            "位于依赖/字节码缓存目录", "high",
            skip_reason="non_knowledge_dependency", system_excluded=True,
        )
    for markers, subcategory in _CACHE_MARKERS:
        if all(marker in lowered for marker in markers):
            return _result(
                "cache", subcategory, "exclude_content",
                "位于依赖/构建缓存目录", "high",
                skip_reason="non_knowledge_ide_cache", system_excluded=True,
            )
    return None


def classify(path: str | Path, size: int | None = None) -> dict:
    """轻量分类；只读扩展名、路径、文件头和 JAR 目录，不读正文。"""
    target = Path(path)
    ext = target.suffix.lower().lstrip(".")
    name = target.name.lower()
    parts = list(target.parts)

    # 精确文件名优先于扩展名
    if name.startswith("~$"):
        return _result("cache", "office_lock", "exclude_content",
                       "Office 临时锁文件", "high",
                       skip_reason="non_knowledge_temp_file", detected_format="OFFICE_LOCK",
                       system_excluded=True)
    if name in _EXACT_CACHE_NAMES:
        return _result("cache", name, "exclude_content",
                       "系统/工具缓存文件", "high",
                       skip_reason="non_knowledge_temp_file", system_excluded=True)
    if name in _IDE_FILE_NAMES:
        return _result("ide_metadata", name.lstrip("."), "exclude_content",
                       "IDE 工程元数据文件", "high",
                       skip_reason="non_knowledge_ide_cache", system_excluded=True)

    path_rule = _path_rule(parts)
    if path_rule is not None:
        return path_rule

    head = _head_bytes(target, size)
    detected = _detect_format(head)

    if ext in _COMPILE_EXT:
        return _result("dependency", ext, "exclude_content",
                       "编译产物，不进入正文档队列", "high",
                       skip_reason="non_knowledge_compile_artifact",
                       detected_format=detected)
    if ext == "mf":
        return _result("dependency", "manifest", "metadata_only",
                       "JAR Manifest/工具清单，仅保留元数据", "medium",
                       skip_reason="non_knowledge_dependency",
                       detected_format=detected)
    if ext in _BUILD_EXT:
        return _result("ide_metadata", ext, "exclude_content",
                       "IDE/构建辅助文件，不进入正文档队列", "high",
                       skip_reason="non_knowledge_ide_cache",
                       detected_format=detected)
    if ext in _SOFTWARE_EXT:
        return _result("software", ext, "metadata_only",
                       "软件/运行库，仅保留资产元数据", "high",
                       skip_reason="non_knowledge_runtime",
                       detected_format=detected)
    if ext in _DEPENDENCY_EXT:
        if ext == "jar":
            info = _jar_info(target)
            lowered = [part.lower() for part in parts]
            if "web-inf" in lowered and "lib" in lowered:
                subcategory = "jar_dependency"
                reason = "WEB-INF/lib 依赖 JAR，仅保留元数据"
            elif info["sources"]:
                subcategory = "jar_sources"
                reason = "sources JAR，仅保留元数据；可选解析源码"
            elif info["javadoc"]:
                subcategory = "jar_javadoc"
                reason = "javadoc JAR，仅保留元数据；可选解析文档"
            elif info["has_maven_metadata"] and info["class_count"]:
                subcategory = "jar_library"
                reason = f"第三方库 JAR（{info['class_count']} 个 class），仅保留元数据"
            elif info["error"]:
                subcategory = "jar_unknown"
                reason = f"JAR 无法读取（{info['error']}），仅保留元数据"
            else:
                subcategory = "jar_custom"
                reason = f"JAR 包（{info['class_count']} 个 class），默认仅元数据"
            return _result(
                "dependency", subcategory, "metadata_only", reason,
                "medium" if info["error"] else "high",
                skip_reason="non_knowledge_dependency",
                detected_format=detected, jar=info,
            )
        return _result("dependency", ext, "metadata_only",
                       "依赖/编译产物，仅保留元数据", "high",
                       skip_reason="non_knowledge_dependency",
                       detected_format=detected)
    if ext in _DESIGN_EXT:
        return _result("design", ext, "metadata_only",
                       "设计源文件，保留资产元数据", "high",
                       detected_format=detected)
    if ext in _RAW_PHOTO_EXT:
        return _result("photo", ext, "metadata_only",
                       "用户 RAW 照片，保留资产并预留 EXIF", "high",
                       detected_format=detected)
    if ext in _GIF_EXT:
        return _result("image", "gif", "metadata_only",
                       "GIF 图片，默认仅元数据，可选首帧 OCR", "high",
                       detected_format=detected)
    if ext in _OCR_IMAGE_EXT:
        return _result("image", ext, "deep_parse",
                       "图片资产，按深度策略执行 OCR", "high",
                       detected_format=detected)
    if ext in _NON_KNOWLEDGE_EXT:
        return _result("non_knowledge", ext, "metadata_only",
                       "网页/前端脚本/日志/结构化数据/压缩包，默认仅登记不解析",
                       "high", skip_reason="non_knowledge_content",
                       detected_format=detected)
    if ext in _DOCUMENT_EXT:
        return _result("document", ext, "standard_parse",
                       "文档，执行标准正文解析", "high",
                       detected_format=detected)
    if ext in _SHEET_EXT:
        return _result("spreadsheet", ext, "standard_parse",
                       "表格，执行标准正文解析", "high",
                       detected_format=detected)
    if ext in _PRESENTATION_EXT:
        return _result("presentation", ext, "standard_parse",
                       "演示文稿，执行标准正文解析（不执行宏）", "high",
                       detected_format=detected)
    if ext in _TEXT_EXT:
        return _result("text", ext, "standard_parse",
                       "文本/配置，执行标准正文解析", "high",
                       detected_format=detected)
    if ext in _CODE_EXT:
        return _result("code", ext, "standard_parse",
                       "源代码，执行标准正文解析", "high",
                       detected_format=detected)
    if ext in _EMAIL_EXT:
        return _result("email", ext, "standard_parse",
                       "邮件，执行标准正文解析", "high",
                       detected_format=detected)
    if ext in _ARCHIVE_OTHER_EXT:
        return _result("archive", ext, "metadata_only",
                       "暂未支持的归档格式，仅保留元数据", "medium",
                       detected_format=detected)
    if ext in _RESEARCH_EXT:
        return _result("research", ext, "metadata_only",
                       "科研专业数据，预留流式摘要解析", "high",
                       detected_format=detected)
    if ext in _MEDIA_EXT:
        return _result("media", ext, "metadata_only",
                       "音视频，仅保留元数据", "high",
                       detected_format=detected)
    if ext in _CAD_EXT:
        if ext == "dwg":
            return _result("cad", "dwg", "metadata_only",
                           "DWG 需要外部转换器，默认仅元数据", "medium",
                           detected_format=detected)
        return _result("cad", ext, "standard_parse",
                       "DXF 文本/结构解析", "high",
                       detected_format=detected)
    if ext in _IDE_PROJECT_EXT:
        return _result("ide_project", ext, "metadata_only",
                       "IDE 工程配置，默认仅元数据", "high",
                       detected_format=detected)
    if ext in _FONT_EXT:
        return _result("font", ext, "metadata_only",
                       "字体文件，仅保留元数据", "high",
                       detected_format=detected)

    if not ext:
        sniff, note = _sniff_no_ext(target)
        if sniff == "text":
            return _result("text", "no_extension", "metadata_only",
                           f"无扩展名，{note}；保护性仅元数据", "medium",
                           detected_format=detected)
        if sniff == "empty":
            return _result("unknown", "empty", "metadata_only",
                           "空文件", "high", detected_format="EMPTY")
        return _result("unknown", "no_extension_binary", "metadata_only",
                       f"无扩展名，{note}；仅保留元数据", "medium",
                       detected_format=detected)

    # 扩展名与 Magic Bytes 明显冲突时，降级为仅元数据
    if detected in {"PE", "ELF", "JAVA_CLASS"} and ext not in _SOFTWARE_EXT | _COMPILE_EXT:
        return _result("unknown", "format_conflict", "metadata_only",
                       f"扩展名 .{ext} 与文件头 {detected} 冲突，保护性仅元数据",
                       "low", skip_reason="format_conflict",
                       detected_format=detected, conflict=True)

    return _result("unknown", ext, "metadata_only",
                   "未知扩展名，保护性仅元数据", "low",
                   skip_reason="unknown_format", detected_format=detected)


def _inventory_rows(conn: sqlite3.Connection, source_id: str | None) -> list[sqlite3.Row]:
    sql = [
        "SELECT l.location_id, l.path, l.ext, l.size, l.source_id, s.label AS source_label",
        "FROM asset_locations l JOIN sources s ON s.source_id = l.source_id",
        "WHERE l.availability = 'AVAILABLE' AND s.state = 'active'",
    ]
    params: list = []
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    return conn.execute(" ".join(sql), params).fetchall()


def _policy_key(source_id: str | None) -> str:
    return POLICY_KEY if not source_id else f"{POLICY_KEY}:{source_id}"


def load_policies(conn: sqlite3.Connection, source_id: str | None = None) -> dict[str, str]:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (_policy_key(source_id),)
    ).fetchone()
    if row is None:
        return {}
    try:
        stored = json.loads(row["value"])
    except (TypeError, ValueError):
        return {}
    if isinstance(stored, dict) and isinstance(stored.get("policies"), dict):
        stored = stored["policies"]
    if not isinstance(stored, dict):
        return {}
    return {
        str(key).lower(): value
        for key, value in stored.items()
        if isinstance(key, str) and value in POLICIES
    }


def is_confirmed(conn: sqlite3.Connection, source_id: str) -> bool:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (_policy_key(source_id),)
    ).fetchone()
    if row is None:
        return False
    try:
        stored = json.loads(row["value"])
    except (TypeError, ValueError):
        return False
    if isinstance(stored, dict) and stored.get("confirmed") is True:
        return True
    return bool(load_policies(conn, source_id))


def allowed_exts(conn: sqlite3.Connection, source_id: str) -> set[str] | None:
    """返回允许进入正文/OCR 队列的扩展名；None 表示未设置策略，保持旧行为。"""
    policies = load_policies(conn, source_id)
    if not policies:
        return None
    allowed = {
        ext for ext, policy in policies.items()
        if policy in {"standard_parse", "deep_parse"}
    }
    for ext in NEWLY_SUPPORTED_EXTS:
        if ext in policies:
            continue
        present = conn.execute(
            "SELECT 1 FROM asset_locations WHERE source_id = ? AND lower(ext) = ?"
            " LIMIT 1", (source_id, ext)).fetchone()
        if present is not None:
            allowed.add(ext)
    return allowed


def save_policies(conn: sqlite3.Connection, policies: dict,
                  source_id: str | None = None) -> dict[str, str]:
    if not isinstance(policies, dict):
        raise ValueError("policies 必须是对象")
    normalized: dict[str, str] = {}
    for key, value in policies.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("类型和策略必须是字符串")
        name = key.strip().lower().lstrip(".")
        if not name:
            name = "none"
        if value not in POLICIES:
            raise ValueError(f"未知策略：{value}")
        normalized[name] = value
    payload = {
        "policies": normalized,
        "confirmed": True,
        "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_policy_key(source_id), json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return normalized


def file_types(conn: sqlite3.Connection, *, source_id: str | None = None,
               sample_limit: int = 2) -> dict:
    """按扩展名聚合盘点结果，供 File types 选择界面使用。"""
    rows = _inventory_rows(conn, source_id)
    saved = load_policies(conn, source_id)
    confirmed = is_confirmed(conn, source_id) if source_id else bool(saved)
    groups: dict[str, dict] = {}
    total_files = 0
    total_bytes = 0
    for row in rows:
        size = int(row["size"] or 0)
        info = classify(row["path"], size)
        key = (row["ext"] or "").lower() or "none"
        group = groups.get(key)
        if group is None:
            group = {
                "ext": key,
                "count": 0,
                "bytes": 0,
                "effective_count": 0,
                "effective_bytes": 0,
                "system_excluded_count": 0,
                "system_excluded_bytes": 0,
                "categories": {},
                "recommendations": {},
                "subcategories": {},
                "reasons": set(),
                "confidence": info["confidence"],
                "samples": [],
            }
            groups[key] = group
        total_files += 1
        total_bytes += size
        group["count"] += 1
        group["bytes"] += size
        if info["system_excluded"]:
            group["system_excluded_count"] += 1
            group["system_excluded_bytes"] += size
            continue
        group["effective_count"] += 1
        group["effective_bytes"] += size
        group["categories"][info["category"]] = group["categories"].get(info["category"], 0) + 1
        group["recommendations"][info["content_policy"]] = (
            group["recommendations"].get(info["content_policy"], 0) + 1
        )
        group["subcategories"][info["subcategory"]] = (
            group["subcategories"].get(info["subcategory"], 0) + 1
        )
        group["reasons"].add(info["reason"])
        if info["confidence"] == "low":
            group["confidence"] = "low"
        elif info["confidence"] == "medium" and group["confidence"] != "low":
            group["confidence"] = "medium"
        if len(group["samples"]) < sample_limit:
            group["samples"].append({"path": row["path"], "size": size})

    items = []
    for key, group in groups.items():
        recommended = (
            max(group["recommendations"].items(), key=lambda item: item[1])[0]
            if group["recommendations"] else "exclude_content"
        )
        category = (
            max(group["categories"].items(), key=lambda item: item[1])[0]
            if group["categories"] else "system"
        )
        items.append({
            "ext": key,
            "label": f".{key}" if key != "none" else "无扩展名",
            "count": group["count"],
            "bytes": group["bytes"],
            "effective_count": group["effective_count"],
            "effective_bytes": group["effective_bytes"],
            "system_excluded_count": group["system_excluded_count"],
            "system_excluded_bytes": group["system_excluded_bytes"],
            "count_pct": round(group["count"] / total_files * 100, 2) if total_files else 0,
            "bytes_pct": round(group["bytes"] / total_bytes * 100, 2) if total_bytes else 0,
            "category": category,
            "recommended_policy": recommended,
            "selected_policy": saved.get(key),
            "confidence": group["confidence"],
            "reasons": sorted(group["reasons"]),
            "subcategories": dict(sorted(
                group["subcategories"].items(), key=lambda item: -item[1]
            )),
            "samples": group["samples"],
        })
    items.sort(key=lambda item: (-item["effective_count"], -item["count"], item["label"]))
    effective_files = sum(item["effective_count"] for item in items)
    effective_bytes = sum(item["effective_bytes"] for item in items)
    system_files = sum(item["system_excluded_count"] for item in items)
    system_bytes = sum(item["system_excluded_bytes"] for item in items)
    return {
        "items": items,
        "policies": saved,
        "confirmed": confirmed,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "effective_files": effective_files,
        "effective_bytes": effective_bytes,
        "system_excluded_files": system_files,
        "system_excluded_bytes": system_bytes,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rules_version": RULES_VERSION,
    }


def dry_run(conn: sqlite3.Connection, *, source_id: str | None = None,
            sample_limit: int = SAMPLE_LIMIT_DEFAULT) -> dict:
    """对当前已登记资产做分类 dry-run，不写入分类或策略。"""
    rows = _inventory_rows(conn, source_id)

    groups: dict[tuple, dict] = {}
    policies: dict[str, dict] = {}
    skip_reasons: dict[str, dict] = {}
    total_files = 0
    total_bytes = 0
    conflicts: list[dict] = []

    for row in rows:
        size = int(row["size"] or 0)
        info = classify(row["path"], size)
        total_files += 1
        total_bytes += size
        key = (info["category"], info["subcategory"], info["content_policy"],
               info["skip_reason"] or "")
        group = groups.get(key)
        if group is None:
            group = {
                "category": info["category"],
                "subcategory": info["subcategory"],
                "content_policy": info["content_policy"],
                "skip_reason": info["skip_reason"],
                "count": 0,
                "bytes": 0,
                "confidence": info["confidence"],
                "reasons": set(),
                "samples": [],
            }
            groups[key] = group
        group["count"] += 1
        group["bytes"] += size
        group["reasons"].add(info["reason"])
        if info["confidence"] == "low":
            group["confidence"] = "low"
        elif info["confidence"] == "medium" and group["confidence"] != "low":
            group["confidence"] = "medium"
        if len(group["samples"]) < sample_limit:
            group["samples"].append({
                "path": row["path"],
                "size": size,
                "source": row["source_label"],
                "reason": info["reason"],
            })
        for bucket, key_name in ((policies, info["content_policy"]),
                                 (skip_reasons, info["skip_reason"] or "none")):
            entry = bucket.get(key_name)
            if entry is None:
                entry = {"count": 0, "bytes": 0}
                bucket[key_name] = entry
            entry["count"] += 1
            entry["bytes"] += size
        if info["conflict"] and len(conflicts) < 20:
            conflicts.append({"path": row["path"], "reason": info["reason"]})

    categories = []
    for group in groups.values():
        categories.append({
            **{key: value for key, value in group.items() if key != "reasons"},
            "reasons": sorted(group["reasons"]),
            "count_pct": round(group["count"] / total_files * 100, 2) if total_files else 0,
            "bytes_pct": round(group["bytes"] / total_bytes * 100, 2) if total_bytes else 0,
        })
    categories.sort(key=lambda item: (-item["count"], item["category"], item["subcategory"]))
    for bucket in (policies, skip_reasons):
        for entry in bucket.values():
            entry["count_pct"] = round(entry["count"] / total_files * 100, 2) if total_files else 0
            entry["bytes_pct"] = round(entry["bytes"] / total_bytes * 100, 2) if total_bytes else 0

    return {
        "rules_version": RULES_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_id": source_id,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "policies": dict(sorted(policies.items(), key=lambda item: -item[1]["count"])),
        "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda item: -item[1]["count"])),
        "categories": categories,
        "conflicts": conflicts,
    }


def _print_table(report: dict) -> None:
    print(f"rules={report['rules_version']} files={report['total_files']} "
          f"bytes={report['total_bytes']} generated={report['generated_at']}")
    print("\nPOLICIES")
    for name, entry in report["policies"].items():
        print(f"  {name:16} {entry['count']:6} files  {entry['bytes']/1024/1024:10.1f} MB  {entry['count_pct']:5.1f}%")
    print("\nTOP CATEGORIES")
    for item in report["categories"][:30]:
        print(f"  {item['category']}/{item['subcategory']:22} {item['content_policy']:15} "
              f"{item['count']:6} files  {item['bytes']/1024/1024:9.1f} MB  "
              f"conf={item['confidence']}")
    print("\nSKIP REASONS")
    for name, entry in report["skip_reasons"].items():
        print(f"  {name:28} {entry['count']:6} files  {entry['bytes']/1024/1024:9.1f} MB")
    if report["conflicts"]:
        print("\nCONFLICTS")
        for item in report["conflicts"][:10]:
            print(f"  {item['path']} :: {item['reason']}")


def main() -> int:
    from . import db

    parser = argparse.ArgumentParser(description="拾源资产分类 dry-run")
    parser.add_argument("--source-id")
    parser.add_argument("--samples", type=int, default=SAMPLE_LIMIT_DEFAULT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = db.connect()
    try:
        report = dry_run(conn, source_id=args.source_id, sample_limit=max(1, args.samples))
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
