"""数据质量检查测试 — 用合成数据湖, 不依赖真实 data/。

覆盖六类检查各自的"该报"与"不该报":
  - 重复 / 停牌残留 / 跨源冲突 的检出
  - 异常跳变: 真跳变要报; **除权除息日要豁免**（用除权参考价当基准）;
    **未定版分区要跳过**（实时快照价格还在变）; enriched 要用 raw_close 而不是前复权 close
  - 跨源冲突落在未定版分区时降级为 low 并写明原因
  - 复权: 事件日在行情里没有行 / 因子与事件字段不自洽
  - 未声明唯一键的表记 limitation 而不是猜键
  - 分区缺失把 data_integrity 的 issue.kind 翻译成中文并映射严重度
  - /api/datasets/{id}/quality 的形状 / kind 过滤 / 非法 kind / 404
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import datasets as datasets_api
from app.services import data_quality as dq

CN_TZ = timezone(timedelta(hours=8))

D1 = date(2026, 11, 3)
D2 = date(2026, 11, 4)
D3 = date(2026, 11, 5)   # 已定版的最后一个完整交易日
D4 = date(2026, 11, 6)   # 盘中快照

DAYS = (D1, D2, D3, D4)

#: symbol -> {日期: 收盘价}。600002 是"真跳变"，600003 是"除权日假跳变"，
#: 600009 只在未定版的那天跳，600010 用来看 enriched 是否误用前复权 close。
CLOSES: dict[str, dict[date, float]] = {
    "600001.SH": {D1: 10.00, D2: 10.10, D3: 10.05, D4: 10.06},
    "600002.SH": {D1: 10.00, D2: 10.00, D3: 12.00, D4: 12.10},
    "600003.SH": {D1: 8.00, D2: 8.28, D3: 7.37, D4: 7.40},
    "600004.SH": {D1: 20.00, D2: 20.10, D3: 20.20, D4: 20.30},
    "600005.SH": {D1: 10.00, D2: 10.00, D3: 10.00, D4: 10.00},
    "600007.SH": {D1: 5.00, D2: 5.05, D3: 5.10, D4: 5.15},
    "600009.SH": {D1: 10.00, D2: 10.00, D3: 10.00, D4: 13.00},
    "600010.SH": {D1: 10.00, D2: 10.00, D3: 10.00, D4: 10.00},
}

#: ext_valuation 的 11-06 分区故意与行情差 2%（未定版日 → 冲突降级）
EXT_MISMATCH_REL = 0.02


def _ms(day: date, hour: int, minute: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ).timestamp() * 1000)


def _rows(table: str, day: date) -> list[dict[str, object]]:
    """按表构造当天的行。600004 在 D2 出两行（重复键），600005 在 D2 是停牌残留行。"""
    out: list[dict[str, object]] = []
    for symbol, series in CLOSES.items():
        if day not in series:
            continue
        close = series[day]
        row: dict[str, object] = {
            "symbol": symbol,
            "date": day,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
            "amount": close * 100_000,
            "raw_close": close,
            "raw_high": close,
            "raw_low": close,
        }
        if table == "kline_daily_enriched":
            row["turnover_rate"] = 1.0
            if symbol == "600010.SH" and day != D1:
                # 前复权 close 缩放到 1/10，raw_close 保持真实价。
                # 跳变检查若误用 close 会在 D2 报 -90%。
                row["close"] = close / 10.0
                row["open"] = close / 10.0
            if symbol == "600004.SH" and day == D2:
                row["raw_close"] = close + 1.0  # 跨源冲突（与 kline_daily.close 不等）
        if symbol == "600005.SH" and day == D2:
            # 停牌残留: 无集合竞价 → open/high 为 0，量额也为 0
            row.update({"open": 0.0, "high": 0.0, "volume": 0, "amount": 0.0})
        out.append(row)
    if table == "kline_daily" and day == D2:
        # 重复键: 同一 symbol+date 落两行
        twin = dict(next(r for r in out if r["symbol"] == "600004.SH"))
        twin["close"] = 20.15
        twin["raw_close"] = 20.15
        out.append(twin)
    return out


def _write_partition(root: Path, table: str, day: date, quote_ts: int | None) -> None:
    part = root / table / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    rows = _rows(table, day)
    if not rows:
        return
    df = pl.DataFrame(rows).with_columns(
        pl.Series("quote_ts", [quote_ts] * len(rows), dtype=pl.Int64)
    )
    df.write_parquet(part / "part.parquet")


def _write_ext_config(root: Path, table_id: str, field_names: tuple[str, ...]) -> Path:
    d = root / "ext_data" / table_id
    d.mkdir(parents=True, exist_ok=True)
    fields = [
        {"name": n, "dtype": "string" if n == "symbol" else "float", "label": n}
        for n in field_names
    ]
    cfg = {"id": table_id, "label": table_id, "mode": "timeseries", "fields": fields}
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def lake(tmp_path) -> Path:
    root = tmp_path / "data"
    root.mkdir()

    for day in DAYS:
        settled = day != D4
        for table in ("kline_daily", "kline_daily_enriched"):
            _write_partition(root, table, day, None if settled else _ms(day, 10, 30))

    # ETF 只到 D2，落后一个定版会话
    for day in (D1, D2):
        _write_partition(root, "kline_etf_daily", day, None)
        _write_partition(root, "kline_etf_enriched", day, None)

    (root / "instruments").mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": list(CLOSES),
            "name": [f"股票{i}" for i in range(len(CLOSES))],
            "listing_date": ["2015-06-01"] * len(CLOSES),
        }
    ).write_parquet(root / "instruments" / "instruments.parquet")

    (root / "adj_factor").mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600003.SH", "600006.SH", "600007.SH"],
            "trade_date": [D3, D2, D1],
            "ex_factor": [1.010989, 1.05, 2.0],
            "dividend": [0.089, 0.2, 1.0],
            "bonus": [0.0, 0.0, 0.0],
            "allot": [0.0, 0.0, 0.0],
            "allot_price": [0.0, 0.0, 0.0],
            "prev_close": [8.28, 10.0, 10.0],
        }
    ).write_parquet(root / "adj_factor" / "all.parquet")

    # ext_valuation: D1~D3 与行情一致；D4（未定版）偏 2%
    d = _write_ext_config(root, "ext_valuation", ("symbol", "date", "price"))
    for day in DAYS:
        part = d / "timeseries" / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        rows = []
        for symbol, series in CLOSES.items():
            if day not in series or symbol not in ("600001.SH", "600003.SH"):
                continue
            price = series[day]
            if day == D4:
                price = round(price * (1 + EXT_MISMATCH_REL), 2)
            rows.append({"symbol": symbol, "price": price})
        pl.DataFrame(rows).write_parquet(part / "part.parquet")

    # 未声明唯一键的表（一 symbol 合法多行）→ 只应记 limitation
    d = _write_ext_config(root, "ext_gn_ths", ("symbol", "所属概念"))
    pl.DataFrame({"symbol": ["600001.SH", "600001.SH"], "所属概念": ["A", "B"]}).write_parquet(
        d / "part.parquet"
    )

    return root


def _report(lake: Path, dataset_id: str, **kw) -> dq.QualityReport:
    spec = dq.get_dataset(lake, dataset_id)
    assert spec is not None, dataset_id
    return dq.check_dataset(lake, spec, **kw)


def _kinds(report: dq.QualityReport) -> set[str]:
    return {f.kind for f in report.findings}


def _finding(report: dq.QualityReport, kind: str, *scope_parts: str):
    for f in report.findings:
        if f.kind != kind:
            continue
        if all(p in f.scope or p in f.detail for p in scope_parts):
            return f
    return None


# --- 干净/重复/停牌 -------------------------------------------------------


def test_clean_symbols_produce_no_findings(lake):
    report = _report(lake, "ext_valuation")
    assert report.ok, [f.detail for f in report.findings]


def test_duplicate_keys_are_reported(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_DUPLICATE,))
    assert _kinds(report) == {dq.KIND_DUPLICATE}
    finding = _finding(report, dq.KIND_DUPLICATE)
    assert finding is not None
    assert "600004.SH" in finding.symbols
    assert finding.affected == 2
    assert report.counts["duplicate_keys_found"] == 1


def test_undeclared_key_columns_are_skipped_with_limitation(lake):
    report = _report(lake, "ext_gn_ths", kinds=(dq.KIND_DUPLICATE,))
    assert report.findings == ()
    assert any("未声明唯一键" in t for t in report.limitations)


def test_halt_residue_rows_are_reported(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_HALT,))
    finding = _finding(report, dq.KIND_HALT)
    assert finding is not None
    assert "600005.SH" in finding.symbols


# --- 异常跳变 -------------------------------------------------------------


def test_real_jump_is_reported(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_JUMP,))
    finding = _finding(report, dq.KIND_JUMP)
    assert finding is not None, report.findings
    assert "600002.SH" in finding.symbols
    assert finding.severity == dq.SEVERITY_HIGH
    assert str(D3) in finding.detail


def test_ex_dividend_day_is_exempt(lake):
    """002249.SZ 2026-10-30 那类假阳性: 原始昨收 8.28 → 看着 -10.99% 超限,
    但当日 10 派 0.089 元, 除权参考价 8.19、跌停价 7.37, 收盘 7.37 是正常跌停。"""
    report = _report(lake, "kline_daily", kinds=(dq.KIND_JUMP,))
    finding = _finding(report, dq.KIND_JUMP)
    assert finding is not None
    assert "600003.SH" not in finding.symbols


def test_same_row_is_reported_without_adj_factor(lake):
    """把复权事件拿掉, 同一行就会被判成超限 —— 证明豁免真的来自 adj_factor。"""
    (lake / "adj_factor" / "all.parquet").unlink()
    report = _report(lake, "kline_daily", kinds=(dq.KIND_JUMP,))
    finding = _finding(report, dq.KIND_JUMP)
    assert finding is not None
    assert "600003.SH" in finding.symbols
    assert any("adj_factor" in t and "原始昨收" in t for t in report.limitations)


def test_unfinalized_partition_is_skipped_for_jump(lake):
    """未定版分区价格还在变（实测盘中快照两次运行 close 从 3.84 变 3.69），
    拿它判跳变只会产噪音。"""
    report = _report(lake, "kline_daily", kinds=(dq.KIND_JUMP,))
    finding = _finding(report, dq.KIND_JUMP)
    assert finding is not None
    assert "600009.SH" not in finding.symbols
    assert any(D4.isoformat() in t and "未定版" in t for t in report.limitations)


def test_enriched_jump_uses_raw_close_not_adjusted_close(lake):
    report = _report(lake, "kline_daily_enriched", kinds=(dq.KIND_JUMP,))
    # 600010 的 close 是 1/10 的复权价, 用 close 判会在 D2 报 -90%
    finding = _finding(report, dq.KIND_JUMP)
    assert finding is None or "600010.SH" not in finding.symbols
    assert any("raw_close" in t for t in report.limitations)


# --- 跨源冲突 -------------------------------------------------------------


def test_conflict_on_settled_day_is_medium(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_CONFLICT,))
    finding = _finding(report, dq.KIND_CONFLICT, "kline_daily_enriched.raw_close")
    assert finding is not None, report.findings
    assert finding.severity == dq.SEVERITY_MEDIUM
    assert "600004.SH" in finding.symbols
    assert "尚未定版" not in finding.detail


def test_conflict_on_unfinalized_day_is_downgraded(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_CONFLICT,))
    finding = _finding(report, dq.KIND_CONFLICT, "ext_valuation.price")
    assert finding is not None, report.findings
    assert finding.severity == dq.SEVERITY_LOW
    assert "600001.SH" in finding.symbols
    assert D4.isoformat() in finding.detail
    assert "尚未定版" in finding.detail


def test_conflict_on_other_source_is_counted(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_CONFLICT,))
    assert report.counts["conflict_ext_valuation_compared"] == 8
    assert report.counts["conflict_ext_valuation_mismatch"] == 2


# --- 复权 -----------------------------------------------------------------


def test_adjust_event_without_price_row_is_reported(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_ADJUST,))
    finding = _finding(report, dq.KIND_ADJUST, "复权基准缺失")
    assert finding is not None, report.findings
    assert "600006.SH" in finding.symbols


def test_adjust_factor_inconsistent_with_event_fields(lake):
    report = _report(lake, "kline_daily", kinds=(dq.KIND_ADJUST,))
    finding = _finding(report, dq.KIND_ADJUST, "重算")
    assert finding is not None, report.findings
    assert "600007.SH" in finding.symbols
    assert report.counts["adjust_factors_scanned"] == 3


def test_theoretical_ex_factor_matches_stored_value_for_real_event(lake):
    theo = dq.theoretical_ex_factor(0.089, 0.0, 0.0, 0.0, 8.28)
    assert theo is not None
    assert abs(1.010989 / theo - 1.0) < dq._ADJUST_REL_TOLERANCE


# --- 分区缺失（把 data_integrity 的 issue.kind 翻译成中文） ----------------


def test_missing_check_translates_issue_kind_and_maps_severity(lake, monkeypatch):
    from app.services.data_integrity import IntegrityIssue

    def fake_scan(data_dir, *, lookback_days, tables):
        return [
            IntegrityIssue(day=D3, table="kline_daily", kind="snapshot", symbols=("600001.SH",)),
            IntegrityIssue(
                day=D2,
                table="kline_daily",
                kind="missing",
                symbols=(),
                reason="交易日日K缺失",
            ),
        ]

    monkeypatch.setattr(dq, "scan_recent_integrity", fake_scan)
    report = _report(lake, "kline_daily", kinds=(dq.KIND_MISSING,))
    kinds = [f.detail for f in report.findings]
    assert any("实时轮询写入" in d for d in kinds), kinds
    assert any("交易日日K缺失" in d for d in kinds), kinds
    snapshot = next(f for f in report.findings if f.day == D3)
    assert snapshot.severity == dq.SEVERITY_HIGH
    assert report.counts["missing_partitions"] == 2


def test_non_daily_price_table_skips_missing_check_with_limitation(lake):
    report = _report(lake, "ext_valuation", kinds=(dq.KIND_MISSING,))
    assert report.findings == ()
    assert any("只覆盖三张日频权威历史表" in t for t in report.limitations)


# --- 入口 -----------------------------------------------------------------


def test_kind_filter_limits_work_and_records_scanned_counts(lake):
    only_dup = _report(lake, "kline_daily", kinds=(dq.KIND_DUPLICATE,))
    assert _kinds(only_dup) == {dq.KIND_DUPLICATE}
    assert set(only_dup.counts) == {"duplicate_keys_scanned", "duplicate_keys_found"}

    everything = _report(lake, "kline_daily")
    assert {dq.KIND_DUPLICATE, dq.KIND_HALT, dq.KIND_ADJUST, dq.KIND_JUMP, dq.KIND_CONFLICT} <= _kinds(
        everything
    )
    assert everything.checked_days == tuple(d.isoformat() for d in DAYS)


def test_lookback_days_limits_the_window(lake):
    report = _report(lake, "kline_daily", lookback_days=2, kinds=(dq.KIND_JUMP,))
    assert report.checked_days == (D3.isoformat(), D4.isoformat())
    # 窗口首日也要有真实昨收 —— 所以跳变检查多读一个会话
    assert report.counts["jump_rows_scanned"] > 0


def test_report_to_dict_shape(lake):
    payload = _report(lake, "kline_daily").to_dict()
    assert payload["dataset_id"] == "kline_daily"
    assert payload["ok"] is False
    assert isinstance(payload["finding_counts"], dict)
    assert payload["checked_at"].startswith("2026-")
    assert payload["findings"] and all("detail" in f and "affected" in f for f in payload["findings"])


# --- API ------------------------------------------------------------------


@pytest.fixture
def client(lake, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(datasets_api, "settings", SimpleNamespace(data_dir=lake))
    app = FastAPI()
    app.include_router(datasets_api.router)
    return TestClient(app)


def test_quality_endpoint_shape(client):
    resp = client.get("/api/datasets/kline_daily/quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_id"] == "kline_daily"
    assert body["checked_days"] == [d.isoformat() for d in DAYS]
    assert body["finding_counts"].get(dq.KIND_JUMP, 0) == 1


def test_quality_endpoint_kind_filter(client):
    resp = client.get("/api/datasets/kline_daily/quality", params={"kind": "duplicate"})
    assert resp.status_code == 200
    body = resp.json()
    assert {f["kind"] for f in body["findings"]} == {"duplicate"}


def test_quality_endpoint_rejects_unknown_kind(client):
    resp = client.get("/api/datasets/kline_daily/quality", params={"kind": "no_such_kind"})
    assert resp.status_code == 400
    assert "no_such_kind" in resp.json()["detail"]


def test_quality_endpoint_unknown_dataset(client):
    assert client.get("/api/datasets/no_such_table/quality").status_code == 404
