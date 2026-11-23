<div align="center">

# 拾源 · 原位知识工作台

**你的文件按原来的方式保存，知识库自己生长。**

[![Release](https://img.shields.io/github/v/release/JunyangLian/Shiyuan)](https://github.com/JunyangLian/Shiyuan/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/JunyangLian/Shiyuan/total)](https://github.com/JunyangLian/Shiyuan/releases)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4)

[下载安装包](https://github.com/JunyangLian/Shiyuan/releases/latest) · [产品需求](docs/PRD.md) · [开发断点](docs/BREAKPOINT.md) · [解析说明](docs/INGESTION.md)

</div>

---

## 这是什么

拾源是一个**本地优先**的知识工作台：它索引你已有的文件夹，让你用自然语言在里面找文件、读内容、问问题。

它不是网盘，也不是笔记软件——文件留在原地，拾源只在旁边建一层可检索、可回答的索引。

三条设计原则：

| 原则 | 含义 |
| --- | --- |
| **不搬文件** | 不移动、不改名、不删除原件；撤销授权即清除索引 |
| **不替你做判断** | 同一份文档的多个版本只做归并，不推荐、不裁定哪个"更好" |
| **不默认联网** | 默认本地摘录回答；云端模型与 Embedding 需显式开启 |

## 核心能力

**索引与解析**

- 授权本地目录后自动扫描，记录文件名、路径、大小、修改时间与稳定文件 ID，支持增量对账
- 实时监听（1.5s 去抖 + 10s 封顶），同盘移动/改名沿用同一 asset_id；周期对账兜住休眠与事件丢失
- 解析 TXT/MD/代码、DOCX、PPTX、PDF 文本层、XLSX（值 + 公式）、旧版 Office（经 LibreOffice）、
  ZIP/7Z/RAR（安全解压递归索引）、EML/MSG、DXF，音视频只取元数据
- OCR 双引擎：随包 PP-OCRv6 small（扫描 PDF，离线）与 Windows 本机 OCR（图片、混合版式 PDF、PPTX 位图），
  结果带页码与行坐标，无文本层的扫描件会被显式标记而不是当成"内容为空"

**检索与问答**

- 三路召回：**文件夹路径 + 文件名 + 正文**，目录层级越深权重越高；通用目录词（实验/数据/报告）不单独构成证据
- 查询理解：剥离疑问词与口水词，中文 2/3-gram 展开，内置科研同义词（样本数 ↔ 样本量）
- 目录概览：问「大三下有哪些课」「pilot_batches 里有什么」直接列出子目录与文件数
- 位置问句：「放在哪个文件夹」「课程表在哪」只认具体路径命中，找不到就明确拒答
- 多轮追问：同一会话里「那第三章呢」「它的环境变量怎么配」会带上上一轮问题检索，
  上一轮只涉及一个数据源时自动收窄
- 回答默认本地摘录（不联网），可切换 OpenAI 兼容接口或 Ollama；每条结论带 `[n]` 引用，
  没有证据直接拒答并说明内容覆盖

**文档家族**

- 同一份知识的多版本自动归并为家族，家族 ID 基于成员哈希，重建后引用不失效
- 按**内容段落**去重而非按文件：重复段落合并展示并保留全部来源，各版本独有或修改过的段落仍在
- 版本差异视图：共享 / 仅左 / 仅右 / 近似修改（SimHash 预筛 + 序列相似度）
- 引用里同家族的多个版本折叠成一组，点开可看全部成员

**协作与运维**

- 外部调用：为 Codex、Claude Code、DeepSeek Harness 等生成独立只读令牌与连接配置（MCP / HTTP）
- 内置只读 MCP 服务，直接读取同一个 SQLite 库，不扫描、不解析、不改原件
- 任务账本与失败重试、每日解析 / OCR 预算、低质量文本自动复核、一键重建索引与清理缓存

## 下载与安装

到 [Releases](https://github.com/JunyangLian/Shiyuan/releases/latest) 下载 `Shiyuan_x.y.z_x64-setup.exe`。
安装包自带 Python 核心与 OCR 模型，无需另装 Python 或数据库。

- 系统：Windows 10 / 11（x64）
- 数据目录：`%LOCALAPPDATA%\ShiyuanData\`（可用 `SHIYUAN_HOME` 覆盖）
- 安装版启动时自动拉起核心进程，窗口关闭时结束；若 8737 端口已有核心在跑则直接复用

## 源码运行

前置：Windows 10/11、Node 20+、Python 3.10+、Rust 1.80+（仅桌面壳需要）。

```powershell
# 1. 本地核心（默认 http://127.0.0.1:8737）
cd core
python -m pip install -r requirements.txt
python -m shiyuan

# 2. 另开终端启动桌面端
cd ..\desktop
npm install
npm run tauri dev
```

只看界面可用浏览器模式：`cd desktop; npm run dev` 后打开 http://localhost:1420
（浏览器无法调用系统目录选择框，会退化为手动输入路径）。

常用环境变量：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `SHIYUAN_HOME` | `%LOCALAPPDATA%\ShiyuanData` | 数据库、令牌与日志位置 |
| `SHIYUAN_WATCH` | `1` | 设为 `0` 关闭实时监听，只保留手动扫描 |
| `SHIYUAN_RECONCILE_SECONDS` | `600` | 周期对账间隔（最小 30 秒） |
| `SHIYUAN_DAILY_PARSE_BUDGET` | `500` | 每日正文解析文件数上限，`0` 为不限 |
| `SHIYUAN_DAILY_OCR_BUDGET` | `200` | 每日 OCR 文件数上限，`0` 为不限 |
| `SHIYUAN_MODEL_TIMEOUT` | `60` | 模型单次回答超时秒数（推理模型可调 120） |

## 架构

```
core/                 Knowledge Core（Python + FastAPI + SQLite）
  shiyuan/            scanner · watcher · ingest · extractors · ocr · indexer
                      query · retrieval · families · answer · chat
                      external · mcp_server · inventory · maintenance · api
  tests/              172 项用例（扫描 / 监听 / 检索 / 问答 / OCR / 家族 / 召回 / 安全 / 基线）
  benchmarks/         目录级扫描基准
desktop/              Tauri v2 + React + TypeScript
  src/                数据 · 对话 · 外部调用 · 更新 四个模块
  src-tauri/          Rust 外壳（dialog / opener / shell）
docs/                 PRD、解析说明、开发断点
```

数据流：`扫描 → 解析/OCR → 正文分块 → 路径索引 + FTS + 家族归并 → 检索 → 证据 → 回答/引用`。

## 检索是怎么打分的

1. **查询理解**：去掉疑问结构与口水词，按「的」切词，剥句尾方位词，展开同义词
2. **路径召回**：按数据源内相对路径打分（根目录名不参与），文件夹按深度加权，文件名权重更高
3. **正文召回**：FTS5 trigram + 两字词 LIKE 兜底；正文命中的文件同时获得路径分
4. **证据门槛**：按「最大词」计（避免 `创新 / 新点 / 创新点` 这类 gram 膨胀误杀），
   纯通用目录词不构成证据
5. **排序**：`追问词命中 × 120 + 最大词命中 × 10 + min(路径分, 300)`，
   同文件最多 1 条、同家族最多 2 条证据，避免多版本占位
6. **生成**：只喂入选片段，要求逐句标注引用；证据不足直接拒答

## 本机安全边界

- **API 令牌**：核心启动生成进程级令牌写入 `api-token`，除 OPTIONS 预检外所有 `/api/*` 需携带
- **CORS**：只允许 `localhost:1420` 与 Tauri 来源
- **密钥加密**：回答 / Embedding 的 Key 用 Windows DPAPI 加密（`dpapi:v1:`）写入 SQLite，旧明文自动迁移
- **外部连接**：每个 Agent 独立令牌（`syext_`），可单独撤销，HTTP 令牌只放行只读接口
- `SHIYUAN_API_AUTH=0` 仅用于测试排障，不要长期关闭

## MCP

```powershell
cd core
python -m shiyuan.mcp_server                 # stdio 模式
python -m shiyuan.mcp_server --print-config  # 打印客户端配置
```

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| tool | `search_knowledge` | 检索文件名 / 路径 / 正文 / OCR，返回片段与页码、工作表 |
| tool | `get_document_text` | 按 `location_id` 或绝对路径读取正文，可按页 / 工作表过滤 |
| tool | `list_sources` | 授权目录、扫描健康度与文件数 |
| tool | `knowledge_stats` | 内容覆盖、类型分布、OCR 状态与维护计数 |
| resource | `shiyuan://sources` | 数据源 JSON |
| resource | `shiyuan://stats` | 统计 JSON |

推荐顺序：先 `search_knowledge`，再用返回的 `location_id` 调 `get_document_text`。
MCP 不触发扫描，新文件仍需先在桌面端完成索引。安装版无需单独 exe：

```json
{
  "mcpServers": {
    "shiyuan": {
      "command": "C:\\Users\\<你>\\AppData\\Local\\Shiyuan\\shiyuan-core.exe",
      "args": ["--mcp"]
    }
  }
}
```

> onefile 首次启动需解包核心（约 10–15 秒），客户端启动超时请设为 30 秒以上。
> 也可以直接在桌面端「外部调用」页选择预设、生成配置并测试连通性。

## 打包安装包

<details>
<summary>PyInstaller sidecar + Tauri NSIS（点击展开）</summary>

```powershell
# 1. 生成核心可执行文件（约 185MB，需 pip install pyinstaller）
cd core
$root = Split-Path (Get-Location) -Parent
$env:TMP = Join-Path $root ".tmp\tmp"
$env:TEMP = $env:TMP
$env:PYINSTALLER_CONFIG_DIR = Join-Path $root ".tmp\pyinstaller"
New-Item -ItemType Directory -Path $env:TMP, $env:PYINSTALLER_CONFIG_DIR -Force | Out-Null
python -m PyInstaller --onefile --noconsole --name shiyuan-core --paths . `
  --hidden-import shiyuan.api --hidden-import shiyuan.mcp_server `
  --collect-submodules uvicorn --collect-submodules winsdk `
  --collect-data rapidocr `
  --hidden-import rapidocr.inference_engine.onnxruntime `
  --hidden-import rapidocr.inference_engine.onnxruntime.main `
  --hidden-import rapidocr.inference_engine.onnxruntime.provider_config `
  --collect-binaries onnxruntime --collect-data onnxruntime `
  --exclude-module onnx --exclude-module torch --exclude-module paddle `
  --exclude-module openvino --exclude-module tensorrt --exclude-module jupyter_client `
  --exclude-module ipykernel --exclude-module zmq --exclude-module PyQt5 `
  --exclude-module matplotlib --exclude-module pandas --exclude-module tkinter `
  --collect-all pypdfium2 sidecar.py
Copy-Item .\dist\shiyuan-core.exe ..\desktop\src-tauri\binaries\shiyuan-core-x86_64-pc-windows-gnu.exe -Force

# 2. 构建 NSIS 安装包
$env:CARGO_TARGET_DIR="D:\cargo-target\shiyuan"
cd ..\desktop
npm run tauri build
```

产物：`D:\cargo-target\shiyuan\release\bundle\nsis\Shiyuan_<版本>_x64-setup.exe`。
`desktop/src-tauri/binaries/` 是构建产物，不入库；新克隆仓库需先执行第 1 步。

**Windows + MinGW 注意**：仓库路径含中文时，`ld.exe` 会按 GBK 解析 UTF-8 路径并在链接阶段报
`cannot find ...<xxx>.rlib`。把 `CARGO_TARGET_DIR` 指到纯 ASCII 路径即可。`crate-type` 只保留
`rlib`，因为 MinGW 链接 `cdylib` 会报 `export ordinal too large`（桌面端不需要）。

</details>

## 验证

```powershell
cd core; python -m pytest -q          # 172 passed（另需 pip install pytest httpx）
cd desktop; npm test                  # 14 passed（Vitest + jsdom）
cd desktop; npm run build             # tsc --noEmit + vite build
cd desktop/src-tauri; cargo check     # 先设置 CARGO_TARGET_DIR
```

检索基线（120 篇文档 / 140 道题）：R@1 = 1.000、R@5 = 1.000、MRR = 1.000（合成唯一标记语料）。
扫描基线：2000 个文件首次扫描约 2.1s（约 940 files/s），二次对账约 0.56s。

## 已知限制

- **OCR 只保留行坐标**，不还原表格结构与版面，没有手写专项
- **不内置本地大模型**：Ollama 需自行安装；语义检索依赖外部 Embedding 服务，未使用 ANN
- **会话记忆只到上一轮**：不保留完整对话状态，跨会话无记忆
- **跨盘移动**识别为「新增 + 失联」，不做自动合并
- **网络盘 / SMB** 的监听可靠性未实测，主要依赖周期对账
- **新建 / 删除 / 改名文件夹**会触发整源重扫，大目录会明显变慢
- **短词检索**（<3 字符）走 LIKE 扫描，语料很大时比 FTS 慢
- **引用只校验编号**：无法证明模型每句话都忠于证据，仍需点开来源核对

## 文档

- [docs/PRD.md](docs/PRD.md) — 产品需求与设计
- [docs/INGESTION.md](docs/INGESTION.md) — 解析与 OCR 策略
- [docs/BREAKPOINT.md](docs/BREAKPOINT.md) — 开发断点与已知问题
