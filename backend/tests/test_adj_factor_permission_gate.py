"""复权因子同步: 回退 TickFlow 前必须确认 TickFlow **自身**有权限。

回归背景 (2026-11-09 15:35 盘后管道):
adj_factor_provider = fuyao, 能力探测因"fuyao 声明了 adj_factor 数据集"而赋予
ADJ_FACTOR —— 于是 capset.has(Cap.ADJ_FACTOR) 为 **True**, 尽管 TickFlow 免费档
根本没有这个权限 (探测日志: "能力探测完成: adj_factor ✗ 无权限")。
fuyao 对 ETF 返回空后代码回退 TickFlow, 36 个分块全部 403, 1763/1763 只 ETF
一只没拿到。

危害不只是白跑: 那 36 次请求和 ETF 日K、股票日K 抢同一条 60rpm 通道,
是当天 ETF 日K被 429 打掉 11 批的直接原因。

所以判据必须是「TickFlow 自身有没有这个能力」, 不是「任一源能不能提供」。
"""
from __future__ import annotations

import pytest

from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _capset_with_adj() -> CapabilitySet:
    """模拟被自定义源增补过的能力集: 表面上有 ADJ_FACTOR。"""
    return CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits()})


@pytest.fixture
def tickflow_probe_denied(monkeypatch):
    """让 TickFlow 自身探测返回"无 adj_factor 权限"(免费档实况)。

    打补丁的是 policy._detect_tickflow_caps —— 即被测函数内部真正调用的
    探测入口, 而不是 _tickflow_has_adj_factor 本身, 否则测的就不是实现了。
    """
    from app.tickflow import policy

    monkeypatch.setattr(
        policy, "_detect_tickflow_caps", lambda force=False: CapabilitySet({}),
    )


@pytest.fixture
def tickflow_probe_allowed(monkeypatch):
    from app.tickflow import policy

    monkeypatch.setattr(
        policy, "_detect_tickflow_caps",
        lambda force=False: CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits()}),
    )


def test_has_adj_factor_ignores_custom_source_augmentation(
    monkeypatch, tickflow_probe_denied,
) -> None:
    """核心断言: capset 有 ADJ_FACTOR 但 TickFlow 自身没有 → 判 False。

    这条直接锁住 bug 的根因。修复前它返回 True (只看 capset), 于是回退打空请求。
    """
    assert _capset_with_adj().has(Cap.ADJ_FACTOR) is True   # 表面有
    assert kline_sync._tickflow_has_adj_factor(_capset_with_adj()) is False


def test_has_adj_factor_true_when_tickflow_really_permits(
    monkeypatch, tickflow_probe_allowed,
) -> None:
    """TickFlow 自身有权限时判 True (不得把正常付费用户挡掉)。"""
    assert kline_sync._tickflow_has_adj_factor(_capset_with_adj()) is True


def test_has_adj_factor_false_when_capset_lacks_it(monkeypatch) -> None:
    """capset 本身没有该能力 → 直接 False, 连探测都不做。"""
    called = {"n": 0}

    from app.tickflow import policy

    def _probe(force=False):
        called["n"] += 1
        return CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits()})

    monkeypatch.setattr(policy, "_detect_tickflow_caps", _probe)

    assert kline_sync._tickflow_has_adj_factor(CapabilitySet({})) is False
    assert called["n"] == 0


def test_has_adj_factor_fails_closed_on_probe_error(monkeypatch) -> None:
    """探测抛异常 → 按无权限处理 (fail-closed), 不得放行去打请求。"""
    from app.tickflow import policy

    def _boom(force=False):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(policy, "_detect_tickflow_caps", _boom)

    assert kline_sync._tickflow_has_adj_factor(_capset_with_adj()) is False
