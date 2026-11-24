"""SQLite 连接与表结构。

M1 落地"位置 / 资产 / 内容版本"；M3 增加 parse_artifacts、chunks 与 FTS5。
index_jobs（异步任务表）仍未建：当前解析在扫描线程内同步完成。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id    TEXT PRIMARY KEY,
    root_uri     TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    policy       TEXT NOT NULL DEFAULT 'readonly',
    excludes     TEXT NOT NULL DEFAULT '[]',
    state        TEXT NOT NULL DEFAULT 'active',
    health       TEXT NOT NULL DEFAULT 'unknown',
    last_scan_at TEXT,
    l0_completed_at TEXT,
    l1_completed_at TEXT,
    l2_completed_at TEXT,
    full_coverage_at TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'file',
    lifecycle  TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS asset_locations (
    location_id    TEXT PRIMARY KEY,
    asset_id       TEXT NOT NULL REFERENCES assets(asset_id),
    source_id      TEXT NOT NULL REFERENCES sources(source_id),
    path           TEXT NOT NULL,
    rel_path       TEXT NOT NULL,
    native_file_id TEXT,
    availability   TEXT NOT NULL DEFAULT 'AVAILABLE',
    size           INTEGER NOT NULL DEFAULT 0,
    mtime_ns       INTEGER NOT NULL DEFAULT 0,
    ext            TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    UNIQUE (source_id, path)
);

CREATE INDEX IF NOT EXISTS idx_locations_asset ON asset_locations(asset_id);
CREATE INDEX IF NOT EXISTS idx_locations_native ON asset_locations(source_id, native_file_id);
CREATE INDEX IF NOT EXISTS idx_locations_avail ON asset_locations(source_id, availability);

CREATE TABLE IF NOT EXISTS content_versions (
    version_id  TEXT PRIMARY KEY,
    asset_id    TEXT NOT NULL REFERENCES assets(asset_id),
    hash        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    is_current  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_versions_asset ON content_versions(asset_id, is_current);

CREATE TABLE IF NOT EXISTS source_scan_runs (
    run_id        TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    state         TEXT NOT NULL,
    files_seen    INTEGER NOT NULL DEFAULT 0,
    files_new     INTEGER NOT NULL DEFAULT 0,
    files_updated INTEGER NOT NULL DEFAULT 0,
    files_moved   INTEGER NOT NULL DEFAULT 0,
    files_missing INTEGER NOT NULL DEFAULT 0,
    files_skipped INTEGER NOT NULL DEFAULT 0,
    skip_reasons  TEXT,
    errors        INTEGER NOT NULL DEFAULT 0,
    message       TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_events (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT NOT NULL,
    source_id TEXT,
    detail    TEXT,
    outcome   TEXT,
    timestamp TEXT NOT NULL
);

-- 以下为 M3 内容层：解析产物与检索块。缓存键 = version_id + parser_signature。
CREATE TABLE IF NOT EXISTS parse_artifacts (
    artifact_id      TEXT PRIMARY KEY,
    version_id       TEXT NOT NULL,
    parser_signature TEXT NOT NULL,
    state            TEXT NOT NULL,          -- ok / empty / failed
    quality          TEXT NOT NULL DEFAULT 'good',  -- good / low / empty
    char_count       INTEGER NOT NULL DEFAULT 0,
    page_count       INTEGER NOT NULL DEFAULT 0,    -- PDF 总页数（其他类型为 0）
    text_pages       INTEGER NOT NULL DEFAULT 0,    -- 有文本层的页数
    image_count      INTEGER NOT NULL DEFAULT 0,    -- 演示文稿等内嵌图片数（供 L2 OCR 判定）
    message          TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (version_id, parser_signature)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_version ON parse_artifacts(version_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    version_id  TEXT NOT NULL,
    asset_id    TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    page        INTEGER,
    sheet       TEXT,
    bbox        TEXT,                        -- OCR 行坐标 JSON：[[x,y,w,h], …]
    char_start  INTEGER NOT NULL DEFAULT 0,
    char_end    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chunks_artifact ON chunks(artifact_id);
CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(version_id);
CREATE INDEX IF NOT EXISTS idx_chunks_asset ON chunks(asset_id);

-- 中文没有词边界，trigram 让"结果表"这类子串可以直接命中（查询需 ≥3 字符，
-- 更短的查询在 search.py 里退化为 LIKE 扫描）。
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text,
    chunk_id UNINDEXED,
    version_id UNINDEXED,
    tokenize = 'trigram'
);

-- M4：回答设置（提供商、模型、密钥等）。密钥只在本机数据库里，接口永不回显。
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- M6：解析任务账本。一个内容版本在一个阶段（l1 文本 / l2 OCR）只有一条记录。
CREATE TABLE IF NOT EXISTS index_jobs (
    job_id     TEXT PRIMARY KEY,
    source_id  TEXT NOT NULL,
    version_id TEXT NOT NULL,
    asset_id   TEXT NOT NULL,
    stage      TEXT NOT NULL,
    status     TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (version_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON index_jobs(stage, status);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON index_jobs(source_id);

-- 可选语义检索：按 chunk 保存向量，模型名参与匹配；换模型需要重建。
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id   TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON chunk_embeddings(model);

-- 不值得做语义向量的 chunk（纯数字表格、序列、空文本）：记下原因，避免每次重建都重新判断。
CREATE TABLE IF NOT EXISTS chunk_embedding_skips (
    chunk_id   TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embedding_skips_model ON chunk_embedding_skips(model);

-- 对话管理：会话与消息（助手消息的完整结果存 payload JSON）。
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    role       TEXT NOT NULL,            -- user / assistant
    content    TEXT NOT NULL,
    payload    TEXT,                     -- assistant：完整 AnswerResult JSON
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages(session_id, created_at);

-- 文档家族：同一份知识内容的不同版本文件。成员仍各自保留独立资产与路径。
CREATE TABLE IF NOT EXISTS document_families (
    family_id            TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    recommended_asset_id TEXT,
    confidence           TEXT NOT NULL DEFAULT 'medium',
    rules_version        TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_family_members (
    family_id    TEXT NOT NULL,
    asset_id     TEXT NOT NULL,
    version_label TEXT,
    similarity   REAL,
    is_recommended INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    PRIMARY KEY (family_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_family_members_asset
    ON document_family_members(asset_id);

-- 家族内段落级内容分组：相同段落只存一份，来源保留到 chunk。
CREATE TABLE IF NOT EXISTS family_content_groups (
    content_group_id    TEXT PRIMARY KEY,
    family_id           TEXT NOT NULL,
    normalized_hash     TEXT NOT NULL,
    representative_text TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (family_id, normalized_hash)
);

CREATE INDEX IF NOT EXISTS idx_content_groups_family
    ON family_content_groups(family_id);

CREATE TABLE IF NOT EXISTS family_content_sources (
    content_group_id TEXT NOT NULL,
    chunk_id         TEXT NOT NULL,
    asset_id         TEXT NOT NULL,
    location_id      TEXT,
    PRIMARY KEY (content_group_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_content_sources_group
    ON family_content_sources(content_group_id);

-- 外部 Agent 连接：Codex / Claude Code / DeepSeek Harness 等本机客户端。
-- token 用 DPAPI 保护；列表接口永不回显明文，只有配置接口按需解密。
CREATE TABLE IF NOT EXISTS external_clients (
    client_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    preset       TEXT NOT NULL,
    transport    TEXT NOT NULL,            -- mcp / http
    token        TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    last_test_at TEXT,
    last_status  TEXT,                     -- ok / failed
    last_error   TEXT,
    call_count   INTEGER NOT NULL DEFAULT 0,
    last_call_at TEXT
);

-- 衍生知识：由生成模型批量产出的层级摘要、实体关系与 FAQ。
-- 任务账本 UNIQUE(kind, scope_id)：同一份内容（文档/家族）每种知识只有一条任务，
-- 内容重新解析后重新入队会重置为 pending，再次生成。
CREATE TABLE IF NOT EXISTS knowledge_jobs (
    job_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,              -- summary / entities / questions
    scope_type TEXT NOT NULL,              -- document / family
    scope_id   TEXT NOT NULL,
    model      TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending / running / done / failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_jobs_status
    ON knowledge_jobs(status, updated_at);

CREATE TABLE IF NOT EXISTS knowledge_summaries (
    summary_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL,              -- document / family
    scope_id   TEXT NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    model      TEXT,
    status     TEXT NOT NULL DEFAULT 'ok', -- ok / failed
    updated_at TEXT NOT NULL,
    UNIQUE (scope_type, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_summaries_scope
    ON knowledge_summaries(scope_type, scope_id);

-- 衍生知识 - 关系层：实体 / 关系 / FAQ。family_id 存生成时的分组键
-- （家族级为 family_id，文档级为 asset_id）。
CREATE TABLE IF NOT EXISTS knowledge_entities (
    entity_id   TEXT PRIMARY KEY,
    family_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    model       TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (family_id, name)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_entities_family
    ON knowledge_entities(family_id);

CREATE TABLE IF NOT EXISTS knowledge_relations (
    relation_id TEXT PRIMARY KEY,
    family_id   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 0.5,
    evidence    TEXT NOT NULL DEFAULT '',
    model       TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (family_id, subject, predicate, object)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_relations_family
    ON knowledge_relations(family_id);

CREATE TABLE IF NOT EXISTS knowledge_questions (
    question_id        TEXT PRIMARY KEY,
    family_id          TEXT NOT NULL,
    question           TEXT NOT NULL,
    answer             TEXT NOT NULL,
    related_entity_ids TEXT NOT NULL DEFAULT '[]',
    model              TEXT,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_questions_family
    ON knowledge_questions(family_id);

-- FAQ 的语义向量，独立于 chunk_embeddings：chunk 清理不会误删知识向量。
CREATE TABLE IF NOT EXISTS knowledge_vectors (
    knowledge_id TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,              -- question
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vector       BLOB NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_vectors_model
    ON knowledge_vectors(model);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    # FastAPI 的同步依赖可能在线程池的不同线程里初始化与收尾，连接不能沿用
    # sqlite3 默认的同线程限制；每个连接仍只服务单次请求，不跨请求共享。
    conn = sqlite3.connect(str(target), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给旧数据库补列：CREATE IF NOT EXISTS 不会修改已存在的表。"""
    additions = {
        "sources": {
            "l0_completed_at": "TEXT",
            "l1_completed_at": "TEXT",
            "l2_completed_at": "TEXT",
            "full_coverage_at": "TEXT",
        },
        "source_scan_runs": {
            "files_skipped": "INTEGER NOT NULL DEFAULT 0",
            "skip_reasons": "TEXT",
        },
        "parse_artifacts": {
            "page_count": "INTEGER NOT NULL DEFAULT 0",
            "text_pages": "INTEGER NOT NULL DEFAULT 0",
            "image_count": "INTEGER NOT NULL DEFAULT 0",
        },
        "chunks": {"bbox": "TEXT"},
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
