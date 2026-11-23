"""告警「少而重要」摘要 —— 分级、四问、去重、只报增量的行为锁。

重点锁住三条**产品承诺**（不是实现细节）：

1. 弱关联推断必须落到 ``theme_hint``，不得与公司公告并列；
2. 缺字段时如实标未判定/未提供，**不默认成"直接相关"**；
3. 宏观/行业背景不进推送列表（"少"才成立），但仍可在 ``suppressed`` 查到。
"""
from __future__ import annotations

import time

from app.services import alert_digest as ad

NOW = int(time.time() * 1000)


def _ev(
    message: str,
    *,
    symbol: str = "605366.SH",
    type_: str = "policy_产业",
    ts: int | None = None,
    **extra,
) -> dict:
    ev = {
        "ts": ts if ts is not None else NOW,
        "source": "policy_radar",
        "type": type_,
        "symbol": symbol,
        "name": "",
        "message": message,
    }
    ev.update(extra)
    return ev


def _group(*alerts: dict, sources: list[dict] | None = None) -> dict:
    """把告警包成一个事件组，字段与 ``alert_groups.group_events`` 输出同形。

    ``sources=None`` 表示"采集侧没给来源"，用于测缺失字段的如实标注。
    """
    first = alerts[0]
    return {
        "event_group_id": first.get("event_group_id") or "",
        "grouped": bool(first.get("event_group_id")),
        "event_id": first.get("event_id"),
        "title": first.get("message", "")[:80],
        "event_date": first.get("event_date") or "unknown",
        "alert_count": len(alerts),
        "symbols": [a.get("symbol") for a in alerts],
        "names": [a.get("name") for a in alerts],
        "directions": [a.get("event_direction") for a in alerts],
        "mapping_levels": [a.get("mapping_level") for a in alerts],
        "verification": first.get("verification") or "pending",
        "verification_label": first.get("verification") or "pending",
        "source_count": 1,
        "sources": sources if sources is not None else [{"name": first.get("source_name") or "来源"}],
        "first_ts": min(a.get("ts", 0) for a in alerts),
        "last_ts": max(a.get("ts", 0) for a in alerts),
        "alerts": alerts,
    }


# ── 关系判定 ────────────────────────────────────────────


def test_weak_link_is_inferred_not_direct():
    """宏柏新材那条风电告警：采集侧标了间接 + C 级 → 必须判为推断。"""
    ev = _ev(
        "【弱关联·推测】▲ 利好·政策 | 上海推动大兆瓦海上风机批量应用",
        mapping_level="C",
        mapping_level_desc="弱映射（仅同行业或边缘业务，需多级传导）",
        directness="间接",
        verification="inferred",
    )
    relation, reason = ad.classify_relation(ev)
    assert relation == "inferred"
    assert "间接" in reason


def test_missing_mapping_fields_are_unknown_not_direct():
    """三个字段都缺时必须是 unknown —— 默认 direct 会把未知伪装成事实。"""
    ev = _ev("9月新增新能源发电项目3552个")
    relation, reason = ad.classify_relation(ev)
    assert relation == "unknown", "缺字段不能默认成直接相关"
    assert "未判定" in reason


def test_directness_wins_over_mapping_level():
    # 采集侧说直接，等级却是 C：以显式 directness 为准，但仍如实标注等级
    ev = _ev("某行业新闻", mapping_level="C", directness="直接")
    relation, reason = ad.classify_relation(ev)
    assert relation == "direct"
    assert "C" in reason


def test_weak_marker_in_title_falls_back_to_inferred():
    ev = _ev("【弱关联·推测】某政策可能影响某行业")
    relation, _ = ad.classify_relation(ev)
    assert relation == "inferred"


# ── 分级 ────────────────────────────────────────────────


def test_weak_link_graded_as_theme_hint():
    group = _group(_ev(
        "【弱关联·推测】上海推动大兆瓦海上风机批量应用",
        mapping_level="C",
        mapping_level_desc="弱映射（仅同行业或边缘业务，需多级传导）",
        directness="间接",
    ))
    tier, reason = ad.grade(group)
    assert tier == ad.TIER_THEME_HINT
    assert "线索" in reason


