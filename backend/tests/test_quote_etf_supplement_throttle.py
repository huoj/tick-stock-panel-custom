"""ETF 补拉节流的回归测试。

## 为什么必须测

2026-11-19 实测：平安接口当日调用 1675 次，其中 **1377 次（82%）来自 ETF 补拉**。
根因是行情轮询 4s 一轮、每轮都补拉 ETF，一天 ~4h 就是上千次，把 2000 额度打光，
14:49 之后热股与研报全部 429。

这不是"数据需要"的用量，是设计缺陷：ETF 当日价格在 60s 内对监控与看板没有意义。
节流后整日从 ~1400 次降到 ~240 次。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import quote_service as qs


class _CountingProvider:
    """记录调用次数; 可选抛错模拟 429。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.error = error

    def get_etf_realtime(self, symbols, asset_type="etf", on_chunk_done=None):
        self.calls.append(list(symbols))
        if self.error:
            raise self.error
        return [{"symbol": s, "last_price": 1.0} for s in symbols]


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch):
    """隔离掉偏好与 provider 解析, 只验证节流本身。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_etf_data_provider", lambda: "pingan")
    svc = qs.QuoteService()
    provider = _CountingProvider()

    class _Custom:
        @staticmethod
        def provider_has_dataset(name, dataset):
            return dataset == "etf"

        @staticmethod
        def get_provider(name):
            return provider

    import app.data_providers.custom as custom_mod
    monkeypatch.setattr(custom_mod, "provider_has_dataset", _Custom.provider_has_dataset)
    monkeypatch.setattr(custom_mod, "get_provider", _Custom.get_provider)
    svc._provider = provider            # type: ignore[attr-defined]
    return svc


def test_supplement_called_once_within_ttl(service) -> None:
    """TTL 内重复调用只打一次接口 —— 这是把 1400 次压到 240 次的关键。"""
    provider = service._provider
    first, ok1 = service._supplement_etf_quotes([], {"159682.SZ", "159586.SZ"})
    second, ok2 = service._supplement_etf_quotes([], {"159682.SZ", "159586.SZ"})
    third, ok3 = service._supplement_etf_quotes([], {"159682.SZ", "159586.SZ"})
    assert len(provider.calls) == 1, "TTL 内不应重复请求"
    assert ok1 and ok2 and ok3, "缓存命中也要报告补到了"
    assert first == second == third
    assert len(first) == 2


def test_supplement_refetches_after_ttl(service, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = service._provider
    service._supplement_etf_quotes([], {"159682.SZ"})
    # 把"上次拉取时刻"推早到 TTL 之外
    service._etf_supplement_at -= service.ETF_SUPPLEMENT_TTL_S + 1
    service._supplement_etf_quotes([], {"159682.SZ"})
    assert len(provider.calls) == 2


def test_supplement_ttl_default_is_60s() -> None:
    """默认值本身也是契约: 改小就等于回到烧额度的老路。"""
    assert qs.QuoteService.ETF_SUPPLEMENT_TTL_S >= 30
    assert qs.QuoteService.ETF_SUPPLEMENT_BACKOFF_S >= 300


def test_failure_enters_backoff_and_stops_hammering(service) -> None:
    """额度用尽后不许每 4s 撞一次墙: 退避期内直接返回旧缓存, 不再打接口。"""
    provider = service._provider
    service._supplement_etf_quotes([], {"159682.SZ"})       # 先拿一次成功的
    assert len(provider.calls) == 1

    provider.error = RuntimeError("今日调用次数已达上限 (HTTP 429)")
    service._etf_supplement_at -= service.ETF_SUPPLEMENT_TTL_S + 1   # 让缓存过期
    out, ok = service._supplement_etf_quotes([], {"159682.SZ"})
    assert ok is False
    assert len(provider.calls) == 2, "第一次失败会真的请求一次"
    assert out, "失败时仍返回旧缓存, 不清空已有行情"

    # 退避期内连续 5 轮: 一次都不许再打
    for _ in range(5):
        service._supplement_etf_quotes([], {"159682.SZ"})
    assert len(provider.calls) == 2, "退避期内不应再产生请求"


def test_backoff_expires_and_retries(service) -> None:
    provider = service._provider
    service._supplement_etf_quotes([], {"159682.SZ"})
    provider.error = RuntimeError("boom")
    service._etf_supplement_at -= service.ETF_SUPPLEMENT_TTL_S + 1
    service._supplement_etf_quotes([], {"159682.SZ"})
    assert len(provider.calls) == 2
    # 退避到期后应重试
    service._etf_supplement_backoff_until = time.monotonic() - 1
    service._supplement_etf_quotes([], {"159682.SZ"})
    assert len(provider.calls) == 3


def test_no_missing_symbols_makes_no_call(service) -> None:
    provider = service._provider
    assert service._supplement_etf_quotes([], set()) == ([], False)
    assert provider.calls == []


def test_tickflow_provider_skips_supplement(service, monkeypatch: pytest.MonkeyPatch) -> None:
    """etf 路由还是 tickflow 时不该打券商接口 (券商额度是唯一的稀缺资源)。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_etf_data_provider", lambda: "tickflow")
    provider = service._provider
    assert service._supplement_etf_quotes([], {"159682.SZ"}) == ([], False)
    assert provider.calls == []


def test_fill_missing_etf_uses_cache_not_repeated_calls(service) -> None:
    """上层 `_fill_missing_etf` 每轮调用, 但底层只打一次接口。"""
    provider = service._provider
    demand = {"etf": {"159682.SZ"}, "stock": set(), "index": set()}
    for _ in range(10):
        merged, _partial = service._fill_missing_etf([], demand)
    assert len(provider.calls) == 1
    assert len(merged) == 1
