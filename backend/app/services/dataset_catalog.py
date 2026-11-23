"""数据集目录 — 来源 / 币种 / 逐列单位 / 时区 / 新鲜度 / PIT 口径的单一权威。

## 为什么需要它

单位与来源此前散落在 CONTRIBUTING、docs/*.md、provider 实现和代码注释里，页面和
API 只能靠约定，于是出现三类具体问题：

1. **同名字段不同单位**：`ext_valuation.amount` 是**万元**（上游口径），而
   `kline_daily.amount` 是**元**（内部契约）。两者都能渲染，数字都"看起来对"。
2. **百分比口径在同一张表里摇摆**：`regime_history.median_pct` 是**小数**
   （-0.1001 = -10.01%），而 `regime_history.strong_up_pct` 是**百分数**
   （75.43 = 75.43%）。按后缀猜必错。
3. **来源在落盘时被丢弃**：`data_providers/schemas.py` 的 `DAILY_COLUMNS` 定义了
   `source` 列，但 `kline_daily` 的 parquet 里没有它 —— 逐行溯源已经不可能。

本模块把口径集中声明一次，供 API 与前端引用。

## 诚实边界

* 只声明**有证据**的口径。行情域依据 CONTRIBUTING §3.1/§3.2 与
  `indicators/pipeline.py` 的实现；`ext_*` 依据 `docs/` 接入说明 **加上实值域采样**
  （列的 min/max 与已知个案吻合才写死，采样脚本见提交说明）。
* 证据不足一律 `UNIT_UNKNOWN`，**不按列名猜**：`goodwill_ratio` / `pledge_ratio`
  是百分数而 `avg_premium` 是小数比率 —— 同为 `*_ratio` 后缀却不同口径。
* `source` 是**当前路由/配置解析出的来源**，不是逐行溯源，因此每条都带
  `source_basis` 说明结论从哪来（配置文件 / 文档 / 路由配置 / 代码）。
* PIT 口径用 `pit_mode` 区分四种情况，**只有真的拿得到公告日的才标 `announcement`**；
  只有报表期的标 `period_only`（当前不可安全回测），不要用报表期冒充可知日。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import CN_TZ
from app.services.data_integrity import partition_settlement
from app.services.ext_data import ExtConfig, ExtConfigStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 单位词表 —— 固定枚举，避免各模块自造同义词（"元" vs "人民币元"）
# ---------------------------------------------------------------------------
UNIT_UNKNOWN = "unknown"
UNIT_CNY = "元"
UNIT_WAN = "万元"
UNIT_YI = "亿元"
UNIT_PER_SHARE = "元/股"
UNIT_PER_10_SHARE = "元/10股"
UNIT_PERCENT = "%"
UNIT_DECIMAL = "小数"          # 0.05 = 5%，与百分数相对
UNIT_SHARE = "股"
UNIT_WAN_SHARE = "万股"
UNIT_LOT = "手"                # 1 手 = 100 股
UNIT_HOUSEHOLD = "户"
UNIT_TIMES = "倍"
UNIT_COUNT = "计数"
UNIT_TS_MS = "毫秒时间戳"
UNIT_TS_S = "秒级时间戳"
UNIT_DATE = "日期"
UNIT_BOOL = "布尔"
UNIT_TEXT = "文本"
UNIT_POINT = "点"              # 指数点位
UNIT_SHARE_PER_SHARE = "股/股"  # 每股送转
UNIT_PER_HOUSEHOLD = "元/户"
UNIT_PCT_POINT = "百分点"

CURRENCY_CNY = "CNY"
CURRENCY_NONE = "none"

# PIT（point-in-time）口径
PIT_EVENT_DATE = "event_date"        # 行情类：日期列本身就是可知日
PIT_ANNOUNCEMENT = "announcement"    # 有公告日列，可按「当时已知」对齐
PIT_PERIOD_ONLY = "period_only"      # 只有报告期，无公告日 → 不可安全回测
PIT_UNKNOWN = "unknown"

#: 能按「当时已知」取数的 PIT 模式。
#: 这里是唯一定义处：`data_pit.USABLE_MODES` 直接引用它，catalog 的 `pit.usable`
#: 也用它计算 —— 两个端点对"能不能按当时已知取数"必须给同一个答案。
PIT_USABLE_MODES: tuple[str, ...] = (PIT_EVENT_DATE, PIT_ANNOUNCEMENT)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一列的语义 —— 名字之外还要说清单位。"""

    name: str
    unit: str = UNIT_UNKNOWN
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "unit": self.unit}
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """一个数据集的完整目录条目。"""

    id: str
    label: str
    domain: str                 # kline / ext / financial / dimension / derived / event
    layer: str                  # raw / enriched / snapshot / timeseries / dimension
    asset_type: str | None      # stock / etf / index / None
    relpath: str                # 相对 data_dir
    layout: str                 # date_partition / single_file / empty
    date_column: str | None
    date_format: str | None     # "YYYY-MM-DD" / "YYYYMMDD" / None
    currency: str
    timezone: str
    source: str
    source_basis: str           # config_pull / docs / routing / code / none
    pit_mode: str
    cadence: str = "daily"      # daily / weekly / quarterly / event / static
    period_column: str | None = None
    known_at_column: str | None = None
    fields: tuple[FieldSpec, ...] = ()
    notes: tuple[str, ...] = ()
    # 唯一键列 —— 供重复行检查使用。空元组 = 未声明, 检查器会如实跳过而不是猜
    # （derive "symbol + 任意日期列" 会把 ext_gn_ths 这类"一 symbol 多个概念"的
    # 维表整表误报成重复）。
    key_columns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "domain": self.domain,
            "layer": self.layer,
            "asset_type": self.asset_type,
            "relpath": self.relpath,
            "layout": self.layout,
            "cadence": self.cadence,
            "date_column": self.date_column,
            "date_format": self.date_format,
            "currency": self.currency,
            "timezone": self.timezone,
            "source": self.source,
            "source_basis": self.source_basis,
            "pit": {
                "mode": self.pit_mode,
                # 与 data_pit 同一个口径：event_date / announcement 且确实有可知日列
                # 才算可用。其余模式显式说明原因，避免调用方把 None 当成"没有 PIT 需求"。
                "known_at_column": self.known_at_column,
                "period_column": self.period_column,
                "usable": (
                    self.pit_mode in PIT_USABLE_MODES and bool(self.known_at_column)
                ),
            },
            "fields": [f.to_dict() for f in self.fields],
            "notes": list(self.notes),
            "key_columns": list(self.key_columns),
        }


