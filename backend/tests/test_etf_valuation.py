"""ETF 估值分位 (平安金库 ETF_FILTER) 的单测。

覆盖三层:
  1. 行归一 —— 单位换算 (小数→百分数)、文本清洗、代码映射、无法识别的代码丢弃
  2. 游标翻页 —— code 游标去重、短页/游标停滞/max_pages 三条终止条件
  3. 框架接线 —— PullConfig.adapter 往返、ext_pull 分发、since_cursor 跳过、
     内置预设的播种与「绝不覆盖用户配置」契约

为什么这些断言值得写: 平安这套接口的坑全在语义而不是语法上 —— 数值全是字符串
且单位混杂 (分位是小数、股息率是百分数), scope 这类字段看着能筛其实返回
total=0, code 游标翻页会重复末条。任何一处回退都不会抛异常, 只会静默写入错数,
所以每条都在断言具体数值。
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import date
from pathlib import Path

import pytest

from app.services import etf_valuation, ext_pull
from app.services.etf_valuation import (
    CONFIG_ID,
    SCREEN_FILTERS,
    VALUATION_FIELDS,
    _filter_code,
    _sort_rows,
    apply_filters,
    ensure_preset,
    pa_to_symbol,
    preset,
    screen,
    to_ext_row,
    walk_universe,
)
from app.services.ext_data import ExtConfig, ExtConfigStore, PullConfig

DAY = date(2026, 11, 18)

# 真实响应样例行 (2026-11 实测 SZ159682), 保留原始字符串形态与数字类型混杂
SAMPLE = {
    "code": "SZ159682",
    "cost": "0.002",
    "dayRise120": "-0.459300",
    "dayRise20": "-0.190200",
    "dayRise5": "-0.088700",
    "dayRise60": "-0.420300",
    "etfName": "创业板50ETF景顺",
    "favoriteNum": "2470",
    "foldingPremiunRates": "-1.58",
    "fundCompany": "景顺长城基金",
    "fundType1": "规模宽基",
    "fundType2": "规模宽基,大盘",
    "indexCode": "SZ399673",
    "indexName": "创业板50",
    "oneYearRise": "-0.300100",
    "pb": "3.7291",
    "pbFenWei": "0.0465",
    "pe": "20.905",
    "peFenWei": "0.0015",
    "roe": "0.1961",
    "rsi": "25.196194",
    "scope": 3874240000,
    "scopeIncreaseM": "10.12",
    "search1W": "3818",
    "thisYearRise": "-0.3257",
    "turnVolume20": 7686718736,
    "yielD": "1.4646",
}


# ------------------------------------------------------------------ 行归一
def test_to_ext_row_converts_decimal_percentiles_to_percent() -> None:
    """分位/费率/涨跌幅/ROE 源为小数 → 落库为百分数 (0.0015 → 0.15)。"""
    row = to_ext_row(SAMPLE, "2026-11-18")
    assert row is not None
    assert row["pe_percentile"] == pytest.approx(0.15)
    assert row["pb_percentile"] == pytest.approx(4.65)
    assert row["cost"] == pytest.approx(0.2)
    assert row["roe"] == pytest.approx(19.61)
    assert row["rise_5d"] == pytest.approx(-8.87)
    assert row["rise_1y"] == pytest.approx(-30.01)


def test_to_ext_row_keeps_already_percent_fields_as_is() -> None:
    """股息率/折溢价/规模增幅源已是百分数, 不能再乘 100。"""
    row = to_ext_row(SAMPLE, "2026-11-18")
    assert row is not None
    assert row["dividend_yield"] == pytest.approx(1.4646)
    assert row["premium_rate"] == pytest.approx(-1.58)
    assert row["scope_growth_m"] == pytest.approx(10.12)


def test_to_ext_row_keeps_ratios_as_ratios() -> None:
    """pe/pb/rsi 是比值不是百分数, 原样保留。"""
    row = to_ext_row(SAMPLE, "2026-11-18")
    assert row is not None
    assert row["pe"] == pytest.approx(20.905)
    assert row["pb"] == pytest.approx(3.7291)
    assert row["rsi"] == pytest.approx(25.196194)
    assert row["scope"] == pytest.approx(3874240000)
    assert row["amount_20d"] == pytest.approx(7686718736)


def test_to_ext_row_maps_symbol_and_text_fields() -> None:
    row = to_ext_row(SAMPLE, "2026-11-18")
    assert row is not None
    assert row["symbol"] == "159682.SZ"
    assert row["code"] == "159682"
    assert row["name"] == "创业板50ETF景顺"
    assert row["fund_company"] == "景顺长城基金"
    assert row["index_name"] == "创业板50"
    assert row["source_date"] == "2026-11-18"


def test_to_ext_row_drops_unrecognized_code() -> None:
    """内部代码 (159682.SZ) 或残缺代码直接丢弃整行, 不写脏 symbol。"""
    for bad in ("", "159682.SZ", "SZ15968", "BJ430047", "SHABCDEF", None):
        assert to_ext_row({"code": bad, "pe": "1"}, "2026-11-18") is None


def test_to_ext_row_treats_placeholders_as_null() -> None:
    """空串/None/"-"/"--" 一律 None, 不能变成 0.0 (0 会被当成真实估值)。"""
    row = to_ext_row(
        {"code": "SH510300", "pe": "", "pb": None, "peFenWei": "-", "roe": "--"},
        "2026-11-18",
    )
    assert row is not None
    assert row["pe"] is None
    assert row["pb"] is None
    assert row["pe_percentile"] is None
    assert row["roe"] is None


def test_to_ext_row_strips_percent_suffix_and_thousands() -> None:
    """源里若带 % 或千分位也能吃下 (防御性, 平安现版本不带)。"""
    row = to_ext_row({"code": "SH510300", "yielD": "3.5%", "scope": "1,234,567"}, DAY.isoformat())
    assert row is not None
    assert row["dividend_yield"] == pytest.approx(3.5)
    assert row["scope"] == pytest.approx(1234567)


def test_pa_to_symbol() -> None:
    assert pa_to_symbol("SH510300") == "510300.SH"
    assert pa_to_symbol("sz159682") == "159682.SZ"
    assert pa_to_symbol("SH51030") is None
    assert pa_to_symbol("510300") is None


def test_valuation_fields_cover_every_normalized_column() -> None:
    """归一表与字段声明必须一一对应, 否则新增列会静默丢失。"""
    declared = {name for name, _dtype, _label in VALUATION_FIELDS}
    row = to_ext_row(SAMPLE, "2026-11-18")
    assert row is not None
    assert set(row) - {"symbol", "code"} == declared


# ------------------------------------------------------------- 游标翻页
class _FakeClient:
    """按 code 游标返回预置页的快照客户端 (复刻服务端的边界重复行为)。"""

    def __init__(self, pages: list[list[str]], source_date: str = "2026-11-18") -> None:
        self._pages = pages
        self._source_date = source_date
        self.requests: list[list[dict]] = []

    def query_etf_filter(self, payload: dict) -> tuple[list[dict], int, str]:
        self.requests.append(payload["wheres"])
        index = len(self.requests) - 1
        codes = self._pages[index] if index < len(self._pages) else []
        rows = [{"code": c, "pe": "10"} for c in codes]
        return rows, len(self._pages), self._source_date


def _codes(count: int, prefix: str = "SH51") -> list[str]:
    return [f"{prefix}{6000 + i}" for i in range(count)]


def test_walk_universe_pages_by_code_cursor() -> None:
    page1 = _codes(100, "SH51")
    page2 = [page1[-1], *_codes(3, "SZ15")]   # 服务端重复末个 code
    client = _FakeClient([page1, page2])
    rows, source_date = walk_universe(client, page_size=100, max_pages=10)
    assert source_date == "2026-11-18"
    assert len(rows) == 103, "边界重复的 code 必须去重"
    # 第二个请求的游标 = 上一页末个 code
    assert client.requests[1] == [{"field": "code", "type": 3, "value": page1[-1]}]


def test_walk_universe_stops_on_short_page() -> None:
    client = _FakeClient([_codes(100), _codes(38, "SZ15")])
    rows, _ = walk_universe(client, page_size=100, max_pages=10)
    assert len(rows) == 138
    assert len(client.requests) == 2, "短页应终止翻页, 不再发第 3 个请求"


def test_walk_universe_stops_when_cursor_stalls() -> None:
    """服务端只返回同一页时 (游标不前进) 必须停下, 不能死循环。"""
    page = _codes(100)
    client = _FakeClient([page, page])
    rows, _ = walk_universe(client, page_size=100, max_pages=10)
    assert len(rows) == 100
    assert len(client.requests) == 2


def test_walk_universe_respects_max_pages() -> None:
    """max_pages 是防止拖死拉取的硬上限 (也是额度保护)。"""
    client = _FakeClient([_codes(100, "SH51"), _codes(100, "SZ15"), _codes(100, "SZ16")])
    rows, _ = walk_universe(client, page_size=100, max_pages=2)
    assert len(rows) == 200
    assert len(client.requests) == 2


def test_walk_universe_empty_first_page() -> None:
    client = _FakeClient([[]])
    rows, _ = walk_universe(client)
    assert rows == []


# ------------------------------------------------------------- 本地筛选
def _row(**kw: object) -> dict:
    """造一行归一后的数据 (缺省是 SAMPLE 的等价形态)。"""
    base = {
        "symbol": "159682.SZ", "code": "SZ159682", "name": "创业板50ETF景顺",
        "fund_type1": "规模宽基", "fund_type2": "规模宽基,大盘",
        "index_name": "创业板50", "pe": 20.905, "pb": 3.7291,
        "pe_percentile": 0.15, "pb_percentile": 4.65, "roe": 19.61,
        "cost": 0.2, "dividend_yield": 1.4646, "rsi": 25.196,
        "scope": 3874240000.0, "amount_20d": 7686718736.0,
    }
    base.update(kw)
    return base


def test_apply_filters_compares_numerically_not_lexicographically() -> None:
    """服务端按字符串比 (`"10" < "6"`), 本地必须按数值比 —— 这是本次改动的根因。"""
    rows = [_row(symbol="A.SH", pe=10.0823), _row(symbol="B.SH", pe=6.482),
            _row(symbol="C.SH", pe=25.0)]
    hit = apply_filters(rows, {"pe_max": 20})
    assert [r["symbol"] for r in hit] == ["A.SH", "B.SH"], "6.482 与 10.08 都必须命中"


def test_apply_filters_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="不支持的筛选参数"):
        apply_filters([_row()], {"pe_ttm_max": 20})


def test_apply_filters_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="需要数值"):
        apply_filters([_row()], {"pe_max": "便宜"})


def test_apply_filters_text_filters() -> None:
    rows = [_row(symbol="A.SH", fund_type1="规模宽基", index_name="沪深300"),
            _row(symbol="B.SH", fund_type1="行业主题", index_name="中证白酒")]
    assert [r["symbol"] for r in apply_filters(rows, {"fund_type1": "规模宽基"})] == ["A.SH"]
    assert [r["symbol"] for r in apply_filters(rows, {"index_name": "白酒"})] == ["B.SH"]


def test_apply_filters_multi_value_fund_type() -> None:
    rows = [_row(symbol="A.SH", fund_type1="规模宽基"),
            _row(symbol="B.SH", fund_type1="债券资产"),
            _row(symbol="C.SH", fund_type1="行业主题")]
    hit = apply_filters(rows, {"fund_type1": "规模宽基，债券资产"})   # 全角逗号也认
    assert [r["symbol"] for r in hit] == ["A.SH", "B.SH"]


def test_apply_filters_rejects_blank_text() -> None:
    with pytest.raises(ValueError, match="需要文本"):
        apply_filters([_row()], {"fund_type1": "   "})


def test_apply_filters_code_accepts_three_notations() -> None:
    rows = [_row(symbol="510300.SH"), _row(symbol="159682.SZ")]
    for text in ("510300", "SH510300", "510300.SH", "sh510300"):
        hit = apply_filters(rows, {"code": text})
        assert [r["symbol"] for r in hit] == ["510300.SH"], text


def test_filter_code_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="代码格式无法识别"):
        _filter_code("5103000")


def test_apply_filters_null_never_matches() -> None:
    """QDII 没有 PE/分位, 不能被当成 0 混进「分位 ≤ 30%」。"""
    rows = [_row(symbol="A.SH", pe_percentile=None), _row(symbol="B.SH", pe_percentile=0.0)]
    hit = apply_filters(rows, {"pe_percentile_max": 30})
    assert [r["symbol"] for r in hit] == ["B.SH"]


def test_apply_filters_supports_scope_and_amount() -> None:
    """scope / amount_20d 在源端任何操作符都是 0 条, 本地筛选才是它们的正确用法。"""
    rows = [_row(symbol="A.SH", scope=1e9, amount_20d=1e8),
            _row(symbol="B.SH", scope=5e9, amount_20d=5e8)]
    assert [r["symbol"] for r in apply_filters(rows, {"scope_min": 2e9})] == ["B.SH"]
    assert [r["symbol"] for r in apply_filters(rows, {"amount_20d_min": 2e8})] == ["B.SH"]


def test_screenable_fields_are_the_verified_ones() -> None:
    assert {"pe_max", "pb_max", "pe_percentile_max", "dividend_yield_min",
            "scope_min", "amount_20d_min"} <= set(SCREEN_FILTERS)


def test_sort_rows_keeps_nulls_last_in_both_directions() -> None:
    rows = [_row(symbol="A.SH", pe=None), _row(symbol="B.SH", pe=3.0),
            _row(symbol="C.SH", pe=9.0)]
    assert [r["symbol"] for r in _sort_rows(rows, "pe", False)] == ["B.SH", "C.SH", "A.SH"]
    assert [r["symbol"] for r in _sort_rows(rows, "pe", True)] == ["C.SH", "B.SH", "A.SH"]


def test_screen_filters_and_sorts_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(symbol="A.SH", pe_percentile=0.15), _row(symbol="B.SH", pe_percentile=92.74),
            _row(symbol="C.SH", pe_percentile=None)]
    monkeypatch.setattr(etf_valuation, "_load_universe", lambda **kw: (rows, "2026-11-19", False))
    out = screen({"pe_percentile_max": 30}, order_by="pe_percentile")
    assert out["total"] == 1
    assert out["returned"] == 1
    assert out["cached"] is False
    assert out["items"][0]["symbol"] == "A.SH"
    assert out["source_date"] == "2026-11-19"


def test_screen_reports_total_before_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """total 是命中总数, returned 才是本页条数。"""
    rows = [_row(symbol=f"S{i}.SH", scope=float(i)) for i in range(30)]
    monkeypatch.setattr(etf_valuation, "_load_universe", lambda **kw: (rows, "2026-11-19", True))
    out = screen({}, order_by="scope", limit=5)
    assert out["total"] == 30
    assert out["returned"] == 5
    assert out["cached"] is True


def test_screen_clamps_limit_to_100(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(symbol=f"S{i}.SH", scope=float(i)) for i in range(300)]
    monkeypatch.setattr(etf_valuation, "_load_universe", lambda **kw: (rows, "2026-11-19", False))
    assert screen({}, limit=500)["returned"] == 100


def test_screen_accepts_source_and_column_order_names(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(symbol="A.SH", dividend_yield=1.0), _row(symbol="B.SH", dividend_yield=3.0)]
    monkeypatch.setattr(etf_valuation, "_load_universe", lambda **kw: (rows, "2026-11-19", False))
    for alias in ("dividend_yield", "yielD"):
        out = screen({}, order_by=alias, descending=True)
        assert out["order_by"] == "dividend_yield", alias
        assert out["items"][0]["symbol"] == "B.SH"


def test_screen_rejects_unknown_order_by() -> None:
    with pytest.raises(ValueError, match="不支持的排序字段"):
        screen({}, order_by="nope")


def test_screen_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    etf_valuation.clear_universe_cache()
    monkeypatch.setattr("app.plugins.pingan.provider.get_api_key", lambda: "")
    with pytest.raises(ValueError, match="API Key"):
        screen({})


def test_universe_cache_avoids_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 分钟缓存: 第二次筛选不再翻 18 页 (额度 2000/日 撑不住每点一次就全量)。"""
    calls: list[int] = []

    def fake_walk(client, *, page_size=100, max_pages=40):
        calls.append(1)
        return [to_ext_row(SAMPLE, "2026-11-19")], "2026-11-19"

    etf_valuation.clear_universe_cache()
    monkeypatch.setattr("app.plugins.pingan.provider.get_api_key", lambda: "ak_test")
    monkeypatch.setattr(etf_valuation, "walk_universe", fake_walk)
    first = screen({})
    second = screen({})
    assert len(calls) == 1, "第二次必须命中缓存"
    assert first["cached"] is False and second["cached"] is True
    assert first["items"][0]["symbol"] == second["items"][0]["symbol"] == "159682.SZ"
    etf_valuation.clear_universe_cache()



