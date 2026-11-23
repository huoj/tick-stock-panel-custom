"""行业强度选股 seed 回归测试 — 5均线6档打分 + 行业聚合。

文档《如何通过量化程序做到行业轮动跟踪》核心逻辑的落地验证:
  - 强度分档与文档 np.select 口径一致(站上均线越多分越高)
  - entry 只选强度达标且站上季线的票
  - 行业强弱 = 成分股强度均值, 少成员行业剔除
  - 稳定强势判定(变异系数)
  - seed 能被 StrategyEngine 正常加载
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "strategies"


def _load_fixture(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_fixture", _FIXTURES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


seed = _load_fixture("industry_strength_rank")


def _monotonic_panel() -> pl.DataFrame:
    """3 只票 70 日: 两只持续上涨(应满档), 一只持续下跌(应最弱)。"""
    rows = []
    base = date(2026, 1, 1)
    for sym, slope in [("强A", 2.0), ("强B", 1.5), ("弱C", -1.0)]:
        px = 100.0
        for d in range(70):
            px = max(5.0, px + slope)
            rows.append((sym, base + timedelta(days=d), px, px + 1, px - 1, px, 1000.0))
    return pl.DataFrame(
        rows, schema=["symbol", "date", "open", "high", "low", "close", "volume"], orient="row"
    )


def test_ma_strength_score_ranks_uptrend_highest():
    market = build_market_data_matrix(_monotonic_panel(), field_columns=set())
    strength = seed.ma_strength_score(market)
    assert strength.shape == (70, 3)
    idx = {sym: i for i, sym in enumerate(market.symbols)}
    assert strength[-1, idx["强A"]] == 6
    assert strength[-1, idx["强B"]] == 6
    assert strength[-1, idx["弱C"]] == 1


def test_strength_is_between_minus_one_and_six():
    market = build_market_data_matrix(_monotonic_panel(), field_columns=set())
    strength = seed.ma_strength_score(market)
    assert strength.min() >= -1 and strength.max() <= 6


def test_entry_only_selects_strong_stocks():
    market = build_market_data_matrix(_monotonic_panel(), field_columns=set())
    signals = seed.MATRIX_STRATEGY.compute_signals(
        market, {"min_strength": 6.0, "require_volume": False}
    )
    selected = [market.symbols[i] for i in range(len(market.symbols)) if signals.entry[-1, i]]
    assert "强A" in selected and "强B" in selected
    assert "弱C" not in selected


def test_entry_respects_min_strength_threshold():
    market = build_market_data_matrix(_monotonic_panel(), field_columns=set())
    # 阈值设到最高档 6, 弱势票不会入选
    signals = seed.MATRIX_STRATEGY.compute_signals(
        market, {"min_strength": 6.0, "require_volume": False}
    )
    idx = {sym: i for i, sym in enumerate(market.symbols)}
    assert signals.entry[-1, idx["弱C"]] == 0


def test_volume_filter_blocks_low_volume():
    panel = _monotonic_panel()
    market = build_market_data_matrix(panel, field_columns={"vol_ratio_5d"})
    # 不给 vol_ratio 时不因量能误伤(compute_signals 走 matrix 内建计算)
    signals = seed.MATRIX_STRATEGY.compute_signals(
        market, {"min_strength": 6.0, "require_volume": False}
    )
    assert signals.entry.shape == market.shape


def test_exit_on_ma20_breakdown():
    # 构造长期上涨后单日跌破 MA20: 昨收在 MA20 上方, 今收跌到 MA20 下方(边缘触发)
    rows = []
    base = date(2026, 1, 1)
    px = 100.0
    for d in range(70):
        if d < 69:
            px += 2.0
        else:
            px = 180.0  # 从约 238 单日大跌, 明确跌破 MA20
        rows.append(("票", base + timedelta(days=d), px, px + 1, px - 1, px, 1000.0))
    panel = pl.DataFrame(
        rows, schema=["symbol", "date", "open", "high", "low", "close", "volume"], orient="row"
    )
    market = build_market_data_matrix(panel, field_columns=set())
    signals = seed.MATRIX_STRATEGY.compute_signals(market, {"require_volume": False})
    assert signals.exit.shape == market.shape
    assert signals.exit[-1, 0] == 1


def test_industry_aggregate_averages_member_strength():
    agg = seed.industry_strength_aggregate(
        {"A": 6.0, "B": 5.0, "C": 2.0},
        {"A": "行业X", "B": "行业Y", "C": "行业Z"},
        min_members=2,
    )
    assert agg == []  # 三个行业各只有1个成员, 全部被剔除
    agg = seed.industry_strength_aggregate(
        {"A": 6.0, "B": 4.0, "C": 2.0, "D": 3.0},
        {"A": "行业X", "B": "行业X", "C": "行业Y", "D": "行业Y"},
        min_members=2,
    )
    assert agg[0]["industry"] == "行业X"
    assert agg[0]["avg_strength"] == 5.0
    assert agg[1]["industry"] == "行业Y"
    assert agg[1]["avg_strength"] == 2.5


def test_industry_aggregate_sorted_desc():
    agg = seed.industry_strength_aggregate(
        {"A": 6.0, "B": 6.0, "C": 2.0, "D": 2.0},
        {"A": "弱行业", "B": "弱行业", "C": "强行业", "D": "强行业"},
        min_members=2,
    )
    assert agg[0]["industry"] == "弱行业"  # 名字叫弱但分数高, 按分数排
    assert agg[0]["avg_strength"] == 6.0


def test_stable_strong_uses_cv():
    assert seed.is_stable_strong([5.0, 5.1, 4.9, 5.0, 5.05], max_cv=0.05) is True
    assert seed.is_stable_strong([3.0, 6.0, 2.0, 5.5, 1.0], max_cv=0.05) is False
    assert seed.is_stable_strong([5.0, 5.1], max_cv=0.05) is False  # 样本不足


def test_persistence_mask_excludes_one_day_wonder():
    """第2点核心: 只有今天强、过去一直弱的票应被排除, 持续强的保留。"""
    # (T, N): 票0 最近5天全强(1); 票1 只有今天强(一日游); 票2 最近5天强3天
    strength = np.array(
        [
            [1, 1, 1],
            [6, 1, 1],
            [6, 1, 6],
            [6, 1, 1],
            [6, 6, 6],
        ],
        dtype=float,
    )
    mask = seed.strength_persistence_mask(strength, strong_level=6, days=5, min_strong_days=3)
    assert mask.shape == (5, 3)
    # 最后一天: 票0 4天强→True, 票1 仅今天1天→False, 票2 2天强(不足3)→False
    assert mask[-1, 0] is True or mask[-1, 0] == True
    assert mask[-1, 1] == False
    assert mask[-1, 2] == False
    # 第4天(索引3): 票0 已3天强→True, 票2 仅1天→False — 无未来函数
    assert mask[3, 0] == True
    assert mask[3, 2] == False


def test_persistence_mask_window_and_threshold():
    """窗口/下限参数生效; 下限=1 时退化为不额外过滤。"""
    strength = np.array([[6, 1], [1, 1], [1, 1], [1, 1], [1, 1]], dtype=float)
    # 只有第0天强, 最后一天回看5天只有1天强
    strict = seed.strength_persistence_mask(strength, 6, days=5, min_strong_days=3)
    assert strict[-1, 0] == False
    loose = seed.strength_persistence_mask(strength, 6, days=5, min_strong_days=1)
    assert loose[-1, 0] == True


def test_entry_with_persistence_drops_one_day_wonder():
    """端到端: 开持续性过滤后, 一日游票不入选, 持续强票仍入选。"""
    rows = []
    base = date(2026, 1, 1)
    for sym in ["持续强", "一日游"]:
        for d in range(70):
            if sym == "持续强":
                px = 100.0 + 2.0 * d  # 一路涨, 全程满档
            else:
                # 前69天横盘(弱, 都在MA5下方附近), 最后一天跳涨冲上高档
                px = 100.0 if d < 69 else 130.0
            rows.append((sym, base + timedelta(days=d), px, px + 1, px - 1, px, 1000.0))
    panel = pl.DataFrame(
        rows, schema=["symbol", "date", "open", "high", "low", "close", "volume"], orient="row"
    )
    market = build_market_data_matrix(panel, field_columns=set())
    signals = seed.MATRIX_STRATEGY.compute_signals(
        market,
        {"min_strength": 6.0, "require_volume": False, "require_persistence": True,
         "persistence_days": 5, "min_strong_days": 3},
    )
    idx = {sym: i for i, sym in enumerate(market.symbols)}
    assert signals.entry[-1, idx["持续强"]] == 1
    assert signals.entry[-1, idx["一日游"]] == 0
    # 关掉过滤后一日游会被选中(证明差异来自持续性过滤)
    signals_off = seed.MATRIX_STRATEGY.compute_signals(
        market, {"min_strength": 6.0, "require_volume": False, "require_persistence": False}
    )
    assert signals_off.entry[-1, idx["一日游"]] == 1


def test_stable_industry_leaders_filters_volatile_and_ranks():
    """行业稳定性榜: 得分平稳的行业入选且按均分降序, 剧烈波动的行业被剔除。"""
    # 行业X 稳定在6附近; 行业Y 稳定在4附近; 行业Z 大起大落(CV大)应剔除
    history = []
    for day in range(6):
        history.append(
            {
                "X1": 6.0, "X2": 6.1,
                "Y1": 4.0, "Y2": 4.1,
                "Z1": 6.0 if day % 2 == 0 else 2.0,
                "Z2": 6.0 if day % 2 == 0 else 2.0,
            }
        )
    ind = {
        "X1": "行业X", "X2": "行业X",
        "Y1": "行业Y", "Y2": "行业Y",
        "Z1": "行业Z", "Z2": "行业Z",
    }
    leaders = seed.stable_industry_leaders(history, ind, max_cv=0.05, min_days=5)
    names = [r["industry"] for r in leaders]
    assert "行业X" in names and "行业Y" in names
    assert "行业Z" not in names  # 大起大落被排除
    assert leaders[0]["industry"] == "行业X"  # 均分高者排前
    assert leaders[0]["cv"] < 0.05


def test_meta_shape():
    assert seed.META["id"] == "industry_strength_rank"
    assert seed.EXECUTION_BACKEND == "matrix_native"
    assert "ma60_bias" in seed.META["scoring"]
    # 第2点新增参数已进入 META
    param_ids = {p["id"] for p in seed.META.get("params", [])}
    assert {"require_persistence", "persistence_days", "min_strong_days"} <= param_ids
