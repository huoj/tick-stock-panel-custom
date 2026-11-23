"""手工录入真实持仓 —— 让不玩模拟盘的人也能做组合分析。

守的三条边界（见 services/manual_holdings.py）：
1. 不假装与券商对账过（口径标签 + limitations 必须写明）；
2. 现金没录就按 0 处理并说明「权重合计会等于 1」，不悄悄当成满仓事实；
3. 不做 T+1 推断 —— 没有流水就算不出可用数量，如实给 None。
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.services import manual_holdings as mh
from app.services import portfolio_positions as pp

SYM = "600519.SH"


def _seed(tmp_path, *, cash=None, holdings=None):
    if cash is not None:
        mh.save_account(tmp_path, cash=cash)
    for h in holdings or []:
        mh.save_one(tmp_path, h)


def _h(symbol=SYM, qty=100, cost=10.0, entry="2026-10-01", **over):
    return {"symbol": symbol, "asset_type": "stock", "qty": qty, "avg_cost": cost,
            "entry_date": entry, **over}


# ── 存储与校验 ──────────────────────────────────────────

def test_save_and_load_roundtrip(tmp_path):
    mh.save_one(tmp_path, _h())
    rows = mh.load_all(tmp_path)
    assert SYM in rows
    assert rows[SYM]["qty"] == 100
    assert rows[SYM]["avg_cost"] == 10.0
    assert rows[SYM]["entry_date"] == "2026-10-01"
    # 落盘文件名必须是 holding_<symbol>.json，否则 load_all 的 glob 找不到
    assert (tmp_path / "user_data" / "manual_holdings" / f"holding_{SYM}.json").exists()


def test_save_is_upsert_and_keeps_created_at(tmp_path):
    first = mh.save_one(tmp_path, _h(qty=100))
    second = mh.save_one(tmp_path, _h(qty=300))
    assert second["qty"] == 300
    assert second["created_at"] == first["created_at"]
    assert len(mh.load_all(tmp_path)) == 1


def test_delete_removes_file(tmp_path):
    mh.save_one(tmp_path, _h())
    assert mh.delete_one(tmp_path, SYM) is True
    assert mh.load_all(tmp_path) == {}
    assert mh.delete_one(tmp_path, SYM) is False


@pytest.mark.parametrize("qty,msg", [
    (0, "大于 0"),
    (-100, "大于 0"),
    ("100", "必须是数字"),
    (True, "必须是数字"),
    (1_000_000_001, "上限"),
])
def test_qty_validation(tmp_path, qty, msg):
    with pytest.raises(ValueError, match=msg):
        mh.save_one(tmp_path, _h(qty=qty))


def test_stock_qty_must_be_integer(tmp_path):
    with pytest.raises(ValueError, match="整数股"):
        mh.save_one(tmp_path, _h(qty=100.5))


@pytest.mark.parametrize("cost,msg", [
    (0, "不能为 0"),
    ("10", "必须是数字"),
    (200_000.0, "上限"),
    (-200_000.0, "上限"),   # 绝对值上限同时挡住负数, 防止多打几个 0
])
def test_cost_validation(tmp_path, cost, msg):
    with pytest.raises(ValueError, match=msg):
        mh.save_one(tmp_path, _h(avg_cost=cost))


@pytest.mark.parametrize("cost", [-1, -5.2, -100.0])
def test_negative_cost_is_allowed(tmp_path, cost):
    """反复做 T 把成本摊薄成负数，券商 App 里就是这个数 —— 照实录，不再挡。"""
    mh.save_one(tmp_path, _h(avg_cost=cost))
    assert mh.load_all(tmp_path)[SYM]["avg_cost"] == cost


def test_negative_cost_pnl_and_limitations(tmp_path):
    """负成本: 盈亏金额照算、收益率% 不适用(None)、权重按市值照算、口径说明写明。"""
    _seed(tmp_path, cash=0.0, holdings=[_h(qty=100, cost=-5.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})
    p = out["positions"][0]
    assert p["avg_cost"] == -5.0
    assert p["pnl"] == 1500.0                        # (10 - (-5)) * 100
    assert p["pnl_pct"] is None                      # 分母为负，收益率不适用
    assert p["weight"] == pytest.approx(1.0)         # 权重按市值算，与成本无关
    assert any("成本为负" in t for t in out["limitations"])


def test_entry_date_format_is_checked(tmp_path):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        mh.save_one(tmp_path, _h(entry="2026/10/01"))


def test_unsupported_asset_type_rejected(tmp_path):
    with pytest.raises(ValueError, match="asset_type"):
        mh.save_one(tmp_path, _h(asset_type="index"))


# ── 账户现金 ────────────────────────────────────────────

def test_account_defaults_to_none_when_missing(tmp_path):
    assert mh.load_account(tmp_path)["cash"] is None


def test_account_corrupt_file_degrades(tmp_path):
    path = tmp_path / "user_data" / "manual_holdings" / "account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert mh.load_account(tmp_path)["cash"] is None


def test_account_rejects_negative_cash(tmp_path):
    with pytest.raises(ValueError, match="不能为负"):
        mh.save_account(tmp_path, cash=-1)


# ── 口径与权重 ──────────────────────────────────────────

def test_manual_basis_has_no_available_qty(tmp_path):
    """没有成交流水就算不出 T+1 可用量 —— 如实给 None，不拿总数量冒充。"""
    _seed(tmp_path, cash=0.0, holdings=[_h(qty=100)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 12.0})

    assert out["basis"] == pp.MANUAL_BASIS
    assert out["basis_label"] == pp.BASIS_LABELS[pp.MANUAL_BASIS]
    p = out["positions"][0]
    assert p["available_qty"] is None
    assert p["priced"] is True
    assert p["market_value"] == 1200.0
    assert p["pnl"] == 200.0
    assert p["pnl_pct"] == 20.0


def test_manual_basis_says_not_reconciled_with_broker(tmp_path):
    _seed(tmp_path, cash=0.0, holdings=[_h()])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})
    assert any("未与券商对账" in t for t in out["limitations"])


def test_missing_cash_is_disclosed_not_silently_full(tmp_path):
    """现金没录 → 权重合计等于 1，必须明说"看起来像满仓"。"""
    _seed(tmp_path, holdings=[_h(qty=100, cost=10.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})

    assert out["cash"] == 0.0
    assert out["positions"][0]["weight"] == pytest.approx(1.0)
    assert any("看起来像满仓" in t for t in out["limitations"])


def test_cash_lowers_weight_denominator(tmp_path):
    _seed(tmp_path, cash=1000.0, holdings=[_h(qty=100, cost=10.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})

    assert out["total"] == 2000.0
    assert out["positions"][0]["weight"] == pytest.approx(0.5)
    assert not any("看起来像满仓" in t for t in out["limitations"])


def test_holding_days_use_entry_date(tmp_path):
    _seed(tmp_path, cash=0.0, holdings=[_h(entry="2026-10-01")])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})
    expected = (dt.date.today() - dt.date(2026, 10, 1)).days
    assert out["positions"][0]["holding_days"] == expected


def test_missing_entry_date_gives_none_and_limitation(tmp_path):
    _seed(tmp_path, cash=0.0, holdings=[_h(entry=None)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})
    assert out["positions"][0]["holding_days"] is None
    assert any("没有填建仓日期" in t for t in out["limitations"])


def test_unpriced_holding_falls_back_to_cost_and_says_so(tmp_path):
    _seed(tmp_path, cash=0.0, holdings=[_h(qty=100, cost=10.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={})
    p = out["positions"][0]
    assert out["price_source"] == "cost"
    assert p["priced"] is False
    assert p["pnl"] == 0.0
    assert any("按成本估算" in t for t in out["limitations"])


def test_empty_manual_store_is_not_initialized(tmp_path):
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL)
    assert out["initialized"] is False
    assert out["positions"] == []


def test_manual_reads_daily_close_when_price_map_absent(tmp_path):
    """不给 price_map 时退回日线收盘（用真实 parquet 形状，不 mock paper）。"""
    import polars as pl

    day_dir = tmp_path / "kline_daily" / "date=2026-11-13"
    day_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": [SYM],
        "date": [dt.date(2026, 11, 13)],
        "close": [12.5],
    }).write_parquet(day_dir / "part-000.parquet")

    _seed(tmp_path, cash=0.0, holdings=[_h(qty=100, cost=10.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL)

    assert out["price_source"] == "daily"
    assert out["positions"][0]["last_price"] == 12.5
    assert out["positions"][0]["pnl"] == 250.0


# ── auto 优先级 ─────────────────────────────────────────

def test_auto_prefers_manual_over_paper(tmp_path):
    """手工录的是真钱，模拟盘是演习 —— 两者都在时按真钱算，并说明忽略了模拟盘。"""
    from app.strategy import paper

    paper.create_account(tmp_path, 100_000)
    acc = paper.get_account(tmp_path, paper.DEFAULT_ACCOUNT_ID)
    acc["cash"] = 100_000
    paper.save_account(tmp_path, acc, paper.DEFAULT_ACCOUNT_ID)
    root = paper._root(tmp_path, paper.DEFAULT_ACCOUNT_ID)
    (root / "positions.json").write_text(
        json.dumps({"positions": {"000001.SZ": {"asset_type": "stock", "qty": 100,
                                                "avg_cost": 10.0, "available_qty": 100,
                                                "lots": []}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    # 没有手工持仓时走模拟盘
    assert pp.load_actual_positions(tmp_path, price_map={})["basis"] == pp.ACTUAL_BASIS

    _seed(tmp_path, holdings=[_h()])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_AUTO, price_map={SYM: 10.0})
    assert out["basis"] == pp.MANUAL_BASIS
    assert any("未使用模拟盘账户" in t for t in out["limitations"])

    # 显式要模拟盘时仍可拿到
    explicit = pp.load_actual_positions(tmp_path, source=pp.SOURCE_PAPER, price_map={})
    assert explicit["basis"] == pp.ACTUAL_BASIS


def test_unknown_source_rejected(tmp_path):
    with pytest.raises(ValueError, match="source 只支持"):
        pp.load_actual_positions(tmp_path, source="broker")


def test_holdings_for_risk_feeds_weights(tmp_path):
    _seed(tmp_path, cash=1000.0, holdings=[_h(qty=100, cost=10.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})
    fed = pp.holdings_for_risk(out)
    assert fed == [{"symbol": SYM, "weight": pytest.approx(0.5)}]


# ── 回归：NaN / ±Inf 不能穿过校验（见 validate_holding / save_account） ──

@pytest.mark.parametrize("qty", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_qty_rejected_with_chinese_reason(tmp_path, qty):
    """NaN/Inf 数量必须报中文原因。

    回归 bug：`qty=NaN` 时 `qty <= 0` 与 `qty > MAX_QTY` 都是 False，
    会落到 `float(qty) != int(qty)`，抛英文 `cannot convert float NaN to
    integer` —— 违背「校验失败给中文原因」契约。
    """
    with pytest.raises(ValueError, match="有效数字"):
        mh.save_one(tmp_path, _h(qty=qty))


@pytest.mark.parametrize("cash", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_cash_rejected(tmp_path, cash):
    """回归 bug：`cash=NaN` 时 `isinstance`/`< 0`/`> MAX_CASH` 三关全过，
    json.dumps 默认允许 NaN，会落盘成 `{"cash": NaN}` 这种非法 JSON。"""
    with pytest.raises(ValueError, match="有效数字"):
        mh.save_account(tmp_path, cash=cash)


def test_account_legacy_nan_file_self_heals(tmp_path):
    """旧版本可能已经把 `{"cash": NaN}` 写进文件；读回时按「未录入」处理并告警，
    不让前端 JSON.parse 因为一个 NaN 整页崩掉。"""
    path = tmp_path / "user_data" / "manual_holdings" / "account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"cash": NaN}', encoding="utf-8")
    assert mh.load_account(tmp_path)["cash"] is None


# ── 回归：负成本 + 取不到行情时不能算出负市值 ──────────────

def test_negative_cost_without_quote_does_not_go_negative(tmp_path):
    """回归 bug：`last = avg_cost` 在负成本（做 T 摊薄）下会把 -5.2 当现价，
    市值 = 100 × -5.2 = -520，总权益变负、权重变 None。负成本不能当现价。"""
    _seed(tmp_path, cash=0.0, holdings=[_h(qty=100, cost=-5.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={})
    p = out["positions"][0]

    assert p["market_value"] == 0.0
    assert p["last_price"] == 0.0
    assert p["pnl"] == 0.0          # 没有行情就算不出盈亏，不能凭空变出 500
    assert out["market_value"] == 0.0
    assert out["total"] >= 0.0
    assert any("负成本" in t for t in out["limitations"])


def test_negative_cost_with_quote_still_prices_correctly(tmp_path):
    """负成本但**有**行情时，盈亏金额照常算（做 T 摊薄后的真实收益）。"""
    _seed(tmp_path, cash=0.0, holdings=[_h(qty=100, cost=-5.0)])
    out = pp.load_actual_positions(tmp_path, source=pp.SOURCE_MANUAL, price_map={SYM: 10.0})
    p = out["positions"][0]

    assert p["last_price"] == 10.0
    assert p["market_value"] == 1000.0
    assert p["pnl"] == 1500.0        # (10 - (-5)) × 100
    assert p["pnl_pct"] is None      # 分母为负，收益率不适用


# ── 回归：/ledger 端点在台账有事件时不能 500 ────────────────

def test_ledger_endpoint_survives_upsert_and_delete(tmp_path, monkeypatch):
    """回归 bug：`ledger_summary` 迭代 `replay_positions()` 返回的 dict 拿到的是
    symbol 字符串，`row.get("qty")` 抛 AttributeError → 有任何 upsert 事件就 500。
    这里同时覆盖 upsert → delete：清仓后累计成本必须回落到 0。"""
    from types import SimpleNamespace

    from app.api import manual_holdings as api
    from app.services import holding_ledger

    monkeypatch.setattr(api, "_data_dir", lambda request: tmp_path)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    mh.save_one(tmp_path, _h(qty=100, cost=10.0))
    holding_ledger.append_event(
        data_dir=tmp_path,
        event={"op": "upsert", "symbol": SYM, "qty": 100, "avg_cost": 10.0},
    )
    out = api.ledger_summary(request)
    assert out["event_count"] == 1
    assert SYM in out["positions"]

    mh.delete_one(tmp_path, SYM)
    holding_ledger.append_event(
        data_dir=tmp_path, event={"op": "delete", "symbol": SYM}
    )
    out2 = api.ledger_summary(request)
    assert out2["event_count"] == 2
    assert SYM not in out2["positions"]                 # 清仓后回放为空
    # delete 事件不带 qty/avg_cost，也必须触发「按前缀重算」：
    # 否则累计成本会永远停在建仓时的 1000，估值序列不会回落。
    equities = [p["equity"] for p in out2["twr_mwr"].get("periods") or []]
    assert equities and all(e == 0.0 for e in equities)
