"""PingAnProvider 契约与单位标准化测试。

不依赖真实网络与 API Key: 用假 PingAnClient 返回样例快照页, 验证字段映射、
单位口径 (CONTRIBUTING §3.1: 百分数→小数、volume 股→手)、代码格式转换
(`159682.SZ` ↔ `SZ159682`)、盘前 0 价回退、能力声明 (未声明数据集回退默认源)
与设置页试拉。

关键回归点 (都是实测踩过的):
1. 平安所有数值是字符串, 百分比带 `%` 后缀 —— 直接 float() 会抛异常并静默变 None
2. 盘前 price=0 且 OHLC 全 0 —— 不回退昨收会显示 0 元假跌停
3. 内部代码格式直接透传会被整批判为 not_found (实测 2 只全部未识别)
4. 北交所代码平安不支持 —— 必须过滤, 否则整批连坐
"""

from __future__ import annotations

import pytest

from app.data_providers.base import ProviderRequestError
from app.plugins.pingan import client as pc
from app.plugins.pingan import provider as pp
from app.plugins.pingan.provider import PingAnProvider


class _FakeClient:
    """按调用顺序返回预置页, 记录调用供断言。"""

    def __init__(self, pages: list[list[dict]], error: Exception | None = None):
        self.pages = pages
        self.error = error
        self.calls: list[list[str]] = []
        self.last_server_ts = 0

    def quote(self, symbols):
        self.calls.append(list(symbols))
        if self.error:
            raise self.error
        if not self.pages:
            return [], 0
        idx = min(len(self.calls) - 1, len(self.pages) - 1)
        return list(self.pages[idx]), self.last_server_ts

    def close(self):
        pass


def _row(code: str = "SZ159682", **over):
    """实测快照行结构 (2026-11): 全部字符串, 百分比带 % 后缀。"""
    row = {
        "code": code,
        "name": "创业板50ETF景顺",
        "price": "0.99",
        "change": "-0.006",
        "open": "1.0",
        "high": "1.004",
        "low": "0.988",
        "volume": "190107000",
        "amount": "189250000",
        "pb": "0.0",
        "iopv": "0.9906",
        "change_pct": "-0.60%",
        "prev_close": "0.996",
        "turnover_pct": "4.88%",
        "pe_ttm": "0.0",
        "market_cap": "3857340000",
        "circ_market_cap": "3857340000",
        "tracking_index": "SZ399673",
        "premium_rate": "-0.06%",
        "fund_size": "3857344593",
    }
    row.update(over)
    return row


@pytest.fixture()
def provider(monkeypatch):
    """注入假 client, 隔离网络与 Key。"""
    p = PingAnProvider()
    fake = _FakeClient([[_row()]])
    monkeypatch.setattr(p, "_get_client", lambda: fake)
    p._fake = fake
    return p


# ---------------------------------------------------------------- 代码格式


def test_to_pa_symbol_converts_internal_format():
    assert pp.to_pa_symbol("159682.SZ") == "SZ159682"
    assert pp.to_pa_symbol("600519.SH") == "SH600519"
    assert pp.to_pa_symbol("159682.sz") == "SZ159682"
    # 裸代码原样返回 (平安会判 not_found, 但不在本层伪造市场)
    assert pp.to_pa_symbol("159682") == "159682"


def test_from_pa_symbol_roundtrip_and_rejects_garbage():
    assert pp.from_pa_symbol("SZ159682") == "159682.SZ"
    assert pp.from_pa_symbol("SH600519") == "600519.SH"
    assert pp.from_pa_symbol("BJ430047") == "430047.BJ"
    # 非标准格式 → None (宁可不识别, 也不猜市场)
    assert pp.from_pa_symbol("") is None
    assert pp.from_pa_symbol("SZ") is None
    assert pp.from_pa_symbol("SZABCDEF") is None
    assert pp.from_pa_symbol("XX123456") is None


def test_symbols_are_converted_before_request(provider):
    """回归: 内部格式直接透传会被整批判 not_found (实测 2 只全未识别)。"""
    provider.get_etf_realtime(["159682.SZ", "600519.SH"])
    assert provider._fake.calls == [["SZ159682", "SH600519"]]


def test_bj_symbols_are_filtered_out(provider):
    """北交所平安不支持, 不过滤会让整批返回 not_found。"""
    provider.get_etf_realtime(["159682.SZ", "430047.BJ", ""])
    assert provider._fake.calls == [["SZ159682"]]


# ---------------------------------------------------------------- 单位口径


def test_number_and_percent_parsing():
    assert pp._num("1.004") == pytest.approx(1.004)
    assert pp._num("") is None
    assert pp._num("--") is None
    assert pp._num("null") is None
    assert pp._num(None) is None
    # 百分数字符串 → 百分数数值本身 (不 /100)
    assert pp._pct("-0.60%") == pytest.approx(-0.60)
    assert pp._pct("4.88%") == pytest.approx(4.88)
    assert pp._pct("") is None
    assert pp._pct("3.87") == pytest.approx(3.87)