@dataclass(frozen=True, slots=True)
class Freshness:
    """一个数据集的新鲜度 —— 最新可用日期 + 落盘更新时间 + 定版状态。"""

    dataset_id: str
    status: str                 # ok / stale / empty / absent
    latest_date: str | None     # 数据内最新日期（YYYY-MM-DD）
    updated_at: str | None      # 落盘 mtime（北京时间，YYYY-MM-DD HH:MM:SS）
    rows: int | None            # 最新分区的行数
    settlement: str             # final / intraday / unknown / n/a
    anchor_date: str | None     # 全市场行情最新交易日（判滞后的基准）
    lag_sessions: int | None    # 落后锚点多少个交易日
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "status": self.status,
            "latest_date": self.latest_date,
            "updated_at": self.updated_at,
            "rows": self.rows,
            "settlement": self.settlement,
            "anchor_date": self.anchor_date,
            "lag_sessions": self.lag_sessions,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 行情域 —— 单位依据 CONTRIBUTING §3.1/§3.2 与 indicators/pipeline.py
# ---------------------------------------------------------------------------
def _ohlc(adjusted: bool) -> tuple[FieldSpec, ...]:
    note = "前复权" if adjusted else "不复权原始价"
    return tuple(FieldSpec(c, UNIT_PER_SHARE, note) for c in ("open", "high", "low", "close"))


_KLINE_RAW_FIELDS = (
    FieldSpec("symbol", UNIT_TEXT),
    FieldSpec("date", UNIT_DATE),
    *_ohlc(adjusted=False),
    FieldSpec("volume", UNIT_LOT, "1 手 = 100 股"),
    FieldSpec("amount", UNIT_CNY),
    FieldSpec("quote_ts", UNIT_TS_MS, "实时 flush 写真实值；batch 权威历史为 null"),
)

_KLINE_ENRICHED_FIELDS = (
    FieldSpec("symbol", UNIT_TEXT),
    FieldSpec("date", UNIT_DATE),
    *_ohlc(adjusted=True),
    FieldSpec("volume", UNIT_LOT, "1 手 = 100 股"),
    FieldSpec("amount", UNIT_CNY),
    FieldSpec("raw_close", UNIT_PER_SHARE, "不复权原始价"),
    FieldSpec("raw_high", UNIT_PER_SHARE, "不复权原始价"),
    FieldSpec("raw_low", UNIT_PER_SHARE, "不复权原始价"),
    FieldSpec("turnover_rate", UNIT_PERCENT, "百分数值：5 = 5%（不是 0.05）"),
    FieldSpec("consecutive_limit_ups", UNIT_COUNT, "连续涨停天数"),
    FieldSpec("consecutive_limit_downs", UNIT_COUNT, "连续跌停天数"),
    FieldSpec("quote_ts", UNIT_TS_MS, "权威历史为 null"),
)

_INDEX_FIELDS = (
    FieldSpec("symbol", UNIT_TEXT),
    FieldSpec("date", UNIT_DATE),
    *_ohlc(adjusted=True),
    FieldSpec("volume", UNIT_LOT),
    FieldSpec("amount", UNIT_CNY),
)


def _kline_specs() -> list[DatasetSpec]:
    """行情日K六张表。

    目录名与数据集 id 保持一致（`kline_daily` / `kline_etf_daily` /
    `kline_index_daily` 及各 `_enriched`）—— 与 tickflow/repository.py 的
    `daily_dirname()` / `enriched_dirname()` 映射同一套名字，便于按 id 直接定位文件。
    """
    out: list[DatasetSpec] = []
    # 名字与 tickflow/repository.py 的 daily_dirname()/enriched_dirname() 一一对应。
    # 注意 stock 的 enriched 是 kline_daily_enriched（追加），而 etf/index 是
    # kline_etf_enriched / kline_index_enriched（把 _daily 换成 _enriched）——
    # 两者规则不同，不能靠字符串拼接推导，只能显式列出。
    tables = {
        "stock": ("kline_daily", "kline_daily_enriched"),
        "etf": ("kline_etf_daily", "kline_etf_enriched"),
        "index": ("kline_index_daily", "kline_index_enriched"),
    }
    for asset, label in (("stock", "股票"), ("etf", "ETF"), ("index", "指数")):
        raw_dir, enriched_dir = tables[asset]
        for layer, is_enriched in (("raw", False), ("enriched", True)):
            dirname = enriched_dir if is_enriched else raw_dir
            if asset == "index":
                fields = (
                    (*_INDEX_FIELDS, FieldSpec("quote_ts", UNIT_TS_MS))
                    if is_enriched
                    else _INDEX_FIELDS
                )
            else:
                fields = _KLINE_ENRICHED_FIELDS if is_enriched else _KLINE_RAW_FIELDS
            out.append(
                DatasetSpec(
                    id=dirname,
                    label=f"{label}日K（{'前复权+指标' if is_enriched else '原始价'}）",
                    domain="kline",
                    layer=layer,
                    asset_type=asset,
                    relpath=dirname,
                    layout="date_partition",
                    cadence="daily",
                    date_column="date",
                    date_format="YYYY-MM-DD",
                    currency=CURRENCY_CNY,
                    timezone="Asia/Shanghai",
                    source=_routing_source(),
                    source_basis="routing",
                    pit_mode=PIT_EVENT_DATE,
                    fields=fields,
                    notes=(
                        "指标列（ma/macd/rsi 等）由 indicators/pipeline.py 运行时计算，不落盘",
                        "落盘不含 source 列 —— 无法逐行溯源",
                    ),
                )
            )
    return out


def _routing_source() -> str:
    """行情来源：由能力矩阵路由决定，落盘时丢弃了逐行来源。"""
    return "数据源插件（按能力矩阵路由，见 data/user_data/preferences.json）"


