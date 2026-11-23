"""数据集目录与新鲜度测试 — 用合成数据湖, 不依赖真实 data/。

覆盖:
  - id 与磁盘目录名一致 (§ 便于 grep / 不再拼 kline_stock_daily)
  - 每个数据集都声明币种, 每个字段都声明单位 (§ 需求 1)
  - ext 配置里的 unit/currency 覆盖优先于代码侧证据表
  - 盘中快照识别 → 期望锚点退到上一个会话, 避免交易时段误报 stale
  - 滞后按"全市场会话"而不是"自己的分区"计算 (§ ETF 落后两天)
  - absent / empty / quarterly / event 各自的判定
  - PIT 的 usable 与 data_pit 的模式口径一致
  - /api/datasets 四个端点的形状与 404
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import datasets as datasets_api
from app.services import dataset_catalog as dc

CN_TZ = timezone(timedelta(hours=8))

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)     # 已定版的最后一个完整交易日
D4 = date(2026, 11, 6)     # 盘中快照


def _ms(day: date, hour: int, minute: int) -> int:
    return int(
        datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ).timestamp() * 1000
    )


def _write_partition(root: Path, table: str, day: date, *, quote_ts: int | None) -> None:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "date": [day],
            "open": [10.0],
            "high": [10.5],
            "low": [9.9],
            "close": [10.2],
            "volume": [1000],
            "amount": [1_020_000.0],
            "raw_close": [10.2],
            "raw_high": [10.5],
            "raw_low": [9.9],
            "turnover_rate": [2.5],
            "quote_ts": pl.Series([quote_ts], dtype=pl.Int64),
        }
    ).write_parquet(part / "part.parquet")


def _write_ext_config(root: Path, table_id: str, *, mode: str, unit: str | None,
                      currency: str | None, field_names: tuple[str, ...] = ("symbol",)) -> Path:
    d = root / "ext_data" / table_id
    d.mkdir(parents=True, exist_ok=True)
    fields: list[dict[str, object]] = []
    for name in field_names:
        field: dict[str, object] = {"name": name, "dtype": "float", "label": name}
        if name == "symbol":
            field["dtype"] = "string"
        if unit is not None:
            field["unit"] = unit
        fields.append(field)
    cfg: dict[str, object] = {
        "id": table_id,
        "label": table_id,
        "mode": mode,
        "fields": fields,
    }
    if currency is not None:
        cfg["currency"] = currency
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def lake(tmp_path) -> Path:
    root = tmp_path / "data"
    root.mkdir()

    # 行情: 11-03~11-05 定版, 11-06 盘中快照
    for day in (D1, D2, D3):
        _write_partition(root, "kline_daily", day, quote_ts=None)
        _write_partition(root, "kline_daily_enriched", day, quote_ts=None)
    _write_partition(root, "kline_daily", D4, quote_ts=_ms(D4, 10, 30))
    _write_partition(root, "kline_daily_enriched", D4, quote_ts=_ms(D4, 10, 30))

    # ETF: 只到 11-04, 落后一个完整交易日
    for day in (D1, D2):
        _write_partition(root, "kline_etf_daily", day, quote_ts=None)
        _write_partition(root, "kline_etf_enriched", day, quote_ts=None)

    # 复权因子: 事件频率, 最近事件在很久以前, 不应判 stale
    (root / "adj_factor").mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "trade_date": [date(2026, 6, 12)],
            "ex_factor": [1.5],
            "dividend": [0.3],
            "bonus": [0.0],
            "allot": [0.0],
            "allot_price": [0.0],
            "prev_close": [10.0],
        }
    ).write_parquet(root / "adj_factor" / "all.parquet")

    # 财务: 季度数据 + 公告日; shares 目录存在但为空
    (root / "financials" / "metrics").mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "period_end": ["2026-09-30"],
            "announce_date": ["2026-10-23"],
            "eps_basic": [0.5],
        }
    ).write_parquet(root / "financials" / "metrics" / "part.parquet")
    (root / "financials" / "shares").mkdir(parents=True)

    # ext: 显式声明 unit/currency 的表
    d = _write_ext_config(root, "ext_demo_ts", mode="timeseries",
                          unit="自定义单位", currency="HKD")
    part = d / "timeseries" / f"date={D3.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600001.SH"], "date": [D3], "v": [1.0]}).write_parquet(
        part / "part.parquet"
    )
    snap = _write_ext_config(root, "ext_demo_snap", mode="snapshot", unit=None, currency=None,
                             field_names=("symbol", "v"))
    pl.DataFrame({"symbol": ["600001.SH"], "v": [1.0]}).write_parquet(snap / "part.parquet")

    # ext: 未声明单位时回退到代码侧证据表 (ext_valuation.amount 上游是万元)
    plain = _write_ext_config(root, "ext_valuation", mode="timeseries",
                              unit=None, currency=None,
                              field_names=("symbol", "amount", "pe_ttm"))
    vpart = plain / "timeseries" / f"date={D3.isoformat()}"
    vpart.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600001.SH"], "date": [D3], "amount": [12_345.0], "pe_ttm": [20.0]}
    ).write_parquet(vpart / "part.parquet")

    return root


def _fresh(lake: Path, dataset_id: str):
    spec = dc.get_dataset(lake, dataset_id)
    assert spec is not None, dataset_id
    return dc.dataset_freshness(lake, spec)


# --- 目录 -----------------------------------------------------------------


def test_ids_follow_disk_directory_names(lake):
    ids = [s.id for s in dc.build_catalog(lake)]
    assert len(ids) == len(set(ids))
    # 六张行情表的 id 必须与磁盘目录逐字一致 —— 这里是"拼接推导"踩过的坑:
    # stock 是 kline_daily(_enriched), etf/index 是 kline_etf_enriched /
    # kline_index_enriched, 规则不统一。
    assert {
        "kline_daily",
        "kline_daily_enriched",
        "kline_etf_daily",
        "kline_etf_enriched",
        "kline_index_daily",
        "kline_index_enriched",
    } <= set(ids)
    # 拼接推导会得到 kline_etf_daily_enriched / kline_index_daily_enriched 这类
    # 磁盘上不存在的名字 —— 显式确认没有。
    assert not any(
        "kline_stock" in i or "etf_daily_enriched" in i or "index_daily_enriched" in i
        for i in ids
    )
    for spec in dc.build_catalog(lake):
        if spec.domain == "kline":
            assert spec.id == spec.relpath
    assert "ext_demo_ts" in ids and "ext_demo_snap" in ids


def test_every_dataset_declares_currency_timezone_and_field_units(lake):
    for spec in dc.build_catalog(lake):
        assert spec.currency, spec.id
        assert spec.timezone == "Asia/Shanghai", spec.id
        assert spec.source, spec.id
        assert spec.source_basis in {
            "config_pull", "docs", "routing", "code", "none"
        }, spec.id
        for field in spec.fields:
            assert field.unit, f"{spec.id}.{field.name} 缺单位"


def test_enriched_turnover_rate_is_percent_not_fraction(lake):
    """§3.1: enriched 的 turnover_rate 是百分数值, 目录必须写明而不是留给人猜。"""
    spec = dc.get_dataset(lake, "kline_daily_enriched")
    field = next(f for f in spec.fields if f.name == "turnover_rate")
    assert field.unit == dc.UNIT_PERCENT
    assert "0.05" in field.note or "5" in field.note


def test_ext_config_unit_and_currency_override_code_tables(lake):
    spec = dc.get_dataset(lake, "ext_demo_ts")
    assert spec.currency == "HKD"
    assert spec.fields[0].unit == "自定义单位"


def test_ext_falls_back_to_code_side_unit_evidence(lake):
    """配置未声明单位时用代码侧证据表: ext_valuation.amount 上游口径是万元。"""
    spec = dc.get_dataset(lake, "ext_valuation")
    units = {f.name: f.unit for f in spec.fields}
    assert units["amount"] == dc.UNIT_WAN
    assert units["pe_ttm"] == dc.UNIT_TIMES


def test_undeclared_field_unit_falls_back_to_dtype_only(lake):
    """既没显式声明也没证据表的列：只按 dtype 给能确定的单位，数值列仍 unknown。"""
    spec = dc.get_dataset(lake, "ext_demo_snap")
    units = {f.name: f.unit for f in spec.fields}
    assert units["symbol"] == dc.UNIT_TEXT
    assert units["v"] == dc.UNIT_UNKNOWN


# --- 新鲜度 ---------------------------------------------------------------


def test_intraday_partition_is_flagged_and_anchor_shifts(lake):
    fresh = _fresh(lake, "kline_daily_enriched")
    assert fresh.settlement == "intraday"
    assert fresh.latest_date == D4.isoformat()
    # 盘中快照就是"应到"的最新, 交易时段不该判 stale
    assert fresh.lag_sessions == 0
    assert fresh.status == "ok"
    assert D3.isoformat() in fresh.note


def test_lag_measured_against_market_sessions_not_own_partitions(lake):
    """曾经的 bug: 拿自己的分区日算滞后 → ETF 落后一天却显示 lag=0。"""
    fresh = _fresh(lake, "kline_etf_enriched")
    assert fresh.latest_date == D2.isoformat()
    assert fresh.settlement == "final"
    assert fresh.lag_sessions == 1
    assert fresh.status == "stale"
    assert fresh.anchor_date == D3.isoformat()


def test_absent_and_empty_are_distinguished(lake):
    assert _fresh(lake, "kline_minute").status == "absent"
    assert _fresh(lake, "financials_shares").status == "empty"


def test_quarterly_cadence_does_not_report_stale(lake):
    fresh = _fresh(lake, "financials_metrics")
    assert fresh.latest_date == "2026-09-30"
    assert fresh.status == "ok"


def test_event_cadence_is_never_stale(lake):
    fresh = _fresh(lake, "adj_factor")
    assert fresh.status == "ok"
    assert "event" in fresh.note


def test_snapshot_table_without_date_column_falls_back_to_mtime(lake):
    fresh = _fresh(lake, "ext_demo_snap")
    assert fresh.latest_date is None
    assert fresh.updated_at is not None
    assert fresh.status == "ok"
    assert "落盘时间" in fresh.note


def test_rows_reported_for_latest_partition(lake):
    assert _fresh(lake, "kline_daily").rows == 1


# --- PIT ------------------------------------------------------------------


def test_pit_usable_matches_data_pit_modes(lake):
    fin = dc.get_dataset(lake, "financials_metrics").to_dict()["pit"]
    assert fin["mode"] == dc.PIT_ANNOUNCEMENT
    assert fin["known_at_column"] == "announce_date"
    assert fin["period_column"] == "period_end"
    assert fin["usable"] is True

    # 行情类用日期列本身当可知日 —— data_pit 认为可用，catalog 必须给同一答案。
    market = dc.get_dataset(lake, "kline_daily").to_dict()["pit"]
    assert market["mode"] == dc.PIT_EVENT_DATE
    assert market["known_at_column"] == "date"
    assert market["usable"] is True

    # 其余数据集：usable 必须严格等于「模式可用 且 真的有可知日列」。
    # 这条不变式同时保证 catalog 与 data_pit.USABLE_MODES 不会各说各话。
    for spec in dc.build_catalog(lake):
        pit = spec.to_dict()["pit"]
        expected = spec.pit_mode in dc.PIT_USABLE_MODES and bool(spec.known_at_column)
        assert pit["usable"] is expected, spec.id
        if pit["usable"] is False:
            assert pit["mode"] not in dc.PIT_USABLE_MODES or not spec.known_at_column

# --- API ------------------------------------------------------------------


@pytest.fixture
def client(lake, monkeypatch):
    monkeypatch.setattr(datasets_api, "settings", SimpleNamespace(data_dir=lake))
    app = FastAPI()
    app.include_router(datasets_api.router)
    return TestClient(app)


def test_catalog_endpoint_shape(client):
    resp = client.get("/api/datasets/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == len(body["items"]) >= 5
    assert body["session_anchor"] == D4.isoformat()
    ids = {it["id"] for it in body["items"]}
    assert {"kline_daily", "kline_etf_enriched", "ext_demo_ts"} <= ids
    sample = next(it for it in body["items"] if it["id"] == "ext_valuation")
    assert sample["currency"] == "CNY"
    assert sample["freshness"]["latest_date"] == D3.isoformat()


def test_catalog_endpoint_domain_filter(client):
    resp = client.get("/api/datasets/catalog", params={"domain": "ext"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(it["domain"] == "ext" for it in items)


def test_freshness_endpoint_and_unknown_id(client):
    ok = client.get("/api/datasets/kline_etf_enriched/freshness")
    assert ok.status_code == 200
    assert ok.json()["status"] == "stale"

    detail = client.get("/api/datasets/kline_etf_enriched")
    assert detail.status_code == 200
    assert detail.json()["freshness"]["lag_sessions"] == 1

    missing = client.get("/api/datasets/no_such_table")
    assert missing.status_code == 404
