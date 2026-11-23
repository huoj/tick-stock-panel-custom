"""风险建议的「可执行性标注」与「尾部情景」——纯函数、标准库、确定性。

## 为什么风险建议要拆成「建议减多少」和「今天能减多少」

``portfolio_risk.drawdown_budget`` 的 ``suggested_cuts[i].reduce_by`` 是一个
**静态权重预算解**：它只回答「为了让压力回撤回到预算内，理论上该把这只票的权重
削掉多少」。它默认了一个隐含前提——想减就能按当前价减掉。但 A 股盘中并不满足
这个前提：停牌没有成交、当日买入受 T+1 锁定、跌停封板卖单排在巨额封单后面、
成交额枯竭时大单还会砸出冲击成本。前端只显示「建议减仓 8.53%」会让用户把它
当成一个今天就能下的指令，而实际可能一股都卖不出去。

所以这里**不改写** ``reduce_by``（那是风险目标，保留原值供复盘/次日顺延用），
而是并列给出 ``executable_reduce_by``（今日真正能减的比例）、
``executable_today``、``actionability`` 与中文 ``actionability_note``：
- 「建议减多少」= 风险模型的目标；
- 「今天能减多少」= 叠加交易制度/流动性约束后的可成交量。
两者分开，才能既保留风险结论，又不把不可成交的建议伪装成可执行指令。

## 为什么尾部放大是近似

``portfolio_risk.scenario_impact`` 是线性近似（假设其余持仓不动）。当平均相关性
趋近 1 时，所有持仓同向下跌，线性情景会系统性低估尾部损失。精确刻画需要完整
协方差矩阵、联合收益分布和危机时的相关性跳变——本模块是纯函数、不联网、不取
行情，拿不到这些输入。因此 ``tail_scenarios`` 只给一个**有界、可解释的放大近似**
``1 + max(0, corr) * (n-1) * 0.15``，并在 note 与 limitations 里明确写
「这是相关性放大近似，不是精确协方差模型」。``historical_stress`` 的 4 个窗口
是**固定字面重放系数**（纯常数 + 白话说明），用于把「相关趋 1 / 流动性枯竭」
落成可对照的情景，不是实时行情、也不是回测结果。

## fail-closed 约定

- 只用标准库；不修改入参（每条 cut 返回浅拷贝）；相同输入恒定输出。
- 缺字段 / 字段类型异常 / symbol 不在 ``market_state`` 一律不抛异常。
- 拿不到当日市场状态时，用 ``actionability='可减'`` 保留建议数值，但必须带
  「缺少当日市场状态，未做可执行性校验」和 ``limitations``，**不假装已校验**。
- ``limitations`` 放在每条返回 dict 里（``list[dict]`` 返回类型不变），便于调用方
  直接 ``[x for row in rows for x in row['limitations']]`` 汇总。
"""

from __future__ import annotations

import math

# 成交额/自由流通规模的「偏低」阈值：按绝对金额（元）口径，由上游统一提供。
_LOW_TURNOVER = 1e6
# 三个会直接令卖单不可成交的制度性闸门。
_GATES = ("is_suspended", "t_plus_1_locked", "is_limit_down")

_UNKNOWN_NOTE = "缺少当日市场状态，未做可执行性校验"
_SUSPENDED_NOTE = "今日停牌，无法成交，减仓建议顺延"
_T_PLUS_1_NOTE = "今日买入部分受 T+1 限制，无法卖出"
_LIMIT_DOWN_NOTE = "今日跌停封板，卖单难成交，减仓建议顺延"
_LOW_LIQUIDITY_NOTE = "成交额偏低，按流动性折半执行"
_VALIDATED_NOTE = "已核对停牌/T+1/跌停与流动性状态，今日可正常减仓"

_APPROX_NOTE = "这是相关性放大近似，不是精确协方差模型"

