"""冻结的市场研究结论 —— 行动卡「规则有效性」的默认诚实性披露。

## 这个模块为什么存在

``action_card.build_inference`` 早就支持 ``validation=`` 参数：传入跨样本
检验结果后，会把「这套规则在历史上到底有没有用」如实写进 confidence，并对
``inverted`` 档强制压到 ``very_low`` + 插入反向警告。但默认行动卡路径
（``api/action_card.get_action_card``）从来没传过这个参数 —— 于是整条
「检验 → 降置信 → 前端红字」链路在真实产品里是**死代码**：用户看到的
「偏强 / 偏弱」是无保留判断，而后台研究早就发现它系统性写反了。

本模块把 2026-11-13 完成的全市场研究结论**冻结**成一份带日期、带范围、
带显著性、带样本外检验的结构化结论，由 ``build_action_card`` 在未显式传
``validation`` 时默认注入。这样：

1. 「默认卡片必带诚实性披露」变成卡片本身的属性，覆盖**所有**调用方
   （API 与测试都在内），一处改动即彻底修掉死代码；
2. 结论是**全市场研究结论、非本票单独检验** —— note 与 basis 都写明，
   不把群体统计伪装成个股诊断；
3. 仍允许调用方显式传 ``validation=`` 覆盖（例如未来接单票 live 检验）。

## 数据来源（全部可复现）

- ``docs/强弱规则反向归因.md``：四假说裁决 + 全部数字。
- ``docs/强弱规则反向归因.json``：A~I 九段分析，``I_online_crosscheck`` 是
  与线上 ``validate_across_symbols`` 同批面板逐点对照的结果。
- ``docs/强弱规则反向_OOS.json``：按标的 / 按时间各切两半的样本外检验。
- 复现脚本：``backend/scripts/diagnose_strength_inversion.py`` 与
  ``backend/scripts/oos_strength_check.py``（只读本地 parquet，不改生产代码）。

## 早期结论的纠正（load-bearing）

曾宣称「76 只票 / 2754 采样点 / 20 日跑输基准 9.1pp 且三期方向一致」。
在最终 74 只票 / 2660 采样点上复算：**反向只在 5 日 / 10 日显著**
（block bootstrap p=0.015 / 0.008），20 日 -2.8pp / p=0.345 **不显著**。
因此这里只把 5/10 日作为结论，绝不复用「20 日 / 9.1pp / 三期一致」的说法。

## 刻意不改的东西

**不修改 ``judge_strength``、不翻转 d5（收盘位置）判据。** 归因显示 d5
方向写反、翻转后 strong 档由反向翻正向，但时间维样本外检验里 d5− 那一半
会衰减；更重要的是改了就无法分辨「修对了」还是「过拟合」。当前只做诚实性
披露 + 留可复现的研究结论，把「改不改规则」留作独立决策。
"""
from __future__ import annotations

from typing import Any

# ── 研究基线（2026-11-13 冻结；数字来自上述 json / md） ──────────────
STUDY_DATE = "2026-11-13"
SYMBOLS_USED = 74
SAMPLED_POINTS = 2660
DATE_FROM = "2025-04-18"
DATE_TO = "2026-10-16"
FORWARD_WINDOWS = (5, 10, 20)
STUDY_SOURCE = "docs/强弱规则反向归因.md"

# 全样本无条件基准（胜率 %）—— 没有基准的胜率是骗人的。
_BASELINE = {
    "5": {"mean_pct": -0.16, "median_pct": -0.34, "win_rate": 46.8, "samples": SAMPLED_POINTS},
    "10": {"mean_pct": 0.30, "median_pct": -0.27, "win_rate": 48.3, "samples": SAMPLED_POINTS},
    "20": {"mean_pct": 0.42, "median_pct": -0.81, "win_rate": 46.8, "samples": SAMPLED_POINTS},
}

_SCOPE_NOTE = (
    f"全市场研究结论（{STUDY_DATE}，{SYMBOLS_USED} 只票 / {SAMPLED_POINTS} 采样点，"
    f"{DATE_FROM}→{DATE_TO}），不是本只股票单独检验。"
)


