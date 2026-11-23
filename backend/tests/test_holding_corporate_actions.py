"""手工持仓成本口径对齐与公司行动回放 —— 定向单测。

覆盖：align_cost_to_quote 三种成本口径 + 未知值/非法因子不抛异常；
apply_corporate_actions 除权总成本守恒、现金分红独立、ex_factor 优先不双计、
按日期升序、空/脏数据 fail-closed、负成本（做 T 摊薄）可回放。
"""
from __future__ import annotations

import pytest

from app.services.holding_corporate_actions import (
    COST_BASIS_CHOICES,
    align_cost_to_quote,
    apply_corporate_actions,
)

# ---------- align_cost_to_quote ----------

def test_cost_basis_choices() -> None:
    assert COST_BASIS_CHOICES == ("broker_display", "forward_adjusted")
    assert isinstance(COST_BASIS_CHOICES, tuple)


def test_align_broker_display() -> None:
    r = align_cost_to_quote(
        cost=10.0, cost_basis="broker_display", adjustment_factor=None
    )
    assert r["aligned_cost"] == 10.0
    assert r["cost_basis"] == "broker_display"
    assert r["comparable"] is True
    assert r["limitations"] == []
    assert isinstance(r["scale_note"], str)


def test_align_forward_adjusted_with_factor() -> None:
    r = align_cost_to_quote(
        cost=8.0, cost_basis="forward_adjusted", adjustment_factor=1.25
    )
    assert r["aligned_cost"] == pytest.approx(10.0)
    assert r["cost_basis"] == "forward_adjusted"
    assert r["comparable"] is True
    assert "成本已按复权因子换算到与现价一致的不复权尺度" in r["scale_note"]
    assert r["limitations"] == []


def test_align_forward_adjusted_without_factor() -> None:
    r = align_cost_to_quote(
        cost=8.0, cost_basis="forward_adjusted", adjustment_factor=None
    )
    assert r["aligned_cost"] is None
    assert r["comparable"] is False
    assert any(
        "缺复权因子，成本与现价尺度可能不一致，盈亏不可比" in s
        for s in r["limitations"]
    )


def test_align_unknown_cost_basis_no_exception() -> None:
    r = align_cost_to_quote(
        cost=10.0, cost_basis="whatever", adjustment_factor=1.0
    )
    assert r["comparable"] is False
    assert r["aligned_cost"] is None
    assert r["limitations"]
    assert "whatever" in " ".join(r["limitations"])


def test_align_invalid_factor_fail_closed() -> None:
    r = align_cost_to_quote(
        cost=10.0, cost_basis="forward_adjusted", adjustment_factor="oops"
    )
    assert r["comparable"] is False
    assert r["aligned_cost"] is None
    assert r["limitations"]


# ---------- apply_corporate_actions ----------

def test_single_ex_rights_total_cost_conserved() -> None:
    """单次除权：qty*avg_cost 总成本近似不变（pytest.approx）。"""
    qty, avg = 1000.0, 10.0
    before = qty * avg
    r = apply_corporate_actions(
        qty=qty,
        avg_cost=avg,
        actions=[{"date": "2024-06-03", "ex_factor": 1.5}],
    )
    assert r["qty"] == pytest.approx(1500.0)
    assert r["avg_cost"] == pytest.approx(10.0 / 1.5)
    assert r["qty"] * r["avg_cost"] == pytest.approx(before)
    assert r["limitations"] == []
    assert len(r["applied"]) == 1
    assert r["applied"][0]["qty_after"] == pytest.approx(1500.0)
    assert r["applied"][0]["avg_cost_after"] == pytest.approx(10.0 / 1.5)


def test_cash_dividend_separate_from_cost() -> None:
    """含现金分红：总额正确且 avg_cost 未被分红改动。"""
    r = apply_corporate_actions(
        qty=1000.0,
        avg_cost=10.0,
        actions=[{"date": "2024-06-03", "dividend": 0.5}],
    )
    assert r["cash_dividend_total"] == pytest.approx(500.0)
    assert r["qty"] == pytest.approx(1000.0)
    assert r["avg_cost"] == pytest.approx(10.0)
    assert r["applied"][0]["dividend_cash"] == pytest.approx(500.0)