def test_quote_row_units_follow_data_contract():
    """契约 §3.1: change_pct / turnover_rate 入口小数制; volume 单位手。"""
    rec = pp._map_quote_row(_row(), 1_700_000_000_000)
    assert rec is not None
    assert rec["symbol"] == "159682.SZ"
    # "−0.60%" → 百分数 −0.60 → 小数制 −0.006
    assert rec["change_pct"] == pytest.approx(-0.006)
    # "4.88%" → 小数制 0.0488
    assert rec["turnover_rate"] == pytest.approx(0.0488)
    # 股 → 手 (floor)
    assert rec["volume"] == 1_901_070
    assert rec["amount"] == pytest.approx(189_250_000.0)
    # ETF 独有字段: 百分数口径不做 /100 换算 (项目无既有契约, 保持原值)
    assert rec["iopv"] == pytest.approx(0.9906)
    assert rec["premium_rate"] == pytest.approx(-0.06)
    assert rec["tracking_index"] == "SZ399673"
    assert rec["timestamp"] == 1_700_000_000_000


def test_volume_floors_to_lots():
    rec = pp._map_quote_row(_row(volume="150"), 0)
    assert rec["volume"] == 1  # 150 股 → 1 手 (floor, 不四舍五入)


def test_change_pct_derived_when_absent():
    """change_pct 缺失时按 change_amount/prev_close 推导 (小数制, 不乘 100)。"""
    rec = pp._map_quote_row(_row(change_pct="", change="-0.006", prev_close="0.996"), 0)
    assert rec["change_pct"] == pytest.approx(-0.006 / 0.996)


def test_missing_fields_stay_none_not_zero():
    """接口不提供的字段返回 None; 不得启发式补 0 (会掩盖真实数据错误)。"""
    rec = pp._map_quote_row(
        {"code": "SZ159682", "price": "1.0", "prev_close": "1.0"}, 0,
    )
    assert rec is not None
    assert rec["open"] is None or rec["open"] == 1.0  # OHLC 缺失 → 回退最新价
    assert rec["turnover_rate"] is None
    assert rec["amplitude"] is None
    assert rec["iopv"] is None
    assert rec["tracking_index"] is None


# ---------------------------------------------------------------- 盘前 0 价


def test_pre_open_zero_price_falls_back_to_prev_close():
    """回归 (踩坑 #7): 盘前 price=0 / OHLC=0 → 回退昨收, 不显示 0 元假跌停。"""
    rec = pp._map_quote_row(
        _row(price="0", change="0", open="0", high="0", low="0",
             change_pct="0.00%", prev_close="0.996"),
        0,
    )
    assert rec["last_price"] == pytest.approx(0.996)
    assert rec["open"] == pytest.approx(0.996)
    assert rec["high"] == pytest.approx(0.996)
    assert rec["low"] == pytest.approx(0.996)


def test_zero_price_without_prev_close_stays_none():
    """没有昨收可退时保持 None, 不伪造价格。"""
    rec = pp._map_quote_row({"code": "SZ159682", "price": "0", "prev_close": ""}, 0)
    assert rec["last_price"] is None


# ---------------------------------------------------------------- 软失败


def test_rate_limit_returns_partial_and_warns(provider, caplog):
    """限流: 保留已拿到的记录, 不抛异常打断行情线程。"""
    provider._fake.error = pc.PingAnRateLimitError("限流")
    with caplog.at_level("WARNING"):
        out = provider.get_etf_realtime(["159682.SZ"])
    assert out == []
    assert any("限流" in r.message for r in caplog.records)


def test_generic_error_returns_empty(provider, caplog):
    provider._fake.error = pc.PingAnError("boom")
    with caplog.at_level("WARNING"):
        out = provider.get_etf_realtime(["159682.SZ"])
    assert out == []
    assert any("失败" in r.message for r in caplog.records)


def test_unrecognized_rows_warn_not_silent(monkeypatch, caplog):
    """整页识别不出 code → 明确告警而非静默空数据 (接口结构变化信号)。"""
    p = PingAnProvider()
    fake = _FakeClient([[{"name": "x"}, {"name": "y"}]])
    monkeypatch.setattr(p, "_get_client", lambda: fake)
    with caplog.at_level("WARNING"):
        out = p.get_etf_realtime(["159682.SZ"])
    assert out == []
    assert any("结构变化" in r.message for r in caplog.records)


def test_empty_symbols_returns_empty_without_request(provider):
    assert provider.get_etf_realtime([]) == []
    assert provider.get_etf_realtime(None) == []
    assert provider._fake.calls == []


