"""actionability 纯函数契约测试：可执行性标注 + 尾部情景。

覆盖：停牌/T+1/跌停三种顺延、低成交额折半、正常可减、缺 symbol 的诚实标注、
字段缺失不抛异常、高相关放大、空权重/corr=None 不抛异常，以及入参不被改写。
"""
from __future__ import annotations

from app.services.actionability import annotate_executability, tail_scenarios

_SYMBOL = "000001.SZ"


def _cut(symbol: str = _SYMBOL, reduce_by: float = 0.0853) -> dict:
    return {
        "symbol": symbol,
        "current_weight": 0.2,
        "reduce_by": reduce_by,
        "new_weight": 0.2 - reduce_by,
    }


def _full_ok_state() -> dict:
    return {
        "is_suspended": False,
        "t_plus_1_locked": False,
        "is_limit_down": False,
        "turnover": 5e6,
        "free_float": 5e9,
        "last_price": 10.5,
    }


def test_suspended_t1_limit_down_all_defer():
    """停牌 / T+1 / 跌停都顺延，且 note 含对应中文关键词。"""
    cases = [
        ({"is_suspended": True, "t_plus_1_locked": False, "is_limit_down": False}, "停牌"),
        ({"is_suspended": False, "t_plus_1_locked": True, "is_limit_down": False}, "T+1"),
        ({"is_suspended": False, "t_plus_1_locked": False, "is_limit_down": True}, "跌停"),
    ]
    for state, keyword in cases:
        out = annotate_executability(suggested_cuts=[_cut()], market_state={_SYMBOL: state})
        row = out[0]
        assert row["actionability"] == "顺延"
        assert row["executable_today"] is False
        assert row["executable_reduce_by"] is None
        assert keyword in row["actionability_note"]


def test_suspended_takes_priority_over_limit_down():
    """多闸门同时命中时按优先级取停牌。"""
    out = annotate_executability(
        suggested_cuts=[_cut()],
        market_state={_SYMBOL: {"is_suspended": True, "is_limit_down": True}},
    )
    assert "停牌" in out[0]["actionability_note"]
    assert out[0]["actionability"] == "顺延"


def test_low_turnover_half_executable():
    """成交额 < 1e6 → 部分可减，executable_reduce_by == reduce_by * 0.5。"""
    state = _full_ok_state()
    state["turnover"] = 5e5
    out = annotate_executability(suggested_cuts=[_cut()], market_state={_SYMBOL: state})
    row = out[0]
    assert row["actionability"] == "部分可减"
    assert row["executable_today"] is True
    assert row["executable_reduce_by"] == 0.0853 * 0.5
    assert "折半" in row["actionability_note"]


def test_low_free_float_also_half_executable():
    """自由流通规模同样偏低时也按部分可减处理。"""
    state = _full_ok_state()
    state["free_float"] = 8e5
    out = annotate_executability(suggested_cuts=[_cut()], market_state={_SYMBOL: state})
    assert out[0]["actionability"] == "部分可减"
    assert out[0]["executable_reduce_by"] == 0.0853 * 0.5


def test_normal_is_fully_executable_without_mutating_input():
    """正常状态 → 可减、全额可执行；返回浅拷贝且不改写入参。"""
    cut = _cut()
    original = dict(cut)
    out = annotate_executability(
        suggested_cuts=[cut],
        market_state={_SYMBOL: _full_ok_state()},
    )
    row = out[0]
    assert row["actionability"] == "可减"
    assert row["executable_today"] is True
    assert row["executable_reduce_by"] == 0.0853
    assert row is not cut
    assert cut == original
    assert row["new_weight"] == original["new_weight"]


def test_missing_symbol_is_honest_with_limitations():
    """market_state 缺 symbol：不抛异常、note 含「缺少当日市场状态」、limitations 非空。"""
    out = annotate_executability(suggested_cuts=[_cut()], market_state={})
    row = out[0]
    assert row["actionability"] == "可减"
    assert row["executable_reduce_by"] == 0.0853
    assert "缺少当日市场状态" in row["actionability_note"]
    assert row["limitations"]


def test_missing_fields_never_raise():
    """字段缺失 / 值为 None / 空 dict 都不抛异常，并诚实标注未校验。"""
    out = annotate_executability(
        suggested_cuts=[_cut(), {"symbol": "X", "reduce_by": None}],
        market_state={_SYMBOL: {}, "X": None},
    )
    assert len(out) == 2
    assert "缺少当日市场状态" in out[0]["actionability_note"]
    assert out[0]["limitations"]
    assert out[1]["executable_reduce_by"] == 0.0
    assert out[1]["limitations"]


def test_high_correlation_amplifies_tail_loss():
    """corr 高：放大系数 > 1，两条 loss 都为负且 amplified 更负；4 条历史窗口结构完整。"""
    result = tail_scenarios(
        weights={"A": 0.5, "B": 0.25, "C": 0.25},
        avg_correlation=0.9,
    )
    amp = result["correlation_amplified"]
    assert amp["amplification_factor"] > 1
    assert amp["base_loss_10pct"] < 0
    assert amp["amplified_loss_10pct"] < 0
    assert amp["amplified_loss_10pct"] < amp["base_loss_10pct"]
    assert "这是相关性放大近似，不是精确协方差模型" in amp["note"]
    assert any("这是相关性放大近似，不是精确协方差模型" in s for s in result["limitations"])
    stress = result["historical_stress"]
    assert len(stress) == 4
    for row in stress:
        assert row["label"]
        assert row["date_range"]
        assert "equity_drawdown_pct" in row
        assert row["liquidity_note"]


def test_empty_weights_and_none_corr_do_not_raise():
    """空 weights / corr=None：不抛异常、limitations 非空。"""
    result = tail_scenarios(weights={}, avg_correlation=None)
    assert result["limitations"]
    assert result["correlation_amplified"]["amplification_factor"] >= 1
    assert len(result["historical_stress"]) == 4
