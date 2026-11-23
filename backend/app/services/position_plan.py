"""持仓计划与买入计划 —— 把「自选观察」和「真实持仓」分开的第三块。

## 为什么要这个模块

用户 2026-11-13 的要求:
「自选列表不等于投资组合。系统应让用户记录持仓数量、成本、买入时间、
计划持有期和可接受亏损」以及「记录分批价格和资金安排, 避免把提示误认为已持仓」。

现状盘点:
- **真实持仓**: `services/portfolio_positions.load_actual_positions` 已有
  (读 paper 台账, 给权重/成本/持仓天数/浮盈亏) —— 复用, 不重写;
- **自选观察**: 由 watchlist + `observation()` 承担 —— 复用;
- **计划买入 / 计划持有期 / 可接受亏损**: **完全缺失**, 本模块补。

## 三种状态必须互斥且显式

同一只标的在同一时刻只能属于一种:

| 状态 | 含义 | 有会计? |
|---|---|---|
| ``watch`` | 只是观察, 没有钱在里面 | 否 |
| ``planning`` | 记录了分批买入计划, 但**一股都没买** | 否 |
| ``holding`` | 真实持仓 (paper 台账里有) | 是 |

**「计划买入」绝不参与盈亏计算** —— 这正是用户担心的
「把提示误认为已持仓」。

## 存储

计划放在 ``data/user_data/position_plans/plan_<symbol>.json``,
走统一的 `JsonDefinitionStore` 契约 (与 lots/custom_signals 同源)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.definitions import JsonDefinitionStore

logger = logging.getLogger(__name__)

# 三种状态 (互斥)
STATE_WATCH = "watch"
STATE_PLANNING = "planning"
STATE_HOLDING = "holding"
STATE_LABELS = {
    STATE_WATCH: "自选观察",
    STATE_PLANNING: "计划买入",
    STATE_HOLDING: "实际持仓",
}
STATE_NOTES = {
    STATE_WATCH: "只做跟踪提醒, 不计算账户盈亏",
    STATE_PLANNING: "已记录买入计划, 但尚未成交, 不计入任何盈亏",
    STATE_HOLDING: "真实持仓, 参与权重与浮盈亏计算",
}

# 计划持有期的合法范围 (交易日/自然日混合口径下用自然日, 与 paper 的持仓天数一致)
MAX_HOLD_DAYS = 3650
# 可接受亏损上限 100%
MAX_ACCEPTABLE_LOSS_PCT = 100.0


def _now_iso() -> str:
    return date.today().isoformat()


# ── 归一化与校验 ────────────────────────────────────────

def normalize_plan(raw: dict) -> dict:
    """补全默认值 (不校验), 供 JsonDefinitionStore 读取时调用。"""
    d = dict(raw)
    d["symbol"] = str(d.get("symbol") or "").strip().upper()
    d.setdefault("note", "")
    d.setdefault("planned_hold_days", None)
    d.setdefault("acceptable_loss_pct", None)
    d.setdefault("batches", [])
    d.setdefault("created_at", _now_iso())
    d.setdefault("updated_at", d["created_at"])
    # 分批计划的每一项: 价格 + 预估金额 + 是否已执行
    batches = []
    for b in d.get("batches") or []:
        if not isinstance(b, dict):
            continue
        try:
            price = float(b.get("price") or 0)
        except (TypeError, ValueError):
            continue
        try:
            amount = float(b.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        batches.append({
            "price": round(price, 4),
            "amount": round(amount, 2),
            "label": str(b.get("label") or ""),
            "done": bool(b.get("done")),
        })
    d["batches"] = batches
    return d


def validate_plan(plan: dict) -> None:
    """校验并抛 ValueError (中文信息)。fail-closed —— 不静默修正用户的计划。"""
    symbol = str(plan.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol 不能为空")

    hold = plan.get("planned_hold_days")
    if hold is not None:
        if isinstance(hold, bool) or not isinstance(hold, int):
            raise ValueError("计划持有期必须是整数天")
        if not (1 <= hold <= MAX_HOLD_DAYS):
            raise ValueError(f"计划持有期必须在 1~{MAX_HOLD_DAYS} 天之间")

    loss = plan.get("acceptable_loss_pct")
    if loss is not None:
        if isinstance(loss, bool) or not isinstance(loss, (int, float)):
            raise ValueError("可接受亏损必须是数字(百分比)")
        if not (0 < float(loss) <= MAX_ACCEPTABLE_LOSS_PCT):
            raise ValueError(f"可接受亏损必须在 0~{MAX_ACCEPTABLE_LOSS_PCT}% 之间")

    batches = plan.get("batches") or []
    if batches:
        if not isinstance(batches, list):
            raise ValueError("batches 必须是数组")
        for i, b in enumerate(batches, 1):
            if not isinstance(b, dict):
                raise ValueError(f"第 {i} 批计划格式非法")
            price = b.get("price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
                raise ValueError(f"第 {i} 批的计划价格必须是正数")
            amount = b.get("amount")
            if (
                amount is not None
                and not isinstance(amount, bool)
                and isinstance(amount, (int, float))
                and amount < 0
            ):
                raise ValueError(f"第 {i} 批的金额不能为负")


# ── 存储 ────────────────────────────────────────────────

_PLAN_STORE = JsonDefinitionStore(
    "user_data/position_plans",
    glob_pattern="plan_*.json",
    label="position_plan",
    normalize=normalize_plan,
)


def load_all(data_dir: Path) -> dict[str, dict]:
    """全部计划, 以 symbol 为键。

    存储文件名是 ``plan_<symbol>.json``, 而 store 以 ``id`` 决定文件名 ——
    因此落盘时 id 写成 ``plan_<symbol>``, 读回后必须能从 id 还原 symbol
    (老文件若只写了 symbol 字段也兼容)。
    """
    out: dict[str, dict] = {}
    for plan in _PLAN_STORE.load_all(data_dir):
        symbol = str(plan.get("symbol") or "").strip().upper()
        if not symbol:
            raw_id = str(plan.get("id") or "")
            if raw_id.startswith("plan_"):
                symbol = raw_id[len("plan_"):].upper()
        if symbol:
            out[symbol] = {**plan, "symbol": symbol}
    return out


def _plan_file_id(symbol: str) -> str:
    """存储文件名用的 id。store 按 id 落盘, 必须与 glob ``plan_*.json`` 匹配。"""
    return f"plan_{symbol}"


def load_one(data_dir: Path, symbol: str) -> dict | None:
    return load_all(data_dir).get(str(symbol).strip().upper())


def save_one(data_dir: Path, plan: dict) -> dict:
    """保存 (校验 + 补 updated_at)。"""
    symbol = str(plan.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol 不能为空")
    normalized = normalize_plan({**plan, "symbol": symbol})
    validate_plan(normalized)
    existing = load_one(data_dir, symbol)
    normalized["updated_at"] = _now_iso()
    if existing and existing.get("created_at"):
        normalized["created_at"] = existing["created_at"]
    # 对外返回的计划不带 id (id 是存储细节); 落盘才补 plan_<symbol>
    _PLAN_STORE.save_one(data_dir, {**normalized, "id": _plan_file_id(symbol)})
    return normalized


def delete_one(data_dir: Path, symbol: str) -> bool:
    sym = str(symbol).strip().upper()
    # 文件名是 plan_<symbol>.json, 删除必须用同一个 id ——
    # 直接用 symbol 会找不到文件 (store.path 拼的是 <id>.json)。
    return _PLAN_STORE.delete_one(data_dir, _plan_file_id(sym))


# ── 状态判定 ────────────────────────────────────────────

def resolve_state(
    symbol: str,
    *,
    holding_symbols: set[str],
    planned_symbols: set[str],
) -> str:
    """同一标的只能属于一种状态。持仓优先于计划 —— 已成交的计划不再是计划。"""
    s = str(symbol).strip().upper()
    if s in holding_symbols:
        return STATE_HOLDING
    if s in planned_symbols:
        return STATE_PLANNING
    return STATE_WATCH


def classify_all(
    symbols: list[str],
    *,
    holding_symbols: set[str],
    plans: dict[str, dict],
) -> list[dict]:
    """把一批标的分类成三种状态, 并附上状态说明。"""
    planned = {s.upper() for s in plans}
    rows = []
    for sym in symbols:
        s = str(sym).strip().upper()
        state = resolve_state(s, holding_symbols=holding_symbols, planned_symbols=planned)
        rows.append({
            "symbol": s,
            "state": state,
            "state_label": STATE_LABELS[state],
            "state_note": STATE_NOTES[state],
            # 明确告诉调用方: 这个状态参不参与会计
            "counts_toward_pnl": state == STATE_HOLDING,
        })
    return rows


# ── 计划兑现度评估 ──────────────────────────────────────

@dataclass
class PlanProgress:
    """计划 vs 现实的对照 —— 只陈述事实, 不给建议。"""

    symbol: str
    state: str
    planned_batches: int = 0
    done_batches: int = 0
    planned_amount: float = 0.0
    executed_amount: float = 0.0
    remaining_amount: float = 0.0
    avg_planned_price: float | None = None
    batches_below_current: int = 0
    batches_above_current: int = 0
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state,
            "planned_batches": self.planned_batches,
            "done_batches": self.done_batches,
            "planned_amount": round(self.planned_amount, 2),
            "executed_amount": round(self.executed_amount, 2),
            "remaining_amount": round(self.remaining_amount, 2),
            "avg_planned_price": (
                round(self.avg_planned_price, 4) if self.avg_planned_price else None
            ),
            "batches_below_current": self.batches_below_current,
            "batches_above_current": self.batches_above_current,
            "facts": self.facts,
            "warnings": self.warnings,
        }


def assess_plan(plan: dict | None, current_price: float | None) -> PlanProgress | None:
    """把计划与现价对照, 产出「事实」与「需要注意」。

    刻意不产出"该不该买": 那是用户的决定, 系统只负责把差距摆出来。
    """
    if not plan:
        return None

    symbol = str(plan.get("symbol") or "")
    progress = PlanProgress(symbol=symbol, state=STATE_PLANNING)
    batches = plan.get("batches") or []
    progress.planned_batches = len(batches)

    weighted_num = 0.0
    weighted_den = 0.0
    for b in batches:
        price = float(b.get("price") or 0)
        amount = float(b.get("amount") or 0)
        progress.planned_amount += amount
        if b.get("done"):
            progress.done_batches += 1
            progress.executed_amount += amount
        else:
            if current_price and price > 0:
                if price < current_price:
                    progress.batches_below_current += 1
                elif price > current_price:
                    progress.batches_above_current += 1
        if price > 0 and amount > 0:
            weighted_num += price * amount
            weighted_den += amount

    progress.remaining_amount = progress.planned_amount - progress.executed_amount
    if weighted_den > 0:
        progress.avg_planned_price = weighted_num / weighted_den

    # ── 事实陈述 (可核对, 无观点) ──
    if progress.planned_batches:
        progress.facts.append(
            f"计划共 {progress.planned_batches} 批, 已执行 {progress.done_batches} 批, "
            f"剩余待执行 {progress.remaining_amount:,.0f} 元"
        )
    else:
        progress.warnings.append("计划里没有任何分批价格 —— 这只是一条备注, 不是买入计划")

    if progress.avg_planned_price and current_price:
        gap = (current_price / progress.avg_planned_price - 1) * 100
        progress.facts.append(
            f"计划加权均价 {progress.avg_planned_price:.2f}, 现价 {current_price:.2f} "
            f"({'高于' if gap > 0 else '低于'}计划价 {abs(gap):.1f}%)"
        )
        if progress.batches_below_current:
            progress.facts.append(
                f"有 {progress.batches_below_current} 批计划价低于现价 (等回调才成交)"
            )
        if progress.batches_above_current:
            progress.warnings.append(
                f"有 {progress.batches_above_current} 批计划价高于现价 —— 若已错过, 需重新评估计划"
            )

    loss = plan.get("acceptable_loss_pct")
    hold = plan.get("planned_hold_days")
    if loss:
        progress.facts.append(f"可接受亏损 {float(loss):g}%")
    else:
        progress.warnings.append("未设置可接受亏损 —— 无法判断何时该认错")
    if hold:
        progress.facts.append(f"计划持有 {int(hold)} 天")
    else:
        progress.warnings.append("未设置计划持有期 —— 无法判断是短炒还是长持")

    return progress


# ── 计划与实际成交自动对账 ──────────────────────────────

@dataclass
class ReconcileMatch:
    """一批计划 → 一笔实际成交的匹配结果。"""

    batch_price: float
    batch_amount: float
    batch_label: str
    fill_date: str | None = None
    fill_price: float | None = None
    fill_qty: int | None = None
    fill_amount: float | None = None
    price_diff_pct: float | None = None
    status: str = "unmatched"     # matched | unmatched | partial

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_price": round(self.batch_price, 4),
            "batch_amount": round(self.batch_amount, 2),
            "batch_label": self.batch_label,
            "fill_date": self.fill_date,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "fill_amount": self.fill_amount,
            "price_diff_pct": self.price_diff_pct,
            "status": self.status,
        }


@dataclass
class ReconcileReport:
    symbol: str
    matches: list[ReconcileMatch] = field(default_factory=list)
    matched_count: int = 0
    unmatched_batches: int = 0
    orphan_fills: int = 0
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "matches": [m.to_dict() for m in self.matches],
            "matched_count": self.matched_count,
            "unmatched_batches": self.unmatched_batches,
            "orphan_fills": self.orphan_fills,
            "facts": self.facts,
            "warnings": self.warnings,
            "suggested_actions": self.suggested_actions,
            "note": "自动对账只做匹配与提示；是否改变计划由用户决定（不自动改写）",
        }


# 价格容差: 成交价与计划价相差在此比例内视为"按计划成交"
_RECONCILE_PRICE_TOLERANCE = 0.02


def reconcile_with_fills(
    plan: dict | None,
    fills: list[dict],
    *,
    symbol: str = "",
) -> ReconcileReport | None:
    """把计划的每一批与真实成交台账对账。

    **只读** —— 不自动改写计划。用户看到差异后自己决定是否标记 ``done``。
    自动改写会在"分批计划 vs 实际成交顺序不一致"时把计划悄悄改错，
    而计划是用户的意图记录。

    匹配规则（保守，宁可报未匹配也不乱认）：
    1. 只看该 symbol 的 buy 成交（corp_action 与 sell 不参与）；
    2. 按时间顺序，逐个计划批次找**价格差在容差内**的第一笔未被占用的成交；
    3. 已标记 ``done`` 的批次直接记为 matched，不消耗成交。
    """
    if not plan:
        return None

    sym = str(plan.get("symbol") or symbol).upper()
    report = ReconcileReport(symbol=sym)

    buys = [
        f for f in fills
        if str(f.get("symbol", "")).upper() == sym
        and f.get("side") == "buy"
        and f.get("kind", "fill") == "fill"
    ]
    buys.sort(key=lambda f: (str(f.get("date") or ""), str(f.get("ts") or "")))

    used: set[int] = set()
    for idx, batch in enumerate(plan.get("batches") or []):
        price = float(batch.get("price") or 0)
        amount = float(batch.get("amount") or 0)
        label = str(batch.get("label") or f"第 {idx + 1} 批")

        if batch.get("done"):
            report.matches.append(ReconcileMatch(
                batch_price=price, batch_amount=amount, batch_label=label, status="matched",
            ))
            report.matched_count += 1
            report.facts.append(f"{label}：已手动标记完成")
            continue

        best: tuple[int, dict] | None = None
        for j, f in enumerate(buys):
            if j in used:
                continue
            fp = float(f.get("price") or 0)
            if price <= 0 or fp <= 0:
                continue
            if abs(fp / price - 1) <= _RECONCILE_PRICE_TOLERANCE:
                best = (j, f)
                break

        if best is None:
            report.matches.append(ReconcileMatch(
                batch_price=price, batch_amount=amount, batch_label=label, status="unmatched",
            ))
            report.unmatched_batches += 1
            continue

        j, fill = best
        used.add(j)
        fp = float(fill.get("price") or 0)
        qty = int(fill.get("qty") or 0)
        report.matches.append(ReconcileMatch(
            batch_price=price,
            batch_amount=amount,
            batch_label=label,
            fill_date=str(fill.get("date") or "")[:10] or None,
            fill_price=round(fp, 4),
            fill_qty=qty,
            fill_amount=round(qty * fp, 2),
            price_diff_pct=round((fp / price - 1) * 100, 2) if price else None,
            status="matched",
        ))
        report.matched_count += 1
        diff = (fp / price - 1) * 100 if price else 0
        report.facts.append(
            f"{label}：已于 {str(fill.get('date') or '')[:10]} 成交 {qty} 股 @ {fp:.2f}"
            f"（计划价 {price:.2f}，偏差 {diff:+.1f}%）"
        )

    report.orphan_fills = len(buys) - len(used)
    if report.orphan_fills > 0:
        report.warnings.append(
            f"有 {report.orphan_fills} 笔买入成交找不到对应计划批次 —— "
            "可能是计划外下单，或计划价偏离超过容差"
        )
    if report.unmatched_batches > 0:
        report.warnings.append(
            f"有 {report.unmatched_batches} 批计划尚无成交（价格未触及或尚未下单）"
        )

    if report.matched_count and report.unmatched_batches:
        report.suggested_actions.append("已成交的批次可标记完成，避免计划与实际持仓重复计入")
    if report.orphan_fills:
        report.suggested_actions.append("核对计划外成交：补录计划，或确认是临时决定")

    return report


# ── 组合级资金校验 ──────────────────────────────────────

@dataclass
class CapitalCheck:
    """多只票的分批计划合计 vs 可用资金 —— 只校验, 不拦截。"""

    available_cash: float | None = None
    total_planned: float = 0.0
    total_executed: float = 0.0
    total_remaining: float = 0.0
    per_symbol: list[dict[str, Any]] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_cash": (
                round(self.available_cash, 2) if self.available_cash is not None else None
            ),
            "total_planned": round(self.total_planned, 2),
            "total_executed": round(self.total_executed, 2),
            "total_remaining": round(self.total_remaining, 2),
            "over_budget": (
                self.available_cash is not None
                and self.total_remaining > self.available_cash
            ),
            "shortfall": (
                round(self.total_remaining - self.available_cash, 2)
                if self.available_cash is not None and self.total_remaining > self.available_cash
                else 0.0
            ),
            "per_symbol": self.per_symbol,
            "facts": self.facts,
            "warnings": self.warnings,
            "note": "只做资金可行性校验，不会拦截下单，也不会自动调整计划金额",
        }


def check_aggregate_capital(
    plans: dict[str, dict],
    *,
    available_cash: float | None,
) -> CapitalCheck:
    """把所有计划的分批金额加起来, 与可用资金对照。

    **关键**: 加总的是**尚未执行**的金额（``remaining``），不是全部计划金额 ——
    已成交的部分已经花掉现金，再加一遍会重复计算。

    ``available_cash`` 为 None 时只报合计，不判断够不够（没有账户就无从比较）。
    """
    check = CapitalCheck(available_cash=available_cash)
    for symbol, plan in sorted(plans.items()):
        batches = plan.get("batches") or []
        planned = sum(float(b.get("amount") or 0) for b in batches)
        executed = sum(float(b.get("amount") or 0) for b in batches if b.get("done"))
        remaining = planned - executed
        check.total_planned += planned
        check.total_executed += executed
        check.total_remaining += remaining
        if planned > 0:
            check.per_symbol.append({
                "symbol": symbol,
                "planned": round(planned, 2),
                "executed": round(executed, 2),
                "remaining": round(remaining, 2),
                "batches": len(batches),
            })

    check.facts.append(
        f"全部计划合计 {check.total_planned:,.0f} 元，已执行 {check.total_executed:,.0f} 元，"
        f"待执行 {check.total_remaining:,.0f} 元"
    )

    if available_cash is None:
        check.warnings.append("没有可用账户现金信息，无法判断这些计划是否买得起")
        return check

    check.facts.append(f"账户可用现金 {available_cash:,.0f} 元")
    if check.total_remaining > available_cash:
        gap = check.total_remaining - available_cash
        check.warnings.append(
            f"待执行计划合计 {check.total_remaining:,.0f} 元超过可用现金 "
            f"{available_cash:,.0f} 元，缺口 {gap:,.0f} 元 —— "
            "全部触发时会有部分批次无法成交"
        )
    elif check.total_planned > available_cash:
        check.warnings.append(
            "全部计划金额超过可用现金，但扣除已执行部分后仍在现金范围内 —— "
            "继续执行剩余批次时需注意现金变化"
        )

    return check


__all__ = [
    "MAX_ACCEPTABLE_LOSS_PCT",
    "MAX_HOLD_DAYS",
    "STATE_HOLDING",
    "STATE_LABELS",
    "STATE_NOTES",
    "STATE_PLANNING",
    "STATE_WATCH",
    "CapitalCheck",
    "PlanProgress",
    "ReconcileMatch",
    "ReconcileReport",
    "assess_plan",
    "check_aggregate_capital",
    "classify_all",
    "delete_one",
    "load_all",
    "load_one",
    "normalize_plan",
    "reconcile_with_fills",
    "resolve_state",
    "save_one",
    "validate_plan",
]