# 固定字面重放系数：不联网、不取行情。equity_drawdown_pct 为账户权益跌幅(负数)。
_HISTORICAL_STRESS = (
    {
        "label": "2015-06 去杠杆",
        "date_range": "2015-06-15 ~ 2015-07-08",
        "equity_drawdown_pct": -32.0,
        "liquidity_note": "千股跌停、连续一字板，卖单几乎无法成交；对应「跌停封板→顺延」。",
    },
    {
        "label": "2018-02 波动率冲击",
        "date_range": "2018-02-01 ~ 2018-02-09",
        "equity_drawdown_pct": -12.0,
        "liquidity_note": "权重股同向下跌，相关性短线趋近 1，分散化失效；线性情景低估组合回撤。",
    },
    {
        "label": "2020-02 疫情缺口",
        "date_range": "2020-02-03 ~ 2020-02-04",
        "equity_drawdown_pct": -8.0,
        "liquidity_note": "节后首日大面积跌停开盘，缺口日卖不出；重放的是「当日不可减」而非持续下跌。",
    },
    {
        "label": "2024-02 微盘流动性冲击",
        "date_range": "2024-02-01 ~ 2024-02-08",
        "equity_drawdown_pct": -20.0,
        "liquidity_note": "微盘股踩踏、买盘缺失，小市值标的折价也难出货；对应「成交额偏低→折半」的极端版。",
    },
)


def _finite_float(value, default: float = 0.0) -> float:
    """安全转 float：None / bool / 非法类型 / nan / inf 一律回落到 default。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _optional_float(value) -> float | None:
    """与 _finite_float 相同，但「无有效数值」返回 None 以区分「缺失」与 0。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _truthy_flag(value) -> bool:
    """把字符串形式的 'true'/'是' 也当作真，避免上游序列化后误判。"""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "t", "是", "真"}
    return bool(value)


def _state_for(states: dict, symbol) -> dict | None:
    """取该 symbol 的状态；缺 symbol / 值非 dict / symbol 不可哈希都视为未知。"""
    try:
        state = states.get(symbol)
    except TypeError:
        return None
    return state if isinstance(state, dict) else None


def _finalize(
    cut: dict,
    executable_today: bool,
    executable_reduce_by: float | None,
    actionability: str,
    note: str,
    limitations: list[str],
) -> dict:
    cut["executable_today"] = bool(executable_today)
    cut["executable_reduce_by"] = executable_reduce_by
    cut["actionability"] = actionability
    cut["actionability_note"] = note
    cut["limitations"] = list(limitations)
    return cut


