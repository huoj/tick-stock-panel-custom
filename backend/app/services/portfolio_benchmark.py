"""持仓 / 组合「相对基准」层 —— 纯标准库、纯函数、确定性、fail-closed。

本模块不读数据库、不拉行情、不依赖 numpy / pandas, 只把调用方已经算好的
日收益序列 / 权重 / 因子暴露 / 因子收益(dict) 变成相对基准指标与归因 dict,
便于在真实持仓 / 组合风控侧直接接线而不引入新依赖。所有不可计算项一律给
``None`` 并在 ``limitations`` 中说明, 绝不抛异常、绝不静默补 0 冒充实测值。

为什么用「共同交易日」:
    组合净值与基准指数的日期轴经常不对齐 —— 组合建仓日晚于基准起点、停牌、
    补数口径差异、数据源缺日等都会让两边各多出几天。若各自累计再相减,
    「超额」里会混入「只有一方有行情」的区间收益, 既不是主动收益也不是
    相对收益, 会系统性高估或低估; 而 beta、alpha、跟踪误差、信息比率都建立在
    同一天的 ``(p_t, b_t)`` 配对上, 非配对样本会直接破坏协方差与均值差的统计
    含义。因此一律取两个日期集合的交集并排序, 只在共同交易日上计算, ``as_of``
    取共同日最大值, 并把被剔除的日期数量写进 ``limitations``, 让调用方知道
    数据被裁剪过, 而不是把不对称样本当成完整区间。

为什么 ``specific``(特异性收益) 无法给:
    Barra 式归因把组合收益拆成「因子贡献 ``sum_f X_f * F_f``」与「特异性贡献
    ``sum_s w_s * u_s``」; ``u_s`` 是个股残差收益
    ``r_s - sum_f x_{s,f} * F_f``, 必须有「个股实际收益序列 ``r_s``」才能
    回归或反解。本层只拿到权重 ``w``、截面暴露 ``X`` 和因子收益 ``F``, 既没有
    ``r_s``, 也没有组合实际收益, 无法求残差; 用 ``factor_total`` 去凑一个残差
    会把缺失暴露、权重未归一、现金与手续费全塞进去, 属于伪精度。所以
    ``specific`` 恒为 ``None``, 并在 ``limitations`` 里写明「无个股残差收益序列,
    无法拆出特异性收益」, 不编造 0 或兜底值。

单位口径: 收益均为小数比例(``0.01`` = 1%), 与 ``app/backtest/strategy.py`` 中
    ``daily["avg_ret"]`` 及 ``benchmark_return = closes[-1]/closes[0]-1`` 的
    小数制保持一致; 本层不做百分比 / 百分数换算, 换算由数据边界负责。
"""

from __future__ import annotations

import math
import statistics

__all__ = ["factor_attribution", "relative_performance"]

# 日收益为小数比例, 1e-12 远低于任何有经济意义的波动; 用它把浮点噪声(如
# (b+0.01)-b 各日尾数不同)造成的「近似 0」按 0 处理, 避免把 1e-18 量级的
# 伪波动除以出 1e15 量级的伪信息比率 / 伪 sharpe。
_ZERO_TOL = 1e-12


def _is_zero(value: float) -> bool:
    return abs(value) <= _ZERO_TOL


