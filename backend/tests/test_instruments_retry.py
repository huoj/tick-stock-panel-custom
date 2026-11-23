"""维表同步失败补打 (_scheduled_instruments_retry) 的判定逻辑。

背景: 09:10 的盘前同步对 free-api 的 exchanges/*/instruments 系统性不稳 ——
2026-11-09 与 2026-11-12 两次都是深市连续 503 把整次同步打掉。改成「全或无」后
旧表不会再被压坏, 但当天也没有第二次机会, 所以要在 09:40 / 10:30 各补打一次。

判定完全靠维表 parquet 的落盘日期 (同步失败不落盘), 这些用例覆盖:
成功即停 / 表陈旧 → 重跑 / 表不存在 → 重跑 / 休市 → 跳过。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.jobs import daily_pipeline


@pytest.fixture()
def data_tmp(tmp_path, monkeypatch):
    """把 settings.data_dir 指向临时目录, 不碰真实数据目录。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    return tmp_path


def _write_table(root: Path, when: datetime | None) -> None:
    """在 root/instruments/ 下放一个维表文件, 并把 mtime 设成 when。"""
    d = root / "instruments"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "instruments.parquet"
    p.write_bytes(b"stub")
    if when is not None:
        ts = when.timestamp()
        os.utime(p, (ts, ts))


def _patch_env(monkeypatch, *, trading: bool) -> list[str]:
    """隔离交易日历与 _run_tracked, 返回被投递的任务名列表。"""
    monkeypatch.setattr(
        "app.services.trading_day.is_trading_day", lambda *a, **k: trading, raising=False
    )
    ran: list[str] = []

    def _fake_tracked(fn, label):
        ran.append(label)
        return True

    monkeypatch.setattr(daily_pipeline, "_run_tracked", _fake_tracked)
    return ran


def test_synced_today_true_when_mtime_is_today(data_tmp) -> None:
    _write_table(data_tmp, datetime.now())
    assert daily_pipeline._instruments_synced_today() is True


def test_synced_today_false_when_mtime_is_yesterday(data_tmp) -> None:
    _write_table(data_tmp, datetime.now() - timedelta(days=1))
    assert daily_pipeline._instruments_synced_today() is False


def test_synced_today_false_when_file_missing(data_tmp) -> None:
    assert daily_pipeline._instruments_synced_today() is False


def test_retry_skips_when_table_already_refreshed_today(data_tmp, monkeypatch) -> None:
    """成功即停: 09:10 那次成功的话, 09:40/10:30 不该再打一遍供应商。"""
    _write_table(data_tmp, datetime.now())
    ran = _patch_env(monkeypatch, trading=True)

    daily_pipeline._scheduled_instruments_retry(object())

    assert ran == []


def test_retry_runs_when_table_is_stale(data_tmp, monkeypatch) -> None:
    """09:10 被 503 打掉 → 维表 mtime 停在昨天 → 补打。"""
    _write_table(data_tmp, datetime.now() - timedelta(days=1))
    ran = _patch_env(monkeypatch, trading=True)

    daily_pipeline._scheduled_instruments_retry(object())

    assert ran == ["instruments_retry"]


def test_retry_runs_when_table_missing(data_tmp, monkeypatch) -> None:
    """首次部署/文件被删: 没有旧表也要补。"""
    ran = _patch_env(monkeypatch, trading=True)

    daily_pipeline._scheduled_instruments_retry(object())

    assert ran == ["instruments_retry"]


def test_retry_skips_on_non_trading_day(data_tmp, monkeypatch) -> None:
    """休市日补打没有意义 (今天本来就没有新数据)。"""
    _write_table(data_tmp, datetime.now() - timedelta(days=3))
    ran = _patch_env(monkeypatch, trading=False)

    daily_pipeline._scheduled_instruments_retry(object())

    assert ran == []


def test_retry_times_are_after_premarket_sync() -> None:
    """补打时点必须晚于盘前同步 (09:10), 否则只是把同一次失败再打一遍。"""
    for hour, minute in daily_pipeline._INSTRUMENTS_RETRY_TIMES:
        assert (hour, minute) > (9, 10)
    assert daily_pipeline._INSTRUMENTS_RETRY_TIMES == ((9, 40), (10, 30))


def test_retry_passes_to_run_tracked_in_its_real_call_convention(data_tmp, monkeypatch) -> None:
    """补打传给 _run_tracked 的函数必须接受 ``on_progress`` 关键字。

    回归背景: 一开始传的是裸 lambda, 而 _run_tracked 以 ``fn(on_progress=...)``
    调用 → 实跑时 TypeError (2026-11-12 手动触发时炸出)。单测若把 _run_tracked
    stub 成不调用 fn, 这个错误就抓不到, 所以这里用**真实的调用约定**去调。
    """
    _write_table(data_tmp, datetime.now() - timedelta(days=1))
    monkeypatch.setattr(
        "app.services.trading_day.is_trading_day", lambda *a, **k: True, raising=False
    )
    seen: list[str] = []

    def _tracked_like_real(fn, label):
        # 与 _run_tracked 内部保持一致: 以关键字 on_progress 调用
        fn(on_progress=lambda *a, **k: None)
        seen.append(label)
        return True

    monkeypatch.setattr(daily_pipeline, "_run_tracked", _tracked_like_real)
    monkeypatch.setattr(
        daily_pipeline, "run_instruments_sync", lambda repo: {"instruments_rows": 1}
    )

    daily_pipeline._scheduled_instruments_retry(object())

    assert seen == ["instruments_retry"]