def load_market_validation() -> dict[str, Any]:
    """返回与 ``strength_validation.validate_across_symbols`` 同形的研究结论。

    每次调用构造**全新** dict，避免调用方就地修改污染后续卡片。

    ``basis`` 是本模块新增的可选字段，供 ``build_inference`` 透传到
    ``inference.rule_validation``，让前端能显示研究日期与范围，而不是让用户
    误以为这是「刚刚对这只票跑出来的实时检验」。
    """
    groups: list[dict[str, Any]] = [
        {
            "stance": "strong",
            "label": "偏强",
            "samples": 293,
            "forward": {"5": -6.9, "10": -7.3, "20": -2.8},  # 相对基准的超额胜率 pp
            "verdict": "inverted",
            "note": (
                f"{_SCOPE_NOTE} 被判「偏强」的样本前瞻 5 日 / 10 日分别跑输基准 "
                "6.9pp / 7.3pp（按标的整块 bootstrap p=0.015 / 0.008，显著）；"
                "20 日 -2.8pp / p=0.345 不显著。归因：净票高度绑定「近期已涨」，"
                "且「收盘位置≥80%分位」这条判据方向写反（样本外按标的与按时间"
                "两维留出均稳健复现）。本档是**反向指标**，只能当事实摘要，"
                "不要当交易信号。"
            ),
        },
        {
            "stance": "weak",
            "label": "偏弱",
            "samples": 1024,
            "forward": {"5": 3.1, "10": 0.1, "20": -0.2},
            "verdict": "no_edge",
            "note": (
                f"{_SCOPE_NOTE} 被判「偏弱」的样本与全样本基准无显著差异"
                "（5/10/20 日超额 +3.1 / +0.1 / -0.2pp，未过 5pp 门槛且方向不一致）。"
                "这一档不具备预测力，不该被当决策依据。"
            ),
        },
        {
            "stance": "unclear",
            "label": "方向不明",
            "samples": 1343,
            "forward": {"5": -0.9, "10": 1.5, "20": 0.7},
            "verdict": "validated",
            "note": (
                f"{_SCOPE_NOTE} 「方向不明」档与基准偏离≤1.8pp，与「证据分歧、"
                "不下定论」的语义一致。这一档只说明证据相互矛盾，本身也不是买入依据。"
            ),
        },
    ]

    conclusions = [
        (
            "「偏强」档在 5/10 日显著跑输基准（inverted）；早期宣称的「20 日 -9.1pp、"
            "三期一致」在最终样本上不成立，20 日不显著（p=0.345）。"
        ),
        (
            "反向来源可拆解为：净票与「近期已涨」高度相关（corr(net, past20)=0.453）+ "
            "「收盘位置≥80%分位→+1」判据方向写反 + 放量上涨的短期追高；"
            "不是全样本单调反转、不是单期行情、也不是头顶筹码维度。"
        ),
        (
            "样本外检验：按标的切两半 all_pass=true；按时间切两半，"
            "「高位收盘追高→跑输」稳健复现，但「低位收盘必涨」那一半在后半段衰减。"
            "因此只做诚实性披露，不翻转判据、不改 judge_strength。"
        ),
        (
            "用法红线：本结论是全市场群体统计，不能替代单票独立检验；"
            "行动卡把「偏强/偏弱」当作**事实摘要**展示，不作为买卖信号。"
        ),
    ]

    return {
        "symbols_used": SYMBOLS_USED,
        "symbols_total": SYMBOLS_USED,
        "baseline": {k: dict(v) for k, v in _BASELINE.items()},
        "groups": groups,
        "conclusions": conclusions,
        "per_symbol": [],
        "note": (
            f"{_SCOPE_NOTE} 由 {STUDY_SOURCE} 冻结，供默认行动卡披露；"
            "单票独立检验请用 /api/action-card/{symbol}/validate-strength。"
        ),
        "basis": {
            "study_date": STUDY_DATE,
            "symbols_used": SYMBOLS_USED,
            "sampled_points": SAMPLED_POINTS,
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "forward_windows": list(FORWARD_WINDOWS),
            "source": STUDY_SOURCE,
            "scope": "market_research",  # 与 live 单票检验区分
            "title": "全市场历史检验（非本票）",
        },
    }


__all__ = [
    "DATE_FROM",
    "DATE_TO",
    "SAMPLED_POINTS",
    "STUDY_DATE",
    "STUDY_SOURCE",
    "SYMBOLS_USED",
    "load_market_validation",
]
