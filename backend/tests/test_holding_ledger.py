"""手工持仓 append-only 台账的定向测试。"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.services import holding_ledger as ledger


def test_append_load_roundtrip_and_increasing_seq(tmp_path):
    first = ledger.append_event(
        data_dir=tmp_path,
        event={
            "op": "upsert",
            "symbol": "000001.SZ",
            "qty": 100,
            "avg_cost": 10.5,
            "entry_date": "2026-01-01",
            "note": "first",
            "ts": "2026-01-01T09:30:00",
        },
    )
    assert first["seq"] == 1
    assert first["event_id"]
    rows = ledger.load_events(data_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0] == first

    second = ledger.append_event(
        data_dir=tmp_path,
        event={
            "op": "delete",
            "symbol": "000001.SZ",
            "ts": "2026-01-02T09:30:00",
        },
    )
    assert second["seq"] == 2
    assert second["event_id"] != first["event_id"]

    rows2 = ledger.load_events(data_dir=tmp_path)
    assert [row["seq"] for row in rows2] == [1, 2]
    for row in rows2:
        expected = hashlib.sha256(
            f"{row['seq']}|{row['ts']}|{row['op']}|{row['symbol']}".encode()
        ).hexdigest()[:16]
        assert row["event_id"] == expected


def test_append_missing_fields_does_not_write(tmp_path):
    bad_missing_op = ledger.append_event(data_dir=tmp_path, event={"symbol": "A"})
    assert bad_missing_op["recorded"] is False
    assert isinstance(bad_missing_op["reason"], str) and bad_missing_op["reason"]

    path = tmp_path / "manual_holdings_ledger" / "events.jsonl"
    assert not path.exists()

    bad_missing_symbol = ledger.append_event(data_dir=tmp_path, event={"op": "upsert"})
    assert bad_missing_symbol["recorded"] is False
    assert not path.exists()


def test_replay_positions_latest_wins_and_delete():
    events = [
        {
            "seq": 1, "ts": "t1", "op": "upsert", "symbol": "A",
            "qty": 100, "avg_cost": 10, "entry_date": "d1", "note": "a1",
        },
        {
            "seq": 2, "ts": "t2", "op": "upsert", "symbol": "A",
            "qty": 200, "avg_cost": 11, "entry_date": "d2", "note": "a2",
        },
        {
            "seq": 3, "ts": "t3", "op": "upsert", "symbol": "B",
            "qty": 50, "avg_cost": 20, "entry_date": "d3", "note": "b",
        },
        {"seq": 4, "ts": "t4", "op": "delete", "symbol": "A"},
    ]
    result = ledger.replay_positions(events=events)
    assert set(result.keys()) == {"B"}
    assert result["B"] == {
        "qty": 50, "avg_cost": 20, "entry_date": "d3", "note": "b",
    }
    assert result.limitations == []
    assert result["limitations"] == []


def test_version_history_filters_symbol_and_sorts():
    events = [
        {"seq": 5, "ts": "t5", "op": "upsert", "symbol": "B", "qty": 1, "avg_cost": 2},
        {"seq": 2, "ts": "t2", "op": "upsert", "symbol": "A", "qty": 100, "avg_cost": 10},
        {"seq": 1, "ts": "t1", "op": "cash", "amount": 1000},
        {"seq": 4, "ts": "t4", "op": "delete", "symbol": "A"},
        {"seq": 3, "ts": "t3", "op": "dividend", "symbol": "A", "amount": 5},
    ]
    history = ledger.version_history(events=events, symbol="A")
    assert [row["seq"] for row in history] == [2, 3, 4]
    assert all(row["symbol"] == "A" for row in history)
    assert [row["op"] for row in history] == ["upsert", "dividend", "delete"]


def test_load_events_skips_bad_lines(tmp_path):
    path = tmp_path / "manual_holdings_ledger" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    good1 = {
        "seq": 1, "event_id": "x1", "ts": "t1", "op": "upsert",
        "symbol": "A", "qty": 1, "avg_cost": 1,
    }
    good2 = {"seq": 3, "event_id": "x3", "ts": "t3", "op": "delete", "symbol": "A"}
    lines = [
        json.dumps(good1, ensure_ascii=False),
        "{broken json",
        json.dumps([1, 2, 3]),
        "",
        json.dumps(good2, ensure_ascii=False),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = ledger.load_events(data_dir=tmp_path)
    assert [row["seq"] for row in rows] == [1, 3]
    assert rows.limitations
    assert any("坏行" in text for text in rows.limitations)


def test_load_events_symbol_filter_excludes_global(tmp_path):
    # append_event 严格校验 symbol；无 symbol 的全局事件用直接落盘模拟其它写入方。
    path = tmp_path / "manual_holdings_ledger" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    global_cash = {
        "seq": 1, "event_id": "g1", "ts": "t1", "op": "cash", "amount": 1000,
    }
    symbol_event = {
        "seq": 2, "event_id": "a2", "ts": "t2", "op": "upsert",
        "symbol": "A", "qty": 1, "avg_cost": 1, "entry_date": "d1", "note": "",
    }
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in (global_cash, symbol_event)) + "\n",
        encoding="utf-8",
    )
    all_rows = ledger.load_events(data_dir=tmp_path)
    assert len(all_rows) == 2
    filtered = ledger.load_events(data_dir=tmp_path, symbol="A")
    assert [row["op"] for row in filtered] == ["upsert"]
    assert all(row.get("symbol") == "A" for row in filtered)


def test_append_event_requires_symbol_even_for_cash(tmp_path):
    rejected = ledger.append_event(
        data_dir=tmp_path,
        event={"op": "cash", "amount": 1000, "ts": "t1"},
    )
    assert rejected["recorded"] is False
    assert "symbol" in rejected["reason"]
    path = tmp_path / "manual_holdings_ledger" / "events.jsonl"
    assert not path.exists()


def test_twr_mwr_three_periods_with_one_inflow():
    valuations = [
        {"date": "2026-01-01", "equity": 100},
        {"date": "2026-01-02", "equity": 120},
        {"date": "2026-01-03", "equity": 150},
    ]
    cashflows = [{"date": "2026-01-02", "amount": 20}]
    result = ledger.twr_mwr(valuations=valuations, cashflows=cashflows)
    assert result["twr"] == pytest.approx(0.25)
    assert len(result["periods"]) == 2
    assert result["periods"][0]["r"] == pytest.approx(0.0)
    assert result["periods"][1]["r"] == pytest.approx(0.25)
    assert isinstance(result["mwr"], float) or result["mwr"] is None


def test_twr_mwr_too_few_valuations():
    result = ledger.twr_mwr(
        valuations=[{"date": "2026-01-01", "equity": 100}],
        cashflows=[],
    )
    assert result["twr"] is None
    assert result["mwr"] is None
    assert result["periods"] == []
    assert result["limitations"]


def test_replay_undo_snapshot_and_limitation():
    events = [
        {
            "seq": 1, "ts": "t1", "op": "upsert", "symbol": "A",
            "qty": 100, "avg_cost": 10, "entry_date": "d1", "note": "a",
        },
        {
            "seq": 2, "ts": "t2", "op": "upsert", "symbol": "B",
            "qty": 50, "avg_cost": 20, "entry_date": "d2", "note": "b",
        },
        {"seq": 3, "ts": "t3", "op": "undo", "symbol": "A", "ref_seq": 1},
    ]
    result = ledger.replay_positions(events=events)
    assert "A" not in result
    assert result["B"]["qty"] == 50
    assert any("undo" in text for text in result.limitations)
    assert result["limitations"] == result.limitations

    events_with_snapshot = [
        *events[:2],
        {
            "seq": 3, "ts": "t3", "op": "undo", "symbol": "A", "ref_seq": 1,
            "snapshot": {
                "positions": {
                    "A": {
                        "qty": 80, "avg_cost": 9,
                        "entry_date": "d0", "note": "restored",
                    },
                },
            },
        },
    ]
    restored = ledger.replay_positions(events=events_with_snapshot)
    assert restored["A"] == {
        "qty": 80, "avg_cost": 9, "entry_date": "d0", "note": "restored",
    }
    assert restored["B"]["qty"] == 50
    assert restored.limitations == []
