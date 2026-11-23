"""组合层风险测试 —— 行业集中度 / 相关性 / 单笔风险 / 回撤预算。

用合成数据湖，不依赖真实 data/。重点断言几条"不能含糊"的结论：
  - 等权 4 只同行业：HHI 高、有效行业数 ≈ 1、触发集中警告
  - 相关性：完全同步的两只 ρ≈1；重叠天数不足时**不给数**而是记 limitation
  - 单笔风险按「权重 × 潜在回撤」排序，最集中的那笔要出现在首位并触发警告
  - 回撤预算超额时给出可执行的削减目标，且削减后回到预算内
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from app.services import portfolio_risk as pr

D0 = dt.date(2026, 1, 5)


def _write_industry(tmp_path: Path, mapping: dict[str, str]) -> None:
    base = tmp_path / "ext_data" / "ext_hy_ths"
    base.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": list(mapping),
        "所属同花顺行业": list(mapping.values()),
    }).write_parquet(base / "part.parquet")


def _write_prices(tmp_path: Path, series: dict[str, list[float]], days: int = 40) -> None:
    base = tmp_path / "kline_daily_enriched"
    for i in range(days):
        day = D0 + dt.timedelta(days=i)
        part = base / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        rows = [
            {"symbol": sym, "date": day, "raw_close": values[i]}
            for sym, values in series.items()
            if i < len(values)
        ]
        if rows:
            pl.DataFrame(rows).write_parquet(part / "part.parquet")


# --- 权重归一化 -----------------------------------------------------------


def test_normalize_weights_sums_to_one():
    out = pr.normalize_weights([2.0, 2.0, 6.0])
    assert sum(out) == pytest.approx(1.0)
    assert out[2] == pytest.approx(0.6)


def test_normalize_weights_all_zero_falls_back_to_equal():
    assert pr.normalize_weights([0.0, 0.0, 0.0]) == pytest.approx([1 / 3] * 3)


def test_normalize_weights_negative_clamped():
    out = pr.normalize_weights([-5.0, 5.0])
    assert out == pytest.approx([0.0, 1.0])


# --- 行业集中度 -----------------------------------------------------------


def test_industry_concentration_flags_same_industry():
    positions = [
        pr.PositionInput("600001.SH", 0.25, 0.05, "银行"),
        pr.PositionInput("600002.SH", 0.25, 0.05, "银行"),
        pr.PositionInput("600003.SH", 0.25, 0.05, "银行"),
        pr.PositionInput("600004.SH", 0.25, 0.05, "银行"),
    ]
    out = pr.industry_concentration(positions)
    assert out["top_industry"] == "银行"
    assert out["top_industry_share"] == pytest.approx(1.0)
    assert out["hhi"] == pytest.approx(1.0)
    assert out["effective_industries"] == pytest.approx(1.0)
    assert any("最大行业" in w for w in out["warnings"])
    assert any("有效行业数" in w for w in out["warnings"])


def test_industry_concentration_diversified_has_no_warnings():
    positions = [
        pr.PositionInput("A", 0.25, 0.05, "银行"),
        pr.PositionInput("B", 0.25, 0.05, "半导体"),
        pr.PositionInput("C", 0.25, 0.05, "白酒"),
        pr.PositionInput("D", 0.25, 0.05, "医药"),
    ]
    out = pr.industry_concentration(positions)
    assert out["effective_industries"] == pytest.approx(4.0)
    assert out["warnings"] == []


def test_industry_concentration_counts_unclassified_separately():
    positions = [
        pr.PositionInput("A", 0.5, 0.05, "银行"),
        pr.PositionInput("B", 0.5, 0.05, None),
    ]
    out = pr.industry_concentration(positions)
    assert out["unclassified_weight"] == pytest.approx(0.5)
    assert any("没有行业归属" in w for w in out["warnings"])


# --- 相关性 ---------------------------------------------------------------


def test_correlation_matrix_identical_series_is_one():
    series = [0.01, -0.02, 0.03, 0.01] * 10
    matrix, limits = pr.correlation_matrix({"A": series, "B": list(series)})
    assert matrix["A"]["B"] == pytest.approx(1.0)
    assert limits == []


def test_correlation_matrix_too_few_days_returns_none():
    short = [0.01, -0.02, 0.03]
    matrix, limits = pr.correlation_matrix({"A": short, "B": short})
    assert matrix["A"]["B"] is None
    assert any("重叠交易日" in t for t in limits)


def test_correlation_matrix_zero_variance_returns_none():
    flat = [0.0] * 30
    moving = [0.01 if i % 2 else -0.01 for i in range(30)]
    matrix, limits = pr.correlation_matrix({"A": flat, "B": moving})
    assert matrix["A"]["B"] is None
    assert any("无波动" in t for t in limits)


def test_correlation_summary_reports_effective_bets():
    series = [0.01, -0.02, 0.03, 0.01] * 10
    matrix, _ = pr.correlation_matrix({"A": series, "B": series, "C": series})
    out = pr.correlation_summary(matrix, {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3})
    assert out["average_correlation"] == pytest.approx(1.0)
    # 三只完全相关 → 有效独立注数 ≈ 1
    assert out["effective_bets"] == pytest.approx(1.0, abs=0.05)
    assert any("接近同一笔风险" in w for w in out["warnings"])


def test_correlation_summary_negative_correlation_has_no_effective_bets():
    """高度负相关的两只：组合方差相对量 ≤ 0，1/0 无定义 → 必须报 None 并说明。"""
    a = [0.01 if i % 2 else -0.01 for i in range(40)]
    b = [-0.01 if i % 2 else 0.01 for i in range(40)]
    matrix, _ = pr.correlation_matrix({"A": a, "B": b})
    out = pr.correlation_summary(matrix, {"A": 0.5, "B": 0.5})
    assert out["average_correlation"] is not None
    assert out["average_correlation"] < -0.9
    assert out["effective_bets"] is None
    assert any("无定义" in w for w in out["warnings"])


# --- 单笔风险 -------------------------------------------------------------


def test_per_position_risk_sorts_by_contribution():
    positions = [
        pr.PositionInput("BIG", 0.6, 0.10, "银行"),
        pr.PositionInput("SMALL", 0.4, 0.02, "医药"),
    ]
    out = pr.per_position_risk(positions)
    assert out["positions"][0]["symbol"] == "BIG"
    assert out["positions"][0]["risk_contribution"] == pytest.approx(0.06)
    assert out["total_risk_contribution"] == pytest.approx(0.068)
    assert any("过度集中" in w for w in out["warnings"])


def test_per_position_risk_flags_missing_drawdown():
    positions = [
        pr.PositionInput("A", 0.5, 0.05, "银行"),
        pr.PositionInput("B", 0.5, None, "医药"),
    ]
    out = pr.per_position_risk(positions)
    assert any("没有潜在回撤" in w for w in out["warnings"])
    assert out["positions"][-1]["risk_contribution"] is None


# --- 回撤预算 -------------------------------------------------------------


def test_drawdown_budget_within_limit():
    positions = [pr.PositionInput("A", 0.5, 0.05, "银行"), pr.PositionInput("B", 0.5, 0.05, "医药")]
    out = pr.drawdown_budget(positions, budget_pct=15.0)
    assert out["stressed_drawdown_pct"] == pytest.approx(5.0)
    assert out["over_budget"] is False
    assert out["budget_usage_pct"] == pytest.approx(33.33, abs=0.1)
    assert out["suggested_cuts"] == []


def test_drawdown_budget_over_limit_suggests_cuts():
    positions = [
        pr.PositionInput("BIG", 0.8, 0.30, "银行"),
        pr.PositionInput("SMALL", 0.2, 0.10, "医药"),
    ]
    out = pr.drawdown_budget(positions, budget_pct=15.0)
    assert out["over_budget"] is True
    assert out["stressed_drawdown_pct"] > 15.0
    assert out["suggested_cuts"], out
    # 削减建议按风险贡献从大到小
    assert out["suggested_cuts"][0]["symbol"] == "BIG"

    # 应用削减后必须回到预算内
    reduced = {
        p.symbol: p.weight for p in positions
    }
    for cut in out["suggested_cuts"]:
        reduced[cut["symbol"]] = cut["new_weight"]
    stressed_after = sum(
        reduced[p.symbol] * abs(p.drawdown_pct or 0.0) for p in positions
    )
    assert stressed_after <= 0.15 + 1e-9


def test_drawdown_budget_never_promises_safety():
    """预算是配置约束：即使不超预算，结论里也不会出现"不会亏损"这类承诺。"""
    out = pr.drawdown_budget([pr.PositionInput("A", 1.0, 0.01, "银行")], budget_pct=15.0)
    text = str(out)
    assert "不会亏" not in text


# --- 组合报告 -------------------------------------------------------------


@pytest.fixture
def lake(tmp_path) -> Path:
    _write_industry(tmp_path, {
        "600001.SH": "银行", "600002.SH": "银行",
        "600003.SH": "白酒", "600004.SH": "医药",
    })
    _write_prices(tmp_path, {
        "600001.SH": [10.0 + 0.05 * i for i in range(40)],
        "600002.SH": [10.0 + 0.05 * i for i in range(40)],
        "600003.SH": [20.0 - 0.02 * i for i in range(40)],
        "600004.SH": [8.0 + 0.01 * i for i in range(40)],
    })
    return tmp_path


def test_build_portfolio_report_end_to_end(lake):
    report = pr.build_portfolio_report(
        lake,
        holdings=[
            {"symbol": "600001.SH", "weight": 25, "drawdown_pct": 0.05},
            {"symbol": "600002.SH", "weight": 25, "drawdown_pct": 0.05},
            {"symbol": "600003.SH", "weight": 25, "drawdown_pct": 0.08},
            {"symbol": "600004.SH", "weight": 25, "drawdown_pct": 0.03},
        ],
        budget_pct=15.0,
    )
    # 权重被归一化
    assert sum(p["weight"] for p in report["positions"]) == pytest.approx(1.0)
    # 行业集中度: 银行 50%
    assert report["industry_concentration"]["top_industry"] == "银行"
    assert report["industry_concentration"]["top_industry_share"] == pytest.approx(0.5)
    # 相关性: 600001 与 600002 涨跌同步 → 高度相关
    corr = report["correlation"]
    assert corr["common_days"] > 0
    pair = corr["highest_pair"]
    assert pair is not None
    assert {"600001.SH", "600002.SH"} == {pair["a"], pair["b"]}
    assert pair["correlation"] > 0.99
    # 单笔风险与回撤预算都有结论
    assert report["per_position_risk"]["total_risk_contribution"] > 0
    assert "budget_pct" in report["drawdown_budget"]
    # 这块 fixture 没写概念表, 所以会有「缺少 ext_gn_ths」的 limitation、以及
    # 波动率的口径说明 —— 那都是如实披露, 不是错误。除此之外不该有别的问题。
    allowed = ("ext_gn_ths", "年化")
    assert all(any(k in t for k in allowed) for t in report["limitations"]), report["limitations"]
    # 表缺的时候只该说一次, 不要同一件事报两遍
    assert sum("概念" in t for t in report["limitations"]) == 1


def test_build_portfolio_report_without_drawdown_records_limitation(lake):
    report = pr.build_portfolio_report(
        lake,
        holdings=[
            {"symbol": "600001.SH", "weight": 50},
            {"symbol": "600003.SH", "weight": 50},
        ],
    )
    # 调用方没给潜在回撤时用历史实现最大回撤兜底, 并把口径写进 limitations
    assert any("历史实现最大回撤" in t for t in report["limitations"])
    assert report["drawdown_budget"]["stressed_drawdown_pct"] > 0.0


def test_build_portfolio_report_empty_holdings(lake):
    report = pr.build_portfolio_report(lake, holdings=[])
    assert report["positions"] == []
    assert any("组合为空" in t for t in report["limitations"])


def test_build_portfolio_report_missing_industry_table(tmp_path):
    _write_prices(tmp_path, {"600001.SH": [10.0 + 0.05 * i for i in range(40)]})
    report = pr.build_portfolio_report(
        tmp_path, holdings=[{"symbol": "600001.SH", "weight": 100, "drawdown_pct": 0.05}]
    )
    assert any("行业表" in t for t in report["limitations"])
    assert report["industry_concentration"]["unclassified_weight"] == pytest.approx(1.0)


def test_build_portfolio_report_skips_symbol_less_rows(lake):
    report = pr.build_portfolio_report(
        lake,
        holdings=[
            {"symbol": "600001.SH", "weight": 100, "drawdown_pct": 0.05},
            {"weight": 50},
        ],
    )
    assert any("缺少 symbol" in t for t in report["limitations"])
    assert [p["symbol"] for p in report["positions"]] == ["600001.SH"]


def test_load_industry_map_missing(tmp_path):
    mapping, limits = pr.load_industry_map(tmp_path)
    assert mapping == {}
    assert limits


# --- 主题（概念）重复暴露 -------------------------------------------------

def _write_concepts(tmp_path: Path, mapping: dict[str, str]) -> None:
    base = tmp_path / "ext_data" / "ext_gn_ths"
    base.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": list(mapping),
        "所属概念": list(mapping.values()),
    }).write_parquet(base / "part.parquet")


@pytest.fixture
def concept_lake(tmp_path) -> Path:
    _write_industry(tmp_path, {
        "600001.SH": "银行", "600002.SH": "汽车零部件",
        "600003.SH": "机械设备", "600004.SH": "医药",
    })
    concepts = {
        # 三只票共享「减速器」; 融资融券覆盖全市场, 必须被剔除
        "600001.SH": "减速器;融资融券;人形机器人",
        "600002.SH": "减速器;融资融券",
        "600003.SH": "减速器;机器人概念",
        "600004.SH": "融资融券",
    }
    # 概念表的池子要接近真实规模(5582 只), 否则「3 只票共享减速器」在 4 只票的
    # 池子里就是 75% 覆盖 —— 会被当成通用标签剔除, 测不到分组逻辑。
    for i in range(200):
        concepts[f"9{i:05d}.SZ"] = f"融资融券;小众主题{i}"
    _write_concepts(tmp_path, concepts)
    _write_prices(tmp_path, {
        "600001.SH": [10.0 + 0.05 * i for i in range(40)],
        "600002.SH": [12.0 + 0.03 * i for i in range(40)],
        "600003.SH": [20.0 - 0.02 * i for i in range(40)],
        "600004.SH": [8.0 + 0.01 * i for i in range(40)],
    })
    return tmp_path


def test_load_concept_map_splits_semicolon_tags(concept_lake):
    mapping, coverage, limits = pr.load_concept_map(concept_lake)
    assert limits == []
    assert mapping["600001.SH"] == ["减速器", "融资融券", "人形机器人"]
    assert coverage["减速器"] == 3


def test_load_concept_map_missing_table(tmp_path):
    mapping, coverage, limits = pr.load_concept_map(tmp_path)
    assert mapping == {} and coverage == {}
    assert any("概念" in t for t in limits)


def test_concept_exposure_drops_coverage_too_high_tags(concept_lake):
    mapping, coverage, _ = pr.load_concept_map(concept_lake)
    holdings = [
        pr.PositionInput(symbol=s, weight=0.25)
        for s in ("600001.SH", "600002.SH", "600003.SH", "600004.SH")
    ]
    out = pr.concept_exposure(holdings, mapping, coverage)
    names = [g["concept"] for g in out["groups"]]
    # 融资融券覆盖概念表全部标的 → 剔除, 不当作"共享主题"
    assert "融资融券" not in names
    assert "减速器" in names
    assert any(g["concept"] == "融资融券" for g in out["excluded_generic"])


def test_concept_exposure_only_counts_shared_tags(concept_lake):
    mapping, coverage, _ = pr.load_concept_map(concept_lake)
    holdings = [
        pr.PositionInput(symbol=s, weight=0.25)
        for s in ("600001.SH", "600002.SH", "600003.SH", "600004.SH")
    ]
    out = pr.concept_exposure(holdings, mapping, coverage)
    by_name = {g["concept"]: g for g in out["groups"]}
    # 只有 1 只票持有的概念不是"重复暴露"
    assert "人形机器人" not in by_name
    assert by_name["减速器"]["symbol_count"] == 3
    assert by_name["减速器"]["weight"] == pytest.approx(0.75)


def test_concept_exposure_warns_when_concentrated(concept_lake):
    mapping, coverage, _ = pr.load_concept_map(concept_lake)
    holdings = [
        pr.PositionInput(symbol=s, weight=w)
        for s, w in (("600001.SH", 0.4), ("600002.SH", 0.4), ("600003.SH", 0.2))
    ]
    out = pr.concept_exposure(holdings, mapping, coverage)
    assert out["warnings"]
    assert "同一个风险驱动" in out["warnings"][0]


def test_concept_exposure_universe_falls_back_to_table_size(concept_lake):
    """没传 total_universe 时分母用概念表行数, 而不是「覆盖最多的标签的计数」。"""
    mapping, coverage, _ = pr.load_concept_map(concept_lake)
    assert len(mapping) == 204
    out = pr.concept_exposure(
        [pr.PositionInput(symbol="600001.SH", weight=0.5),
         pr.PositionInput(symbol="600002.SH", weight=0.5)],
        mapping, coverage,
    )
    # 融资融券 204/204 = 100% → 剔除; 减速器 3/204 ≈ 1.5% → 保留
    assert [g["concept"] for g in out["groups"]] == ["减速器"]


def test_build_portfolio_report_includes_concept_block(concept_lake):
    report = pr.build_portfolio_report(
        concept_lake,
        holdings=[{"symbol": s, "weight": 25, "drawdown_pct": 0.05}
                  for s in ("600001.SH", "600002.SH", "600003.SH", "600004.SH")],
    )
    assert report["concept"]["concept_count"] >= 1
    assert any(g["concept"] == "减速器" for g in report["concept"]["groups"])


# --- 波动率与逐标的回撤（不依赖共同交易日） ------------------------------

def test_symbol_drawdown_stats_uses_per_symbol_series(lake):
    stats, limits = pr.symbol_drawdown_stats(
        lake, ["600001.SH", "600003.SH"], lookback_days=40
    )
    # 600001 单调上涨 → 回撤 0; 600003 单调下跌 → 有回撤
    assert stats["600001.SH"] == pytest.approx(0.0)
    assert stats["600003.SH"] > 0.0
    # 口径必须自己说明白: 这是历史实现值, 不是未来损失上限
    assert any("历史实现值" in t for t in limits)


def test_load_price_series_does_not_require_common_dates(tmp_path):
    """多买一只票不该改变其它票的历史序列 —— 买入模拟 before/after 可比的前提。"""
    _write_prices(tmp_path, {"600001.SH": [10.0 + 0.1 * i for i in range(30)]})
    one, _ = pr.load_price_series(tmp_path, ["600001.SH"], lookback_days=30)
    _write_prices(tmp_path, {"600002.SH": [5.0] * 10})  # 只有 10 天
    two, _ = pr.load_price_series(tmp_path, ["600001.SH", "600002.SH"], lookback_days=30)
    assert one["600001.SH"] == two["600001.SH"]


def test_symbol_volatility_survives_missing_overlap(lake):
    vols, limits = pr.symbol_volatility(lake, ["600001.SH", "600003.SH"], lookback_days=40)
    assert set(vols) == {"600001.SH", "600003.SH"}
    assert any("年化" in t for t in limits)


# --- 情景冲击 -------------------------------------------------------------

def test_scenario_impact_without_equity_gives_points_only():
    positions = [pr.PositionInput(symbol="600001.SH", weight=0.3)]
    out = pr.scenario_impact(positions, equity=None)
    row = out["positions"][0]
    assert row["equity_impact_pct_10"] == pytest.approx(-3.0)
    # 没权益就编不出金额, 但键必须存在(前端直接读) —— 值给 None, 显示成「—」
    assert "amount_10" in row and row["amount_10"] is None
    assert out["total_amount_10"] is None
    assert out["total_equity_impact_pct_10"] == pytest.approx(-3.0)


def test_scenario_impact_with_equity_gives_amounts():
    positions = [pr.PositionInput(symbol="600001.SH", weight=0.3)]
    out = pr.scenario_impact(positions, equity=1_000_000)
    row = out["positions"][0]
    assert row["amount_10"] == pytest.approx(-30_000)
    assert row["amount_20"] == pytest.approx(-60_000)
    assert "线性近似" in out["note"]


# --- 账户口径（不归一化） -------------------------------------------------

def test_account_basis_keeps_weights_below_one(lake):
    """权重分母是账户权益: 有现金时合计 < 1 是正常的, 不能被归一化掉。"""
    report = pr.build_portfolio_report(
        lake,
        holdings=[
            {"symbol": "600001.SH", "weight": 0.5, "drawdown_pct": 0.05},
            {"symbol": "600003.SH", "weight": 0.2, "drawdown_pct": 0.08},
        ],
        equity=1_000_000, cash=300_000, normalize=False,
    )
    assert report["invested_ratio"] == pytest.approx(0.7)
    assert report["cash_ratio"] == pytest.approx(0.3)
    assert sum(p["weight"] for p in report["positions"]) == pytest.approx(0.7)


def test_account_basis_flags_weight_over_one(lake):
    report = pr.build_portfolio_report(
        lake,
        holdings=[
            {"symbol": "600001.SH", "weight": 0.7, "drawdown_pct": 0.05},
            {"symbol": "600003.SH", "weight": 0.6, "drawdown_pct": 0.08},
        ],
        equity=1_000_000, normalize=False,
    )
    assert any("超过账户权益" in t for t in report["limitations"])


def test_missing_drawdown_falls_back_to_realized_max_drawdown(lake):
    report = pr.build_portfolio_report(
        lake,
        holdings=[
            {"symbol": "600001.SH", "weight": 0.5},
            {"symbol": "600003.SH", "weight": 0.5},
        ],
        normalize=False,
    )
    risks = {p["symbol"]: p["drawdown_pct"] for p in report["per_position_risk"]["positions"]}
    assert risks["600003.SH"] is not None
    assert any("历史实现最大回撤" in t for t in report["limitations"])


# --- 买入前仓位模拟 -------------------------------------------------------

def test_simulate_buy_before_matches_direct_report(lake):
    existing = [
        {"symbol": "600001.SH", "weight": 0.5, "drawdown_pct": 0.05},
        {"symbol": "600003.SH", "weight": 0.2, "drawdown_pct": 0.08},
    ]
    direct = pr.build_portfolio_report(
        lake, holdings=existing, equity=1_000_000, normalize=False,
    )
    sim = pr.simulate_buy(
        lake, existing=existing, new_symbol="600004.SH",
        new_weight_pct=10.0, equity=1_000_000,
    )
    assert sim["before"]["stressed_drawdown_pct"] == pytest.approx(
        direct["drawdown_budget"]["stressed_drawdown_pct"], abs=1e-4
    )


def test_simulate_buy_reports_cash_shortfall(lake):
    sim = pr.simulate_buy(
        lake,
        existing=[{"symbol": "600001.SH", "weight": 0.95, "drawdown_pct": 0.05}],
        new_symbol="600004.SH", new_weight_pct=10.0, equity=1_000_000,
    )
    assert sim["over_cash"] is True
    assert any("超过可用现金占比" in t for t in sim["limitations"])


def test_simulate_buy_within_cash_is_not_flagged(lake):
    sim = pr.simulate_buy(
        lake,
        existing=[{"symbol": "600001.SH", "weight": 0.3, "drawdown_pct": 0.05}],
        new_symbol="600004.SH", new_weight_pct=10.0, equity=1_000_000,
    )
    assert sim["over_cash"] is False


def test_simulate_buy_worst_case_loss_is_negative_amount(lake):
    sim = pr.simulate_buy(
        lake,
        existing=[{"symbol": "600001.SH", "weight": 0.5, "drawdown_pct": 0.2},
                  {"symbol": "600003.SH", "weight": 0.3, "drawdown_pct": 0.1}],
        new_symbol="600004.SH", new_weight_pct=10.0, equity=1_000_000,
    )
    stressed = sim["after"]["stressed_drawdown_pct"]
    assert sim["worst_case_loss"] == pytest.approx(-1_000_000 * stressed / 100, abs=0.01)


def test_simulate_buy_deltas_cover_key_metrics(lake):
    sim = pr.simulate_buy(
        lake,
        existing=[{"symbol": "600001.SH", "weight": 0.3, "drawdown_pct": 0.05}],
        new_symbol="600002.SH", new_weight_pct=10.0, equity=1_000_000,
    )
    assert set(sim["deltas"]) == {
        "top_industry_share", "effective_industries", "effective_bets",
        "max_position_weight", "stressed_drawdown_pct", "budget_usage_pct",
    }
    for value in sim["deltas"].values():
        assert value is None or isinstance(value, float)


def test_simulate_buy_reports_warnings_key(lake):
    """前端要渲染 warnings, 不能只给 limitations。"""
    sim = pr.simulate_buy(
        lake,
        existing=[{"symbol": "600001.SH", "weight": 0.3, "drawdown_pct": 0.05}],
        new_symbol="600002.SH", new_weight_pct=10.0, equity=1_000_000,
    )
    assert isinstance(sim["warnings"], list)


# ---------------------------------------------------------------------------
# 概念标签归并 (近义标签 vs 通用标签)
# ---------------------------------------------------------------------------

def _cluster_lake(tmp_path: Path) -> Path:
    """造一个能同时测「该并」与「不该并」的概念表。

    复刻真实数据（5582 只票）的两个关键形状：

    - ``人形机器人``(120) 100% 落在 ``机器人概念``(400) 里 → 必须并入；
    - ``减速器``(143) 有 110 只在 ``机器人概念``（0.769，过 0.75 阈值）、
      106 只在 ``新能源汽车``（0.741，**不过阈值**）→ 干净地并进机器人概念。
      这正是真实数据里的值（0.769 / 0.741），不是随便捏的；
    - ``AI视频``(60) 三条父都在 0.84~0.88 之间 → **归属歧义，不并**，
      且必须记进 overlaps 让人看见"这三个概念在底层是同一批票"；
    - ``融资融券`` 覆盖全表 → 既不做子也不做父（第一版漏了这条，导致所有
      标签都被"包含"进融资融券，面板出现「10 只持仓共享融资融券 97%」）。
    """
    pool: dict[str, list[str]] = {}

    # 减速器 143 只
    for i in range(143):
        pool.setdefault(f"R{i:03d}.SZ", []).append("减速器")
    # 机器人概念 400 只 = 减速器前 110 只 + A 组 290 只
    for i in range(110):
        pool[f"R{i:03d}.SZ"].append("机器人概念")
    for i in range(290):
        pool.setdefault(f"A{i:03d}.SZ", []).append("机器人概念")
    # 人形机器人 120 只，全部落在机器人概念池内
    for i in range(120):
        pool[f"A{i:03d}.SZ"].append("人形机器人")
    # 新能源汽车 400 只 = 减速器前 106 只 + B 组 294 只
    for i in range(106):
        pool[f"R{i:03d}.SZ"].append("新能源汽车")
    for i in range(294):
        pool.setdefault(f"B{i:03d}.SZ", []).append("新能源汽车")

    # AI视频 60 只，三条父各自覆盖它的一部分（复刻真实数据的 88/85/84）
    for i in range(60):
        pool.setdefault(f"V{i:03d}.SZ", []).append("AI视频")
    for i in range(53):          # AIGC 覆盖 53/60 = 88.3%
        pool[f"V{i:03d}.SZ"].append("AIGC概念")
    for i in range(51):          # 人工智能 覆盖 51/60 = 85.0%
        pool[f"V{i:03d}.SZ"].append("人工智能")
    for i in range(50):          # AI应用 覆盖 50/60 = 83.3%
        pool[f"V{i:03d}.SZ"].append("AI应用")
    # 三条父各自的其余成员，让它们的集合比 AI视频 大（父必须更大）
    for i in range(120):
        pool.setdefault(f"X{i:03d}.SZ", []).append("AIGC概念")
    for i in range(200):
        pool.setdefault(f"Y{i:03d}.SZ", []).append("人工智能")
    for i in range(160):
        pool.setdefault(f"Z{i:03d}.SZ", []).append("AI应用")
    # 冷门尾巴：只带融资融券，把覆盖率分母撑到接近真实全市场
    for i in range(1000):
        pool.setdefault(f"C{i:04d}.SZ", []).extend(["融资融券", f"冷门主题{i}"])

    concepts = {}
    for symbol, tags in pool.items():
        if "融资融券" not in tags:
            tags.append("融资融券")
        concepts[symbol] = ";".join(dict.fromkeys(tags))

    _write_industry(tmp_path, {
        "R000.SZ": "机械设备", "A000.SZ": "机械设备", "B000.SZ": "汽车零部件",
    })
    _write_concepts(tmp_path, concepts)
    return tmp_path


def test_build_concept_clusters_merges_nested_tag(tmp_path):
    lake = _cluster_lake(tmp_path)
    mapping, coverage, _ = pr.load_concept_map(lake)
    clusters, _ = pr.build_concept_clusters(
        mapping, coverage, ["机器人概念", "人形机器人", "减速器", "新能源汽车"],
    )
    # 人形机器人 100% 在机器人概念内 → 并入
    assert clusters.get("人形机器人") == "机器人概念"
    # 减速器: 0.769 过阈值、0.741 不过 → 干净地并进机器人概念 (与实机一致)
    assert clusters.get("减速器") == "机器人概念"


def test_build_concept_clusters_leaves_ambiguous_tag_alone(tmp_path):
    """三条父都过阈值且只差几个百分点时必须不并, 否则会误报归属。

    真实数据里的样本是「AI视频」: AIGC概念 88.0% / 人工智能 85.3% /
    AI应用 84.0% —— 并到任一条, 另外两条的成员数会凭空消失。
    """
    lake = _cluster_lake(tmp_path)
    mapping, coverage, _ = pr.load_concept_map(lake)
    clusters, overlaps = pr.build_concept_clusters(
        mapping, coverage, ["AI视频", "AIGC概念", "人工智能", "AI应用"],
    )
    assert clusters.get("AI视频") is None
    amb = next(o for o in overlaps if o["tag"] == "AI视频")
    parents = {c["parent"] for c in amb["candidates"]}
    assert {"AIGC概念", "人工智能", "AI应用"} <= parents
    assert "归属歧义" in amb["reason"]


def test_build_concept_clusters_excludes_generic_tags(tmp_path):
    """通用标签既不做子也不做父 — 否则融资融券会把所有主题都"包含"进去。"""
    lake = _cluster_lake(tmp_path)
    mapping, coverage, _ = pr.load_concept_map(lake)
    clusters, _ = pr.build_concept_clusters(mapping, coverage, ["融资融券", "机器人概念"])
    assert "融资融券" not in clusters
    assert all(v != "融资融券" for v in clusters.values())


def test_build_concept_clusters_empty_when_single_tag(tmp_path):
    lake = _cluster_lake(tmp_path)
    mapping, coverage, _ = pr.load_concept_map(lake)
    clusters, overlaps = pr.build_concept_clusters(mapping, coverage, ["机器人概念"])
    assert clusters == {}
    assert overlaps == []


def test_concept_exposure_merges_before_counting():
    """同一只票的多个近义标签归并后只能算一次, 否则 symbol_count 虚高。"""
    positions = [
        pr.PositionInput(symbol="600001.SH", weight=0.4),
        pr.PositionInput(symbol="600002.SH", weight=0.6),
    ]
    mapping = {
        "600001.SH": ["机器人概念", "人形机器人"],
        "600002.SH": ["机器人概念"],
    }
    coverage = {"机器人概念": 50, "人形机器人": 12}
    clusters = {"人形机器人": "机器人概念"}
    block = pr.concept_exposure(
        positions, mapping, coverage, total_universe=1000, clusters=clusters,
    )
    group = next(g for g in block["groups"] if g["concept"] == "机器人概念")
    assert group["symbol_count"] == 2
    assert group["members"] == ["人形机器人"]


def test_concept_exposure_limitation_mentions_merge_basis():
    positions = [pr.PositionInput(symbol="600001.SH", weight=1.0)]
    mapping = {"600001.SH": ["机器人概念", "人形机器人"]}
    coverage = {"机器人概念": 50, "人形机器人": 12}
    block = pr.concept_exposure(
        positions, mapping, coverage, total_universe=1000,
        clusters={"人形机器人": "机器人概念"},
    )
    merged = [t for t in block["limitations"] if "近义概念标签" in t]
    assert merged, block["limitations"]
    assert "标的包含度" in merged[0]
    assert "不是语义理解" in merged[0]


def test_concept_exposure_cluster_members_exclude_own_label():
    """clusters 里每个簇只列被并进来的更细标签, 不能把自己也列成成员。

    界面直接渲染「机器人概念 ← 人形机器人、减速器」; 若把自己也列进去,
    会变成「机器人概念 ← 人形机器人、减速器、机器人概念」这种自指。
    """
    positions = [pr.PositionInput(symbol="600001.SH", weight=1.0)]
    mapping = {"600001.SH": ["机器人概念", "人形机器人"]}
    coverage = {"机器人概念": 50, "人形机器人": 12}
    block = pr.concept_exposure(
        positions, mapping, coverage, total_universe=1000,
        clusters={"人形机器人": "机器人概念"},
    )
    cluster = next(c for c in block["clusters"] if c["label"] == "机器人概念")
    assert cluster["members"] == ["人形机器人"]
    assert "机器人概念" not in cluster["members"]


