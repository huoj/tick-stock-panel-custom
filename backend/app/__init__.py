"""Tick Stock Panel backend."""

import json
import logging
import os
import sys
from pathlib import Path

# 版本号唯一权威 = frontend/package.json: 安装包 AppVersion (release.yml 读它传给
# ISCC)、macOS 包元数据 (tsp.spec 读它)、页面版本徽标与更新检查 (本模块) 全部同源,
# 改版本只改那一份文件。dev 模式读仓库文件; PyInstaller 冻结后读 _MEIPASS/package.json
# (tsp.spec 的 datas 会收集)。读取失败回退硬编码值 —— 版本号绝不阻塞应用启动。
# 三处伴生标记 (VERSION / backend/pyproject.toml / _FALLBACK_VERSION) 由
# scripts/sync_versions.py 从 package.json 派生, release/docker 流水线自动执行。
_FALLBACK_VERSION = "0.3.37"


def _load_version() -> str:
    if getattr(sys, "frozen", False):
        pkg = Path(getattr(sys, "_MEIPASS", ".")) / "package.json"
    else:
        # backend/app/__init__.py 上溯两级 = 项目根
        pkg = Path(__file__).resolve().parents[2] / "frontend" / "package.json"
    try:
        return json.loads(pkg.read_text(encoding="utf-8"))["version"]
    except Exception as e:
        # 静默降级但必须可观测 (issue #513: Docker 镜像缺该文件时整站显示旧版本,
        # 无任何日志线索)。Dockerfile 已 COPY 兜底, 走到这通常是打包配置坏了。
        logging.getLogger(__name__).warning(
            "读 %s 失败 (%s), 回退 _FALLBACK_VERSION=%s", pkg, e, _FALLBACK_VERSION
        )
        return _FALLBACK_VERSION


__version__ = _load_version()

# Windows 默认 stdout/stderr 编码为 GBK(cp936),TickFlow SDK 内部输出含 emoji 的
# 指数/标的名称(如 \U0001f193)时会抛 UnicodeEncodeError,导致请求失败。
# 进程加载最早阶段强制 UTF-8,根治此类编码崩溃。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

# 系统代理工具常把 no_proxy 写成 "...,[::1]" 这种带方括号的 IPv6 写法, 而 httpx 的
# proxy pattern 解析不认方括号: 走本地回环的请求直接抛
# InvalidURL("Invalid port: ':1]'")。后果不是"个别请求失败", 而是所有 HTTP 出站
# 一起挂 —— 实时行情连续失败会把 realtime_quotes_enabled 自动降级成 False。
# 这条清洗以前只写在 run-local.py 启动脚本里, 于是 pytest / uvicorn 直起 / Docker
# 等入口全部中招。挪到包导入的最早阶段, 任何进程进来都先修好环境。
# 幂等: 已干净的值原样不动, 不覆盖用户配置的其它条目。
for _np_key in ("NO_PROXY", "no_proxy"):
    _np_raw = os.environ.get(_np_key)
    if not _np_raw:
        continue
    _np_items = [p.strip() for p in _np_raw.split(",") if p.strip()]
    _np_kept = [p for p in _np_items if not (p.startswith("[") and p.endswith("]"))]
    if len(_np_kept) == len(_np_items) and "::1" in _np_kept:
        continue
    if "::1" not in _np_kept:
        _np_kept.append("::1")
    os.environ[_np_key] = ",".join(_np_kept)