def _finite_float(value: object) -> float | None:
    """转成有限 float; 非法 / NaN / ±Inf 返回 None, 由调用方决定如何降级。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _empty_relative(limitations: list[str]) -> dict:
    """空输入 / 无可用共同日的诚实降级结果(全 None / 0, 不抛异常)。"""
    return {
        "period_return": 0.0,
        "benchmark_return": 0.0,
        "excess_return": 0.0,
        "beta": None,
        "alpha": None,
        "tracking_error": None,
        "information_ratio": None,
        "sharpe": None,
        "common_days": 0,
        "as_of": None,
        "limitations": limitations,
    }


def relative_performance(
    *,
    portfolio_returns: dict[str, float],   # {date: daily_return(比例, 如 0.01)}
    benchmark_returns: dict[str, float],   # 同上
    risk_free_daily: float = 0.0,
) -> dict:
    """组合相对基准的收益 / 风险指标, 仅在共同交易日上计算。

    ``period_return`` / ``benchmark_return`` 为共同日连乘 ``prod(1+r)-1``;
    ``excess_return`` 为两者之差。``beta = cov(p,b)/var(b)``、``alpha`` 为
    CAPM 口径(样本协方差 / 样本方差, 分母同为 n-1, 比值不受影响);
    ``tracking_error`` 为主动收益 ``p-b`` 的样本标准差; ``information_ratio``
    为主动收益均值 / 跟踪误差; ``sharpe`` 为 ``(mean(p)-rf)/stdev(p)``。
    ``var(b)==0``、样本不足 2 天、标准差为 0 等情形一律 ``None`` + limitation。
    """
    limitations: list[str] = []
    p_map = portfolio_returns or {}
    b_map = benchmark_returns or {}

    rf = _finite_float(risk_free_daily)
    if rf is None:
        rf = 0.0
        limitations.append("无风险利率非有限数值, 按 0 处理")

    common = sorted(set(p_map) & set(b_map))
    if not common:
        limitations.append("无共同交易日, 无法计算相对基准指标")
        if not p_map:
            limitations.append("组合收益序列为空")
        if not b_map:
            limitations.append("基准收益序列为空")
        return _empty_relative(limitations)

    p: list[float] = []
    b: list[float] = []
    used_dates: list[str] = []
    invalid_days = 0
    for day in common:
        pv = _finite_float(p_map[day])
        bv = _finite_float(b_map[day])
        if pv is None or bv is None:
            invalid_days += 1
            continue
        used_dates.append(day)
        p.append(pv)
        b.append(bv)

    if invalid_days:
        limitations.append(f"共同交易日中 {invalid_days} 天收益非有限数值, 已剔除")
    if not used_dates:
        limitations.append("无可用共同交易日数值, 全部指标不可计算")
        return _empty_relative(limitations)

    common_days = len(used_dates)
    dropped_p = len(p_map) - common_days
    dropped_b = len(b_map) - common_days
    if dropped_p or dropped_b:
        limitations.append(
            f"仅使用共同交易日: 组合 {dropped_p} 日、基准 {dropped_b} 日未参与计算"
        )

    period_return = math.prod(1.0 + r for r in p) - 1.0
    benchmark_return = math.prod(1.0 + r for r in b) - 1.0
    excess_return = period_return - benchmark_return

    mean_p = statistics.fmean(p)
    mean_b = statistics.fmean(b)
    active = [pv - bv for pv, bv in zip(p, b, strict=True)]
    excess_mean = statistics.fmean(active)

    beta: float | None = None
    alpha: float | None = None
    if common_days < 2:
        limitations.append("共同交易日不足 2 天, beta/alpha 不可计算")
    else:
        var_b = statistics.variance(b)
        if _is_zero(var_b):
            limitations.append("基准收益序列方差为 0, beta/alpha 不可计算")
        else:
            beta = statistics.covariance(p, b) / var_b
            alpha = mean_p - rf - beta * (mean_b - rf)

    tracking_error: float | None = (
        statistics.stdev(active) if common_days >= 2 else None
    )
    if tracking_error is None:
        limitations.append("共同交易日不足 2 天, 跟踪误差不可计算")

    if tracking_error is None:
        information_ratio = None
    elif _is_zero(tracking_error):
        # 主动收益恒为 0(完美复制基准)时把 IR 约定为 0; 否则「0 波动 + 非零
        # 主动收益」是 0/0 无定义, 保持 None 并标注, 不伪造成有限数。
        # 用容差判定: (b+0.01)-b 的浮点尾数会让 stdev 变成 1e-18 量级的
        # 伪波动, 直接除以它会得到 1e15 量级的伪信息比率。
        if _is_zero(excess_mean):
            information_ratio = 0.0
            limitations.append("跟踪误差为 0 且主动收益恒为 0, 信息比率按 0 处理")
        else:
            information_ratio = None
            limitations.append("跟踪误差为 0 但主动收益非零, 信息比率不可计算")
    else:
        information_ratio = excess_mean / tracking_error

    std_p: float | None = statistics.stdev(p) if common_days >= 2 else None
    if std_p is None or _is_zero(std_p):
        sharpe = None
        limitations.append("组合收益标准差为 0 或不可计算, sharpe 不可计算")
    else:
        sharpe = (mean_p - rf) / std_p

    if common_days < 5:
        limitations.append("样本不足, 指标仅供参考")

    return {
        "period_return": period_return,
        "benchmark_return": benchmark_return,
        "excess_return": excess_return,
        "beta": beta,
        "alpha": alpha,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "sharpe": sharpe,
        "common_days": common_days,
        "as_of": used_dates[-1],
        "limitations": limitations,
    }


def factor_attribution(
    *,
    weights: dict[str, float],               # symbol -> weight
    factor_exposures: dict[str, dict],       # {symbol: {size, value, momentum, volatility, beta}}
    factor_returns: dict[str, float],        # {factor_name: period_return}
) -> dict:
    """Barra 式因子归因: 组合暴露 ``sum(w_s * x_s)``, 贡献 ``暴露 * 因子收益``。

    只统计「权重表里、且 ``factor_exposures`` 有暴露记录」的标的(``coverage``)。
    缺暴露的标的权重不计入因子部分, 但写进 ``coverage`` 与 ``limitations``;
    缺因子收益的因子贡献记 0 并标注。``specific`` 恒为 ``None``, 理由见模块
    docstring。空输入返回全 0 / 空 dict 的诚实降级结果, 不抛异常。
    """
    limitations: list[str] = []
    w_map = weights or {}
    x_map = factor_exposures or {}
    f_map = factor_returns or {}

    symbols = sorted(w_map)
    covered: list[str] = []
    missing: list[str] = []
    for sym in symbols:
        if isinstance(x_map.get(sym), dict):
            covered.append(sym)
        else:
            missing.append(sym)

    if missing:
        limitations.append(
            f"{len(missing)} 个标的缺少因子暴露, 其权重不计入因子部分: {', '.join(missing)}"
        )
    if not w_map:
        limitations.append("权重表为空, 无组合暴露")

    extra_symbols = sorted(set(x_map) - set(w_map))
    if extra_symbols:
        limitations.append(f"因子暴露表中 {len(extra_symbols)} 个标的不在权重表, 已忽略")

    # 因子集合 = 覆盖标的暴露里出现过的键; 排序保证确定性。
    factor_names = sorted({key for sym in covered for key in x_map[sym]})

    w: dict[str, float] = {}
    for sym in covered:
        wv = _finite_float(w_map[sym])
        if wv is None:
            limitations.append(f"标的 {sym} 权重非有限数值, 按 0 处理")
            wv = 0.0
        w[sym] = wv

    def _exposure(sym: str, factor: str) -> float:
        value = _finite_float(x_map[sym].get(factor, 0.0))
        if value is None:
            limitations.append(f"标的 {sym} 的因子 {factor} 暴露非有限数值, 按 0 处理")
            return 0.0
        return value

    fr: dict[str, float] = {}
    for factor in sorted(f_map):
        fv = _finite_float(f_map[factor])
        if fv is None:
            limitations.append(f"因子 {factor} 收益非有限数值, 按 0 处理")
            fv = 0.0
        fr[factor] = fv

    exposures: dict[str, float] = {}
    contribution: dict[str, float] = {}
    for factor in factor_names:
        exposure = math.fsum(w[sym] * _exposure(sym, factor) for sym in covered)
        exposures[factor] = exposure
        if factor in fr:
            contribution[factor] = exposure * fr[factor]
        else:
            contribution[factor] = 0.0
            limitations.append(f"因子 {factor} 缺少因子收益, 贡献按 0 处理")

    unused_returns = sorted(set(fr) - set(factor_names))
    if unused_returns:
        limitations.append(
            f"因子收益 {', '.join(unused_returns)} 在覆盖标的中无对应暴露, 已忽略"
        )

    factor_total = math.fsum(contribution.values())
    limitations.append("无个股残差收益序列, 无法拆出特异性收益")

    return {
        "exposures": exposures,
        "contribution": contribution,
        "factor_total": factor_total,
        "specific": None,
        "coverage": {"covered": len(covered), "total": len(w_map)},
        "limitations": limitations,
    }
