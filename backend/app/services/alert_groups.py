"""告警分组与统计 — 区分「成交日志 / 策略信号 / 政策新闻」, 并把同一政策的转载算作一个事件。

为什么需要这一层 (用户明确要求):

1. ``alerts.jsonl`` 是混合流水。实测 242 条里 ``source="paper"`` (模拟成交/委托日志)
   177 条、政策类 64 条, 全写进同一个文件。默认列表把两者混在一起, 用户会把成交日志
   当成"事件", 也会把一家媒体的多条转载累计成"多份利好"。所以统计必须按
   **来源 / 类型 / 事件** 三个维度分开, 且事件维度只对政策新闻计数。
2. 同一政策常被多家媒体转载。数据侧由 databridge ``event_group.py`` 写 ``event_group_id``,
   但**历史告警没有这个字段**, 且它以"新落盘的告警"为界。因此这里在缺失时退回
   「每条告警自成一组」—— 宁可高估事件数, 也不能为了凑数把不同政策并成一个事件。
3. 事件维度计数取**组内最弱的一条**核实状态 (与 databridge 侧同序): 一个官方原文
   加一条语义推测, 整组仍只能是「系统推断」, 不能因为有一条硬的就把整组说成事实。
"""
from __future__ import annotations

import hashlib

# ── 分类口径 (写死在常量里, 便于测试锁住) ─────────────────────

POLICY_SOURCES = ("policy_radar", "policy_calendar")
TRADING_SOURCES = ("paper",)
TRADING_TYPES = ("paper_fill", "fill", "auto_order")
SIGNAL_SOURCES = ("signal", "price", "market", "strategy")

KIND_POLICY = "policy_news"
KIND_LABELS = {
    KIND_POLICY: "政策/新闻事件",
    "trading": "模拟成交/委托日志",
    "signal": "策略/行情信号",
    "other": "其他",
}
KINDS = tuple(KIND_LABELS)

# 核实状态强弱 (与 databridge/policy_radar.py 的 VERIFY_RANK 同序)
VERIFY_RANK = {"pending": 0, "inferred": 1, "reported": 2, "fact": 3, "evidenced": 4}
VERIFY_LABELS = {
    "pending": "待核实",
    "inferred": "系统推断",
    "reported": "媒体报道",
    "fact": "已核实事实",
    "evidenced": "证据可核对",
}

# 事件展开时返回的成员告警字段 (够看清证据即可; 完整字段仍走 /api/alerts)
MEMBER_KEYS = (
    "ts", "source", "type", "symbol", "name", "message", "severity",
    "mapping_level", "mapping_level_desc", "mapping_basis", "ai_link",
    "verification", "verification_label", "directness", "uncertainty",
    "impact_path_text", "impact_path",
    "source_name", "source_url", "source_host", "source_tier", "source_tier_label",
    "published_at", "event_date", "effective_date",
    "event_id", "event_group_id", "event_update_seq", "is_event_update",
    "event_title", "event_direction", "event_sources", "event_source_count",
)


def classify_alert(ev: dict) -> str:
    """把一条告警归入 policy_news / trading / signal / other。

    判定顺序即优先级, 注释即口径 (政策优先于类型, 因为政策告警也可能带 fill 之类字段)。
    """
    source = ev.get("source") or ""
    type_ = ev.get("type") or ""
    if source in POLICY_SOURCES:
        return KIND_POLICY
    if type_ in TRADING_TYPES or source in TRADING_SOURCES:
        return "trading"
    if source in SIGNAL_SOURCES:
        return "signal"
    return "other"


