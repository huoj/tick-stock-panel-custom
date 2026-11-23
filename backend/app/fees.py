"""交易成本口径 (单一事实源) — 回测 / 模拟盘 / 因子回测共用。

为什么收在一处
--------------
此前费率默认值散在三处, 同一个策略在三个入口给出三种净收益:

- ``backtest/engine.py``: ``stamp_tax_pct`` 默认 None → 卖出腿印花税按 0 计;
- 前端回测页: 印花税默认 1‰ = 0.1%, 是真实值 (0.05%) 的 2 倍;
- ``strategy/paper.py``: 0.0005 (正确), 但 ``sell_fee`` 不区分品种, ETF 被多扣。

本模块只放 **制度性事实** (税率 / 最低佣金 / 品种差异) 与由它派生的费用函数;
各入口的请求模型负责把用户参数解析成一个 :class:`CostModel`, 不再各自定义默认值。

A 股现行口径 (截至 2026)
------------------------
- 佣金: 券商双边收取, 默认万分之 2.5 (0.00025); **单笔最低 5 元**;
- 印花税: **仅卖出**单边 0.05% (2023-08-28 由 0.1% 减半), **仅股票**;
  ETF / LOF / 债券免征 —— 固定收益与场内基金交易不缴印花税;
- 过户费: 双边 0.001% (十万分之一, 2022-04-29 由 0.002% 下调), 仅股票;
- 滑点: 默认 5 bps。**这是与下单规模无关的近似** —— 真实冲击成本随下单量
  上升, 本模块不建模; 容量约束见回测的 ``max_participation_pct``。

口径边界
--------
- "最低佣金" 是 **逐笔** 的, 只能在已知 (数量, 价格) 时计算。因此
  :meth:`CostModel.buy_cost_pct` / :meth:`CostModel.sell_cost_pct` 只用于
  **事前估算与手数预分配**, 实际账目一律走 :meth:`CostModel.buy_amount` /
  :meth:`CostModel.sell_amount` (含最低佣金)。
- ``asset_type`` 取值与回测一致: "stock" / "etf" / "index"。指数不参与交易,
  仅为统一解析而列入免税集合。
- 印花税 / 过户费的**显式零值**不被默认值覆盖: 传 ``0.0`` 就是零,
  传 ``None`` 才回退到品种制度默认。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 制度性默认值 ────────────────────────────────────────
DEFAULT_COMMISSION_PCT = 0.00025   # 双边佣金 万2.5
MIN_COMMISSION = 5.0               # 佣金单笔最低 5 元
DEFAULT_STAMP_TAX_PCT = 0.0005     # 印花税 0.05%, 仅卖出, 仅股票
DEFAULT_TRANSFER_FEE_PCT = 0.00001  # 过户费 0.001% (十万分之一), 双边, 仅股票
DEFAULT_SLIPPAGE_BPS = 5.0         # 滑点 5 bps (万分之5)

# 免征印花税的品种 (场内基金 / 债券 / 指数不参与交易但统一解析)
STAMP_FREE_ASSET_TYPES = frozenset({"etf", "fund", "lof", "bond", "index"})
# 缴过户费的品种 (仅 A 股股票)
TRANSFER_FEE_ASSET_TYPES = frozenset({"stock"})


def _normalize_asset_type(asset_type: str | None) -> str:
    return str(asset_type or "stock").strip().lower()


def default_stamp_tax_pct(asset_type: str | None) -> float:
    """按品种取印花税制度默认 (仅股票卖出单边 0.05%)。"""
    return 0.0 if _normalize_asset_type(asset_type) in STAMP_FREE_ASSET_TYPES else DEFAULT_STAMP_TAX_PCT


def default_transfer_fee_pct(asset_type: str | None) -> float:
    """按品种取过户费制度默认 (仅股票双边 0.001%)。"""
    return DEFAULT_TRANSFER_FEE_PCT if _normalize_asset_type(asset_type) in TRANSFER_FEE_ASSET_TYPES else 0.0


@dataclass(frozen=True)
class CostModel:
    """一笔交易的成本口径。构造请走 :func:`resolve_cost_model`。"""

    asset_type: str = "stock"
    commission_pct: float = DEFAULT_COMMISSION_PCT
    min_commission: float = MIN_COMMISSION
    stamp_tax_pct: float = DEFAULT_STAMP_TAX_PCT
    transfer_fee_pct: float = DEFAULT_TRANSFER_FEE_PCT
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS

    # ── 逐笔绝对费用 (账目口径, 含最低佣金) ──
    def buy_fee(self, qty: float, price: float) -> float:
        gross = abs(float(qty) * float(price))
        commission = max(gross * self.commission_pct, self.min_commission)
        return round(commission + gross * self.transfer_fee_pct, 2)

    def sell_fee(self, qty: float, price: float) -> float:
        gross = abs(float(qty) * float(price))
        commission = max(gross * self.commission_pct, self.min_commission)
        return round(commission + gross * self.stamp_tax_pct + gross * self.transfer_fee_pct, 2)

    def buy_amount(self, qty: float, price: float) -> float:
        """买入实际现金流出 (成交额 + 费用)。"""
        return abs(float(qty) * float(price)) + self.buy_fee(qty, price)

    def sell_amount(self, qty: float, price: float) -> float:
        """卖出实际现金流入 (成交额 - 费用)。"""
        return abs(float(qty) * float(price)) - self.sell_fee(qty, price)

    # ── 百分比近似 (事前估算 / 手数预分配, 不含最低佣金) ──
    def buy_cost_pct(self) -> float:
        return self.commission_pct + self.transfer_fee_pct + self.slippage_bps / 10000.0

    def sell_cost_pct(self) -> float:
        return (
            self.commission_pct
            + self.transfer_fee_pct
            + self.stamp_tax_pct
            + self.slippage_bps / 10000.0
        )

    def round_trip_cost_pct(self) -> float:
        """一买一卖的合计成本率 (因子分组回测按日扣减用; 不含最低佣金)。"""
        return self.buy_cost_pct() + self.sell_cost_pct()

    def describe(self) -> dict[str, float | str]:
        """回测结果/审计里的可读快照。"""
        return {
            "asset_type": self.asset_type,
            "commission_pct": self.commission_pct,
            "min_commission": self.min_commission,
            "stamp_tax_pct": self.stamp_tax_pct,
            "transfer_fee_pct": self.transfer_fee_pct,
            "slippage_bps": self.slippage_bps,
        }


def resolve_cost_model(
    *,
    asset_type: str = "stock",
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    transfer_fee_pct: float | None = None,
    min_commission: float | None = None,
    slippage_bps: float | None = None,
    fees_pct: float | None = None,
) -> CostModel:
    """把各入口参数解析成统一成本模型。

    解析优先级 (两个口径, 由 ``fees_pct`` 是否为 None 区分):

    1. **现代全项口径** (``fees_pct=None``, 默认): 佣金取 ``commission_pct``
       或 :data:`DEFAULT_COMMISSION_PCT`; 印花税/过户费按 ``asset_type`` 取
       制度默认 (股票卖出 0.05% + 十万分之一, ETF/债券 0)。
    2. **历史一价全包口径** (``fees_pct`` 显式给出): ``fees_pct`` 就是佣金,
       且印花税/过户费默认 **0** —— 这是 `fees_pct` 字段诞生时的语义
       ("测试里传 fees_pct=0 表示零成本"), 保留它是为了不改变既有调用方与
       历史回测的可比性。要在这条路径上收印花税, 必须显式传 ``stamp_tax_pct``。

    - ``commission_pct`` 非 None 时永远优先于 ``fees_pct``, 且走现代口径。
    - ``stamp_tax_pct`` / ``transfer_fee_pct`` 的**品种免税无条件生效**:
      ``asset_type="etf"`` 时即使显式传税率也归零 (法定事实, 非可配置项)。
    - ``stamp_tax_pct=0.0`` 显式零值不会被默认值覆盖。
    - ``min_commission=None`` → :data:`MIN_COMMISSION` (5 元);
      ``slippage_bps=None`` → :data:`DEFAULT_SLIPPAGE_BPS`。
    """
    resolved_asset = _normalize_asset_type(asset_type)
    stamp_free = resolved_asset in STAMP_FREE_ASSET_TYPES
    transfer_free = resolved_asset not in TRANSFER_FEE_ASSET_TYPES
    # fees_pct 显式给出且未给 commission_pct = 历史一价全包口径。
    legacy_all_in = commission_pct is None and fees_pct is not None

    if commission_pct is not None:
        commission = float(commission_pct)
    elif fees_pct is not None:
        commission = float(fees_pct)
    else:
        commission = DEFAULT_COMMISSION_PCT

    if stamp_free:
        stamp = 0.0
    elif stamp_tax_pct is None:
        stamp = 0.0 if legacy_all_in else DEFAULT_STAMP_TAX_PCT
    else:
        stamp = max(float(stamp_tax_pct), 0.0)

    if transfer_free:
        transfer = 0.0
    elif transfer_fee_pct is None:
        transfer = 0.0 if legacy_all_in else DEFAULT_TRANSFER_FEE_PCT
    else:
        transfer = max(float(transfer_fee_pct), 0.0)

    return CostModel(
        asset_type=resolved_asset,
        commission_pct=max(commission, 0.0),
        # 历史一价全包口径下也没有"单笔最低 5 元"这个概念 —— fees_pct=0 在
        # 既有测试与历史回测里就是字面意义的零成本, 不能被最低佣金悄悄改掉。
        min_commission=(
            0.0 if legacy_all_in
            else (MIN_COMMISSION if min_commission is None else max(float(min_commission), 0.0))
        ),
        stamp_tax_pct=stamp,
        transfer_fee_pct=transfer,
        slippage_bps=(
            DEFAULT_SLIPPAGE_BPS if slippage_bps is None else max(float(slippage_bps), 0.0)
        ),
    )


__all__ = [
    "DEFAULT_COMMISSION_PCT",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_STAMP_TAX_PCT",
    "DEFAULT_TRANSFER_FEE_PCT",
    "MIN_COMMISSION",
    "STAMP_FREE_ASSET_TYPES",
    "TRANSFER_FEE_ASSET_TYPES",
    "CostModel",
    "default_stamp_tax_pct",
    "default_transfer_fee_pct",
    "resolve_cost_model",
]
