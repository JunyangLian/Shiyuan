"""可直接用绝对路径启动的拾源 MCP 入口。

MCP 客户端配置里写：
    python <repo>/core/mcp_server.py

这样不需要设置 working directory；模块方式仍然可用：
    cd core && python -m shiyuan.mcp_server
"""

from __future__ import annotations

import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from shiyuan.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