def _static_specs() -> list[DatasetSpec]:
    specs: list[DatasetSpec] = []
    specs.extend(_kline_specs())

    specs.append(
        DatasetSpec(
            id="kline_minute",
            label="股票分钟K（1 分钟）",
            domain="kline",
            layer="raw",
            asset_type="stock",
            relpath="kline_minute",
            layout="date_partition",
            date_column="date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="分钟数据源（preferences.minute_data_provider）",
            source_basis="routing",
            pit_mode=PIT_EVENT_DATE,
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("datetime", UNIT_TEXT, "北京时间墙钟 naive，如 09:35:00"),
                *_ohlc(adjusted=True),
                FieldSpec("volume", UNIT_LOT),
                FieldSpec("amount", UNIT_CNY),
            ),
            notes=("datetime 是墙钟 naive，禁止当 UTC 解释",),
        )
    )
    specs.append(
        DatasetSpec(
            id="kline_etf_minute",
            label="ETF 分钟K（1 分钟）",
            domain="kline",
            layer="raw",
            asset_type="etf",
            relpath="kline_etf_minute",
            layout="date_partition",
            date_column="date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="分钟数据源（preferences.minute_data_provider）",
            source_basis="routing",
            pit_mode=PIT_EVENT_DATE,
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("datetime", UNIT_TEXT, "北京时间墙钟 naive"),
                *_ohlc(adjusted=True),
                FieldSpec("volume", UNIT_LOT),
                FieldSpec("amount", UNIT_CNY),
            ),
            notes=("当前为空目录",),
        )
    )

    specs.append(
        DatasetSpec(
            id="adj_factor",
            label="除权除息事件与复权因子",
            domain="dimension",
            layer="dimension",
            asset_type="stock",
            relpath="adj_factor/all.parquet",
            layout="single_file",
            date_column="trade_date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="复权数据源（preferences.adj_factor_provider）",
            source_basis="routing",
            pit_mode=PIT_EVENT_DATE,
            # trade_date 是最后一次除权除息事件日，不是同步时间 —— 无事件的日子
            # 文件不会重写，所以按事件频率判新鲜度，否则会天天误报 stale。
            cadence="event",
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("trade_date", UNIT_DATE, "除权除息日"),
                FieldSpec("ex_factor", UNIT_TIMES, "单事件比值，非累计因子"),
                FieldSpec("dividend", UNIT_PER_SHARE, "每股派现（上游每 10 股口径已 ÷10）"),
                FieldSpec("bonus", UNIT_SHARE_PER_SHARE, "每股送转股"),
                FieldSpec("allot", UNIT_SHARE_PER_SHARE, "每股配股"),
                FieldSpec("allot_price", UNIT_PER_SHARE, "配股价"),
                FieldSpec("prev_close", UNIT_PER_SHARE, "除权前收盘价"),
            ),
        )
    )
    specs.append(
        DatasetSpec(
            id="adj_factor_etf",
            label="ETF 除权除息事件",
            domain="dimension",
            layer="dimension",
            asset_type="etf",
            relpath="adj_factor_etf",
            layout="empty",
            date_column="trade_date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="复权数据源（preferences.adj_factor_provider）",
            source_basis="routing",
            pit_mode=PIT_EVENT_DATE,
            cadence="event",
            fields=(),
            notes=("当前为空目录",),
        )
    )

    specs.append(
        DatasetSpec(
            id="instruments",
            label="股票维表（股本/最小变动/涨跌停）",
            domain="dimension",
            layer="dimension",
            asset_type="stock",
            relpath="instruments/instruments.parquet",
            layout="single_file",
            date_column="as_of",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="维表（由行情管道刷新）",
            source_basis="code",
            pit_mode=PIT_EVENT_DATE,
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("name", UNIT_TEXT),
                FieldSpec("code", UNIT_TEXT),
                FieldSpec("exchange", UNIT_TEXT),
                FieldSpec("region", UNIT_TEXT),
                FieldSpec("type", UNIT_TEXT),
                FieldSpec("listing_date", UNIT_DATE),
                FieldSpec("total_shares", UNIT_SHARE),
                FieldSpec("float_shares", UNIT_SHARE),
                FieldSpec("tick_size", UNIT_PER_SHARE),
                FieldSpec("limit_up", UNIT_PER_SHARE, "实测全列为空，未回填"),
                FieldSpec("limit_down", UNIT_PER_SHARE, "实测全列为空，未回填"),
                FieldSpec("as_of", UNIT_DATE, "维表定版日；仅当等于行情日时涨跌停哨兵才生效"),
            ),
            notes=("as_of 是点值而非时序：历史交易日回看会用到今日股本",),
        )
    )
    for sub, atype, label in (
        ("instruments_etf", "etf", "ETF 维表"),
        ("instruments_index", "index", "指数维表"),
    ):
        specs.append(
            DatasetSpec(
                id=sub,
                label=label,
                domain="dimension",
                layer="dimension",
                asset_type=atype,
                relpath=f"{sub}/{sub}.parquet",
                layout="single_file",
                date_column=None,
                date_format=None,
                currency=CURRENCY_CNY,
                timezone="Asia/Shanghai",
                source="维表（由行情管道刷新）",
                source_basis="code",
                pit_mode=PIT_UNKNOWN,
                cadence="static",
                fields=(
                    FieldSpec("symbol", UNIT_TEXT),
                    FieldSpec("name", UNIT_TEXT),
                    FieldSpec("code", UNIT_TEXT),
                    FieldSpec("asset_type", UNIT_TEXT),
                ),
            )
        )

    specs.append(
        DatasetSpec(
            id="depth5",
            label="收盘五档封单快照",
            domain="kline",
            layer="snapshot",
            asset_type="stock",
            relpath="depth5",
            layout="date_partition",
            date_column="date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="深度数据源（preferences.depth5_data_provider）",
            source_basis="routing",
            pit_mode=PIT_EVENT_DATE,
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("sealed_up", UNIT_BOOL, "是否封涨停"),
                FieldSpec("sealed_down", UNIT_BOOL, "是否封跌停"),
                FieldSpec("ask1_vol", UNIT_LOT, "卖一量"),
                FieldSpec("bid1_vol", UNIT_LOT, "买一量"),
                FieldSpec("status", UNIT_TEXT),
                FieldSpec("fetched_at", UNIT_TS_S, "秒级 Unix 时间戳"),
            ),
        )
    )

    specs.append(
        DatasetSpec(
            id="financials_metrics",
            label="财务指标（报告期）",
            domain="financial",
            layer="timeseries",
            asset_type="stock",
            relpath="financials/metrics",
            layout="single_file",
            date_column="period_end",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_CNY,
            timezone="Asia/Shanghai",
            source="财务数据源（preferences.financial_data_provider）",
            source_basis="routing",
            pit_mode=PIT_ANNOUNCEMENT,
            cadence="quarterly",
            period_column="period_end",
            known_at_column="announce_date",
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("period_end", UNIT_DATE, "报表期"),
                FieldSpec("announce_date", UNIT_DATE, "公告日（可知日）"),
                FieldSpec("eps_basic", UNIT_PER_SHARE),
                FieldSpec("bps", UNIT_PER_SHARE),
                FieldSpec("revenue_yoy", UNIT_PERCENT),
                FieldSpec("net_income_yoy", UNIT_PERCENT),
                FieldSpec("gross_margin", UNIT_PERCENT),
                FieldSpec("net_margin", UNIT_PERCENT),
                FieldSpec("roe", UNIT_PERCENT),
                FieldSpec("roa", UNIT_PERCENT),
                FieldSpec("debt_to_asset_ratio", UNIT_PERCENT),
            ),
            notes=(
                "写入端每 (symbol, period_end) 只保留一行 → 重述会覆盖旧值，无法重建 as-known-at-T 版本",
            ),
        )
    )
    specs.append(
        DatasetSpec(
            id="financials_shares",
            label="历史股本（流通股）",
            domain="financial",
            layer="timeseries",
            asset_type="stock",
            relpath="financials/shares",
            layout="empty",
            date_column="period_end",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_NONE,
            timezone="Asia/Shanghai",
            source="财务数据源（preferences.financial_data_provider）",
            source_basis="routing",
            pit_mode=PIT_ANNOUNCEMENT,
            cadence="quarterly",
            period_column="period_end",
            known_at_column="announce_date",
            fields=(
                FieldSpec("symbol", UNIT_TEXT),
                FieldSpec("period_end", UNIT_DATE),
                FieldSpec("announce_date", UNIT_DATE, "公告日（可知日）"),
                FieldSpec("float_shares", UNIT_SHARE),
            ),
            notes=(
                "实测目录为空 → 换手率当前总是走 instruments 兜底分支（CONTRIBUTING §3.4 规则未被真实数据验证）",
            ),
        )
    )

    specs.append(
        DatasetSpec(
            id="regime_history",
            label="市场状态（每日一行）",
            domain="derived",
            layer="timeseries",
            asset_type=None,
            relpath="regime_history/part.parquet",
            layout="single_file",
            date_column="date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_NONE,
            timezone="Asia/Shanghai",
            source="本系统自算（regime_builder）",
            source_basis="code",
            pit_mode=PIT_EVENT_DATE,
            fields=(
                FieldSpec("date", UNIT_DATE),
                FieldSpec("state", UNIT_TEXT),
                FieldSpec("score", UNIT_COUNT, "0-100"),
                FieldSpec("limit_up", UNIT_COUNT),
                FieldSpec("limit_down", UNIT_COUNT),
                FieldSpec("seal_rate", UNIT_DECIMAL, "0.6364 = 63.64%"),
                FieldSpec("up_ratio", UNIT_DECIMAL, "上涨/下跌家数比"),
                FieldSpec("above_ma20_pct", UNIT_DECIMAL, "0.4195 = 41.95%"),
                FieldSpec("median_pct", UNIT_DECIMAL, "-0.1001 = -10.01%"),
                FieldSpec("avg_pct", UNIT_DECIMAL),
                FieldSpec("index_pct", UNIT_DECIMAL),
                FieldSpec("strong_up_pct", UNIT_PERCENT, "与同表 median_pct 口径不同"),
                FieldSpec("strong_down_pct", UNIT_PERCENT),
                FieldSpec("total_amount", UNIT_CNY),
                FieldSpec("promo_rate", UNIT_DECIMAL),
                FieldSpec("ladder_completeness", UNIT_DECIMAL),
            ),
            notes=(
                "同表百分比口径混用：*_pct 里 median_pct/avg_pct/index_pct/above_ma20_pct 是小数，"
                "strong_up_pct/strong_down_pct 是百分数",
            ),
        )
    )
    specs.append(
        DatasetSpec(
            id="mainline_history",
            label="主线检测历史",
            domain="derived",
            layer="timeseries",
            asset_type=None,
            relpath="mainline_history/part.parquet",
            layout="single_file",
            date_column="date",
            date_format="YYYY-MM-DD",
            currency=CURRENCY_NONE,
            timezone="Asia/Shanghai",
            source="本系统自算（mainline 检测）",
            source_basis="code",
            pit_mode=PIT_EVENT_DATE,
            fields=(
                FieldSpec("date", UNIT_DATE),
                FieldSpec("kind", UNIT_TEXT),
                FieldSpec("member", UNIT_TEXT),
                FieldSpec("limit_up_count", UNIT_COUNT),
                FieldSpec("ge2_count", UNIT_COUNT),
                FieldSpec("max_boards", UNIT_COUNT, "最高连板数"),
                FieldSpec("leader_symbol", UNIT_TEXT),
                FieldSpec("score", UNIT_COUNT, "0-100"),
                FieldSpec("rank", UNIT_COUNT),
            ),
        )
    )
    return specs


