"""文档家族：把同一份知识的不同版本文件归并到逻辑家族。

原则：
- 原文件不移动、不删除、不改名；
- 只建立版本关系，不推荐、不裁定哪个版本更好；
- 识别依据优先使用内容 hash 与文件名规范化，其次才用文本 SimHash；
- 文件名高度相似但正文差异很大的文件不会被归并；
- 用户可以随时通过接口重建家族关系。

检索层按家族聚合，并按段落内容去重：重复段落合并来源，各版本独有或
修改过的段落仍然保留，引用始终可以追溯到具体文件。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path

from . import db, indexer, inventory, knowledge, store

RULES_VERSION = "family.v1"
TEXT_LIMIT = 20_000
SHINGLE_SAMPLE = 600
STRICT_DISTANCE = 8
MAX_FAMILY_SIZE = 60

# 这些名字去掉日期后只剩工具/通用词，不能据此把几百个无关文件并成一家。
_GENERIC_KEYS = {
    "snipaste", "screenshot", "screenshots", "capture", "image", "images",
    "img", "photo", "photos", "pic", "picture", "untitled", "document",
    "scan", "scanned", "wechatimg", "wechat", "wx",
    "微信图片", "微信截图", "屏幕截图", "截图", "未命名", "新建", "文档",
    "扫描", "扫描件", "图片", "照片", "报告", "总结", "笔记", "数据",
    "表格", "简历", "合同", "演示文稿", "幻灯片",
}

_VERSION_TOKEN_RE = re.compile(r"(?:^|[^a-z])(?:v|ver|version)\s*\d+", re.IGNORECASE)

# 只对文档类资产做版本家族；代码、配置、图片和媒体按文件名批量归并会产生大量误判。
_FAMILY_EXTS = {
    "pdf", "doc", "docx", "odt", "rtf",
    "ppt", "pptx", "pptm", "odp",
    "xls", "xlsx", "xlsm", "ods", "csv",
    "md", "markdown", "txt", "tex", "wps",
}

_MARKER_WORDS = (
    "副本", "拷贝", "copy", "backup", "备份", "修复的", "修复", "最新",
    "最终", "终稿", "定稿", "初稿", "修改后", "修改版", "修改", "修订",
    "revised", "draft", "final", "版本", "本科毕业论文", "毕业论文",
    "学位论文", "论文", "设计", "封面", "正文",
)

_DATE_RE = re.compile(r"\d{1,4}[.\-_/]\d{1,2}(?:[.\-_/]\d{1,4})?")
_COPY_RE = re.compile(r"[（(]\s*\d+\s*[)）]")
_VERSION_NUM_RE = re.compile(r"(?:终稿|定稿|final)\s*[（(]?(\d+)", re.IGNORECASE)
_VERSION_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})")

_rebuild_lock = threading.Lock()
_rebuild_thread: threading.Thread | None = None


def normalize_name(name: str) -> str:
    """去掉版本号、日期、副本标记和“论文/封面”等通用词，留下可比较的主体。"""
    stem = Path(name).stem.lower()
    stem = _COPY_RE.sub("", stem)
    stem = _DATE_RE.sub("", stem)
    for word in _MARKER_WORDS:
        stem = stem.replace(word, "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", stem)


def version_label(name: str) -> str:
    lower = Path(name).stem.lower()
    match = _VERSION_NUM_RE.search(lower)
    if match:
        return f"终稿 {match.group(1)}"
    if "终稿" in lower or "定稿" in lower or "final" in lower:
        return "终稿"
    if "最新" in lower or "latest" in lower:
        return "最新"
    dates = _VERSION_DATE_RE.findall(lower)
    if dates:
        return f"{dates[-1][0]}.{dates[-1][1]}"
    for word, label in (
        ("修改后", "修改后"), ("修改版", "修改版"), ("修改", "修改"),
        ("修订", "修订"), ("初稿", "初稿"), ("draft", "初稿"),
        ("副本", "副本"), ("拷贝", "副本"), ("备份", "备份"),
        ("封面", "封面"),
    ):
        if word in lower:
            return label
    return ""


def display_name(name: str) -> str:
    """家族展示名：只去掉版本/副本/日期标记，保留“论文”等主体词。"""
    stem = Path(name).stem
    stem = _COPY_RE.sub("", stem)
    stem = _DATE_RE.sub("", stem)
    for word in (
        "副本", "拷贝", "copy", "backup", "备份", "修复的", "修复", "最新",
        "最终", "终稿", "定稿", "初稿", "修改后", "修改版", "修改", "修订",
        "revised", "draft", "final", "版本", "封面", "正文",
    ):
        stem = stem.replace(word, "")
    cleaned = re.sub(r"\s+", " ", stem).strip(" -_")
    return cleaned or Path(name).stem


def content_hash(text: str) -> str:
    """段落级去重键：忽略空白、标点和大小写后取 SHA-1。"""
    return hashlib.sha1(_normalize_text(text).encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _simhash(text: str) -> int:
    normalized = _normalize_text(text)[:TEXT_LIMIT]
    if len(normalized) < 20:
        return 0
    shingles = [normalized[i:i + 3] for i in range(0, len(normalized) - 2, 3)]
    if not shingles:
        return 0
    step = max(1, len(shingles) // SHINGLE_SAMPLE)
    sampled = shingles[::step][:SHINGLE_SAMPLE]
    vector = [0] * 32
    for shingle in sampled:
        value = hash(shingle) & 0xFFFFFFFF
        for bit in range(32):
            vector[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit in range(32):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _similarity(a: dict, b: dict) -> float:
    if not a["simhash"] or not b["simhash"]:
        return 0.0
    return 1.0 - _hamming(a["simhash"], b["simhash"]) / 32.0


def _same_title(a: dict, b: dict) -> bool:
    if not a["title"] or not b["title"]:
        return False
    return difflib.SequenceMatcher(None, a["title"], b["title"]).ratio() >= 0.6


def _text_for_version(conn: sqlite3.Connection, version_id: str) -> str:
    parts: list[str] = []
    total = 0
    for row in conn.execute(
        "SELECT text FROM chunks WHERE version_id = ? ORDER BY ordinal",
        (version_id,),
    ):
        piece = row["text"] or ""
        parts.append(piece)
        total += len(piece)
        if total >= TEXT_LIMIT:
            break
    return "\n".join(parts)[:TEXT_LIMIT]


def _load_assets(conn: sqlite3.Connection) -> list[dict]:
    locations: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT * FROM asset_locations WHERE availability = 'AVAILABLE'"
        " ORDER BY asset_id, path"
    ):
        locations.setdefault(row["asset_id"], row)

    assets: list[dict] = []
    for asset_id, location in locations.items():
        if (location["ext"] or "").lower() not in _FAMILY_EXTS:
            continue
        if inventory.classify(location["path"], location["size"])["system_excluded"]:
            continue
        version = conn.execute(
            "SELECT version_id, hash FROM content_versions"
            " WHERE asset_id = ? AND is_current = 1"
            " ORDER BY observed_at DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        if version is None:
            continue
        artifact = conn.execute(
            "SELECT char_count FROM parse_artifacts"
            " WHERE version_id = ? AND parser_signature = ?",
            (version["version_id"], indexer.PARSER_SIGNATURE),
        ).fetchone()
        chars = int(artifact["char_count"] or 0) if artifact else 0
        text = _text_for_version(conn, version["version_id"]) if chars > 0 else ""
        name = Path(location["path"]).name
        assets.append({
            "asset_id": asset_id,
            "location_id": location["location_id"],
            "path": location["path"],
            "name": name,
            "dir": str(Path(location["path"]).parent).lower(),
            "size": int(location["size"] or 0),
            "mtime_ns": int(location["mtime_ns"] or 0),
            "hash": version["hash"] or "",
            "version_id": version["version_id"],
            "chars": chars,
            "title": next(
                (line.strip()[:120] for line in text.splitlines() if len(line.strip()) >= 4),
                Path(name).stem,
            ),
            "key": normalize_name(name),
            "simhash": _simhash(text),
            "version_label": version_label(name),
        })
    return assets


def rebuild(conn: sqlite3.Connection) -> dict:
    """重建全部文档家族。原文件不变，只重写逻辑关系表。"""
    assets = _load_assets(conn)
    parent = {asset["asset_id"]: asset["asset_id"] for asset in assets}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(first: str, second: str) -> None:
        root_a, root_b = find(first), find(second)
        if root_a != root_b:
            parent[root_b] = root_a

    by_hash: dict[str, list[dict]] = {}
    for asset in assets:
        if asset["hash"]:
            by_hash.setdefault(asset["hash"], []).append(asset)
    for group in by_hash.values():
        for asset in group[1:]:
            union(group[0]["asset_id"], asset["asset_id"])

    by_key: dict[str, list[dict]] = {}
    for asset in assets:
        if len(asset["key"]) >= 2 and asset["key"] not in _GENERIC_KEYS:
            by_key.setdefault(asset["key"], []).append(asset)
    for group in by_key.values():
        if len(group) < 2:
            continue
        # 有一处明确的版本标记（终稿/修改/副本/v2）才允许仅凭文件名归并；
        # 否则要求正文足够接近，避免把同名工具产物或通用标题合并。
        has_marker = any(
            version_label(asset["name"])
            or _VERSION_TOKEN_RE.search(asset["name"])
            for asset in group
        )
        if len({asset["name"].lower() for asset in group}) == 1 and not has_marker:
            # 同名副本散落在不同目录时更像资源复制，而不是文档版本。
            continue
        base = group[0]
        for asset in group[1:]:
            if has_marker:
                union(base["asset_id"], asset["asset_id"])
            elif (base["simhash"] and asset["simhash"]
                  and _hamming(base["simhash"], asset["simhash"]) <= STRICT_DISTANCE
                  and _same_title(base, asset)):
                union(base["asset_id"], asset["asset_id"])

    by_dir: dict[str, list[dict]] = {}
    for asset in assets:
        by_dir.setdefault(asset["dir"], []).append(asset)
    for group in by_dir.values():
        if len(group) < 2 or len(group) > 60:
            continue
        for index, first in enumerate(group):
            for second in group[index + 1:]:
                if find(first["asset_id"]) == find(second["asset_id"]):
                    continue
                if not first["simhash"] or not second["simhash"]:
                    continue
                if _hamming(first["simhash"], second["simhash"]) <= STRICT_DISTANCE \
                        and _same_title(first, second):
                    union(first["asset_id"], second["asset_id"])

    clusters: dict[str, list[dict]] = {}
    for asset in assets:
        clusters.setdefault(find(asset["asset_id"]), []).append(asset)

    now = store.now_iso()
    conn.execute("DELETE FROM family_content_sources")
    conn.execute("DELETE FROM family_content_groups")
    conn.execute("DELETE FROM document_family_members")
    conn.execute("DELETE FROM document_families")
    family_count = 0
    member_count = 0
    family_ids: list[str] = []
    for members in clusters.values():
        if len(members) < 2 or len(members) > MAX_FAMILY_SIZE:
            continue
        # 家族名只是展示标签，不表示哪个版本更好。
        title = Counter(display_name(asset["name"]) for asset in members).most_common(1)[0][0]
        has_marker = any(
            version_label(asset["name"]) or _VERSION_TOKEN_RE.search(asset["name"])
            for asset in members
        )
        family_id = "fam_" + hashlib.sha1(
            "|".join(sorted(asset["asset_id"] for asset in members)).encode("utf-8")
        ).hexdigest()[:12]
        conn.execute(
            "INSERT INTO document_families"
            " (family_id, title, recommended_asset_id, confidence, rules_version,"
            " created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?, ?)",
            (
                family_id,
                title,
                "high" if has_marker else "medium",
                RULES_VERSION,
                now,
                now,
            ),
        )
        for asset in members:
            others = [other for other in members if other["asset_id"] != asset["asset_id"]]
            same_hash = any(other["hash"] == asset["hash"] for other in others)
            same_name = bool(asset["key"]) and any(
                other["key"] == asset["key"] for other in others
            )
            if same_hash:
                reason = "same_hash"
                similarity = 1.0
            elif same_name:
                reason = "same_name"
                similarity = round(max(_similarity(asset, other) for other in others), 3)
            else:
                reason = "similar_text"
                similarity = round(max(_similarity(asset, other) for other in others), 3)
            conn.execute(
                "INSERT INTO document_family_members"
                " (family_id, asset_id, version_label, similarity, is_recommended, reason)"
                " VALUES (?, ?, ?, ?, 0, ?)",
                (
                    family_id,
                    asset["asset_id"],
                    asset["version_label"],
                    similarity,
                    reason,
                ),
            )
            member_count += 1
        _build_content_groups(conn, family_id, members, now)
        family_ids.append(family_id)
        family_count += 1

    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("document_families", json.dumps({
            "rules_version": RULES_VERSION,
            "built_at": now,
            "families": family_count,
            "members": member_count,
        }, ensure_ascii=False)),
    )
    conn.commit()
    # 家族重建完成即入队：家族级摘要（zeroth/层级综合层的输入）。
    for family_id in family_ids:
        knowledge.enqueue(conn, "summary", "family", family_id)
    return {
        "families": family_count,
        "members": member_count,
        "assets": len(assets),
        "built_at": now,
    }


MAX_CONTENT_GROUPS_PER_FAMILY = 20_000


def _build_content_groups(conn: sqlite3.Connection, family_id: str,
                          members: list[dict], now: str) -> None:
    """把家族成员中相同段落归为一组，来源保留到 chunk。"""
    groups: dict[str, dict] = {}
    for asset in members:
        for row in conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE version_id = ? ORDER BY ordinal",
            (asset["version_id"],),
        ):
            text = row["text"] or ""
            if len(_normalize_text(text)) < 20:
                continue
            digest = content_hash(text)
            group = groups.get(digest)
            if group is None:
                if len(groups) >= MAX_CONTENT_GROUPS_PER_FAMILY:
                    break
                group = {"text": text[:1000], "sources": []}
                groups[digest] = group
            group["sources"].append(
                (row["chunk_id"], asset["asset_id"], asset.get("location_id"))
            )
    for digest, group in groups.items():
        group_id = store.new_id("fcg")
        conn.execute(
            "INSERT INTO family_content_groups"
            " (content_group_id, family_id, normalized_hash, representative_text, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (group_id, family_id, digest, group["text"], now),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO family_content_sources"
            " (content_group_id, chunk_id, asset_id, location_id)"
            " VALUES (?, ?, ?, ?)",
            [
                (group_id, chunk_id, asset_id, location_id)
                for chunk_id, asset_id, location_id in group["sources"]
            ],
        )


def content_groups(conn: sqlite3.Connection, family_id: str,
                   limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT content_group_id, normalized_hash, representative_text, created_at"
        " FROM family_content_groups WHERE family_id = ?"
        " ORDER BY content_group_id LIMIT ?",
        (family_id, max(1, min(limit, 1000))),
    ).fetchall()
    result = []
    for row in rows:
        sources = conn.execute(
            "SELECT s.chunk_id, s.asset_id, s.location_id, l.path"
            " FROM family_content_sources s"
            " LEFT JOIN asset_locations l ON l.location_id = s.location_id"
            " WHERE s.content_group_id = ?",
            (row["content_group_id"],),
        ).fetchall()
        unique_sources = []
        seen_assets: set[str] = set()
        for source in sources:
            if source["asset_id"] in seen_assets:
                continue
            seen_assets.add(source["asset_id"])
            unique_sources.append({
                "chunk_id": source["chunk_id"],
                "asset_id": source["asset_id"],
                "location_id": source["location_id"],
                "path": source["path"],
                "name": Path(source["path"]).name if source["path"] else None,
            })
        result.append({
            "content_group_id": row["content_group_id"],
            "normalized_hash": row["normalized_hash"],
            "text": row["representative_text"],
            "created_at": row["created_at"],
            "sources": unique_sources,
        })
    return result


def family_map(conn: sqlite3.Connection) -> dict[str, dict]:
    sizes = {
        row["family_id"]: row["n"]
        for row in conn.execute(
            "SELECT family_id, COUNT(*) AS n FROM document_family_members GROUP BY family_id"
        )
    }
    rows = conn.execute(
        "SELECT m.asset_id, m.family_id, m.version_label, f.title"
        " FROM document_family_members m"
        " JOIN document_families f ON f.family_id = m.family_id"
    )
    return {
        row["asset_id"]: {
            "family_id": row["family_id"],
            "family_size": sizes.get(row["family_id"], 1),
            "version_label": row["version_label"],
            "family_title": row["title"],
        }
        for row in rows
    }


def family_members(conn: sqlite3.Connection, family_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT m.asset_id, m.version_label, l.location_id, l.path, l.rel_path"
        " FROM document_family_members m"
        " LEFT JOIN asset_locations l ON l.asset_id = m.asset_id AND l.location_id = ("
        "  SELECT l2.location_id FROM asset_locations l2"
        "  WHERE l2.asset_id = m.asset_id AND l2.availability = 'AVAILABLE'"
        "  ORDER BY l2.path LIMIT 1)"
        " WHERE m.family_id = ? ORDER BY l.path",
        (family_id,),
    ).fetchall()
    return [
        {
            "asset_id": row["asset_id"],
            "location_id": row["location_id"],
            "path": row["path"],
            "name": Path(row["path"]).name if row["path"] else None,
            "rel_path": row["rel_path"],
            "version_label": row["version_label"] or "",
        }
        for row in rows
    ]


DIFF_PAIR_LIMIT = 400
DIFF_MODIFIED_RATIO = 0.55
DIFF_HAMMING_LIMIT = 12


def _diff_groups(conn: sqlite3.Connection, family_id: str) -> dict[str, dict]:
    """按内容组加载段落及来源，保留组内首次出现的顺序。"""
    groups: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT g.content_group_id, g.representative_text,"
        " s.asset_id, s.chunk_id, s.location_id, l.path, c.ordinal"
        " FROM family_content_groups g"
        " JOIN family_content_sources s ON s.content_group_id = g.content_group_id"
        " LEFT JOIN chunks c ON c.chunk_id = s.chunk_id"
        " LEFT JOIN asset_locations l ON l.location_id = s.location_id"
        " WHERE g.family_id = ?"
        " ORDER BY c.ordinal, g.content_group_id",
        (family_id,),
    ):
        group_id = row["content_group_id"]
        group = groups.get(group_id)
        if group is None:
            group = {
                "content_group_id": group_id,
                "text": row["representative_text"] or "",
                "order": int(row["ordinal"] or 0),
                "assets": {},
            }
            groups[group_id] = group
        asset_id = row["asset_id"]
        if asset_id in group["assets"]:
            continue
        path = row["path"]
        group["assets"][asset_id] = {
            "chunk_id": row["chunk_id"],
            "asset_id": asset_id,
            "location_id": row["location_id"],
            "path": path,
            "name": Path(path).name if path else None,
        }
    return groups


def _diff_item(group: dict, members: dict[str, dict],
               only_assets: set[str] | None = None) -> dict:
    sources = []
    for asset_id, source in group["assets"].items():
        if only_assets is not None and asset_id not in only_assets:
            continue
        member = members.get(asset_id, {})
        sources.append({
            "asset_id": asset_id,
            "name": source["name"],
            "path": source["path"],
            "version_label": member.get("version_label", ""),
        })
    return {
        "content_group_id": group["content_group_id"],
        "text": group["text"],
        "sources": sources,
    }


def _pair_modified(groups: dict[str, dict],
                   left_only: list[str], right_only: list[str]) -> tuple[list, list, list]:
    """把“只在一侧出现”的近似段落配对为修改；配对不上的分别算删除/新增。"""
    left_candidates = []
    for group_id in left_only[:DIFF_PAIR_LIMIT]:
        text = _normalize_text(groups[group_id]["text"])
        if len(text) < 20:
            continue
        left_candidates.append((group_id, text, _simhash(groups[group_id]["text"])))

    right_candidates = []
    for group_id in right_only[:DIFF_PAIR_LIMIT]:
        text = _normalize_text(groups[group_id]["text"])
        if len(text) < 20:
            continue
        right_candidates.append((group_id, text, _simhash(groups[group_id]["text"])))

    pairs: list[tuple[float, str, str]] = []
    for left_id, left_text, left_hash in left_candidates:
        if not left_hash:
            continue
        for right_id, right_text, right_hash in right_candidates:
            if not right_hash:
                continue
            if _hamming(left_hash, right_hash) > DIFF_HAMMING_LIMIT:
                continue
            ratio = difflib.SequenceMatcher(
                None, left_text, right_text, autojunk=False).ratio()
            if ratio >= DIFF_MODIFIED_RATIO:
                pairs.append((ratio, left_id, right_id))
                if len(pairs) >= DIFF_PAIR_LIMIT * 4:
                    break

    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_left: set[str] = set()
    used_right: set[str] = set()
    modified: list[tuple[float, str, str]] = []
    for ratio, left_id, right_id in pairs:
        if left_id in used_left or right_id in used_right:
            continue
        used_left.add(left_id)
        used_right.add(right_id)
        modified.append((ratio, left_id, right_id))
        if len(modified) >= DIFF_PAIR_LIMIT:
            break

    removed = [group_id for group_id in left_only if group_id not in used_left]
    added = [group_id for group_id in right_only if group_id not in used_right]
    modified.sort(key=lambda item: groups[item[1]]["order"])
    return modified, removed, added


def diff_versions(conn: sqlite3.Connection, family_id: str, left_asset_id: str,
                  right_asset_id: str, limit: int = 200) -> dict:
    """对比家族内两个版本：共享、新增、删除与近似修改的段落。

    left 作为基准版本，right 作为对比版本；不判断哪个版本更好，只呈现差异。
    """
    family = conn.execute(
        "SELECT family_id, title FROM document_families WHERE family_id = ?",
        (family_id,),
    ).fetchone()
    if family is None:
        raise KeyError("文档家族不存在")
    members = {member["asset_id"]: member for member in family_members(conn, family_id)}
    if left_asset_id not in members or right_asset_id not in members:
        raise ValueError("对比的版本不属于该文档家族")
    if left_asset_id == right_asset_id:
        raise ValueError("请选择两个不同版本")
    limit = max(1, min(limit, 500))

    groups = _diff_groups(conn, family_id)
    left_ids = [group_id for group_id, group in groups.items()
                if left_asset_id in group["assets"]]
    right_ids = [group_id for group_id, group in groups.items()
                 if right_asset_id in group["assets"]]
    right_set = set(right_ids)
    left_set = set(left_ids)
    shared_ids = [group_id for group_id in left_ids if group_id in right_set]
    left_only = [group_id for group_id in left_ids if group_id not in right_set]
    right_only = [group_id for group_id in right_ids if group_id not in left_set]

    modified, removed, added = _pair_modified(groups, left_only, right_only)

    def render(group_id: str, only_assets: set[str] | None = None) -> dict:
        return _diff_item(groups[group_id], members, only_assets)

    return {
        "family_id": family_id,
        "family_title": family["title"],
        "left": {
            "asset_id": left_asset_id,
            "name": members[left_asset_id]["name"],
            "path": members[left_asset_id]["path"],
            "version_label": members[left_asset_id]["version_label"],
        },
        "right": {
            "asset_id": right_asset_id,
            "name": members[right_asset_id]["name"],
            "path": members[right_asset_id]["path"],
            "version_label": members[right_asset_id]["version_label"],
        },
        "summary": {
            "left_groups": len(left_ids),
            "right_groups": len(right_ids),
            "shared": len(shared_ids),
            "removed": len(removed),
            "added": len(added),
            "modified": len(modified),
        },
        "shared": [render(group_id) for group_id in shared_ids[:limit]],
        "removed": [render(group_id, {left_asset_id}) for group_id in removed[:limit]],
        "added": [render(group_id, {right_asset_id}) for group_id in added[:limit]],
        "modified": [
            {
                "similarity": round(ratio, 3),
                "left": render(left_id, {left_asset_id}),
                "right": render(right_id, {right_asset_id}),
            }
            for ratio, left_id, right_id in modified[:limit]
        ],
        "truncated": {
            "shared": len(shared_ids) > limit,
            "removed": len(removed) > limit,
            "added": len(added) > limit,
            "modified": len(modified) > limit,
        },
    }


def dedupe_chunk_hits(conn: sqlite3.Connection, hits: list, *,
                      path_only: bool = False) -> list:
    """按家族和段落内容去重：重复段落合并，各版本独有段落保留。"""
    if not hits:
        return hits

    def sort_key(hit) -> tuple:
        # 位置问题按完整路径分排序；其它问题保留原来的 300 分上限。
        path_score = hit.path_score if path_only else min(hit.path_score, 300)
        return (-(hit.focus * 120 + hit.maximal * 10 + path_score),
                -hit.concepts, -hit.variants, -len(hit.text))

    mapping = family_map(conn)
    groups: dict[tuple, list] = {}
    for hit in hits:
        info = mapping.get(hit.asset_id)
        family_id = info["family_id"] if info else None
        key = (family_id or f"asset:{hit.asset_id}", content_hash(hit.text))
        groups.setdefault(key, []).append(hit)

    deduped = []
    for group_hits in groups.values():
        group_hits.sort(key=sort_key)
        representative = group_hits[0]
        representative.focus = max(getattr(hit, "focus", 0) for hit in group_hits)
        info = mapping.get(representative.asset_id)
        representative.family_id = info["family_id"] if info else None
        representative.family_size = info["family_size"] if info else 1
        representative.family_title = info["family_title"] if info else ""
        representative.version_label = info["version_label"] if info else ""
        sources = []
        seen: set[str] = set()
        for hit in group_hits:
            if hit.asset_id in seen:
                continue
            seen.add(hit.asset_id)
            hit_info = mapping.get(hit.asset_id)
            sources.append({
                "asset_id": hit.asset_id,
                "location_id": hit.location_id,
                "path": hit.path,
                "name": Path(hit.path).name,
                "version_label": hit_info["version_label"] if hit_info else "",
            })
        representative.sources = sources
        deduped.append(representative)
    deduped.sort(key=sort_key)
    return deduped


def dedupe_semantic_hits(conn: sqlite3.Connection, hits: list[dict]) -> list[dict]:
    """语义召回同样按家族和段落内容去重，保留各版本独有内容。"""
    if not hits:
        return hits
    mapping = family_map(conn)
    groups: dict[tuple, list[dict]] = {}
    for hit in hits:
        info = mapping.get(hit.get("asset_id"))
        family_id = info["family_id"] if info else None
        key = (
            family_id or f"asset:{hit.get('asset_id')}",
            content_hash(hit.get("text", "")),
        )
        groups.setdefault(key, []).append(hit)

    deduped = []
    for group_hits in groups.values():
        group_hits.sort(key=lambda hit: -hit.get("similarity", 0))
        representative = dict(group_hits[0])
        info = mapping.get(representative.get("asset_id"))
        representative["family_id"] = info["family_id"] if info else None
        representative["family_size"] = info["family_size"] if info else 1
        representative["family_title"] = info["family_title"] if info else ""
        representative["version_label"] = info["version_label"] if info else ""
        sources = []
        seen: set[str] = set()
        for hit in group_hits:
            asset_id = hit.get("asset_id")
            if not asset_id or asset_id in seen:
                continue
            seen.add(asset_id)
            hit_info = mapping.get(asset_id)
            path = hit.get("path") or ""
            sources.append({
                "asset_id": asset_id,
                "location_id": hit.get("location_id"),
                "path": path,
                "name": Path(path).name if path else None,
                "version_label": hit_info["version_label"] if hit_info else "",
            })
        representative["sources"] = sources
        deduped.append(representative)
    deduped.sort(key=lambda hit: -hit.get("similarity", 0))
    return deduped


def rebuild_async(store_path: str | None = None) -> bool:
    """后台重建；已有任务在跑时直接返回 False。"""
    global _rebuild_thread
    with _rebuild_lock:
        if _rebuild_thread is not None and _rebuild_thread.is_alive():
            return False

        def run() -> None:
            try:
                conn = db.connect(store_path)
                try:
                    db.init_db(conn)
                    rebuild(conn)
                finally:
                    conn.close()
            except Exception:
                # 家族重建是派生能力，失败不应影响核心索引与检索。
                pass

        _rebuild_thread = threading.Thread(
            target=run, name="families-rebuild", daemon=True)
        _rebuild_thread.start()
        return True
