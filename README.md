# 拾源 · 原位知识工作台

> 你的文件按原来的方式保存，知识库自己生长。

当前进度：**M1 目录层 + M2 实时监听 + M3 内容检索 + M4 问答引用 + M5 多模态（OCR）+ M6 知识库管理 + 召回增强 + 多格式解析 + MCP + 本机安全加固 + Windows 安装包**。
产品需求与技术设计见 [docs/PRD.md](docs/PRD.md)，开发断点见 [docs/BREAKPOINT.md](docs/BREAKPOINT.md)。

## 目录结构

```
core/                 Knowledge Core（Python + FastAPI + SQLite）
  shiyuan/            数据源、扫描/监听、L1 解析、L2 OCR、检索、问答引用、HTTP 接口
  tests/              172 项用例：扫描 / 监听 / 索引 / 检索 / 问答 / OCR / 维护 / 召回 / 安全 / 基线
  benchmarks/         10 万级目录扫描基准
desktop/              Tauri v2 + React + TypeScript 桌面端
  src/                检索与对话、数据源与维护两个整合页面
  src-tauri/          Rust 外壳（dialog / opener / shell 插件）
docs/                 PRD、断点文档
```

## 运行

前置：Windows 10/11、Node 20+、Python 3.10+、Rust 1.80+（仅桌面壳需要）。

```powershell
# 0. 安装核心依赖（首次）
cd core
python -m pip install -r requirements.txt

# 1. 启动本地核心（默认 http://127.0.0.1:8737）
python -m shiyuan

# 2. 另开一个终端启动桌面端（开发模式）
cd ..\desktop
npm install
npm run tauri dev
```

只想看界面时可用浏览器模式：`cd desktop; npm run dev`，打开 http://localhost:1420
（浏览器无法调用系统目录选择框，会退化为手动输入路径）。

应用数据默认写入 `%LOCALAPPDATA%\ShiyuanData\shiyuan.db`，可用环境变量 `SHIYUAN_HOME` 覆盖。
（刻意不叫 `Shiyuan`：安装包默认装到 `%LOCALAPPDATA%\Shiyuan`，卸载会删除该目录。）

实时监听默认开启（watchdog + 1.5s 去抖 + 10s 上限）：

- `SHIYUAN_WATCH=0` 可关闭监听，只保留手动扫描；
- `SHIYUAN_RECONCILE_SECONDS=600` 调整周期对账间隔（最小 30 秒），兜住休眠、网络盘与事件丢失；
- `SHIYUAN_DAILY_PARSE_BUDGET=500` / `SHIYUAN_DAILY_OCR_BUDGET=200` 控制每日解析与 OCR 的文件数上限（0 表示不限制）；
- `SHIYUAN_MODEL_TIMEOUT=60` 调整云端/本地模型单次回答的超时秒数（推理模型可调到 120）。
- 目录级事件（新建/删除/改名文件夹）会退化为对该数据源的一次完整对账；
- 扫描期间到达的事件会记为 follow-up，当前扫描结束后自动再补一轮，不丢事件。

## 本机安全边界

- **API 访问令牌**：核心启动时生成进程级令牌，写入 `%LOCALAPPDATA%\ShiyuanData\api-token`；
  桌面端通过 Tauri `api_token` 命令读取，浏览器开发模式由 Vite `/__shiyuan_token` 转发。
  除 OPTIONS 预检外，所有 `/api/*` 缺少或带错令牌都返回 401。
- **CORS**：只允许 `localhost:1420`、`127.0.0.1:1420` 与 Tauri 来源，不再放行任意 localhost 端口。
- `SHIYUAN_API_AUTH=0` 只用于测试/排障，不要长期关闭。
- 令牌文件对当前 Windows 用户可读，能挡住其他用户和误连的网页，但不能替代系统级隔离；
  同一用户下的恶意进程仍可读取。
- **API Key 加密**：回答/Embedding 的 Key 使用 Windows DPAPI 加密后写入 SQLite，
  前缀 `dpapi:v1:`；旧版明文在核心启动时自动迁移，并用 `secure_delete` + `VACUUM` 清理旧页。
- 核心使用命名互斥量保证同一端口只有一个 HTTP 实例，避免重复写令牌。

## 检索与扫描基线

