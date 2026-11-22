"""PyInstaller 打包入口：桌面端以 sidecar 形式拉起本地核心。

`python -m shiyuan` 依赖包上下文，冻结后不可用；这里显式导入 app 模块，
既保证包结构可用，也让 PyInstaller 的静态分析能收集到 `shiyuan.api`。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 源码直接运行时，保证 core/ 在 sys.path 中（冻结后此路径无效但无害）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shiyuan.config import app_home


def _bind_log_streams() -> None:
    """--noconsole 打包后 stdout/stderr 为 None，uvicorn 的日志配置会直接报错。

    把两者接到应用目录下的日志文件：既避免崩溃，也保留 sidecar 的诊断信息。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_file = open(app_home() / "core.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file


def _log_bootstrap_error() -> None:
    """--noconsole 没有 stderr，启动期异常写入应用目录，便于安装版排障。"""
    try:
        path = app_home() / "bootstrap-error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            traceback.print_exc(file=handle)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        if "--mcp" in args or "--print-config" in args:
            # MCP 模式必须保留真实 stdout，不能重定向到日志文件。
            from shiyuan.mcp_server import main as mcp_main

            mcp_main([arg for arg in args if arg != "--mcp"])
        else:
            from shiyuan.instance import acquire_http_instance

            port = int(os.environ.get("SHIYUAN_PORT", "8737"))
            if not acquire_http_instance(port):
                raise SystemExit(0)
            _bind_log_streams()
            from shiyuan import api  # noqa: F401  让打包工具收集 HTTP 应用
            from shiyuan.__main__ import main

            main()
    except SystemExit:
        raise
    except BaseException:
        _log_bootstrap_error()
        raise