def group_key(ev: dict) -> str:
    """归组键: 有 event_group_id 用事件组, 否则「每条告警自成一组」。

    回退路径**不能**用 ts+symbol 造键: 同一轮运行的告警 ts 是同一毫秒, 实测会把
    「谷歌数据中心扩建(利好)」与「美的回购公告(不确定)」并成一个事件, 还虚增出
    10 个假的多来源事件 —— 正是用户警告的"把不同事件并成一个"。

    改用告警自身的 event_id (单篇报道哈希), 没有就退回标题消息哈希; 再叠上 symbol 与 ts:
      - 同一毫秒 + 不同政策 → 标题哈希不同 → 两组 (修掉上面那个坑);
      - 同一毫秒 + 同一条消息重复落两次 → 一条事实, 一组;
      - 不同时刻的同类记录 (如两笔成交) → 两条记录, 两组, 不会被吞掉。
    """
    gid = ev.get("event_group_id")
    if gid:
        return f"grp:{gid}"
    ident = ev.get("event_id") or hashlib.md5(
        (ev.get("message") or "").encode("utf-8"),
    ).hexdigest()[:12]
    return f"one:{ev.get('symbol') or ''}:{ev.get('ts')}:{ident}"


def _dedupe_key(src: dict) -> tuple:
    """来源去重: 优先按链接, 无链接按「媒体名 + 标题」。"""
    url = (src.get("url") or "").strip()
    if url:
        return ("url", url)
    return ("title", (src.get("name") or "").strip(), (src.get("title") or "").strip())


def _sources_of(members: list[dict]) -> list[dict]:
    """汇总组内全部来源证据。

    两个来源: ①告警自带的 ``event_sources`` (数据侧已把组内各家整理好);
    ②成员告警自己的 ``source_name/source_url`` —— 历史告警没有前者的兜底,
    少了它, 展开时会把"多家转载"显示成只有一家。

    同一条来源被两处描述时**按字段补全**而不是丢弃: 2026-11-09 首次真实
    运行里 ``event_sources[].name`` 落成了 None (采集侧当时不提供媒体名),
    而成员告警的 ``source_name`` 是正确的「东方财富」; 早期实现只保留
    先到的那个, 结果展开视图显示「None 媒体转述」—— 用户看不出是哪家媒体。
    现在合并取并集, 历史数据无需重跑也能在读取端被修好。
    """
    found: dict[tuple, dict] = {}

    def _add(item: dict) -> None:
        key = _dedupe_key(item)
        if not any(key):
            return
        cur = found.get(key)
        if cur is None:
            found[key] = dict(item)
            return
        for field, value in item.items():
            if not cur.get(field) and value:
                cur[field] = value

    for ev in members:
        for item in ev.get("event_sources") or ():
            if isinstance(item, dict):
                _add({
                    "title": item.get("title"),
                    "name": item.get("name"),
                    "url": item.get("url"),
                    "published_at": item.get("published_at"),
                    "channel": item.get("channel"),
                    "tier": item.get("tier"),
                    "tier_label": item.get("tier_label"),
                })
        _add({
            "title": ev.get("event_title") or (ev.get("message") or "")[:80],
            "name": ev.get("source_name"),
            "url": ev.get("source_url"),
            "published_at": ev.get("published_at"),
            "channel": None,
            "tier": ev.get("source_tier"),
            "tier_label": ev.get("source_tier_label"),
        })
    out = list(found.values())
    # 有发布时间的按报道先后排, 未知时间排最后 (展开时按时间读证据更自然)
    out.sort(key=lambda s: (s.get("published_at") in (None, "", "unknown"), s.get("published_at") or ""))
    return out


def _weakest_verification(members: list[dict]) -> str:
    """组内最弱核实状态 (pending < inferred < reported < fact/evidenced)。"""
    ranks = [VERIFY_RANK.get(ev.get("verification") or "", 0) for ev in members]
    if not ranks:
        return "pending"
    worst = min(ranks)
    for name, rank in VERIFY_RANK.items():
        if rank == worst:
            return name
    return "pending"


def _unique(values) -> list:
    out = []
    for v in values:
        if v in (None, "", "unknown"):
            continue
        if v not in out:
            out.append(v)
    return out


def _member(ev: dict) -> dict:
    return {k: ev.get(k) for k in MEMBER_KEYS if k in ev}


