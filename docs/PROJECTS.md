# 项目知识空间

拾源从「全局文件检索」升级为「以项目为知识组织单元的本地知识基础设施」。本文件记录这一层的
数据模型、检索语义、API / MCP 接口与迁移方式；解析、OCR 与家族归并仍见
[INGESTION.md](INGESTION.md) 与 [BREAKPOINT.md](BREAKPOINT.md)。

## 1. 四层架构

| 层 | 职责 | 实现 |
| --- | --- | --- |
| 全局资产目录 | 文件身份、位置、大小、类型、修改时间、哈希、内容版本、家族 | `sources` · `asset_locations` · `content_versions` · `document_families` |
| 全局内容与知识索引 | 解析产物、Chunk、FTS5、Embedding、摘要 / 实体 / 关系 / FAQ | `parse_artifacts` · `chunks` · `chunk_fts` · `chunk_embeddings` · `knowledge_*` |
| 项目知识空间 | 逻辑知识空间，只建立关系，不复制文件与索引 | `projects` · `project_sources` · `project_assets` · `projects.py` |
| 项目感知检索 | Project / Expanded / Global 三种范围与逐级扩展 | `search.py` · `retrieval.py` · `answer.py` |

核心不变量：

- 一个实际文件永远只有一个 `asset_id`；多个目录中的相同内容按现有去重规则合并为同一资产。
- 项目关系只写 `project_assets`，**不复制资产、不重复解析、不重复切块、不重复生成向量与摘要**。
- 同一资产可以同时属于任意多个项目；项目删除只删除关系，不删除资产或索引。
- 项目不是文件夹：项目可以引用整个数据源、数据源内的子目录（`path_prefix`）或分散的单个资产。
- **全局项目**有且只有一个：数据页录入的文件夹自动属于它，子项目按需从全局项目拉取。
- 外部接口不挂在全局，而是挂在具体项目上；一个令牌只能读取它绑定的那个项目。

## 2. 数据模型

```sql
projects(project_id, name, description, status, kind, retrieval_policy,
         privacy_policy, created_at, updated_at)   -- kind: global / standard

project_sources(project_id, source_id, relation_type, retrieval_policy,
                path_prefix, created_at)          -- PK(project_id, source_id)

project_assets(project_id, asset_id, membership_type, priority,
               status, created_at)                -- PK(project_id, asset_id)

external_clients(..., project_id)                 -- 令牌绑定的项目
```

- `kind = 'global'` 的项目固定为 `prj_global`，不能删除、归档或排除资产；
  它的资料范围就是全部已授权数据源，不写 `project_sources` / `project_assets`，因此新文件天然属于全局项目。
- 普通项目（`kind = 'standard'`）通过 `project_sources` 引用文件夹；文件夹若已在数据页授权，
  只建立逻辑引用，不创建第二个数据源、不重新扫描。

- `project_sources.relation_type`：`primary` / `linked`，决定文件夹内新文件的默认成员关系。
- `project_sources.path_prefix`：只关联数据源内的某个子目录；扫描新增文件时按
  `rel_path LIKE prefix%` 自动补进项目，不会把整个数据源算进项目。
- `project_assets.membership_type`：
  - `primary`：项目主资料，默认优先参与检索；
  - `linked`：关联资料，只在 Expanded / Global 范围参与；
  - `excluded`：仅在本项目排除，其他项目与全局检索不受影响。
- `projects.retrieval_policy`：`project_first`（默认）/ `expanded` / `global`，作为未显式传
  `scope` 时的默认范围。
- `projects.privacy_policy`：`external_allowed`（默认）/ `local_only`。后者对桌面端照常可用，
  但 MCP / HTTP 外部令牌会拒绝读取该项目，为未来的 Privacy Gateway 预留统一出口检查点。

### 全局项目

