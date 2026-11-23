"""信号风险画像与统计测试 —— 分数/胜率/区间/样本量/流动性/仓位上限。

用合成历史帧，不依赖真实 data/。重点是几条**不能说谎**的不变式：
  - Wilson 区间在小样本处不退化（3/3 的下界必须明显低于 1）
  - 样本不足时分数不会因高胜率而抬高
  - 「分数」与「胜率」是两套字段，且分数带「不是概率」的说明
  - 潜在回撤取前瞻收益低分位，不是 0
  - 流动性未知 / 回撤缺失时仓位上限退回保守默认，且依据可读
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from app.services import signal_risk as sr


def _history(symbols: list[str], days: int, *, hit: list[bool], close: list[float],
             amount: float = 50_000_000.0) -> pl.DataFrame:
    days_list = [dt.date(2026, 1, 5) + dt.timedelta(days=i) for i in range(days)]
    rows = []
    for sym in symbols:
        for i, day in enumerate(days_list):
            rows.append({
                "symbol": sym,
                "date": day,
                "close": close[i],
                "raw_close": close[i],
                "amount": amount,
                "csg_demo": hit[i % len(hit)],
            })
    return pl.DataFrame(rows)


# --- Wilson 区间 ----------------------------------------------------------


def test_wilson_interval_does_not_degenerate_at_extremes():
    """3/3 全胜：正态近似会给 [1,1]，Wilson 必须给出宽区间。"""
    low, high = sr.wilson_interval(3, 3)
    assert low < 0.6, low          # 下界必须明显低于 1
    assert high <= 1.0
    assert high > low

    low0, high0 = sr.wilson_interval(0, 3)
    assert high0 > 0.4             # 上界不能是 0
    assert low0 >= 0.0


def test_wilson_interval_narrows_with_more_samples():
    small = sr.wilson_interval(6, 10)
    large = sr.wilson_interval(600, 1000)
    assert (large[1] - large[0]) < (small[1] - small[0])


def test_wilson_interval_zero_total():
    assert sr.wilson_interval(0, 0) == (0.0, 1.0)


# --- 分数 vs 胜率 ---------------------------------------------------------


def test_signal_score_rewards_lower_confidence_bound():
    """同样的胜率，区间下界高的那个分数更高 —— 分数用的不是胜率本身。"""
    strong = sr.signal_score(win_rate=0.6, ci_low=0.55, excess_mean=0.02,
                             samples=500, potential_drawdown=-0.05)
    weak = sr.signal_score(win_rate=0.6, ci_low=0.30, excess_mean=0.02,
                           samples=5, potential_drawdown=-0.05)
    assert strong > weak


def test_small_sample_high_win_rate_does_not_get_high_score():
    """3 笔全胜（胜率 100%）不能得到高分数。"""
    lucky = sr.signal_score(win_rate=1.0, ci_low=sr.wilson_interval(3, 3)[0],
                            excess_mean=0.01, samples=3, potential_drawdown=-0.03)
    trusted = sr.signal_score(win_rate=0.58, ci_low=sr.wilson_interval(580, 1000)[0],
                              excess_mean=0.02, samples=1000, potential_drawdown=-0.04)
    assert lucky < trusted
    assert lucky <= 60


def test_signal_score_bounded_0_100():
    assert 0 <= sr.signal_score(win_rate=None, ci_low=None, excess_mean=None,
                                samples=0, potential_drawdown=None) <= 100
    assert 0 <= sr.signal_score(win_rate=1.0, ci_low=1.0, excess_mean=1.0,
                                samples=10_000, potential_drawdown=0.0) <= 100


def test_signal_score_penalises_deep_drawdown():
    shallow = sr.signal_score(win_rate=0.55, ci_low=0.5, excess_mean=0.02,
                              samples=200, potential_drawdown=-0.02)
    deep = sr.signal_score(win_rate=0.55, ci_low=0.5, excess_mean=0.02,
                           samples=200, potential_drawdown=-0.25)
    assert shallow > deep


# --- 流动性 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "bucket"),
    [
        (5_000_000_000.0, "high"),
        (200_000_000.0, "mid"),
        (5_000_000.0, "low"),
        (None, "unknown"),
        (0.0, "unknown"),
    ],
)
def test_liquidity_bucket(amount, bucket):
    assert sr.liquidity_bucket(amount) == bucket


# --- 仓位上限 -------------------------------------------------------------


def test_position_cap_uses_risk_budget_and_liquidity():
    cap, basis = sr.position_cap_pct(
        declared_cap=None, potential_drawdown=-0.05, liquidity="high"
    )
    assert cap == pytest.approx(30.0) or cap <= 30.0
    assert "风险预算" in basis


def test_position_cap_lower_liquidity_reduces_cap():
    high, _ = sr.position_cap_pct(declared_cap=None, potential_drawdown=-0.05, liquidity="high")
    low, basis = sr.position_cap_pct(declared_cap=None, potential_drawdown=-0.05, liquidity="low")
    assert low < high
    assert "流动性 low" in basis


def test_position_cap_respects_declared_ceiling():
    cap, basis = sr.position_cap_pct(
        declared_cap=3.0, potential_drawdown=-0.02, liquidity="high"
    )
    assert cap == pytest.approx(3.0)
    assert "用户声明上限" in basis


def test_position_cap_without_drawdown_is_conservative():
    cap, basis = sr.position_cap_pct(declared_cap=None, potential_drawdown=None, liquidity="unknown")
    assert cap <= 10.0
    assert "默认" in basis


# --- 端到端统计 -----------------------------------------------------------


@pytest.fixture
def trend_history() -> pl.DataFrame:
    """一个真实有效的信号：收盘价上行 + 每天触发。"""
    closes = [10.0 + 0.1 * i for i in range(40)]
    return _history(["600001.SH"], 40, hit=[True], close=closes)


@pytest.fixture
def losing_history() -> pl.DataFrame:
    closes = [10.0 - 0.1 * i for i in range(40)]
    return _history(["600001.SH"], 40, hit=[True], close=closes)


def test_compute_stats_reports_samples_win_rate_and_ci(trend_history):
    stats = sr.compute_signal_stats(
        signal_id="demo", column="csg_demo", history=trend_history, horizon_days=5,
        reason="上涨趋势中每天触发", falsify="收盘跌破 MA20",
    )
    body = stats.to_dict()
    assert body["samples"] > 0
    assert body["win_rate"] == pytest.approx(1.0)
    ci = body["confidence_interval"]
    assert ci["method"] == "wilson"
    # 全胜但样本有限 → 下界必须低于 1，不能显示"100% 确定"
    assert ci["low"] < 0.95
    assert body["signal_score"]["meaning"].startswith("排序用强度指标")
    assert "confidence_interval" in body and "signal_score" in body
    assert body["reason"] and body["falsify"]


def test_compute_stats_verdict_flags_small_samples(trend_history):
    stats = sr.compute_signal_stats(
        signal_id="demo", column="csg_demo", history=trend_history, horizon_days=5,
        min_samples=100,
    )
    assert stats.verdict == sr.VERDICT_INSUFFICIENT
    assert any("样本" in t for t in stats.limitations)


def test_compute_stats_detects_losing_signal(losing_history):
    stats = sr.compute_signal_stats(
        signal_id="demo", column="csg_demo", history=losing_history, horizon_days=5
    )
    assert stats.win_rate is not None and stats.win_rate < 0.5
    assert stats.signal_score < 50


def test_potential_drawdown_is_a_real_low_quantile(trend_history):
    """上涨趋势里 5% 分位不会太差，但必须是一个真实样本而不是 0。"""
    stats = sr.compute_signal_stats(
        signal_id="demo", column="csg_demo", history=trend_history, horizon_days=5
    )
    assert stats.potential_drawdown is not None
    assert stats.best is not None and stats.worst is not None
    assert stats.worst <= stats.potential_drawdown <= stats.best


def test_compute_stats_missing_column_returns_empty_with_limitation(trend_history):
    stats = sr.compute_signal_stats(
        signal_id="ghost", column="csg_ghost", history=trend_history, horizon_days=5
    )
    assert stats.samples == 0
    assert stats.verdict == sr.VERDICT_NO_SAMPLES
    assert stats.win_rate is None


def test_compute_stats_never_triggers_returns_no_samples(trend_history):
    no_hit = trend_history.with_columns(pl.lit(False).alias("csg_demo"))
    stats = sr.compute_signal_stats(
        signal_id="demo", column="csg_demo", history=no_hit, horizon_days=5
    )
    assert stats.samples == 0
    assert any("没有可评估" in t for t in stats.limitations)


def test_stats_to_dict_separates_score_from_win_rate(trend_history):
    body = sr.compute_signal_stats(
        signal_id="demo", column="csg_demo", history=trend_history, horizon_days=5
    ).to_dict()
    # 两个字段各自独立，且顶层不出现歧义命名
    assert body["win_rate"] is not None
    assert body["signal_score"]["value"] != body["win_rate"]
    assert "概率" in body["signal_score"]["meaning"]
    assert {"samples", "confidence_interval", "potential_drawdown",
            "liquidity", "position_cap", "expected_holding_days"} <= set(body)


# --- 历史加载 -------------------------------------------------------------


def test_load_enriched_history_reads_partitions(tmp_path):
    base = tmp_path / "kline_daily_enriched"
    rows = []
    for i in range(12):
        day = dt.date(2026, 1, 5) + dt.timedelta(days=i)
        part = base / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["600001.SH"], "date": [day], "close": [10.0],
            "raw_close": [10.0], "amount": [1.0e8], "csg_demo": [True],
        }).write_parquet(part / "part.parquet")
        rows.append(day)
    frame = sr.load_enriched_history(tmp_path, lookback_days=5)
    assert frame.height == 5
    assert frame["date"].max() == max(rows)


def test_load_enriched_history_missing_dir_returns_empty(tmp_path):
    assert sr.load_enriched_history(tmp_path, lookback_days=5).is_empty()
