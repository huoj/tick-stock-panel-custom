"""盘后日K补拉: 当日日K发布晚于管道时刻时, 按点重试且成功即停。

回归背景 (2026-11-11 实测): 免费档供应商的**当日日K在收盘后 1~2 小时才发布**
(tiers.yaml: none/free 档"仅历史日K / 盘中不会实时更新")。15:35 的盘后管道
18/18 批全部"成功"返回、共 1718 行, 但落盘只有上一交易日分区, 当天一行没进 —
用户看到自选页 ETF 停在上一交易日, 而同步历史写着"成功"。

同一接口、同一窗口在 18:58 手动拉一次就成功 (1718 只), 19:03 查询接口已有当日数据。
所以问题在**调度时刻**, 不在拉取代码: 15:35 时供应商还没发布当日日K。

修复: 另加补拉任务, 在配置的多个时点各打一次; 每次先只读校验当日分区的真实覆盖,
齐全就跳过 (成功即停), 覆盖不足才重跑管道。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.jobs import daily_pipeline
from app.services import preferences

TODAY = date(2026, 11, 11)
PREV = TODAY - timedelta(days=1)
_SYMS = [f"{i:06d}.SZ" for i in range(10)]


@pytest.fixture
def prefs_tmp(tmp_path, monkeypatch):
    """把 preferences 落到 tmp, 不碰真实用户配置。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    yield tmp_path
    preferences._invalidate_cache()


def _prefs(monkeypatch, **over):
    data = {
        "pipeline_pull_a_share": True,
        "pipeline_pull_etf": True,
        "pipeline_pull_index": True,
    }
    data.update(over)
    monkeypatch.setattr(preferences, "load", lambda: dict(data))


def _write_partition(data_dir: Path, table: str, day: date, symbols: list[str]) -> None:
    part = data_dir / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": symbols,
        "date": [day] * len(symbols),
        "close": [1.0] * len(symbols),
    }).write_parquet(part / "part.parquet")


def _repo(data_dir: Path):
    return SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))


# ── 调度偏好 ──────────────────────────────────────────────

def test_backfill_schedule_default(prefs_tmp) -> None:
    """默认开启, 18:30 / 19:30 / 20:30 三次尝试。"""
    assert preferences.get_daily_backfill_schedule() == {
        "enabled": True, "hours": [18, 19, 20], "minute": 30,
    }


def test_backfill_schedule_clamps_and_dedups(prefs_tmp) -> None:
    """越界的小时丢弃 (夹取会造出早于盘后管道的补拉), 重复项去掉, 分钟夹到 59。"""
    got = preferences.set_daily_backfill_schedule(True, [9, 18, 18, 25], 90)
    assert got == {"enabled": True, "hours": [18], "minute": 59}
    assert preferences.get_daily_backfill_schedule() == got


def test_backfill_schedule_falls_back_when_all_hours_invalid(prefs_tmp) -> None:
    """小时全非法 → 回退默认时点, 而不是留下一个永不触发的空调度。"""
    got = preferences.set_daily_backfill_schedule(True, [1, 2, 3], 30)
    assert got["hours"] == [18, 19, 20]


def test_backfill_schedule_can_be_disabled(prefs_tmp) -> None:
    got = preferences.set_daily_backfill_schedule(False, [18], 0)
    assert got["enabled"] is False
    assert preferences.get_daily_backfill_schedule()["enabled"] is False


# ── 当日覆盖前置检查 ──────────────────────────────────────

def test_gaps_empty_when_today_complete(tmp_path, monkeypatch) -> None:
    """三族当日分区与上一交易日齐平 → 无缺口 (补拉应直接跳过)。"""
    _prefs(monkeypatch)
    for table in ("kline_daily", "kline_etf_daily", "kline_index_daily"):
        _write_partition(tmp_path, table, PREV, _SYMS)
        _write_partition(tmp_path, table, TODAY, _SYMS)

    assert daily_pipeline._daily_backfill_gaps(_repo(tmp_path), TODAY) == []


def test_gaps_reports_partial_today(tmp_path, monkeypatch) -> None:
    """当日分区只写了一部分标的 (拉取被限流/供应商未发布) → 报缺口。"""
    _prefs(monkeypatch)
    for table in ("kline_daily", "kline_etf_daily", "kline_index_daily"):
        _write_partition(tmp_path, table, PREV, _SYMS)
    _write_partition(tmp_path, "kline_daily", TODAY, _SYMS)
    _write_partition(tmp_path, "kline_etf_daily", TODAY, _SYMS[:2])
    _write_partition(tmp_path, "kline_index_daily", TODAY, _SYMS)

    gaps = daily_pipeline._daily_backfill_gaps(_repo(tmp_path), TODAY)

    assert gaps == ["ETF(2/10)"]