# ------------------------------------------------------------- 预设与接线
def test_preset_ships_complete_pull_recipe() -> None:
    config = preset()
    assert config.id == CONFIG_ID
    assert config.mode == "timeseries"
    pull = config.pull
    assert pull is not None
    assert pull.url.startswith("https://ai.stock.pingan.com/restapi/jinku/")
    assert pull.method == "POST"
    assert pull.response_path == "rows", "信封没有 data 层, 行在顶层 rows"
    assert pull.auth and pull.auth.get("type") == "header"
    assert pull.auth.get("header") == "X-API-Key"
    assert pull.auth.get("key_ref") == "pingan_api_key", "复用行情插件同一把 Key"
    assert pull.adapter and pull.adapter.get("kind") == "pingan_etf_valuation"
    assert pull.schedule_time == "17:20"
    assert pull.schedule_trading_only is True
    assert pull.enabled is True
    # 分位表是时序表: 每日一分区, 序列查询才出得来
    assert config.code_map == {"type": "computed", "from": "symbol", "method": "strip_exchange"}


def test_pull_config_roundtrip_preserves_adapter() -> None:
    """adapter 随 config.json 持久化, 不丢字段 (与 fanout 同契约)。"""
    config = preset()
    restored = PullConfig.from_dict(config.pull.to_dict())
    assert restored.adapter == config.pull.adapter


