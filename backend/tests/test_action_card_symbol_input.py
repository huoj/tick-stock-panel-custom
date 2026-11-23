"""行动卡 API 的"名称输入"回归测试。

背景（真实 bug）: 用户输入「美的」或「美的集团」，接口原来直接把输入 ``.upper()``
当 symbol 查日线 → 404 ``美的 没有可用日线数据 (asset_type=stock)``。
数据其实是齐的，缺的是"名称→代码"这一步。

这里从 HTTP 层锁住三条:
- 名称能解析 → 200，且卡上显示的是解析后的标准代码
- 名称歧义（「美的」= 美的电器 / 美的集团）→ 404 **并说明有哪几只**，不擅自挑一个
- 名字确实不存在 → 404 且文案是"未找到"，不能伪装成"数据缺失"

（只挂 action_card 路由，repo 用内存假实现 —— 不碰真实数据湖。）
"""
from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import action_card as ac_api
from app.config import settings

INSTRUMENTS = [
    ("000333.SZ", "美的集团", "stock"),
    ("000527.SZ", "美的电器", "stock"),
    ("605366.SH", "宏柏新材", "stock"),
]
TARGET = "000333.SZ"


def _bars(symbol: str = TARGET, n: int = 240) -> pl.DataFrame:
    """造一段温和上升的日线 —— 只为让 diagnose 跑得通，不关心结论。"""
    start = dt.date(2026, 1, 5)
    days = [start + dt.timedelta(days=i) for i in range(n)]
    closes = [10.0 + 0.02 * i for i in range(n)]
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "date": days,
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [100000.0] * n,
        }
    )


class _FakeRepo:
    """只实现 action_card 用到的三个方法。"""

    def __init__(self) -> None:
        self._inst = pl.DataFrame(
            {
                "symbol": [r[0] for r in INSTRUMENTS],
                "name": [r[1] for r in INSTRUMENTS],
                "type": [r[2] for r in INSTRUMENTS],
            }
        )

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        return self._inst.filter(pl.col("type") == asset_type)

    def get_daily_asset(self, asset_type, symbol, start, end):
        if asset_type != "stock":
            return pl.DataFrame()
        if symbol.upper() != TARGET:
            return pl.DataFrame()
        return _bars()

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    app = FastAPI()
    app.include_router(ac_api.router)
    app.include_router(ac_api.plans_router)
    app.state.repo = _FakeRepo()
    return TestClient(app)


def test_name_input_resolves_to_symbol(client):
    resp = client.get("/api/action-card/美的集团")
    assert resp.status_code == 200
    body = resp.json()
    # 卡上必须是标准代码 + 真名，而不是用户输入的原文
    assert body["symbol"] == TARGET
    assert body["name"] == "美的集团"


def test_bare_code_input_resolves(client):
    assert client.get("/api/action-card/000333").status_code == 200


def test_ambiguous_name_explains_candidates(client):
    resp = client.get("/api/action-card/美的")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "多只标的" in detail
    assert "000333.SZ" in detail and "000527.SZ" in detail
    # 不能只说"没有日线数据" —— 那会把"你输入得不够具体"说成"数据缺失"
    assert "没有可用日线数据" not in detail


def test_unknown_name_says_not_found(client):
    resp = client.get("/api/action-card/完全不存在")
    assert resp.status_code == 404
    assert "未找到" in resp.json()["detail"]


def test_diagnosis_endpoint_also_resolves_names(client):
    resp = client.get("/api/action-card/美的集团/diagnosis")
    assert resp.status_code == 200


def test_classify_reports_unresolved_without_failing_whole_request(client):
    resp = client.get("/api/position-plans/classify/000333.SZ,美的,不存在")
    assert resp.status_code == 200
    body = resp.json()
    assert [i["symbol"] for i in body["items"]] == [TARGET]
    unresolved = body["unresolved"]
    assert len(unresolved) == 2
    assert any("多只标的" in m for m in unresolved)
    assert any("未找到" in m for m in unresolved)
