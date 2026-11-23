"""数据质量检查 —— 六类问题的统一产出。

覆盖: 缺失(分区) / 重复(唯一键) / 停牌(残留行) / 复权(事件覆盖 + 因子自洽)
      / 异常跳变(超出涨跌停可解释范围) / 跨源冲突(同 symbol+日期 两来源不一致)。

设计取舍:

- **只报告, 不修数据。** 仓库里已有多条修补路径 (`data_integrity.launch_integrity_repair`、
  `daily_repair_nontrading_symbols` 等)。质量检查的价值在于"把问题说清楚"; 顺手改数据会让
  调用方无法区分"本来就没问题"和"被悄悄改好了"。
- **口径一律复用既有实现, 不自造。** 停牌判据与 `indicators.pipeline.filter_halt_days`
  一致, 涨跌停与上市初期无涨跌幅窗口复用 `app.price_limits`, 分区缺失/盘中快照复用
  `services.data_integrity`。这类检查最容易腐化的地方就是长出第二套口径。
- **不确定就如实说。** 没有声明唯一键的表不猜键, 直接记 limitation; 列缺失、文件读不动
  同样记 limitation —— 不用"没发现"冒充"没问题"。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import CN_TZ, cn_now
from app.price_limits import (
    no_limit_window_days,
    no_limit_window_margin_days,
    parse_listing_date,
    polars_is_risk_warning_name,
    polars_limit_price,
    polars_price_limit_pct,
)
from app.services.data_integrity import partition_settlement, scan_recent_integrity
from app.services.dataset_catalog import DatasetSpec, get_dataset, session_days

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 问题分类
# ---------------------------------------------------------------------------
KIND_MISSING = "missing"        # 应有分区缺失 / 未定版
KIND_DUPLICATE = "duplicate"    # 唯一键重复行
KIND_HALT = "halt"              # 停牌残留行（应被 filter_halt_days 过滤）
KIND_ADJUST = "adjust"          # 复权事件覆盖 / 因子自洽
KIND_JUMP = "jump"              # 超出涨跌停可解释范围的价格跳变
KIND_CONFLICT = "conflict"      # 同 symbol+日期 两来源数值不一致

ALL_KINDS: tuple[str, ...] = (
    KIND_MISSING,
    KIND_DUPLICATE,
    KIND_HALT,
    KIND_ADJUST,
    KIND_JUMP,
    KIND_CONFLICT,
)

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

#: 默认回看窗口(交易日)。与 data_integrity.SCAN_WINDOW_DAYS 同量级 —— 再长会让
#: 每次检查的读放大变得不可接受, 而绝大多数问题在最近两周内就会显形。
DEFAULT_LOOKBACK_DAYS = 10

#: finding 里最多回带的标的样本数（完整数量在 affected 里）
MAX_SYMBOL_SAMPLES = 12

#: 复权因子自洽容忍度。实测全样本 (10,148 个事件) 的偏差 p50=0、p99=0.10%、
#: max=0.28%（源于每股分红四舍五入到 0.001 元 + prev_close 两位小数），
#: 所以 0.5% 之外才值得报。
_ADJUST_REL_TOLERANCE = 0.005

#: 跨源一致容忍度: 同一交易日的收盘价在两个独立来源之间允许的差异。
_CONFLICT_REL_TOLERANCE = 0.005

#: 同一管线的两阶段（原始落盘 vs enriched 快照）应当逐位相等。
_PRECISE_REL_TOLERANCE = 1e-9

#: 涨跌停判定价的交易所容差（与 indicators/pipeline.py 一致：生效价 ∓ 0.005）
_LIMIT_PRICE_TOLERANCE = 0.005

#: 只有这三张表是 data_integrity 的"日频权威历史"对象
_DAILY_PRICE_TABLES = ("kline_daily", "kline_etf_daily", "kline_index_daily")


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------
def _iso(day: dt.date | None) -> str | None:
    return day.isoformat() if day else None


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """一条可展示的质量问题。"""

    dataset_id: str
    kind: str
    severity: str
    scope: str                       # 人类可读范围, 如 "2026-11-04" / "近 10 个交易日"
    day: dt.date | None
    affected: int                    # 受影响的行/标的/事件数
    symbols: tuple[str, ...] = ()    # 样本（截断到 MAX_SYMBOL_SAMPLES）
    detail: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "severity": self.severity,
            "scope": self.scope,
            "day": _iso(self.day),
            "affected": self.affected,
            "symbols": list(self.symbols),
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    """一次检查的完整结论。"""

    dataset_id: str
    checked_at: str                  # 北京时间
    checked_days: tuple[str, ...]
    findings: tuple[QualityFinding, ...]
    counts: dict[str, int]           # 每类检查实际覆盖的行/事件数 —— "检查真的跑了"的证据
    limitations: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {k: 0 for k in ALL_KINDS}
        for f in self.findings:
            by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        return {
            "dataset_id": self.dataset_id,
            "checked_at": self.checked_at,
            "checked_days": list(self.checked_days),
            "ok": self.ok,
            "findings": [f.to_dict() for f in self.findings],
            "finding_counts": by_kind,
            "counts": dict(self.counts),
            "limitations": list(self.limitations),
        }


# ---------------------------------------------------------------------------
# 读取工具
# ---------------------------------------------------------------------------
class _SchemaGapError(Exception):
    """分区里缺列/读不动 —— 记 limitation，不当成"没问题"。"""


def _partition_day(path: Path) -> dt.date | None:
    """从 `.../date=YYYY-MM-DD/part.parquet` 取分区日期。"""
    for parent in path.parents:
        if parent.name.startswith("date="):
            raw = parent.name[len("date=") :]
            try:
                return dt.date.fromisoformat(raw[:10])
            except ValueError:
                return None
    return None


def _read(paths: list[Path], columns: list[str] | None = None) -> pl.DataFrame:
    """按需选列读多个分区。

    分区日期兜底: 时间序列表的 `date` 只存在于 Hive 分区路径上（如
    `ext_data/ext_valuation/timeseries/date=2026-11-05/part.parquet` 的文件里并没有
    date 列）。请求 date 而文件里没有时, 用分区名补一列, 而不是报缺列。
    """
    if not paths:
        return pl.DataFrame()
    frames: list[pl.LazyFrame] = []
    for p in paths:
        schema = pl.read_parquet_schema(p)
        requested = list(columns) if columns is not None else list(schema.keys())
        missing = [c for c in requested if c not in schema]
        if missing == ["date"] or set(missing) == {"date"}:
            day = _partition_day(p)
            if day is None:
                raise _SchemaGapError(f"{p.parent.name} 缺 date 列且分区名无日期")
            missing = []
        elif missing:
            raise _SchemaGapError(f"{p.name} 缺少列 {missing}")
        cols = [c for c in requested if c in schema]
        lf = pl.scan_parquet(p).select(cols)
        if "date" in requested and "date" not in cols:
            lf = lf.with_columns(pl.lit(day).alias("date"))
        frames.append(lf)
    try:
        return pl.concat(frames, how="diagonal_relaxed").collect()
    except Exception as e:  # 分区 schema 冲突不应让整表检查崩掉
        raise _SchemaGapError(f"分区 schema 冲突: {e}") from e


def _dataset_files(data_dir: Path, spec: DatasetSpec, days: list[dt.date]) -> list[Path]:
    root = data_dir / spec.relpath
    if spec.layout == "date_partition":
        if not root.is_dir():
            return []
        wanted = {d.isoformat() for d in days}
        out: list[Path] = []
        for part in sorted(root.glob("date=*")):
            if wanted and part.name[len("date=") :][:10] not in wanted:
                continue
            out.extend(sorted(part.glob("*.parquet")))
        return out
    if root.is_file():
        return [root]
    return []


def _sample(values: list[Any]) -> tuple[str, ...]:
    uniq = sorted({str(v) for v in values if v not in (None, "")})
    return tuple(uniq[:MAX_SYMBOL_SAMPLES])


def _normalize_date(raw: Any) -> dt.date | None:
    """兼容 YYYY-MM-DD / YYYYMMDD / date / datetime 的日期归一化。"""
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-":
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    if len(text) == 8 and text.isdigit():
        try:
            return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 检查 1: 缺失（分区）
# ---------------------------------------------------------------------------
_KIND_SEVERITY = {
    "missing": SEVERITY_HIGH,
    "snapshot": SEVERITY_HIGH,
    "corrupt": SEVERITY_HIGH,
    "invalid": SEVERITY_HIGH,
    "unconfirmed": SEVERITY_MEDIUM,
}

#: `data_integrity` 的 issue.kind 是给程序看的关键字，reason 不一定有值；
#: 这里补齐中文解释，避免下游只看到一行 "snapshot"。
_ISSUE_KIND_TEXT = {
    "snapshot": "分区由实时轮询写入（quote_ts 早于收盘），尚未被盘后权威数据替换",
    "missing": "交易日分区缺失",
    "corrupt": "分区文件读取失败（列缺失或损坏）",
    "invalid": "分区含非法数值行（零价/负价/量价关系矛盾）",
    "unconfirmed": "无法确认该分区是否已定版",
}


def _check_missing(
    data_dir: Path,
    spec: DatasetSpec,
    days: list[dt.date],
    out: list[QualityFinding],
    counts: dict[str, int],
    limits: list[str],
) -> None:
    if spec.id not in _DAILY_PRICE_TABLES:
        limits.append(
            f"{spec.id}: 分区缺失检查只覆盖三张日频权威历史表 "
            "（kline_daily / kline_etf_daily / kline_index_daily）；本表请以 freshness 的 "
            "lag_sessions 为准"
        )
        return
    if not days:
        limits.append(f"{spec.id}: 无会话窗口，跳过分区缺失检查")
        return
    try:
        issues = scan_recent_integrity(data_dir, lookback_days=len(days), tables=[spec.id])
    except Exception as e:  # noqa: BLE001 — 检查失败必须显式暴露，不能静默通过
        limits.append(f"{spec.id}: 分区缺失扫描失败: {e}")
        return
    counts["missing_partitions"] = len(issues)
    for issue in issues:
        out.append(
            QualityFinding(
                dataset_id=spec.id,
                kind=KIND_MISSING,
                severity=_KIND_SEVERITY.get(issue.kind, SEVERITY_MEDIUM),
                scope=_iso(issue.day) or "未知日期",
                day=issue.day,
                affected=len(issue.symbols) or 1,
                symbols=_sample(list(issue.symbols)),
                detail=(
                    f"{issue.kind}: "
                    f"{issue.reason or _ISSUE_KIND_TEXT.get(issue.kind, '分区状态异常')}"
                ),
                evidence=tuple(issue.files[:3]),
            )
        )


# ---------------------------------------------------------------------------
# 检查 2: 重复（唯一键）
# ---------------------------------------------------------------------------
def _check_duplicate(
    data_dir: Path,
    spec: DatasetSpec,
    files: list[Path],
    out: list[QualityFinding],
    counts: dict[str, int],
    limits: list[str],
) -> None:
    if not spec.key_columns:
        limits.append(f"{spec.id}: 未声明唯一键，跳过重复行检查")
        return
    if not files:
        limits.append(f"{spec.id}: 无可读分区文件，跳过重复行检查")
        return
    keys = list(spec.key_columns)
    try:
        df = _read(files, keys)
    except _SchemaGapError as e:
        limits.append(f"{spec.id}: 重复行检查跳过 —— {e}")
        return
    present = [c for c in keys if c in df.columns]
    if len(present) != len(keys):
        limits.append(f"{spec.id}: 唯一键列 {keys} 实际只有 {present}，跳过重复行检查")
        return
    counts["duplicate_keys_scanned"] = df.height
    if df.is_empty():
        return
    dups = df.group_by(keys).len().filter(pl.col("len") > 1).sort("len", descending=True)
    if dups.is_empty():
        return
    counts["duplicate_keys_found"] = dups.height
    affected_rows = int(dups["len"].sum())
    sample_symbols = (
        [str(v) for v in dups["symbol"].head(MAX_SYMBOL_SAMPLES)]
        if "symbol" in dups.columns
        else []
    )
    examples = [
        " / ".join(str(row[k]) for k in keys) + f" ×{row['len']}"
        for row in dups.head(3).to_dicts()
    ]
    out.append(
        QualityFinding(
            dataset_id=spec.id,
            kind=KIND_DUPLICATE,
            severity=SEVERITY_HIGH,
            scope=f"近 {len(files)} 个分区文件",
            day=None,
            affected=affected_rows,
            symbols=_sample(sample_symbols),
            detail=(
                f"唯一键 {' + '.join(keys)} 出现 {dups.height} 组重复，涉及 {affected_rows} 行；"
                f"例：{'；'.join(examples)}"
            ),
            evidence=(f"key={'+'.join(keys)}",),
        )
    )


# ---------------------------------------------------------------------------
# 检查 3: 停牌残留行
# ---------------------------------------------------------------------------
def _check_halt(
    data_dir: Path,
    spec: DatasetSpec,
    files: list[Path],
    out: list[QualityFinding],
    counts: dict[str, int],
    limits: list[str],
) -> None:
    if not files:
        return
    try:
        df = _read(files, ["symbol", "open", "high", "volume", "amount"])
    except _SchemaGapError as e:
        limits.append(f"{spec.id}: 停牌检查跳过 —— {e}")
        return
    if df.is_empty():
        return
    counts["halt_rows_scanned"] = df.height
    halted = (pl.col("open") == 0) & (pl.col("high") == 0)
    if "volume" in df.columns and "amount" in df.columns:
        halted = halted | ((pl.col("volume") == 0) & (pl.col("amount") == 0))
    bad = df.filter(halted)
    if bad.is_empty():
        return
    counts["halt_rows_found"] = bad.height
    out.append(
        QualityFinding(
            dataset_id=spec.id,
            kind=KIND_HALT,
            severity=SEVERITY_MEDIUM,
            scope=f"近 {len(files)} 个分区文件",
            day=None,
            affected=bad.height,
            symbols=_sample(bad["symbol"].to_list()),
            detail=(
                f"{bad.height} 行满足停牌判据（open=high=0，或 volume=amount=0）；"
                "这些行会污染 MA/ATR 等指标，下游应经 filter_halt_days 过滤"
            ),
            evidence=("判据: (open==0 & high==0) | (volume==0 & amount==0)",),
        )
    )


# ---------------------------------------------------------------------------
# 检查 4: 复权（事件覆盖 + 因子自洽）
# ---------------------------------------------------------------------------
def theoretical_ex_factor(
    dividend: float | None,
    bonus: float | None,
    allot: float | None,
    allot_price: float | None,
    prev_close: float | None,
) -> float | None:
    """由除权除息字段重算 ex_factor。

    理论除权参考价 = (prev_close - 每股分红 + 配股数 × 配股价) / (1 + 送转 + 配股)
    ex_factor      = prev_close / 理论除权参考价

    实测: 全部 10,148 个事件的相对偏差 p99 = 0.10%、max = 0.28%
    （来自每股分红四舍五入到 0.001 元与 prev_close 两位小数）。
    """
    if not prev_close or prev_close <= 0:
        return None
    ref = (prev_close - (dividend or 0.0) + (allot or 0.0) * (allot_price or 0.0)) / (
        1.0 + (bonus or 0.0) + (allot or 0.0)
    )
    if ref <= 0:
        return None
    return prev_close / ref


def _check_adjust(
    data_dir: Path,
    subject_id: str,
    days: list[dt.date],
    out: list[QualityFinding],
    counts: dict[str, int],
    limits: list[str],
) -> None:
    """复权口径核对：事件在行情中是否有行 + 因子是否与事件字段自洽。"""
    if not days:
        limits.append(f"{subject_id}: 无会话窗口，跳过复权检查")
        return
    price_files = _dataset_files(data_dir, kline_daily_spec(data_dir), days)
    adj_path = data_dir / "adj_factor" / "all.parquet"
    if not price_files:
        limits.append(f"{subject_id}: 无 kline_daily 分区，跳过复权检查")
        return
    if not adj_path.is_file():
        limits.append(f"{subject_id}: 缺少 adj_factor/all.parquet，跳过复权检查")
        return
    try:
        prices = _read(price_files, ["symbol", "date"])
    except _SchemaGapError as e:
        limits.append(f"{subject_id}: 复权检查跳过 —— {e}")
        return
    try:
        af = pl.read_parquet(adj_path)
    except Exception as e:  # noqa: BLE001
        limits.append(f"{subject_id}: adj_factor 读取失败: {e}")
        return

    af = af.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    events = af.filter(
        pl.col("trade_date").is_not_null()
        & (pl.col("trade_date") >= pl.lit(days[0]))
        & (pl.col("trade_date") <= pl.lit(days[-1]))
    )
    counts["adjust_events_scanned"] = events.height
    if events.is_empty():
        return
    prices = prices.with_columns(pl.col("date").cast(pl.Date, strict=False))

    # (a) 事件日在行情里没有对应行 → 复权基准缺失
    probe = events.select(pl.col("symbol"), pl.col("trade_date").alias("date")).join(
        prices.with_columns(pl.lit(True).alias("_has")),
        on=["symbol", "date"],
        how="left",
    )
    missing_rows = probe.filter(pl.col("_has").is_null())
    if missing_rows.height:
        counts["adjust_events_missing_price"] = missing_rows.height
        out.append(
            QualityFinding(
                dataset_id=subject_id,
                kind=KIND_ADJUST,
                severity=SEVERITY_MEDIUM,
                scope=f"{days[0].isoformat()} ~ {days[-1].isoformat()}",
                day=None,
                affected=missing_rows.height,
                symbols=_sample(missing_rows["symbol"].to_list()),
                detail=(
                    f"{missing_rows.height} 个除权除息事件在 kline_daily 中找不到当日行情行，"
                    "复权基准缺失"
                ),
                evidence=("依据: adj_factor/all.parquet × kline_daily",),
            )
        )

    # (b) 因子与事件字段不自洽
    factor_rows = events.filter(pl.col("ex_factor").is_not_null() & (pl.col("ex_factor") > 0))
    bad_symbols: list[str] = []
    bad_examples: list[str] = []
    scanned = 0
    for row in factor_rows.iter_rows(named=True):
        theo = theoretical_ex_factor(
            row.get("dividend"),
            row.get("bonus"),
            row.get("allot"),
            row.get("allot_price"),
            row.get("prev_close"),
        )
        if not theo or theo <= 0:
            continue
        scanned += 1
        rel = abs(row["ex_factor"] / theo - 1.0)
        if rel > _ADJUST_REL_TOLERANCE:
            bad_symbols.append(str(row["symbol"]))
            if len(bad_examples) < 3:
                bad_examples.append(
                    f"{row['symbol']}@{row['trade_date']} 存储 {row['ex_factor']:.6f} "
                    f"vs 重算 {theo:.6f}（偏差 {rel:.2%}）"
                )
    counts["adjust_factors_scanned"] = scanned
    if bad_symbols:
        counts["adjust_factors_mismatch"] = len(bad_symbols)
        out.append(
            QualityFinding(
                dataset_id=subject_id,
                kind=KIND_ADJUST,
                severity=SEVERITY_MEDIUM,
                scope=f"{days[0].isoformat()} ~ {days[-1].isoformat()}",
                day=None,
                affected=len(bad_symbols),
                symbols=_sample(bad_symbols),
                detail=(
                    f"{len(bad_symbols)} 个事件的 ex_factor 与自身字段重算值偏差超过 "
                    f"{_ADJUST_REL_TOLERANCE:.1%}；例：{'；'.join(bad_examples)}"
                ),
                evidence=(
                    "重算公式: prev_close / ((prev_close - dividend + allot×allot_price) "
                    "/ (1 + bonus + allot))",
                ),
            )
        )


# ---------------------------------------------------------------------------
# 检查 5: 异常跳变
# ---------------------------------------------------------------------------
def _check_jump(
    data_dir: Path,
    spec: DatasetSpec,
    files: list[Path],
    out: list[QualityFinding],
    counts: dict[str, int],
    limits: list[str],
) -> None:
    """不复权收盘价的日间跳变超出涨跌停可解释范围。

    判据用交易所生效价（含 0.005 容差），与 `indicators/pipeline.py` 的涨停判定同口径；
    上市初期无涨跌幅窗口按 `app.price_limits` 的规则豁免。
    """
    if spec.asset_type != "stock":
        limits.append(f"{spec.id}: 异常跳变检查只覆盖 A 股日K（指数/ETF/维表不适用）")
        return
    if not files:
        return
    # 未定版分区里的价格还在变（实测 2026-11-06 盘中快照的 close 在两次运行之间从
    # 3.84 变成 3.69），拿它判"异常跳变"只会产出噪音，定版后复跑才有意义。
    settled: list[Path] = []
    unsettled: list[str] = []
    for path in files:
        part_day = _partition_day(path)
        if part_day is not None and partition_settlement(path.parent, part_day) == "intraday":
            unsettled.append(_iso(part_day) or "")
            continue
        settled.append(path)
    if unsettled:
        limits.append(
            f"{spec.id}: 异常跳变检查跳过未定版分区 {'、'.join(sorted(set(unsettled)))}"
            "（实时快照，价格仍在变动；定版后复跑）"
        )
    files = settled
    if not files:
        return
    # 涨跌停是对不复权价格定的：enriched 的 close 是前复权价，必须换用 raw_close，
    # 否则除权日两侧的复权比例变化会被当成跳变。
    price_column = "raw_close" if any(f.name == "raw_close" for f in spec.fields) else "close"
    try:
        df = _read(files, ["symbol", "date", price_column])
    except _SchemaGapError as e:
        limits.append(f"{spec.id}: 异常跳变检查跳过 —— {e}")
        return
    if price_column != "close":
        df = df.rename({price_column: "close"})
        limits.append(f"{spec.id}: 异常跳变检查使用不复权列 raw_close（close 为前复权价）")
    if df.is_empty():
        return

    ins_path = data_dir / "instruments" / "instruments.parquet"
    if not ins_path.is_file():
        limits.append(f"{spec.id}: 缺 instruments 维表，异常跳变检查跳过")
        return
    try:
        ins = pl.read_parquet(ins_path).select(["symbol", "name", "listing_date"])
    except Exception as e:  # noqa: BLE001
        limits.append(f"{spec.id}: instruments 读取失败: {e}")
        return

    df = df.with_columns(pl.col("date").cast(pl.Date, strict=False)).sort(["symbol", "date"])
    df = df.with_columns(
        pl.col("close").shift(1).over("symbol").alias("_prev_close"),
        pl.col("date").rank("ordinal").over("symbol").alias("_day_rank"),
        pl.col("date").min().over("symbol").alias("_first_date"),
    ).join(ins, on="symbol", how="left")

    # 除权除息日不能用原始昨收算涨跌停：交易所按除权参考价定价，adj_factor 里
    # prev_close 是除权前收盘价、ex_factor 是除权比例，二者相除即参考价。
    # 实测 002249.SZ 2026-10-30：原始昨收 8.28 看着是 -10.99% 超限，但当日 10 派 0.89 元，
    # 除权参考价 8.19、跌停价 7.37，收盘 7.37 正是正常跌停。
    adj_path = data_dir / "adj_factor" / "all.parquet"
    if adj_path.is_file():
        try:
            adj = (
                pl.read_parquet(
                    adj_path, columns=["symbol", "trade_date", "ex_factor", "prev_close"]
                )
                .drop_nulls(["symbol", "trade_date", "ex_factor", "prev_close"])
                .filter(pl.col("ex_factor") > 0)
                .with_columns(pl.col("trade_date").cast(pl.Date, strict=False).alias("date"))
                .with_columns((pl.col("prev_close") / pl.col("ex_factor")).alias("_ex_ref"))
                .select(["symbol", "date", "_ex_ref"])
                .unique(subset=["symbol", "date"], keep="last")
            )
            df = df.join(adj, on=["symbol", "date"], how="left").with_columns(
                pl.coalesce([pl.col("_ex_ref"), pl.col("_prev_close")]).alias("_prev_close")
            )
        except Exception as e:  # noqa: BLE001 — 读不到复权事件就退回原始昨收，并如实记录
            limits.append(f"{spec.id}: 除权参考价读取失败，跳变判据退回原始昨收: {e}")
    else:
        limits.append(f"{spec.id}: 无 adj_factor，除权除息日的跳变判据退回原始昨收")
    df = df.with_columns(
        polars_is_risk_warning_name(pl.col("name")).alias("_is_st"),
        pl.col("listing_date").map_elements(parse_listing_date, return_dtype=pl.Date).alias("_listing"),
    )
    df = df.with_columns(
        polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.col("_is_st")).alias("_limit_pct")
    )
    df = df.with_columns(
        polars_limit_price(pl.col("_prev_close"), pl.col("_limit_pct"), up=True).alias("_limit_up"),
        polars_limit_price(pl.col("_prev_close"), pl.col("_limit_pct"), up=False).alias("_limit_down"),
    )

    # 上市初期无涨跌幅窗口：按 symbol 预算（唯一标的数远小于行数），
    # 判定式与 indicators/pipeline.py:896-905 同构。
    rows: list[tuple[str, int, dt.date | None]] = []
    for sym, listing in df.select(["symbol", "_listing"]).unique().iter_rows():
        sym_s = str(sym)
        win = no_limit_window_days(sym_s, listing)
        boundary = (
            listing + dt.timedelta(days=no_limit_window_margin_days(sym_s))
            if listing is not None
            else None
        )
        rows.append((sym_s, win, boundary))
    win_df = pl.DataFrame(
        rows,
        schema={"symbol": pl.String, "_win": pl.Int64, "_boundary": pl.Date},
        orient="row",
    )
    df = df.join(win_df, on="symbol", how="left").with_columns(pl.col("_win").fill_null(0))
    df = df.with_columns(
        (
            (pl.col("_win") > 0)
            & pl.col("_boundary").is_not_null()
            & (pl.col("_first_date") <= pl.col("_boundary"))
            & (pl.col("date") <= pl.col("_boundary"))
            & (pl.col("_day_rank") <= pl.col("_win"))
        )
        .fill_null(False)
        .alias("_no_limit")
    )

    counts["jump_rows_scanned"] = df.height
    usable = df.filter(
        pl.col("_prev_close").is_not_null()
        & (pl.col("_prev_close") > 0)
        & (pl.col("close") > 0)
        & ~pl.col("_no_limit")
    )
    bad = usable.filter(
        (pl.col("close") > pl.col("_limit_up") + _LIMIT_PRICE_TOLERANCE)
        | (pl.col("close") < pl.col("_limit_down") - _LIMIT_PRICE_TOLERANCE)
    )
    if bad.is_empty():
        return
    counts["jump_rows_found"] = bad.height
    examples = [
        f"{row['symbol']}@{row['date']} 收 {row['close']} / 基准 {row['_prev_close']}"
        f"（{'除权参考价' if row.get('_ex_ref') is not None else '昨收'}）"
        f"（可解释区间 {row['_limit_down']}~{row['_limit_up']}）"
        for row in bad.head(3).iter_rows(named=True)
    ]
    out.append(
        QualityFinding(
            dataset_id=spec.id,
            kind=KIND_JUMP,
            severity=SEVERITY_HIGH,
            scope=f"近 {len(files)} 个已定版分区文件",
            day=None,
            affected=bad.height,
            symbols=_sample(bad["symbol"].to_list()),
            detail=(
                f"{bad.height} 行不复权收盘价超出涨跌停可解释范围"
                f"（判定价 ∓ {_LIMIT_PRICE_TOLERANCE}）；例：{'；'.join(examples)}"
            ),
            evidence=(
                "判据: 交易所生效价（price_limits）+ 上市初期无涨跌幅窗口豁免"
                " + 除权除息日改用除权参考价",
            ),
        )
    )


# ---------------------------------------------------------------------------
# 检查 6: 跨源冲突
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CrossCheck:
    """一对"同一事实、不同来源"的列。"""

    other_id: str          # 对照数据集 id
    left_column: str       # 本表列
    right_column: str      # 对照表列
    tolerance: float       # 相对容忍度
    note: str              # 为何这两列应当相等

    def to_dict(self) -> dict[str, Any]:
        return {
            "other_id": self.other_id,
            "left_column": self.left_column,
            "right_column": self.right_column,
            "tolerance": self.tolerance,
            "note": self.note,
        }


#: 只声明真正应当逐行相等的组合 —— 宁可少比几个，也不要把"本来就不该相等"的列
#: 写成冲突。新增比对必须先确认两列语义相同（单位、时点、复权口径）。
_CROSS_CHECKS: dict[str, tuple[CrossCheck, ...]] = {
    "kline_daily": (
        CrossCheck(
            "kline_daily_enriched",
            "close",
            "raw_close",
            _PRECISE_REL_TOLERANCE,
            "同一管线两阶段：kline_daily 原始落盘价 vs enriched 保留的不复权价",
        ),
        CrossCheck(
            "ext_valuation",
            "close",
            "price",
            _CONFLICT_REL_TOLERANCE,
            "行情源 vs 东财 push2 估值快照；同一天的收盘价/最新价应一致"
            "（快照时点差异按 0.5% 容忍）",
        ),
    ),
    "kline_etf_daily": (
        CrossCheck(
            "kline_etf_enriched",
            "close",
            "raw_close",
            _PRECISE_REL_TOLERANCE,
            "同一管线两阶段：kline_etf_daily 原始落盘价 vs enriched 的不复权价",
        ),
    ),
}


def cross_checks_for(dataset_id: str) -> tuple[CrossCheck, ...]:
    return _CROSS_CHECKS.get(dataset_id, ())


def _check_conflict(
    data_dir: Path,
    spec: DatasetSpec,
    files: list[Path],
    out: list[QualityFinding],
    counts: dict[str, int],
    limits: list[str],
) -> None:
    pairs = cross_checks_for(spec.id)
    if not pairs:
        return
    if not files:
        return
    # 本表各分区是否已定版 —— 未定版分区里的价格还会被盘后权威数据替换，
    # 此时的"两源不一致"多半只是时点差，降级并写明，避免把噪音当结论。
    settled_by_day: dict[dt.date, bool] = {}
    for path in files:
        part_day = _partition_day(path)
        if part_day is not None:
            settled_by_day[part_day] = (
                partition_settlement(path.parent, part_day) == "final"
            )
    for pair in pairs:
        try:
            left = _read(files, ["symbol", "date", pair.left_column])
        except _SchemaGapError as e:
            limits.append(f"{spec.id}: 跨源比对跳过 —— {e}")
            continue
        if left.is_empty():
            continue
        left = (
            left.with_columns(pl.col("date").cast(pl.Date, strict=False))
            .select(["symbol", "date", pl.col(pair.left_column).alias("_left")])
            .drop_nulls(["symbol", "date", "_left"])
        )
        days = [v for v in left["date"].unique().sort().to_list() if isinstance(v, dt.date)]
        if not days:
            limits.append(f"{spec.id} ↔ {pair.other_id}: 本表窗口内无有效日期，未做比对")
            continue
        right_spec = get_dataset(data_dir, pair.other_id)
        if right_spec is None:
            limits.append(f"{spec.id} ↔ {pair.other_id}: 目录中没有该数据集，未做比对")
            continue
        right_date_col = right_spec.date_column or right_spec.known_at_column or "date"
        right_files = _dataset_files(data_dir, right_spec, days)
        if not right_files:
            limits.append(f"{spec.id} ↔ {pair.other_id}: 窗口内无分区，未做比对")
            continue
        try:
            right = _read(right_files, ["symbol", right_date_col, pair.right_column])
        except _SchemaGapError as e:
            limits.append(f"{spec.id} ↔ {pair.other_id}: 跨源比对跳过 —— {e}")
            continue
        right = right.select(
            [
                pl.col("symbol"),
                pl.col(right_date_col).map_elements(_normalize_date, return_dtype=pl.Date).alias("date"),
                pl.col(pair.right_column).alias("_right"),
            ]
        ).drop_nulls(["symbol", "date", "_right"])

        joined = left.join(right, on=["symbol", "date"], how="inner").filter(
            (pl.col("_left") > 0) & (pl.col("_right") > 0)
        )
        compared = joined.height
        counts[f"conflict_{pair.other_id}_compared"] = compared
        if compared == 0:
            limits.append(f"{spec.id} ↔ {pair.other_id}: 窗口内无共同 (symbol, date)，未做比对")
            continue
        joined = joined.with_columns(
            ((pl.col("_left") - pl.col("_right")).abs() / pl.col("_left")).alias("_rel")
        )
        mismatch = joined.filter(pl.col("_rel") > pair.tolerance)
        if mismatch.is_empty():
            continue
        counts[f"conflict_{pair.other_id}_mismatch"] = mismatch.height
        examples = [
            f"{row['symbol']}@{row['date']} {row['_left']} vs {row['_right']}（差 {row['_rel']:.2%}）"
            for row in mismatch.sort("_rel", descending=True).head(3).iter_rows(named=True)
        ]
        bad_days = sorted(
            {d for d in mismatch["date"].unique().to_list() if isinstance(d, dt.date)}
        )
        unsettled_days = [d for d in bad_days if not settled_by_day.get(d, True)]
        suffix = ""
        if unsettled_days:
            suffix = (
                "；其中 "
                + "、".join(_iso(d) or "" for d in unsettled_days)
                + " 尚未定版（实时快照），差异可能来自定版前的价格，定版后需复跑"
            )
        out.append(
            QualityFinding(
                dataset_id=spec.id,
                kind=KIND_CONFLICT,
                severity=SEVERITY_LOW if unsettled_days else SEVERITY_MEDIUM,
                scope=f"{spec.id}.{pair.left_column} ↔ {pair.other_id}.{pair.right_column}",
                day=None,
                affected=mismatch.height,
                symbols=_sample(mismatch["symbol"].to_list()),
                detail=(
                    f"{mismatch.height}/{compared} 个共同 (symbol, date) 的相对偏差超过 "
                    f"{pair.tolerance:.3%}；例：{'；'.join(examples)}{suffix}"
                ),
                evidence=(pair.note,),
            )
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
#: 复权检查需要一份"kline_daily 分区布局"的 spec —— 直接取真实条目，不另建。
def kline_daily_spec(data_dir: Path) -> DatasetSpec:
    spec = get_dataset(data_dir, "kline_daily")
    if spec is None:  # pragma: no cover - 目录里必有 kline_daily
        raise LookupError("目录中缺少 kline_daily 条目")
    return spec


def check_dataset(
    data_dir: Path,
    spec: DatasetSpec,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    kinds: tuple[str, ...] | None = None,
) -> QualityReport:
    """对一个数据集跑全部（或选定的）质量检查。"""
    sessions = session_days(data_dir)
    days = sessions[-lookback_days:] if sessions else []
    files = _dataset_files(data_dir, spec, days)
    wanted = set(kinds) if kinds else set(ALL_KINDS)

    findings: list[QualityFinding] = []
    counts: dict[str, int] = {}
    limits: list[str] = []

    if KIND_MISSING in wanted:
        _check_missing(data_dir, spec, days, findings, counts, limits)
    if KIND_DUPLICATE in wanted:
        _check_duplicate(data_dir, spec, files, findings, counts, limits)
    if KIND_HALT in wanted:
        _check_halt(data_dir, spec, files, findings, counts, limits)
    if KIND_ADJUST in wanted and spec.id in ("kline_daily", "adj_factor"):
        _check_adjust(data_dir, spec.id, days, findings, counts, limits)
    if KIND_JUMP in wanted:
        # 多读一个会话，让窗口首日也能用真实昨收判定跳变
        jump_files = _dataset_files(data_dir, spec, sessions[-(lookback_days + 1):])
        _check_jump(data_dir, spec, jump_files, findings, counts, limits)
    if KIND_CONFLICT in wanted:
        _check_conflict(data_dir, spec, files, findings, counts, limits)

    findings.sort(key=lambda f: (f.kind, f.day or dt.date.min, f.scope, f.dataset_id))
    now = cn_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    return QualityReport(
        dataset_id=spec.id,
        checked_at=now.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        checked_days=tuple(d.isoformat() for d in days),
        findings=tuple(findings),
        counts=counts,
        limitations=tuple(dict.fromkeys(limits)),
    )
