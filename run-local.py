"""本地启动器（Windows）。

用途：在 Python 进程内部修正从父进程继承的代理环境变量，再启动 uvicorn。

背景：本机代理工具把 NO_PROXY 写成 `127.0.0.1,localhost,::1,[::1]`，
httpx 解析 `[::1]` 这一项时会抛出 `InvalidURL: Invalid port: ':1]'`，
导致 TickFlow 客户端无法创建。外层环境变量注入会覆盖子进程的 set，
因此只能在本进程启动时（import httpx 之前）就地清洗。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe ..\\run-local.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_BACKEND = _ROOT / "backend"
_ENV_FILE = _ROOT / ".env"

# 必须在任何 httpx / tickflow 导入之前执行
for _key in ("NO_PROXY", "no_proxy"):
    if _key in os.environ:
        _parts = [p.strip() for p in os.environ[_key].split(",") if p.strip()]
        # 去掉带方括号的 IPv6 写法（httpx 的 proxy pattern 解析不认 `[::1]`）
        _parts = [p for p in _parts if not (p.startswith("[") and p.endswith("]"))]
        if "::1" not in _parts:
            _parts.append("::1")
        os.environ[_key] = ",".join(_parts)

_HOST = os.environ.get("TSP_HOST", "0.0.0.0")
_PORT = int(os.environ.get("TSP_PORT", "3018"))

if __name__ == "__main__":
    sys.path.insert(0, str(_BACKEND))
    os.chdir(_BACKEND)

    if _ENV_FILE.exists():
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE, override=False)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=_HOST,
        port=_PORT,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
