"""热股风向标 (平安 skill=hot-stock) 测试。

覆盖四层:
  1. 参数校验 —— 协议对非法取值**不报错**而是静默返回空榜单, 只能自己拦
  2. 行归一 —— chg 小数→百分数 / tagList 去重 / news 空串 / 板块号 / 未知市场跳过
  3. 取数 —— payload 形态 (time_range 必须 int) / 单类失败不拖垮整表
  4. 预设与接线 —— 一榜一张表 / adapter 配方 / 独立播种标记 / 路由注册
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

import pytest

from app.services.ext_data import PLATE_TS_FIELD, ExtConfig, ExtConfigStore, PullConfig
from app.services.hot_stock import (
    ADAPTER_KIND,
    ASSET_KINDS,
    BOARDS,
    DEFAULT_SIZE,
    DEFAULT_TIME_RANGE,
    HOT_STOCK_FIELDS,
    INTRADAY_INTERVAL_MIN,
    INTRADAY_TIME_FIELD,
    INTRADAY_WINDOW_END,
    INTRADAY_WINDOW_START,
    LANDED_ASSET_KINDS,
    PRESET_IDS,
    _needs_intraday_upgrade,
    collect_boards,
    ensure_preset,
    fetch_board,
    get_preset,
    live,
    normalize_asset_kind,
    normalize_board,
    normalize_rows,
    normalize_size,
    normalize_time_range,
    pa_symbol,
    preset,
    to_ext_row,
)

DAY = date(2026, 11, 19)

# 真实响应样例行 (2026-11-19 实测 热搜榜/aStock), 保留原始形态:
# chg 是小数字符串, cnt 是整数, calcDate 是 yyyymmddhh 数字
RAW_STOCK = {
    "avgPrice": "",
    "calcDate": 2026111906,
    "chg": "-0.0999",
    "cnt": 66121,
    "firmVolume": 0,
    "fusion": False,
    "marketType": "SZ",
    "name": "九阳股份",
    "news": {"newsUrl": "", "title": ""},
    "stockCode": "002242",
    "tagList": [{"name": "首板", "type": 1, "url": ""}],
    "updateTime": "2026-11-19 06:00:00",
}

RAW_STOCK_5TAGS = {
    "chg": "0.1001",
    "cnt": 27068,
    "marketType": "SH",
    "name": "华胜天成",
    "news": {
        "newsUrl": "https://stock.pingan.com/a/1",
        "title": "常山北明11月18日市盈率（TTM）583.38倍，历史分位87.4%",
    },
    "stockCode": "600410",
    "tagList": [
        {"name": "智能家居", "type": 2, "url": ""},
        {"name": "机器人概念", "type": 2, "url": ""},
        {"name": "智能家居", "type": 2, "url": ""},   # 重复, 应去重
        {"name": "20日强势", "type": 3, "url": ""},
        {"name": "", "type": ""},
    ],
    "updateTime": "2026-11-19 06:00:00",
}

RAW_ETF = {
    "chg": "0.0135",
    "cnt": 23996,
    "marketType": "SH",
    "name": "黄金ETF华安",
    "news": {"newsUrl": "", "title": ""},
    "stockCode": "518880",
    "tagList": [{"name": "份额117.35亿", "type": "", "url": ""}],
    "updateTime": "2026-11-19 07:00:00",
}

RAW_BLOCK = {
    "chg": "0.0231",
    "cnt": 15200,
    "marketType": "BI",
    "name": "算力概念",
    "news": {"newsUrl": "", "title": ""},
    "stockCode": "994247",
    "tagList": [],
    "updateTime": "2026-11-19 06:00:00",
}


# ---------------------------------------------------------------------------
# 1. 参数校验 (协议静默失败, 必须前置拦)
# ---------------------------------------------------------------------------

def test_normalize_board_accepts_all_four() -> None:
    for board in ("hot_stock", "hot_select", "hot_watch", "hot_surge"):
        assert normalize_board(board) == board


def test_normalize_board_rejects_unknown_with_choices() -> None:
    with pytest.raises(ValueError, match="hot_stock"):
        normalize_board("hot_nope")


def test_normalize_asset_kind_accepts_all_five() -> None:
    for kind in ("aStock", "etf", "block", "hkStock", "bond"):
        assert normalize_asset_kind(kind) == kind


def test_normalize_asset_kind_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="aStock"):
        normalize_asset_kind("stock")


def test_normalize_time_range_accepts_int_and_readable() -> None:
    assert normalize_time_range(1) == 1
    assert normalize_time_range(2) == 2
    assert normalize_time_range("1h") == 1
    assert normalize_time_range("1") == 1
    assert normalize_time_range("1小时") == 1
    assert normalize_time_range("24h") == 2
    assert normalize_time_range("1d") == 2
    assert normalize_time_range("24小时") == 2


def test_normalize_time_range_defaults_when_missing() -> None:
    assert normalize_time_range(None) == DEFAULT_TIME_RANGE
    assert normalize_time_range("") == DEFAULT_TIME_RANGE


def test_normalize_time_range_rejects_silent_empty_trap() -> None:
    """源对 time_range=3 返回空榜单而非报错, 所以这里必须自己拦。"""
    with pytest.raises(ValueError, match="时间区间"):
        normalize_time_range(3)
    with pytest.raises(ValueError, match="时间区间"):
        normalize_time_range("7d")


def test_normalize_size_bounds() -> None:
    assert normalize_size(None) == DEFAULT_SIZE
    assert normalize_size(1) == 1
    assert normalize_size("100") == 100
    with pytest.raises(ValueError, match="1\\.\\.100"):
        normalize_size(101)
    with pytest.raises(ValueError, match="1\\.\\.100"):
        normalize_size(0)
    with pytest.raises(ValueError, match="整数"):
        normalize_size("abc")


# ---------------------------------------------------------------------------
# 2. 行归一
# ---------------------------------------------------------------------------

def test_pa_symbol_maps_each_market() -> None:
    assert pa_symbol("SZ", "002242") == "002242.SZ"
    assert pa_symbol("SH", "600410") == "600410.SH"
    assert pa_symbol("BJ", "430047") == "430047.BJ"
    assert pa_symbol("HK", "00700") == "00700.HK"
    assert pa_symbol("BI", "994247") == "994247.BI"
    assert pa_symbol("sz", "002242") == "002242.SZ"


def test_pa_symbol_skips_unknown_market_and_missing_code() -> None:
    assert pa_symbol("US", "AAPL") is None
    assert pa_symbol("", "002242") is None
    assert pa_symbol("SZ", "") is None
    assert pa_symbol("SZ", None) is None


def test_to_ext_row_converts_decimal_chg_to_percent() -> None:
    """chg 是小数 (-0.0999 = -9.99%), 与 TSP 其它涨跌幅列一样存百分数。"""
    row = to_ext_row(RAW_STOCK, "aStock", 1, "2026-11-19")
    assert row is not None
    assert row["chg"] == pytest.approx(-9.99)
    assert row["hot_value"] == 66121
    assert row["rank"] == 1
    assert row["symbol"] == "002242.SZ"
    assert row["asset_kind"] == "aStock"
    assert row["name"] == "九阳股份"
    assert row["market_type"] == "SZ"
    assert row["source_date"] == "2026-11-19"
    assert row["updated_at"] == "2026-11-19 06:00:00"
    assert row["calc_date"] == "2026111906"


def test_to_ext_row_dedupes_tags_and_keeps_news() -> None:
    row = to_ext_row(RAW_STOCK_5TAGS, "aStock", 2, "2026-11-19")
    assert row is not None
    assert row["tags"] == "智能家居|机器人概念|20日强势", row["tags"]
    assert row["news_title"].startswith("常山北明11月18日市盈率")
    assert row["news_url"] == "https://stock.pingan.com/a/1"
    assert row["chg"] == pytest.approx(10.01)


def test_to_ext_row_empty_news_becomes_empty_string() -> None:
    row = to_ext_row(RAW_STOCK, "aStock", 1)
    assert row is not None
    assert row["news_title"] == ""
    assert row["news_url"] == ""


def test_to_ext_row_keeps_block_rows_with_bi_market() -> None:
    """板块榜的 marketType 是 BI, 代码是板块号 —— 保留但标清 market_type。"""
    row = to_ext_row(RAW_BLOCK, "block", 1, "2026-11-19")
    assert row is not None
    assert row["symbol"] == "994247.BI"
    assert row["market_type"] == "BI"
    assert row["tags"] == ""


def test_to_ext_row_returns_none_for_unusable_rows() -> None:
    assert to_ext_row({"marketType": "US", "stockCode": "AAPL"}, "aStock", 1) is None
    assert to_ext_row({"marketType": "SZ"}, "aStock", 1) is None
    assert to_ext_row("not-a-dict", "aStock", 1) is None  # type: ignore[arg-type]


def test_to_ext_row_handles_missing_numeric_fields() -> None:
    row = to_ext_row(
        {"marketType": "SZ", "stockCode": "002242", "cnt": "", "chg": "-"},
        "aStock", 3, "2026-11-19",
    )
    assert row is not None
    assert row["hot_value"] is None
    assert row["chg"] is None, "占位符不能被当成 0"


def test_normalize_rows_numbers_ranks_and_skips_bad_rows() -> None:
    meta = {"update_time": "2026-11-19 06:00:00"}
    rows = normalize_rows(
        [RAW_STOCK, {"marketType": "US", "stockCode": "AAPL"}, RAW_STOCK_5TAGS],
        "aStock", meta,
    )
    assert [r["symbol"] for r in rows] == ["002242.SZ", "600410.SH"]
    assert [r["rank"] for r in rows] == [1, 3], "rank 用原始序位, 不能因跳过而压缩"
    assert all(r["source_date"] == "2026-11-19" for r in rows)


def test_normalize_rows_source_date_falls_back_to_update_time() -> None:
    rows = normalize_rows([RAW_ETF], "etf", {})
    assert rows[0]["source_date"] == "2026-11-19"


def test_field_spec_covers_every_normalized_key() -> None:
    row = to_ext_row(RAW_STOCK_5TAGS, "aStock", 1, "2026-11-19")
    assert row is not None
    assert {name for name, _, _ in HOT_STOCK_FIELDS} == set(row)


# ---------------------------------------------------------------------------
# 3. 取数
# ---------------------------------------------------------------------------

class _FakeClient:
    """记录调用并回放固定响应; time_range 非 int 时按真源行为报 400。"""

    def __init__(self, rows=None, update_time="2026-11-19 06:00:00", fail=()):
        self.calls: list[dict] = []
        self._rows = [RAW_STOCK, RAW_STOCK_5TAGS] if rows is None else rows
        self._update_time = update_time
        self._fail = set(fail)

    def hot_stock_list(self, payload: dict):
        if not isinstance(payload.get("time_range"), int):
            raise AssertionError(f"time_range 必须是整数: {payload['time_range']!r}")
        kind = payload.get("type_code")
        if kind in self._fail:
            raise RuntimeError(f"boom {kind}")
        if kind == "etf":
            return [RAW_ETF], {
                "update_time": self._update_time, "total": 100, "page": 1, "size": 100,
            }
        self.calls.append(dict(payload))
        return list(self._rows), {
            "update_time": self._update_time, "total": 100, "page": 1, "size": 100,
        }


def test_fetch_board_sends_expected_payload() -> None:
    client = _FakeClient()
    rows, meta = fetch_board(client, "hot_stock", "aStock", time_range="24h", size=50)
    assert client.calls[0] == {
        "module_code": "hot_stock",
        "type_code": "aStock",
        "time_range": 2,
        "size": 50,
    }
    assert meta["update_time"] == "2026-11-19 06:00:00"
    assert len(rows) == 2


def test_fetch_board_omits_page_for_first_page_but_sends_later() -> None:
    client = _FakeClient()
    fetch_board(client, "hot_stock", "aStock")
    assert "page" not in client.calls[-1]
    fetch_board(client, "hot_stock", "aStock", page=2)
    assert client.calls[-1]["page"] == 2


def test_fetch_board_rejects_bad_params_before_request() -> None:
    client = _FakeClient()
    with pytest.raises(ValueError):
        fetch_board(client, "nope", "aStock")
    with pytest.raises(ValueError):
        fetch_board(client, "hot_stock", "stock")
    assert client.calls == [], "校验失败不该打请求"


def test_collect_boards_serial_and_merges_kinds() -> None:
    client = _FakeClient()
    rows, source_date = collect_boards(client, "hot_stock")
    assert source_date == "2026-11-19"
    assert {r["asset_kind"] for r in rows} == {"aStock", "etf"}
    assert len(rows) == 3
    assert client.calls[0]["type_code"] == "aStock", "默认先 A股"


def test_collect_boards_survives_single_kind_failure() -> None:
    """8 个组合里挂一个不该让当天整表全空。"""
    client = _FakeClient(fail={"aStock"})
    rows, _ = collect_boards(client, "hot_stock")
    assert rows, "A股失败后 ETF 仍应落库"
    assert all(r["asset_kind"] == "etf" for r in rows)


def test_collect_boards_returns_empty_when_all_fail() -> None:
    client = _FakeClient(fail={"aStock", "etf"})
    rows, source_date = collect_boards(client, "hot_stock")
    assert rows == []
    assert source_date == ""


def test_fetch_rows_reads_adapter_and_requires_api_key(monkeypatch, tmp_path: Path) -> None:
    from app.plugins.pingan import provider as pp

    calls: list[dict] = []

    class _Stub:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def hot_stock_list(self, payload):
            calls.append(payload)
            return [RAW_STOCK], {"update_time": "2026-11-19 06:00:00"}

    monkeypatch.setattr(pp, "get_api_key", lambda: "ak_test")
    monkeypatch.setattr("app.plugins.pingan.client.PingAnClient", _Stub)

    cfg = preset("ext_hot_watch")
    rows = asyncio.run(__import__("app.services.hot_stock", fromlist=["x"]).fetch_rows(cfg, DAY))
    assert len(rows) == 2, "aStock + etf 两类"
    assert calls[0]["api_key"] == "ak_test"
    payload = next(c for c in calls if "module_code" in c)
    assert payload["module_code"] == "hot_watch", "adapter 里的榜单必须生效"
    assert payload["time_range"] == 2


def test_fetch_rows_raises_without_api_key(monkeypatch) -> None:
    from app.plugins.pingan import provider as pp

    monkeypatch.setattr(pp, "get_api_key", lambda: "")
    with pytest.raises(ValueError, match="API Key"):
        asyncio.run(
            __import__("app.services.hot_stock", fromlist=["x"]).fetch_rows(
                preset("ext_hot_stock"), DAY
            )
        )


def test_fetch_rows_raises_on_empty_board(monkeypatch) -> None:
    from app.plugins.pingan import provider as pp

    class _Empty:
        def __init__(self, **kwargs):
            pass

        def hot_stock_list(self, payload):
            return [], {"update_time": ""}

    monkeypatch.setattr(pp, "get_api_key", lambda: "ak_test")
    monkeypatch.setattr("app.plugins.pingan.client.PingAnClient", _Empty)
    with pytest.raises(ValueError, match="0 行"):
        asyncio.run(
            __import__("app.services.hot_stock", fromlist=["x"]).fetch_rows(
                preset("ext_hot_stock"), DAY
            )
        )


# ---------------------------------------------------------------------------
# 4. live / 预设 / 接线
# ---------------------------------------------------------------------------

def test_live_returns_labelled_payload(monkeypatch) -> None:
    from app.plugins.pingan import provider as pp

    class _Stub:
        def __init__(self, **kwargs):
            pass

        def hot_stock_list(self, payload):
            return [RAW_ETF], {"update_time": "2026-11-19 07:00:00"}

    monkeypatch.setattr(pp, "get_api_key", lambda: "ak_test")
    monkeypatch.setattr("app.plugins.pingan.client.PingAnClient", _Stub)
    out = live("hot_surge", "etf", time_range="1h", size=10)
    assert out["board_label"] == "飙升榜"
    assert out["asset_kind_label"] == "ETF"
    assert out["time_range_label"] == "1小时"
    assert out["source_date"] == "2026-11-19"
    assert out["returned"] == 1
    assert out["items"][0]["symbol"] == "518880.SH"


def test_live_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        live("hot_nope", "aStock")
    with pytest.raises(ValueError):
        live("hot_stock", "stock")


def test_preset_one_table_per_board() -> None:
    ids = set(PRESET_IDS)
    assert ids == {
        "ext_hot_stock", "ext_hot_select", "ext_hot_watch", "ext_hot_surge",
    }
    assert len(ids) == len(BOARDS)


def test_preset_pull_recipe() -> None:
    cfg = preset("ext_hot_surge")
    assert cfg.id == "ext_hot_surge"
    assert cfg.label == "热股·飙升榜"
    assert cfg.mode == "timeseries"
    assert len(cfg.fields) == len(HOT_STOCK_FIELDS)
    pull = cfg.pull
    assert pull is not None
    assert pull.url.endswith("restapi/hotstock/isspContentService/hotStock/list")
    assert pull.method == "POST"
    assert pull.response_path == "results", "热股信封的数组在 results.list 下, 由适配器自己取"
    assert pull.auth == {"type": "header", "header": "X-API-Key", "key_ref": "pingan_api_key"}
    # 盘中轮询: 区间制 + 时间窗, 不用固定时刻 (有 schedule_time 会走墙钟对齐, 忽略间隔)
    assert pull.schedule_time is None
    assert pull.schedule_minutes == INTRADAY_INTERVAL_MIN
    assert pull.time_window_start == INTRADAY_WINDOW_START
    assert pull.time_window_end == INTRADAY_WINDOW_END
    # time_field 用保留名 → 每次拉取各存一盘, 热度增量才算得出来
    assert pull.time_field == INTRADAY_TIME_FIELD == PLATE_TS_FIELD
    assert pull.schedule_trading_only is True
    assert pull.enabled is True
    assert pull.adapter == {
        "kind": ADAPTER_KIND,
        "board": "hot_surge",
        "asset_kinds": list(LANDED_ASSET_KINDS),
        "time_range": DEFAULT_TIME_RANGE,
        "size": DEFAULT_SIZE,
    }
    assert pull.adapter["asset_kinds"] == ["aStock", "etf"]


def test_preset_rejects_foreign_id() -> None:
    with pytest.raises(ValueError, match="未知的热股榜单表"):
        preset("ext_valuation")


def test_get_preset_whitelist() -> None:
    assert get_preset("ext_hot_stock") is not None
    assert get_preset("ext_etf_valuation") is None


def test_preset_ids_do_not_collide_with_builtin_ext_presets() -> None:
    from app.services.ext_presets import _PRESET_IDS as BUILTIN

    assert not (set(PRESET_IDS) & set(BUILTIN))


def test_adapter_kind_is_registered_in_ext_pull() -> None:
    from app.services.ext_pull import _ADAPTER_KINDS

    assert ADAPTER_KIND in _ADAPTER_KINDS
    module_name, func_name = _ADAPTER_KINDS[ADAPTER_KIND]
    assert (module_name, func_name) == ("app.services.hot_stock", "fetch_rows")


def test_pull_config_roundtrip_keeps_adapter() -> None:
    cfg = preset("ext_hot_watch")
    assert cfg.pull is not None
    restored = PullConfig.from_dict(cfg.pull.to_dict())
    assert restored.adapter == cfg.pull.adapter


def test_ensure_preset_seeds_four_fresh_install(tmp_path: Path) -> None:
    asyncio.run(ensure_preset(tmp_path))
    store = ExtConfigStore(tmp_path)
    for config_id in PRESET_IDS:
        config = store.get(config_id)
        assert config is not None, f"{config_id} 未播种"
        assert config.pull and config.pull.adapter
    assert (tmp_path / "ext_data" / ".hot_stock_seeded").exists(), "未写播种标记"


def test_ensure_preset_does_not_overwrite_user_pull(tmp_path: Path) -> None:
    store = ExtConfigStore(tmp_path)
    custom = preset("ext_hot_stock")
    custom.pull.url = "https://example.com/my-own"   # type: ignore[union-attr]
    store.upsert(custom)

    asyncio.run(ensure_preset(tmp_path))
    assert store.get("ext_hot_stock").pull.url == "https://example.com/my-own"  # type: ignore[union-attr]


def test_ensure_preset_fills_factory_shell(tmp_path: Path) -> None:
    """用户建了同名空壳 (无 URL) → 补配方而不是覆盖成全新配置。"""
    store = ExtConfigStore(tmp_path)
    store.upsert(
        ExtConfig(
            id="ext_hot_stock", label="我自己建的", mode="timeseries",
            fields=[], pull=PullConfig(),
        )
    )
    asyncio.run(ensure_preset(tmp_path))
    cfg = store.get("ext_hot_stock")
    assert cfg is not None
    assert cfg.pull is not None
    assert cfg.pull.adapter and cfg.pull.adapter["kind"] == ADAPTER_KIND
    assert cfg.label == "我自己建的", "用户起的名字要留着"


def test_ensure_preset_respects_user_deletion(tmp_path: Path) -> None:
    asyncio.run(ensure_preset(tmp_path))          # 首次播种, 写标记
    ExtConfigStore(tmp_path).delete("ext_hot_select")
    asyncio.run(ensure_preset(tmp_path))          # 二次启动
    assert ExtConfigStore(tmp_path).get("ext_hot_select") is None, "删过就不该复活"


def test_ensure_preset_never_raises(tmp_path: Path, monkeypatch) -> None:
    """播种失败不该让应用起不来。"""
    def _boom(self, config):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ExtConfigStore, "upsert", _boom)
    asyncio.run(ensure_preset(tmp_path))


# ── 盘中化迁移 (1、2 项) ──────────────────────────────────────────────
def _legacy_daily_config(config_id: str = "ext_hot_stock") -> ExtConfig:
    """复刻「收盘后单次落库」的旧出厂配方。"""
    cfg = preset(config_id)
    assert cfg.pull is not None
    cfg.pull.schedule_minutes = 1440
    cfg.pull.schedule_time = "17:10"
    cfg.pull.schedule_period = "daily"
    cfg.pull.time_field = None
    cfg.pull.time_window_start = None
    cfg.pull.time_window_end = None
    return cfg


def test_needs_intraday_upgrade_recognises_legacy_shape(tmp_path: Path) -> None:
    cfg = _legacy_daily_config()
    assert _needs_intraday_upgrade(cfg.pull) is True
    assert _needs_intraday_upgrade(preset().pull) is False, "新配方不该再被判为待升级"
    assert _needs_intraday_upgrade(None) is False


def test_needs_intraday_upgrade_ignores_foreign_and_user_config() -> None:
    legacy = _legacy_daily_config()
    assert legacy.pull is not None

    # 别人的 adapter
    legacy.pull.adapter = {"kind": "pingan_etf_valuation"}
    assert _needs_intraday_upgrade(legacy.pull) is False

    # 用户自己改成 60 分钟 (不是出厂 1440) → 不动
    user = _legacy_daily_config()
    assert user.pull is not None
    user.pull.schedule_minutes = 60
    assert _needs_intraday_upgrade(user.pull) is False

    # 用户自己配了别的时间列 → 不动
    user_tf = _legacy_daily_config()
    assert user_tf.pull is not None
    user_tf.pull.time_field = "trade_time"
    assert _needs_intraday_upgrade(user_tf.pull) is False


def test_ensure_preset_upgrades_legacy_daily_config(tmp_path: Path) -> None:
    """旧出厂配方 (17:10 单次) 要迁到盘中轮询, 用户 URL/label 保留。"""
    store = ExtConfigStore(tmp_path)
    legacy = _legacy_daily_config()
    legacy.label = "我的热股"
    store.upsert(legacy)

    asyncio.run(ensure_preset(tmp_path))
    cfg = store.get("ext_hot_stock")
    assert cfg is not None and cfg.pull is not None
    assert cfg.pull.time_field == PLATE_TS_FIELD
    assert cfg.pull.schedule_minutes == INTRADAY_INTERVAL_MIN
    assert cfg.pull.schedule_time is None, "留着 schedule_time 会走墙钟对齐, 忽略间隔"
    assert cfg.pull.time_window_start == INTRADAY_WINDOW_START
    assert cfg.pull.time_window_end == INTRADAY_WINDOW_END
    assert cfg.label == "我的热股", "用户改过的名字要留着"


def test_ensure_preset_keeps_user_schedule_on_upgrade(tmp_path: Path) -> None:
    store = ExtConfigStore(tmp_path)
    user = _legacy_daily_config()
    assert user.pull is not None
    user.pull.schedule_minutes = 60          # 用户自己定的间隔
    store.upsert(user)

    asyncio.run(ensure_preset(tmp_path))
    cfg = store.get("ext_hot_stock")
    assert cfg is not None and cfg.pull is not None
    assert cfg.pull.schedule_minutes == 60, "用户设的间隔不许被覆盖"
    assert cfg.pull.time_field is None, "不动就是全不动, 不能只改一半"


def test_routes_registered() -> None:
    from app.api import hot_stock as api_mod

    assert api_mod.router.prefix == "/api/hot-stock"
    paths = {r.path for r in api_mod.router.routes}   # type: ignore[attr-defined]
    assert paths == {"/api/hot-stock/boards", "/api/hot-stock/list"}


def test_routes_wired_into_app() -> None:
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/api/hot-stock/list" in paths
    assert "/api/hot-stock/boards" in paths


def test_wrap_maps_pingan_errors_to_http() -> None:
    from fastapi import HTTPException

    from app.api.hot_stock import _wrap
    from app.plugins.pingan.client import (
        PingAnAuthError,
        PingAnError,
        PingAnQuotaError,
        PingAnRateLimitError,
    )

    assert _wrap(ValueError("x")).status_code == 400
    assert _wrap(PingAnAuthError("x")).status_code == 400
    assert _wrap(PingAnQuotaError("x")).status_code == 429
    assert _wrap(PingAnRateLimitError("x")).status_code == 429
    assert _wrap(PingAnError("x")).status_code == 502
    assert isinstance(_wrap(RuntimeError("x")), HTTPException)
    assert _wrap(RuntimeError("x")).status_code == 500


def test_asset_kinds_cover_five_types() -> None:
    assert set(ASSET_KINDS) == {"aStock", "etf", "block", "hkStock", "bond"}
    assert set(LANDED_ASSET_KINDS) == {"aStock", "etf"}, "只落 A股+ETF, 其余供即时查询"


def test_module_is_importable_without_side_effects() -> None:
    assert "app.services.hot_stock" in sys.modules