# ---------------------------------------------------------------------------
# ext_* 表 —— 单位依据 docs/ 接入说明 + 实值域采样
# ---------------------------------------------------------------------------
# 来源证据：config_pull = config.json 里存了拉取配方（可运行时读到 URL）；
# docs = 只在接入说明里有记录（数据本身不含来源列）。
_EXT_SOURCE: dict[str, tuple[str, str]] = {
    "ext_valuation": ("东财 push2 估值接口（databridge fetch_valuation）", "docs"),
    "ext_fundflow": ("东财 push2 qt/clist 直连（databridge fetch_batch2.py）", "docs"),
    "ext_goodwill": ("东财数据中心 RPT_GOODWILL_STOCKDETAILS（databridge fetch_batch2.py）", "docs"),
    "ext_pledge": ("东财数据中心 RPT_CSDC_LIST（databridge fetch_batch2.py）", "docs"),
    "ext_insider": ("akshare stock_hold_management_detail_em（databridge）", "docs"),
    "ext_margin": ("akshare stock_margin_detail_szse/sse（databridge）", "docs"),
    "ext_ztlimit": ("akshare stock_zt_pool_em + stock_zt_pool_strong_em（databridge）", "docs"),
    "ext_bsdetail": ("akshare 资产负债表明细（databridge）", "docs"),
    "ext_shareholders": ("akshare 股东户数（databridge）", "docs"),
    "ext_fundhold": ("akshare 基金持股（databridge）", "docs"),
    "ext_dividend": ("akshare 分红送配（databridge）", "docs"),
    "ext_forecast": ("akshare 业绩预告（databridge）", "docs"),
    "ext_research": ("akshare 机构评级（databridge）", "docs"),
    "ext_unlock": ("akshare 限售解禁（databridge）", "docs"),
    "ext_blocktrade": ("akshare 大宗交易（databridge）", "docs"),
    "ext_gdhs_ts": ("由 ext_shareholders 前向填充派生（databridge）", "code"),
}

