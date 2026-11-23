"""告警触发记录 API — 查询/清空/生成演示数据 alerts.jsonl。"""
from __future__ import annotations

import random
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import alert_digest, alert_groups, alert_seen, alert_store

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


@router.get("")
def list_alerts(
    request: Request,
    # 上限取存储侧保留策略 (alert_store.MAX_DAYS / MAX_RECORDS): 超出也没有可返回的记录。
    # 无下限时 days=-1 会把 cutoff 推到未来直接返回空, limit=-1 会走负数切片静默丢掉最旧一条。
    days: int = Query(alert_store.MAX_DAYS, ge=1, le=alert_store.MAX_DAYS),
    limit: int = Query(alert_store.MAX_RECORDS, ge=1, le=alert_store.MAX_RECORDS),
    source: str | None = None,
    type: str | None = None,
    ext_columns: str | None = None,
):
    """查询触发记录 (时间倒序)。

    ext_columns: 逗号分隔的 "configId.fieldName", 传入后按 symbol 富化行业/概念等 ext 字段,
    每条记录附带 {configId}__{fieldName} 键 (与 watchlist/screener 一致)。
    """
    events = alert_store.list_recent(
        _data_dir(request), days=days, limit=limit, source=source, type=type,
    )
    if ext_columns and events:
        try:
            from app.api.screener import _load_ext_value_maps, _rows_with_ext
            repo = request.app.state.repo
            value_maps = _load_ext_value_maps(repo, ext_columns)
            if value_maps:
                events = _rows_with_ext(events, value_maps)
        except Exception:  # noqa: BLE001
            pass
    total = alert_store.count(_data_dir(request))
    return {"alerts": events, "total": total}


@router.get("/summary")
def alerts_summary(
    request: Request,
    days: int = Query(alert_store.MAX_DAYS, ge=1, le=alert_store.MAX_DAYS),
    limit: int = Query(alert_store.MAX_RECORDS, ge=1, le=alert_store.MAX_RECORDS),
    source: str | None = None,
    type: str | None = None,
):
    """按来源 / 类型 / 事件三个维度分开统计。

    默认列表把模拟成交 (source=paper) 与政策新闻混在一起, 这里分开计数, 让界面能显示
    「政策/新闻 64 条 = N 个事件」而不是把成交日志也当成事件。
    事件维度只统计政策新闻, 且方向/等级按**事件数**计 (三家媒体转载同一政策 = 1 个利好)。
    """
    alerts = alert_store.list_recent(
        _data_dir(request), days=days, limit=limit, source=source, type=type,
    )
    stats = alert_groups.summarize(alerts)
    stats["filters"] = {"days": days, "limit": limit, "source": source, "type": type}
    return stats


@router.get("/event-groups")
def list_alert_events(
    request: Request,
    days: int = Query(alert_store.MAX_DAYS, ge=1, le=alert_store.MAX_DAYS),
    limit: int = Query(alert_store.MAX_RECORDS, ge=1, le=alert_store.MAX_RECORDS),
    group_limit: int = Query(200, ge=1, le=1000),
    source: str | None = None,
    type: str | None = None,
    kinds: str = alert_groups.KIND_POLICY,
    include_ungrouped: bool = True,
):
    """按事件归组返回告警 (最新在前), 每个事件可展开看全部来源证据。

    路径不叫 /events: api_gateway 把所有以 /events 结尾的路径当作 SSE 流端点排除在
    开放面之外 (见 api_gateway.required_scope 的流式端点守卫), 那样这个普通的
    JSON 查询接口会被 Token 客户端拒之门外。

    kinds: 逗号分隔的类别 (policy_news / trading / signal / other), 默认只看政策新闻。
    include_ungrouped: 无 event_group_id 的告警 (历史记录) 各自成一组, grouped=false。
        关掉它会静默少几条, 所以默认打开。
    """
    want = tuple(k.strip() for k in (kinds or "").split(",") if k.strip())
    bad = [k for k in want if k not in alert_groups.KINDS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"未知类别 {bad}, 可选: {list(alert_groups.KINDS)}",
        )
    alerts = alert_store.list_recent(
        _data_dir(request), days=days, limit=limit, source=source, type=type,
    )
    groups = alert_groups.group_events(
        alerts, kinds=want or (alert_groups.KIND_POLICY,), include_ungrouped=include_ungrouped,
    )
    return {
        "events": groups[:group_limit],
        "total": len(groups),
        "alerts": len(alerts),
        "truncated": len(groups) > group_limit,
        "summary": alert_groups.summarize(alerts)["events"],
    }