def test_dividend_uses_pre_ex_qty_when_combined() -> None:
    """分红按除权前份额计，且与除权同条出现时仍独立进现金流。"""
    r = apply_corporate_actions(
        qty=1000.0,
        avg_cost=10.0,
        actions=[{"date": "2024-06-03", "ex_factor": 2.0, "dividend": 0.5}],
    )
    assert r["cash_dividend_total"] == pytest.approx(500.0)
    assert r["qty"] == pytest.approx(2000.0)
    assert r["avg_cost"] == pytest.approx(5.0)
    assert r["qty"] * r["avg_cost"] == pytest.approx(10000.0)


def test_ex_factor_wins_over_bonus_no_double_count() -> None:
    """ex_factor 与 bonus 同时给：以 ex_factor 为准、不双计、applied 有注明。"""
    r = apply_corporate_actions(
        qty=1000.0,
        avg_cost=10.0,
        actions=[{"date": "2024-06-03", "ex_factor": 2.0, "bonus": 0.5}],
    )
    assert r["qty"] == pytest.approx(2000.0)  # 不是 1000*2*1.5=3000
    assert r["avg_cost"] == pytest.approx(5.0)
    assert r["qty"] * r["avg_cost"] == pytest.approx(10000.0)
    note = r["applied"][0]["note"]
    assert "ex_factor" in note
    assert "忽略" in note or "双计" in note


def test_bonus_allot_dilution_without_ex_factor() -> None:
    """未给 ex_factor 时按 (1+bonus+allot) 摊薄，总成本守恒。"""
    r = apply_corporate_actions(
        qty=1000.0,
        avg_cost=13.0,
        actions=[{"date": "2024-06-03", "bonus": 0.2, "allot": 0.1}],
    )
    assert r["qty"] == pytest.approx(1300.0)
    assert r["avg_cost"] == pytest.approx(10.0)
    assert r["qty"] * r["avg_cost"] == pytest.approx(13000.0)


def test_replays_in_date_order() -> None:
    """按 date 升序逐条回放。"""
    r = apply_corporate_actions(
        qty=1000.0,
        avg_cost=10.0,
        actions=[
            {"date": "2024-09-01", "ex_factor": 2.0},
            {"date": "2024-06-01", "ex_factor": 1.5},
        ],
    )
    assert [a["date"] for a in r["applied"]] == ["2024-06-01", "2024-09-01"]
    assert r["qty"] == pytest.approx(3000.0)
    assert r["avg_cost"] == pytest.approx(10.0 / 3.0)


def test_empty_and_dirty_actions_fail_closed() -> None:
    """actions 为空 / 含脏数据：不抛异常、limitations 非空、qty/avg_cost 原样。"""
    r0 = apply_corporate_actions(qty=1000.0, avg_cost=10.0, actions=[])
    assert r0["qty"] == pytest.approx(1000.0)
    assert r0["avg_cost"] == pytest.approx(10.0)
    assert r0["limitations"]
    assert r0["applied"] == []

    dirty = [
        "not-a-dict",
        {"ex_factor": 1.2},
        {"date": "2024-06-03", "ex_factor": "oops"},
    ]
    r1 = apply_corporate_actions(qty=1000.0, avg_cost=10.0, actions=dirty)
    assert r1["qty"] == pytest.approx(1000.0)
    assert r1["avg_cost"] == pytest.approx(10.0)
    assert r1["limitations"]
    assert r1["applied"] == []


def test_negative_avg_cost_replays_without_exception() -> None:
    """负成本（做 T 摊薄）输入也能回放，不抛异常，总成本仍守恒。"""
    before = 1000.0 * (-2.0)
    r = apply_corporate_actions(
        qty=1000.0,
        avg_cost=-2.0,
        actions=[{"date": "2024-06-03", "ex_factor": 2.0, "dividend": 0.3}],
    )
    assert r["qty"] == pytest.approx(2000.0)
    assert r["avg_cost"] == pytest.approx(-1.0)
    assert r["qty"] * r["avg_cost"] == pytest.approx(before)
    assert r["cash_dividend_total"] == pytest.approx(300.0)
    assert r["applied"]