```powershell
cd core
# 120 篇文档 / 140 道题（文件名、正文唯一标记、中文两字短词、自然语言）
python -m pytest tests/test_retrieval_benchmark.py -q -s

# 目录级全量对账；10 万文件请按需调大 --files
python benchmarks/scan_benchmark.py --files 2000
```

当前结果：检索 R@1=1.000、R@5=1.000、MRR=1.000（合成唯一标记语料；真实用户语料仍需补充标注）；
2000 个文件首次扫描约 2.12s（943 files/s），二次对账约 0.56s。

## 打包 Windows 安装包

安装包内置 Python 核心（PyInstaller 单文件 sidecar），用户无需另装 Python 或数据库。

```powershell
# 1. 生成 core 可执行文件（约 185MB，需先 pip install pyinstaller）
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

产物：`D:\cargo-target\shiyuan\release\bundle\nsis\Shiyuan_0.1.2_x64-setup.exe`（约 180MB，
含多格式解析、监听、Windows OCR、PP-OCRv6 small 与 MCP `--mcp` 模式）。
`desktop/src-tauri/binaries/` 是构建产物，不入库；新克隆仓库需先执行第 1 步。

运行行为：

- 安装版启动时桌面端自动拉起 sidecar；窗口退出时结束它。若 8737 已有核心在跑则直接复用。
- 开发模式（`npm run tauri dev`）不自动拉起，仍需另开终端执行 `npm run core`。
- 核心在无控制台模式下运行，日志写入 `%LOCALAPPDATA%\ShiyuanData\core.log`。

### Windows 构建注意事项（MinGW 工具链）

仓库路径含中文时，MinGW 的 `ld.exe` 会把 UTF-8 路径按 GBK 解析，链接阶段报
`cannot find ...\<xxx>.rlib: No error`（文件其实存在）。把 target 目录挪到纯 ASCII 路径即可：

```powershell
$env:CARGO_TARGET_DIR="D:\cargo-target\shiyuan"
cd desktop; npm run tauri dev
```

`cargo check` / `cargo build` 同样需要这个环境变量。另外 `src-tauri/Cargo.toml` 的
`crate-type` 只保留 `rlib`——MinGW 的 ld 链接 `cdylib` 时会报 `export ordinal too large`
（Tauri 移动端才需要 staticlib/cdylib，本项目只做 Windows 桌面）。

换用 MSVC 工具链（`rustup default stable-x86_64-pc-windows-msvc` + VS Build Tools）则不受影响。

## MCP（供 Codex / Claude Code / Trae 等 Agent 调用）

内置一个只读 MCP 服务，直接读取桌面端同一个 SQLite 库（WAL，可与桌面端同时运行）。
它不扫描、不解析、不改原件，只把已索引的文件、正文、页码/工作表定位与覆盖面暴露给 Agent。

```powershell
cd core
python -m shiyuan.mcp_server                 # stdio 模式
python -m shiyuan.mcp_server --print-config  # 打印客户端 JSON 配置
```

也可以从任意目录启动（不需要设置工作目录）：

```powershell
python D:\path\to\拾源\core\mcp_server.py
```

主流客户端都支持 `command` + `args` + `cwd` + `env` 的 stdio 配置，把下面这段放进各自的
`mcpServers` 即可（Claude Code、Codex、Trae、Qoder、WorkBuddy、DeepSeek harness 等）：

```json
{
  "mcpServers": {
    "shiyuan": {
      "command": "python",
      "args": ["-m", "shiyuan.mcp_server"],
      "cwd": "D:\\1_yanjiusheng\\实习项目\\拾源\\core",
      "env": { "SHIYUAN_HOME": "C:\\Users\\<你>\\AppData\\Local\\ShiyuanData" }
    }
  }
}
```

如果客户端不设置 cwd，把 `args` 改成 `["D:\\...\\core\\mcp_server.py"]` 即可。

安装版不需要单独的 MCP exe：MCP 已合入 `shiyuan-core.exe`，用 `--mcp` 启动即可。
```
"%LOCALAPPDATA%\Shiyuan\shiyuan-core.exe" --mcp
```
对应配置：
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
安装版也支持 `shiyuan-core.exe --mcp --print-config` 直接生成这段配置。桌面端正常启动时仍走 HTTP 模式。

> 首次启动 MCP 时，onefile 需要先解包核心（本机实测约 10–15 秒），请把客户端的 MCP 启动超时
> 设为至少 30 秒；后续调用不受影响。

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| tool | `search_knowledge` | 检索文件名 / 路径 / 正文 / OCR，返回片段与页码、工作表 |
| tool | `get_document_text` | 按 `location_id` 或绝对路径读取正文，可按页 / 工作表过滤 |
| tool | `list_sources` | 授权目录、扫描健康度与文件数 |
| tool | `knowledge_stats` | 内容覆盖、类型分布、OCR 状态与维护计数 |
| resource | `shiyuan://sources` | 数据源 JSON |
| resource | `shiyuan://stats` | 统计 JSON |

