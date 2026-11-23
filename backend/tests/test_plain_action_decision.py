"""白话动作映射审计测试 —— 防止「买点被报成止损」这类方向性错误回归。

背景: 告警卡给入门投资者一句「现在该做什么」的白话动作。第一版从 message
自由文本猜关键词, 结果 ll_buy2(买第2批)的消息里顺带写了"跌破12.20止损",
被误判成"该止损了" —— 买点报成止损, 方向完全反了。

修复: 两套映射都只认结构化标识(TSP 认 rule_id, watch 认 kind+方向), 不读
自由文本。本测试把全套规则/信号类型的正确动作钉死, 任何人改成"猜文本"或
调错优先级都会被拦下。
"""
from __future__ import annotations

import pytest

from app.services.webhook_adapter import _plain_action as tsp_action


# ── TSP: 按 rule_id 的命名契约映射 ────────────────────────────────
@pytest.mark.parametrize(
    "rule_id, expected",
    [
        # 真止损: 一律"减仓", 不补仓不硬扛
        ("hk_000333_sz_stop", "该止损了"),
        ("hk_605366_sh_stop", "该止损了"),
        ("hk_159529_sz_stop", "该止损了"),
        ("ll_stop", "该止损了"),
        # 买入档: 绝不能被消息里的"止损"字样污染
        ("ll_buy1", "到买入价位"),
        ("ll_buy2", "到买入价位"),  # 血泪回归点: message 含"跌破12.20止损"
        ("cm_buy_97", "到买入价位"),
        ("cm_deep_95", "到买入价位"),  # pa_rule 之外的 _deep 才是买点
        # 上沿减仓: 绝不能被消息里的"突破"字样带偏
        ("up_000651_40_1", "涨到减仓档"),  # 血泪回归点: message 含"向上突破观察线"
        ("up_000333_90_5", "涨到减仓档"),
        # 突破 / 左侧 / 急跌 / 风险线
        ("br_605366_8_2", "放量突破"),
        ("ll_break", "放量突破"),
        ("lz_605366_7_1_7_3", "进入左侧观察区"),
        ("ll_dump", "急跌放量"),
        ("cm_risk_92", "跌破风险线"),
        # 板块/指数异动
        ("sec_7665988545", "板块或大盘异动"),
        ("idx_000300_sh_dn", "板块或大盘异动"),
        # 日内涨跌
        ("hk_000333_sz_drop", "日内跌幅"),
        ("hk_000333_sz_rise", "日内涨幅"),
        # 平安策略信号: pa_rule_ 优先级最高, etf_deep_value 含 _deep 也不能误吞成买入
        ("pa_rule_etf_deep_value", "策略信号"),
        ("pa_rule_hot_rising", "策略信号"),
    ],
)
def test_tsp_plain_action_by_rule_id(rule_id, expected):
    assert expected in tsp_action({"rule_id": rule_id})


def test_tsp_plain_action_ignores_message_text():
    """铁证: 即使 message 写满"止损", 买点规则也不能被带偏。"""
    buy = tsp_action({"rule_id": "ll_buy2", "message": "跌破12.20止损", "severity": "critical"})
    assert "止损" not in buy
    assert "买入" in buy


def test_tsp_plain_action_no_guess_for_unknown():
    assert tsp_action({"rule_id": "totally_new_rule_xyz"}) == ""


# ── watch.py: 按 kind + 方向映射 ──────────────────────────────────
# watch.py 是独立部署的 DSH 脚本, 不在本仓库 sys.path; 用 importlib 按路径加载。
# 路径通过环境变量 WATCH_CARD_PATH 提供, 未配置则跳过这些测试。
import importlib.util
import os
from pathlib import Path

_WATCH_CARD = Path(os.environ.get("WATCH_CARD_PATH", ""))


@pytest.fixture(scope="module")
def watch_action():
    import sys
    if not os.environ.get("WATCH_CARD_PATH", "") or not _WATCH_CARD.is_file():
        pytest.skip("WATCH_CARD_PATH 未配置或文件不存在(非 watch.py 部署环境)")
    # watch_card.py 顶层 import 同目录的 feishu_notify, 把其目录临时加进 sys.path
    watch_dir = str(_WATCH_CARD.parent)
    added = watch_dir not in sys.path
    if added:
        sys.path.insert(0, watch_dir)
    try:
        spec = importlib.util.spec_from_file_location("_watch_card_under_test", _WATCH_CARD)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._plain_action
    finally:
        if added:
            sys.path.remove(watch_dir)


@pytest.mark.parametrize(
    "sig, expected",
    [
        ({"kind": "reversal"}, "止跌反弹"),
        ({"kind": "breakout"}, "突破到位"),
        ({"kind": "level", "tone": "green"}, "到买入区"),   # 向下跌到 = 买档
        ({"kind": "level", "tone": "red"}, "涨到减仓档"),   # 向上涨到 = 减仓档
        ({"kind": "fast", "pct": 0.006}, "急涨"),
        ({"kind": "fast", "pct": -0.006}, "急跌"),
        ({"kind": "day", "pct": 0.09}, "涨得猛"),
        ({"kind": "day", "pct": -0.14}, "大跌"),
    ],
)
def test_watch_plain_action_by_kind(watch_action, sig, expected):
    assert expected in watch_action(sig)


def test_watch_plain_action_ignores_headline_text(watch_action):
    """买档信号即使 headline 写满"止损", 也按 kind 判为买入。"""
    got = watch_action({
        "kind": "level", "tone": "green",
        "headline": "跌到6.40 跌破止损线", "reason": "止损", "severity": "P0",
    })
    assert "买入" in got
    assert "止损" not in got


def test_watch_plain_action_no_kind_no_guess(watch_action):
    assert watch_action({"headline": "大跌 9%", "severity": "P0"}) == ""
