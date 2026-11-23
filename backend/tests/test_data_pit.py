"""`services/data_pit.py` —— 「当时已知」取数与 fail-closed 断言的回归测试。

测试数据湖全部合成，刻意区分四种口径：
- `financials_metrics`：`announcement`，公告日在盘后，必须 T+1 才可用；
- `ext_valuation`：`event_date`，日期列本身就是可知日，当日即可用；
- `ext_forecast`：`period_only`，只有报告期，必须拒绝；
- `ext_unlock`：`unknown`，没有任何日期列，必须拒绝。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import datasets as datasets_api
from app.services import data_pit as dp
from app.services import dataset_catalog as dc

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)


def _write_ext_config(root: Path, table_id: str, *, mode: str,
                      field_names: tuple[str, ...]) -> Path:
    d = root / "ext_data" / table_id
    d.mkdir(parents=True, exist_ok=True)
    fields = [
        {"name": n, "dtype": "string" if n == "symbol" else "float", "label": n}
        for n in field_names
    ]
    (d / "config.json").write_text(
        json.dumps({"id": table_id, "label": table_id, "mode": mode, "fields": fields},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def lake(tmp_path) -> Path:
    root = tmp_path / "data"
    root.mkdir()

    # 行情基准: 只用来给 session_days 提供会话，不参与 PIT 断言。
    for day in (D1, D2, D3):
        part = root / "kline_daily" / f"date={day.isoformat()}"
        part.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600001.SH"], "date": [day], "close": [10.0]}).write_parquet(
            part / "part.parquet"
        )

    # 财务: 同一标的两个报告期, 公告日分别是 10-23 与 10-30, 其中一行的公告日缺失。
    (root / "financials" / "metrics").mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "600001.SH", "600002.SH"],
            "period_end": ["2026-09-30", "2026-06-30", "2026-09-30"],
            "announce_date": ["2026-10-23", "2026-10-30", None],
            "eps_basic": [0.5, 0.4, 0.3],
        }
    ).write_parquet(root / "financials" / "metrics" / "part.parquet")

    # 财务: 整张表的公告日全为空 → 数据集级不可用, 取数必须 fail-closed。
    (root / "financials" / "shares").mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600003.SH"],
            "period_end": ["2026-09-30"],
            "announce_date": [None],
            "float_shares": [1.0e9],
        }
    ).write_parquet(root / "financials" / "shares" / "part.parquet")

    # ext: 时序表，date 只存在于 Hive 路径上（文件里没有 date 列）。
    ts = _write_ext_config(root, "ext_valuation", mode="timeseries",
                           field_names=("symbol", "amount"))
    for day in (D1, D2, D3):
        part = ts / "timeseries" / f"date={day.isoformat()}"
        part.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600001.SH"], "amount": [float(day.day)]}).write_parquet(
            part / "part.parquet"
        )

    # ext: 快照表, 可知日是 YYYYMMDD 字符串（ext_bsdetail 的真实形态）。
    snap = _write_ext_config(root, "ext_bsdetail", mode="snapshot",
                             field_names=("symbol", "announce_date", "inventory"))
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH"],
            "announce_date": ["20261023", "20261105"],
            "inventory": [100.0, 200.0],
        }
    ).write_parquet(snap / "part.parquet")

    # ext: 只有报告期 → 必须拒绝。
    plain = _write_ext_config(root, "ext_forecast", mode="snapshot",
                              field_names=("symbol", "period", "forecast_value"))
    pl.DataFrame(
        {"symbol": ["600001.SH"], "period": ["2026-09-30"], "forecast_value": [1.0]}
    ).write_parquet(plain / "part.parquet")

    # ext: 完全无日期列 → 必须拒绝。
    nodate = _write_ext_config(root, "ext_unlock", mode="snapshot",
                               field_names=("symbol", "total_mv"))
    pl.DataFrame({"symbol": ["600001.SH"], "total_mv": [1.0]}).write_parquet(
        nodate / "part.parquet"
    )

    return root


def _decl(lake: Path, dataset_id: str) -> dp.PitDeclaration:
    spec = dc.get_dataset(lake, dataset_id)
    assert spec is not None, dataset_id
    return dp.declare(spec)


# --- 声明 -----------------------------------------------------------------


def test_declaration_usable_only_for_event_and_announcement(lake):
    assert _decl(lake, "financials_metrics").usable is True
    assert _decl(lake, "financials_metrics").mode == dc.PIT_ANNOUNCEMENT
    assert _decl(lake, "ext_valuation").usable is True
    assert _decl(lake, "ext_valuation").mode == dc.PIT_EVENT_DATE
    assert _decl(lake, "ext_bsdetail").usable is True

    forecast = _decl(lake, "ext_forecast")
    assert forecast.usable is False
    assert "报告期" in forecast.reason

    unlock = _decl(lake, "ext_unlock")
    assert unlock.usable is False
    assert unlock.known_at_column is None


def test_caveats_distinguish_snapshot_storage_and_restatement(lake):
    snapshot = _decl(lake, "ext_bsdetail")
    assert any("快照存储" in c for c in snapshot.caveats)
    metrics = _decl(lake, "financials_metrics")
    assert any("重述" in c for c in metrics.caveats)
    # 时序表不是快照存储，不该拿到那条 caveat。
    assert not any("快照存储" in c for c in _decl(lake, "ext_valuation").caveats)


def test_declare_all_is_sorted_and_matches_catalog(lake):
    decls = dp.declare_all(lake)
    assert [d.dataset_id for d in decls] == sorted(s.id for s in dc.build_catalog(lake))
    assert any(d.dataset_id == "ext_forecast" and not d.usable for d in decls)


# --- 取数口径 -------------------------------------------------------------


def test_announcement_is_strictly_t_plus_one(lake):
    # 公告日当天不算已知 —— 公告盘后发布, 当天回测用它就是未来信息。
    same_day = dp.known_as_of(lake, "financials_metrics", date(2026, 10, 23))
    assert same_day.rows == 0

    # 公告日次日: 只有 10-23 那一期可见 (10-30 那期还没公告)。
    next_day = dp.known_as_of(lake, "financials_metrics", date(2026, 10, 24))
    assert next_day.rows == 1
    assert next_day.frame["period_end"].to_list() == ["2026-09-30"]

    # 10-31 之后两期都可见。
    both = dp.known_as_of(lake, "financials_metrics", date(2026, 10, 31))
    assert both.rows == 2
    assert both.latest_known_at == date(2026, 10, 30)
    assert both.earliest_known_at == date(2026, 10, 23)


def test_announcement_null_known_at_is_dropped_and_counted(lake):
    result = dp.known_as_of(lake, "financials_metrics", date(2026, 11, 30))
    # 600002.SH 的公告日为 null: 既不算已知, 也不能当成"没公告"随便放行。
    assert result.dropped_unknown_known_at == 1
    assert "600002.SH" not in result.frame["symbol"].to_list()


def test_event_date_is_visible_on_the_same_day(lake):
    # 时序表的日期列本身就是可知日: 11-04 当天可以知道 11-04。
    as_of = dp.known_as_of(lake, "ext_valuation", D2)
    assert as_of.rows == 2
    assert as_of.latest_known_at == D2

    before = dp.known_as_of(lake, "ext_valuation", D1)
    assert before.rows == 1
    assert before.frame["amount"].to_list() == [3.0]


def test_hive_partition_date_is_backfilled_for_timeseries(lake):
    # ext_valuation 的 parquet 文件里没有 date 列, 日期只在 date=YYYY-MM-DD 目录名上。
    frame = dc.read_dataset(
        lake, dc.get_dataset(lake, "ext_valuation"), columns=["symbol", "amount", "date"]
    )
    assert frame.height == 3
    assert frame["date"].to_list() == [D1, D2, D3]


def test_ymd_string_known_at_is_parsed(lake):
    # ext_bsdetail 的 announce_date 是 "20261023" 这种 YYYYMMDD 字符串。
    result = dp.known_as_of(lake, "ext_bsdetail", date(2026, 10, 24))
    assert result.rows == 1
    assert result.frame["symbol"].to_list() == ["600001.SH"]

    later = dp.known_as_of(lake, "ext_bsdetail", date(2026, 11, 5))
    # 11-05 公告的那一行在 11-05 当天不可用（T+1）。
    assert later.frame["symbol"].to_list() == ["600001.SH"]


def test_symbols_and_columns_filter(lake):
    result = dp.known_as_of(
        lake, "financials_metrics", date(2026, 10, 31), symbols=["600002.SH"]
    )
    assert result.rows == 0  # 600002 的公告日为 null, 永远不可知

    narrowed = dp.known_as_of(
        lake, "financials_metrics", date(2026, 10, 31), columns=["eps_basic"]
    )
    assert "eps_basic" in narrowed.frame.columns
    assert "period_end" not in narrowed.frame.columns


# --- fail-closed ----------------------------------------------------------


def test_period_only_dataset_refuses(lake):
    with pytest.raises(dp.PitUnavailableError) as excinfo:
        dp.known_as_of(lake, "ext_forecast", D3)
    assert "报告期" in str(excinfo.value)


def test_dataset_without_date_column_refuses(lake):
    with pytest.raises(dp.PitUnavailableError):
        dp.known_as_of(lake, "ext_unlock", D3)


def test_unknown_dataset_refuses(lake):
    with pytest.raises(dp.PitUnavailableError) as excinfo:
        dp.known_as_of(lake, "no_such_table", D3)
    assert "未知数据集" in str(excinfo.value)


def test_declared_but_missing_known_at_column_refuses(lake):
    # 声明了 annch_date 但落盘没有该列 → 不能悄悄退回全表。
    decl = dp.PitDeclaration(
        dataset_id="financials_metrics",
        mode=dc.PIT_ANNOUNCEMENT,
        known_at_column="not_there",
        period_column="period_end",
        usable=True,
        reason="测试用",
    )
    with pytest.raises(dp.PitUnavailableError) as excinfo:
        dp.apply_known_as_of(
            pl.DataFrame({"symbol": ["600001.SH"], "v": [1.0]}), decl, D3
        )
    assert "没有该列" in str(excinfo.value)


def test_all_null_known_at_is_dropped_not_leaked(lake):
    # 单帧级别：可知日为空的行一律丢掉（放行才是前视偏差），不报错。
    decl = dp.PitDeclaration(
        dataset_id="financials_metrics",
        mode=dc.PIT_ANNOUNCEMENT,
        known_at_column="announce_date",
        period_column="period_end",
        usable=True,
        reason="测试用",
    )
    frame = pl.DataFrame({"symbol": ["600001.SH"], "announce_date": [None]})
    out = dp.apply_known_as_of(frame, decl, D3)
    assert out.is_empty()


def test_dataset_with_all_null_known_at_refuses(lake):
    # 数据集级别：整表的可知日全为空 → 无法判定「当时是否已知」，必须拒绝。
    with pytest.raises(dp.PitUnavailableError) as excinfo:
        dp.known_as_of(lake, "financials_shares", D3)
    assert "全为空" in str(excinfo.value)


def test_symbol_filter_with_no_known_row_returns_empty_not_error(lake):
    # 按标的过滤后子集恰好没有可知行，只应得到 0 行，不该让整个数据集报错。
    sl = dp.known_as_of(lake, "financials_metrics", D3, symbols=["600002.SH"])
    assert sl.rows == 0
    assert sl.dropped_unknown_known_at == 1


def test_apply_known_as_of_is_a_noop_on_empty_frame(lake):
    decl = _decl(lake, "ext_valuation")
    out = dp.apply_known_as_of(pl.DataFrame(), decl, D3)
    assert out.is_empty()


def test_backtest_guard_lists_every_bad_dataset(lake):
    good = dp.backtest_guard(lake, ["kline_daily", "financials_metrics", "ext_valuation"])
    assert {d.dataset_id for d in good} == {
        "kline_daily",
        "financials_metrics",
        "ext_valuation",
    }

    with pytest.raises(dp.PitUnavailableError) as excinfo:
        dp.backtest_guard(lake, ["kline_daily", "ext_forecast", "ext_unlock", "ghost"])
    message = str(excinfo.value)
    # 一次把所有问题列完, 而不是只报第一个。
    assert "ext_forecast" in message
    assert "ext_unlock" in message
    assert "ghost" in message


def test_pit_slice_to_dict_shape(lake):
    body = dp.known_as_of(lake, "ext_valuation", D3).to_dict(rows_limit=1)
    assert body["dataset_id"] == "ext_valuation"
    assert body["as_of"] == D3.isoformat()
    assert body["mode"] == dc.PIT_EVENT_DATE
    assert body["known_at_column"] == "date"
    assert body["rows"] == 3
    assert len(body["rows_preview"]) == 1
    assert body["latest_known_at"] == D3.isoformat()


# --- API ------------------------------------------------------------------


@pytest.fixture
def client(lake, monkeypatch):
    monkeypatch.setattr(datasets_api, "settings", SimpleNamespace(data_dir=lake))
    app = FastAPI()
    app.include_router(datasets_api.router)
    return TestClient(app)


def test_pit_endpoint_lists_declarations(client):
    resp = client.get("/api/datasets/pit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == len(body["items"])
    assert body["usable_count"] < body["count"]
    by_id = {it["dataset_id"]: it for it in body["items"]}
    assert by_id["ext_forecast"]["usable"] is False
    assert by_id["financials_metrics"]["known_at_column"] == "announce_date"


def test_pit_endpoint_usable_only_filter(client):
    resp = client.get("/api/datasets/pit", params={"usable_only": True})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(it["usable"] for it in items)


def test_pit_slice_endpoint(client):
    resp = client.get(
        "/api/datasets/financials_metrics/pit",
        params={"as_of": "2026-10-24", "rows_limit": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == 1
    assert body["caveats"]
    assert body["rows_preview"][0]["period_end"] == "2026-09-30"


def test_pit_slice_endpoint_rejects_unusable_and_bad_input(client):
    refused = client.get(
        "/api/datasets/ext_forecast/pit", params={"as_of": "2026-11-05"}
    )
    assert refused.status_code == 409
    assert "报告期" in refused.json()["detail"]

    unknown = client.get(
        "/api/datasets/no_such_table/pit", params={"as_of": "2026-11-05"}
    )
    assert unknown.status_code == 404

    bad_date = client.get(
        "/api/datasets/ext_valuation/pit", params={"as_of": "2026/11/05"}
    )
    assert bad_date.status_code == 400
