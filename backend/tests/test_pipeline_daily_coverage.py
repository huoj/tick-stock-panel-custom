"""盘后管道: 指数/ETF 当日数据没到位时必须计入 stage_errors。

回归背景 (2026-11-09): ETF 全量 18 批里 11 批被 429 拒, 当天一行没进,
但 sync_and_persist_etf_daily 只要 total_rows > 0 就返回, 管道报 succeeded。
用户看到的是自选页 ETF 停在上一交易日, 而同步历史写着"成功"。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import data_integrity, preferences
from app.services.index_sync import DailyCoverage
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

TODAY = date(2026, 11, 9)

#: ETF/指数日K同步要求 KLINE_DAILY_BATCH; 不给这条能力整块会被跳过
_CAPSET = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits()})


def _stub_pipeline(tmp_path: Path, monkeypatch, coverages: list[DailyCoverage]) -> None:
    """把管道跑通, 只让 ETF 同步回传给定的覆盖结果。"""
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        daily_pipeline.instrument_sync, "sync_instruments", lambda *_: 0
    )
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *_: [])
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *_, **__: [])
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: TODAY)
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False,
        "pipeline_pull_etf": True,
        "pipeline_pull_index": False,
        "pipeline_regime_enabled": False,
        "minute_sync_enabled": False,
        "adj_factor_provider": "tickflow",
        "index_daily_batch_size": 100,
    })
    monkeypatch.setattr(
        daily_pipeline.index_sync, "sync_etf_instruments", lambda repo: 2
    )

    def fake_etf_sync(repo, capset, **kw):
        kw["coverage_out"].extend(coverages)
        return 100

    monkeypatch.setattr(
        daily_pipeline.index_sync, "sync_and_persist_etf_daily", fake_etf_sync
    )


def _fake_repo(tmp_path: Path):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: TODAY,
        get_etf_instruments=lambda: pl.DataFrame({"symbol": ["159529.SZ", "159682.SZ"]}),
        get_index_instruments=lambda: pl.DataFrame(),
        get_index_symbol_set=lambda: set(),
        refresh_index_views=lambda: None,
        refresh_cache=lambda: None,
        symbols_lagging=lambda *a, **k: [],
        earliest_daily_date=lambda: TODAY,
    )


def test_incomplete_etf_coverage_fails_pipeline(tmp_path, monkeypatch) -> None:
    """ETF 当日只覆盖 1/2 → 管道终态 failed, 错误里带缺口数。"""
    _stub_pipeline(tmp_path, monkeypatch, [DailyCoverage(
        target_date=TODAY, expected=2, covered=1, missing=1, asset_label="ETF",
    )])

    with pytest.raises(daily_pipeline.PipelineStageError) as excinfo:
        daily_pipeline.run_now(_fake_repo(tmp_path), _CAPSET)

    msg = str(excinfo.value)
    assert "ETF" in msg
    assert "1/2" in msg
    assert "覆盖异常" in msg


def test_complete_etf_coverage_stays_success(tmp_path, monkeypatch) -> None:
    """覆盖完整时不得误报失败。"""
    _stub_pipeline(tmp_path, monkeypatch, [DailyCoverage(
        target_date=TODAY, expected=2, covered=2, missing=0, asset_label="ETF",
    )])

    result = daily_pipeline.run_now(_fake_repo(tmp_path), _CAPSET)

    assert result is not None
