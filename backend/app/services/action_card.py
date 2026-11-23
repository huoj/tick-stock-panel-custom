"""行动卡 —— 一只票一张卡, 回答四个问题, 并把三类信息分开。

## 用户要求 (2026-11-13)

「普通用户通常不需要几十个指标, 而需要快速回答:
- 这只股票当前偏强、偏弱还是方向不明?
- 我持有时, 什么情况说明原判断可能错了?
- 我没买时, 哪些条件出现后再考虑?
- 财报、公告或告警里有什么必须核对的事实?
行动卡应把「事实、系统推断、用户计划」明显区分, 不能只给一个买入/卖出分数。」

## 三段式设计 (这是本模块存在的主要理由)

| 段 | 内容 | 权威性 |
|---|---|---|
| ``facts`` | 可独立核对的原始数值 | 系统直接测得, 不解释 |
| ``inference`` | 系统基于事实的判断与概率 | 有前提、有样本量、可被推翻 |
| ``user_plan`` | 用户自己填的成本/期限/亏损容忍/计划 | 用户权威, 系统只做对照 |

**绝不合并成一个"买入/卖出分数"**: 分数不可解释、无法追溯、掩盖分歧。
三段各自带 ``confidence`` 与 ``caveats``, 让用户看到「系统在哪儿不确定」。

## 回答四个问题

1. 偏强/偏弱 → ``strength`` (来自 stock_diagnostics.judge_strength 的证据投票)
2. 持有时什么算判断错了 → ``invalidation`` (带具体价位与触发条件)
3. 没买时什么条件再考虑 → ``entry_conditions`` (带具体价位与量能条件)
4. 必须核对的事实 → ``checks`` (财报/公告/告警里需要人工确认的条目)

2 和 3 只陈述**条件**, 不陈述**建议**。系统说"跌破 7.11 说明判断可能错了",
不说"你应该卖"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import polars as pl

from app.services import position_plan, strength_research
from app.services import stock_diagnostics as diag
from app.services.fundamentals_digest import checks_from_digest as digest_checks

logger = logging.getLogger(__name__)

# 三段式标签 (前端直接展示)
SECTION_LABELS = {
    "facts": "事实（可独立核对）",
    "inference": "系统推断（有前提，可被推翻）",
    "user_plan": "你的计划（你填的，系统只做对照）",
}

# 问题 → 对应卡字段
QUESTION_MAP = {
    "偏强偏弱": "strength",
    "持有时什么算判断错了": "invalidation",
    "没买时什么条件再考虑": "entry_conditions",
    "必须核对的事实": "checks",
}


# ── 结构 ────────────────────────────────────────────────

@dataclass
class InvalidationRule:
    """「原判断可能错了」的具体触发条件 —— 必须带价位与含义。"""

    condition: str
    price: float | None
    meaning: str
    source: str          # 来自哪个事实 (技术位/成本/计划亏损容忍)
    severity: str        # warning | critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "price": round(self.price, 2) if self.price else None,
            "meaning": self.meaning,
            "source": self.source,
            "severity": self.severity,
        }


@dataclass
class EntryCondition:
    """「什么条件出现后再考虑」—— 同样是条件, 不是建议。"""

    condition: str
    price: float | None
    rationale: str
    kind: str            # breakout | pullback | volume | valuation

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "price": round(self.price, 2) if self.price else None,
            "rationale": self.rationale,
            "kind": self.kind,
        }


@dataclass
class CheckItem:
    """必须人工核对的事实 —— 系统给不了答案的那些。"""

    item: str
    why: str
    where: str
    status: str = "unverified"     # unverified | checked | unavailable

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "why": self.why,
            "where": self.where,
            "status": self.status,
        }


@dataclass
class ActionCard:
    symbol: str
    name: str = ""
    state: str = position_plan.STATE_WATCH
    state_label: str = ""
    as_of: str = ""

    # 三段
    facts: dict[str, Any] = field(default_factory=dict)
    inference: dict[str, Any] = field(default_factory=dict)
    user_plan: dict[str, Any] = field(default_factory=dict)

    # 四个问题
    strength: dict[str, Any] = field(default_factory=dict)
    invalidation: list[InvalidationRule] = field(default_factory=list)
    entry_conditions: list[EntryCondition] = field(default_factory=list)
    checks: list[CheckItem] = field(default_factory=list)

    # 基本面摘要 (财报 + 公告自动解析) —— 有数据时填, 无数据时为 available=False
    fundamentals: dict[str, Any] = field(default_factory=dict)

    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "state": self.state,
            "state_label": self.state_label,
            "as_of": self.as_of,
            "sections": SECTION_LABELS,
            "questions": QUESTION_MAP,
            "facts": self.facts,
            "inference": self.inference,
            "user_plan": self.user_plan,
            "strength": self.strength,
            "invalidation": [r.to_dict() for r in self.invalidation],
            "entry_conditions": [c.to_dict() for c in self.entry_conditions],
            "checks": [c.to_dict() for c in self.checks],
            "fundamentals": self.fundamentals,
            "caveats": self.caveats,
        }


# ── 构建各段 ────────────────────────────────────────────

def build_facts(diagnosis: dict[str, Any], holding: dict | None) -> dict[str, Any]:
    """事实段: 只放可独立核对的数值, 不做任何解释。"""
    cp = diagnosis["candle"]
    tr = diagnosis["trend"]
    ind = diagnosis["indicators"]
    ch = diagnosis["chips"]
    kl = diagnosis["levels"]

    facts: dict[str, Any] = {
        "price": cp["close"],
        "date": cp["date"],
        "change_pct": cp["change_pct"],
        "open": cp["open"],
        "high": cp["high"],
        "low": cp["low"],
        "close_position_pct": cp["close_position"],
        "body_ratio_pct": cp["body_ratio"],
        "volume": cp["volume"],
        "volume_ratio_pct": cp["volume_ratio"],
        "mas": tr["mas"],
        "ma_deviation_pct": tr["deviation_pct"],
        "high_250": tr["high_250"],
        "low_250": tr["low_250"],
        "position_in_250_pct": tr["position_in_250_pct"],
        "rsi14": ind["rsi14"],
        "macd_hist": ind["macd_hist"],
        "boll": {"upper": ind["boll_upper"], "mid": ind["boll_mid"], "lower": ind["boll_lower"]},
        "atr14": ind["atr14"],
        "atr14_pct": ind["atr14_pct"],
        "vol_20d_annual_pct": ind["vol_20d_annual"],
        "vol_60d_annual_pct": ind["vol_60d_annual"],
        "key_levels": {
            "high_20": kl["high_20"],
            "low_20": kl["low_20"],
            "high_60": kl["high_60"],
            "low_60": kl["low_60"],
        },
        "chip_densest_above": ch["densest_above"],
        "chip_densest_below": ch["densest_below"],
    }
    if holding:
        facts["holding"] = {
            "qty": holding.get("qty"),
            "avg_cost": holding.get("avg_cost"),
            "market_value": holding.get("market_value"),
            "weight": holding.get("weight"),
            "pnl": holding.get("pnl"),
            "pnl_pct": holding.get("pnl_pct"),
            "entry_date": holding.get("entry_date"),
            "holding_days": holding.get("holding_days"),
        }
    return facts


def build_inference(
    diagnosis: dict[str, Any],
    *,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """推断段: 判断 + 概率 + 前提。每条都标 confidence。

    ``validation`` 是 strength_validation 的跨样本检验结果。有它时, 必须把
    "这套规则在历史上到底有没有用"如实写进 confidence —— 2026-11-13 的
    全市场研究 (74 只票 / 2660 采样点) 发现「偏强」档 5/10 日显著跑输基准
    (verdict=inverted)。若还按证据条数报高 confidence, 就是在骗用户。
    参见 docs/强弱规则反向归因.md。
    """
    analogs = diagnosis["analogs"]
    empty_inference: dict[str, Any] = {
        "strength_stance": diagnosis["strength"]["stance"],
        "strength_label": diagnosis["strength"]["label"],
        "bullish_evidence": diagnosis["strength"]["bullish_evidence"],
        "bearish_evidence": diagnosis["strength"]["bearish_evidence"],
        "neutral_evidence": diagnosis["strength"]["neutral_evidence"],
        "analog_pattern": analogs["pattern"],
        "analog_sample_size": analogs["sample_size"],
        "forward_stats": {},
        "baseline_stats": analogs["baseline_stats"],
        "confidence": "low",
        "caveats": list(analogs["warnings"]),
    }

    # 规则有效性的自我披露 —— 这一项会强制下调 confidence
    rule_check: dict[str, Any] | None = None
    confidence_cap: str | None = None
    if validation:
        stance = diagnosis["strength"]["stance"]
        group = next(
            (g for g in validation.get("groups") or [] if g.get("stance") == stance),
            None,
        )
        verdict = (group or {}).get("verdict")
        rule_check = {
            "stance": stance,
            "verdict": verdict,
            "samples": (group or {}).get("samples"),
            "note": (group or {}).get("note"),
            "conclusions": validation.get("conclusions") or [],
            # 冻结研究结论带 basis（日期/范围/scope），让前端说明"这不是本票实时检验"。
            # live 单票检验没有 basis 时保持 None，不影响既有行为。
            "basis": validation.get("basis"),
        }
        if verdict == "inverted":
            confidence_cap = "very_low"
            empty_inference["caveats"].insert(
                0,
                "⚠ 历史检验显示：本档判定在样本上是**反向**指标 —— "
                f"被判为「{diagnosis['strength']['label']}」的标的后续反而跑输基准。"
                "请把这条判定当作事实摘要，不要当交易信号。",
            )
        elif verdict == "no_edge":
            confidence_cap = "low"
            empty_inference["caveats"].insert(
                0,
                "历史检验显示：本档判定与基准无显著差异，不具备预测力。",
            )
        elif verdict == "insufficient":
            confidence_cap = "low"
            empty_inference["caveats"].insert(
                0,
                "历史检验样本不足，无法判断这套判定是否有效 —— "
                "「没检验出效果」不等于「有效」。",
            )
    empty_inference["rule_validation"] = rule_check

    if not analogs["sample_size"]:
        if confidence_cap:
            empty_inference["confidence"] = confidence_cap
            empty_inference["confidence_basis"] = "形态样本为空，且规则有效性检验限制了置信度"
        return empty_inference

    # 只放有样本的窗口, 并显式对比基准 —— 没有基准的胜率是骗人的
    forward: dict[str, Any] = {}
    for w, stats in analogs["pattern_stats"].items():
        base = analogs["baseline_stats"].get(w, {})
        if not stats:
            continue
        edge = None
        if stats.get("win_rate") is not None and base.get("win_rate") is not None:
            edge = stats["win_rate"] - base["win_rate"]
        forward[w] = {
            "win_rate": stats.get("win_rate"),
            "mean_pct": stats.get("mean_pct"),
            "median_pct": stats.get("median_pct"),
            "baseline_win_rate": base.get("win_rate"),
            "baseline_mean_pct": base.get("mean_pct"),
            # 相对基准的超额胜率 —— 这个才是"形态有没有信息"的度量
            "edge_vs_baseline_pp": edge,
        }
    empty_inference["forward_stats"] = forward

    # 置信度: 样本量与相对基准的优势共同决定
    n = analogs["sample_size"]
    edges = [v["edge_vs_baseline_pp"] for v in forward.values() if v["edge_vs_baseline_pp"] is not None]
    consistent = bool(edges) and (all(e > 0 for e in edges) or all(e < 0 for e in edges))
    if n >= 50 and consistent:
        confidence = "medium"
    elif n >= 30 and consistent:
        confidence = "low_to_medium"
    else:
        confidence = "low"

    # 规则检验给出的上限优先 (very_low < low < low_to_medium < medium)
    order = {"very_low": 0, "low": 1, "low_to_medium": 2, "medium": 3}
    if confidence_cap and order.get(confidence_cap, 9) < order.get(confidence, 9):
        confidence = confidence_cap

    empty_inference["confidence"] = confidence
    basis = (
        f"样本 {n} 例; 各前瞻期相对基准方向{'一致' if consistent else '不一致'}; "
        "单只股票的历史统计不等于未来概率"
    )
    if confidence_cap:
        basis += f"; 规则有效性检验把置信度上限压到 {confidence_cap}"
    empty_inference["confidence_basis"] = basis
    return empty_inference


def build_invalidation(
    diagnosis: dict[str, Any],
    holding: dict | None,
    plan: dict | None,
) -> list[InvalidationRule]:
    """问题 2: 持有时, 什么情况说明原判断可能错了。

    三类来源, 优先级从高到低:
    1. 用户自己设的可接受亏损 (用户权威, 最该遵守);
    2. 技术结构位 (MA10/MA60/前低);
    3. 成本价与现价的关系。
    """
    rules: list[InvalidationRule] = []
    close = diagnosis["candle"]["close"]
    tr = diagnosis["trend"]
    kl = diagnosis["levels"]

    # 1. 用户设的亏损容忍 —— 唯一"必须遵守"的一条
    loss_pct = None
    if plan:
        loss_pct = plan.get("acceptable_loss_pct")
    if holding and loss_pct:
        cost = holding.get("avg_cost") or 0
        if cost > 0:
            stop = cost * (1 - float(loss_pct) / 100)
            rules.append(InvalidationRule(
                condition=f"跌破 {stop:.2f}（= 成本 {cost:.2f} 下方 {float(loss_pct):g}%）",
                price=stop,
                meaning=f"亏损达到你设定的容忍上限 {float(loss_pct):g}%，原买入理由需要重新审视",
                source="你在计划里填的「可接受亏损」",
                severity="critical",
            ))
    elif holding:
        rules.append(InvalidationRule(
            condition="（未设置可接受亏损，无法给出亏损纪律线）",
            price=None,
            meaning="建议先在计划里填写可接受亏损百分比，否则亏损时没有客观的认错标准",
            source="计划缺失",
            severity="warning",
        ))

    # 2. 技术结构位
    ma10 = tr["mas"].get("MA10")
    ma60 = tr["mas"].get("MA60")
    low20 = kl["low_20"]["price"]

    if ma60 and close < ma60:
        rules.append(InvalidationRule(
            condition=f"持续运行在 MA60（{ma60:.2f}）下方",
            price=ma60,
            meaning="中期趋势已转弱，原判断若基于「上升趋势」则不成立",
            source="MA60（中期成本线）",
            severity="critical",
        ))
    if ma10 and close > ma10:
        rules.append(InvalidationRule(
            condition=f"跌破 MA10（{ma10:.2f}）",
            price=ma10,
            meaning="短线支撑失守，短期动能转弱",
            source="MA10（短期成本线）",
            severity="warning",
        ))
    if low20 and close > low20:
        gap = (close / low20 - 1) * 100
        if gap <= 15:
            rules.append(InvalidationRule(
                condition=f"跌破近 20 日低点 {low20:.2f}",
                price=low20,
                meaning=f"创出新低（距现价仅 {gap:.1f}%），下跌结构延续",
                source="近 20 日最低价",
                severity="critical",
            ))

    # 3. 计划持有期到期的纪律
    if holding and plan and plan.get("planned_hold_days"):
        days = holding.get("holding_days")
        planned = int(plan["planned_hold_days"])
        if days is not None and days >= planned:
            rules.append(InvalidationRule(
                condition=f"持仓已达 {days} 天，超过计划持有期 {planned} 天",
                price=None,
                meaning="到了你原定的时间框架，无论盈亏都该重新评估一次（不是自动卖出）",
                source="你填的计划持有期",
                severity="warning",
            ))

    return rules


def build_entry_conditions(diagnosis: dict[str, Any]) -> list[EntryCondition]:
    """问题 3: 没买时, 哪些条件出现后再考虑。

    只给「条件」, 不给「建议」。条件必须带价位与可验证的特征。
    """
    conds: list[EntryCondition] = []
    tr = diagnosis["trend"]
    kl = diagnosis["levels"]
    ch = diagnosis["chips"]
    ind = diagnosis["indicators"]
    close = diagnosis["candle"]["close"]


    structure = tr["structure"]

    ma60 = tr["mas"].get("MA60")
    high20 = kl["high_20"]["price"]

    # 向上突破类: 用近 20 日高点 + 量能要求
    if high20 and high20 > close:
        conds.append(EntryCondition(
            condition=f"放量突破近 20 日高点 {high20:.2f}（成交量 > 20 日均量 1.5 倍）",
            price=high20,
            rationale="突破前高且量能配合，说明有新资金承接，而非无量反弹",
            kind="breakout",
        ))

    # 回调类: 中期趋势仍在时, 回踩 MA60 是相对好的风险收益
    if structure in ("uptrend", "uptrend_pullback") and ma60:
        conds.append(EntryCondition(
            condition=f"回踩 MA60（{ma60:.2f}）附近且不跌破",
            price=ma60,
            rationale="上升趋势中回踩中期成本线，止损距离最短，赔率最好",
            kind="pullback",
        ))

    # 下降趋势: 只在超卖 + 放量止跌时才谈条件
    if structure == "downtrend":
        low20 = kl["low_20"]["price"]
        if low20:
            conds.append(EntryCondition(
                condition=f"跌到 {low20:.2f}（近 20 日低点）附近出现放量阳线（成交量 > 1.5 倍均量）",
                price=low20,
                rationale="下降趋势中只做「放量止跌」的右侧信号，缩量阴跌不构成条件",
                kind="volume",
            ))
        if ind["rsi14"] is not None and ind["rsi14"] > 50:
            conds.append(EntryCondition(
                condition="RSI14 回落到 30 以下后再观察",
                price=None,
                rationale=f"当前 RSI14 = {ind['rsi14']}，未到超卖区，下降趋势中缺乏反弹动能",
                kind="valuation",
            ))

    # 头顶筹码: 明确告诉用户"涨上去要面对多少套牢盘"
    above = ch.get("densest_above")
    if above and above["total_pct"] >= 20:
        conds.append(EntryCondition(
            condition=f"确认能否有效突破 {above['low']:.2f}~{above['high']:.2f} 的套牢区",
            price=above["low"],
            rationale=f"该区间堆积约 {above['total_pct']:.0f}% 的近 60 日筹码，是反弹的第一道重压",
            kind="breakout",
        ))

    if not conds:
        conds.append(EntryCondition(
            condition="当前没有可操作的入场条件",
            price=None,
            rationale="结构与量能都没有给出可验证的信号，此时进场等同于猜方向",
            kind="valuation",
        ))

    return conds


def build_checks(
    diagnosis: dict[str, Any],
    *,
    data_status: dict[str, Any] | None = None,
    digest: Any | None = None,
) -> list[CheckItem]:
    """问题 4: 财报/公告/告警里必须核对的事实。

    有两个层次:
    - **有 digest** (fundamentals_digest): 直接把数字取出来 —— 把「去哪查」
      升级为「查到了什么」, 这是本函数存在的主要价值;
    - **无 digest**: 退回"告诉你去看什么", 并明确标注 unverified (不假装查过)。
    """
    checks: list[CheckItem] = []

    if digest is not None:
        # 财务与公告的真实数字 (来自 fundamentals_digest.checks_from_digest)
        for item in digest_checks(digest):
            checks.append(CheckItem(
                item=str(item.get("item") or ""),
                why=str(item.get("why") or ""),
                where=str(item.get("where") or ""),
                status=str(item.get("status") or "unverified"),
            ))

    # 无法从本地数据回答的, 继续如实标为待核
    checks.extend([
        CheckItem(
            item="所属行业景气度与产品价格趋势",
            why="周期股的股价往往先于财报反映产品价格，需要独立核对",
            where="行业数据、行业协会或公司调研纪要",
            status="unverified",
        ),
        CheckItem(
            item="是否有未公告的重大事项（重组/诉讼/监管）",
            why="本地数据只覆盖已落库的公告，未公告事项无法从任何技术指标看出",
            where="交易所公告 / 巨潮资讯",
            status="unverified",
        ),
    ])

    # 针对当前形态, 动态追加
    pct = diagnosis["trend"]["position_in_250_pct"]
    if pct <= 25:
        checks.append(CheckItem(
            item="低位是否有机构调研/评级变化",
            why=f"现价处于 250 日区间 {pct:.0f}% 分位，低位往往伴随分歧加大",
            where="数据中心 → 机构评级（ext_research）",
            status="unverified" if digest is None else "checked",
        ))

    if data_status:
        for name, ok in data_status.items():
            if not ok:
                checks.append(CheckItem(
                    item=f"数据可用性: {name} 不可用",
                    why="该项数据缺失时相关结论无法验证",
                    where="数据页面检查同步状态",
                    status="unavailable",
                ))

    return checks


# ── 组装 ────────────────────────────────────────────────

def build_action_card(
    *,
    df: pl.DataFrame,
    symbol: str,
    name: str = "",
    holding: dict | None = None,
    plan: dict | None = None,
    plan_progress: dict | None = None,
    data_status: dict[str, Any] | None = None,
    digest: Any | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一只票一张行动卡。

    ``holding`` 来自 portfolio_positions (真实持仓, 只有 paper 台账里有的才给);
    ``plan`` 来自 position_plan (用户填的计划 —— **不参与盈亏计算**);
    两者都为 None 时状态是 "自选观察"。
    ``digest`` 是 fundamentals_digest.FundamentalDigest —— 有则把财报与公告的
    **真实数字**填进 checks 与 fundamentals 段, 无则退回"去哪查"并标 unverified。
    ``validation`` 显式传入时用调用方的结果；**不传时默认注入** ``strength_research``
    冻结的全市场研究结论 —— 保证「默认行动卡必带规则有效性披露」，不让
    build_inference 的诚实性链路在真实产品里变成死代码。
    """
    # validation 缺省 = 注入冻结研究结论（覆盖所有调用方：API / 测试 / 脚本）。
    # 显式传 dict（含 None 语义之外的覆盖）仍可覆盖，例如未来接单票 live 检验。
    effective_validation = validation if validation is not None else strength_research.load_market_validation()
    diagnosis = diag.diagnose(df)
    state = position_plan.resolve_state(
        symbol,
        holding_symbols={symbol} if holding else set(),
        planned_symbols={symbol} if plan else set(),
    )

    fundamentals_payload: dict[str, Any] = {}
    if digest is not None:
        try:
            fundamentals_payload = digest.to_dict()
        except Exception as exc:  # noqa: BLE001 - 摘要失败不该拖垮整张卡
            logger.warning("fundamental digest serialize failed: %s", exc)
            fundamentals_payload = {"available": False, "limitations": [f"基本面摘要序列化失败: {exc}"]}
    else:
        fundamentals_payload = {
            "available": False,
            "limitations": ["未取用本地财务/公告数据（调用方未提供摘要）"],
        }

    card = ActionCard(
        symbol=symbol,
        name=name or symbol,
        state=state,
        state_label=position_plan.STATE_LABELS[state],
        as_of=diagnosis["candle"]["date"],
        facts=build_facts(diagnosis, holding),
        inference=build_inference(diagnosis, validation=effective_validation),
        user_plan={
            "state_note": position_plan.STATE_NOTES[state],
            "counts_toward_pnl": state == position_plan.STATE_HOLDING,
            "planned_hold_days": (plan or {}).get("planned_hold_days"),
            "acceptable_loss_pct": (plan or {}).get("acceptable_loss_pct"),
            "batches": (plan or {}).get("batches") or [],
            "note": (plan or {}).get("note") or "",
            "progress": plan_progress,
        },
        strength=diagnosis["strength"],
        invalidation=build_invalidation(diagnosis, holding, plan),
        entry_conditions=build_entry_conditions(diagnosis),
        checks=build_checks(diagnosis, data_status=data_status, digest=digest),
        fundamentals=fundamentals_payload,
        caveats=list(diagnosis["limitations"]),
    )

    # 状态相关的额外提醒 (防止把提示误认为已持仓)
    if state == position_plan.STATE_PLANNING:
        card.caveats.insert(
            0,
            "这条标的是「计划买入」状态 —— 尚未成交，不参与任何盈亏与权重计算",
        )
    if state == position_plan.STATE_WATCH:
        card.caveats.insert(
            0,
            "这条标的是「自选观察」状态 —— 没有持仓，不计盈亏",
        )

    result = card.to_dict()
    result["diagnosis_summary"] = {
        "structure": diagnosis["trend"]["structure"],
        "pattern": diagnosis["analogs"]["pattern"],
        "sample_size": diagnosis["analogs"]["sample_size"],
    }
    # 基本面摘要里的限制也并入 caveats —— 让"数据缺失"与"技术面局限"同屏可见
    for lim in fundamentals_payload.get("limitations") or []:
        if lim not in result["caveats"]:
            result["caveats"].append(lim)
    return result


__all__ = [
    "QUESTION_MAP",
    "SECTION_LABELS",
    "ActionCard",
    "CheckItem",
    "EntryCondition",
    "InvalidationRule",
    "build_action_card",
    "build_checks",
    "build_entry_conditions",
    "build_facts",
    "build_inference",
    "build_invalidation",
]