# 逐列单位。键是列名，值是 (单位, 备注)。缺键 = 未声明 → unknown。
_EXT_UNITS: dict[str, dict[str, tuple[str, str]]] = {
    "ext_valuation": {
        "turnover": (UNIT_PERCENT, ""),
        "amount": (UNIT_WAN, "上游口径为万元；与 kline 的 amount（元）不同"),
        "total_mv": (UNIT_YI, ""),
        "circ_mv": (UNIT_YI, ""),
        "pe_ttm": (UNIT_TIMES, ""),
        "pb": (UNIT_TIMES, ""),
        "price": (UNIT_PER_SHARE, ""),
    },
    "ext_fundflow": {
        "close": (UNIT_PER_SHARE, ""),
        "chg_pct": (UNIT_PERCENT, ""),
        "main_net": (UNIT_CNY, ""),
        "main_pct": (UNIT_PERCENT, ""),
        "xl_net": (UNIT_CNY, ""),
        "xl_pct": (UNIT_PERCENT, ""),
        "lg_net": (UNIT_CNY, ""),
        "lg_pct": (UNIT_PERCENT, ""),
        "md_net": (UNIT_CNY, ""),
        "md_pct": (UNIT_PERCENT, ""),
        "sm_net": (UNIT_CNY, ""),
        "sm_pct": (UNIT_PERCENT, ""),
        "main_net_ratio": (UNIT_PERCENT, "实测与 main_pct 值域完全相同，疑似重复列"),
        "flow_date": (UNIT_DATE, "实际格式 YYYY-MM-DD"),
    },
    "ext_goodwill": {
        "goodwill": (UNIT_CNY, ""),
        "net_equity": (UNIT_CNY, ""),
        "goodwill_ratio": (UNIT_PERCENT, "1152.2 = 1152.2%（不是小数）"),
        "goodwill_prev": (UNIT_CNY, ""),
        "goodwill_chg": (UNIT_CNY, ""),
        "goodwill_chg_pct": (UNIT_PERCENT, ""),
        "net_profit": (UNIT_CNY, ""),
        "np_yoy_pct": (UNIT_PERCENT, ""),
        "se_chg_pct": (UNIT_PERCENT, ""),
        "goodwill_ratio_qoq": (UNIT_PCT_POINT, "占比环比变动"),
        "goodwill_ratio_max": (UNIT_PERCENT, "近 4 期最高占比"),
        "periods": (UNIT_COUNT, ""),
        "risk_flag": (UNIT_COUNT, "0/1；占比 ≥ 30% 置 1"),
        "report_date": (UNIT_DATE, ""),
        "notice_date": (UNIT_DATE, ""),
    },
    "ext_pledge": {
        "pledge_ratio": (UNIT_PERCENT, "78.74 = 78.74%"),
        "pledge_market_cap": (UNIT_WAN, ""),
        "pledge_deal_num": (UNIT_COUNT, ""),
        "repurchase_balance": (UNIT_WAN_SHARE, ""),
        "unlimited_balance": (UNIT_WAN_SHARE, ""),
        "limited_balance": (UNIT_WAN_SHARE, ""),
        "y1_close_chg": (UNIT_PERCENT, ""),
        "risk_flag": (UNIT_COUNT, "0/1；质押比例 ≥ 50% 置 1"),
        "trade_date": (UNIT_DATE, "统计日，每周五"),
    },
    "ext_bsdetail": {
        "period_end": (UNIT_TEXT, "实际格式 YYYYMMDD"),
        "announce_date": (UNIT_TEXT, "实际格式 YYYYMMDD"),
        "inventory": (UNIT_CNY, ""),
        "accounts_receivable": (UNIT_CNY, ""),
        "short_loan": (UNIT_CNY, ""),
        "long_loan": (UNIT_CNY, ""),
        "goodwill": (UNIT_CNY, ""),
        "cip": (UNIT_CNY, ""),
        "contract_liab": (UNIT_CNY, ""),
        "fixed_assets": (UNIT_CNY, ""),
        "total_equity": (UNIT_CNY, ""),
        "equity_parent": (UNIT_CNY, ""),
        "total_assets": (UNIT_CNY, ""),
    },
    "ext_dividend": {
        "cash_per10": (UNIT_PER_10_SHARE, ""),
        "yield_pct": (UNIT_PERCENT, "已知偏差：低估约 2.5 倍，勿用于算股息率"),
        "eps": (UNIT_PER_SHARE, ""),
        "bps": (UNIT_PER_SHARE, "已知偏差：数值错误"),
        "plan_date": (UNIT_DATE, "预案公告日"),
        "record_date": (UNIT_DATE, "股权登记日"),
        "ex_date": (UNIT_DATE, "除权除息日"),
    },
    "ext_forecast": {
        "forecast_value": (UNIT_CNY, ""),
        "change_pct": (UNIT_PERCENT, ""),
        "period": (UNIT_TEXT, "报告期，无公告日列"),
    },
    "ext_fundhold": {
        "fund_count": (UNIT_COUNT, "家"),
        "hold_shares": (UNIT_SHARE, ""),
        "hold_mv": (UNIT_CNY, ""),
        "change_shares": (UNIT_SHARE, ""),
        "change_pct": (UNIT_PERCENT, ""),
    },
    "ext_insider": {
        "delta_shares": (UNIT_SHARE, ""),
        "avg_price": (UNIT_PER_SHARE, ""),
        "delta_amount": (UNIT_CNY, ""),
        "delta_pct": (UNIT_PERCENT, ""),
        "hold_after": (UNIT_SHARE, ""),
        "net_shares_90d": (UNIT_SHARE, ""),
        "net_amount_90d": (UNIT_CNY, ""),
        "cnt_90d": (UNIT_COUNT, ""),
        "trade_date": (UNIT_DATE, ""),
        "last_date": (UNIT_DATE, ""),
    },
    "ext_margin": {
        "fin_balance": (UNIT_CNY, ""),
        "fin_buy": (UNIT_CNY, ""),
        "fin_repay": (UNIT_CNY, ""),
        "short_qty": (UNIT_SHARE, ""),
        "short_balance": (UNIT_CNY, ""),
        "total_balance": (UNIT_CNY, ""),
        "fin_net_buy": (UNIT_CNY, ""),
        "net_ratio_pct": (UNIT_PERCENT, ""),
        "margin_date": (UNIT_TEXT, "实际格式 YYYYMMDD"),
    },
    "ext_blocktrade": {
        "trade_cnt": (UNIT_COUNT, ""),
        "total_amount": (UNIT_CNY, ""),
        "avg_premium": (UNIT_DECIMAL, "折溢价率，小数制：-0.189 = -18.9%"),
        "last_date": (UNIT_DATE, ""),
    },
    "ext_research": {
        "buy_cnt": (UNIT_COUNT, ""),
        "add_cnt": (UNIT_COUNT, ""),
        "neutral_cnt": (UNIT_COUNT, ""),
        "reduce_cnt": (UNIT_COUNT, ""),
        "sell_cnt": (UNIT_COUNT, ""),
    },
    "ext_shareholders": {
        "holders": (UNIT_HOUSEHOLD, ""),
        "holders_prev": (UNIT_HOUSEHOLD, ""),
        "holders_chg": (UNIT_HOUSEHOLD, ""),
        "holders_chg_pct": (UNIT_PERCENT, ""),
        "avg_mv": (UNIT_PER_HOUSEHOLD, ""),
        "total_mv": (UNIT_CNY, ""),
        "stat_date": (UNIT_DATE, "统计截止日"),
        "announce_date": (UNIT_DATE, "公告日（可知日）"),
    },
    "ext_unlock": {
        "total_mv": (UNIT_CNY, ""),
        "batch_cnt": (UNIT_COUNT, ""),
        "max_pct_float": (UNIT_PERCENT, "占流通股比"),
    },
    "ext_ztlimit": {
        "close": (UNIT_PER_SHARE, ""),
        "chg_pct": (UNIT_PERCENT, ""),
        "amount": (UNIT_CNY, ""),
        "circ_mv": (UNIT_CNY, ""),
        "turnover": (UNIT_PERCENT, ""),
        "seal_fund": (UNIT_CNY, ""),
        "break_cnt": (UNIT_COUNT, "炸板次数"),
        "boards": (UNIT_COUNT, "连板数"),
        "from_previous": (UNIT_COUNT, "0/1；是否昨日涨停"),
        "zt_date": (UNIT_TEXT, "实际格式 YYYYMMDD"),
    },
    "ext_gdhs_ts": {
        "holders": (UNIT_HOUSEHOLD, ""),
        "gdhs_chg": (UNIT_PERCENT, ""),
        "gdhs_concentration": (UNIT_PERCENT, ""),
        "gdhs_inv_chg": (UNIT_PERCENT, ""),
    },
}

