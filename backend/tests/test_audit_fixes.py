"""审查修复的回归测试: 换行翻译 / 空分钟目录 / 代理环境变量 / 策略编码。

四条都是 2026-11-12 全量审查中确认的真实缺陷，且都能在旧代码上稳定复现。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest

from app.services import config_backup, fs_utils

# ── 1. atomic_write_text 不得改写行尾 ────────────────────────────────

def test_atomic_write_text_preserves_crlf(tmp_path: Path) -> None:
    """CRLF 文本原样落盘。

    旧实现用默认 newline 写, Windows 上把 `\\r\\n` 变成 `\\r\\r\\n`, 读回 (通用换行)
    就成了两个换行 —— 配置备份恢复/资源导入每往返一次多一个空行。
    """
    target = tmp_path / "a.py"
    text = 'META = {"id": "x"}\r\nimport polars as pl\r\n'

    fs_utils.atomic_write_text(target, text)

    assert target.read_bytes() == text.encode("utf-8")


def test_atomic_write_text_preserves_lf(tmp_path: Path) -> None:
    """LF 文本同样不得被"本地化"成 CRLF。"""
    target = tmp_path / "b.json"
    text = '{\n  "k": 1\n}\n'

    fs_utils.atomic_write_text(target, text)

    assert target.read_bytes() == text.encode("utf-8")


def test_write_payload_roundtrips_bytes_exactly(tmp_path: Path) -> None:
    """备份/资源导入落盘必须字节精确 (这是导出→导入不漂移的前提)。"""
    original = b'# -*- coding: utf-8 -*-\r\nMETA = {"id": "x"}\r\n'
    target = tmp_path / "custom.py"

    config_backup._write_payload(target, original)

    assert target.read_bytes() == original


def test_write_payload_still_rejects_non_utf8_text(tmp_path: Path) -> None:
    """改走字节写之后, 非法 UTF-8 的文本类负载仍必须在写入前失败。"""
    target = tmp_path / "bad.py"

    with pytest.raises(UnicodeDecodeError):
        config_backup._write_payload(target, b"\xc0\xc1not utf8")

    assert not target.exists()


# ── 2. 空分钟目录不该伪装成"查询失败" ────────────────────────────────

def _repo(tmp_path: Path):
    from app.tickflow.repository import DataStore, KlineRepository

    (tmp_path / "kline_etf_minute").mkdir(parents=True, exist_ok=True)
    return KlineRepository(DataStore(tmp_path))


def test_minute_query_on_empty_dir_returns_empty_without_warning(
    tmp_path: Path, caplog,
) -> None:
    """目录存在但一个 parquet 都没有 → 空帧 + 不告警。

    polars 对空 glob 是抛 ComputeError, 旧实现让它冒到 except 分支打 WARNING:
    "ETF 还没有分钟数据"本是正常状态, 却被记成查询失败 (实测每天刷 3 条)。
    """
    import logging
    from datetime import date

    repo = _repo(tmp_path)

    with caplog.at_level(logging.WARNING):
        df = repo.get_minute("510300.SH", date(2026, 11, 12), asset_type="etf")

    assert df.is_empty()
    assert [r for r in caplog.records if "分钟K" in r.message] == []


def test_minute_batch_and_range_on_empty_dir_are_silent(tmp_path: Path, caplog) -> None:
    import logging
    from datetime import date

    repo = _repo(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert repo.get_minute_batch(["510300.SH"], date(2026, 11, 12), asset_type="etf").is_empty()
        assert repo.get_minute_range(
            ["510300.SH"], date(2026, 11, 1), date(2026, 11, 12), asset_type="etf"
        ).is_empty()

    assert [r for r in caplog.records if "分钟K" in r.message] == []


def test_minute_query_still_reads_existing_partition(tmp_path: Path) -> None:
    """有空目录短路之后, 真有数据的路径不得被误伤。"""
    from datetime import date, datetime

    repo = _repo(tmp_path)
    day = date(2026, 11, 12)
    part = tmp_path / "kline_etf_minute" / f"date={day.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["510300.SH"],
        "datetime": [datetime(2026, 11, 12, 9, 31)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    }).write_parquet(part / "part.parquet")

    df = repo.get_minute("510300.SH", day, asset_type="etf")

    assert df.height == 1
    assert df["symbol"][0] == "510300.SH"


# ── 3. no_proxy 清洗必须在包导入时发生 ──────────────────────────────

def test_app_import_sanitizes_bracketed_ipv6_no_proxy(tmp_path: Path) -> None:
    """`app` 一被导入, 带方括号的 IPv6 就该从 no_proxy 里消失。

    httpx 解析不了 `[::1]` (抛 InvalidURL: Invalid port: ':1]'), 而这条清洗
    以前只写在 run-local.py 启动脚本里 —— pytest / uvicorn 直起 / Docker 全部中招。
    """
    env = dict(os.environ)
    env["no_proxy"] = "127.0.0.1,localhost,::1,[::1]"
    env["NO_PROXY"] = "127.0.0.1,localhost,[::1]"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    out = subprocess.run(
        [sys.executable, "-c",
         "import app, os;"
         "print(os.environ['no_proxy']);"
         "print(os.environ['NO_PROXY'])"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )

    assert out.returncode == 0, out.stderr
    lower, upper = out.stdout.strip().splitlines()
    assert "[" not in lower and "::1" in lower
    assert "[" not in upper and "::1" in upper


def test_app_import_keeps_clean_no_proxy_untouched() -> None:
    """已经干净的值不该被反复改写 (幂等)。"""
    env = dict(os.environ)
    env["no_proxy"] = "127.0.0.1,localhost,::1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    out = subprocess.run(
        [sys.executable, "-c", "import app, os; print(os.environ['no_proxy'])"],
        capture_output=True, text=True, env=env,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "127.0.0.1,localhost,::1"


# ── 4. 策略源码按 PEP 263 声明解码 ──────────────────────────────────

def test_strategy_source_honours_pep263_coding_declaration(tmp_path: Path) -> None:
    """头部声明 GBK 的合法策略文件必须能加载 (旧实现一律按 utf-8 读 → 拒绝)。"""
    from app.strategy.engine import StrategyEngine

    path = tmp_path / "gbk_strategy.py"
    body = (
        "# -*- coding: gbk -*-\n"
        "import polars as pl\n"
        'META = {"id": "gbk_demo", "name": "\u793a\u4f8b\u7b56\u7565", "params": []}\n'
        "def filter(df, params):\n"
        '    return pl.col("close") > 0\n'
    )
    path.write_bytes(body.encode("gbk"))

    loaded = StrategyEngine._load_file(path)

    assert loaded.meta["id"] == "gbk_demo"
    assert loaded.meta["name"] == "\u793a\u4f8b\u7b56\u7565"


def test_utf8_strategy_still_loads(tmp_path: Path) -> None:
    """无声明时缺省仍是 utf-8, 既有策略行为不变。"""
    from app.strategy.engine import StrategyEngine

    path = tmp_path / "utf8_strategy.py"
    path.write_text(
        "import polars as pl\n"
        'META = {"id": "u8_demo", "name": "\u4e2d\u6587", "params": []}\n'
        "def filter(df, params):\n"
        '    return pl.col("close") > 0\n',
        encoding="utf-8",
    )

    loaded = StrategyEngine._load_file(path)

    assert loaded.meta["id"] == "u8_demo"
