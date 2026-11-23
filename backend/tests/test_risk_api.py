"""风险 API 契约测试 —— /api/risk 的信号画像与组合风险。

用合成数据湖 + 真实信号定义，验证：
  - 信号端点把「分数」与「胜率」分开返回，且带样本数与置信区间
  - 未知信号 404；enriched 历史缺失 409
  - 组合端点归一化权重、接受 5/0.05 两种回撤写法、数量不一致 400
  - 组合报告四块结论齐全
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import risk as risk_api

D0 = dt.date(2026, 1, 5)


def _write_signal(data_dir: Path, sid: str, *, reason: str = "", falsify: str = "") -> None:
    d = data_dir / "user_data" / "custom_signals"
    d.mkdir(parents=True, exist_ok=True)
    body = {
        "id": sid,
        "name": f"信号 {sid}",
        "kind": "entry",
        "timeframe": "daily",
        "enabled": True,
        "conditions": [{"left": "close", "op": ">", "right": "field:ma20",
                        "leftDays": 0, "rightDays": 0}],
        "column": f"csg_{sid}",
    }
    if reason:
        body["reason"] = reason
    if falsify:
        body["falsify"] = falsify
    (d / f"{sid}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def lake(tmp_path) -> Path:
    _write_signal(tmp_path, "demo", reason="收盘站上 MA20", falsify="收盘跌破 MA20")
    base = tmp_path / "kline_daily_enriched"
    closes = [10.0 + 0.05 * i for i in range(60)]
    for i in range(60):
        day = D0 + dt.timedelta(days=i)
        part = base / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        rows = []
        for sym in ("600001.SH", "600002.SH", "600003.SH"):
            rows.append({
                "symbol": sym, "date": day, "close": closes[i], "raw_close": closes[i],
                "amount": 5.0e7, "csg_demo": i % 3 != 0,
            })
        pl.DataFrame(rows).write_parquet(part / "part.parquet")
    hy = tmp_path / "ext_data" / "ext_hy_ths"
    hy.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH"],
        "所属同花顺行业": ["银行", "银行", "白酒"],
    }).write_parquet(hy / "part.parquet")
    return tmp_path


@pytest.fixture
def client(lake, monkeypatch):
    data_dir = lake
    app = FastAPI()
    app.include_router(risk_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    return TestClient(app)


def test_signal_risk_endpoint_separates_score_and_win_rate(client):
    resp = client.get("/api/risk/signal/demo", params={"horizon_days": 5, "lookback_days": 60})
    assert resp.status_code == 200
    body = resp.json()
    assert body["signal_id"] == "demo"
    assert body["samples"] > 0
    assert body["win_rate"] is not None
    ci = body["confidence_interval"]
    assert ci["method"] == "wilson" and ci["level"] == 0.95
    assert ci["low"] < ci["high"]
    assert body["signal_score"]["meaning"] == "排序用强度指标（0-100），不是概率"
    assert body["position_cap"]["pct"] > 0
    assert body["position_cap"]["basis"]
    assert body["reason"] == "收盘站上 MA20"
    assert body["falsify"] == "收盘跌破 MA20"
    assert body["expected_holding_days"] == 5
    assert body["liquidity"]["bucket"] in {"high", "mid", "low", "unknown"}
    assert body["potential_drawdown"] is not None


def test_signal_risk_endpoint_unknown_signal(client):
    resp = client.get("/api/risk/signal/nope")
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_signal_risk_endpoint_without_history(client, monkeypatch):
    monkeypatch.setattr(risk_api.signal_risk, "load_enriched_history",
                        lambda *a, **k: pl.DataFrame())
    resp = client.get("/api/risk/signal/demo")
    assert resp.status_code == 409
    assert "enriched" in resp.json()["detail"]


def test_list_signals_endpoint(client):
    resp = client.get("/api/risk/signals", params={"lookback_days": 60})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert "不是概率" in body["note"]
    first = body["signals"][0]
    assert first["signal_score"]["value"] >= 0
    # 按分数降序
    scores = [s["signal_score"]["value"] for s in body["signals"]]
    assert scores == sorted(scores, reverse=True)


def test_portfolio_get_normalizes_and_reports(client):
    resp = client.get("/api/risk/portfolio", params={
        "symbols": "600001.SH,600002.SH,600003.SH",
        "weights": "50,30,20",
        "drawdowns": "5,0.08,3",
        "budget_pct": 15,
        "lookback_days": 30,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert sum(p["weight"] for p in body["positions"]) == pytest.approx(1.0, abs=1e-4)
    # 5 与 0.08 都被归一成小数
    dd = {p["symbol"]: p["drawdown_pct"] for p in body["positions"]}
    assert dd["600001.SH"] == pytest.approx(0.05)
    assert dd["600002.SH"] == pytest.approx(0.08)
    assert dd["600003.SH"] == pytest.approx(0.03)
    assert body["industry_concentration"]["top_industry"] == "银行"
    assert body["industry_concentration"]["top_industry_share"] == pytest.approx(0.8)
    assert body["drawdown_budget"]["budget_pct"] == 15
    assert body["per_position_risk"]["total_risk_contribution"] > 0


def test_portfolio_get_weight_count_mismatch(client):
    resp = client.get("/api/risk/portfolio", params={"symbols": "A,B", "weights": "50"})
    assert resp.status_code == 400
    assert "不一致" in resp.json()["detail"]


def test_portfolio_get_drawdown_count_mismatch(client):
    resp = client.get("/api/risk/portfolio", params={"symbols": "A,B,C", "drawdowns": "5"})
    assert resp.status_code == 400


def test_portfolio_get_empty_symbols(client):
    resp = client.get("/api/risk/portfolio", params={"symbols": " , "})
    assert resp.status_code == 400


def test_portfolio_post_structured(client):
    resp = client.post("/api/risk/portfolio", json={
        "holdings": [
            {"symbol": "600001.SH", "weight": 60, "drawdown_pct": 10},
            {"symbol": "600003.SH", "weight": 40, "drawdown_pct": 20},
        ],
        "budget_pct": 12,
        "lookback_days": 30,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert sum(p["weight"] for p in body["positions"]) == pytest.approx(1.0, abs=1e-4)
    assert body["drawdown_budget"]["over_budget"] is True
    assert body["drawdown_budget"]["suggested_cuts"]


def test_portfolio_post_rejects_bad_budget(client):
    resp = client.post("/api/risk/portfolio", json={
        "holdings": [{"symbol": "600001.SH", "weight": 100}],
        "budget_pct": 0.5,
    })
    assert resp.status_code == 400


def test_portfolio_post_empty_holdings_reports_limitation(client):
    resp = client.post("/api/risk/portfolio", json={"holdings": []})
    assert resp.status_code == 200
    body = resp.json()
    assert any("组合为空" in t for t in body["limitations"])
