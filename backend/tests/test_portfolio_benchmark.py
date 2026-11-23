"""组合「相对基准」层纯函数测试 (零第三方依赖, 仅标准库 + pytest)。

覆盖: 完全相同组合/基准、2 倍杠杆组合、基准零波动、共同日不足 5 天、
空输入、因子归因覆盖度与数值、因子归因空输入; 另加日期不对齐、缺因子收益、
确定性、0 跟踪误差但主动收益非零等 fail-closed 边界。

为什么用「共同交易日」: 组合净值与基准指数日期轴不对齐时, 各自累计再相减
会把「只有一方有行情」的区间收益混进超额, 且 beta/alpha/TE/IR 都依赖同一天
(p_t, b_t) 配对协方差; 实现取交集后计算, 测试用不对齐日期验证裁剪与 as_of。

为什么 specific 无法给: 缺个股实际收益序列 r_s 就无法反解残差
u_s = r_s - sum_f x_{s,f} * F_f, 用 factor_total 凑残差会把缺暴露、权重未归一、
现金手续费塞进特异性项, 属伪精度; 实现恒为 None, 测试断言该诚实标注。
"""

from __future__ import annotations

import pytest

from app.services import portfolio_benchmark as pb

_DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def _series(values: list[float]) -> dict[str, float]:
    return dict(zip(_DAYS, values, strict=True))


def test_identical_portfolio_and_benchmark() -> None:
    """完全复制基准: 超额 0、beta 1、alpha 0、信息比率 0, 且给出共同日数/as_of。"""
    returns = _series([0.01, -0.005, 0.02, 0.003, -0.01])
    result = pb.relative_performance(
        portfolio_returns=dict(returns),
        benchmark_returns=dict(returns),
    )

    assert result["period_return"] == pytest.approx(result["benchmark_return"])
    assert result["excess_return"] == pytest.approx(0.0)
    assert result["beta"] == pytest.approx(1.0)
    assert result["alpha"] == pytest.approx(0.0)
    assert result["tracking_error"] == pytest.approx(0.0)
    # TE==0 且主动收益恒为 0(完美复制): 约定 IR=0, 而非伪造有限数或抛异常。
    assert result["information_ratio"] == pytest.approx(0.0)
    assert result["common_days"] == 5
    assert result["as_of"] == "2026-01-09"
    assert not any("样本不足" in text for text in result["limitations"])


def test_portfolio_is_twice_benchmark() -> None:
    """组合日收益恒为基准 2 倍: beta 2, 全正收益日下累计超额为正。"""
    benchmark = _series([0.010, 0.020, 0.005, 0.015, 0.008])
    portfolio = {day: 2.0 * value for day, value in benchmark.items()}
    result = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )

    assert result["beta"] == pytest.approx(2.0)
    assert result["excess_return"] > 0
    assert result["period_return"] > result["benchmark_return"]
    assert result["common_days"] == 5


def test_zero_variance_benchmark_marks_beta_unavailable() -> None:
    """基准零波动: beta/alpha 为 None, limitations 明确写出「方差为 0」。"""
    benchmark = _series([0.01] * 5)
    portfolio = _series([0.03, -0.01, 0.02, 0.005, -0.004])
    result = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )

    assert result["beta"] is None
    assert result["alpha"] is None
    assert any("方差为 0" in text for text in result["limitations"])
    assert result["sharpe"] is not None
    assert result["tracking_error"] is not None


def test_fewer_than_five_common_days_is_flagged() -> None:
    """共同日 < 5: 仍返回数值, 但 limitations 含「样本不足, 指标仅供参考」。"""
    portfolio = {"2026-01-05": 0.01, "2026-01-06": 0.02}
    benchmark = {"2026-01-05": 0.005, "2026-01-06": -0.01}
    result = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )

    assert result["common_days"] == 2
    assert result["as_of"] == "2026-01-06"
    assert result["period_return"] != 0.0 or result["benchmark_return"] != 0.0
    assert any("样本不足" in text for text in result["limitations"])


def test_empty_input_does_not_raise_and_is_honest() -> None:
    """空输入: 不抛异常, 全 None/0 降级, limitations 非空, as_of 为 None。"""
    result = pb.relative_performance(
        portfolio_returns={},
        benchmark_returns={},
    )

    assert result["period_return"] == 0.0
    assert result["benchmark_return"] == 0.0
    assert result["excess_return"] == 0.0
    assert result["beta"] is None
    assert result["alpha"] is None
    assert result["tracking_error"] is None
    assert result["information_ratio"] is None
    assert result["sharpe"] is None
    assert result["common_days"] == 0
    assert result["as_of"] is None
    assert result["limitations"]
    assert any("无共同交易日" in text for text in result["limitations"])


