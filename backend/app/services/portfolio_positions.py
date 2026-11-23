"""真实持仓 vs 自选观察 —— 组合分析只能建立在真实持仓上。

用户要求（2026-11-09）：
「组合风险也需要真实持仓权重、成本和持仓期限。目前没有这些信息，就不应把自选
清单当作真实组合；建议把『自选股观察』与『实际持仓分析』分开。」

本模块是这两件事的**唯一入口**，靠 `basis` 字段区分，调用方不许自己猜：

- ``basis="actual_positions"``：读 ``data/paper/accounts/<acc>/`` 的
  ``account.json`` + ``positions.json``，给出**真实权重**（市值 / 账户权益）、
  成本、持仓天数、浮动盈亏。
- ``basis="manual_positions"``：用户手工录入的数量/成本/建仓日（不玩模拟盘、
  也不接券商接口的人用这个）。同样有权重、成本、持仓期限，但**未与券商对账**，
  且算不出可用数量。
- ``basis="watchlist_observation"``：只有自选符号，**没有权重、没有成本、
  没有持仓期限**。它不是组合：任何组合级结论（行业集中度、相关性、
  回撤预算）在它上面都不成立，只能作为观察线索。

``source`` 决定用哪个口径：``"paper"`` 只用模拟盘、``"manual"`` 只用手工录入、
``"auto"``（默认）优先手工录入 —— 手工录入的是用户自己的真钱，模拟盘是演习，
两者都在时按真钱算，并把"忽略了模拟盘账户"写进 limitations。

为什么必须分开：自选清单可以随手塞进几十只票，把等权当成组合会算出
"行业很分散、回撤可控"这种**看起来像结论的东西** —— 那是清单的性质，
不是持仓的性质。以前 ``GET /api/risk/portfolio`` 只接 ``symbols`` 且省略权重
时按等权处理，正是这个问题。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from app.services import manual_holdings
from app.strategy import paper

logger = logging.getLogger(__name__)

#: 真实持仓：有权重、成本、持仓期限。
ACTUAL_BASIS = "actual_positions"
#: 手工录入的真实持仓：同样有权重与成本，但未与券商对账、无可用数量。
MANUAL_BASIS = "manual_positions"
#: 自选观察：只有符号，权重与成本都是假设出来的，不能当组合。
WATCHLIST_BASIS = "watchlist_observation"
#: 调用方给了权重，但没与任何账户核对过 —— 可能真实，也可能是随手填的。
CALLER_BASIS = "caller_supplied_weights"

BASIS_LABELS = {
    ACTUAL_BASIS: "实际持仓分析",
    MANUAL_BASIS: "手工录入的真实持仓（未对账）",
    WATCHLIST_BASIS: "自选股观察（不是组合）",
    CALLER_BASIS: "调用方提供的权重（未核对账户）",
}

#: 口径选择：auto 优先手工录入（真钱）> 模拟盘（演习）。
SOURCE_AUTO = "auto"
SOURCE_PAPER = "paper"
SOURCE_MANUAL = "manual"

#: 自选观察口径下必须随结果一起给出的提醒（前端应显著展示）。
WATCHLIST_WARNING = (
    "这是自选清单的观察结果，不是实际持仓组合：权重按你给的数字或等权假设，"
    "没有成本与持仓期限。集中度、相关性与回撤预算只有在真实持仓上才有意义。"
)

#: 调用方自报权重时的提醒。
CALLER_WEIGHT_NOTE = (
    "权重由调用方提供，未与任何模拟盘账户核对；若这不是你的真实持仓，"
    "请不要据此判断组合层面的风险。"
)


def resolve_account(data_dir: Path, account_id: str | None = None) -> tuple[str, str | None]:
    """决定用哪个模拟盘账户。

    指定了 ``account_id`` 就用它；没指定时取**权益最大的账户**（模拟盘通常有
    多个 arena 账户，随便挑一个会让用户看不懂数字从哪来），并返回一句
    ``resolved_note`` 说明这个选择。找不到任何账户时返回默认账户名。
    """
    if account_id:
        return paper.validate_account_id(account_id), None

    best_id, best_total = None, -1.0
    for acc_id in paper.list_account_ids(data_dir):
        try:
            ov = paper.overview(data_dir, account_id=acc_id)
        except Exception as e:                                # pragma: no cover - 坏账户不该拖垮接口
            logger.warning("账户 %s 总览失败，跳过: %s", acc_id, e)
            continue
        if not ov.get("initialized") or not ov.get("holdings"):
            continue
        total = float(ov.get("total") or 0.0)
        if total > best_total:
            best_id, best_total = acc_id, total

    if best_id is None:
        return paper.DEFAULT_ACCOUNT_ID, "没有找到有持仓的模拟盘账户，已回退默认账户"
    return best_id, f"未指定账户，已取权益最大的账户 {best_id}（可用 account= 指定）"


def _holding_days(entry_date: str | None, today: dt.date) -> int | None:
    """持有天数（**自然日**，不是交易日）。缺入场日期就不给数，而不是猜 0。"""
    if not entry_date:
        return None
    try:
        return max((today - dt.date.fromisoformat(entry_date[:10])).days, 0)
    except ValueError:
        return None


def _entry_date(pos: dict) -> str | None:
    """最早一笔建仓日期。``lots`` 是 T+1 可用量记账，最早 date 即入场日。"""
    dates = [str(lot.get("date")) for lot in (pos.get("lots") or []) if lot.get("date")]
    return min(dates) if dates else None


def load_actual_positions(
    data_dir: Path,
    *,
    account_id: str | None = None,
    price_map: dict[str, float] | None = None,
    source: str = SOURCE_AUTO,
) -> dict[str, Any]:
    """真实持仓：权重、成本、持仓期限。

    ``source`` 选口径：``auto``（默认）优先手工录入，没有手工持仓才退模拟盘；
    ``paper`` / ``manual`` 强制指定。

    ``price_map`` 给了就用它估值（盘中实时）；不给时**自动退回最近日线收盘**，
    而不是按成本估 —— 后者会让 ``pnl`` 恒为 0、权重按成本算，用户在
    「持仓总览」「行动卡」里看到的浮盈亏全是 0，看起来像没亏没赚。
    权重分母是**账户权益**（现金 + 持仓市值），不是持仓市值之和 ——
    否则空仓比例大的账户会被算成满仓。
    """
    kind = (source or SOURCE_AUTO).strip().lower()
    if kind == SOURCE_MANUAL:
        return _load_manual_positions(data_dir, price_map=price_map)
    if kind not in (SOURCE_AUTO, SOURCE_PAPER):
        raise ValueError(f"source 只支持 {SOURCE_AUTO}/{SOURCE_PAPER}/{SOURCE_MANUAL}")

    if kind == SOURCE_AUTO and manual_holdings.load_all(data_dir):
        manual = _load_manual_positions(data_dir, price_map=price_map)
        manual["limitations"].append(
            "你录入了手工持仓，因此本次用你的真实持仓计算，未使用模拟盘账户；"
            "若想改看模拟盘，请显式传 source=paper"
        )
        return manual
    return _load_paper_positions(data_dir, account_id=account_id, price_map=price_map)


def _load_manual_positions(
    data_dir: Path,
    *,
    price_map: dict[str, float] | None = None,
) -> dict[str, Any]:
    """手工录入口径：数量/成本/建仓日 由用户提供。

    **算不出可用数量**（没有成交流水，推不出 T+1），所以 ``available_qty``
    一律是 None —— 不拿总数量冒充。权重分母 = 现金 + 持仓市值；现金没录
    就按 0 处理并明确说明"权重合计会等于 1，看起来像满仓"。
    """
    rows = manual_holdings.load_all(data_dir)
    acct = manual_holdings.load_account(data_dir)
    cash = acct.get("cash")
    limits: list[str] = [
        "手工录入的持仓，未与券商对账：成本按你填的数字算，"
        "分红送股、手续费、部分成交都不做还原",
    ]
    # 做 T 把成本摊薄成负数是券商 App 里真实存在的数, 照实录即可; 但要说清口径:
    # 收益率(%)因分母为负不适用, 盈亏金额与权重(按市值)照常算。
    neg_cost = sum(
        1 for r in rows.values()
        if isinstance(r.get("avg_cost"), (int, float))
        and not isinstance(r.get("avg_cost"), bool)
        and r["avg_cost"] < 0
    )
    if neg_cost:
        limits.append(
            f"{neg_cost} 笔持仓成本为负（做 T 摊薄后的正常结果）：盈亏金额与权重照常按现价算，"
            "收益率(%)因成本为负不适用、显示「—」"
        )

    if not rows:
        return {
            "basis": MANUAL_BASIS,
            "basis_label": BASIS_LABELS[MANUAL_BASIS],
            "initialized": False,
            "account_id": None,
            "positions": [],
            "limitations": [*limits, "还没有录入任何手工持仓"],
        }

    by_type: dict[str, list[str]] = {}
    for symbol, row in rows.items():
        by_type.setdefault(str(row.get("asset_type") or "stock"), []).append(symbol)

    if price_map:
        price_source = "live"
    else:
        # 调用方给了空 dict 也按"没有实时价"处理 —— 否则会把它标成 live，
        # 而持仓其实按成本估（口径标签与实际数字不符）。
        price_map = paper.latest_close_map(data_dir, symbols_by_type=by_type)
        price_source = "daily" if price_map else "cost"

    # 「专家增强」就地挂在持仓行上：成本口径对齐、除权分红回放、卖出费用穿透。
    # 三个新模块都是延迟 import + 局部兜底 —— 任一缺失/异常只让这几列显示「未核实」，
    # 绝不让一份本来能用的持仓汇总变成 500，也不改上面任何旧键的口径。
    try:
        from app.services import holding_corporate_actions as hca
        from app.fees import resolve_cost_model
    except Exception:  # noqa: BLE001
        hca = None
        resolve_cost_model = None

    today = dt.date.today()
    positions: list[dict[str, Any]] = []
    market_value = 0.0
    unpriced = 0
    neg_cost_unpriced: list[str] = []
    for symbol, row in rows.items():
        qty = float(row.get("qty") or 0.0)
        avg_cost = float(row.get("avg_cost") or 0.0)
        last = price_map.get(symbol)
        priced = last is not None
        if not priced:
            # 取不到行情时用成本当现价，这是一贯的「按成本估算」降级口径。
            # 但负成本（做 T 摊薄）不能当现价：qty × 负成本 会算出负市值，
            # 把总权益和所有持仓权重一起带偏。这类持仓市值按 0 计并单独标注。
            last = avg_cost if avg_cost > 0 else 0.0
            unpriced += 1
            if avg_cost <= 0:
                neg_cost_unpriced.append(symbol)
        mv = qty * float(last)
        market_value += mv
        entry = str(row.get("entry_date") or "")[:10] or None

        cost_basis = str(row.get("cost_basis") or "broker_display")
        # P0-2：现价走不复权 raw_close，成本若是「前复权」口径必须换算到同一尺度。
        # 手工录入目前拿不到逐日复权因子，前复权口径就如实标 comparable=False，
        # 不拿一个猜出来的因子去改用户填的成本（不静默修正数字）。
        align = (
            hca.align_cost_to_quote(cost=avg_cost, cost_basis=cost_basis, adjustment_factor=None)
            if hca else {"aligned_cost": None, "comparable": True, "limitations": ["成本口径模块不可用"]}
        )
        # 除权分红：没有公司事件台账时回放空列表，模块会留 fail-closed 提示。
        actions = (
            hca.apply_corporate_actions(qty=qty, avg_cost=avg_cost, actions=[])
            if hca else {"qty": qty, "avg_cost": avg_cost, "cash_dividend_total": 0.0, "applied": [], "limitations": ["除权分红模块不可用"]}
        )
        # P2-8：用 fees.py 同一套卖出费率给「预估到手」。这里用的是事前估算
        # sell_cost_pct()（不含单笔最低 5 元佣金），实际账目仍需走 sell_amount。
        sell_cost_pct = (
            float(resolve_cost_model(asset_type=str(row.get("asset_type") or "stock")).sell_cost_pct())
            if resolve_cost_model else None
        )
        gross_pnl = round((float(last) - avg_cost) * qty, 2) if priced else 0.0
        net_pnl_after_fees = (
            round(gross_pnl - mv * sell_cost_pct, 2) if sell_cost_pct is not None else None
        )

        positions.append({
            "symbol": symbol,
            "name": row.get("name") or "",
            "asset_type": row.get("asset_type") or "stock",
            "qty": qty,
            # 手工录入没有流水，推不出 T+1 可用量 —— 如实不给
            "available_qty": None,
            "avg_cost": round(avg_cost, 4),
            "last_price": round(float(last), 4),
            "priced": priced,
            "market_value": round(mv, 2),
            "weight": None,                       # 权益算出来后再填
            "pnl": gross_pnl,
            "pnl_pct": round((float(last) / avg_cost - 1.0) * 100, 2) if avg_cost > 0 else None,
            "entry_date": entry,
            "holding_days": _holding_days(entry, today),
            "lots_count": 0,
            "note": row.get("note") or "",
            # —— 以下为「专家增强」新增键，旧键一字未动 ——
            "cost_basis": cost_basis,
            "cost_basis_comparable": bool(align.get("comparable")),
            "cost_basis_note": align.get("scale_note") or "",
            "cash_dividend_total": actions.get("cash_dividend_total"),
            "corporate_actions_applied": actions.get("applied") or [],
            "sell_cost_pct": round(sell_cost_pct, 6) if sell_cost_pct is not None else None,
            "net_pnl_after_fees": net_pnl_after_fees,
            "basis_limitations": [
                *(align.get("limitations") or []),
                *(actions.get("limitations") or []),
            ],
        })

    if cash is None:
        equity = market_value
        cash = 0.0
        limits.append(
            "没有录入现金余额，权重合计会等于 1（看起来像满仓）—— "
            "若你还有现金或其它资产没录，实际仓位比这里显示的更低"
        )
    else:
        equity = market_value + float(cash)

    for p in positions:
        p["weight"] = round(float(p["market_value"]) / equity, 6) if equity > 0 else None
    positions.sort(key=lambda p: (p["weight"] is None, -(p["weight"] or 0.0)))

    unknown_days = sum(1 for p in positions if p["holding_days"] is None)
    if unknown_days:
        limits.append(f"{unknown_days} 笔持仓没有填建仓日期，持仓期限按缺失处理")
    if neg_cost_unpriced:
        limits.append(
            f"{len(neg_cost_unpriced)} 笔负成本持仓取不到行情（{', '.join(neg_cost_unpriced)}）："
            "市值按 0 计、盈亏暂不可算 —— 负成本不能当现价，否则会算出负市值把权重带偏"
        )
    if price_source == "cost":
        limits.append(
            "取不到任何行情，持仓市值与浮盈亏按成本估算 —— 此时盈亏恒为 0，"
            "不代表真的不亏不赚"
        )
    elif price_source == "daily":
        detail = f"（其中 {unpriced} 笔没有日线，按成本计）" if unpriced else ""
        limits.append(f"估值用各标的最近一根日线收盘价{detail}；盘中实时价需由调用方传入 price_map")
    if equity <= 0:
        limits.append("持仓市值与现金合计为 0，权重无法计算")

    total_pnl = sum(float(p["pnl"] or 0.0) for p in positions)
    return {
        "basis": MANUAL_BASIS,
        "basis_label": BASIS_LABELS[MANUAL_BASIS],
        "initialized": True,
        "account_id": None,
        "account_name": "手工录入",
        "status": "manual",
        "cash": round(float(cash), 2),
        "market_value": round(market_value, 2),
        "total": round(equity, 2),
        "initial_cash": None,
        # 未对账，所以没有"相对初始本金的总盈亏"；给持仓层面的浮盈亏合计
        "total_pnl": round(total_pnl, 2),
        "estimating": price_source == "live",
        "price_source": price_source,
        "position_count": len(positions),
        "weight_basis": "market_value / (cash + market_value)",
        "positions": positions,
        "limitations": limits,
    }


def _load_paper_positions(
    data_dir: Path,
    *,
    account_id: str | None = None,
    price_map: dict[str, float] | None = None,
) -> dict[str, Any]:
    acc_id, note = resolve_account(data_dir, account_id)
    limits: list[str] = []
    if note:
        limits.append(note)

    if price_map:
        price_source = "live"
    else:
        price_map = paper.latest_close_map(data_dir, acc_id)
        price_source = "daily" if price_map else "cost"

    ov = paper.overview(data_dir, price_map or None, account_id=acc_id, price_source=price_source)
    if not ov.get("initialized"):
        return {
            "basis": ACTUAL_BASIS,
            "basis_label": BASIS_LABELS[ACTUAL_BASIS],
            "initialized": False,
            "account_id": acc_id,
            "positions": [],
            "limitations": [*limits, f"账户 {acc_id} 尚未初始化，没有持仓可分析"],
        }

    raw = paper.load_positions(data_dir, acc_id)
    total = float(ov.get("total") or 0.0)
    today = dt.date.today()

    positions: list[dict[str, Any]] = []
    missing_cost = 0
    for h in ov.get("holdings") or []:
        symbol = h["symbol"]
        pos = raw.get(symbol) or {}
        entry = _entry_date(pos)
        days = _holding_days(entry, today)
        mv = float(h.get("market_value") or 0.0)
        avg_cost = float(h.get("avg_cost") or 0.0)
        if avg_cost <= 0:
            missing_cost += 1
        positions.append({
            "symbol": symbol,
            "asset_type": h.get("asset_type") or pos.get("asset_type") or "stock",
            "qty": h.get("qty"),
            "available_qty": h.get("available_qty"),
            "avg_cost": round(avg_cost, 4),
            "last_price": h.get("last_price"),
            "priced": h.get("priced"),
            "market_value": round(mv, 2),
            "weight": round(mv / total, 6) if total > 0 else None,
            "pnl": h.get("pnl"),
            "pnl_pct": h.get("pnl_pct"),
            "entry_date": entry,
            "holding_days": days,
            "lots_count": len(pos.get("lots") or []),
        })

    positions.sort(key=lambda p: (p["weight"] is None, -(p["weight"] or 0.0)))
    if missing_cost:
        limits.append(f"{missing_cost} 笔持仓的成本缺失或为 0，成本相关字段不可用")
    unknown_days = sum(1 for p in positions if p["holding_days"] is None)
    if unknown_days:
        limits.append(f"{unknown_days} 笔持仓没有建仓日期（lots 为空），持仓期限按缺失处理")
    if total <= 0:
        limits.append("账户权益为 0，权重无法计算")
    if price_source == "cost":
        limits.append(
            "取不到任何行情，持仓市值与浮盈亏按成本估算 —— 此时盈亏恒为 0，"
            "不代表真的不亏不赚"
        )
    elif price_source == "daily":
        limits.append("估值用各标的最近一根日线收盘价；盘中实时价需由调用方传入 price_map")

    return {
        "basis": ACTUAL_BASIS,
        "basis_label": BASIS_LABELS[ACTUAL_BASIS],
        "initialized": True,
        "account_id": acc_id,
        "account_name": ov.get("account_name"),
        "status": ov.get("status"),
        "cash": ov.get("cash"),
        "market_value": ov.get("market_value"),
        "total": ov.get("total"),
        "initial_cash": ov.get("initial_cash"),
        "total_pnl": ov.get("total_pnl"),
        "estimating": ov.get("estimating"),
        "price_source": price_source,
        "position_count": len(positions),
        "weight_basis": "market_value / account_equity",
        "positions": positions,
        "limitations": limits,
    }


def observation(
    symbols: list[str],
    *,
    weights: list[float] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """非账户口径的持仓输入：只有符号（或调用方自报权重），明确它不是已验证的组合。

    ``weights`` 省略 → 等权假设，``basis="watchlist_observation"``；
    给了权重 → ``basis="caller_supplied_weights"``（仍与账户无关，只是"不是猜的"
    这一点更强）。两种都不带成本与持仓期限。
    """
    cleaned = [s.strip() for s in symbols if (s or "").strip()]
    limits = list(notes or [])
    limits.append("没有成本与持仓期限：这些字段需要真实账户数据，本口径一律不给")
    if weights:
        limits.append(CALLER_WEIGHT_NOTE)
        basis = CALLER_BASIS
        weight_basis = "调用方提供"
        warning = CALLER_WEIGHT_NOTE
    else:
        limits.append("权重按等权假设，不代表真实仓位")
        basis = WATCHLIST_BASIS
        weight_basis = "等权假设"
        warning = WATCHLIST_WARNING
    return {
        "basis": basis,
        "basis_label": BASIS_LABELS[basis],
        "is_portfolio": False,
        "verified_against_account": False,
        "symbols": cleaned,
        "symbol_count": len(cleaned),
        "weight_basis": weight_basis,
        "warning": warning,
        "limitations": limits,
    }


def holdings_for_risk(actual: dict[str, Any]) -> list[dict[str, Any]]:
    """把真实持仓转成 ``portfolio_risk.build_portfolio_report`` 要的入参。

    权重直接用市值占权益的比例（账户里的现金天然体现为"未投资"，所以权重
    合计 < 1 是正常的，不该被归一化掉）。
    """
    out: list[dict[str, Any]] = []
    for p in actual.get("positions") or []:
        if p.get("weight") is None:
            continue
        out.append({"symbol": p["symbol"], "weight": p["weight"]})
    return out