@router.get("/digest")
def alerts_digest(
    request: Request,
    days: int = Query(alert_store.MAX_DAYS, ge=1, le=alert_store.MAX_DAYS),
    limit: int = Query(alert_store.MAX_RECORDS, ge=1, le=alert_store.MAX_RECORDS),
    symbol: str | None = None,
    kinds: str = alert_groups.KIND_POLICY,
    max_items: int = Query(20, ge=1, le=200),
    use_seen: bool = Query(
        True,
        description="true 时用落盘的「已看过」状态判新事实；false 时全部当新事实",
    ),
    ack: bool = Query(
        False,
        description="true 时把本次结果记为「已看过」，下次不再重复提醒",
    ),
    since_ts: int | None = Query(
        None,
        description="显式时间切分：只把此后的当作新事实（优先于落盘状态）",
    ),
    watch_symbol: str | None = Query(None, description="命中即升到「需要知道」的标的"),
    watch_type: str | None = Query(None, description="命中即升到「需要知道」的告警类型"),
):
    """告警「少而重要」摘要：分级 + 四问 + 去重 + 只报增量。

    与 ``/event-groups`` 的区别是**面向"要不要看"而不是"有哪些"**：

    - 每条给出 ``tier``（must_know / worth_notice / background / theme_hint）、
      ``what_happened`` / ``why_related`` / ``impact_channel`` / ``action`` 四问；
    - 弱关联推断（如「上海推动海上风机」→ 宏柏新材）落到 ``theme_hint``，
      不会与公司公告并列；
    - 已看过的旧事实进 ``suppressed``，不重复打扰。**「已看过」是落盘的**
      （``data/user_data/alert_seen.json``），刷新页面/换设备仍然有效；
      只有 ``ack=true`` 时才会把它记进去 —— 读接口默认不改变状态；
    - 映射/核实字段缺失时如实标 ``unknown`` 与「未提供」，不编传导链。

    诚实边界见 ``app/services/alert_digest.py`` 与 ``alert_seen.py`` 的模块
    docstring；分级只用于排序与措辞，不是买卖建议。
    """
    want = tuple(k.strip() for k in (kinds or "").split(",") if k.strip())
    bad = [k for k in want if k not in alert_groups.KINDS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"未知类别 {bad}, 可选: {list(alert_groups.KINDS)}",
        )
    data_dir = _data_dir(request)
    alerts = alert_store.list_recent(data_dir, days=days, limit=limit)

    # 三种时间基准，优先级从高到低：
    #   1. since_ts（调用方显式指定）—— 用于"我上次看到这里"
    #   2. 落盘的已看过状态 —— 默认，跨刷新/跨设备有效
    #   3. 无状态 —— 全部按新事实（首次使用，或无状态文件）
    prior_state = None
    state_source = "none"
    if since_ts:
        seen = [a for a in alerts if int(a.get("ts") or 0) <= since_ts]
        unseen = [a for a in alerts if int(a.get("ts") or 0) > since_ts]
        if seen:
            prior_digest = alert_digest.build_digest(seen, kinds=want or (alert_groups.KIND_POLICY,))
            prior_state = alert_digest.state_from_digest(prior_digest)
            state_source = "since_ts"
        alerts = unseen
    elif use_seen:
        persisted = alert_seen.load(data_dir)
        if persisted:
            prior_state = persisted
            state_source = "persisted"

    rules = []
    if watch_symbol or watch_type:
        rules.append({"symbol": watch_symbol, "type": watch_type})

    digest = alert_digest.build_digest(
        alerts,
        kinds=want or (alert_groups.KIND_POLICY,),
        prior_state=prior_state,
        watch_rules=rules or None,
        symbol=symbol,
        limit=max_items,
    )

    # 只在明确 ack 时落盘。读接口默认不改状态 —— 否则前端 60 秒轮询一次，
    # 第一次拉完就把所有条目标成"看过"，用户根本没机会看到。
    #
    # 注意 ack 记的是**本次返回的条目**（受 max_items 截断），不是全部新事实：
    # 用户点了「这些我看过了」，说的是屏幕上这 N 条。被截断的其它条目下次仍会
    # 出现，这样文案必须讲清楚，否则用户以为"点一次就都清了"。
    seen_groups = None
    if ack:
        fresh = alert_digest.state_from_digest(digest, include_suppressed=False)
        if fresh:
            merged = alert_seen.record(data_dir, fresh)
            seen_groups = len(merged)
            digest["limitations"].append(
                f"本次展示的 {len(fresh)} 条已记为「已看过」（累计 {seen_groups} 条），"
                "下次不再把它们当新事实；被 max_items 截断的其它条目仍会出现"
            )
        else:
            digest["limitations"].append("本次没有可记录的事件，已看过状态未变化")

    digest["filters"] = {
        "days": days,
        "limit": limit,
        "symbol": symbol,
        "kinds": list(want),
        "max_items": max_items,
        "since_ts": since_ts,
        "use_seen": use_seen,
        "ack": ack,
        "state_source": state_source,
        "prior_groups": len(prior_state or {}),
    }
    # 查询窗口会静默丢更早的记录（alert_store 默认只保留 7 天）。实测
    # 605366.SH 的减持公告已经是 9.3 天前，按默认窗口就查不到 —— 不说明
    # 的话会被误读成"这只票没有公告"。
    digest["limitations"].append(
        f"只查最近 {days} 天（存储侧上限 {alert_store.MAX_DAYS} 天）；更早的告警不在本次结果里，"
        "不代表没发生过"
    )
    if not ack:
        digest["limitations"].append(
            "「已看过」只在调用方传 ack=true 时才更新；重复请求会重复列出同一条新事实"
        )
    return digest


