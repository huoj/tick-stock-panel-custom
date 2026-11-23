"""真实持仓 vs 自选观察 —— 组合分析的口径区分。

守的是用户 2026-11-09 的要求：
「组合风险需要真实持仓权重、成本和持仓期限。目前没有这些信息，就不应把自选
清单当作真实组合；建议把『自选股观察』与『实际持仓分析』分开。」

最容易做错的一件事：把自选清单等权当作组合，于是算出"行业分散、回撤可控"的
假结论。这里锁住 —— 真实持仓的权重分母是**账户权益**（含现金），不是持仓
市值之和，也不是等权。
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.services import portfolio_positions as pp
from app.strategy import paper

SYM = "600519.SH"
SYM2 = "000001.SZ"


def _account(data_dir, *, cash: float, positions: dict) -> str:
    """建一个模拟盘账户并把持仓写成 positions.json（格式与线上一致）。"""
    paper.create_account(data_dir, 1_000_000)
    acc_id = paper.DEFAULT_ACCOUNT_ID
    acc = paper.get_account(data_dir, acc_id)
    acc["cash"] = cash
    paper.save_account(data_dir, acc, acc_id)
    root = paper._root(data_dir, acc_id)
    (root / "positions.json").write_text(
        json.dumps({"updated_at": "2026-11-09T00:00:00+08:00", "positions": positions},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return acc_id


def _pos(qty: int, avg_cost: float, lots: list[dict] | None = None) -> dict:
    return {"asset_type": "stock", "qty": qty, "avg_cost": avg_cost,
            "available_qty": qty, "lots": lots or []}


def test_weight_denominator_is_account_equity_not_position_sum(tmp_path):
    """权重分母是账户权益（现金 + 市值）。

    现金 90 万 + 持仓 2 万 = 权益 92 万，这一笔权重约 2.17%。
    如果按"持仓市值之和"算会得到 100%，账户看起来像满仓 —— 这正是要避免的。
    """
    _account(tmp_path, cash=900_000, positions={SYM: _pos(100, 100.0, [{"date": "2026-10-01", "qty": 100}])})
    out = pp.load_actual_positions(tmp_path, price_map={SYM: 200.0})

    assert out["basis"] == pp.ACTUAL_BASIS
    assert out["is_portfolio"] if "is_portfolio" in out else True
    p = out["positions"][0]
    assert p["market_value"] == 20_000.0
    assert out["total"] == 920_000.0
    assert p["weight"] == pytest.approx(20_000 / 920_000, abs=1e-5)
    assert p["weight"] < 0.05, "现金占大头时不该被算成满仓"
    assert out["weight_basis"] == "market_value / account_equity"


def test_holding_days_use_earliest_lot(tmp_path):
    """持仓期限取最早一笔建仓日（多笔加仓时不能被最后一笔覆盖）。"""
    lots = [{"date": "2026-11-05", "qty": 100}, {"date": "2026-10-01", "qty": 100}]
    _account(tmp_path, cash=0, positions={SYM: _pos(200, 50.0, lots)})
    p = pp.load_actual_positions(tmp_path, price_map={SYM: 60.0})["positions"][0]

    assert p["entry_date"] == "2026-10-01"
    assert p["lots_count"] == 2
    expected = (dt.date.today() - dt.date(2026, 10, 1)).days
    assert p["holding_days"] == expected


def test_missing_lots_and_cost_are_declared_not_guessed(tmp_path):
    """没有建仓日期/成本就如实记进 limitations，不补 0 也不猜。"""
    _account(tmp_path, cash=1000, positions={SYM: _pos(100, 0.0, [])})
    out = pp.load_actual_positions(tmp_path, price_map={SYM: 10.0})

    assert out["positions"][0]["holding_days"] is None
    assert out["positions"][0]["entry_date"] is None
    joined = " / ".join(out["limitations"])
    assert "建仓日期" in joined and "成本" in joined


def test_positions_sorted_by_weight_desc(tmp_path):
    _account(tmp_path, cash=0, positions={
        SYM: _pos(100, 10.0, [{"date": "2026-11-01", "qty": 100}]),
        SYM2: _pos(100, 30.0, [{"date": "2026-11-01", "qty": 100}]),
    })
    out = pp.load_actual_positions(tmp_path, price_map={SYM: 10.0, SYM2: 30.0})
    assert [p["symbol"] for p in out["positions"]] == [SYM2, SYM]


def test_resolve_account_prefers_largest_equity(tmp_path):
    """多账户时取权益最大的那个，并说明这个选择（不静默乱挑）。"""
    small, _ = pp.resolve_account(tmp_path)
    _account(tmp_path, cash=1_000, positions={SYM: _pos(100, 10.0, [{"date": "2026-11-01", "qty": 100}])})
    big = paper.create_account(tmp_path, 5_000_000)["id"]
    acc = paper.get_account(tmp_path, big)
    acc["cash"] = 4_000_000
    paper.save_account(tmp_path, acc, big)

    acc_id, note = pp.resolve_account(tmp_path)
    assert acc_id == big
    assert note and big in note
    assert small  # 空账户时不崩
    assert pp.resolve_account(tmp_path, big)[0] == big


def test_resolve_account_without_positions_is_explicit(tmp_path):
    acc_id, note = pp.resolve_account(tmp_path)
    assert acc_id == paper.DEFAULT_ACCOUNT_ID
    assert note and "没有找到有持仓的模拟盘账户" in note


def test_observation_basis_is_not_a_portfolio():
    """自选观察口径必须自带"这不是组合"的标记与提醒。"""
    out = pp.observation(["600519.SH", " ", "000001.SZ"])
    assert out["basis"] == pp.WATCHLIST_BASIS
    assert out["is_portfolio"] is False
    assert out["symbols"] == ["600519.SH", "000001.SZ"]
    assert "不是实际持仓组合" in out["warning"]
    assert out["basis_label"] == "自选股观察（不是组合）"
    assert not any(k in out for k in ("positions", "total", "weight"))


def test_holdings_for_risk_skips_unknown_weights(tmp_path):
    _account(tmp_path, cash=0, positions={SYM: _pos(100, 10.0, [{"date": "2026-11-01", "qty": 100}])})
    out = pp.load_actual_positions(tmp_path, price_map={SYM: 10.0})
    rows = pp.holdings_for_risk(out)
    assert rows == [{"symbol": SYM, "weight": pytest.approx(1.0, rel=1e-6)}]

    out["positions"][0]["weight"] = None
    assert pp.holdings_for_risk(out) == [], "权重未知时不能编一个权重喂给风险模型"


def test_close_price_fallback_is_disclosed(tmp_path):
    """不给实时价时必须说明估值来源。

    两档都要能分辨: 有日线 → ``daily``（用最近收盘价）; 连日线都没有 → ``cost``
    （按成本估, 此时盈亏恒为 0, 必须明说"不代表真的不亏不赚"）。
    """
    _account(tmp_path, cash=0, positions={SYM: _pos(100, 10.0, [{"date": "2026-11-01", "qty": 100}])})
    out = pp.load_actual_positions(tmp_path)
    # 本夹具没有 kline_daily → 只能退回成本档
    assert out["price_source"] == "cost"
    assert any("盈亏恒为 0" in x for x in out["limitations"])


def test_close_price_fallback_uses_daily_when_available(tmp_path):
    """有日线时必须真的用日线估值, 而不是按成本把盈亏抹成 0。"""
    import polars as pl

    from app.tickflow.repository import DataStore, KlineRepository

    _account(tmp_path, cash=0, positions={SYM: _pos(100, 10.0, [{"date": "2026-11-01", "qty": 100}])})
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(
        pl.DataFrame(
            {
                "symbol": [SYM],
                "date": [__import__("datetime").date(2026, 11, 13)],
                "open": [12.0],
                "high": [12.0],
                "low": [12.0],
                "close": [12.0],
                "volume": [1000.0],
                "amount": [12000.0],
            }
        )
    )
    out = pp.load_actual_positions(tmp_path)
    assert out["price_source"] == "daily"
    pos = out["positions"][0]
    assert pos["last_price"] == 12.0
    assert pos["pnl_pct"] == pytest.approx(20.0)
    assert any("最近一根日线收盘价" in x for x in out["limitations"])


# ── API 层：两个口径必须在返回值里就能分辨 ──────────────────


def _client(data_dir):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import risk as risk_api

    app = FastAPI()
    app.include_router(risk_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    return TestClient(app)


def test_positions_endpoint_returns_real_weights(tmp_path):
    _account(tmp_path, cash=900_000, positions={SYM: _pos(100, 100.0, [{"date": "2026-10-01", "qty": 100}])})
    body = _client(tmp_path).get("/api/risk/positions").json()

    assert body["basis"] == pp.ACTUAL_BASIS
    assert body["basis_label"] == "实际持仓分析"
    assert body["weight_basis"] == "market_value / account_equity"
    assert body["positions"][0]["symbol"] == SYM
    assert body["positions"][0]["holding_days"] is not None


def test_portfolio_by_account_is_actual_positions(tmp_path):
    """account= 走真实持仓：带 basis 标记与账户号，不出现观察口径的 warning。"""
    _account(tmp_path, cash=900_000, positions={SYM: _pos(100, 100.0, [{"date": "2026-10-01", "qty": 100}])})
    body = _client(tmp_path).get("/api/risk/portfolio", params={"account": paper.DEFAULT_ACCOUNT_ID}).json()

    assert body["basis"] == pp.ACTUAL_BASIS
    assert body["account_id"] == paper.DEFAULT_ACCOUNT_ID
    assert body["positions_fed"] == 1
    assert "warning" not in body, "真实持仓口径不该带「这不是组合」的警告"
    assert isinstance(body["limitations"], list)


def test_portfolio_by_symbols_is_labelled_observation(tmp_path):
    """symbols= 是观察口径：必须显式告诉调用方这不是实际持仓组合。"""
    body = _client(tmp_path).get("/api/risk/portfolio", params={"symbols": f"{SYM},{SYM2}"}).json()

    assert body["basis"] == pp.WATCHLIST_BASIS
    assert body["basis_label"] == "自选股观察（不是组合）"
    assert body["is_portfolio"] is False
    assert "不是实际持仓组合" in body["warning"]
    assert body["weight_basis"] == "等权假设"

    with_weights = _client(tmp_path).get(
        "/api/risk/portfolio", params={"symbols": f"{SYM},{SYM2}", "weights": "0.6,0.4"}
    ).json()
    assert with_weights["basis"] == pp.CALLER_BASIS
    assert with_weights["weight_basis"] == "调用方提供"


def test_portfolio_requires_exactly_one_of_account_or_symbols(tmp_path):
    client = _client(tmp_path)
    assert client.get("/api/risk/portfolio").status_code == 400
    both = client.get("/api/risk/portfolio",
                      params={"symbols": SYM, "account": paper.DEFAULT_ACCOUNT_ID})
    assert both.status_code == 400
    assert "只能给一个" in both.json()["detail"]


def test_portfolio_for_account_without_positions_is_400(tmp_path):
    paper.create_account(tmp_path, 1_000_000)
    resp = _client(tmp_path).get("/api/risk/portfolio", params={"account": paper.DEFAULT_ACCOUNT_ID})
    assert resp.status_code == 400
    assert "没有可用于组合分析的持仓" in resp.json()["detail"]


def test_post_portfolio_marks_caller_supplied_weights(tmp_path):
    resp = _client(tmp_path).post("/api/risk/portfolio", json={
        "holdings": [{"symbol": SYM, "weight": 0.5}, {"symbol": SYM2, "weight": 0.5}],
    })
    body = resp.json()
    assert resp.status_code == 200
    assert body["basis"] == pp.CALLER_BASIS
    assert "未与任何模拟盘账户核对" in body["warning"]
    assert body["weight_basis"] == "调用方提供"