def test_company_announcement_is_worth_notice_not_theme_hint():
    """公司自身的减持公告必须是 worth_notice —— 曾经被判成主题线索。

    采集侧 type 是 ``policy_公告``（不是英文 ``announcement``），第一版只比
    英文枚举就漏了；同时它只挂 1 个标的，也不该因此被当成"自身事件"的
    唯一依据（那条兜底后来单独删掉了，见 ``test_industry_news_on_single_symbol``）。
    """
    group = _group(_ev(
        "◆ 不确定·公告 | 宏柏新材:江西宏柏新材料股份有限公司股东减持股份计划公告",
        type_="policy_公告",
    ))
    tier, reason = ad.grade(group)
    assert tier == ad.TIER_WORTH_NOTICE, reason
    assert "自身事件" in reason


def test_verified_company_event_is_must_know():
    group = _group(_ev(
        "美的集团:关于回购注销部分限制性股票的公告",
        symbol="000333.SZ",
        type_="policy_公告",
        verification="fact",
    ))
    tier, _ = ad.grade(group)
    assert tier == ad.TIER_MUST_KNOW


def test_macro_news_graded_as_background():
    """「央行第三批互换便利」挂在单只 ETF 上，也不是公司的事。"""
    group = _group(_ev("央行第三批互换便利操作1000亿元", symbol="159682.SZ"))
    tier, reason = ad.grade(group)
    assert tier == ad.TIER_BACKGROUND
    assert "宏观" in reason


def test_policy_meeting_is_background_not_theme_hint():
    """国常会/政治局这类整场会议表态是市场级信息, 不该占主题线索的位置。

    实测踩过: 「国常会要求完善资本市场稳市机制」被挂到个别标的上, 原本落到
    theme_hint —— 用户会把它当成那只票的题材线索。
    """
    tier, reason = ad.grade(_group(_ev("国常会要求完善资本市场稳市机制")))
    assert tier == ad.TIER_BACKGROUND
    assert "宏观" in reason


def test_regulator_penalty_on_company_is_still_self_event():
    """部委名出现在「被处罚」语境时是公司自身事件 —— 不能因为含"证监会"就降级。

    这是上一条的反向约束: 宏观词列表只放"整场会议/整体表态", 不放部委名。
    """
    tier, _ = ad.grade(_group(_ev("某公司被证监会立案调查")))
    assert tier != ad.TIER_BACKGROUND


def test_industry_news_on_single_symbol_is_not_self_event():
    """行业新闻只挂一只 ETF 也不能算公司自身事件。

    第一版用"只关联 1 个标的 → 自身事件"兜底，把「字节跳动扩大巴西数据中心」
    「前三季度老旧小区改造」全判成了公司公告。标的数量反映采集匹配宽度，
    不是事件主体。
    """
    group = _group(_ev("字节跳动考虑扩大巴西数据中心", symbol="159682.SZ"))
    tier, reason = ad.grade(group)
    assert tier in (ad.TIER_THEME_HINT, ad.TIER_BACKGROUND), reason
    assert tier != ad.TIER_WORTH_NOTICE


def test_direct_industry_mapping_is_worth_notice():
    group = _group(_ev(
        "上海推进5G揽海与AI+海洋",
        symbol="600941.SH",
        mapping_level="B",
        mapping_level_desc="强映射（事件板块与该股主营/概念精确命中）",
        directness="直接",
    ))
    tier, reason = ad.grade(group)
    assert tier == ad.TIER_WORTH_NOTICE
    assert "直接映射" in reason


# ── 四问 ────────────────────────────────────────────────