def test_realtime_contract_slot_is_explicit_empty(provider, caplog):
    """本源不提供全市场快照: 显式空 + 告警, 让误路由可见。"""
    with caplog.at_level("WARNING"):
        out = provider.get_realtime()
    assert out == []
    assert any("不提供全市场" in r.message for r in caplog.records)


# ---------------------------------------------------------------- 能力声明


def test_config_declares_only_etf_dataset():
    """只声明 etf: realtime/daily 必须回退默认源, 不得抢占全市场轮询。"""
    p = PingAnProvider()
    assert p.name == "pingan"
    assert set(p.config.datasets) == {"etf"}
    for absent in ("realtime", "daily", "minute", "depth5", "financial", "adj_factor"):
        assert absent not in p.config.datasets


def test_provider_has_no_daily_implementation():
    """日K不实现 (平台只有前复权口径, 进库会污染 raw 基准)。"""
    p = PingAnProvider()
    assert not hasattr(p, "get_daily")


def test_test_dataset_reports_unsupported_as_error(provider):
    out = provider.test_dataset("daily")
    assert out["rows"] == 0
    assert "回退默认数据源" in out["error"]


def test_test_dataset_returns_preview_for_etf(provider):
    out = provider.test_dataset("etf", ["159682.SZ"])
    assert out["rows"] == 1
    assert "symbol" in out["columns"]
    assert out["preview"][0]["symbol"] == "159682.SZ"


# ---------------------------------------------------------------- 分片与限频


def test_batches_respect_max_codes(provider):
    provider.get_etf_realtime([f"15900{i}.SZ" for i in range(7)])
    assert [len(c) for c in provider._fake.calls] == [pc.MAX_CODES_PER_CALL, 2]


def test_chunked_helper():
    assert list(pc.chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(pc.chunked([], 2)) == []
    assert list(pc.chunked([1, 2], 0)) == [[1, 2]]


def test_on_chunk_done_reports_progress(provider):
    seen: list[tuple[int, int]] = []
    provider.get_etf_realtime(
        [f"15900{i}.SZ" for i in range(7)],
        on_chunk_done=lambda cur, total: seen.append((cur, total)),
    )
    assert seen == [(5, 7), (7, 7)]


# ---------------------------------------------------------------- Key 语义


def test_availability_two_states(monkeypatch):
    monkeypatch.setattr(pp, "get_api_key", lambda: "")
    ok, reason = pp.availability()
    assert ok is False
    assert pp.API_KEY_ENV in reason

    monkeypatch.setattr(pp, "get_api_key", lambda: "ak_x")
    assert pp.availability() == (True, "ok")


def test_probe_key_accepts_rate_limited_key(monkeypatch):
    """限流说明服务端已通过鉴权 → Key 有效, 不能判失败。"""
    class _Limited:
        def __init__(self, *a, **k):
            pass

        def quote(self, symbols):
            raise pc.PingAnRateLimitError("限流")

        def close(self):
            pass

    monkeypatch.setattr(pp, "PingAnClient", _Limited)
    ok, reason = pp.probe_api_key("ak_x")
    assert ok is True
    assert "限流" in reason


def test_probe_key_rejects_invalid(monkeypatch):
    class _Bad:
        def __init__(self, *a, **k):
            pass

        def quote(self, symbols):
            raise pc.PingAnAuthError("403")

        def close(self):
            pass

    monkeypatch.setattr(pp, "PingAnClient", _Bad)
    ok, _ = pp.probe_api_key("ak_bad")
    assert ok is False


def test_provider_requires_close():
    p = PingAnProvider()
    assert callable(p.close)
    p.close()  # 未建连时也不得抛异常


# ---------------------------------------------------------------- 异常分类


def test_client_rejects_empty_key():
    with pytest.raises(pc.PingAnError):
        pc.PingAnClient(api_key="", timeout=1.0)


def test_quota_error_is_not_rate_limit():
    """额度用尽重试无意义, 必须与限流分开 (否则白烧 3 次退避)。"""
    err = pc._rate_error({"error_code": "DAILY_QUOTA_EXCEEDED"}, 429)
    assert isinstance(err, pc.PingAnQuotaError)
    err2 = pc._rate_error({"error_code": "RATE_LIMIT_EXCEEDED"}, 429)
    assert isinstance(err2, pc.PingAnRateLimitError)


def test_quote_raises_on_malformed_items(monkeypatch):
    c = pc.PingAnClient(api_key="ak_x", timeout=1.0)
    monkeypatch.setattr(c, "_post_with_retry", lambda path, payload: {"items": "oops"})
    with pytest.raises(pc.PingAnError, match="items"):
        c.quote(["SZ159682"])


def test_provider_request_error_is_importable():
    """上层契约: 数据源明确失败用 ProviderRequestError (未被本插件滥用)。"""
    assert issubclass(ProviderRequestError, RuntimeError)
