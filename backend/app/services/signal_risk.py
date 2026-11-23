"""信号风险画像与统计口径 —— 「信号分数」与「胜率」是两个不同的东西。

## 为什么必须分开

用户明确要求把「信号分数」与「胜率」区分开。它们在本模块里的定义完全不同：

- **胜率（win_rate）** 是一个**概率估计**：信号触发后 H 日收益为正的比例。
  它必须带**样本数量**与**置信区间**，否则「3 笔全胜 = 100%」会被当成结论。
  这里用 Wilson score 区间 —— 小样本下比正态近似可靠得多（3/3 的下界约 0.44，
  而正态近似会给出 1.0 这种荒谬的上界）。
- **信号分数（signal_score）** 是一个**排序用的强度指标**（0-100），
  由效应量、可靠性（区间下界）、样本充足度、回撤惩罚四项合成。
  **它不是概率，也不能读成「有 82% 的概率会涨」。**

两者并列展示时，分数负责「哪条信号值得先看」，胜率负责「历史上多常成立」，
样本量与区间负责「这个胜率能不能信」。

## 诚实边界

- 历史统计基于本地 enriched 历史缓存；缓存未覆盖的区间**不推断**，直接记 `limitations`。
- 样本不足（默认 < 20）时照常给出胜率与区间，但 `verdict` 标注 `insufficient`，
  且分数不因小样本的高胜率而抬高（可靠性项会自动压低）。
- 前瞻收益用**不复权收盘价**（`raw_close`），避免复权口径把跨除权日的收益算错。
- 潜在回撤取历史前瞻收益的**最差分位**（默认 5%），不是「未来一定不会超过」的保证。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import polars as pl

from app.backtest.stats_v2 import _normal_ppf

logger = logging.getLogger(__name__)

#: 低于这个样本数时，胜率的区间会宽到不足以支撑结论。
MIN_TRUSTED_SAMPLES = 20

#: 潜在回撤取前瞻收益的这个分位（越小越保守）。
DRAWDOWN_PERCENTILE = 5.0

#: 流动性分档阈值（日均成交额，元）。低于 MIN_LIQUIDITY_CNY 视为不适合重仓。
LIQUIDITY_HIGH_CNY = 1_000_000_000.0    # 10 亿
LIQUIDITY_MID_CNY = 100_000_000.0       # 1 亿
MIN_LIQUIDITY_CNY = 20_000_000.0        # 2000 万

#: 单笔风险预算（占组合权益的比例）。仓位上限由它和回撤一起推导。
DEFAULT_RISK_PER_TRADE_PCT = 1.5

#: 置信水平。
CONFIDENCE_LEVEL = 0.95

VERDICT_TRUSTED = "trusted"          # 样本充足，区间可读
VERDICT_INSUFFICIENT = "insufficient"  # 样本不足，仅作参考
VERDICT_NO_SAMPLES = "no_samples"    # 完全没有样本


# ---------------------------------------------------------------------------
# 统计基元
# ---------------------------------------------------------------------------
def wilson_interval(wins: int, total: int, *, confidence: float = CONFIDENCE_LEVEL) -> tuple[float, float]:
    """二项比例的 Wilson score 置信区间。

    选 Wilson 而不是正态近似：样本小时正态近似会给出越界值（0/0 或 1/1 处
    区间宽度为 0，给人「确定」的错觉）。Wilson 在 0 和 1 附近仍然给出非退化区间。

    返回 (下界, 上界)，均在 [0, 1]。
    """
    if total <= 0:
        return (0.0, 1.0)
    z = _normal_ppf(0.5 + confidence / 2.0)
    p = wins / total
    denom = 1.0 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, center - margin), min(1.0, center + margin))


def _quantile(values: list[float], q: float) -> float | None:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    if not finite:
        return None
    finite.sort()
    if len(finite) == 1:
        return finite[0]
    pos = (len(finite) - 1) * q / 100.0
    low = math.floor(pos)
    high = min(low + 1, len(finite) - 1)
    weight = pos - low
    return finite[low] * (1 - weight) + finite[high] * weight


def liquidity_bucket(median_amount_cny: float | None) -> str:
    """日均成交额 → 流动性档位（high / mid / low / unknown）。"""
    if median_amount_cny is None or not math.isfinite(median_amount_cny) or median_amount_cny <= 0:
        return "unknown"
    if median_amount_cny >= LIQUIDITY_HIGH_CNY:
        return "high"
    if median_amount_cny >= LIQUIDITY_MID_CNY:
        return "mid"
    return "low"


def position_cap_pct(
    *,
    declared_cap: float | None,
    potential_drawdown: float | None,
    liquidity: str,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
) -> tuple[float, str]:
    """推导单信号仓位上限（占组合权益的百分比），并给出推导依据。

    逻辑：先按「单笔风险预算 ÷ 潜在回撤」算出风险能容纳的仓位，再按流动性打折扣，
    最后与用户显式声明的上限取较小值。任何一步缺失都退回更保守的默认，
    并把依据写进返回的第二项，避免"数字有了但不知道怎么来的"。
    """
    parts: list[str] = []
    if potential_drawdown is not None and potential_drawdown < 0:
        # 仓位上限 = 单笔风险预算 ÷ 潜在回撤。两者都是「占自身价值的比例」，
        # 相除即得出「该仓位最多能占组合权益的多少」（1.5% ÷ 5% = 30%），
        # 所以这里不能再乘 100（那样会得到 3000%，被钳制后所有档位都变成同一个值）。
        risk_cap = risk_per_trade_pct / (abs(potential_drawdown) * 100.0) * 100.0
        parts.append(f"风险预算 {risk_per_trade_pct:.1f}% ÷ 潜在回撤 {abs(potential_drawdown):.1%}")
    else:
        risk_cap = 10.0
        parts.append("无回撤样本 → 默认 10%")
    cap = risk_cap
    liquidity_factor = {"high": 1.0, "mid": 0.6, "low": 0.25}.get(liquidity, 0.4)
    if liquidity != "high":
        parts.append(f"流动性 {liquidity} → ×{liquidity_factor:g}")
    cap *= liquidity_factor
    if declared_cap is not None and declared_cap > 0:
        if declared_cap < cap:
            parts.append(f"取用户声明上限 {declared_cap:.1f}%")
        cap = min(cap, declared_cap)
    cap = max(0.5, min(30.0, cap))
    parts.append(f"钳制到 0.5~30% → {cap:.1f}%")
    return round(cap, 1), "；".join(parts)


def signal_score(
    *,
    win_rate: float | None,
    ci_low: float | None,
    excess_mean: float | None,
    samples: int,
    potential_drawdown: float | None,
) -> int:
    """合成 0-100 的信号分数。

    四项加权：效应量（超额收益，40分）＋ 可靠性（胜率区间下界，25分）＋
    样本充足度（20分）＋ 回撤惩罚（15分，回撤越大扣得越多）。

    **这是排序指标，不是概率**。样本不足时可靠性项自动被压低 ——
    3 笔全胜不会得到高分，因为 `ci_low` 只有约 0.44。
    """
    score = 0.0
    if excess_mean is not None and math.isfinite(excess_mean):
        # ±5% 超额打满 / 归零
        score += 40.0 * max(0.0, min(1.0, excess_mean / 0.05))
    if ci_low is not None and math.isfinite(ci_low):
        # 胜率区间下界 0.5（抛硬币）→ 0 分，0.75 → 满分
        score += 25.0 * max(0.0, min(1.0, (ci_low - 0.5) / 0.25))
    score += 20.0 * min(1.0, samples / 100.0)
    if potential_drawdown is not None and math.isfinite(potential_drawdown):
        # 回撤 0 扣 0，回撤 -20% 扣满
        score += 15.0 * max(0.0, 1.0 - abs(potential_drawdown) / 0.20)
    else:
        score += 7.5  # 无回撤样本时给一半，不惩罚也不奖励
    return round(max(0.0, min(100.0, score)))


# ---------------------------------------------------------------------------
# 单信号历史统计
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SignalStats:
    """一个信号在本地历史上的实测统计。"""

    signal_id: str
    column: str
    horizon_days: int
    samples: int
    win_rate: float | None
    ci_low: float | None
    ci_high: float | None
    excess_mean: float | None          # 平均超额收益（相对全市场中位）
    excess_median: float | None
    potential_drawdown: float | None   # 前瞻收益的低分位
    best: float | None
    worst: float | None
    avg_holding_days: int
    liquidity: str
    median_amount_cny: float | None
    signal_score: int
    position_cap_pct: float
    position_cap_basis: str
    verdict: str
    checked_from: str | None
    checked_to: str | None
    limitations: tuple[str, ...] = ()
    reason: str = ""
    falsify: str = ""
    horizon_basis: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "column": self.column,
            "horizon_days": self.horizon_days,
            "samples": self.samples,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "confidence_interval": {
                "level": CONFIDENCE_LEVEL,
                "low": None if self.ci_low is None else round(self.ci_low, 4),
                "high": None if self.ci_high is None else round(self.ci_high, 4),
                "method": "wilson",
            },
            "excess_mean": None if self.excess_mean is None else round(self.excess_mean, 4),
            "excess_median": None if self.excess_median is None else round(self.excess_median, 4),
            "potential_drawdown": (
                None if self.potential_drawdown is None else round(self.potential_drawdown, 4)
            ),
            "best": None if self.best is None else round(self.best, 4),
            "worst": None if self.worst is None else round(self.worst, 4),
            "expected_holding_days": self.avg_holding_days,
            "liquidity": {
                "bucket": self.liquidity,
                "median_amount_cny": (
                    None if self.median_amount_cny is None else round(self.median_amount_cny, 0)
                ),
            },
            # 「分数」与「胜率」在这里并列且有各自的说明，避免被当成同一个东西。
            "signal_score": {
                "value": self.signal_score,
                "meaning": "排序用强度指标（0-100），不是概率",
                "components": ["效应量 40", "胜率区间下界 25", "样本充足度 20", "回撤惩罚 15"],
            },
            "position_cap": {"pct": self.position_cap_pct, "basis": self.position_cap_basis},
            "verdict": self.verdict,
            "checked_window": {"from": self.checked_from, "to": self.checked_to},
            "reason": self.reason,
            "falsify": self.falsify,
            "horizon_basis": self.horizon_basis,
            "limitations": list(self.limitations),
        }


def _select_history(history: pl.DataFrame, *, keep: str) -> tuple[pl.DataFrame, list[str]]:
    """挑出计算所需列（外加上信号列本身），并把缺失情况记成 limitation。"""
    limits: list[str] = []
    wanted = ["symbol", "date", "close", "raw_close", "amount"]
    present = [c for c in wanted if c in history.columns]
    for column in wanted:
        if column not in history.columns:
            limits.append(f"历史帧缺少列 {column}")
    # 信号列必须保留 —— 只 select 行情列会把被评估的列一起丢掉。
    if keep in history.columns:
        present.append(keep)
    frame = history.select(present)
    if "raw_close" not in frame.columns and "close" in frame.columns:
        frame = frame.with_columns(pl.col("close").alias("raw_close"))
        limits.append("缺少 raw_close，用 close 替代（跨除权日收益可能不准）")
    return frame, limits


def compute_signal_stats(
    *,
    signal_id: str,
    column: str,
    history: pl.DataFrame,
    horizon_days: int = 5,
    reason: str = "",
    falsify: str = "",
    declared_cap: float | None = None,
    min_samples: int = MIN_TRUSTED_SAMPLES,
) -> SignalStats:
    """在历史帧上评估一条信号，产出带区间与样本量的统计。"""
    limits: list[str] = []
    frame, select_limits = _select_history(history, keep=column)
    limits.extend(select_limits)

    empty = SignalStats(
        signal_id=signal_id, column=column, horizon_days=horizon_days, samples=0,
        win_rate=None, ci_low=None, ci_high=None, excess_mean=None, excess_median=None,
        potential_drawdown=None, best=None, worst=None, avg_holding_days=horizon_days,
        liquidity="unknown", median_amount_cny=None, signal_score=0,
        position_cap_pct=position_cap_pct(
            declared_cap=declared_cap, potential_drawdown=None, liquidity="unknown"
        )[0],
        position_cap_basis=position_cap_pct(
            declared_cap=declared_cap, potential_drawdown=None, liquidity="unknown"
        )[1],
        verdict=VERDICT_NO_SAMPLES, checked_from=None, checked_to=None,
        limitations=tuple(limits), reason=reason, falsify=falsify,
        horizon_basis=f"固定 {horizon_days} 个交易日",
    )
    if frame.is_empty() or column not in history.columns:
        return empty

    try:
        frame = (
            frame.with_columns(pl.col(column).cast(pl.Boolean, strict=False).fill_null(False).alias("_hit"))
            .sort(["symbol", "date"])
        )
    except Exception as exc:  # noqa: BLE001
        limits.append(f"信号列 {column} 无法转为布尔: {exc}")
        return replace(empty, limitations=tuple(limits))

    # 前瞻收益：同 symbol 内往后挪 H 行（交易日语义，不用自然日）。
    frame = frame.with_columns(
        (pl.col("raw_close").shift(-horizon_days).over("symbol") / pl.col("raw_close") - 1.0)
        .alias("_fwd")
    )
    # 全市场同日中位收益作为基准（等权近似），超额 = 个股 - 中位。
    market = (
        frame.filter(pl.col("raw_close").is_not_null())
        .with_columns(
            (pl.col("raw_close").shift(-1).over("symbol") / pl.col("raw_close") - 1.0).alias("_d1")
        )
        .group_by("date")
        .agg(pl.col("_d1").median().alias("_mkt_d1"))
        .sort("date")
        .with_columns(pl.col("_mkt_d1").fill_null(0.0).cum_sum().alias("_mkt_cum"))
    )
    joined = frame.join(market.select(["date", "_mkt_cum"]), on="date", how="left").sort(
        ["symbol", "date"]
    )
    joined = joined.with_columns(
        (pl.col("_mkt_cum").shift(-horizon_days).over("symbol") - pl.col("_mkt_cum") + 1.0)
        .alias("_mkt_fwd")
    )
    joined = joined.with_columns((pl.col("_fwd") - (pl.col("_mkt_fwd") - 1.0)).alias("_excess"))

    hits = joined.filter(pl.col("_hit") & pl.col("_fwd").is_not_null())
    samples = hits.height
    if samples == 0:
        limits.append("该信号在本地历史窗口内没有可评估的触发样本")
        # 复用 empty 模板，但要把上面新增的 limitation 一起带上 ——
        # 直接 return empty 会把"为什么没有样本"这条原因丢掉。
        return replace(empty, limitations=tuple(limits))

    fwd_values = [float(v) for v in hits["_fwd"].to_list() if v is not None]
    excess_values = [
        float(v) for v in hits["_excess"].to_list() if v is not None and math.isfinite(v)
    ]
    wins = sum(1 for v in fwd_values if v > 0)
    win_rate = wins / samples
    ci_low, ci_high = wilson_interval(wins, samples)
    excess_mean = sum(excess_values) / len(excess_values) if excess_values else None
    excess_median = _quantile(excess_values, 50.0)
    potential = _quantile(fwd_values, DRAWDOWN_PERCENTILE)
    best = max(fwd_values) if fwd_values else None
    worst = min(fwd_values) if fwd_values else None

    median_amount: float | None = None
    if "amount" in hits.columns:
        amounts = [float(v) for v in hits["amount"].to_list() if v is not None and v > 0]
        median_amount = _quantile(amounts, 50.0) if amounts else None
    liquidity = liquidity_bucket(median_amount)

    cap, cap_basis = position_cap_pct(
        declared_cap=declared_cap, potential_drawdown=potential, liquidity=liquidity
    )
    score = signal_score(
        win_rate=win_rate, ci_low=ci_low, excess_mean=excess_mean,
        samples=samples, potential_drawdown=potential,
    )
    verdict = VERDICT_TRUSTED if samples >= min_samples else VERDICT_INSUFFICIENT
    if verdict == VERDICT_INSUFFICIENT:
        limits.append(
            f"样本 {samples} < {min_samples}：胜率区间过宽，结论仅作参考，分数不会因小样本高胜率抬高"
        )

    dates = hits["date"].drop_nulls()
    return SignalStats(
        signal_id=signal_id,
        column=column,
        horizon_days=horizon_days,
        samples=samples,
        win_rate=win_rate,
        ci_low=ci_low,
        ci_high=ci_high,
        excess_mean=excess_mean,
        excess_median=excess_median,
        potential_drawdown=potential,
        best=best,
        worst=worst,
        avg_holding_days=horizon_days,
        liquidity=liquidity,
        median_amount_cny=median_amount,
        signal_score=score,
        position_cap_pct=cap,
        position_cap_basis=cap_basis,
        verdict=verdict,
        checked_from=dates.min().isoformat() if not dates.is_empty() else None,
        checked_to=dates.max().isoformat() if not dates.is_empty() else None,
        limitations=tuple(limits),
        reason=reason,
        falsify=falsify,
        horizon_basis=f"固定 {horizon_days} 个交易日（信号自身未声明持有周期时的默认）",
    )


def load_enriched_history(
    data_dir: Path, *, lookback_days: int, with_signals: bool = True
) -> pl.DataFrame:
    """读取用于信号统计的 enriched 历史帧，并（默认）补齐信号列。

    为什么需要补信号列：`ENRICHED_STORAGE_COLS` 只落 15 列基础行情，**指标列与
    `csg_*` 信号列都不落盘**（由 indicators/pipeline.py 运行时计算）。所以直接读
    parquet 得到的帧里没有任何信号列 —— 拿它评估信号会全部报「没有样本」。

    做法：按需在本地计算指标 + 信号列（`compute_indicators` + `compute_signals`），
    并且**只算这一份帧**，不碰任何落盘数据。计算失败时退回不含信号列的原帧，
    由调用方把原因记进 limitations。

    另外 `kline_daily_enriched` 只含股票；ETF/指数在各自的 enriched 目录，
    这里会把三者按需合并，否则自选股里的 ETF 会整批落到「没有行业/没有行情」。
    """
    frames: list[pl.DataFrame] = []
    for name in ("kline_daily_enriched", "kline_etf_enriched", "kline_index_enriched"):
        base = data_dir / name
        if not base.is_dir():
            continue
        parts = sorted(base.glob("date=*"))
        if not parts:
            continue
        keep = [p for p in parts[-lookback_days:]] if lookback_days > 0 else parts
        for part in keep:
            path = part / "part.parquet"
            if not path.is_file():
                continue
            try:
                frames.append(pl.read_parquet(path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("信号统计：分区读取失败 %s: %s", path, exc)
    if not frames:
        logger.warning("信号统计：enriched 目录为空 %s", data_dir)
        return pl.DataFrame()
    try:
        history = pl.concat(frames, how="diagonal_relaxed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("信号统计：历史帧拼接失败: %s", exc)
        return pl.DataFrame()

    if not with_signals:
        return history
    return _attach_computed_signals(history, data_dir)


def _attach_computed_signals(history: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    """在不落盘的前提下补齐指标与信号列。"""
    required = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume")
                if c not in history.columns]
    if required:
        logger.warning("信号统计：历史帧缺少计算指标所需列 %s，无法补算信号", required)
        return history
    try:
        from app.indicators.pipeline import compute_indicators, compute_signals

        frame = compute_indicators(history)
        frame = compute_signals(frame)
        return frame
    except Exception as exc:  # noqa: BLE001 — 补算失败不应让整个统计崩掉
        logger.warning("信号统计：指标/信号列补算失败，信号统计将为空: %s", exc)
        return history


def latest_session(data_dir: Path) -> dt.date | None:
    from app.services.dataset_catalog import session_anchor

    return session_anchor(data_dir)


__all__ = [
    "MIN_TRUSTED_SAMPLES",
    "SignalStats",
    "compute_signal_stats",
    "liquidity_bucket",
    "load_enriched_history",
    "position_cap_pct",
    "signal_score",
    "wilson_interval",
]