def test_questions_answer_all_four_with_real_fields():
    group = _group(_ev(
        "【弱关联·推测】上海推动大兆瓦海上风机批量应用",
        mapping_level="C",
        directness="间接",
        impact_path_text="海上风机 → 玻纤/复材需求增加 → 硅烷偶联剂受益",
        published_at="2026-11-10 17:01:19",
        source_name="上海证券报",
    ))
    q = ad.build_questions(group)
    assert q["what_happened"]["published_at"] == "2026-11-10 17:01:19"
    assert q["what_happened"]["sources"] == ["上海证券报"]
    assert q["why_related"]["relation"] == "inferred"
    assert q["impact_channel"]["available"] is True
    assert "玻纤" in q["impact_channel"]["channel_text"]
    assert q["action"]["needs_action"] is False
    assert q["action"]["label"] == "仅作线索"


def test_impact_channel_not_fabricated_when_missing():
    group = _group(_ev("某政策"))
    q = ad.build_questions(group)
    assert q["impact_channel"]["available"] is False
    assert q["impact_channel"]["channel_text"] == "未提供"
    assert q["impact_channel"]["note"]


def test_missing_source_and_date_are_listed_not_blank():
    group = _group(_ev("某政策"), sources=[])
    q = ad.build_questions(group)
    assert "来源媒体" in q["what_happened"]["missing"]
    assert "发布日期" in q["what_happened"]["missing"]


def test_must_know_asks_for_action():
    group = _group(_ev(
        "某公司回购公告",
        type_="policy_公告",
        verification="fact",
    ))
    q = ad.build_questions(group)
    assert q["action"]["needs_action"] is True
    assert q["action"]["label"] == "建议核对"


# ── 去重 ────────────────────────────────────────────────


def test_dedupe_keeps_latest_and_counts_repeats():
    old = _group(_ev("某政策发布 100 亿元", ts=NOW - 5000))
    new = _group(_ev("某政策发布 120 亿元", ts=NOW))
    out = ad.dedupe([old, new])
    assert len(out) == 1
    assert out[0]["repeat_count"] == 2
    assert out[0]["last_ts"] == NOW, "应保留最新一条"


def test_dedupe_separates_different_symbols_for_self_events():
    """两家公司各自的公告是两件事, 即使标题完全一样。"""
    a = _group(_ev("某公司:股东减持股份计划公告", symbol="600941.SH", type_="policy_公告"))
    b = _group(_ev("某公司:股东减持股份计划公告", symbol="000333.SZ", type_="policy_公告"))
    assert len(ad.dedupe([a, b])) == 2


def test_dedupe_merges_same_news_across_symbols():
    """同一条行业新闻被采集侧挂到 3 只 ETF 上是 1 件事, 不是 3 件事。

    实测「字节跳动考虑扩大巴西数据中心项目」就出现过 3 行, 正是用户抱怨的
    "提醒太多"。合并后必须把标的并起来, 让用户知道这条影响哪几只。
    """
    groups = [
        _group(_ev("字节跳动考虑扩大巴西数据中心项目", symbol=sym))
        for sym in ("159682.SZ", "159586.SZ", "000333.SZ")
    ]
    merged = ad.dedupe(groups)
    assert len(merged) == 1
    assert merged[0]["repeat_count"] == 3
    assert set(merged[0]["symbols"]) == {"159682.SZ", "159586.SZ", "000333.SZ"}


# ── 摘要整体行为 ────────────────────────────────────────


def test_background_not_pushed_but_listed_in_suppressed():
    """宏观背景不进推送 —— "少"才成立；但仍能在 suppressed 里查到。"""
    alerts = [
        _ev("央行第三批互换便利操作1000亿元", symbol="MARKET"),
        _ev("某公司股东减持计划公告", type_="policy_公告"),
    ]
    digest = ad.build_digest(alerts)
    tiers = {i["tier"] for i in digest["items"]}
    assert ad.TIER_BACKGROUND not in tiers
    suppressed_tiers = {i["tier"] for i in digest["suppressed"]["items"]}
    assert ad.TIER_BACKGROUND in suppressed_tiers
    assert digest["suppressed"]["count"] >= 1


def test_symbol_filter_keeps_only_that_symbol():
    alerts = [
        _ev("宏柏新材:股东减持计划公告", symbol="605366.SH", type_="policy_公告"),
        _ev("美的集团:回购公告", symbol="000333.SZ", type_="policy_公告"),
    ]
    digest = ad.build_digest(alerts, symbol="605366.SH")
    assert digest["items"], "605366.SH 应有一条"
    for item in digest["items"]:
        assert "605366.SH" in item["symbols"]


