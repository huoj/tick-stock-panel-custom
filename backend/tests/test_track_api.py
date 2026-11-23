"""前瞻跟踪 API 测试 —— 冻结、版本、评测、分组报表。

守的四个点：
1. 重新冻结只增版本，**旧版本不被覆盖**（用户明确要求）；
2. `data_cutoff` 晚于 `as_of` → 400（否则等于用未来数据）；
3. 报表把「官方证据足/映射明确」与「推测性主题关联」**分开**统计；
4. 样本不足时计入 `unscored`，而不是当成 0 收益/算作猜错。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import track as track_api

D0 = dt.date(2026, 1, 5)


def _client(data_dir) -> TestClient:
    app = FastAPI()
    app.include_router(track_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    return TestClient(app)


def _prices(data_dir, symbol, closes, start=D0):
    for i, close in enumerate(closes):
        day = start + dt.timedelta(days=i)
        part = data_dir / "kline_daily_enriched" / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame([{"symbol": symbol, "raw_close": close}]).write_parquet(part / "part.parquet")


def _payload(**over):
    base = {"symbol": "600941.SH", "direction": "利好", "verification": "fact",
            "mapping_level": "B", "event_title": "某政策"}
    base.update(over)
    return base


def test_freeze_then_read_back(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/track/event", json={
        "key": "evtgrp_1", "payload": _payload(), "as_of": "2026-01-01",
        "model_version": "v1",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1 and body["ok"] is True
    assert "旧版本不会被覆盖" in body["note"]

    listed = client.get("/api/track/event").json()
    assert listed["count"] == 1
    assert listed["items"][0]["versions"] == [1]
    assert listed["items"][0]["model_version"] == "v1"


def test_refreeze_keeps_old_version(tmp_path):
    client = _client(tmp_path)
    for direction, version in (("利好", 1), ("利空", 2)):
        r = client.post("/api/track/event", json={
            "key": "evtgrp_1", "payload": _payload(direction=direction),
            "as_of": f"2026-01-0{version}", "model_version": f"v{version}",
        })
        assert r.json()["version"] == version

    hist = client.get("/api/track/event/evtgrp_1").json()
    assert [v["version"] for v in hist["versions"]] == [1, 2]
    assert hist["versions"][0]["payload"]["direction"] == "利好", "旧预测必须保留原样"
    assert hist["versions"][1]["model_version"] == "v2"


def test_cutoff_after_as_of_is_400(tmp_path):
    resp = _client(tmp_path).post("/api/track/event", json={
        "key": "k", "payload": _payload(), "as_of": "2026-01-01", "data_cutoff": "2026-01-05",
    })
    assert resp.status_code == 400
    assert "晚于 as_of" in resp.json()["detail"]


def test_bad_date_and_unknown_kind_are_422(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/track/event", json={
        "key": "k", "payload": _payload(), "as_of": "not-a-date"}).status_code == 422
    assert client.get("/api/track/nope").status_code == 422
    assert client.post("/api/track/nope", json={
        "key": "k", "payload": _payload(), "as_of": "2026-01-01"}).status_code == 422


def test_missing_key_is_404(tmp_path):
    assert _client(tmp_path).get("/api/track/event/absent").status_code == 404


def test_report_evaluates_and_separates_tiers(tmp_path):
    _prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0, 14.0])
    client = _client(tmp_path)
    # 硬证据 + 利好 + 上涨 → 命中；推测性 + 利空 + 上涨 → 不命中
    client.post("/api/track/event", json={
        "key": "solid", "as_of": "2026-01-03", "data_cutoff": "2026-01-02",
        "payload": _payload(direction="利好", verification="fact", mapping_level="B")})
    client.post("/api/track/event", json={
        "key": "spec", "as_of": "2026-01-03", "data_cutoff": "2026-01-02",
        "payload": _payload(direction="利空", verification="inferred", mapping_level="C")})

    rep = client.get("/api/track/event/report", params={"horizon_days": 2}).json()
    assert rep["newly_evaluated"] == 2

    solid = rep["tiers"]["官方证据足/映射明确"]
    spec = rep["tiers"]["推测性主题关联"]
    assert solid["scored"] == 1 and solid["direction_accuracy"] == 1.0
    assert spec["scored"] == 1 and spec["direction_accuracy"] == 0.0
    assert rep["overall"]["direction_accuracy"] == 0.5
    assert "分开统计" in rep["note"]


def test_report_is_idempotent_for_same_horizon(tmp_path):
    _prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0])
    client = _client(tmp_path)
    client.post("/api/track/event", json={
        "key": "k", "as_of": "2026-01-03", "data_cutoff": "2026-01-02", "payload": _payload()})

    first = client.get("/api/track/event/report", params={"horizon_days": 2}).json()
    second = client.get("/api/track/event/report", params={"horizon_days": 2}).json()
    assert first["newly_evaluated"] == 1
    assert second["newly_evaluated"] == 0, "同一 horizon 不应重复打分"
    assert second["overall"]["scored"] == 1


def test_insufficient_data_lands_in_unscored(tmp_path):
    _prices(tmp_path, "600941.SH", [10.0])
    client = _client(tmp_path)
    client.post("/api/track/event", json={
        "key": "k", "as_of": "2026-01-03", "data_cutoff": "2026-01-02", "payload": _payload()})

    rep = client.get("/api/track/event/report", params={"horizon_days": 5}).json()
    assert rep["overall"]["scored"] == 0
    assert rep["overall"]["unscored"] == 1
    assert rep["overall"]["direction_accuracy"] is None
    assert rep["skipped"], "样本不足要有可读原因"


def test_evaluate_pending_false_skips_scoring(tmp_path):
    _prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0])
    client = _client(tmp_path)
    client.post("/api/track/event", json={
        "key": "k", "as_of": "2026-01-03", "data_cutoff": "2026-01-02", "payload": _payload()})
    rep = client.get("/api/track/event/report",
                     params={"evaluate_pending": False}).json()
    assert rep["newly_evaluated"] == 0
    assert rep["overall"]["samples"] == 0


def test_factor_kind_is_supported(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/api/track/factor", json={
        "key": "momentum_20d", "as_of": "2026-01-03",
        "payload": {"factor_id": "momentum_20d", "ic": 0.03}})
    assert resp.status_code == 200
    assert client.get("/api/track/factor").json()["count"] == 1
