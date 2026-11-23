"""统一成本模型 (app/fees.py) 与三处入口的口径一致性。

背景 (P0): 同一个策略在三个入口给出三种净收益 —— 回测 API 默认零印花税、
前端默认 0.1% (真实值的 2 倍)、模拟盘 0.05% 但不区分品种 (ETF 也被扣)。
这组测试把"制度事实"锁住, 防止三处再次分叉。
"""
from __future__ import annotations

import pytest

from app import fees as fee_rules

# ── 品种差异: 免税是法定事实, 不可配置 ──────────────────


@pytest.mark.parametrize("asset_type", ["etf", "fund", "lof", "bond"])
def test_stamp_free_assets_never_pay_stamp_tax(asset_type):
    """ETF/LOF/债券免印花税: 即使显式传税率也必须归零。

    这条是 P0 的核心 —— 模拟盘以前 sell_fee 不区分品种, ETF 每次卖出被
    多扣 0.05%。
    """
    model = fee_rules.resolve_cost_model(asset_type=asset_type, stamp_tax_pct=0.0005)
    assert model.stamp_tax_pct == 0.0
    assert model.transfer_fee_pct == 0.0


def test_stock_pays_stamp_tax_and_transfer_fee():
    model = fee_rules.resolve_cost_model(asset_type="stock")
    assert model.stamp_tax_pct == pytest.approx(0.0005)
    assert model.transfer_fee_pct == pytest.approx(0.00001)


def test_stock_explicit_zero_stamp_tax_is_respected():
    """股票可以显式关掉印花税 (例如做 ETF 化回测), 不被默认值覆盖。"""
    model = fee_rules.resolve_cost_model(asset_type="stock", stamp_tax_pct=0.0)
    assert model.stamp_tax_pct == 0.0


# ── 最低佣金只影响绝对金额, 不影响百分比 ────────────────


def test_min_commission_floors_small_trades():
    """单笔 1000 元买入: 佣金 0.25 元 < 最低 5 元 → 实收 5 元 (+ 过户费)。"""
    model = fee_rules.resolve_cost_model(asset_type="stock")
    fee = model.buy_fee(100, 10.0)          # 1000 元
    assert fee == pytest.approx(5.0 + 1000 * 0.00001, abs=1e-6)


def test_min_commission_does_not_apply_to_large_trades():
    model = fee_rules.resolve_cost_model(asset_type="stock")
    fee = model.buy_fee(100_000, 10.0)      # 100 万元
    assert fee == pytest.approx(1_000_000 * 0.00025 + 1_000_000 * 0.00001, abs=0.02)
    assert fee > fee_rules.MIN_COMMISSION


def test_buy_cost_pct_excludes_min_commission():
    """百分比口径只用于事前估算 —— 它不可能知道最低佣金是否触发。"""
    model = fee_rules.resolve_cost_model(asset_type="stock")
    assert model.buy_cost_pct() == pytest.approx(
        model.commission_pct + model.transfer_fee_pct + model.slippage_bps / 10000.0
    )


# ── 买入/卖出金额的方向与盈亏恒等 ───────────────────────


def test_sell_amount_is_gross_minus_fee_and_round_trip_is_a_loss():
    model = fee_rules.resolve_cost_model(asset_type="stock")
    qty, price = 1_000, 10.0
    buy = model.buy_amount(qty, price)
    sell = model.sell_amount(qty, price)
    assert buy > qty * price            # 买入现金流出 > 成交额
    assert sell < qty * price           # 卖出现金流入 < 成交额
    # 平价买卖必然亏钱, 亏损额 = 双边成本。
    assert buy - sell == pytest.approx(model.buy_fee(qty, price) + model.sell_fee(qty, price))


def test_etf_round_trip_cheaper_than_stock():
    """同价位同手数, ETF 往返成本必定低于股票 (免印花税/过户费)。"""
    stock = fee_rules.resolve_cost_model(asset_type="stock")
    etf = fee_rules.resolve_cost_model(asset_type="etf")
    assert stock.round_trip_cost_pct() > etf.round_trip_cost_pct()


# ── 三处入口共用同一套默认值 ────────────────────────────