- 固定 `prj_global`，始终排在项目列表第一位，不能删除、归档或排除资产。
- 数据页新增文件夹 → 自动属于全局项目；不需要在项目页重复添加，也不会复制索引。
- 子项目详情里的「从全局项目拉取」会列出尚未关联的文件夹，点击后只建 `project_sources` 关系。
- 全局项目的检索范围固定为 `global`；在对话页选中它时范围选择器会锁定为「全局」。

### 外部接口

- 外部连接（`external_clients`）现在必须绑定一个项目；不传时默认绑定全局项目。
- 每个项目可以创建多个令牌（Codex / Claude Code / DeepSeek / 自定义 MCP 或 HTTP）。
- MCP 进程启动时根据令牌解析出 `project_id`，之后：
  - `list_projects` 只返回绑定项目；
  - 检索默认使用绑定项目，普通项目固定 `expanded`（主资料 + 关联），全局项目固定 `global`；
  - 传入其他 `project_id` 直接报错，`scope=global` 不能把普通项目越权到全局；
  - `get_document_text`、`get_evidence`、`get_related_documents`、文档家族与版本对比
    都会校验资产是否在分享范围内。
- HTTP 外部令牌同样按项目限权：`/api/search`、`/api/ask`、`/api/sources`、`/api/stats`
  只看到绑定项目；不能读取 `/api/events`、`/api/jobs`、外部连接配置或修改接口。
- 桌面端本机令牌不受项目限制，仍可管理全部项目。

扫描联动：`scanner.scan_source` / `sync_paths` 完成位置更新后调用
`projects.sync_source_assets`，按 `path_prefix` 把新资产补进引用该数据源的项目；
已存在的成员关系（包括 `excluded`）不会被覆盖。

## 3. 检索范围

| 范围 | 允许的资产 | 典型场景 |
| --- | --- | --- |
| `project` | 主资料 − 本项目排除 | 日常在当前项目内提问 |
| `expanded` | 主资料 + 关联资料 − 本项目排除 | 需要引用其他项目 / 公共资料 |
| `global` | 全部允许访问的资产 − 本项目排除 | 不知道文件属于哪个项目 |

扩展是确定性的，不依赖模型自评：

1. 先按请求范围检索；
2. `project` 范围证据少于 2 条时扩展到 `expanded`，仍不足再扩展到 `global`；
3. 只有新范围真正补到证据时才更新 `scope.used`；外层没有新增证据时保持原范围；
4. 合并证据按 `chunk_id` 去重，项目侧证据优先；
5. 同一资产不会因为属于多个项目而重复返回；文档家族仍按现有规则折叠。

排序在原有「查询相关性 + 路径/正文 + 时效」基础上增加项目维度：Expanded 范围内
主资料获得 `+120` 分。项目范围优先用 `VALUES` CTE 下推到 FTS 与路径召回，
先限定资产再取候选，避免项目内文档被全局候选挤出；资产超过 2 万个时退回内存过滤。
语义召回则把项目范围放进 SQL / 向量缓存掩码，先过滤再取 Top-K，保证小项目
不会被全局高相似片段挤空。

### 版本新旧判断

- 家族成员带 `version_label`（如 `0.13` / `终稿` / `修改版`），并按
  `is_latest_version`、`family_latest_version_label`、`family_latest_mtime` 标记最新版本。
- 新旧排序不是简单看 mtime：`终稿 / 定稿 / final`（带序号按序号，`终稿 10 > 终稿 9`）
  高于数字版本；数字版本按数字段比较（`0.13 > 0.9 > 0.8`）；再往下是 `修订`、`初稿`、`副本`。
  `最新` 只是命名，可能比终稿更旧，因此压在终稿与数字版本之下。只有无标签时才退回 mtime。
  因此“初稿_备份”即使复制时间更晚，也不会被当成最新稿。
- 没有版本标签的家族不判断“最新版本”（`is_latest_version=null`），但会额外给出
  `newest_asset_id / newest_mtime`（最后修改的成员），供外部 Agent 如实回答“哪个文件更新过”。
