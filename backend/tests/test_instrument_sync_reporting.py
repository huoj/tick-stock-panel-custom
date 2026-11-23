"""维表同步不完整时, 调用侧必须如实上报 (不得静默当作成功)。

回归背景 (2026-11-09): instrument_sync 旧实现「部分成功就落盘」, 而调用侧
把返回的 2823 行当成成功 —— 盘前任务 job_store 记 succeeded, 盘后管道
不进 stage_errors, 用户看到的是"同步完成", 实际 52% 标的换手率已变空。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import data_integrity, preferences
from app.services import instrument_sync as isync
from app.tickflow.capabilities import CapabilitySet


def _write_existing(data_dir: Path) -> None:
    out = data_dir / "instruments"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "code": ["600000", "000001"],
        "exchange": ["SH", "SZ"],
        "region": ["CN", "CN"],
        "type": ["stock", "stock"],
        "listing_date": ["1999-11-10", "1991-04-03"],
        "total_shares": [2.9e10, 1.9e10],
        "float_shares": [2.9e10, 1.9e10],
        "tick_size": [0.01, 0.01],
        "limit_up": None,
        "limit_down": None,
    }).write_parquet(out / "instruments.parquet")


def _incomplete(data_dir):
    raise isync.InstrumentSyncError(("SZ",), ["SZ: 503 Service Unavailable"])


def test_premarket_sync_propagates_incompleteness(tmp_path, monkeypatch) -> None:
    """盘前同步不完整 → 异常上抛, 由 _run_tracked 把任务标 failed。

    旧行为: 返回 {"instruments_rows": 2823} → job_store.succeed → 用户看到成功。
    """
    _write_existing(tmp_path)
    monkeypatch.setattr(isync, "sync_instruments", _incomplete)
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(isync.InstrumentSyncError):
        daily_pipeline.run_instruments_sync(repo)

    # 关键: 上一版完整维表没有被残缺数据覆盖
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df.height == 2
    assert set(df["exchange"].to_list()) == {"SH", "SZ"}


def test_pipeline_records_incompleteness_as_stage_failure(tmp_path, monkeypatch) -> None:
    """盘后管道 Step 0 不完整 → 计入 stage_errors, 终态 failed; 后续阶段照常跑。

    与 run_now 里其他软失败阶段同一套语义 (末尾 throw PipelineStageError)。
    """
    _write_existing(tmp_path)
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        daily_pipeline.instrument_sync, "sync_instruments", _incomplete
    )
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *_: [])
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *_, **__: [])
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False, "pipeline_pull_etf": False,
        "pipeline_pull_index": False, "pipeline_regime_enabled": False,
        "minute_sync_enabled": False, "adj_factor_provider": "tickflow",
    })

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date(2026, 11, 9),
    )

    with pytest.raises(daily_pipeline.PipelineStageError) as excinfo:
        daily_pipeline.run_now(repo, CapabilitySet(set()))

    assert "instruments 维表同步不完整" in str(excinfo.value)
    # 失败原因可追溯到具体交易所
    assert "SZ" in str(excinfo.value)
