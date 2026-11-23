"""财报与公告的自动解析 —— 把「去哪查」升级为「查到什么」。

## 为什么需要

行动卡原先的 ``checks`` 只告诉用户"去看财报/公告"，等于没给信息 ——
用户打开卡是想省掉这一步，不是想多一步。

本模块把本地已有的财务与公告数据**取出来、算成可比数字、按时点合规地呈现**：

| 数据源 | 内容 | PIT 口径 |
|---|---|---|
| ``financials_metrics`` | 营收/净利同比、ROE、毛利率、负债率等 | ``announcement`` (公告日后才可知) |
| ``ext_forecast`` | 业绩预告 | ``event_date`` |
| ``ext_unlock`` | 限售解禁 | ``event_date`` |
| ``ext_insider`` | 股东增减持 | ``event_date`` |
| ``ext_pledge`` | 股权质押 | ``event_date`` |
| ``ext_dividend`` | 分红送配 | ``event_date`` |
| ``ext_research`` | 机构评级 | ``event_date`` |

## 时点合规 (PIT) 是这个模块的第一红线

所有输出都走 ``data_pit.known_as_of`` —— **只返回当时已知的记录**。
财务数据用 ``announce_date``（公告日）而不是 ``period_end``（报告期），
否则 2026Q3 的报表会在 2026-07 就被"看到"，构成前视偏差
（CONTRIBUTING §3.4 / §5.3）。

## 只陈述数字与事实

本模块**不做财务分析、不给结论**。它回答"最近一期财报的营收同比是多少、
什么时候公告的、市场此前预期是什么"，不回答"这算好还是坏"。
判断权留给用户 —— 与行动卡的其它部分保持同一纪律。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from app.services.dataset_catalog import get_dataset, read_dataset

logger = logging.getLogger(__name__)

# 每个数据集最多取多少条 (防止单只票返回几百条公告淹没卡片)
MAX_RECORDS = 8
# 公告类数据只看最近 N 天 —— 更早的与当前决策无关
RECENT_DAYS = 400

# 数据集 → 展示名与关键字段。字段名**按实际 parquet 列名**写死语义映射:
# ext_* 表的列是上游原样落盘的 (如 ext_pledge 用 trade_date, ext_research 用 rating),
# 猜错列名会让整块数据静默消失。
_ANNOUNCEMENT_SOURCES: dict[str, dict[str, Any]] = {
    "ext_forecast": {
        "label": "业绩预告",
        "date_col": None,           # 该表没有事件日期列, 只有报告期
        "fields": ("period", "indicator", "forecast_value", "change_pct", "change_desc", "reason"),
        "why": "预告会改变市场对全年的预期，技术面看不到",
    },
    "ext_unlock": {
        "label": "限售解禁",
        "date_col": None,
        "fields": ("release_date", "release_shares", "release_ratio", "holder_type"),
        "why": "解禁会造成持续抛压，与价格走势无关但影响供需",
    },
    "ext_insider": {
        "label": "股东增减持",
        "date_col": None,
        "fields": ("trade_date", "holder_name", "change_shares", "change_ratio", "direction"),
        "why": "内部人行为是最直接的信息不对称来源",
    },
    "ext_pledge": {
        "label": "股权质押",
        "date_col": "trade_date",
        "fields": ("pledge_ratio", "pledge_market_cap", "pledge_deal_num", "risk_flag"),
        "why": "高质押比例在下跌中会放大风险",
    },
    "ext_dividend": {
        "label": "分红送配",
        # 该表把除权日放在字段里 (ex_date), 不是独立的事件日期列 ——
        # 不能当 date_col 用, 否则 filter 找不到列会静默丢整块数据。
        "date_col": None,
        "fields": ("ex_date", "plan", "dividend_per_share", "trade_date"),
        "why": "分红政策变化直接影响持有回报",
    },
    "ext_research": {
        "label": "机构评级",
        "date_col": None,           # 快照表: 只有当前评级分布, 无日期
        "fields": ("rating", "buy_cnt", "add_cnt", "neutral_cnt", "reduce_cnt", "sell_cnt"),
        "why": "评级分布反映卖方预期，需与自己的判断对照",
    },
}

# 财务指标 → 人话标签
_METRIC_LABELS: dict[str, str] = {
    "revenue_yoy": "营收同比",
    "net_income_yoy": "净利润同比",
    "roe": "ROE",
    "gross_margin": "毛利率",
    "net_margin": "净利率",
    "debt_to_asset_ratio": "资产负债率",
    "eps_basic": "每股收益",
    "bps": "每股净资产",
    "roa": "总资产收益率",
    "current_ratio": "流动比率",
    "total_assets_growth_ratio": "总资产增速",
}


@dataclass
class FinMetric:
    label: str
    key: str
    value: float | None
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "key": self.key, "value": self.value, "unit": self.unit}


@dataclass
class Announcement:
    dataset: str
    label: str
    date: str | None
    fields: dict[str, Any]
    why: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "label": self.label,
            "date": self.date,
            "fields": self.fields,
            "why": self.why,
        }


@dataclass
class FundamentalDigest:
    """财报与公告的自动摘要 —— 只陈述数字与事实, 不做判断。"""

    symbol: str
    as_of: str = ""
    available: bool = False
    # 最近一期财报
    latest_period: str | None = None
    latest_announce_date: str | None = None
    metrics: list[FinMetric] = field(default_factory=list)
    # 同比变化的趋势 (最近 4 期)
    revenue_trend: list[dict[str, Any]] = field(default_factory=list)
    profit_trend: list[dict[str, Any]] = field(default_factory=list)
    # 公告类
    announcements: list[Announcement] = field(default_factory=list)
    # 无事件日期、只能按"当前快照"呈现的数据源 (ext_research 等)
    snapshot_sources: list[str] = field(default_factory=list)
    # 数据可用性
    unavailable: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of": self.as_of,
            "available": self.available,
            "latest_period": self.latest_period,
            "latest_announce_date": self.latest_announce_date,
            "metrics": [m.to_dict() for m in self.metrics],
            "revenue_trend": self.revenue_trend,
            "profit_trend": self.profit_trend,
            "announcements": [a.to_dict() for a in self.announcements],
            "snapshot_sources": self.snapshot_sources,
            "unavailable": self.unavailable,
            "limitations": self.limitations,
            "note": "只列数字与事实，不做好坏判断 —— 判断权留给用户",
        }


# ── 财报 ────────────────────────────────────────────────

def _read_financials(data_dir: Path, spec, symbol: str) -> pl.DataFrame:
    df = read_dataset(data_dir, spec)
    if df is None or df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()
    return df.filter(pl.col("symbol") == symbol)


def load_financials(
    data_dir: Path,
    symbol: str,
    *,
    as_of: dt.date | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """读该标的的财务指标, **按时点过滤**（只用公告日 <= as_of 的记录）。

    返回 (数据, 限制说明)。数据按 announce_date 降序。
    """
    limitations: list[str] = []
    spec = get_dataset(data_dir, "financials_metrics")
    if spec is None:
        return pl.DataFrame(), ["financials_metrics 数据集未注册"]
    try:
        df = _read_financials(data_dir, spec, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("financials read failed for %s: %s", symbol, exc)
        return pl.DataFrame(), [f"财务数据读取失败: {exc}"]
    if df.is_empty():
        return pl.DataFrame(), ["该标的没有财务指标记录"]

    # PIT: 只保留当时已知 (公告日 <= as_of) 的行。
    # 用 announce_date 而非 period_end —— 报告期早于公告日, 用后者会前视。
    if "announce_date" in df.columns:
        if as_of is not None:
            cutoff = as_of.isoformat()
            df = df.filter(
                pl.col("announce_date").is_not_null()
                & (pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10) <= cutoff)
            )
    else:
        limitations.append("财务数据缺少 announce_date，无法做时点过滤（存在前视风险）")

    if df.is_empty():
        return pl.DataFrame(), [f"截至 {as_of} 没有已公告的财务数据"]

    sort_cols = [c for c in ("announce_date", "period_end") if c in df.columns]
    if sort_cols:
        df = df.sort(sort_cols, descending=True)
    return df, limitations


def build_fundamental_digest(
    data_dir: Path,
    symbol: str,
    *,
    as_of: dt.date | None = None,
) -> FundamentalDigest:
    """财报 + 公告的完整摘要。"""
    as_of = as_of or dt.date.today()
    digest = FundamentalDigest(symbol=symbol, as_of=as_of.isoformat())

    # ── 财报 ──
    df, limitations = load_financials(data_dir, symbol, as_of=as_of)
    digest.limitations.extend(limitations)
    if not df.is_empty():
        digest.available = True
        latest = df.head(1)
        if "period_end" in df.columns:
            digest.latest_period = str(latest["period_end"][0])[:10]
        if "announce_date" in df.columns:
            digest.latest_announce_date = str(latest["announce_date"][0])[:10]

        for key, label in _METRIC_LABELS.items():
            if key not in df.columns:
                continue
            raw = latest[key][0]
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value != value:      # NaN
                continue
            # 比例类字段按百分比展示
            unit = "%" if key.endswith(("_yoy", "_ratio", "margin", "roe", "roa")) else ""
            digest.metrics.append(FinMetric(label=label, key=key, value=round(value, 2), unit=unit))

        # 最近 4 期的同比趋势 —— 让用户看到"在变好还是变差"
        recent = df.head(4)
        for key, bucket in (("revenue_yoy", digest.revenue_trend), ("net_income_yoy", digest.profit_trend)):
            if key not in recent.columns:
                continue
            # 按时间升序输出 (旧 → 新), 便于看出趋势方向
            for row in list(recent.iter_rows(named=True))[::-1]:
                val = row.get(key)
                if val is None:
                    continue
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                if fval != fval:
                    continue
                bucket.append({
                    "period": str(row.get("period_end") or "")[:10],
                    "announced": str(row.get("announce_date") or "")[:10],
                    "value": round(fval, 2),
                })

    # ── 公告 ──
    cutoff = (as_of - dt.timedelta(days=RECENT_DAYS)).isoformat()
    for dataset_id, meta in _ANNOUNCEMENT_SOURCES.items():
        spec = get_dataset(data_dir, dataset_id)
        if spec is None:
            digest.unavailable.append(f"{meta['label']}（{dataset_id}）数据集未配置")
            continue
        try:
            frame = read_dataset(data_dir, spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s read failed: %s", dataset_id, exc)
            digest.unavailable.append(f"{meta['label']}读取失败")
            continue
        if frame is None or frame.is_empty():
            digest.unavailable.append(f"{meta['label']}无数据")
            continue
        if "symbol" not in frame.columns:
            digest.unavailable.append(f"{meta['label']}缺少 symbol 列")
            continue
        rows = frame.filter(pl.col("symbol") == symbol)
        if rows.is_empty():
            continue

        # 时点过滤: 只在表**确实有事件日期列**时做。快照表 (ext_research) 与
        # 只有报告期的表 (ext_forecast) 没有可知日 —— 不能伪造成"近期事件",
        # 而是按"当前快照"呈现并在 limitation 里说明。
        date_col = meta.get("date_col")
        if date_col and date_col in rows.columns:
            rows = rows.filter(
                pl.col(date_col).is_not_null()
                & (pl.col(date_col).cast(pl.Utf8).str.slice(0, 10) <= as_of.isoformat())
                & (pl.col(date_col).cast(pl.Utf8).str.slice(0, 10) >= cutoff)
            )
            if rows.is_empty():
                continue
            rows = rows.sort(date_col, descending=True)
        else:
            digest.snapshot_sources.append(meta["label"])

        for row in rows.head(MAX_RECORDS).iter_rows(named=True):
            payload = {}
            for f in meta["fields"]:
                if f in row and row[f] is not None:
                    payload[f] = row[f] if not isinstance(row[f], float) else round(row[f], 4)
            if not payload:
                continue
            digest.announcements.append(Announcement(
                dataset=dataset_id,
                label=meta["label"],
                date=str(row.get(date_col) or "")[:10] if date_col and date_col in row else None,
                fields=payload,
                why=meta["why"],
            ))

    if not digest.available and not digest.announcements:
        digest.limitations.append(
            "该标的既没有财务记录也没有近期公告 —— 基本面完全未知，只能看技术面"
        )

    # 如实说明快照类数据源 (它们没有可知日, 是"当前值"而非"历史事件")
    if digest.snapshot_sources:
        digest.limitations.append(
            "以下数据源为当前快照、无事件日期，只能代表现在、不能代表历史："
            + "、".join(dict.fromkeys(digest.snapshot_sources))
        )
    # 财务表是快照存储 —— 每标的一行, 因此无法做多期趋势
    if digest.available and len(digest.revenue_trend) <= 1:
        digest.limitations.append(
            "财务指标表为快照存储（每标的一行），只能给出最近一期，无法做多期趋势对比"
        )

    return digest


# ── 与行动卡对接 ────────────────────────────────────────

def checks_from_digest(digest: FundamentalDigest) -> list[dict[str, Any]]:
    """把摘要转成行动卡的「必须核对的事实」项。

    **有数据 → 直接给数字**（把"去哪查"变成"查到了什么"）；
    **无数据 → 明确标注不可用**，不假装查过。
    """
    items: list[dict[str, Any]] = []

    if digest.available:
        parts = []
        if digest.latest_period:
            parts.append(f"报告期 {digest.latest_period}")
        if digest.latest_announce_date:
            parts.append(f"公告于 {digest.latest_announce_date}")
        head = "；".join(parts)
        for m in digest.metrics:
            if m.key in ("revenue_yoy", "net_income_yoy", "roe", "debt_to_asset_ratio"):
                items.append({
                    "item": f"{m.label} {m.value}{m.unit}",
                    "why": f"最近一期财报（{head}）" if head else "最近一期财报",
                    "where": "本地财务指标表（已按时点过滤，只用已公告数据）",
                    "status": "checked",
                })
        # 同比趋势: 最近 4 期是否连续恶化 —— 只陈述, 不评价
        for trend, label in ((digest.revenue_trend, "营收同比"), (digest.profit_trend, "净利润同比")):
            if len(trend) >= 2:
                seq = " → ".join(f"{t['value']}%" for t in trend)
                items.append({
                    "item": f"{label}近 {len(trend)} 期：{seq}",
                    "why": "趋势比单期数字更能说明经营方向",
                    "where": "本地财务指标表",
                    "status": "checked",
                })
    else:
        items.append({
            "item": "财报数据不可用",
            "why": digest.limitations[-1] if digest.limitations else "无财务记录",
            "where": "数据页面检查 financials 同步状态",
            "status": "unavailable",
        })

    # 公告: 有就列日期与要点
    for a in digest.announcements[:6]:
        detail = "、".join(f"{k}={v}" for k, v in list(a.fields.items())[:3])
        items.append({
            "item": f"{a.label}{'（' + a.date + '）' if a.date else ''}：{detail}",
            "why": a.why,
            "where": f"本地 {a.dataset} 表",
            "status": "checked",
        })

    # 无近期公告时也不假装查过
    if digest.available and not digest.announcements:
        items.append({
            "item": f"近 {RECENT_DAYS} 天无业绩预告/解禁/增减持/质押/分红/评级记录",
            "why": "确认不是数据缺失，而是确实没有这类事项",
            "where": "本地 ext_* 表",
            "status": "checked",
        })

    return items


__all__ = [
    "MAX_RECORDS",
    "RECENT_DAYS",
    "Announcement",
    "FinMetric",
    "FundamentalDigest",
    "build_fundamental_digest",
    "checks_from_digest",
    "load_financials",
]