# PIT 口径：只有真的拿得到公告日的才标 announcement。
_EXT_PIT: dict[str, tuple[str, str | None, str | None]] = {
    "ext_bsdetail": (PIT_ANNOUNCEMENT, "period_end", "announce_date"),
    "ext_shareholders": (PIT_ANNOUNCEMENT, "stat_date", "announce_date"),
    "ext_goodwill": (PIT_ANNOUNCEMENT, "report_date", "notice_date"),
    "ext_dividend": (PIT_ANNOUNCEMENT, None, "plan_date"),
    "ext_insider": (PIT_EVENT_DATE, None, "trade_date"),
    "ext_margin": (PIT_EVENT_DATE, None, "margin_date"),
    "ext_ztlimit": (PIT_EVENT_DATE, None, "zt_date"),
    "ext_blocktrade": (PIT_EVENT_DATE, None, "last_date"),
    "ext_fundflow": (PIT_EVENT_DATE, None, "flow_date"),
    "ext_valuation": (PIT_EVENT_DATE, None, "date"),
    "ext_gdhs_ts": (PIT_EVENT_DATE, None, "date"),
    "ext_forecast": (PIT_PERIOD_ONLY, "period", None),
    "ext_pledge": (PIT_EVENT_DATE, None, "trade_date"),
    "ext_unlock": (PIT_UNKNOWN, None, None),
    "ext_fundhold": (PIT_UNKNOWN, None, None),
    "ext_research": (PIT_UNKNOWN, None, None),
}

_EXT_NOTES: dict[str, tuple[str, ...]] = {
    "ext_forecast": ("只有报告期，无公告日 → 不可安全回测",),
    "ext_unlock": ("无任何日期列 → 不可安全回测",),
    "ext_fundhold": ("无任何日期列 → 不可安全回测",),
    "ext_research": ("无任何日期列 → 不可安全回测",),
    "ext_fundflow": ("timeseries 但当前只有 1 个分区：历史会被当日覆盖",),
    "ext_gdhs_ts": ("已前向填充至每个交易日，可用作 PIT 因子",),
}


def _unit_from_dtype(dtype: str | None) -> str:
    """按声明的 dtype 给出兜底单位。

    只从 dtype 推导 —— 不做列名猜测。数值列的单位无法从 dtype 判断
    （元、万元、%）故仍记 unknown；文本/布尔/日期列则能确定。
    """
    token = (dtype or "").lower()
    if "bool" in token:
        return UNIT_BOOL
    if "date" in token or "time" in token:
        return UNIT_DATE
    if "str" in token or "object" in token or "category" in token:
        return UNIT_TEXT
    return UNIT_UNKNOWN


def ext_dataset_spec(cfg: ExtConfig, data_dir: Path) -> DatasetSpec:
    """把一张 ext 表配置转成目录条目。"""
    units = _EXT_UNITS.get(cfg.id, {})
    fields: list[FieldSpec] = []
    for f in cfg.fields:
        # ExtField.unit 是显式覆盖（用户在「扩展数据」页填的），优先级最高；
        # 其次才是代码侧的证据表；再退到按 dtype 能确定的单位；最后如实记 unknown。
        declared = units.get(f.name, (None, ""))
        unit = getattr(f, "unit", None) or declared[0] or _unit_from_dtype(getattr(f, "dtype", None))
        note = declared[1]
        fields.append(FieldSpec(f.name, unit or UNIT_UNKNOWN, note))

    source, basis = _EXT_SOURCE.get(
        cfg.id, ("未记录来源（databridge 导入，见 docs/ 接入说明）", "none")
    )
    pull = getattr(cfg, "pull", None)
    if pull is not None and getattr(pull, "url", None):
        source, basis = f"扩展数据拉取：{pull.url}", "config_pull"

    pit_mode, period_col, known_col = _EXT_PIT.get(cfg.id, (PIT_UNKNOWN, None, None))
    layout = "date_partition" if cfg.mode == "timeseries" else "single_file"
    relpath = (
        f"ext_data/{cfg.id}/timeseries" if cfg.mode == "timeseries" else f"ext_data/{cfg.id}/part.parquet"
    )
    date_col = "date" if cfg.mode == "timeseries" else None

    notes = list(_EXT_NOTES.get(cfg.id, ()))
    # ext 表不经 provider 边界（走 akshare/databridge 导入），单位与内部契约不一致。
    notes.append("外部分发的单位与内部行情契约不同，按本表声明为准")

    return DatasetSpec(
        id=cfg.id,
        label=cfg.label or cfg.id,
        domain="ext",
        layer=cfg.mode if cfg.mode in ("snapshot", "timeseries") else "snapshot",
        asset_type=None,
        relpath=relpath,
        layout=layout,
        date_column=date_col,
        date_format="YYYY-MM-DD" if date_col else None,
        # ExtConfig.currency 是用户在「扩展数据」页显式声明的覆盖；未声明才按人民币。
        currency=getattr(cfg, "currency", None) or CURRENCY_CNY,
        timezone="Asia/Shanghai",
        source=source,
        source_basis=basis,
        pit_mode=pit_mode,
        period_column=period_col,
        known_at_column=known_col,
        fields=tuple(fields),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 目录组装与新鲜度
# ---------------------------------------------------------------------------
# 各数据集的唯一键 —— 集中声明，避免在 20 多处构造点重复。未列出的表不参与
# 重复行检查（检查器会把"未声明键"如实记成 limitation，而不是拿 symbol 硬猜）。
_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "kline_daily": ("symbol", "date"),
    "kline_daily_enriched": ("symbol", "date"),
    "kline_etf_daily": ("symbol", "date"),
    "kline_etf_enriched": ("symbol", "date"),
    "kline_index_daily": ("symbol", "date"),
    "kline_index_enriched": ("symbol", "date"),
    "kline_minute": ("symbol", "datetime"),
    "kline_etf_minute": ("symbol", "datetime"),
    "adj_factor": ("symbol", "trade_date"),
    "instruments": ("symbol",),
    "instruments_etf": ("symbol",),
    "instruments_index": ("symbol",),
    "depth5": ("symbol",),
    "financials_metrics": ("symbol", "period_end"),
    "financials_shares": ("symbol", "period_end"),
    "regime_history": ("date",),
    "mainline_history": ("date", "kind", "member"),
    # ext 表：snapshot 一行一 symbol；timeseries 一行 (symbol, date)。
    "ext_valuation": ("symbol", "date"),
    "ext_fundflow": ("symbol", "flow_date"),
    "ext_gdhs_ts": ("symbol", "date"),
    "ext_bsdetail": ("symbol",),
    "ext_shareholders": ("symbol",),
    "ext_goodwill": ("symbol",),
    "ext_dividend": ("symbol",),
    "ext_forecast": ("symbol",),
    "ext_fundhold": ("symbol",),
    "ext_research": ("symbol",),
    "ext_unlock": ("symbol",),
    "ext_insider": ("symbol",),
    "ext_margin": ("symbol", "margin_date"),
    "ext_ztlimit": ("symbol", "zt_date"),
    "ext_blocktrade": ("symbol",),
    "ext_pledge": ("symbol",),
    # ext_gn_ths / ext_hy_ths 故意不声明：一 symbol 合法多行（多个概念/行业）。
}


