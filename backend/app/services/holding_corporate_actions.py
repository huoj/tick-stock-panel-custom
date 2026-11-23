"""手工持仓的公司行动回放与成本口径对齐（纯函数、标准库、确定性、fail-closed）。

背景
----
手工持仓 ``avg_cost`` 是用户从券商 App 抄来的数，口径未知：可能是券商展示的
不复权价，也可能是前复权价。而现价走不复权 ``raw_close``。两者尺度不一致时
``pnl = (last - avg_cost) * qty`` 会静默全错；除权送股/分红若不回放，数量与
成本同样失真。

本模块只做两件纯计算：

1. :func:`align_cost_to_quote` —— 把成本换算到与现价一致的不复权尺度。缺复权
   因子或口径未知时 fail-closed，绝不返回貌似可比的错误数字。
2. :func:`apply_corporate_actions` —— 按日期升序回放除权/送股/配股/现金分红。

口径与 ``app/strategy/paper.py`` 的 corp_action 台账一致：全程不复权价，除权按
ex_factor 调整数量（× factor）与成本（÷ factor）。

为什么总成本守恒
----------------
除权（送股/拆股/配股）只是把同一笔投入资本按机械比例摊到更多或更少的份额上，
并不增减已投入的本金。因此 ``qty × factor`` 与 ``avg_cost ÷ factor`` 成对出现，
令 ``qty × avg_cost``（总持仓成本）保持不变；这样用不复权现价相减得到的盈亏，
才不会被份额变动凭空放大或缩小。这正是 paper.py 台账的既有做法。

为什么分红不进成本
------------------
现金分红是一笔**独立的现金流入**，既不改变持仓份额，也不改变每股买入成本，
所以只累加进 ``cash_dividend_total``，绝不塞进 ``avg_cost``。若把分红摊进成本，
会和「做 T 摊薄出的负成本」口径混在一起（负成本是已实现盈亏对成本的抵减），
使同一字段承载两种相反语义、重复计算收益。paper.py 亦明确「现金分红金额不在
因子数据中，不处理」；此处把分红作为独立现金流分离，口径清晰、可核对。
"""
from __future__ import annotations

import math

COST_BASIS_CHOICES: tuple[str, ...] = ("broker_display", "forward_adjusted")

_MISSING_FACTOR_LIMITATION = "缺复权因子，成本与现价尺度可能不一致，盈亏不可比"