def test_backtest_engine_uses_fee_module_defaults():
    from app.backtest.engine import MatcherConfig

    config = MatcherConfig()
    model = config.cost_model()
    assert model.commission_pct == pytest.approx(fee_rules.DEFAULT_COMMISSION_PCT)
    assert model.stamp_tax_pct == pytest.approx(fee_rules.DEFAULT_STAMP_TAX_PCT)
    assert model.transfer_fee_pct == pytest.approx(fee_rules.DEFAULT_TRANSFER_FEE_PCT)
    assert model.slippage_bps == pytest.approx(fee_rules.DEFAULT_SLIPPAGE_BPS)
    # 默认不再是"零印花税": 这正是旧版 API 默认值的问题。
    assert config.sell_cost_pct() > config.buy_cost_pct()


def test_paper_account_uses_same_model_and_respects_asset_type():
    from app.strategy import paper

    acc = {
        "commission_pct": paper.DEFAULT_COMMISSION_PCT,
        "stamp_tax_pct": paper.DEFAULT_STAMP_TAX_PCT,
        "slippage_bps": paper.DEFAULT_SLIPPAGE_BPS,
    }
    stock = paper.account_cost_model(acc, "stock")
    etf = paper.account_cost_model(acc, "etf")
    assert stock.stamp_tax_pct == pytest.approx(fee_rules.DEFAULT_STAMP_TAX_PCT)
    assert etf.stamp_tax_pct == 0.0
    # 模拟盘与回测在股票上完全一致。
    from app.backtest.engine import MatcherConfig

    assert stock.sell_cost_pct() == pytest.approx(MatcherConfig().sell_cost_pct())


def test_paper_sell_fee_skips_stamp_tax_for_etf():
    """回归: 以前 sell_fee 不区分品种, ETF 被多扣 0.05%。"""
    from app.strategy import paper

    stock_fee = paper.sell_fee(1_000, 10.0, 0.00025, 0.0005, asset_type="stock")
    etf_fee = paper.sell_fee(1_000, 10.0, 0.00025, 0.0005, asset_type="etf")
    assert stock_fee == pytest.approx(10.1)      # 佣金 5 + 印花 5 + 过户 0.1
    assert etf_fee == pytest.approx(5.0)         # 仅佣金最低 5
    assert stock_fee > etf_fee


def test_api_request_defaults_match_paper_defaults():
    """三个入口的默认费率必须落在同一套制度事实上 (P0 的直接目标)。

    API 的 fees_pct 默认 None 是刻意的: 它让请求走"现代全项口径"
    (佣金万2.5 + 印花税按品种 + 过户费仅股票 + 最低佣金 5 元), 与模拟盘
    account_cost_model 的默认结果逐项相等。旧版把 fees_pct 设为 0.0002,
    等于宣布"这个请求不含印花税" —— 那正是回测收益虚高的来源。
    """
    from app.api.backtest import BacktestRequest, StrategyBacktestRequest
    from app.strategy import paper

    for model in (BacktestRequest, StrategyBacktestRequest):
        fields = model.model_fields
        assert fields["fees_pct"].default is None
        assert fields["slippage_bps"].default == pytest.approx(paper.DEFAULT_SLIPPAGE_BPS)
        # stamp_tax_pct / transfer_fee_pct 默认 None = 按品种取制度默认。
        assert fields["stamp_tax_pct"].default is None
        assert fields["transfer_fee_pct"].default is None

    # 解析结果与模拟盘默认完全一致 (股票)。
    from app import fees as fee_rules

    api_default = fee_rules.resolve_cost_model(asset_type="stock")
    paper_default = paper.account_cost_model(
        {
            "commission_pct": paper.DEFAULT_COMMISSION_PCT,
            "stamp_tax_pct": paper.DEFAULT_STAMP_TAX_PCT,
            "slippage_bps": paper.DEFAULT_SLIPPAGE_BPS,
        },
        "stock",
    )
    assert api_default.describe() == paper_default.describe()


def test_strategy_backtest_request_exposes_capacity_constraint():
    from app.api.backtest import StrategyBacktestRequest

    fields = StrategyBacktestRequest.model_fields
    assert fields["max_participation_pct"].default == pytest.approx(0.10)