def annotate_executability(
    *,
    suggested_cuts: list[dict],
    market_state: dict[str, dict],
) -> list[dict]:
    """给每条减仓建议标注「今日实际能减多少」与可执行性，返回浅拷贝列表。

    规则按优先级短路：停牌 → T+1 → 跌停封板 → 低流动性折半 → 可减。
    market_state 缺 symbol / 字段不完整时返回 ``可减`` + 未校验说明 + limitations，
    不抛异常、也不假装已经做过可执行性校验。每条结果都带 ``limitations`` 键。
    """
    cuts = suggested_cuts if isinstance(suggested_cuts, list) else []
    states = market_state if isinstance(market_state, dict) else {}
    out: list[dict] = []
    for raw in cuts:
        cut = dict(raw) if isinstance(raw, dict) else {}
        symbol = cut.get("symbol")
        reduce_by = _finite_float(cut.get("reduce_by"))
        state = _state_for(states, symbol)

        if state is None:
            out.append(_finalize(
                cut, True, reduce_by, "可减", _UNKNOWN_NOTE,
                [f"{symbol!r} 缺少当日市场状态，未做可执行性校验"],
            ))
            continue

        if _truthy_flag(state.get("is_suspended")):
            out.append(_finalize(cut, False, None, "顺延", _SUSPENDED_NOTE, []))
            continue
        if _truthy_flag(state.get("t_plus_1_locked")):
            out.append(_finalize(cut, False, None, "顺延", _T_PLUS_1_NOTE, []))
            continue
        if _truthy_flag(state.get("is_limit_down")):
            out.append(_finalize(cut, False, None, "顺延", _LIMIT_DOWN_NOTE, []))
            continue

        turnover = _optional_float(state.get("turnover"))
        free_float = _optional_float(state.get("free_float"))
        low_liquidity = (
            (turnover is not None and turnover < _LOW_TURNOVER)
            or (free_float is not None and free_float < _LOW_TURNOVER)
        )
        if low_liquidity:
            out.append(_finalize(
                cut, True, reduce_by * 0.5, "部分可减", _LOW_LIQUIDITY_NOTE,
                ["流动性折半为简化近似，未接入真实盘口深度/冲击成本模型"],
            ))
            continue

        lims: list[str] = []
        if turnover is None:
            lims.append(f"{symbol!r} 缺少成交额(turnover)，未做流动性校验")
        # 三个制度闸门都显式给过（且都为假）才算「已校验」；否则诚实标注未校验。
        gates_explicit = all(key in state for key in _GATES)
        if gates_explicit:
            out.append(_finalize(cut, True, reduce_by, "可减", _VALIDATED_NOTE, lims))
        else:
            out.append(_finalize(
                cut, True, reduce_by, "可减", _UNKNOWN_NOTE,
                [*lims, f"{symbol!r} 当日市场状态字段不完整，未做完整可执行性校验"],
            ))
    return out


def tail_scenarios(
    *,
    weights: dict[str, float],
    avg_correlation: float | None,
) -> dict:
    """相关性放大近似 + 4 个固定历史压力窗口的字面重放系数。

    ``base_loss_10pct = -0.10 * sum(weights)``（线性 -10% 情景）；
    ``amplification_factor = 1 + clamp(corr, 0, 1) * max(0, n-1) * 0.15``，
    有界且可解释，但**不是精确协方差模型**。weights 为空 / corr 缺失时仍返回
    完整结构并写入 limitations，不抛异常。
    """
    w = weights if isinstance(weights, dict) else {}
    limitations: list[str] = [
        _APPROX_NOTE,
        "historical_stress 为固定字面重放系数，不联网、不取实时行情，仅作情景对照",
    ]

    total = 0.0
    for value in w.values():
        total += _finite_float(value)
    n = len(w)

    corr = _optional_float(avg_correlation)
    if corr is None:
        limitations.append("avg_correlation 缺失或非法，按 0 处理，未做相关性放大")
    corr_clamped = 0.0 if corr is None else min(1.0, max(0.0, corr))
    if corr is not None and corr != corr_clamped:
        limitations.append(f"avg_correlation 已钳制到 [0, 1]（原值 {corr}）以保证放大系数有界")

    extra = (n - 1) if n >= 1 else 0
    factor = 1.0 + corr_clamped * extra * 0.15
    if n == 0:
        limitations.append("weights 为空，未计算组合层面的相关性放大损失")
    if total <= 0:
        limitations.append("权重合计非正，base/amplified 损失不构成有效尾部结论")

    base = -0.10 * total
    amplified = base * factor
    correlation_amplified = {
        "avg_correlation": None if corr is None else round(corr_clamped, 6),
        "base_loss_10pct": round(base, 6),
        "amplified_loss_10pct": round(amplified, 6),
        "amplification_factor": round(factor, 6),
        "note": (
            f"平均相关性 {corr_clamped:.2f}、{n} 只持仓时，线性 -10% 情景会低估"
            f"「同向下跌」的尾部；放大系数 {round(factor, 6)}。{_APPROX_NOTE}"
        ),
    }
    return {
        "correlation_amplified": correlation_amplified,
        "historical_stress": [dict(row) for row in _HISTORICAL_STRESS],
        "limitations": limitations,
    }