@router.delete("")
def clear_alerts(request: Request):
    """清空全部触发记录。"""
    n = alert_store.clear(_data_dir(request))
    return {"ok": True, "cleared": n}

@router.delete("/{ts}")
def delete_alert(ts: int, request: Request):
    """删除单条触发记录 (按 ts 毫秒时间戳)。"""
    deleted = alert_store.delete_one(_data_dir(request), ts)
    if not deleted:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True}


# ── 演示数据生成 (仅 Dev 页用) ─────────────────────────

_DEMO_STOCKS = [
    ("600519.SH", "贵州茅台"), ("000001.SZ", "平安银行"), ("300750.SZ", "宁德时代"),
    ("002594.SZ", "比亚迪"), ("000858.SZ", "五粮液"), ("601318.SH", "中国平安"),
    ("002475.SZ", "立讯精密"), ("600036.SH", "招商银行"), ("000725.SZ", "京东方A"),
    ("300059.SZ", "东方财富"),
]
_DEMO_TEMPLATES = [
    ("signal", "MA金叉触发", ["signal_ma_golden_5_20"], "info"),
    ("signal", "放量突破新高", ["signal_volume_surge", "signal_n_day_high"], "warn"),
    ("signal", "MACD金叉", ["signal_macd_golden"], "info"),
    ("signal", "跌破MA20", ["signal_ma20_breakdown"], "info"),
    ("price", "涨幅超 5%", [], "warn"),
    ("price", "RSI 极度超卖", [], "warn"),
    ("price", "跌幅超 3%", [], "info"),
    ("market", "涨停封板", ["signal_limit_up"], "critical"),
    ("market", "连板异动", ["signal_limit_up"], "warn"),
    ("market", "炸板", ["signal_broken_limit_up"], "warn"),
    # 新策略变更格式
    ("strategy", "策略「趋势突破」进入 贵州茅台 +2.3%", ["signal_n_day_high", "signal_volume_surge"], "info"),
    ("strategy", "策略「趋势突破」移出 五粮液 -1.5%", ["signal_ma20_breakdown"], "info"),
    ("strategy", "策略「新低反转」进入 平安银行 +1.1%", ["signal_n_day_low"], "warn"),
    ("strategy", "策略「MACD金叉」移出 比亚迪 -0.8%", ["signal_macd_golden"], "info"),
    # 批量变更
    ("strategy", "策略「趋势突破」进入 6 只：平安银行、宁德时代、比亚迪、东方财富、招商银行、立讯精密", [], "info"),
    ("strategy", "策略「MACD金叉」移出 7 只：京东方A、平安银行、五粮液、立讯精密、招商银行、东方财富、比亚迪", [], "warn"),
]