- `list_document_families` 返回 `built_at`（家族快照时间）；扫描后家族会重建，版本结论以该时间为准。
- 问「最新版本 / 当前版本」时，检索会补上各大家族的最新版本，并优先展示；
  问「历史版本 / 改了什么 / v0.12」时，不提升最新版本，允许同一家族的不同版本分别出现。
- `search_knowledge` 的每条结果都带版本字段；`list_document_families` 用于先确定
  “这个项目里哪些文档有多个版本、最新是哪一个”；`get_document_family` 返回按新旧排序的成员；
  `compare_versions` 给出共享 / 新增 / 删除 / 修改的段落摘要。
- 项目范围的 FTS 召回已把资产集合下推到 SQL（`VALUES` CTE），避免项目内文档被全局
  候选挤出；旧版本也会被显式标记为 `is_latest_version=false`，而不是当成当前数据。
- 问题里点名版本（如「v0.2 的 BUSCO」）时，即使词面候选被大量同名笔记挤占，
  也会把该版本成员补回候选，并保留历史标记；不会只返回无关文件。
- 没有版本标签的家族只是重复副本：`is_latest_version` 返回 `null`（未知），
  `list_document_families` 也不会把它们列成“多版本文档”。

## 4. Core API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/projects` | 项目列表（含 source / primary / linked / excluded 计数） |
| POST | `/api/projects` | 创建项目，可同时传 `folders`；已授权目录直接复用 |
| GET | `/api/projects/{id}` | 项目详情 + 统计 + 文件夹 |
| PATCH | `/api/projects/{id}` | 名称、描述、状态、检索策略、隐私策略 |
| DELETE | `/api/projects/{id}` | 只删关系，不动资产与索引 |
| POST | `/api/projects/{id}/sources` | 关联文件夹（`root`）或已有数据源（`source_id` + `path_prefix`） |
| DELETE | `/api/projects/{id}/sources/{source_id}` | 移除文件夹引用，清理该前缀下的主资料关系 |
| GET | `/api/projects/{id}/assets` | 按 `membership` / `q` 分页浏览资料 |
| POST | `/api/projects/{id}/assets` | 按 `asset_ids` 或 `paths` 设置成员关系与优先级 |
| DELETE | `/api/projects/{id}/assets/{asset_id}` | 主资料改为本项目排除；关联资料直接解除 |
| GET | `/api/projects/{id}/knowledge` | 项目内摘要 / 实体 / 关系 / FAQ |
| GET | `/api/assets` | 全局资产库，`unassigned=1` 只看未归属文件 |
| GET | `/api/search` | 新增 `project_id`、`scope`，返回 `scope` |
| POST | `/api/ask` · `/api/ask/stream` | 新增 `project_id`、`scope`，回答附带实际使用范围 |

桌面端的项目操作与 MCP 使用同一组 Core 函数，不存在第二套检索或权限逻辑。

## 5. MCP

只读工具新增：

| 工具 | 说明 |
| --- | --- |
| `list_projects` | 列出 `external_allowed` 的活跃项目；绑定令牌只返回绑定项目 |
| `get_project` | 项目信息与统计（不返回隐私策略字段） |
| `search_assets` | 文件名 / 路径检索，支持 `project_id` / `scope` |
| `search_knowledge` | 全文 / 语义检索，支持 `project_id` / `scope` |
| `get_evidence` | 按 `chunk_id` 或 `location_id` 取原始证据（页码 / 工作表） |
| `get_document_family` | 文档家族与成员版本，成员按版本新旧排序 |
| `list_document_families` | 列出项目内所有多版本文档家族，含最新版本与全部版本标签 |
| `get_related_documents` | 同一家族的其他版本，以及同项目内的其他资料 |
| `compare_versions` | 家族内两个版本的段落差异 |
| `get_source_status` | 数据源扫描与覆盖状态 |

权限：

- 外部令牌绑定一个项目：`project_id` 必须存在、状态为 `active` 且 `privacy_policy = external_allowed`；
- 绑定令牌不能查询其他项目；普通项目的 `scope` 只能是 `project` / `expanded`，
  全局项目固定为 `global`；
