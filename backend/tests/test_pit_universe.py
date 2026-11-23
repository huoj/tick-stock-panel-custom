"""按日 PIT 股票池测试 —— 历史某日的 ST 状态不能拿今天的名字顶替。"""

from __future__ import annotations

import pytest

from app.services import pit_universe as pit


DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
SYMS = ["A", "B", "C"]


def test_st_period_marks_only_its_own_days():
    out = pit.build_pit_universe(
        dates=DATES,
        symbols=SYMS,
        st_periods={"A": [("2026-01-06", "2026-01-07")]},
    )
    # 列顺序 = symbols；A 只在第 2、3 天是 ST
    assert [row[0] for row in out["is_st"]] == [False, True, True, False]
    assert [row[1] for row in out["is_st"]] == [False, False, False, False]
    assert out["source"] == "pit"


def test_build_is_st_field_shape():
    field = pit.build_is_st_field(
        dates=DATES, symbols=SYMS, st_periods={"B": [("2026-01-08", "2026-01-08")]}
    )
    assert len(field) == len(DATES)
    assert all(len(row) == len(SYMS) for row in field)
    assert field[3][1] is True


def test_listing_delisting_and_suspension_gate_tradable():
    out = pit.build_pit_universe(
        dates=DATES,
        symbols=SYMS,
        listing_dates={"A": "2026-01-06"},
        delisting_dates={"B": "2026-01-07"},
        suspended_dates={"C": {"2026-01-06"}},
        min_listing_days=0,
    )
    rows = [[row[i] for row in out["tradable"]] for i in range(len(SYMS))]
    # A: 上市前不可交易
    assert rows[0] == [False, True, True, True]
    # B: 退市后不可交易
    assert rows[1] == [True, True, True, False]
    # C: 只停牌一天
    assert rows[2] == [True, False, True, True]


def test_min_listing_days():
    out = pit.build_pit_universe(
        dates=DATES,
        symbols=["A"],
        listing_dates={"A": "2026-01-05"},
        min_listing_days=2,
    )
    assert [row[0] for row in out["tradable"]] == [False, False, True, True]
    assert [row[0] for row in out["listing_age_days"]] == [0, 1, 2, 3]


def test_no_events_falls_back_and_discloses():
    out = pit.build_pit_universe(dates=DATES, symbols=SYMS)
    assert out["source"] == "snapshot_fallback"
    assert any("时点正确性未核实" in t for t in out["limitations"])


def test_bad_dates_do_not_raise():
    out = pit.build_pit_universe(
        dates=["2026-01-05", "not-a-date"],
        symbols=["A"],
        st_periods={"A": [("2026-01-09", "2026-01-01"), ("bad", "2026-01-02")]},
    )
    assert out["tradable"][1] == [False]  # 坏日期按全不可交易
    assert any("无法解析" in t for t in out["limitations"])
    assert any("起晚于止" in t for t in out["limitations"])


def test_snapshot_fallback_matches_legacy():
    assert pit.st_snapshot_fallback(["浦发银行", "*ST海航", "退市美吉"]) == [False, True, True]


def test_describe_coverage():
    assert pit.describe_universe_coverage(has_pit_field=True)["source"] == "pit"
    assert pit.describe_universe_coverage(has_pit_field=False, exclude_st=False)["source"] == "not_applicable"
    fallback = pit.describe_universe_coverage(has_pit_field=False)
    assert fallback["source"] == "snapshot_fallback"
    assert any("幸存者偏差" in t for t in fallback["limitations"])


@pytest.mark.parametrize("n", [0, 1, 5])
def test_coverage_counts_pass_through(n):
    out = pit.describe_universe_coverage(has_pit_field=True, n_dates=n, n_symbols=n * 2)
    assert out["n_dates"] == n
    assert out["n_symbols"] == n * 2