def _is_finite_number(value: object) -> bool:
    """是否有限数值；bool 不算（避免 True/False 被当成 1/0）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def align_cost_to_quote(
    *,
    cost: float,
    cost_basis: str,
    adjustment_factor: float | None,
) -> dict:
    """把持仓成本换算到与不复权现价 ``raw_close`` 一致的尺度。

    ``broker_display``：券商展示成本本就是不复权口径，原样返回即可比。
    ``forward_adjusted``：前复权成本乘以 ``adjustment_factor`` 换算回不复权
    尺度；缺因子或因子不可用时 fail-closed，返回 ``comparable=False``，由调用方
    决定是否展示盈亏，绝不静默给出错误数字。

    返回 ``{aligned_cost, cost_basis, comparable, scale_note, limitations}``。
    """
    if not _is_finite_number(cost):
        return {
            "aligned_cost": None,
            "cost_basis": cost_basis,
            "comparable": False,
            "scale_note": "cost 非有限数值，无法对齐成本口径",
            "limitations": ["cost 必须为有限数值，成本口径无法确定"],
        }

    if cost_basis == "broker_display":
        return {
            "aligned_cost": float(cost),
            "cost_basis": cost_basis,
            "comparable": True,
            "scale_note": "券商展示成本按不复权口径记录，与现价 raw_close 同尺度，无需换算",
            "limitations": [],
        }

    if cost_basis == "forward_adjusted":
        if adjustment_factor is None:
            return {
                "aligned_cost": None,
                "cost_basis": cost_basis,
                "comparable": False,
                "scale_note": "前复权成本缺少换算因子，未换算，盈亏不可比",
                "limitations": [_MISSING_FACTOR_LIMITATION],
            }
        if not _is_finite_number(adjustment_factor):
            return {
                "aligned_cost": None,
                "cost_basis": cost_basis,
                "comparable": False,
                "scale_note": "复权因子不可用，未换算，盈亏不可比",
                "limitations": [
                    "复权因子必须是有限数值，无法换算；"
                    "成本与现价尺度可能不一致，盈亏不可比"
                ],
            }
        return {
            "aligned_cost": float(cost) * float(adjustment_factor),
            "cost_basis": cost_basis,
            "comparable": True,
            "scale_note": (
                "成本已按复权因子换算到与现价一致的不复权尺度"
                "（aligned_cost = 前复权成本 × adjustment_factor）"
            ),
            "limitations": [],
        }

    # 未知 cost_basis：不抛异常，fail-closed 标注不可比。
    return {
        "aligned_cost": None,
        "cost_basis": cost_basis,
        "comparable": False,
        "scale_note": "未知成本口径，未做换算",
        "limitations": [
            f"未知 cost_basis={cost_basis!r}，无法确定与现价的可比性；"
            f"仅支持 {', '.join(COST_BASIS_CHOICES)}"
        ],
    }


def _date_key(date_value: object) -> tuple | None:
    """返回可比较的升序排序键；非法日期返回 None（调用方据此跳过该条）。

    字符串日期（ISO）按字典序即时间序；数值日期按数值序。用 rank 前缀把两类
    分开，避免 str 与 float 直接比较触发 TypeError。
    """
    if isinstance(date_value, str) and date_value.strip():
        return (0, date_value)
    if _is_finite_number(date_value):
        return (1, float(date_value))
    return None


def _optional_number(action: dict, key: str) -> tuple[bool, bool, float]:
    """返回 ``(present, valid, value)``。

    ``None`` 视为字段未提供（调用方常把可空列整行带进来）；出现但非有限数值
    视为非法，交给调用方按「非法字段跳过整条」处理。
    """
    if key not in action or action[key] is None:
        return (False, False, 0.0)
    raw = action[key]
    if _is_finite_number(raw):
        return (True, True, float(raw))
    return (True, False, 0.0)


def apply_corporate_actions(
    *,
    qty: float,
    avg_cost: float,
    actions: list[dict],
) -> dict:
    """按 ``date`` 升序回放公司行动，返回调整后的数量/成本与独立现金分红合计。

    除权因子 ``ex_factor>0`` 优先：数量 × factor、成本 ÷ factor，总成本守恒；
    未给 ex_factor 但给了 ``bonus``/``allot`` 时按 ``1+bonus+allot`` 摊薄；
    两者都给以 ex_factor 为准并在 ``applied`` 注明，避免双计。
    现金分红 = 每股分红 × 除权前数量，累加进 ``cash_dividend_total``，不进成本。

    非法/缺失字段逐条跳过并写入 ``limitations``，不抛异常（fail-closed）。
    返回 ``{qty, avg_cost, cash_dividend_total, applied, limitations}``；
    ``applied`` 每条为 ``{date, qty_after, avg_cost_after, dividend_cash, note}``。
    """
    if not _is_finite_number(qty) or not _is_finite_number(avg_cost):
        return {
            "qty": qty,
            "avg_cost": avg_cost,
            "cash_dividend_total": 0.0,
            "applied": [],
            "limitations": ["输入 qty/avg_cost 非有限数值，已停止回放"],
        }

    cur_qty = float(qty)
    cur_avg = float(avg_cost)

    if not isinstance(actions, (list, tuple)):
        return {
            "qty": cur_qty,
            "avg_cost": cur_avg,
            "cash_dividend_total": 0.0,
            "applied": [],
            "limitations": ["actions 必须是 list[dict]，已停止回放"],
        }

    limitations: list[str] = []
    if not actions:
        limitations.append(
            "未提供公司行动记录，未回放任何除权/分红；"
            "qty/avg_cost 按输入原样返回，且未核验该标的是否真实无公司行动"
        )

    staged: list[tuple[tuple, dict]] = []
    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            limitations.append(f"第 {idx} 条不是 dict，已跳过")
            continue
        date_value = action.get("date")
        key = _date_key(date_value)
        if key is None:
            limitations.append(f"第 {idx} 条 date 缺失或非法（{date_value!r}），已跳过")
            continue

        ex_present, ex_valid, ex_val = _optional_number(action, "ex_factor")
        dv_present, dv_valid, dv_val = _optional_number(action, "dividend")
        bn_present, bn_valid, bn_val = _optional_number(action, "bonus")
        al_present, al_valid, al_val = _optional_number(action, "allot")

        invalid_fields = []
        if ex_present and (not ex_valid or ex_val <= 0):
            invalid_fields.append("ex_factor")
        if dv_present and (not dv_valid or dv_val < 0):
            invalid_fields.append("dividend")
        if bn_present and not bn_valid:
            invalid_fields.append("bonus")
        if al_present and not al_valid:
            invalid_fields.append("allot")
        if invalid_fields:
            limitations.append(
                f"第 {idx} 条（date={date_value!r}）字段非法："
                f"{', '.join(invalid_fields)}，已跳过整条"
            )
            continue

        bonus_allot_present = bn_present or al_present
        derived = 1.0 + bn_val + al_val
        has_any = ex_present or dv_present or bn_present or al_present
        if not has_any:
            limitations.append(
                f"第 {idx} 条（date={date_value!r}）无任何有效除权/分红字段，已跳过"
            )
            continue
        if not ex_present and bonus_allot_present and (
            not math.isfinite(derived) or derived <= 0
        ):
            limitations.append(
                f"第 {idx} 条（date={date_value!r}）由 bonus/allot 得到的"
                f"份额因子 {derived!r} 非正，已跳过"
            )
            continue

        staged.append((
            key,
            {
                "date": date_value,
                "ex_present": ex_present,
                "ex_val": ex_val,
                "dv_present": dv_present,
                "dv_val": dv_val,
                "bonus_allot_present": bonus_allot_present,
                "derived": derived,
            },
        ))

    staged.sort(key=lambda item: item[0])

    applied: list[dict] = []
    cash_dividend_total = 0.0
    for _key, plan in staged:
        qty_before = cur_qty  # 现金分红按本条除权前的份额计
        dividend_cash = 0.0
        notes: list[str] = []

        if plan["dv_present"]:
            dividend_cash = plan["dv_val"] * qty_before
            cash_dividend_total += dividend_cash
            notes.append(
                f"现金分红 {dividend_cash:.6f}（每股 {plan['dv_val']} × "
                f"除权前份额 {qty_before}），独立现金流不进成本"
            )

        if plan["ex_present"]:
            factor = plan["ex_val"]
            cur_qty = cur_qty * factor
            cur_avg = cur_avg / factor
            notes.append(
                f"按 ex_factor={factor} 调整（数量 × factor、成本 ÷ factor），总成本守恒"
            )
            if plan["bonus_allot_present"]:
                notes.append("同条 bonus/allot 已忽略，以 ex_factor 为准，避免双计")
        elif plan["bonus_allot_present"]:
            factor = plan["derived"]
            cur_qty = cur_qty * factor
            cur_avg = cur_avg / factor
            notes.append(f"按 (1+bonus+allot)={factor} 摊薄数量与成本，总成本守恒")
        else:
            notes.append("仅现金分红，份额与成本不变")

        applied.append({
            "date": plan["date"],
            "qty_after": cur_qty,
            "avg_cost_after": cur_avg,
            "dividend_cash": dividend_cash,
            "note": "；".join(notes),
        })

    if not applied and not limitations:
        limitations.append("无任何公司行动被成功回放，持仓口径未变")

    return {
        "qty": cur_qty,
        "avg_cost": cur_avg,
        "cash_dividend_total": cash_dividend_total,
        "applied": applied,
        "limitations": limitations,
    }