推荐调用顺序：先 `search_knowledge`，再用返回的 `location_id` 调 `get_document_text`。
MCP 服务不会触发扫描；新文件仍需要先在桌面端完成扫描与索引。
排障时设置 `SHIYUAN_MCP_DEBUG=1`，日志写入 stderr，stdout 只输出 MCP 协议消息。

> MCP 与桌面端共用同一个 `shiyuan-core.exe`，不再单独分发；源码开发仍可用 `python -m shiyuan.mcp_server`。

## 验证

```powershell
cd core; python -m pytest -q          # 172 passed（另需 pip install pytest httpx）
cd desktop; npm test                  # 14 passed（Vitest + jsdom + Testing Library）
cd desktop; npm run build             # tsc --noEmit + vite build
cd desktop/src-tauri; cargo check     # 先按上文设置 CARGO_TARGET_DIR
```

## 已具备（M1 + M2 + M3 + M4）

- 添加 / 暂停 / 撤销授权本地文件夹，拒绝整盘根目录、系统目录与互相包含的授权。
- L0 扫描：文件名、路径、扩展名、大小、修改时间、稳定文件 ID；增量对账。
- 变更归因：新增、修改（内容版本 + 哈希）、同盘移动/改名（沿用同一 asset_id）、失联、数据源离线。
- 实时监听：watchdog 事件按 1.5 秒去抖、10 秒封顶合并后，只同步涉及的路径；周期对账兜底。
- STALE 语义：事件到达即标记，同步完成前不参与检索；内容未变则恢复，内容变化则写新版本。
- 检索：文件名 / 路径 + 正文混合召回，类型与数据源过滤，相关性排序；正文命中带片段、页码与工作表定位。
- L1 解析：TXT/MD/代码文本、DOCX（正文/表格/页眉页脚/批注）、PPTX（正文/表格/备注）、
  PDF 文本层（PyMuPDF，异常页自动留给 OCR）、XLSX/XLSM（计算值 + 公式原文）；
  DOC/PPT/XLS 旧格式在检测到 LibreOffice 时自动转换解析（`SHIYUAN_SOFFICE` 可指定路径，XLS 失败时回退 xlrd）。
  ZIP/7Z/RAR 安全解压并递归索引（限深、限大小、防路径穿越、SHA-256 去重）；
  EML/MSG 读取正文、附件与嵌套邮件；DXF 读取图层文字与块属性（DWG 需 ODA 转换器）；
  音视频只提取元数据、不转写。无文本层（扫描件）会被显式标记，而不是当成"内容里没有"。
- L2 多模态（OCR）：在「数据源」页按需触发「OCR 深解析」。默认策略 `auto`：
  整份扫描 PDF 用随包的 PP-OCRv6 small（RapidOCR），文本型/混合版式 PDF、图片与 PPTX 内嵌位图
  用 Windows 本机 OCR；混合版式只补缺少文本层的页（单文件最多 30 页，PPTX 最多 20 张图）。
  结果带页码与行坐标（bbox）进入同一检索与问答，可完全离线；
  「数据源与维护 → 维护」可切换为“全部 Windows”或“全部 PP-OCRv6 small”。
- 数据源合并：后添加的上级目录会自动合并已授权的下级目录（不移动原文件），重复授权同一目录
  直接复用；新目录若在已授权目录内，则直接使用最近的上级授权。
- 桌面界面：两个一级页面——「检索与对话」（找文件 / 知识对话切换，对话输入框固定在底部）
  与「数据源与维护」（数据源 / 维护切换）；包含文件卡片、正文片段、打开原件、复制路径、
  扫描/待刷新状态、内容覆盖率与维护日志。
