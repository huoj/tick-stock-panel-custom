"""信号的「风险元数据」契约测试 —— 理由 / 反证条件 / 持有周期 / 仓位上限。

这一组测试守的是一个容易被静默吞掉的点：`SignalModel` 如果没声明这些字段，
Pydantic 会**丢弃**它们，用户填了也存不进 JSON，风险画像里永远是「未填写」。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import risk as risk_api
from app.api import signals as signals_api
from app.strategy import custom_signals


@pytest.fixture
def data_dir(tmp_path) -> Path:
    d = tmp_path / "user_data" / "custom_signals"
    d.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def client(data_dir):
    app = FastAPI()
    app.include_router(signals_api.router)
    app.include_router(risk_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    return TestClient(app)


def _body(**over):
    base = {
        "id": "demo",
        "name": "演示信号",
        "kind": "entry",
        "conditions": [{"left": "close", "op": ">", "right": "field:ma20"}],
    }
    base.update(over)
    return base


# --- 归一化（纯函数）-----------------------------------------------------


def test_normalize_keeps_reason_and_falsify():
    out = custom_signals.normalize_risk_meta({
        "id": "x", "reason": "  收盘站上 MA20  ", "falsify": "跌回中轨下方  ",
    })
    assert out["reason"] == "收盘站上 MA20"
    assert out["falsify"] == "跌回中轨下方"


def test_normalize_blank_becomes_none():
    out = custom_signals.normalize_risk_meta({"id": "x", "reason": "   ", "falsify": ""})
    assert out["reason"] is None
    assert out["falsify"] is None


def test_normalize_coerces_horizon_and_cap():
    out = custom_signals.normalize_risk_meta({
        "id": "x", "horizonDays": "5", "positionCapPct": "8.5",
    })
    assert out["horizonDays"] == 5
    assert out["positionCapPct"] == pytest.approx(8.5)


def test_normalize_defaults_are_none():
    out = custom_signals.normalize_risk_meta({"id": "x"})
    assert out["horizonDays"] is None
    assert out["positionCapPct"] is None
    # 原字段不受影响
    assert out["id"] == "x"


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"horizonDays": 0}, "horizonDays"),
        ({"horizonDays": 999}, "horizonDays"),
        ({"horizonDays": "abc"}, "horizonDays"),
        ({"positionCapPct": 0}, "positionCapPct"),
        ({"positionCapPct": 101}, "positionCapPct"),
        ({"positionCapPct": "abc"}, "positionCapPct"),
        ({"reason": 123}, "reason"),
        ({"falsify": 123}, "falsify"),
        ({"reason": "x" * 500}, "reason"),
    ],
)
def test_normalize_rejects_bad_values(over, message):
    with pytest.raises(ValueError) as excinfo:
        custom_signals.normalize_risk_meta({"id": "x", **over})
    assert message in str(excinfo.value)


def test_validate_also_enforces_risk_meta():
    sig = _body(horizonDays=0)
    with pytest.raises(ValueError) as excinfo:
        custom_signals.validate(sig)
    assert "horizonDays" in str(excinfo.value)


def test_optional_risk_meta_absent_still_valid():
    custom_signals.validate(_body())


# --- 保存链路：字段必须真的落盘 -------------------------------------------


def test_save_persists_risk_meta(client, data_dir):
    resp = client.post("/api/custom-signals", json=_body(
        reason="收盘站上 MA20 且量能放大",
        falsify="收盘跌回中轨下方",
        horizonDays=5,
        positionCapPct=8,
    ))
    assert resp.status_code == 200, resp.text
    saved = json.loads((data_dir / "user_data" / "custom_signals" / "demo.json")
                       .read_text(encoding="utf-8"))
    assert saved["reason"] == "收盘站上 MA20 且量能放大"
    assert saved["falsify"] == "收盘跌回中轨下方"
    assert saved["horizonDays"] == 5
    assert saved["positionCapPct"] == 8


def test_save_without_risk_meta_keeps_nulls(client, data_dir):
    resp = client.post("/api/custom-signals", json=_body())
    assert resp.status_code == 200
    saved = json.loads((data_dir / "user_data" / "custom_signals" / "demo.json")
                       .read_text(encoding="utf-8"))
    # 归一后是 None，而不是缺键 —— 前端读到 null 才敢显示「未填写」
    assert saved.get("reason") is None
    assert saved.get("falsify") is None
    assert saved.get("horizonDays") is None
    assert saved.get("positionCapPct") is None


def test_save_rejects_bad_risk_meta(client):
    resp = client.post("/api/custom-signals", json=_body(positionCapPct=200))
    assert resp.status_code == 400
    assert "positionCapPct" in resp.json()["detail"]


def test_saved_meta_survives_reload(client, data_dir):
    client.post("/api/custom-signals", json=_body(reason="因为 A", falsify="除非 B"))
    loaded = custom_signals.load_all(data_dir)
    assert loaded[0]["reason"] == "因为 A"
    assert loaded[0]["falsify"] == "除非 B"


# --- 风险画像：作者声明要生效 ---------------------------------------------


def _write_history(data_dir: Path, days: int = 40) -> None:
    base = data_dir / "kline_daily_enriched"
    import datetime as dt

    start = dt.date(2026, 1, 5)
    closes = [10.0 + 0.05 * i for i in range(days)]
    for i in range(days):
        day = start + dt.timedelta(days=i)
        part = base / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["600001.SH"], "date": [day], "open": [closes[i]],
            "high": [closes[i]], "low": [closes[i]], "close": [closes[i]],
            "raw_close": [closes[i]], "volume": [1000], "amount": [5.0e7],
        }).write_parquet(part / "part.parquet")


def test_risk_endpoint_uses_declared_horizon_and_cap(client, data_dir):
    client.post("/api/custom-signals", json=_body(
        id="up", reason="一路上行", falsify="转为下行", horizonDays=10, positionCapPct=6,
    ))
    _write_history(data_dir, 40)
    resp = client.get("/api/risk/signal/up")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 作者声明 10 天 → 统计口径就是 10 天，而不是默认 5 天
    assert body["horizon_days"] == 10
    assert body["expected_holding_days"] == 10
    # 作者声明 6% → 仓位上限不能超过 6%
    assert body["position_cap"]["pct"] <= 6.0
    assert body["reason"] == "一路上行"
    assert body["falsify"] == "转为下行"
    assert body["declared"]["horizon_days"] == 10
    assert body["declared"]["position_cap_pct"] == 6


def test_risk_endpoint_query_overrides_declared(client, data_dir):
    client.post("/api/custom-signals", json=_body(id="up", horizonDays=10))
    _write_history(data_dir, 40)
    resp = client.get("/api/risk/signal/up", params={"horizon_days": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["horizon_days"] == 3
    # 声明值仍然回带，便于前端解释「为什么和作者填的不一样」
    assert body["declared"]["horizon_days"] == 10


def test_risk_endpoint_falls_back_to_default(client, data_dir):
    client.post("/api/custom-signals", json=_body(id="up"))
    _write_history(data_dir, 40)
    resp = client.get("/api/risk/signal/up")
    assert resp.status_code == 200
    assert resp.json()["horizon_days"] == risk_api.DEFAULT_HORIZON_DAYS
