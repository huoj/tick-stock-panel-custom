"""告警事件级接口 — 同一事件归组、按事件统计、可展开全部来源证据。

对应需求: 「政策新闻可能被多家媒体重复报道, 不能按多条告警累计成多份利好或利空;
建立稳定事件 ID, 新进展作为事件更新保存; 界面按事件数统计, 并能展开查看所有来源证据。」

这些测试锁住三件容易做错的事:
  1. 模拟成交日志 (source=paper) 不是"事件", 不能混进事件计数;
  2. 同一事件的多家转载算 1 个事件, 方向也按事件数计 (3 条转载 ≠ 3 个利好);
  3. 没有 event_group_id 的历史告警必须各自成组保留 —— 宁可高估事件数, 不能静默丢记录,
     也不能为了凑数把不同政策并成一个事件。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.alerts import router
from app.services import alert_store


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _policy(ts: int, *, group: str = "", symbol: str = "600941.SH", name: str = "中国移动",
            direction: str = "利好", verification: str = "reported", url: str = "",
            title: str = "国常会完善资本市场稳市机制", level: str = "B",
            update_seq: int = 1, is_update: bool = False, sources: list | None = None) -> dict:
    ev = {
        "ts": ts, "source": "policy_radar", "type": "policy_政策", "rule_id": "policy_radar",
        "symbol": symbol, "name": name, "message": f"【强关联】▲ {direction}·政策 | {title}",
        "severity": "warn", "mapping_level": level, "mapping_level_desc": "强关联",
        "mapping_basis": "精确命中:行业", "ai_link": "直接",
        "verification": verification, "verification_label": {"reported": "媒体报道"}.get(verification, ""),
        "directness": "直接", "uncertainty": "传导环节未验证", "impact_path_text": "关税→成本→利润",
        "impact_path": ["关税", "成本", "利润"],
        "source_name": "东方财富", "source_url": url or "https://finance.eastmoney.com/a/1.html",
        "source_host": "finance.eastmoney.com", "source_tier": "media", "source_tier_label": "媒体转述",
        "published_at": "2026-11-08 15:09:38", "event_date": "unknown", "effective_date": "unknown",
        "event_id": f"evt_{abs(hash(url or title)) % 10 ** 8:08d}",
        "event_group_id": group, "event_update_seq": update_seq, "is_event_update": is_update,
        "event_title": title, "event_direction": direction,
    }
    if sources is not None:
        ev["event_sources"] = sources
        ev["event_source_count"] = len(sources)
    return ev


def _trading(ts: int) -> dict:
    return {"ts": ts, "source": "paper", "type": "paper_fill", "symbol": "600519.SH",
            "name": "贵州茅台", "message": "模拟买入 100 股", "severity": "info"}


def _signal(ts: int) -> dict:
    return {"ts": ts, "source": "strategy", "type": "new_entry", "symbol": "000001.SZ",
            "name": "平安银行", "message": "策略「趋势突破」进入 平安银行", "severity": "info"}


def _seed(tmp_path) -> int:
    now = int(time.time() * 1000)
    gid = "evtgrp_aaa111"
    alert_store.append_many(tmp_path, [
        # 同一事件, 三家媒体转载 (其中一条是后续进展)
        _policy(now - 3000, group=gid, url="https://finance.eastmoney.com/a/1.html"),
        _policy(now - 2000, group=gid, url="https://news.futunn.com/a/2.html", verification="reported"),
        _policy(now - 1000, group=gid, url="https://www.cls.cn/a/3.html", update_seq=2, is_update=True),
        # 同一事件影响另一只股票 (方向相反) —— 与第一篇同一篇报道, 不应多算一个来源
        _policy(now - 2500, group=gid, symbol="601633.SH", name="长城汽车", direction="利空",
                url="https://finance.eastmoney.com/a/1.html"),
        # 另一件事
        _policy(now - 4000, group="evtgrp_bbb222", symbol="300750.SZ", name="宁德时代",
                direction="利空", title="欧元区通胀超预期"),
        # 历史告警: 没有 event_group_id, 必须各自成组
        _policy(now - 5000, symbol="159682.SZ", name="创业板50ETF", direction="不确定",
                title="老旧小区改造提速", verification="pending"),
        _policy(now - 6000, symbol="601318.SH", name="中国平安", direction="利好",
                title="郑州支持公积金提取装修", verification="pending"),
        # 非事件流水
        _trading(now - 7000), _trading(now - 7100), _signal(now - 7200),
    ])
    return now


# ── 分类: 成交日志不是事件 ─────────────────────────────


def test_summary_separates_trading_from_policy_news(tmp_path):
    _seed(tmp_path)
    body = _client(tmp_path).get("/api/alerts/summary").json()
    assert body["by_kind"]["policy_news"] == 7
    assert body["by_kind"]["trading"] == 2
    assert body["by_kind"]["signal"] == 1
    assert body["total"] == 10
    # 人类可读标签同时给出, 免得前端各自维护一套中文
    assert body["by_kind_label"]["模拟成交/委托日志"] == 2
    assert body["by_source"]["paper"] == 2


def test_events_exclude_trading_by_default_but_can_include_it(tmp_path):
    _seed(tmp_path)
    client = _client(tmp_path)
    default = client.get("/api/alerts/event-groups").json()
    assert all("模拟买入" not in (a["message"] or "") for e in default["events"] for a in e["alerts"])

    only_trading = client.get("/api/alerts/event-groups", params={"kinds": "trading"}).json()
    assert only_trading["total"] == 2
    assert all(e["grouped"] is False for e in only_trading["events"])


def test_unknown_kind_is_rejected(tmp_path):
    _seed(tmp_path)
    resp = _client(tmp_path).get("/api/alerts/event-groups", params={"kinds": "policy_news,nope"})
    assert resp.status_code == 422, resp.text
    assert "nope" in resp.text


# ── 归组: 多家转载 = 一个事件 ───────────────────────────


def test_multiple_outlets_collapse_into_one_event(tmp_path):
    _seed(tmp_path)
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    groups = {e["event_group_id"]: e for e in body["events"] if e["grouped"]}
    g = groups["evtgrp_aaa111"]
    assert g["alert_count"] == 4                      # 3 家转载 + 1 只关联个股
    assert g["source_count"] == 3                     # 同一链接不重复计
    assert sorted(g["symbols"]) == ["600941.SH", "601633.SH"]
    assert g["update_seq"] == 2 and g["has_update"] is True
    urls = {s["url"] for s in g["sources"]}
    assert urls == {
        "https://finance.eastmoney.com/a/1.html",
        "https://news.futunn.com/a/2.html",
        "https://www.cls.cn/a/3.html",
    }


def test_direction_is_counted_per_event_not_per_alert(tmp_path):
    _seed(tmp_path)
    body = _client(tmp_path).get("/api/alerts/summary").json()
    ev = body["events"]
    # 4 条告警同属一个事件 (含 3 家转载) → 事件级只算 1 个利好, 不能累计成 4 份
    assert ev["count"] == 4                    # aaa111 + bbb222 + 2 条无组历史告警
    assert ev["alerts"] == 7                   # 政策告警总条数 (含 4 条同组)
    assert ev["ungrouped"] == 2
    assert ev["multi_source"] == 1
    assert ev["updates"] == 1
    # aaa111 同时含利好与利空 (同一事件对两只股票方向相反), 各计一次; bbb222 利空
    assert ev["by_direction"] == {"利好": 2, "利空": 2, "不确定": 1}


def test_ungrouped_alerts_are_kept_and_not_merged(tmp_path):
    _seed(tmp_path)
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    ungrouped = [e for e in body["events"] if not e["grouped"]]
    assert len(ungrouped) == 2
    # 两条历史告警是两件不同的事, 必须各自成组, 不能被并成一条
    assert {e["title"] for e in ungrouped} == {"郑州支持公积金提取装修", "老旧小区改造提速"}


def test_include_ungrouped_false_only_drops_ungrouped(tmp_path):
    _seed(tmp_path)
    client = _client(tmp_path)
    with_all = client.get("/api/alerts/event-groups").json()
    without = client.get("/api/alerts/event-groups", params={"include_ungrouped": False}).json()
    assert with_all["total"] == 4
    assert without["total"] == 2
    assert all(e["grouped"] for e in without["events"])


def test_group_verification_is_the_weakest_member(tmp_path):
    now = int(time.time() * 1000)
    alert_store.append_many(tmp_path, [
        _policy(now - 1000, group="evtgrp_ccc", verification="fact"),
        _policy(now - 2000, group="evtgrp_ccc", verification="pending",
                url="https://www.cls.cn/a/9.html"),
    ])
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    g = body["events"][0]
    # 一条硬证据 + 一条推测 → 整组仍只能是「待核实」, 不能被最硬的那条抬上去
    assert g["verification"] == "pending"
    assert g["verification_label"] == "待核实"
    assert body["summary"]["by_verification"] == {"待核实": 1}


def test_same_millisecond_different_policies_are_not_merged(tmp_path):
    """同一轮运行的告警 ts 是同一毫秒: 回退键若用 ts+symbol, 不同政策会被并成一个事件。

    这是真实数据里踩到的坑 (实测 64 条历史政策告警被并成 49 个"事件", 其中 10 个
    假的"多来源事件"是 [谷歌数据中心 利好] 与 [美的回购公告 不确定] 这类拼在一起的)。
    """
    now = int(time.time() * 1000)
    a = _policy(now, symbol="000333.SZ", name="美的集团", direction="利好",
                title="谷歌150亿美元扩建马来西亚数据中心")
    b = _policy(now, symbol="000333.SZ", name="美的集团", direction="不确定",
                title="美的集团:关于回购A股股份进展情况的公告")
    a.pop("event_id"), b.pop("event_id")          # 历史告警连 event_id 都没有
    alert_store.append_many(tmp_path, [a, b])
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    assert body["total"] == 2, body["events"]
    assert all(e["alert_count"] == 1 and e["source_count"] == 1 for e in body["events"])
    assert body["summary"]["multi_source"] == 0


def test_duplicate_identical_alert_in_same_run_is_one_group(tmp_path):
    """同一条消息在同一毫秒重复落两次 = 同一条事实, 合成一组而不是计两个事件。"""
    now = int(time.time() * 1000)
    a = _policy(now)
    b = _policy(now)
    a.pop("event_id"), b.pop("event_id")
    alert_store.append_many(tmp_path, [a, b])
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    assert body["total"] == 1
    assert body["events"][0]["alert_count"] == 2


def test_sources_fall_back_to_member_links_when_event_sources_missing(tmp_path):
    """历史告警没有 event_sources, 展开时也要能看到各家链接, 不能只显示一家。"""
    now = int(time.time() * 1000)
    alert_store.append_many(tmp_path, [
        _policy(now - 1000, group="evtgrp_ddd", url="https://finance.eastmoney.com/a/1.html"),
        _policy(now - 2000, group="evtgrp_ddd", url="https://news.futunn.com/a/2.html"),
    ])
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    g = body["events"][0]
    assert g["source_count"] == 2
    assert {s["url"] for s in g["sources"]} == {
        "https://finance.eastmoney.com/a/1.html", "https://news.futunn.com/a/2.html"}


def test_event_sources_field_is_preferred_and_deduped(tmp_path):
    now = int(time.time() * 1000)
    alert_store.append_many(tmp_path, [
        _policy(now - 1000, group="evtgrp_eee", sources=[
            {"title": "国常会完善资本市场稳市机制", "name": "东方财富",
             "url": "https://finance.eastmoney.com/a/1.html", "published_at": "2026-11-08 15:09:38",
             "channel": "em", "tier": "media", "tier_label": "媒体转述"},
            {"title": "国常会完善资本市场稳市机制", "name": "财联社",
             "url": "https://www.cls.cn/a/3.html", "published_at": "2026-11-08 15:30:00",
             "channel": "cls", "tier": "media", "tier_label": "媒体转述"},
        ]),
    ])
    body = _client(tmp_path).get("/api/alerts/event-groups").json()
    g = body["events"][0]
    # 自带 event_sources 已含两条; 成员自身的链接是同一条, 必须去重而不是变成 3 家
    assert g["source_count"] == 2
    assert [s["name"] for s in g["sources"]] == ["东方财富", "财联社"]


# ── 入参与边界 ────────────────────────────────────────


@pytest.mark.parametrize("path", ["/api/alerts/summary", "/api/alerts/event-groups"])
@pytest.mark.parametrize("params", [{"days": -1}, {"days": 0}, {"limit": -1}, {"limit": 0}])
def test_out_of_range_params_are_rejected(tmp_path, path, params):
    _seed(tmp_path)
    assert _client(tmp_path).get(path, params=params).status_code == 422


@pytest.mark.parametrize("path", ["/api/alerts/summary", "/api/alerts/event-groups"])
def test_above_retention_is_rejected(tmp_path, path):
    _seed(tmp_path)
    client = _client(tmp_path)
    assert client.get(path, params={"days": alert_store.MAX_DAYS + 1}).status_code == 422
    assert client.get(path, params={"limit": alert_store.MAX_RECORDS + 1}).status_code == 422


def test_events_group_limit_truncates_with_flag(tmp_path):
    _seed(tmp_path)
    body = _client(tmp_path).get("/api/alerts/event-groups", params={"group_limit": 2}).json()
    assert len(body["events"]) == 2
    assert body["truncated"] is True
    assert body["total"] == 4          # total 仍是真实事件数, 不因截断而变


def test_nameless_event_source_is_repaired_from_member(tmp_path):
    """来源名缺失时要在读取端补全, 不能显示「None 媒体转述」。

    2026-11-09 首次真实运行里 ``event_sources[].name`` 全是 None (采集侧当时
    不给媒体名), 而成员告警的 ``source_name`` 是正确的「东方财富」。早期实现
    按链接去重、只留先到的那条, 于是 None 把正确名字挤掉了。
    """
    ts = int(time.time() * 1000)
    alert_store.append_many(tmp_path, [_policy(
        ts, group="evtgrp_nameless",
        sources=[{"title": "国常会完善资本市场稳市机制", "name": None,
                  "url": "https://finance.eastmoney.com/a/1.html",
                  "published_at": "2026-11-08 15:09:38", "channel": "em",
                  "tier": "media", "tier_label": "媒体转述"}],
    )])
    g = _client(tmp_path).get("/api/alerts/event-groups").json()["events"][0]
    assert g["source_count"] == 1, "同一条来源被两处描述时不能算成两家"
    assert g["sources"][0]["name"] == "东方财富"
    assert g["sources"][0]["tier_label"] == "媒体转述"


def test_event_source_keeps_richest_fields_on_merge(tmp_path):
    """合并两条同链接的描述时取字段并集 (谁有值用谁), 先到的空值不能被保留。"""
    ts = int(time.time() * 1000)
    alert_store.append_many(tmp_path, [_policy(
        ts, group="evtgrp_merge", url="https://finance.eastmoney.com/a/2.html",
        sources=[{"title": "原标题", "name": None, "url": "https://finance.eastmoney.com/a/2.html",
                  "published_at": None, "channel": "em", "tier": None, "tier_label": None}],
    )])
    g = _client(tmp_path).get("/api/alerts/event-groups").json()["events"][0]
    src = g["sources"][0]
    assert src["name"] == "东方财富"                     # 来自成员告警
    assert src["channel"] == "em"                        # 来自 event_sources
    assert src["published_at"] == "2026-11-08 15:09:38"
    assert src["tier"] == "media" and src["tier_label"] == "媒体转述"