def test_gaps_reports_missing_today_partition(tmp_path, monkeypatch) -> None:
    """当日分区整个不存在 = 零覆盖, 必须报缺口 (2026-11-11 的真实形态)。"""
    _prefs(monkeypatch)
    for table in ("kline_daily", "kline_etf_daily", "kline_index_daily"):
        _write_partition(tmp_path, table, PREV, _SYMS)
    _write_partition(tmp_path, "kline_daily", TODAY, _SYMS)
    _write_partition(tmp_path, "kline_index_daily", TODAY, _SYMS)

    assert daily_pipeline._daily_backfill_gaps(_repo(tmp_path), TODAY) == ["ETF(0/10)"]


def test_gaps_skips_disabled_families(tmp_path, monkeypatch) -> None:
    """用户关掉的拉取开关不参与补拉判定 (别为没开的数据反复重跑管道)。"""
    _prefs(monkeypatch, pipeline_pull_etf=False, pipeline_pull_index=False)
    _write_partition(tmp_path, "kline_daily", PREV, _SYMS)
    _write_partition(tmp_path, "kline_daily", TODAY, _SYMS)

    assert daily_pipeline._daily_backfill_gaps(_repo(tmp_path), TODAY) == []


# ── 补拉任务: 成功即停 / 缺口才跑 / 休市不跑 ──────────────

def _spy_pipeline():
    calls = []

    def fn(on_progress=None):
        calls.append(on_progress)
        return {"ok": True}

    return fn, calls


def _patch_backfill_env(monkeypatch, *, trading: bool | None):
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: TODAY)
    monkeypatch.setattr(
        "app.services.trading_day.is_trading_day", lambda now=None: trading
    )
    ran = []
    monkeypatch.setattr(
        daily_pipeline, "_run_tracked",
        lambda fn, label: ran.append(label),
    )
    return ran


def test_backfill_skips_when_complete(tmp_path, monkeypatch) -> None:
    """当日已齐全 → 不重跑管道 (成功即停)。"""
    _prefs(monkeypatch)
    for table in ("kline_daily", "kline_etf_daily", "kline_index_daily"):
        _write_partition(tmp_path, table, PREV, _SYMS)
        _write_partition(tmp_path, table, TODAY, _SYMS)
    ran = _patch_backfill_env(monkeypatch, trading=True)
    fn, calls = _spy_pipeline()

    daily_pipeline._scheduled_backfill_task(fn, _repo(tmp_path))

    assert calls == []
    assert ran == []


def test_backfill_runs_pipeline_when_gap(tmp_path, monkeypatch) -> None:
    """当日 ETF 缺 → 交给 _run_tracked 重跑管道, 任务名标为 daily_backfill。

    (真正调用 pipeline_fn 是 _run_tracked 的职责, 这里只校验补拉的判定与投递。)
    """
    _prefs(monkeypatch)
    for table in ("kline_daily", "kline_etf_daily", "kline_index_daily"):
        _write_partition(tmp_path, table, PREV, _SYMS)
    _write_partition(tmp_path, "kline_daily", TODAY, _SYMS)
    _write_partition(tmp_path, "kline_index_daily", TODAY, _SYMS)
    ran = _patch_backfill_env(monkeypatch, trading=True)
    fn, _calls = _spy_pipeline()

    daily_pipeline._scheduled_backfill_task(fn, _repo(tmp_path))

    assert ran == ["daily_backfill"]


def test_backfill_skips_non_trading_day(tmp_path, monkeypatch) -> None:
    """休市 (含节假日) 不重跑: 补拉不解决"今天本来就没有数据"。"""
    _prefs(monkeypatch)
    for table in ("kline_daily", "kline_etf_daily", "kline_index_daily"):
        _write_partition(tmp_path, table, PREV, _SYMS)
    ran = _patch_backfill_env(monkeypatch, trading=False)
    fn, calls = _spy_pipeline()

    daily_pipeline._scheduled_backfill_task(fn, _repo(tmp_path))

    assert calls == []
    assert ran == []