def build_catalog(data_dir: Path) -> list[DatasetSpec]:
    """全部数据集条目：静态表 + 磁盘上的 ext_* 表。"""
    specs = _static_specs()
    try:
        for cfg in ExtConfigStore(data_dir).load_all():
            specs.append(ext_dataset_spec(cfg, data_dir))
    except Exception as e:  # noqa: BLE001 — 目录本身不应因单表配置损坏而整体失败
        logger.warning("读取扩展表配置失败，目录缺少 ext_* 条目: %s", e)
    return _with_keys(specs)


def _with_keys(specs: list[DatasetSpec]) -> list[DatasetSpec]:
    """补上唯一键，以及 PIT 可知日的**默认值**，让下游只有一个口径。

    `event_date` 的语义就是「日期列本身就是可知日」，所以没显式写 `known_at_column`
    时补 `date_column`。不补的话同一张表在 `/api/datasets/catalog` 里显示
    `known_at_column: null`，而 PIT 侧却按 `date` 过滤 —— 两处说法不一致本身就是坑。
    """
    out: list[DatasetSpec] = []
    for spec in specs:
        keys = _KEY_COLUMNS.get(spec.id, ())
        known = spec.known_at_column
        if known is None and spec.pit_mode == PIT_EVENT_DATE and spec.date_column:
            known = spec.date_column
        changes: dict[str, Any] = {}
        if keys:
            changes["key_columns"] = keys
        if known != spec.known_at_column:
            changes["known_at_column"] = known
        out.append(replace(spec, **changes) if changes else spec)
    return out


def get_dataset(data_dir: Path, dataset_id: str) -> DatasetSpec | None:
    for spec in build_catalog(data_dir):
        if spec.id == dataset_id:
            return spec
    return None


def _partition_day(path: Path) -> dt.date | None:
    """从 `date=YYYY-MM-DD` 分区目录名取日期；不在分区布局里则返回 None。"""
    for parent in (path.parent, path.parent.parent):
        if parent.name.startswith("date="):
            try:
                return dt.date.fromisoformat(parent.name[len("date=") :][:10])
            except ValueError:
                return None
    return None


def dataset_files(
    data_dir: Path,
    spec: DatasetSpec,
    *,
    days: Sequence[dt.date] | None = None,
) -> list[Path]:
    """数据集在磁盘上的文件清单（升序）。

    `layout` 决定取法：`date_partition` 取 `date=*/part.parquet`（可用 `days` 限定到
    指定会话），其余按 `single_file` 取 `relpath` 本身。文件不存在时返回空列表，
    由调用方如实说"没有数据"，而不是把空当成"通过检查"。
    """
    root = data_dir / spec.relpath
    if spec.layout == "date_partition":
        if days is not None:
            return [
                candidate
                for day in sorted(set(days))
                if (candidate := root / f"date={day.isoformat()}" / "part.parquet").is_file()
            ]
        if not root.is_dir():
            return []
        return sorted(root.glob("date=*/part.parquet"))
    if root.is_file():
        return [root]
    if root.is_dir():
        # `single_file` 的 relpath 可能是文件（instruments.parquet）也可能是目录
        # （financials/metrics），目录形态下按 part.parquet → *.parquet 兜底。
        return sorted(root.glob("part.parquet")) or sorted(root.glob("*.parquet"))
    return []


def read_dataset(
    data_dir: Path,
    spec: DatasetSpec,
    *,
    columns: Sequence[str] | None = None,
    days: Sequence[dt.date] | None = None,
) -> pl.DataFrame:
    """按数据集声明读取行（可选列裁剪）；读不动时返回空表并留下日志。

    分区布局下会把 `date` 从 Hive 路径名补成列 —— `ext_*` 时序表的 parquet 文件里
    通常没有 `date` 列（如 `ext_data/ext_valuation/timeseries/date=2026-11-05/`），
    只有父目录名带着它。因此只有当调用方**点名要 `date`** 而文件里没有时才补。
    """
    files = dataset_files(data_dir, spec, days=days)
    if not files:
        return pl.DataFrame()
    wanted = list(columns) if columns else None
    frames: list[pl.DataFrame] = []
    for path in files:
        try:
            schema = pl.read_parquet_schema(path)
        except Exception as exc:  # noqa: BLE001 — 单个分区坏掉不该让整表读取失败
            logger.warning("读取 %s 的 schema 失败，跳过该文件: %s", path, exc)
            continue
        try:
            frame = pl.read_parquet(
                path, columns=[c for c in (wanted or ()) if c in schema] or None
            )
        except Exception as exc:  # noqa: BLE001 — 同上：坏分区跳过，其余照读
            logger.warning("读取 %s 失败，跳过该文件: %s", path, exc)
            continue
        date_column = spec.date_column or "date"
        if wanted and date_column in wanted and date_column not in frame.columns:
            day = _partition_day(path)
            if day is not None:
                frame = frame.with_columns(pl.lit(day).alias(date_column))
        frames.append(frame)
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]


def _partition_days(root: Path) -> list[dt.date]:
    days: list[dt.date] = []
    if not root.exists():
        return days
    for d in root.glob("date=*"):
        raw = d.name[len("date=") :]
        try:
            days.append(dt.date.fromisoformat(raw[:10]))
        except ValueError:
            continue
    return sorted(days)