def group_events(
    alerts: list[dict],
    *,
    kinds: tuple[str, ...] = (KIND_POLICY,),
    include_ungrouped: bool = True,
) -> list[dict]:
    """按事件归组, 返回事件列表 (最新在前)。

    kinds: 只对哪些类别归组, 默认只归组政策/新闻 (成交日志不是"事件")。
    include_ungrouped: 无 ``event_group_id`` 的告警是否各自成一组。
        True 时它们以 ``grouped=False`` 出现, 保证列表不会"静默少几条"。
    """
    selected = [ev for ev in alerts if classify_alert(ev) in kinds]

    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for ev in selected:
        gid = ev.get("event_group_id")
        if not gid and not include_ungrouped:
            continue
        key = group_key(ev)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(ev)

    groups: list[dict] = []
    for key in order:
        members = sorted(buckets[key], key=lambda x: x.get("ts", 0), reverse=True)
        gid = members[0].get("event_group_id") or ""
        sources = _sources_of(members)
        veri = _weakest_verification(members)
        event_ids = _unique([m.get("event_id") for m in members])
        groups.append({
            "event_group_id": gid,
            "grouped": bool(gid),
            # event_id 标识**单篇报道**, event_group_id 才是"同一件事"。
            # 两者都要给: 只有单篇 id 时跨媒体对不上, 只有组 id 时追溯不到原文。
            "event_id": event_ids[0] if event_ids else None,
            "event_ids": event_ids,
            "title": members[0].get("event_title") or (members[0].get("message") or "")[:80],
            "event_date": members[0].get("event_date") or "unknown",
            "effective_date": members[0].get("effective_date") or "unknown",
            "update_seq": max([m.get("event_update_seq") or 1 for m in members]),
            "has_update": any(bool(m.get("is_event_update")) for m in members),
            "alert_count": len(members),
            "symbols": _unique([m.get("symbol") for m in members]),
            "names": _unique([m.get("name") for m in members]),
            "directions": _unique([m.get("event_direction") for m in members]),
            "mapping_levels": _unique([m.get("mapping_level") for m in members]),
            "verification": veri,
            "verification_label": VERIFY_LABELS.get(veri, veri),
            "source_count": len(sources),
            "sources": sources,
            "first_ts": min([m.get("ts") or 0 for m in members]),
            "last_ts": max([m.get("ts") or 0 for m in members]),
            "alerts": [_member(m) for m in members],
        })

    groups.sort(key=lambda g: g["last_ts"], reverse=True)
    return groups


def summarize(alerts: list[dict]) -> dict:
    """按来源 / 类型 / 事件三个维度分别计数。

    事件维度只统计政策新闻, 且 ``by_direction`` 数的是**事件数**而非告警条数 ——
    同一政策被三家媒体转载, 在这里是 1 个利好, 不是 3 个。
    """
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for ev in alerts:
        kind = classify_alert(ev)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        src = ev.get("source") or "(无)"
        by_source[src] = by_source.get(src, 0) + 1
        type_ = ev.get("type") or "(无)"
        by_type[type_] = by_type.get(type_, 0) + 1

    groups = group_events(alerts, kinds=(KIND_POLICY,), include_ungrouped=True)
    by_direction: dict[str, int] = {}
    by_verification: dict[str, int] = {}
    by_mapping: dict[str, int] = {}
    by_date: dict[str, int] = {}
    for g in groups:
        for d in g["directions"] or ["不确定"]:
            by_direction[d] = by_direction.get(d, 0) + 1
        label = g["verification_label"]
        by_verification[label] = by_verification.get(label, 0) + 1
        for lvl in g["mapping_levels"] or ["?"]:
            by_mapping[lvl] = by_mapping.get(lvl, 0) + 1
        date = g["event_date"] or "unknown"
        by_date[date] = by_date.get(date, 0) + 1

    policy_alerts = by_kind.get(KIND_POLICY, 0)
    return {
        "total": len(alerts),
        "by_kind": by_kind,
        "by_kind_label": {KIND_LABELS.get(k, k): v for k, v in by_kind.items()},
        "by_source": by_source,
        "by_type": by_type,
        "events": {
            "count": len(groups),
            "alerts": policy_alerts,
            "ungrouped": sum(1 for g in groups if not g["grouped"]),
            "multi_source": sum(1 for g in groups if g["source_count"] >= 2),
            "updates": sum(1 for g in groups if g["has_update"]),
            "by_direction": by_direction,
            "by_verification": by_verification,
            "by_mapping_level": by_mapping,
            "by_event_date": by_date,
        },
    }