def test_pull_config_drops_adapter_without_kind() -> None:
    """没有 kind 的 adapter 视为未配置, 避免取数时静默走空分支。"""
    assert PullConfig(url="https://x/y", adapter={"page_size": 100}).adapter is None
    assert PullConfig(url="https://x/y", adapter={"kind": "  "}).adapter is None


def _cfg(adapter: dict | None = None) -> ExtConfig:
    return ExtConfig(
        id="ext_test",
        label="T",
        mode="timeseries",
        fields=[],
        pull=PullConfig(url="https://example.test/rows", adapter=adapter),
    )


def test_fetch_adapter_rows_dispatches_to_registered_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    module = types.ModuleType("app.services._fake_adapter")

    async def fake_fetch(config, target_date, data_dir=None):
        calls["args"] = (config.id, target_date, data_dir)
        return [{"symbol": "510300.SH"}]

    module.fetch_rows = fake_fetch  # type: ignore[attr-defined]
    sys.modules[module.__name__] = module
    try:
        monkeypatch.setitem(ext_pull._ADAPTER_KINDS, "fake", (module.__name__, "fetch_rows"))
        rows = asyncio.run(
            ext_pull._fetch_adapter_rows(_cfg({"kind": "fake"}), DAY, Path("/tmp"))
        )
    finally:
        sys.modules.pop(module.__name__, None)
    assert rows == [{"symbol": "510300.SH"}]
    assert calls["args"] == ("ext_test", DAY, Path("/tmp"))