def test_factor_attribution_with_one_missing_exposure() -> None:
    """2 只有暴露 + 1 只缺: coverage 2/3, 暴露/贡献数值正确, specific 恒 None。"""
    weights = {"600519.SH": 0.5, "000858.SZ": 0.3, "601318.SH": 0.2}
    factor_exposures = {
        "600519.SH": {
            "size": 1.0,
            "value": 0.5,
            "momentum": 0.2,
            "volatility": -0.1,
            "beta": 1.1,
        },
        "000858.SZ": {
            "size": -0.4,
            "value": 1.2,
            "momentum": 0.0,
            "volatility": 0.3,
            "beta": 0.9,
        },
        # 601318.SH 故意缺失
    }
    factor_returns = {
        "size": 0.02,
        "value": -0.01,
        "momentum": 0.03,
        "volatility": 0.005,
        "beta": 0.015,
    }
    result = pb.factor_attribution(
        weights=weights,
        factor_exposures=factor_exposures,
        factor_returns=factor_returns,
    )

    assert result["coverage"] == {"covered": 2, "total": 3}
    assert any("缺少因子暴露" in text for text in result["limitations"])
    assert any("601318.SH" in text for text in result["limitations"])

    expected_exposures = {
        "size": 0.5 * 1.0 + 0.3 * (-0.4),          # 0.38
        "value": 0.5 * 0.5 + 0.3 * 1.2,           # 0.61
        "momentum": 0.5 * 0.2 + 0.3 * 0.0,        # 0.10
        "volatility": 0.5 * (-0.1) + 0.3 * 0.3,   # 0.04
        "beta": 0.5 * 1.1 + 0.3 * 0.9,            # 0.82
    }
    for factor, expected in expected_exposures.items():
        assert result["exposures"][factor] == pytest.approx(expected)
        assert result["contribution"][factor] == pytest.approx(
            expected * factor_returns[factor]
        )

    assert result["factor_total"] == pytest.approx(
        sum(expected * factor_returns[factor] for factor, expected in expected_exposures.items())
    )
    assert result["specific"] is None
    assert any("无个股残差收益序列" in text for text in result["limitations"])


def test_factor_attribution_empty_input() -> None:
    """空输入: 不抛异常, 空 dict / 0 / coverage 0/0, limitations 非空。"""
    result = pb.factor_attribution(
        weights={},
        factor_exposures={},
        factor_returns={},
    )

    assert result["exposures"] == {}
    assert result["contribution"] == {}
    assert result["factor_total"] == 0.0
    assert result["specific"] is None
    assert result["coverage"] == {"covered": 0, "total": 0}
    assert result["limitations"]


# ---- fail-closed 边界增强 -------------------------------------------------

def test_misaligned_dates_use_only_common_days() -> None:
    """日期不对齐: 只取交集, as_of 为共同日最大, 裁剪天数写入 limitations。"""
    portfolio = {
        "2026-01-05": 0.01,
        "2026-01-06": 0.02,
        "2026-01-07": 0.005,
        "2026-01-08": 0.01,   # 基准无此日
        "2026-01-09": -0.01,
    }
    benchmark = {
        "2026-01-04": 0.004,  # 组合无此日
        "2026-01-05": 0.008,
        "2026-01-06": -0.002,
        "2026-01-07": 0.012,
        "2026-01-09": 0.003,
    }
    result = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )

    assert result["common_days"] == 4
    assert result["as_of"] == "2026-01-09"
    assert any("仅使用共同交易日" in text for text in result["limitations"])
    expected_period = 1.01 * 1.02 * 1.005 * 0.99 - 1.0
    expected_benchmark = 1.008 * 0.998 * 1.012 * 1.003 - 1.0
    assert result["period_return"] == pytest.approx(expected_period)
    assert result["benchmark_return"] == pytest.approx(expected_benchmark)


def test_missing_factor_return_contributes_zero_with_limitation() -> None:
    """缺因子收益: 该因子 contribution=0 且写 limitation, 不静默当 0 无说明。"""
    result = pb.factor_attribution(
        weights={"A": 1.0},
        factor_exposures={"A": {"size": 0.7, "value": -0.2}},
        factor_returns={"size": 0.03},
    )

    assert result["exposures"] == {"size": pytest.approx(0.7), "value": pytest.approx(-0.2)}
    assert result["contribution"]["size"] == pytest.approx(0.7 * 0.03)
    assert result["contribution"]["value"] == 0.0
    assert any("缺少因子收益" in text for text in result["limitations"])
    assert result["specific"] is None


def test_zero_te_with_nonzero_active_return_keeps_ir_none() -> None:
    """TE==0 但主动收益恒非零(0/0 无定义): IR 保持 None 并标注, 不伪造成有限数。"""
    benchmark = _series([0.01, 0.02, 0.005, 0.015, 0.008])
    portfolio = {day: value + 0.01 for day, value in benchmark.items()}
    result = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )

    assert result["tracking_error"] == pytest.approx(0.0)
    assert result["information_ratio"] is None
    assert any("跟踪误差为 0 但主动收益非零" in text for text in result["limitations"])


def test_pure_functions_are_deterministic() -> None:
    """同一输入重复调用结果完全一致, 且不修改传入 dict(纯函数)。"""
    portfolio = _series([0.01, -0.005, 0.02, 0.003, -0.01])
    benchmark = _series([0.008, -0.002, 0.015, 0.001, -0.006])
    portfolio_snapshot = dict(portfolio)
    benchmark_snapshot = dict(benchmark)

    first = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )
    second = pb.relative_performance(
        portfolio_returns=portfolio,
        benchmark_returns=benchmark,
    )

    assert first == second
    assert portfolio == portfolio_snapshot
    assert benchmark == benchmark_snapshot