@router.post("/seed")
def seed_demo_alerts(request: Request, count: int = 12, recent: bool = True):
    """生成演示触发记录 (Dev 页用)。

    Args:
        count: 生成条数 (1-50)
        recent: True=时间戳设为"刚刚"(用于测试闪烁效果); False=分散在近3天
    """
    count = max(1, min(50, count))
    now_ms = int(time.time() * 1000)
    events = []
    for i in range(count):
        source, message, signals, severity = _DEMO_TEMPLATES[i % len(_DEMO_TEMPLATES)]
        sym, name = _DEMO_STOCKS[i % len(_DEMO_STOCKS)]
        # 策略类型按消息推导 type: new_entry / dropped, 否则沿用 source
        if source == "strategy":
            if "进入" in message:
                ev_type = "new_entry"
            elif "移出" in message:
                ev_type = "dropped"
            else:
                ev_type = "strategy"
        else:
            ev_type = source
        # recent 模式: 时间戳从现在往前每条错开 30 秒 (最新在前)
        ts = now_ms - (i * 30000) if recent else now_ms - random.randint(60, 4320) * 60 * 1000
        events.append({
            "ts": ts,
            "rule_id": f"demo_rule_{i}",
            "rule_name": message,
            "source": source,
            "type": ev_type,
            "symbol": "" if source == "strategy" and ("只：" in message) else sym,
            "name": name,
            "message": message,
            "price": round(random.uniform(8, 1800), 2) if not (source == "strategy" and "只：" in message) else None,
            "change_pct": round(random.uniform(-0.06, 0.098), 4) if not (source == "strategy" and "只：" in message) else None,
            "signals": signals,
            "severity": severity,
        })
    alert_store.append_many(_data_dir(request), events)

    # 同步推入 SSE 队列, 让所有连着 SSE 的客户端实时收到 (不依赖轮询)
    qs = getattr(request.app.state, "quote_service", None)
    if qs:
        # 转成 SSE 推送格式 (和 _evaluate_monitors 一致)
        sse_alerts = [{
            "source": ev["source"],
            "type": ev["type"],
            "rule_id": ev.get("rule_id"),
            "symbol": ev["symbol"],
            "name": ev["name"],
            "message": ev["message"],
            "price": ev["price"],
            "change_pct": ev["change_pct"],
            "signals": ev["signals"],
            "severity": ev.get("severity", "info"),
        } for ev in events]
        qs.push_alerts(sse_alerts)

    return {"ok": True, "generated": len(events)}

