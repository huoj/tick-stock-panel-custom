"""日K批量同步: 429 限流必须单独识别并退避重试, 不得混同权限错误。

回归背景 (2026-11-09 15:35 盘后管道):
ETF 全量 1763 只按 100/批 = 18 批, 与股票日K、复权因子同时打同一条免费档通道
(60rpm), 3 分钟内 klines/batch 被 429 拒 11 次、ex-factors 被拒 16 次。
被拒批次的标的当天数据直接缺失, 而 sync_and_persist_etf_daily 只要
total_rows > 0 就报成功 —— 日志写着 "+88 rows" 却是历史窗口的行, 当天一行没进。

为什么必须与权限错误分开:
- 429 是"现在太快, 等会儿再来", 退避后必然成功;
- 403 是"你没有这个权限", 重试一万次也没用, 只会白耗配额。
旧代码把两者都归入通用异常, 既不做针对性退避, 又把 429 报成
"无日/周/月K线查询批量查询权限"(SDK 包装所致), 排查时严重误导。
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.services import kline_sync


class _RateLimitError(Exception):
    """模拟 SDK 的 RateLimitError (被测代码按类名识别, 不 import SDK 内部)。"""


# 让按类名匹配生效: 真实 SDK 的异常类名就是 RateLimitError
_RateLimitError.__name__ = "RateLimitError"
_RateLimitError.__qualname__ = "RateLimitError"


class _PermissionDeniedError(Exception):
    """模拟 SDK 的 PermissionError (403)。"""


_PermissionDeniedError.__name__ = "PermissionError"
_PermissionDeniedError.__qualname__ = "PermissionError"


def _frame(symbol: str, day: date = date(2026, 11, 9)) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol], "date": [day],
        "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0],
        "volume": [100.0], "amount": [1000.0],
    })


@pytest.fixture
def no_sleep(monkeypatch):
    """把退避等待替换成记录, 测试不真睡。"""
    waits: list[float] = []
    monkeypatch.setattr(kline_sync.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *a, **k: None)
    return waits


def test_rate_limit_is_retried_then_succeeds(monkeypatch, no_sleep) -> None:
    """首批 429、重试后成功 → 数据完整返回, 不丢标的。"""
    calls = {"n": 0}

    def fake_batch(chunk, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _RateLimitError("429 Too Many Requests")
        return {s: {"timestamp": [1794153600000], "open": [1.0], "high": [1.1],
                    "low": [0.9], "close": [1.0], "volume": [100.0], "amount": [1000.0]}
                for s in chunk}

    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient(fake_batch))
    out = kline_sync.sync_daily_batch(["159529.SZ"], start_time=None, end_time=None)

    assert calls["n"] == 3          # 两次 429 + 一次成功
    assert out.height == 1          # 数据最终拿到了, 没有静默丢
    assert len(no_sleep) >= 2       # 确实退避了
    # 指数退避: 后一次等待不短于前一次
    assert no_sleep[-1] >= no_sleep[0]


def test_rate_limit_exhausted_marks_symbol_failed(monkeypatch, no_sleep) -> None:
    """重试耗尽仍 429 → 该批标的进 failed_out, 上层据此判失败(而非静默成功)。"""
    def always_429(chunk, **kwargs):
        raise _RateLimitError("429 Too Many Requests")

    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient(always_429))
    failed: list[str] = []
    out = kline_sync.sync_daily_batch(
        ["159529.SZ", "159682.SZ"], start_time=None, end_time=None, failed_out=failed,
    )

    assert out.is_empty()
    assert set(failed) == {"159529.SZ", "159682.SZ"}


def test_permission_error_is_not_retried(monkeypatch, no_sleep) -> None:
    """403 权限错误不做重试 —— 重试无用, 只白耗配额。"""
    calls = {"n": 0}

    def always_403(chunk, **kwargs):
        calls["n"] += 1
        raise _PermissionDeniedError("Forbidden")

    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient(always_403))
    failed: list[str] = []
    kline_sync.sync_daily_batch(["159529.SZ"], start_time=None, end_time=None, failed_out=failed)

    assert calls["n"] == 1
    assert failed == ["159529.SZ"]
    assert no_sleep == []           # 一次都不退避


def test_rate_limit_retry_count_is_bounded(monkeypatch) -> None:
    """重试次数有上限, 不会无限挂着。"""
    monkeypatch.setattr(kline_sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *a, **k: None)
    calls = {"n": 0}

    def always_429(chunk, **kwargs):
        calls["n"] += 1
        raise _RateLimitError("429")

    monkeypatch.setattr(kline_sync, "get_client", lambda: _FakeClient(always_429))
    kline_sync.sync_daily_batch(["159529.SZ"], start_time=None, end_time=None)

    assert 1 < calls["n"] <= kline_sync._RATE_LIMIT_MAX_ATTEMPTS


class _FakeClient:
    def __init__(self, batch_fn):
        self.klines = _FakeKlines(batch_fn)


class _FakeKlines:
    def __init__(self, batch_fn):
        self.batch = batch_fn
