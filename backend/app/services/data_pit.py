"""PIT（point-in-time）「当时已知」口径的单一入口。

## 为什么需要它

用户要求「对财报和公告保留『当时已知』的时间，防止历史回测用了后来才公布的信息」。
仓库里其实**已经有两个正确实现**，但它们是各自内联的，而且没有任何地方能回答
「这张表到底能不能按当时已知取数」：

* ``backtest/fundamentals.py:122`` 用 ``date > _announce`` 做严格 T+1 闸门；
* ``share_capital.py:86`` 用 ``join_asof(backward)`` 按公告日对齐历史股本。

本模块把它们背后的口径提升为**可查询的声明**（进 ``/api/datasets``）与**可取数的入口**，
并对拿不到可知日的数据集 ``fail-closed``：宁可报错，也不返回一张"看起来完整"的表 ——
后者正是前视偏差的来源。

## 两条边界

* 口径只有两种可用：``event_date``（日期列本身就是可知日）与 ``announcement``
  （有公告日列，按严格晚于公告日折算）。``period_only`` / ``unknown`` 一律拒绝。
* 声明的是**可知日**，不是**数据质量**。快照存储（``ext_*`` 单文件表）只保留每个标的
  最新一行，按可知日过滤不会泄漏未来信息，但会比真实历史少；这一点写进 ``caveats``，
  不假装数据齐全。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from app.services.dataset_catalog import (
    PIT_ANNOUNCEMENT,
    PIT_EVENT_DATE,
    PIT_PERIOD_ONLY,
    PIT_UNKNOWN,
    PIT_USABLE_MODES,
    DatasetSpec,
    build_catalog,
    get_dataset,
    read_dataset,
)

logger = logging.getLogger(__name__)

#: 只有这两种口径能按「当时已知」取数。定义在 dataset_catalog，catalog 的
#: `pit.usable` 与本模块共用同一个常量，避免两处给出不同答案。
USABLE_MODES: tuple[str, ...] = PIT_USABLE_MODES

_MODE_TEXT: dict[str, str] = {
    PIT_EVENT_DATE: "日期列本身就是可知日（当日收盘后即可知），按 known_at <= as_of 过滤",
    PIT_ANNOUNCEMENT: (
        "有公告日列。公告多在盘后发布，故最早在公告日**次一交易日**起可用"
        "（严格 known_at < as_of），与 backtest/fundamentals.py 的 `date > _announce` 同口径"
    ),
    PIT_PERIOD_ONLY: "只有报告期、没有公告日。拿报告期冒充可知日会把未来信息提前，拒绝取数",
    PIT_UNKNOWN: "没有可用的日期列，无法确定可知日，拒绝取数",
}

_ANNOUNCEMENT_CAVEAT = "财务/公告类按 T+1 生效：公告日当天不算已知，避免盘后发布被当天回测使用"
_RESTATEMENT_CAVEAT = (
    "同一 (symbol, period_end) 在存储上只保留一行：公司若重述过，取到的是**最新版本**"
    "而非当时披露的版本，无法重建历史 vintage（见 services/financial_sync.py:165）"
)
_SNAPSHOT_CAVEAT = (
    "快照存储（单文件）只保留每个标的最新一行：按可知日过滤不会泄漏未来信息，"
    "但会比真实历史**少** —— 更早的版本已被当日覆盖，无法重建"
)


class PitUnavailableError(RuntimeError):
    """该数据集无法按「当时已知」取数。

    独立成一个异常类型，是为了让调用方**不能**用 ``except Exception`` 把
    "取不到" 和 "取到了空表" 混为一谈。
    """


@dataclass(frozen=True, slots=True)
class PitDeclaration:
    """一张表的「当时已知」声明。``usable=False`` 时 ``reason`` 说明为什么。"""

    dataset_id: str
    mode: str
    known_at_column: str | None
    period_column: str | None
    usable: bool
    reason: str
    caveats: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "mode": self.mode,
            "known_at_column": self.known_at_column,
            "period_column": self.period_column,
            "usable": self.usable,
            "reason": self.reason,
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True, slots=True)
class PitSlice:
    """``known_as_of`` 的结果：行 + 可解释的计数。"""

    dataset_id: str
    as_of: dt.date
    mode: str
    known_at_column: str
    rows: int
    dropped_unknown_known_at: int
    earliest_known_at: dt.date | None
    latest_known_at: dt.date | None
    caveats: tuple[str, ...]
    frame: pl.DataFrame

    def to_dict(self, *, rows_limit: int = 20) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "as_of": self.as_of.isoformat(),
            "mode": self.mode,
            "known_at_column": self.known_at_column,
            "rows": self.rows,
            "dropped_unknown_known_at": self.dropped_unknown_known_at,
            "earliest_known_at": _iso(self.earliest_known_at),
            "latest_known_at": _iso(self.latest_known_at),
            "caveats": list(self.caveats),
            "rows_preview": self.frame.head(rows_limit).to_dicts(),
        }


def _iso(day: dt.date | None) -> str | None:
    return day.isoformat() if isinstance(day, dt.date) else None


def declare(spec: DatasetSpec) -> PitDeclaration:
    """把目录条目转成「当时已知」声明（纯声明，不读盘）。"""
    mode = spec.pit_mode
    column = spec.known_at_column
    caveats: list[str] = []
    if mode == PIT_ANNOUNCEMENT:
        caveats.append(_ANNOUNCEMENT_CAVEAT)
    if spec.domain == "ext" and spec.layout == "single_file":
        caveats.append(_SNAPSHOT_CAVEAT)
    if spec.id in ("financials_metrics", "financials_shares"):
        caveats.append(_RESTATEMENT_CAVEAT)
    usable = mode in USABLE_MODES and bool(column)
    if usable:
        reason = f"{_MODE_TEXT[mode]}；可知日列 `{column}`"
    elif mode in USABLE_MODES:
        reason = f"{_MODE_TEXT[mode]}，但没有声明可知日列"
    else:
        reason = _MODE_TEXT.get(mode, _MODE_TEXT[PIT_UNKNOWN])
    return PitDeclaration(
        dataset_id=spec.id,
        mode=mode,
        known_at_column=column,
        period_column=spec.period_column,
        usable=usable,
        reason=reason,
        caveats=tuple(caveats),
    )


def declare_all(data_dir: Path) -> list[PitDeclaration]:
    """整份目录的 PIT 声明，按 dataset_id 排序（供 API 与前端一次看全）。"""
    return sorted((declare(spec) for spec in build_catalog(data_dir)), key=lambda d: d.dataset_id)


def require_declaration(data_dir: Path, dataset_id: str) -> tuple[DatasetSpec, PitDeclaration]:
    """取声明，不可用则抛 ``PitUnavailableError``。"""
    spec = get_dataset(data_dir, dataset_id)
    if spec is None:
        raise PitUnavailableError(f"未知数据集: {dataset_id}")
    decl = declare(spec)
    if not decl.usable:
        raise PitUnavailableError(f"数据集 {dataset_id} 无法按「当时已知」取数：{decl.reason}")
    return spec, decl


def backtest_guard(data_dir: Path, dataset_ids: Sequence[str]) -> list[PitDeclaration]:
    """回测/因子取数入口的 fail-closed 断言。

    只要有一个数据集不能按「当时已知」取数就抛错，并把**全部**问题一次列完 ——
    只报第一个会让调用方改一个试一次。
    """
    good: list[PitDeclaration] = []
    bad: list[str] = []
    for dataset_id in dataset_ids:
        spec = get_dataset(data_dir, dataset_id)
        if spec is None:
            bad.append(f"{dataset_id}（未知数据集）")
            continue
        decl = declare(spec)
        if decl.usable:
            good.append(decl)
        else:
            bad.append(f"{dataset_id}（{decl.reason}）")
    if bad:
        raise PitUnavailableError(
            "以下数据集不能按「当时已知」取数，用于历史回测会引入未来信息：" + "；".join(bad)
        )
    return good


def _known_at_expr(frame: pl.DataFrame, column: str) -> pl.Expr:
    """把可知日列统一成 Date —— 兼容 ISO 字符串、``YYYYMMDD`` 整数/字符串与日期类型。"""
    dtype = frame.schema[column]
    if dtype == pl.Utf8:
        head = pl.col(column).str.slice(0, 10)
        return head.str.to_date("%Y-%m-%d", strict=False).fill_null(
            head.str.to_date("%Y%m%d", strict=False)
        )
    if dtype in (pl.Date, pl.Datetime):
        return pl.col(column).cast(pl.Date, strict=False)
    return pl.col(column).cast(pl.Utf8, strict=False).str.slice(0, 10).str.to_date(
        "%Y-%m-%d", strict=False
    )


def _require_known_at_column(frame: pl.DataFrame, decl: PitDeclaration) -> str:
    """确认可知日列存在。缺列时抛 ``PitUnavailableError``（不能退回全表）。"""
    column = decl.known_at_column
    if not column:
        raise PitUnavailableError(f"数据集 {decl.dataset_id} 没有可知日列：{decl.reason}")
    if frame.is_empty():
        return column
    if column not in frame.columns:
        raise PitUnavailableError(
            f"数据集 {decl.dataset_id} 声明可知日为 `{column}`，但实际数据里没有该列 —— "
            "按「当时已知」取数会退回全表，故拒绝"
        )
    return column


def apply_known_as_of(
    frame: pl.DataFrame,
    decl: PitDeclaration,
    as_of: dt.date,
) -> pl.DataFrame:
    """对**已经读出的**行施加「当时已知」过滤（同一口径，避免重复读盘）。

    可知日为空的行一律**丢掉**，绝不因为"没日期"就放行 —— 放行正是前视偏差的来源。
    "整列的可知日都为空"属于数据集级问题，由 ``known_as_of`` 直接抛错，见那里。
    """
    column = _require_known_at_column(frame, decl)
    if frame.is_empty():
        return frame
    marked = frame.with_columns(_known_at_expr(frame, column).alias("_pit_known_at"))
    keep = (
        pl.col("_pit_known_at") < pl.lit(as_of)
        if decl.mode == PIT_ANNOUNCEMENT
        else pl.col("_pit_known_at") <= pl.lit(as_of)
    )
    return marked.filter(pl.col("_pit_known_at").is_not_null() & keep).drop("_pit_known_at")


def spec_field_names(spec: DatasetSpec) -> tuple[str, ...]:
    """数据集声明的列名（含 key_columns 与日期列），用于在列裁剪时补齐必需列。

    日期列必须算进来：`depth5` / `kline_minute` 的日期只存在于 `date=YYYY-MM-DD`
    分区目录名上，不点名要它就不会被补成列，随后按可知日过滤会因"没有该列"而拒绝。
    """
    names = [f.name for f in spec.fields]
    for column in (*spec.key_columns, spec.date_column, spec.known_at_column):
        if column and column not in names:
            names.append(column)
    return tuple(names)


def known_as_of(
    data_dir: Path,
    dataset_id: str,
    as_of: dt.date,
    *,
    columns: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
) -> PitSlice:
    """返回在 ``as_of`` 当天**已经可知**的行。

    ``columns`` 只做列裁剪；``symbols`` 过滤标的。想直接对已有 DataFrame 施加同一口径
    时用 ``apply_known_as_of``，不要从这里取全表再自己过滤。
    """
    spec, decl = require_declaration(data_dir, dataset_id)
    column = decl.known_at_column
    assert column is not None  # require_declaration 已保证 usable ⇒ column 非空
    declared = spec_field_names(spec)
    # 无论调用方要哪些列，都要把可知日读进来：`ext_*` 时序表的日期列只存在于
    # `date=YYYY-MM-DD` 目录名上，必须点名要 `date` 才会被补成列。
    wanted = list(columns) if columns else list(declared)
    for extra in (column, "symbol"):
        if extra in declared and extra not in wanted:
            wanted.append(extra)
    frame = read_dataset(data_dir, spec, columns=wanted)
    # 整张表的可知日全为空 = 这张表的"当时已知"信息根本不可用（列在、值不在）。
    # 这是数据集级问题，必须 fail-closed；按标的过滤后子集恰好全空则不是，
    # 那种情况由 apply_known_as_of 丢掉空行即可，见其 docstring。
    if not frame.is_empty() and column in frame.columns:
        known_all = frame.select(_known_at_expr(frame, column).alias("_known"))["_known"]
        if int(known_all.null_count()) == frame.height:
            raise PitUnavailableError(
                f"数据集 {dataset_id} 的可知日列 `{column}` 全为空 —— "
                "无法判定「当时是否已知」，按「当时已知」取数会引入未来信息，故拒绝"
            )
    if symbols:
        if "symbol" not in frame.columns:
            raise PitUnavailableError(f"数据集 {dataset_id} 没有 symbol 列，无法按标的过滤")
        frame = frame.filter(pl.col("symbol").is_in([str(s) for s in symbols]))
    kept = apply_known_as_of(frame, decl, as_of)

    dropped = 0
    if not frame.is_empty() and column in frame.columns:
        marked_all = frame.with_columns(_known_at_expr(frame, column).alias("_pit_known_at"))
        dropped = int(marked_all["_pit_known_at"].null_count())
    earliest: dt.date | None = None
    latest: dt.date | None = None
    if not kept.is_empty():
        kept_known = kept.with_columns(
            _known_at_expr(kept, column).alias("_pit_known_at")
        )["_pit_known_at"]
        earliest, latest = kept_known.min(), kept_known.max()
    return PitSlice(
        dataset_id=dataset_id,
        as_of=as_of,
        mode=decl.mode,
        known_at_column=column,
        rows=kept.height,
        dropped_unknown_known_at=dropped,
        earliest_known_at=earliest,
        latest_known_at=latest,
        caveats=decl.caveats,
        frame=kept,
    )