def test_no_prior_state_marks_everything_new_and_says_so():
    alerts = [_ev("某公司回购公告", type_="policy_公告")]
    digest = ad.build_digest(alerts)
    assert digest["items"][0]["is_new_fact"] is True
    assert any("未提供上次状态" in text for text in digest["limitations"])


def test_prior_state_suppresses_seen_items():
    alert = _ev("某公司回购公告", type_="policy_公告", event_group_id="g1")
    first = ad.build_digest([alert])
    state = ad.state_from_digest(first)
    assert "g1" in state

    second = ad.build_digest([alert], prior_state=state)
    assert second["items"] == [], "看过的不再作为新事实推出"
    assert second["suppressed"]["count"] == 1


def test_risk_change_is_reported_even_when_seen():
    """核实状态从 pending 升到 fact 属于风险变化，必须重新提醒。"""
    before = _ev("某公司回购公告", type_="policy_公告", event_group_id="g1", verification="pending")
    state = ad.state_from_digest(ad.build_digest([before]))

    after = _ev("某公司回购公告", type_="policy_公告", event_group_id="g1", verification="fact")
    digest = ad.build_digest([after], prior_state=state)
    assert digest["items"], "核实状态升级应重新提醒"
    item = digest["items"][0]
    assert item["risk_changed"] is True
    assert "升级" in (item["change_note"] or "")


def test_watch_rule_promotes_to_must_know():
    digest = ad.build_digest(
        [_ev("某公司股东减持计划公告", type_="policy_公告")],
        watch_rules=[{"symbol": "605366.SH", "type": "policy_公告"}],
    )
    assert digest["items"][0]["tier"] == ad.TIER_MUST_KNOW
    assert digest["items"][0]["hit_rule"] is not None
    assert digest["items"][0]["action"]["needs_action"] is True


def test_limit_truncates_items():
    alerts = [
        _ev(f"某公司公告 {i}", symbol=f"60000{i}.SH", type_="policy_公告", ts=NOW - i)
        for i in range(5)
    ]
    digest = ad.build_digest(alerts, limit=2)
    assert len(digest["items"]) == 2


def test_digest_reports_unknown_relation_count():
    digest = ad.build_digest([_ev("某政策无字段")])
    assert any("未判定" in text for text in digest["limitations"])


def test_digest_has_no_buy_sell_language():
    """与行动卡同一红线：不返回买卖建议。"""
    alerts = [
        _ev("某公司回购公告", type_="policy_公告"),
        _ev("【弱关联·推测】某政策", mapping_level="C", directness="间接"),
    ]
    digest = ad.build_digest(alerts)
    text = str(digest)
    for banned in ("buy_score", "sell_score", "recommendation", "买入建议", "卖出建议"):
        assert banned not in text


def test_state_from_digest_can_exclude_suppressed():
    """ack 场景要排除 suppressed 条目：那些用户从没在屏幕上看见过。"""
    # 已看过的那条必须与 prior_state 记录的枚举值一致，否则会被判成"风险变化"
    # 而重新推出 —— 那是另一条测试覆盖的行为。
    seen = _ev(
        "某公司回购公告", type_="policy_公告", event_group_id="g1",
        verification="fact", mapping_level="A",
    )
    fresh = _ev("某公司减持公告", type_="policy_公告", event_group_id="g2")
    state_all = ad.state_from_digest(ad.build_digest([seen, fresh]))
    assert "g1" in state_all and "g2" in state_all

    digest = ad.build_digest(
        [seen, fresh],
        prior_state={"g1": {"verification": "fact", "mapping_level": "A"}},
    )
    assert [i["event_group_id"] for i in digest["items"]] == ["g2"]
    assert digest["suppressed"]["count"] == 1

    state_items_only = ad.state_from_digest(digest, include_suppressed=False)
    assert set(state_items_only) == {"g2"}