- `get_document_text` / `get_evidence` / 家族 / 版本对比都会校验资产是否在分享范围内；
- MCP 默认只读；项目创建、删除与文件夹管理保留在桌面端；
- 返回项带 `project_membership` 与 `scope`，证据保留 `location_id` / `path` / 页码，可回到原件核对。

未来的 Privacy Gateway 入口固定在「检索结果 → 项目权限检查 → 数据分类 → 脱敏 → MCP 输出」
这一层；当前 `local_only` 已走同一入口拒绝外部访问。

## 6. 数据库迁移

- `SCHEMA_VERSION = 3`；没有 `schema_version` 记录但有 `app_settings` 的旧库视为 v1。
- 升级前自动在线备份为同目录 `shiyuan.bak-YYYYmmdd-HHMMSS.db`，重复执行 `init_db` 不会重复备份。
- 迁移只执行 `CREATE TABLE IF NOT EXISTS`、`ALTER TABLE ADD COLUMN` 与补数据，不删除、不改写旧表：
  - `projects.kind`：区分全局项目与普通项目；
  - `external_clients.project_id`：历史连接自动归入全局项目；
  - `chunk_embeddings.asset_id`：从 `chunks` 回填，项目统计与范围过滤不再回表读 570MB 向量。
- 新增列的索引在补列之后创建，旧库也能安全升级。
- 历史资产默认不归属任何普通项目；原有 FTS / Chunk / Embedding / 摘要 / 家族全部复用。
- 真实库（13,012 资产 / 382,678 chunk / 138,639 向量）v2 → v3 迁移耗时约 52 秒，
  迁移前后各表计数一致，`PRAGMA foreign_key_check` 为 0。

## 7. 实测（真实库）

环境：Windows 11、13,012 个资产、382,678 个 chunk、138,639 个向量，
项目 A 为 1,564 个资产 / 264,526 个 chunk / 20,488 个向量，查询为自然语言中文问句。

| 场景 | 中位延迟 |
| --- | --- |
| Project Mode（项目主资料） | 1.93 s |
| Expanded Mode（主资料 + 关联） | 1.97 s |
| Global Mode（全部资料） | 2.33 s |
| 空项目 / 全部排除 | 0 ms（直接返回） |
| 项目列表 | 0.2 ms |
| 普通项目统计 | 29 ms |
| 全局项目统计 | 0.69 s |
| 项目资料首页 100 条 | 10 ms |
| 项目知识聚合 | 14 ms |

同一篇论文加入 10 个项目：`assets`、`chunks`、`chunk_embeddings` 数量均不变，
仍只有一份解析与一套向量。迁移前的全局 FTS 检索基线约 1.9–2.2 秒，项目过滤没有引入回退；
语义检索通过共享的 int8 向量缓存做内存掩码，项目范围额外开销约 0.1–0.2 秒。

## 8. 已知限制

- 全局项目统计需要扫描全部内容版本，首次约 0.7 秒；普通项目统计已降至几十毫秒。
- 项目级 `Local-only` 目前只约束 MCP / HTTP 出口；数据分类、字段级脱敏尚未实现。
- MCP 只读，不能在 Agent 侧创建项目、调整成员关系或创建外部接口。
- 跨项目重复文件已在资产层去重；同一资产的多个位置在结果中按现有家族规则展示。
- 项目归档只影响默认列表，历史检索仍可通过 Global 范围访问。
- 全局项目是虚拟的：不写 `project_sources` / `project_assets`，因此不能在全局项目里单独排除文件。
- 文档家族按内容相似度自动归并：同一篇论文如果前后版本标题与结构变化过大（如
  `manuscript_EN_v0.2~v0.7` 与 `CBPD_clean_v0.8~v0.13`），可能被识别成两个家族。
  这时 `list_document_families` 会同时列出两个家族，需按最新版本与修改时间自行判断，
  当前不支持手动合并家族。
