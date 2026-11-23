"""模拟盘总览的"现价从哪来"测试。

背景（真实 bug）: 休市时 ``/api/paper/overview`` 从不传价格, ``overview`` 就按成本填充
``last_price`` ⇒ 所有持仓 ``pnl``/``pnl_pct`` 恒为 0, 用户「看不到收益多少钱」。
docstring 里写着"其余时刻用最近日线收盘价", 但那一路从未实现。

现在三档口径显式写在 ``price_source`` 上:
``live`` 盘中快照 → ``daily`` 最近日线收盘 → ``cost`` 取不到行情按成本。
本文件锁住: ①日线兜底真的能算出非零盈亏 ②取不到行情时必须标 ``priced=False``
而不是悄悄给一个假的 0 ③``estimating`` 语义不变（盘中估算才为 True）。
"""
from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

STOCK = "000065.SZ"
STOCK2 = "002765.SZ"


def _account(tmp_path) -> str:
    acc = paper.create_account(tmp_path, 1_000_000.0)
    return acc["id"]


def _write_positions(tmp_path, account_id: str) -> None:
    """直接物化持仓, 避开撮合链路 —— 本文件只测估值口径。"""
    root = tmp_path / "paper" / "accounts" / account_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "positions.json").write_text(
        json.dumps(
            {
                "positions": {
                    STOCK: {
                        "asset_type": "stock",
                        "qty": 1000,
                        "avg_cost": 10.0,
                        "available_qty": 1000,
                        "lots": [{"date": "2026-01-05", "qty": 1000}],
                    },
                    STOCK2: {
                        "asset_type": "stock",
                        "qty": 500,
                        "avg_cost": 4.0,
                        "available_qty": 500,
                        "lots": [{"date": "2026-01-05", "qty": 500}],
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_daily(tmp_path, rows: list[tuple[str, date, float]]) -> None:
    repo = KlineRepository(DataStore(tmp_path))
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "open": [r[2] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[2] for r in rows],
            "volume": [10000.0] * len(rows),
            "amount": [r[2] * 10000.0 for r in rows],
        }
    )
    repo.append_daily(df)


@pytest.fixture()
def lake(tmp_path):
    acc = _account(tmp_path)
    _write_positions(tmp_path, acc)
    _write_daily(
        tmp_path,
        [
            (STOCK, date(2026, 11, 11), 9.0),
            (STOCK, date(2026, 11, 12), 9.4),
            (STOCK, date(2026, 11, 13), 9.32),
            (STOCK2, date(2026, 11, 13), 4.5),
        ],
    )
    return tmp_path, acc


# ── 日线兜底 ────────────────────────────────────────────
def test_latest_close_map_picks_each_symbol_latest_bar(lake):
    data_dir, acc = lake
    closes = paper.latest_close_map(data_dir, acc)
    # 各标的取各自最近一根, 不要求同一天
    assert closes == {STOCK: 9.32, STOCK2: 4.5}


def test_latest_close_map_skips_symbols_without_bars(tmp_path):
    acc = _account(tmp_path)
    _write_positions(tmp_path, acc)  # 一条日线都没写
    assert paper.latest_close_map(tmp_path, acc) == {}


def test_overview_with_daily_closes_reports_real_pnl(lake):
    """这条是 bug ③ 的回归锁: 有日线就必须算出非零盈亏。"""
    data_dir, acc = lake
    closes = paper.latest_close_map(data_dir, acc)
    ov = paper.overview(data_dir, closes, account_id=acc, price_source="daily")

    assert ov["price_source"] == "daily"
    # 盘中估算标记不能因为用了日线就变成 True
    assert ov["estimating"] is False

    by_symbol = {h["symbol"]: h for h in ov["holdings"]}
    stock = by_symbol[STOCK]
    assert stock["priced"] is True
    assert stock["last_price"] == 9.32
    # 1000 股, 成本 10.0 → 亏 680 元 / -6.8%
    assert stock["pnl"] == pytest.approx(-680.0)
    assert stock["pnl_pct"] == pytest.approx(-6.8)

    other = by_symbol[STOCK2]
    assert other["pnl"] == pytest.approx(250.0)  # 500 股 ×(4.5-4.0)

    # 不看 total_pnl: 它等于「总资产-初始资金」, 而本夹具是直接物化持仓、
    # 没从现金里扣过买入款, 所以那只数字在这个夹具里没有可比意义。
    total_pnl = sum(h["pnl"] for h in ov["holdings"])
    assert total_pnl == pytest.approx(-430.0)


def test_overview_without_prices_marks_cost_and_zero_pnl(lake):
    """取不到行情时必须如实标 priced=False, 并让 price_source 说清是按成本。"""
    data_dir, acc = lake
    ov = paper.overview(data_dir, account_id=acc)

    assert ov["price_source"] == "cost"
    for h in ov["holdings"]:
        assert h["priced"] is False
        assert h["last_price"] == h["avg_cost"]


def test_partial_coverage_marks_only_the_missing_one(tmp_path):
    """只有一只票有日线: 有行情的那只算真盈亏, 另一只标按成本。"""
    acc = _account(tmp_path)
    _write_positions(tmp_path, acc)
    _write_daily(tmp_path, [(STOCK, date(2026, 11, 13), 9.32)])

    closes = paper.latest_close_map(tmp_path, acc)
    ov = paper.overview(tmp_path, closes, account_id=acc, price_source="daily")
    by_symbol = {h["symbol"]: h for h in ov["holdings"]}

    assert by_symbol[STOCK]["priced"] is True
    assert by_symbol[STOCK]["pnl"] != 0
    assert by_symbol[STOCK2]["priced"] is False
    assert by_symbol[STOCK2]["pnl"] == pytest.approx(0.0)


# ── 盘中口径 ────────────────────────────────────────────
def test_live_price_map_keeps_estimating_true(lake):
    """price_map 显式给出且未标口径时, 沿用历史语义: 盘中估算。"""
    data_dir, acc = lake
    ov = paper.overview(data_dir, {STOCK: 10.5}, account_id=acc)
    assert ov["price_source"] == "live"
    assert ov["estimating"] is True
    by_symbol = {h["symbol"]: h for h in ov["holdings"]}
    assert by_symbol[STOCK]["pnl"] == pytest.approx(500.0)
