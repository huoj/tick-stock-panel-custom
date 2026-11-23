"""平安数据接入 2026-11-19 审查发现的 4 个测试盲区回归用例。

这 4 个 bug 在原有 232 个相关测试全绿的情况下依然存在 —— 测试只覆盖了
"正常命中/抛异常" 两条路, 漏掉了: 差额回填、空返回、半截数据、回补 data_dir。
本文件把它们固化下来, 防止再犯。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import ext_pull, preferences, quote_service as qs


class _Provider:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.result: list[dict] = []
        self.complete = True

    def get_etf_realtime(self, symbols, asset_type="etf", on_chunk_done=None):
        self.calls.append(list(symbols))
        return self.result

    @property
    def last_etf_fetch_complete(self):
        return self.complete


@pytest.fixture()
def svc(monkeypatch: pytest.MonkeyPatch):
    import app.data_providers.custom as custom_mod

    monkeypatch.setattr(preferences, "get_etf_data_provider", lambda: "pingan")
    monkeypatch.setattr(custom_mod, "provider_has_dataset", lambda name, ds: ds == "etf")
    p = _Provider()
    monkeypatch.setattr(custom_mod, "get_provider", lambda name: p)
    service = qs.QuoteService()
    service._provider = p  # type: ignore[attr-defined]
    return service, p


_DEMAND_AB = {"etf": {"A.SZ", "B.SZ"}, "stock": set(), "index": set()}


def test_ttl_hit_only_backfills_missing_difference_not_stale_full_snapshot(svc) -> None:
    """BUG-A: 主源已覆盖的 symbol 不得被旧缓存重复行覆盖 (差额回填)。"""
    service, p = svc
    p.result = [{"symbol": "A.SZ", "last_price": 1.0}, {"symbol": "B.SZ", "last_price": 1.0}]
    service._fill_missing_etf([], _DEMAND_AB)                       # R1 全缺
    merged, _ = service._fill_missing_etf(
        [{"symbol": "A.SZ", "last_price": 9.9}], _DEMAND_AB          # R2 主源已覆盖 A.SZ
    )
    a = [r for r in merged if r["symbol"] == "A.SZ"]
    b = [r for r in merged if r["symbol"] == "B.SZ"]
    assert len(a) == 1 and a[0]["last_price"] == 9.9, f"新价必须唯一且不被旧价覆盖: {a}"
    assert len(b) == 1, f"缓存差额只补 B.SZ 一条: {b}"
    assert len(p.calls) == 1, "R2 应命中 TTL 缓存, 不再请求"


def test_empty_result_enters_backoff_and_keeps_old_cache(svc) -> None:
    """BUG-B: 空返回 (软失败) 必须退避, 否则每轮清空缓存并重置 TTL, 额度照烧。"""
    service, p = svc
    p.result = []
    demand = {"etf": {"A.SZ"}, "stock": set(), "index": set()}
    for _ in range(20):
        service._fill_missing_etf([], demand)
    assert len(p.calls) == 1, f"空返回 20 轮应只打 1 次请求, 实际 {len(p.calls)}"
    assert service._etf_supplement_backoff_until > 0, "空返回应设置退避"


def test_partial_data_flagged_by_provider_enters_backoff(svc) -> None:
    """BUG-C: provider 报告不完整 (限流后半截数据) 不得当完整成功缓存。"""
    service, p = svc
    p.result = [{"symbol": "A.SZ", "last_price": 1.0}]
    p.complete = False
    _, partial = service._fill_missing_etf([], _DEMAND_AB)
    assert partial == {"etf"}, "半截数据必须标记 partial"
    assert service._etf_supplement_backoff_until > 0, "半截数据必须进退避"
    service._fill_missing_etf([], _DEMAND_AB)
    assert len(p.calls) == 1, "退避期内不得再请求"


def test_backfill_adapter_passes_data_dir_but_plain_pull_does_not() -> None:
    """BUG-D: 回补路径 adapter 需 data_dir 读自选/持仓; 普通 pull 保持恒全量语义。"""
    from app.services.etf_valuation import preset

    day = date(2026, 11, 18)
    received: dict = {}

    async def fake_fetch(config, target_date, data_dir=None):
        received["data_dir"] = data_dir
        return []

    cfg = preset()
    cfg.pull.date_param = "date"
    cfg.pull.adapter = {"kind": "pingan_etf_valuation"}  # 确保是 adapter 配置
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ext_pull, "fetch_rows_for_date", fake_fetch), \
             patch("app.services.dragon_tiger._local_trading_days", lambda data_dir: [day]):
            asyncio.run(ext_pull._backfill_history(cfg, td, day, day))
    assert received.get("data_dir") == td, "adapter 回补必须透传 data_dir"

    cfg.pull.adapter = None
    with tempfile.TemporaryDirectory() as td:
        with patch.object(ext_pull, "fetch_rows_for_date", fake_fetch), \
             patch("app.services.dragon_tiger._local_trading_days", lambda data_dir: [day]):
            asyncio.run(ext_pull._backfill_history(cfg, td, day, day))
    assert received.get("data_dir") is None, "普通 pull 不应传 data_dir"