def session_days(data_dir: Path) -> list[dt.date]:
    """本地会话列表 = `kline_daily` 的分区日（升序）。

    用数据湖自己的分区名当交易日历：既不引入外部日历依赖，又让"落后几个交易日"
    的分母与本机实际抓到的交易日一致。`kline_daily` 自身缺失时返回空列表，
    调用方据此把滞后记为 None 而不是猜一个数。
    """
    return _partition_days(data_dir / "kline_daily")


def session_anchor(data_dir: Path) -> dt.date | None:
    days = session_days(data_dir)
    return days[-1] if days else None


def expected_anchor(data_dir: Path, sessions: list[dt.date]) -> dt.date | None:
    """当前"应该已经拿到"的最新完整交易日。

    盘中时 `kline_daily` 的当日分区只是快照 —— 今天的收盘数据本来就不存在，
    所以期望锚点要退到上一个会话，否则每个日频表在交易时段都会被误判 stale。
    收盘定版后期望锚点就是最新会话本身。
    """
    if not sessions:
        return None
    latest = sessions[-1]
    part = data_dir / "kline_daily" / f"date={latest.isoformat()}"
    if partition_settlement(part, latest) == "intraday":
        return sessions[-2] if len(sessions) > 1 else None
    return latest


def _sessions_after(sessions: list[dt.date], day: dt.date, anchor: dt.date) -> int:
    return sum(1 for d in sessions if day < d <= anchor)


def _stale_threshold(cadence: str) -> int | None:
    """落后多少个交易日算 stale；None = 该频率不按日期判陈旧。"""
    return {"daily": 0, "weekly": 7, "quarterly": 80}.get(cadence)


def _newest_mtime(root: Path) -> float | None:
    newest: float | None = None
    if not root.exists():
        return None
    targets = [root] if root.is_file() else root.rglob("*")
    for p in targets:
        if not p.is_file():
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or mt > newest:
            newest = mt
    return newest


def _count_rows(paths: list[Path]) -> int | None:
    total = 0
    for p in paths:
        try:
            total += int(pl.scan_parquet(p).select(pl.len()).collect().item())
        except Exception as e:  # noqa: BLE001 — 计数失败不掩盖新鲜度结论
            logger.debug("行数统计跳过 %s: %s", p, e)
            return None
    return total


def dataset_freshness(
    data_dir: Path, spec: DatasetSpec, *, sessions: list[dt.date] | None = None
) -> Freshness:
    """最新可用日期 + 落盘更新时间 + 定版状态 + 相对会话锚点的落后交易日数。"""
    sessions = session_days(data_dir) if sessions is None else sessions
    anchor = sessions[-1] if sessions else None
    expect = expected_anchor(data_dir, sessions)
    root = data_dir / spec.relpath
    updated = _fmt_mtime(_newest_mtime(root))

    if not root.exists():
        return Freshness(spec.id, "absent", None, updated, None, "n/a",
                         _iso(anchor), None, "路径不存在")

    if spec.layout == "date_partition":
        days = _partition_days(root)
        if not days:
            return Freshness(spec.id, "empty", None, updated, 0, "n/a",
                             _iso(anchor), None, "无 date= 分区")
        latest = days[-1]
        part = root / f"date={latest.isoformat()}"
        rows = _count_rows(sorted(part.glob("*.parquet")))
        settlement = partition_settlement(part, latest)
        basis, basis_kind = latest, "data"
    else:
        files = sorted(root.glob("*.parquet")) if root.is_dir() else [root]
        files = [p for p in files if p.is_file()]
        settlement = "n/a"
        if not files:
            return Freshness(spec.id, "empty", None, updated, 0, settlement,
                             _iso(anchor), None, "无 parquet 文件")
        rows = _count_rows(files)
        latest = _max_date_column(files, spec.date_column, spec.date_format)
        if latest is not None:
            basis, basis_kind = latest, "data"
        else:
            # 没有日期列（或列全空）时退回到落盘时间：对 snapshot 类表，
            # "上次同步是什么时候"才是唯一可判的新鲜度信号。
            mt = _newest_mtime(root)
            if mt is None:
                basis, basis_kind = None, "none"
            else:
                basis = dt.datetime.fromtimestamp(mt, tz=CN_TZ).date()
                basis_kind = "mtime"

    lag: int | None = None
    if basis is not None and expect is not None:
        lag = _sessions_after(sessions, basis, expect)

    threshold = _stale_threshold(spec.cadence)
    if lag is None:
        status, note = ("ok", "无法判定滞后（缺少会话锚点或日期依据）")
    elif threshold is None:
        status, note = "ok", f"按 {spec.cadence} 频率不判陈旧"
    elif lag > threshold:
        status = "stale"
        note = f"落后应到数据 {lag} 个交易日（{spec.cadence} 阈值 {threshold}）"
    else:
        status, note = "ok", ""

    if basis_kind == "mtime" and status == "ok":
        note = note or "依据落盘时间判定（本表无日期列）"
    if settlement == "intraday" and status == "ok":
        note = (note + "；" if note else "") + "最新分区为盘中快照，盘后会被重写"
    if expect != anchor and anchor is not None:
        extra = f"全市场最新会话 {anchor} 尚未定版，滞后基准取 {expect}"
        note = (note + "；" if note else "") + extra

    return Freshness(spec.id, status, _iso(latest), updated, rows, settlement,
                     _iso(expect), lag, note)


def _max_date_column(files: list[Path], column: str | None, fmt: str | None) -> dt.date | None:
    if not column:
        return None
    best: dt.date | None = None
    for p in files:
        try:
            schema = pl.read_parquet_schema(p)
        except Exception:  # noqa: BLE001
            continue
        if column not in schema:
            continue
        try:
            s = pl.read_parquet(p, columns=[column])[column]
        except Exception:  # noqa: BLE001
            continue
        for raw in s.drop_nulls().to_list():
            day = _coerce_date(raw, fmt)
            if day is not None and (best is None or day > best):
                best = day
    return best


def _coerce_date(raw: Any, fmt: str | None) -> dt.date | None:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if len(text) == 8 and text.isdigit():
            try:
                return dt.datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                return None
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _iso(day: dt.date | None) -> str | None:
    return day.isoformat() if day else None


def _fmt_mtime(ts: float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, tz=CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def catalog_payload(data_dir: Path, *, include_freshness: bool = True) -> list[dict[str, Any]]:
    """目录 + 新鲜度 —— 直接给 API 用的形状。"""
    sessions = session_days(data_dir)
    out: list[dict[str, Any]] = []
    for spec in build_catalog(data_dir):
        item = spec.to_dict()
        if include_freshness:
            try:
                item["freshness"] = dataset_freshness(data_dir, spec, sessions=sessions).to_dict()
            except Exception as e:  # noqa: BLE001 — 单表新鲜度失败不影响目录
                logger.warning("新鲜度解析失败 %s: %s", spec.id, e)
                item["freshness"] = None
        out.append(item)
    return out
