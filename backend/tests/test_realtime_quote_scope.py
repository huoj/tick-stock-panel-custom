"""实时行情范围偏好: ETF 实时拉取默认开启, 但已保存的显式选择优先。

背景 (2026-11-09): realtime_pull_etf 原默认 False「老用户兼容」, 结果自选里
ETF 行情永远停在上一交易日 —— 涨跌幅被误读成当日值, 且响应的全局 as_of
(三类资产取最小) 会倒退, 连带拖停股票当日蜡烛的实时补画。

改为默认开启后仍须保证: 在设置里显式关掉 ETF 实时的用户不被默认值翻转
(改默认值只影响从未配置过该项的库)。
"""
from __future__ import annotations

import json

import pytest

from app.services import preferences


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    """把 preferences 落盘位置指到临时目录。

    必须 patch `_path()` (save/load 都经它取路径), 并清掉内存缓存 ——
    否则 load() 会命中真实 preferences.json 的 mtime 缓存, 测试会读写用户配置。
    """
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    monkeypatch.setattr(preferences, "_cache", None)
    monkeypatch.setattr(preferences, "_cache_sig", None)
    yield path
    # 恢复真实日志侧缓存状态, 避免后续测试拿到临时目录的快照
    monkeypatch.setattr(preferences, "_cache", None)
    monkeypatch.setattr(preferences, "_cache_sig", None)


def test_etf_realtime_on_by_default(prefs_file) -> None:
    """从未配置过该项 (新建库 / 升级前没存过) → 默认开启。"""
    assert not prefs_file.exists()
    assert preferences.get_realtime_pull_etf() is True


def test_explicit_false_is_respected(prefs_file) -> None:
    """显式关过 ETF 实时的用户: 改默认值不得把它翻回开启。"""
    preferences.save({"realtime_pull_etf": False})

    assert preferences.get_realtime_pull_etf() is False
    # 落盘确实只写进临时文件
    assert json.loads(prefs_file.read_text(encoding="utf-8"))["realtime_pull_etf"] is False


def test_explicit_true_is_respected(prefs_file) -> None:
    preferences.save({"realtime_pull_etf": True})

    assert preferences.get_realtime_pull_etf() is True


def test_scope_reports_effective_values(prefs_file) -> None:
    """设置页读的 scope 与 getter 必须同源, 否则界面显示与实际拉取不一致。"""
    scope = preferences.get_realtime_quote_scope()

    assert scope["realtime_pull_etf"] is True
    assert scope["realtime_pull_stock"] is True

    preferences.set_realtime_quote_scope({"realtime_pull_etf": False})
    assert preferences.get_realtime_quote_scope()["realtime_pull_etf"] is False