def test_fetch_adapter_rows_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="未知的取数适配器"):
        asyncio.run(ext_pull._fetch_adapter_rows(_cfg({"kind": "nope"}), DAY))


def test_fetch_rows_for_date_uses_adapter_and_skips_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter 配了就绕开通用 HTTP 链 (否则会拿空 body 去打平安的 URL)。"""
    async def boom(*_a, **_kw):
        raise AssertionError("adapter 模式不应发起通用 HTTP 请求")

    async def fake_fetch(config, target_date, data_dir=None):
        return [{"symbol": "510300.SH", "pe": 13.4}]

    monkeypatch.setattr(ext_pull, "_request_json", boom)
    monkeypatch.setitem(ext_pull._ADAPTER_KINDS, "pingan_etf_valuation", ("x", "fetch_rows"))
    module = types.ModuleType("x")
    module.fetch_rows = fake_fetch  # type: ignore[attr-defined]
    sys.modules["x"] = module
    try:
        rows = asyncio.run(ext_pull._fetch_rows_for_date(_cfg(preset().pull.adapter), DAY))
    finally:
        sys.modules.pop("x", None)
    assert rows == [{"symbol": "510300.SH", "pe": 13.4}]


def test_since_cursor_skips_adapter_mode(tmp_path: Path) -> None:
    """adapter 模式没有整表游标语义, 不能带 since 参数。"""
    cfg = ExtConfig(
        id="ext_test",
        label="T",
        mode="timeseries",
        fields=[],
        pull=PullConfig(
            url="https://example.test/rows",
            time_field="ts",
            since_param="since",
            adapter={"kind": "pingan_etf_valuation"},
        ),
    )
    assert ext_pull.since_cursor(tmp_path, cfg, DAY) is None


def test_ensure_preset_seeds_fresh_install(tmp_path: Path) -> None:
    asyncio.run(ensure_preset(tmp_path))
    config = ExtConfigStore(tmp_path).get(CONFIG_ID)
    assert config is not None
    assert config.pull and config.pull.adapter
    assert (tmp_path / "ext_data" / ".etf_valuation_seeded").exists(), "未写播种标记"


def test_ensure_preset_does_not_overwrite_user_pull(tmp_path: Path) -> None:
    """用户改过 URL → 一律不碰 (与内置概念/行业预设同纪律)。"""
    store = ExtConfigStore(tmp_path)
    custom = preset()
    custom.pull.url = "https://my.proxy/etf"
    store.upsert(custom)

    asyncio.run(ensure_preset(tmp_path))
    reloaded = ExtConfigStore(tmp_path).get(CONFIG_ID)
    assert reloaded.pull.url == "https://my.proxy/etf"


def test_ensure_preset_fills_factory_shell(tmp_path: Path) -> None:
    """空壳配置 (无 URL) → 补出厂配方, 不是覆盖用户配置。"""
    store = ExtConfigStore(tmp_path)
    shell = preset()
    shell.pull = PullConfig()
    store.upsert(shell)

    asyncio.run(ensure_preset(tmp_path))
    reloaded = ExtConfigStore(tmp_path).get(CONFIG_ID)
    assert reloaded.pull.url.startswith("https://ai.stock.pingan.com/")
    assert reloaded.pull.adapter


def test_ensure_preset_does_not_resurrect_after_delete(tmp_path: Path) -> None:
    asyncio.run(ensure_preset(tmp_path))
    assert ExtConfigStore(tmp_path).delete(CONFIG_ID) is True
    asyncio.run(ensure_preset(tmp_path))
    assert ExtConfigStore(tmp_path).get(CONFIG_ID) is None, "已播种后删除的内置表不应复活"


def test_preset_does_not_collide_with_builtin_presets() -> None:
    """独立播种标记: 不能借用 .builtin_presets_seeded 的顺序。"""
    from app.services import ext_presets

    assert CONFIG_ID not in ext_presets._PRESET_IDS
    assert ext_presets._SEED_MARKER != ".etf_valuation_seeded"


def test_screen_route_normalizes_order_by_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """路由层也要做别名归一 —— 否则 screen() 修好了, 路由前置校验仍先报 400。"""
    from app.api import etf_valuation as api_mod

    captured: dict = {}
    monkeypatch.setattr(
        api_mod, "screen", lambda params, **kw: captured.update(kw) or {"ok": True}
    )
    # 源字段名 → 本表列名
    api_mod.screen_etfs(order_by="yielD", desc=True, limit=5)
    assert captured["order_by"] == "dividend_yield"
    assert captured["descending"] is True
    # 本表列名原样通过
    api_mod.screen_etfs(order_by="pe_percentile", desc=False, limit=5)
    assert captured["order_by"] == "pe_percentile"


def test_screen_route_passes_scope_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    """scope_min / amount_20d_min 必须能透传到服务层 (源端筛不了, 本地能筛)。"""
    from app.api import etf_valuation as api_mod

    captured: dict = {}
    monkeypatch.setattr(
        api_mod, "screen", lambda params, **kw: captured.update(params) or {"ok": True}
    )
    # 直接调函数时 Query 默认值不会展开, 必须显式传 order_by
    api_mod.screen_etfs(order_by="pe_percentile", scope_min=2e9, amount_20d_min=1e8)
    assert captured["scope_min"] == 2e9
    assert captured["amount_20d_min"] == 1e8


def test_etf_valuation_router_registered() -> None:
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/etf-valuation/screen" in paths
