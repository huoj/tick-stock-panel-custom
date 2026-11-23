"""告警「少而重要」—— 把 285 条原始告警压成少数几条该看的事实。

## 问题

系统里告警按"事件"归组已经做了（``alert_groups``），但产品层面仍然是**条数驱动**：
实测 285 条告警里，政策类 100 条只覆盖 11 个标的，同一标的被不同事件反复关联
（``159682.SZ`` 21 条、``000333.SZ`` 18 条）。用户看到的是一串同质条目，
真正的新事实被埋在中间。

## 本文档解决四件事

1. **分级**（``tier``）：不是所有告警都该推给用户。分四档
   - ``must_know``：公司自身、已核实、可能影响业务的直接事实（公告/业绩/减持等）
   - ``worth_notice``：直接映射但未核实，或强映射的行业事件
   - ``background``：行业/宏观背景，知道就行，不需要动作
   - ``theme_hint``：**弱关联推断**。宏柏新材那条"上海推动大兆瓦海上风机"
     就是这一类：玻纤/复材 → 硅烷偶联剂是多级传导，不能和公司订单公告
     摆在同一层级。

2. **四问**（``what / why_related / impact_channel / action``）：每条都必须回答
   「发生了什么+来源和日期 / 为什么与这只票相关（直接还是推断）/ 影响渠道 /
   要不要动作」。缺字段时如实写 `unknown`，不编。

3. **去重**（``dedupe``）：同一标的 + 同一标题骨架 + 同一方向的重复记录只留最新，
   并把重复条数记成 ``repeat_count``。实测同一 message 完全重复只有 1 条，
   所以真正的重复是「同一事件被不同来源/不同轮次再次采集」。

4. **只报"新"**（``is_new_fact`` / ``risk_changed``）：
   - 新事实 = 这个标的此前没见过这个 ``event_id``/标题骨架；
   - 风险变化 = ``mapping_level`` 或 ``verification`` 相比上一次更强/更弱（升级/降级）；
   - 用户条件 = 调用方传 ``watch_rules``，命中即 ``must_know``。

## 诚实边界

- ``impact_channel`` 只在采集侧给了 ``impact_path_text`` 时才有内容；**没有就写
  "未提供"，不替它编一条传导链**。
- ``why_related`` 的 ``direct``/``inferred`` 直接用 ``directness`` 与
  ``mapping_level`` 判定；三个字段都缺失时标 ``unknown`` 而不是默认"直接"。
  实测 285 条里 ``mapping_level`` 只有 58 条有值 —— 默认"直接"会把
  227 条未知全部伪装成确定事实。
- 分级是**排序辅助**，不是"该不该买"。返回值里不出现买卖建议字段。
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services import alert_groups

#: 分级（从高到低）。顺序即优先级，用于 ``rank``。
TIER_MUST_KNOW = "must_know"
TIER_WORTH_NOTICE = "worth_notice"
TIER_BACKGROUND = "background"
TIER_THEME_HINT = "theme_hint"

TIER_ORDER: tuple[str, ...] = (
    TIER_MUST_KNOW,
    TIER_WORTH_NOTICE,
    TIER_BACKGROUND,
    TIER_THEME_HINT,
)

TIER_LABELS: dict[str, str] = {
    TIER_MUST_KNOW: "需要知道",
    TIER_WORTH_NOTICE: "值得留意",
    TIER_BACKGROUND: "背景信息",
    TIER_THEME_HINT: "主题线索",
}

TIER_DESCRIPTIONS: dict[str, str] = {
    TIER_MUST_KNOW: "与公司自身直接相关、且来源可核对的确定性事实，通常需要你判断是否采取动作。",
    TIER_WORTH_NOTICE: "与本标的直接相关但尚未核实，或行业层面较强映射，建议留意但不必立刻动作。",
    TIER_BACKGROUND: "行业或宏观背景，知道即可；它解释环境，不构成个股事件。",
    TIER_THEME_HINT: "弱关联推断：需要多级传导才可能影响业务，只作线索，不可与公司公告同等对待。",
}

#: 映射等级 → 语义。A/B 是直接，C/D 是推断（见 ``docs/告警字段契约.md``）。
_DIRECT_LEVELS = frozenset({"A", "B"})
_INFERRED_LEVELS = frozenset({"C", "D"})

#: 公司自身事件类型：这些属于"这只票自己的事"，比行业新闻重要。
#:
#: 采集侧的 ``type`` 是「来源_分类」格式（实测取值 ``policy_政策`` 52 条 /
#: ``policy_产业`` 24 / ``policy_公告`` 9 / ``policy_地缘`` 7 /
#: ``calendar_domestic`` 8 / ``paper_fill`` / ``fill`` / ``auto_order``），
#: 所以判定要**同时匹配整串与下划线后的分类词**，不能只写英文枚举 ——
#: 第一版只列英文，导致「宏柏新材:股东减持股份计划公告」被判成主题线索。
_SELF_TYPES = frozenset({
    "announcement", "announce", "notice", "earnings", "forecast", "dividend",
    "insider", "unlock", "pledge", "goodwill", "shareholder", "blocktrade",
    "announcement_negative", "announcement_positive", "report",
    # 中文分类词（采集侧实际在用）
    "公告", "业绩", "减持", "增持", "回购", "分红", "解禁", "质押", "诉讼",
    "重组", "停牌", "复牌", "问询", "监管", "退市",
})

#: 标题出现即视为公司自身事件的关键词（``type`` 缺失时兜底）。
_SELF_TITLE_MARKERS = (
    "公告", "减持", "增持", "回购", "业绩预告", "业绩快报", "年报", "季报",
    "分红", "解禁", "质押", "股东", "董事", "高管", "诉讼", "问询", "停牌",
    "立案", "处罚", "警示函", "监管函", "退市",
)

#: **公司自己的动作词**：即使标题里出现了监管机构名（证监会/交易所/证监会立案），
#: 也仍然是这家公司的事。
#:
#: 为什么需要这个：``_MACRO_MARKERS`` 里有 ``证监会``，于是一条
#: 「某公司被证监会立案调查」会先命中宏观排除、再被判成 ``background`` ——
#: 而它恰恰是必须知道的公司风险事件。所以宏观排除要让位给这些词。
#:
#: 这里**刻意不含"公告"与"股东"**：``证监会公告〔2026〕1号`` 是政策文件，
#: 不是公司公告；用宽词会让政策面内容冒进"值得留意"。
_COMPANY_ACTION_MARKERS = (
    "减持", "增持", "回购", "业绩预告", "业绩快报", "年报", "季报", "分红",
    "解禁", "质押", "诉讼", "问询", "停牌", "复牌", "立案", "处罚",
    "警示函", "监管函", "退市", "被调查",
)

#: 宏观/市场级关键词：出现即**不是**某只票自己的事，即使它只挂了一个标的。
#:
#: 实测必须排除：``159682.SZ`` 上挂了「央行第三批互换便利」「央行坚持适度宽松」
#: 等多条宏观新闻，``MARKET`` 上挂了「LPR 报价」「社融 / M2」。第一版用
#: "只挂一个标的就算自身事件" 兜底，把这些全判成了公司公告。
_MACRO_MARKERS = (
    "央行", "LPR", "贷款市场报价利率", "社融", "M2", "M1", "社会融资",
    "统计局", "财政部", "证监会", "发改委", "国务院", "美联储", "欧盟",
    "欧元区", "美联储", "加息", "降息", "降准", "互换便利", "国债",
    "汇率", "通胀", "CPI", "PMI", "GDP", "关税", "制裁", "贸易救济",
    # 政策会议级表态：实测「国常会要求完善资本市场稳市机制」被挂到个别标的
    # 上，原本落到 theme_hint（按线索处理），但它是市场级表态、不是那家公司的
    # 事。**这里只放"整场会议/整体表态"的词**，不放具体部委名 —— 因为
    # 「XX公司被证监会处罚」这类标题里的部委名恰恰是公司自身事件，误排除会把
    # 必须知道的公告降级成背景信息。
    "国常会", "国务院常务会议", "政治局", "中央经济工作会议", "两会", "中央金融工作会议",
)

#: 弱关联含义词：采集侧有时只在文案里写了「弱关联·推测」，字段却是空的。
_WEAK_MARKERS = ("弱关联", "推测", "边缘业务", "仅同行业")

#: 标题骨架去重时保留的字符数（去掉具体数字/日期后比较）。
_SKELETON_LEN = 24


def _skeleton(message: str) -> str:
    """把标题压成"骨架"：只保留中文与字母，用于判"是不是同一件事的重复采集"。

    不去数字是因为部分标题的数字才是主体（"3552个项目"）；改成**只保留文字**
    反而更稳：同一事件的多次采集通常在措辞上一致，数字/标点会变。
    """
    keep = "".join(ch for ch in (message or "") if ch.isalpha())
    return hashlib.md5(keep[:_SKELETON_LEN].encode("utf-8")).hexdigest()[:12]


def classify_relation(member: dict) -> tuple[str, str]:
    """返回 ``(relation, reason)``：relation ∈ direct / inferred / unknown。

    判定优先级：显式 ``directness`` > ``mapping_level`` > 文案弱关联标记 > unknown。
    **三项都缺就是 unknown**，不默认 direct —— 实测 285 条里 227 条没有
    ``mapping_level``，默认"直接"会把大半未知伪装成确定事实。
    """
    directness = (member.get("directness") or "").strip()
    level = (member.get("mapping_level") or "").strip().upper()
    message = member.get("message") or ""

    if directness == "直接":
        return "direct", f"采集侧标注为直接映射（mapping_level={level or '未给'}）"
    if directness == "间接":
        desc = member.get("mapping_level_desc") or "需多级传导"
        return "inferred", f"采集侧标注为间接映射：{desc}"
    if level in _DIRECT_LEVELS:
        desc = member.get("mapping_level_desc") or ""
        return "direct", f"映射等级 {level}{('：' + desc) if desc else ''}"
    if level in _INFERRED_LEVELS:
        desc = member.get("mapping_level_desc") or "需多级传导"
        return "inferred", f"映射等级 {level}：{desc}"
    if any(marker in message for marker in _WEAK_MARKERS):
        return "inferred", "标题自称弱关联/推测，但结构化字段缺失"
    return "unknown", "采集侧既未给 directness 也未给 mapping_level，关系未判定"


def _is_self_event(group: dict) -> bool:
    """判断这是「这只票自己的事」还是「环境/行业的事」。

    判定顺序（先排除、再确认，注释即口径）：

    1. 标题命中 ``_MACRO_MARKERS`` → **不是**自身事件。宏观新闻常被挂到
       单只 ETF 或 ``MARKET`` 上（实测 ``159682.SZ`` 上挂了一串央行新闻）。
       **例外**：标题同时命中 ``_COMPANY_ACTION_MARKERS``（减持/立案/处罚…）
       时仍然算自身事件 —— 「某公司被证监会立案调查」里有"证监会"，但它不是
       宏观新闻，它是这家公司最需要知道的事。
    2. ``type`` 的整串或下划线后的分类词命中 ``_SELF_TYPES``（采集侧格式是
       ``policy_公告``，只比整串会漏掉）→ 自身事件。
    3. 标题命中 ``_SELF_TITLE_MARKERS``（``type`` 缺失时兜底）→ 自身事件。
    4. 其余 → **不是**自身事件。

    这里**不看"只挂了几个标的"**。第一版加过这条兜底，结果「字节跳动扩大巴西
    数据中心」「前三季度老旧小区改造1.6万个」这类行业新闻因为只挂了一只 ETF
    就被判成公司公告 —— 标的数量反映的是采集匹配宽度，不是事件主体。
    """
    members = group.get("alerts") or []
    titles = " ".join((m.get("message") or "") for m in members)
    if any(marker in titles for marker in _MACRO_MARKERS) and not any(
        marker in titles for marker in _COMPANY_ACTION_MARKERS
    ):
        return False

    for member in members:
        raw = (member.get("type") or "").lower()
        parts = {raw, *raw.split("_")}
        if parts & _SELF_TYPES:
            return True
        title = member.get("message") or ""
        if any(marker in title for marker in _SELF_TITLE_MARKERS):
            return True
    return False


def grade(group: dict) -> tuple[str, str]:
    """给一个事件组分档，返回 ``(tier, reason)``。

    判定顺序（先命中先返回，注释即口径）：

    1. 宏观/市场级新闻且不是公司自身事件 → ``background``。这类内容挂在
       单只 ETF 上时（实测 ``159682.SZ`` 上串了一排央行新闻）既不是这只票
       的事，也不是"关于它的推断"，只是环境。
    2. 弱关联/未判定的推断，且不是公司自身事件 → ``theme_hint``。
       宏柏新材风电那条走这里。
    3. 公司自身事件 + 已核实 → ``must_know``。
    4. 公司自身事件但未核实 → ``worth_notice``（公告尚未被媒体核实很常见）。
    5. 强映射（A/B）但非公司自身事件 → ``worth_notice``。
    6. 其余 → ``background``。
    """
    members = group.get("alerts") or []
    first = members[0] if members else {}
    relation, reason = classify_relation(first)
    levels = {str(lvl).upper() for lvl in (group.get("mapping_levels") or []) if lvl}
    verification = group.get("verification") or "pending"
    is_self = _is_self_event(group)
    titles = " ".join((m.get("message") or "") for m in members)
    is_macro = any(marker in titles for marker in _MACRO_MARKERS)

    if is_macro and not is_self:
        return TIER_BACKGROUND, "宏观/市场级信息，非个股事件"
    if relation == "inferred" and not is_self:
        return TIER_THEME_HINT, f"弱关联推断，仅作线索（{reason}）"
    if relation == "unknown" and not is_self:
        return TIER_THEME_HINT, f"关系未判定，按线索处理（{reason}）"
    if is_self and verification in ("fact", "evidenced"):
        return TIER_MUST_KNOW, f"公司自身事件且已核实（{verification}）"
    if is_self:
        return TIER_WORTH_NOTICE, f"公司自身事件但核实状态为 {verification}"
    if levels and levels <= _DIRECT_LEVELS:
        return TIER_WORTH_NOTICE, f"行业事件，映射等级 {sorted(levels)[0]} 属直接映射"
    return TIER_BACKGROUND, "行业/宏观背景，无个股直接映射"


def build_questions(group: dict) -> dict[str, Any]:
    """四问：发生了什么 / 为什么相关 / 影响渠道 / 要不要动作。"""
    members = group.get("alerts") or []
    first = members[0] if members else {}
    relation, relation_reason = classify_relation(first)
    tier, tier_reason = grade(group)

    sources = group.get("sources") or []
    source_names = [s.get("name") for s in sources if s.get("name")]
    published = first.get("published_at") or first.get("event_date")
    channels = [s.get("channel") for s in sources if s.get("channel")]

    what = {
        "title": group.get("title"),
        "event_date": group.get("event_date"),
        "published_at": published or "unknown",
        "sources": source_names[:5],
        "source_count": group.get("source_count"),
        "verification": group.get("verification"),
        "verification_label": group.get("verification_label"),
        "alert_count": group.get("alert_count"),
        "missing": [
            label for label, value in (
                ("来源媒体", source_names),
                ("发布日期", published),
            ) if not value
        ],
    }

    directions = group.get("directions") or []
    why = {
        "symbols": group.get("symbols") or [],
        "relation": relation,
        "relation_label": {"direct": "直接", "inferred": "推断", "unknown": "未判定"}[relation],
        "relation_reason": relation_reason,
        "mapping_levels": group.get("mapping_levels") or [],
        "direction": directions[0] if directions else "不确定",
    }

    impact_text = first.get("impact_path_text") or ""
    impact = {
        "channel_text": impact_text or "未提供",
        "channel_kind": channels[0] if channels else None,
        "available": bool(impact_text),
        "note": None if impact_text else "采集侧没有给出传导路径，本项不代填",
    }

    if tier == TIER_MUST_KNOW:
        action = {
            "needs_action": True,
            "label": "建议核对",
            "hint": "这条事实可能改变你对这只票的判断，去原始公告/财报确认细节。",
        }
    elif tier == TIER_WORTH_NOTICE:
        action = {
            "needs_action": False,
            "label": "留意即可",
            "hint": "先记下来；等它出现第二次确认（成交/业绩/更多来源）再决定。",
        }
    elif tier == TIER_BACKGROUND:
        action = {
            "needs_action": False,
            "label": "无需动作",
            "hint": "环境背景，不构成个股事件。",
        }
    else:
        action = {
            "needs_action": False,
            "label": "仅作线索",
            "hint": "这是弱关联推断，不能当利空/利多用；只在其它证据同向时作辅助。",
        }

    return {
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "tier_reason": tier_reason,
        "tier_description": TIER_DESCRIPTIONS[tier],
        "what_happened": what,
        "why_related": why,
        "impact_channel": impact,
        "action": action,
    }


def dedupe(groups: list[dict]) -> list[dict]:
    """按「标的 + 标题骨架 + 方向」去重，保留最新一条并记 ``repeat_count``。

    实测 285 条告警里同一 message 完全重复只有 1 条，所以真正的重复来自
    「同一事件被不同来源/不同轮次再次采集」。骨架用标题的纯文字部分哈希，
    避免数字与日期差异把同一件事拆成两条。
    """
    buckets: dict[tuple, dict] = {}
    order: list[tuple] = []
    for group in groups:
        symbols = tuple(sorted(group.get("symbols") or []))
        directions = tuple(sorted(group.get("directions") or []))
        skeleton = _skeleton(group.get("title") or "")
        # 自身事件（公司公告）按标的区分：两家公司都发「股东减持公告」是两件事。
        # 非自身事件（行业/宏观新闻）**不按标的区分**：同一条政策被采集侧挂到
        # 3 只 ETF 上，是同一个事件的三条匹配，不是三件事 —— 实测「字节跳动
        # 考虑扩大巴西数据中心项目」就这样重复出现 3 次，正是用户说的"提醒太多"。
        key = (symbols, skeleton, directions) if _is_self_event(group) else ("", skeleton, directions)
        current = buckets.get(key)
        if current is None:
            item = dict(group)
            item["repeat_count"] = 1
            item["superseded_ts"] = []
            item["_symbols_seen"] = set(symbols)
            # 没有 event_group_id 的条目也要有稳定的状态键，否则「已看过」
            # 永远记不住它们（它们每次都是 is_new_fact=True，天天重报）。
            # 用去重键做身份：同一标的（自身事件）或同一标题（环境事件）+ 同一方向。
            item["_state_key"] = "local:" + hashlib.md5(
                f"{key[0]}|{skeleton}|{directions}".encode(),
            ).hexdigest()[:16]
            buckets[key] = item
            order.append(key)
            continue
        current["repeat_count"] += 1
        current["superseded_ts"].append(group.get("last_ts"))
        current["_symbols_seen"].update(symbols)
        if (group.get("last_ts") or 0) > (current.get("last_ts") or 0):
            keep_repeat = current["repeat_count"]
            keep_ts = current["superseded_ts"]
            keep_key = current["_state_key"]
            keep_symbols = current["_symbols_seen"]
            bucket = dict(group)
            bucket["repeat_count"] = keep_repeat
            bucket["superseded_ts"] = keep_ts
            bucket["_state_key"] = keep_key
            bucket["_symbols_seen"] = keep_symbols
            buckets[key] = bucket

    merged: list[dict] = []
    for key in order:
        item = buckets[key]
        # 环境事件合并后把各条匹配到的标的并起来 —— 用户要知道"这条影响我哪几只"，
        # 而不是被拆成三行分别说同一句话。
        if item["_symbols_seen"] and set(item["_symbols_seen"]) != set(item.get("symbols") or []):
            item = dict(item)
            item["symbols"] = sorted(item["_symbols_seen"])
        item.pop("_symbols_seen", None)
        merged.append(item)
    return merged


def _verification_rank(value: str | None) -> int:
    return alert_groups.VERIFY_RANK.get(value or "", 0)


def _level_rank(value: str | None) -> int:
    """映射等级强弱：A 最强（0）→ D 最弱（3）；未知按最弱处理。"""
    return {"A": 0, "B": 1, "C": 2, "D": 3}.get((value or "").upper(), 4)


def build_digest(
    alerts: list[dict],
    *,
    kinds: tuple[str, ...] = (alert_groups.KIND_POLICY,),
    prior_state: dict[str, dict] | None = None,
    watch_rules: list[dict] | None = None,
    symbol: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """把原始告警压成「少而重要」的摘要。

    参数
    ----
    alerts:
        原始告警（``/api/alerts`` 的返回项的并集）。
    kinds:
        只处理哪些类别，默认只处理政策/新闻（成交日志不是"事件"）。
    prior_state:
        上一次的摘要状态 ``{event_group_id: {verification, mapping_level}}``，
        用来判「风险变化」。不传则所有条目都按 ``is_new_fact=True`` 处理。
    watch_rules:
        用户自定义条件，形如 ``{"symbol": "605366.SH", "type": "pledge"}``；
        命中即升到 ``must_know`` 并标 ``hit_rule``。
    symbol:
        只保留与某只标的相关的条目。
    limit:
        截断到前 N 条（按 tier 优先级 + 最近时间排）。

    返回
    ----
    ``{"items": [...], "counts": {...}, "suppressed": {...}, "limitations": [...]}``
    """
    limits: list[str] = []
    groups = alert_groups.group_events(alerts, kinds=kinds, include_ungrouped=True)
    if symbol:
        target = symbol.strip().upper()
        groups = [
            g for g in groups
            if target in {str(s).upper() for s in (g.get("symbols") or [])}
        ]

    deduped = dedupe(groups)
    prior = prior_state or {}
    rules = watch_rules or []

    items: list[dict[str, Any]] = []
    for group in deduped:
        questions = build_questions(group)
        # 状态键：优先真实的 event_group_id；没有就用去重键派生的稳定 id。
        # 不用 ts 参与，否则同一条新闻每次采集都换身份，「已看过」永远失效。
        #
        # 与 ``event_group_id`` 分开两个字段：后者是采集侧的事实（没归组就是
        # None），前者是本地状态管理的坐标。混在一起会让前端分不清"这条到底
        # 有没有事件组"。
        group_id = group.get("event_group_id") or ""
        state_key = group_id or group.get("_state_key") or ""
        prev = prior.get(state_key) if state_key else None

        # 风险变化：核实状态升级/降级，或映射等级变化。
        risk_changed = False
        change_note = None
        if prev:
            v_before = prev.get("verification")
            v_after = group.get("verification")
            if v_before != v_after:
                direction = "升级" if _verification_rank(v_after) > _verification_rank(v_before) else "降级"
                risk_changed = True
                change_note = f"核实状态{direction}：{v_before} → {v_after}"
            l_before = prev.get("mapping_level")
            l_after = (group.get("mapping_levels") or [None])[0]
            if (
                l_before
                and l_after
                and l_before != l_after
                and _level_rank(l_after) < _level_rank(l_before)
            ):
                risk_changed = True
                change_note = (change_note + "；" if change_note else "") + (
                    f"映射强度提升：{l_before} → {l_after}"
                )

        hit_rule = None
        for rule in rules:
            rule_symbol = (rule.get("symbol") or "").strip().upper()
            rule_type = (rule.get("type") or "").strip().lower()
            symbols_hit = (not rule_symbol) or rule_symbol in {
                str(s).upper() for s in (group.get("symbols") or [])
            }
            types_hit = (not rule_type) or rule_type in {
                (m.get("type") or "").lower() for m in (group.get("alerts") or [])
            }
            if symbols_hit and types_hit:
                hit_rule = rule
                break

        if hit_rule is not None:
            questions["tier"] = TIER_MUST_KNOW
            questions["tier_label"] = TIER_LABELS[TIER_MUST_KNOW]
            questions["tier_reason"] = "命中你设定的关注条件"
            questions["tier_description"] = TIER_DESCRIPTIONS[TIER_MUST_KNOW]
            questions["action"] = {
                "needs_action": True,
                "label": "你要求提醒",
                "hint": "这条命中了你设置的条件，去核对具体内容。",
            }

        item = {
            "event_group_id": group_id or None,
            "state_key": state_key or None,
            "grouped": group.get("grouped"),
            "event_id": group.get("event_id"),
            "symbols": group.get("symbols") or [],
            "names": group.get("names") or [],
            "last_ts": group.get("last_ts"),
            "first_ts": group.get("first_ts"),
            "repeat_count": group.get("repeat_count", 1),
            "is_new_fact": prev is None,
            "risk_changed": risk_changed,
            "change_note": change_note,
            "hit_rule": hit_rule,
            **questions,
        }
        items.append(item)

    new_count = sum(1 for i in items if i["is_new_fact"])
    changed_count = sum(1 for i in items if i["risk_changed"])

    # 只留「新事实 / 风险变化 / 命中规则」三类。
    #
    # ``background`` 档即使构成"新事实"也不推：宏观/行业背景每天都在变，
    # 推它们就等于回到条数驱动。它仍会出现在 ``suppressed`` 里，需要时可查。
    def _keep(item: dict) -> bool:
        if item["hit_rule"] or item["risk_changed"]:
            return True
        return bool(item["is_new_fact"] and item["tier"] != TIER_BACKGROUND)

    kept = [i for i in items if _keep(i)]
    suppressed_items = [i for i in items if not _keep(i)]

    def _sort_key(item: dict) -> tuple:
        return (TIER_ORDER.index(item["tier"]), -(item.get("last_ts") or 0))

    kept.sort(key=_sort_key)
    suppressed_items.sort(key=_sort_key)
    if limit is not None and limit > 0:
        kept = kept[:limit]

    counts: dict[str, int] = {tier: 0 for tier in TIER_ORDER}
    for item in kept:
        counts[item["tier"]] += 1

    if not prior:
        limits.append(
            "未提供上次状态，所有条目都按「新事实」列出；"
            "要看真正的增量请把上次结果作为 prior_state 传入"
        )
    unknown_relation = sum(1 for i in items if i["why_related"]["relation"] == "unknown")
    if unknown_relation:
        limits.append(
            f"{unknown_relation}/{len(items)} 条缺少 directness / mapping_level，"
            "关系标为未判定并按线索处理"
        )
    no_impact = sum(1 for i in items if not i["impact_channel"]["available"])
    if no_impact:
        limits.append(f"{no_impact}/{len(items)} 条没有传导路径文本，影响渠道显示为未提供")

    return {
        "items": kept,
        "suppressed": {
            "count": len(suppressed_items),
            "items": suppressed_items[:20],
            "note": "已看过的旧事实；不重复提醒。传 prior_state 后这里才有意义。",
        },
        "counts": {
            "total_groups": len(groups),
            "after_dedupe": len(deduped),
            "items": len(kept),
            "new_facts": new_count,
            "risk_changed": changed_count,
            "by_tier": counts,
            "by_tier_label": {TIER_LABELS[t]: counts[t] for t in TIER_ORDER},
        },
        "limitations": limits,
        "note": "分级用于排序与措辞，不是买卖建议；theme_hint 档不可与公司公告同等对待。",
    }


def state_from_digest(digest: dict, *, include_suppressed: bool = True) -> dict[str, dict]:
    """从摘要里提取下次调用 ``prior_state`` 需要的状态。

    ``include_suppressed`` 默认 **True**：把「未推送」的条目也算进状态。理由是这样
    才算真正"看过"——下一轮它们的 ``is_new_fact`` 会变 False，不会在 ``new_facts``
    计数里重复出现。

    「用户点了『这些我看过了』」的场景要传 **False**：那句话指的是屏幕上这 N 条，
    把用户从没看见的条目一并记成"看过"会让文案里的数字对不上，也会让这些条目
    在真被推上来时缺少"全新"标记。
    """
    state: dict[str, dict] = {}
    sources = [*(digest.get("items") or [])]
    if include_suppressed:
        sources.extend((digest.get("suppressed") or {}).get("items") or [])
    for item in sources:
        # 用 state_key（没有 event_group_id 时是去重键派生的稳定 id）；
        # 只认 event_group_id 会让未归组的条目永远记不住、天天重报。
        key = item.get("state_key") or item.get("event_group_id")
        if not key:
            continue
        state[key] = {
            "verification": item["what_happened"].get("verification"),
            "mapping_level": (item["why_related"].get("mapping_levels") or [None])[0],
        }
    return state


__all__ = [
    "TIER_BACKGROUND",
    "TIER_DESCRIPTIONS",
    "TIER_LABELS",
    "TIER_MUST_KNOW",
    "TIER_ORDER",
    "TIER_THEME_HINT",
    "TIER_WORTH_NOTICE",
    "build_digest",
    "build_questions",
    "classify_relation",
    "dedupe",
    "grade",
    "state_from_digest",
]