- 问答与引用：先在本机检索证据，默认「本地摘录」（不联网）；可切换 OpenAI 兼容接口或 Ollama。
  回答带 [n] 引用，引用缺失/越界会提示；没有证据直接拒答，并说明当前内容覆盖。
- 对话管理：会话与消息保存在本机数据库，左侧可新建/切换/改名/删除对话；切换页面或重启后
  历史问答和引用仍在。同一会话里，追问（如「那第三章呢」「它的环境变量怎么配」）会把上一轮
  问题并进检索；上一轮只涉及一个数据源时会自动收窄到该数据源。
- 目录概览：问「大三下有哪些课」「pilot_batches 里有什么」时，直接按文件夹汇总子目录与文件数，
  不再拿零散正文片段硬答；问「放在哪个文件夹」时只按具体路径命中，找不到就拒答。
- 问答会结合文件名/路径加分（仅在命中少量文件时启用），因此「毒鳍表里有哪些物种」这类
  按文件名提问也能取到正文证据；两字关键词会用 LIKE 兜底，不会被长词挤出召回。
- 云端调用默认关闭，需在「对话 → 回答设置」中显式授权；只发送入选的证据片段，不上传整个文件。
- 知识库管理：解析任务账本（阶段 / 状态 / 尝试次数 / 错误）、失败自动重试 3 次；
  健康页可手动「重试失败 / 重建正文索引 / 重建 OCR / 清理旧版本缓存」，查看缓存块数与数据库大小。
- 每日预算：正文与 OCR 分别计数（默认 500 / 200 个文件/天），超出后任务留到下一天继续。
- 低质量复核：PDF 文本层出现明显乱码时会自动追加 OCR 结果；健康页显示低质量数量。
- 检索增强（查询理解）：剥离「是多少 / 是什么」等疑问词，中文按 2/3-gram 展开，
  内置科研场景同义词（样本数 ↔ 样本量、阈值 ↔ threshold 等），并支持在「对话 → 回答设置」里
  维护自定义同义词组；命中时界面提示改写后的查询。
- 引用带版本时间：每条回答引用都带文件更新时间与内容版本号，来源卡显示「更新于 …」，
  便于判断多份材料的时间条件。
- 语义检索（可选）：支持 OpenAI 兼容 embeddings 或本机 Ollama，向量存本机并按余弦相似度召回，
  再与关键词结果合并；默认关闭，开启后只在上传建索引时联网，检索仍在本机完成。
- 任务列表：健康页列出失败任务，可对单个文件重试；维护日志记录每次单文件重试。
- 撤权即清理：删除数据源同时清空目录记录、正文块与 FTS 索引，暂停的数据源不参与检索。
- 打包：PyInstaller sidecar + Tauri NSIS 安装包，安装版自动拉起 / 退出时结束核心进程。

## 明确未实现（不要误认为已具备）

- **OCR 只保留行坐标，不还原版面**：结果是“按行文本 + 每行 bbox”，没有表格结构、版面还原或手写专项；
  扫描 PDF 用 PP-OCRv6 small，图片与文本 PDF 仍用 Windows OCR；两者都不联网。
- **内置本地大模型**：安装包不含模型；Ollama 需用户自行安装并启动。
- **会话记忆**：只把最近一轮问题当作检索上下文（用于「那第三章呢」这类追问），
  不保留完整对话状态，也不能跨会话记忆；一句话里同时指代多个对象时仍可能选错。
- **跨盘移动**：仍会被识别为「新增 + 失联」，不做自动合并。
- **网络盘 / SMB**：未实测 watchdog 在这些路径上的事件可靠性，主要依赖周期对账。
- **目录级变更**：新建/删除/改名文件夹会整源重扫一次，目录很大时那次同步会明显变慢。
- **短词检索成本**：少于 3 个字符的关键词走 LIKE 扫描，语料很大时比 FTS 慢。
- **解析预算与低质量复核**：预算只按文件数计，不按耗时/字符数；低质量判断是启发式
  （替换字符占比），只对 PDF 追加 OCR，其他格式没有第二套解析器。
- **语义检索依赖外部服务**：不内置向量模型；向量相似度为全量扫描，未使用 ANN，索引很大时会变慢；
  同义词需在内置词表或设置里显式配置，跨语言提问仍未覆盖。
- **引用只校验编号**：无法证明模型每句话都忠于证据，仍需用户点开来源核对；密钥以明文存于本机数据库。
