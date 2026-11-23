"""个股诊断算法 —— K 线性质/趋势/指标/筹码/关键位/历史同形态。

单一实现, 两个消费方:
- ``scripts/analyze_stock.py``: 命令行分析;
- ``services/action_card.py``: 行动卡的「事实」与「系统推断」两段。

为什么不放进 indicators/pipeline: 那些是**全市场批量**物化列 (逐日重算、
落盘、供策略消费); 这里是**单标的按需诊断** (读 512 根日线现算、不落盘、
供人读)。两者精度要求相同但成本模型不同, 硬合并会让批量管道变慢或让
按需诊断变粗糙。

口径红线:
- 所有指标都是**收盘价口径** (前复权), 与策略/enriched 一致;
- 价格档位 (关键位/筹码) 也按前复权算 —— 它们只用于"相对位置"判断,
  不参与成交;
- 同形态统计必须**按当日实际涨跌方向**匹配, 不允许写死"上涨+缩量"
  (否则拿"涨"的历史去预测一只"跌"的票, 结论会完全反掉);
- 胜率必须与**无条件基准**一起给出 —— 没有基准的胜率是骗人的;
- 样本量 < 30 必须显式警告。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

# 分析参数 (与 docs/个股分析标准框架.md 一致)
CHIP_DAYS = 60
CHIP_BINS = 12
ANALOG_MIN_BARS = 60
FORWARD_WINDOWS = (1, 3, 5, 10)
BASELINE_DAYS = 250
MIN_BARS_REQUIRED = 60


# ── 基础指标 (标准公式) ──────────────────────────────────

def ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty(len(values), dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        out[i] = values[i - window + 1:i + 1].mean()
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    delta = np.diff(values, prepend=values[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    out = np.full(len(values), np.nan)
    if len(values) <= period:
        return out
    avg_gain = gain[1:period + 1].mean()
    avg_loss = loss[1:period + 1].mean()
    for i in range(period, len(values)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gain[i]) / period
            avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    out = np.full(len(close), np.nan)
    for i in range(period - 1, len(close)):
        out[i] = true_range[i - period + 1:i + 1].mean()
    return out


def macd(close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    return dif, dea, (dif - dea) * 2.0


def bollinger(close: np.ndarray, window: int = 20, k: float = 2.0):
    mid = moving_average(close, window)
    std = np.full(len(close), np.nan)
    for i in range(window - 1, len(close)):
        std[i] = close[i - window + 1:i + 1].std(ddof=0)
    return mid + k * std, mid, mid - k * std


# ── 各分析块 ────────────────────────────────────────────

def candle_profile(df: pl.DataFrame) -> dict[str, Any]:
    """今日 K 线性质 —— 涨跌幅之外, 这根线是谁买的。"""
    o = float(df["open"][-1])
    h = float(df["high"][-1])
    low_px = float(df["low"][-1])
    c = float(df["close"][-1])
    prev_close = float(df["close"][-2])
    volume = df["volume"].to_numpy().astype(float)
    vol_ma20 = float(volume[-20:].mean()) if len(volume) >= 20 else float(volume.mean())
    span = max(h - low_px, 1e-9)
    ratio = volume[-1] / vol_ma20 if vol_ma20 else None

    if ratio is None:
        vol_state = "unknown"
    elif ratio < 0.7:
        vol_state = "shrink"       # 缩量
    elif ratio > 1.5:
        vol_state = "expand"       # 放量
    else:
        vol_state = "normal"       # 常量

    return {
        "date": str(df["date"][-1]),
        "open": round(o, 2),
        "high": round(h, 2),
        "low": round(low_px, 2),
        "close": round(c, 2),
        "change_pct": round((c / prev_close - 1) * 100, 2),
        "amplitude_pct": round(span / prev_close * 100, 2),
        "body_ratio": round(abs(c - o) / span * 100, 1),
        "close_position": round((c - low_px) / span * 100, 1),
        "upper_shadow": round(h - c, 2),
        "lower_shadow": round(o - low_px, 2),
        "volume": int(volume[-1]),
        "volume_ma20": int(vol_ma20),
        "volume_ratio": round(ratio * 100, 1) if ratio is not None else None,
        "is_up": c >= o,
        "volume_state": vol_state,
    }


def trend_structure(close: np.ndarray) -> dict[str, Any]:
    """趋势结构 —— 三条均线定性质。"""
    c = float(close[-1])
    mas: dict[str, float] = {}
    for n in (5, 10, 20, 60, 120):
        if len(close) >= n:
            mas[f"MA{n}"] = round(float(moving_average(close, n)[-1]), 2)
    deviation = {k: round((c / v - 1) * 100, 2) for k, v in mas.items() if v == v}

    ma20, ma60, ma120 = mas.get("MA20"), mas.get("MA60"), mas.get("MA120")
    if ma60 and ma120 and c > ma60 > ma120:
        structure = "uptrend"                    # 中期上升趋势
    elif ma60 and ma20 and c > ma60 and c < ma20:
        structure = "uptrend_pullback"           # 上升趋势中的调整
    elif ma60 and ma120 and c < ma60 < ma120:
        structure = "downtrend"                  # 中期下降趋势
    else:
        structure = "unclear"                    # 震荡/不明确

    window = min(len(close), 250)
    seg = close[-window:]
    lo, hi = float(seg.min()), float(seg.max())

    # 长期均线是否多头排列 (MA60 > MA120)
    ma60_up = bool(ma60 and ma120 and ma60 > ma120)
    return {
        "close": round(c, 2),
        "mas": mas,
        "deviation_pct": deviation,
        "structure": structure,
        "ma60_above_ma120": ma60_up,
        "high_250": round(hi, 2),
        "low_250": round(lo, 2),
        "position_in_250_pct": round((c - lo) / max(hi - lo, 1e-9) * 100, 1),
    }


def indicators(df: pl.DataFrame) -> dict[str, Any]:
    """四个指标 —— 只选四个, 每个都要能回答一个问题。"""
    close = df["close"].to_numpy().astype(float)
    high = df["high"].to_numpy().astype(float)
    low = df["low"].to_numpy().astype(float)
    dif, dea, hist = macd(close)
    up, mid, down = bollinger(close)
    atr14 = atr(high, low, close)
    rsi14 = rsi(close)
    prev_hist = float(hist[-2]) if len(hist) > 1 else float(hist[-1])
    ret = np.diff(np.log(close)) if len(close) > 1 else np.array([0.0])
    return {
        "rsi14": round(float(rsi14[-1]), 1),
        "rsi14_prev": round(float(rsi14[-2]), 1) if len(rsi14) > 1 else None,
        "macd_dif": round(float(dif[-1]), 2),
        "macd_dea": round(float(dea[-1]), 2),
        "macd_hist": round(float(hist[-1]), 2),
        "macd_hist_prev": round(prev_hist, 2),
        "macd_hist_expanding": abs(float(hist[-1])) > abs(prev_hist),
        "boll_upper": round(float(up[-1]), 2),
        "boll_mid": round(float(mid[-1]), 2),
        "boll_lower": round(float(down[-1]), 2),
        "atr14": round(float(atr14[-1]), 2),
        "atr14_pct": round(float(atr14[-1]) / close[-1] * 100, 2),
        "vol_20d_annual": round(float(ret[-20:].std() * np.sqrt(252) * 100), 1) if len(ret) >= 20 else None,
        "vol_60d_annual": round(float(ret[-60:].std() * np.sqrt(252) * 100), 1) if len(ret) >= 60 else None,
    }


def chips(df: pl.DataFrame, days: int = CHIP_DAYS, bins: int = CHIP_BINS) -> dict[str, Any]:
    """筹码分布 —— 上方最密=阻力, 下方最密=支撑。"""
    close = df["close"].to_numpy().astype(float)[-days:]
    volume = df["volume"].to_numpy().astype(float)[-days:]
    lo, hi = float(close.min()), float(close.max())
    if hi <= lo:
        return {"window": days, "bins": [], "densest_above": None, "densest_below": None,
                "limitation": "区间过窄无法分档"}
    edges = np.linspace(lo, hi, bins + 1)
    bucket = np.clip(np.digitize(close, edges) - 1, 0, bins - 1)
    totals = np.zeros(bins)
    for idx, vol in zip(bucket, volume, strict=True):
        totals[idx] += vol
    total = float(totals.sum()) or 1.0

    rows = [
        {
            "low": round(float(edges[i]), 2),
            "high": round(float(edges[i + 1]), 2),
            "pct": round(float(totals[i] / total * 100), 1),
        }
        for i in range(bins)
    ]
    current = float(close[-1])
    above = [r for r in rows if r["low"] >= current]
    below = [r for r in rows if r["high"] <= current]

    def summarize(rows_subset: list[dict]) -> dict | None:
        if not rows_subset:
            return None
        densest = max(rows_subset, key=lambda r: r["pct"])
        return {
            "low": densest["low"],
            "high": densest["high"],
            "pct": densest["pct"],
            # 该方向上「套牢/获利」筹码总量 —— 决定要消化多少
            "total_pct": round(sum(r["pct"] for r in rows_subset), 1),
        }

    return {
        "window": days,
        "bins": rows,
        "densest_above": summarize(above),
        "densest_below": summarize(below),
    }


def key_levels(df: pl.DataFrame) -> dict[str, Any]:
    """关键位 —— 均线之外再给摆动点与缺口。"""
    close = df["close"].to_numpy().astype(float)
    high = df["high"].to_numpy().astype(float)
    low = df["low"].to_numpy().astype(float)
    current = float(close[-1])

    pivots_high, pivots_low = [], []
    for i in range(5, len(close) - 5):
        if high[i] == high[i - 5:i + 6].max():
            pivots_high.append(round(float(high[i]), 2))
        if low[i] == low[i - 5:i + 6].min():
            pivots_low.append(round(float(low[i]), 2))

    gaps = []
    for i in range(max(1, len(close) - 30), len(close)):
        if low[i] > high[i - 1]:
            gaps.append({"date": str(df["date"][i]), "type": "up",
                         "from": round(float(high[i - 1]), 2), "to": round(float(low[i]), 2)})
        elif high[i] < low[i - 1]:
            gaps.append({"date": str(df["date"][i]), "type": "down",
                         "from": round(float(low[i - 1]), 2), "to": round(float(high[i]), 2)})

    def with_distance(price: float) -> dict[str, float]:
        return {
            "price": round(price, 2),
            "distance_pct": round((current / price - 1) * 100, 2),
        }

    return {
        "pivots_high_recent": pivots_high[-6:],
        "pivots_low_recent": pivots_low[-6:],
        "high_20": with_distance(float(high[-20:].max())),
        "low_20": with_distance(float(low[-20:].min())),
        "high_60": with_distance(float(high[-60:].max())),
        "low_60": with_distance(float(low[-60:].min())),
        "gaps_30d": gaps,
    }


def analog_stats(df: pl.DataFrame) -> dict[str, Any]:
    """历史同形态统计 + 无条件基准。

    形态由**当日实际 K 线**推导 (方向 + 量能档), 不写死。样本 < 30 显式警告。
    """
    close = df["close"].to_numpy().astype(float)
    volume = df["volume"].to_numpy().astype(float)
    dates = df["date"].to_list()
    n = len(close)
    vol_ma20 = moving_average(volume, 20)

    today_up = bool(close[-1] >= close[-2])
    today_ratio = (
        float(volume[-1] / vol_ma20[-1]) if np.isfinite(vol_ma20[-1]) and vol_ma20[-1] > 0 else 1.0
    )
    if today_ratio < 0.7:
        vol_bucket = "shrink"
    elif today_ratio > 1.5:
        vol_bucket = "expand"
    else:
        vol_bucket = "normal"

    def same_volume(r: float) -> bool:
        if vol_bucket == "shrink":
            return r < 0.7
        if vol_bucket == "expand":
            return r > 1.5
        return 0.7 <= r <= 1.5

    cases: list[dict] = []
    for i in range(ANALOG_MIN_BARS, n - max(FORWARD_WINDOWS)):
        if not np.isfinite(vol_ma20[i]) or vol_ma20[i] <= 0:
            continue
        if today_up:
            if not (close[i - 1] < close[i - 2] and close[i] > close[i - 1]):
                continue
        else:
            if not (close[i - 1] > close[i - 2] and close[i] < close[i - 1]):
                continue
        if not same_volume(float(volume[i] / vol_ma20[i])):
            continue
        fwd = {w: (close[i + w] / close[i] - 1) * 100 for w in FORWARD_WINDOWS if i + w < n}
        if len(fwd) == len(FORWARD_WINDOWS):
            cases.append({"date": str(dates[i]), "forward": {str(k): round(v, 2) for k, v in fwd.items()}})

    def summarize(sample: list[dict], window: int) -> dict[str, Any]:
        vals = np.array([item["forward"][str(window)] for item in sample])
        if not vals.size:
            return {}
        return {
            "mean_pct": round(float(vals.mean()), 2),
            "median_pct": round(float(np.median(vals)), 2),
            "win_rate": round(float((vals > 0).mean() * 100)),
            "std_pct": round(float(vals.std()), 2),
            "samples": int(vals.size),
        }

    baseline: list[dict] = []
    start = max(0, n - BASELINE_DAYS)
    for i in range(start, n - max(FORWARD_WINDOWS)):
        fwd = {w: (close[i + w] / close[i] - 1) * 100 for w in FORWARD_WINDOWS if i + w < n}
        if len(fwd) == len(FORWARD_WINDOWS):
            baseline.append({"date": str(dates[i]), "forward": {str(k): v for k, v in fwd.items()}})

    direction_text = "前一日跌 + 当日涨" if today_up else "前一日涨 + 当日跌"
    vol_text = {"shrink": "缩量(<70%均量)", "expand": "放量(>150%均量)", "normal": "常量(70~150%均量)"}[vol_bucket]

    warnings: list[str] = []
    if not cases:
        warnings.append(
            "该形态在本标的历史上无匹配样本 —— 不能给出胜率, 也不得借用其他形态的胜率"
        )
    elif len(cases) < 30:
        warnings.append(
            f"样本仅 {len(cases)} 例 (少于 30), 胜率置信区间很宽; 不得把胜率当确定概率使用"
        )
    else:
        warnings.append(f"样本 {len(cases)} 例; 仅为单只股票的历史, 不代表全市场规律")

    return {
        "pattern": f"{direction_text} + {vol_text}",
        "today_is_up": today_up,
        "today_volume_ratio": round(today_ratio, 2),
        "sample_size": len(cases),
        "pattern_stats": {str(w): summarize(cases, w) for w in FORWARD_WINDOWS},
        "baseline_sample_size": len(baseline),
        "baseline_stats": {str(w): summarize(baseline, w) for w in FORWARD_WINDOWS},
        "recent_cases": cases[-5:],
        "warnings": warnings,
    }


# ── 强弱判定 (可解释, 不用黑箱分数) ──────────────────────

@dataclass(frozen=True)
class StrengthVerdict:
    """方向判定 + 支撑它/反驳它的具体证据。

    刻意不用加权总分: 总分不可解释, 而行动卡必须能回答"凭什么说它强"。
    这里给的是**投票** (几票看多、几票看空) 与其逐条理由, 用户自己判断权重。
    """

    stance: str                     # strong | weak | unclear
    label: str
    bullish: list[str] = field(default_factory=list)
    bearish: list[str] = field(default_factory=list)
    neutral: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "label": self.label,
            "bullish_evidence": self.bullish,
            "bearish_evidence": self.bearish,
            "neutral_evidence": self.neutral,
            "note": "证据投票, 不是加权评分 —— 每条理由都可独立核对",
        }


def judge_strength(trend: dict, ind: dict, candle: dict, chip: dict) -> StrengthVerdict:
    """偏强 / 偏弱 / 方向不明 —— 只做可解释的投票。"""
    bullish: list[str] = []
    bearish: list[str] = []
    neutral: list[str] = []


    dev = trend["deviation_pct"]
    structure = trend["structure"]

    # 1. 结构
    if structure == "uptrend":
        bullish.append("价在 MA60/MA120 之上且 MA60>MA120（中期上升趋势）")
    elif structure == "uptrend_pullback":
        bullish.append(f"价仍在 MA60 上方 {dev.get('MA60', 0):+.2f}%（趋势未坏）")
        bearish.append(f"价在 MA20 下方 {dev.get('MA20', 0):+.2f}%（短期走弱）")
    elif structure == "downtrend":
        bearish.append(f"价在 MA60 下方 {dev.get('MA60', 0):+.2f}%、MA120 下方 {dev.get('MA120', 0):+.2f}%（中期下降趋势）")
    else:
        neutral.append("均线纠缠，趋势方向不明确")

    # 2. 动能
    if ind["macd_hist"] > 0:
        bullish.append(f"MACD 柱为正（{ind['macd_hist']}），多头动能")
    else:
        state = "扩张" if ind["macd_hist_expanding"] else "收敛"
        bearish.append(f"MACD 柱为负（{ind['macd_hist']}，{state}），空头动能")

    # 3. RSI 极值
    rsi_v = ind["rsi14"]
    if rsi_v >= 70:
        bearish.append(f"RSI14 = {rsi_v}，超买区，短期回调风险")
    elif rsi_v <= 30:
        bullish.append(f"RSI14 = {rsi_v}，超卖区，存在反弹条件")
    else:
        neutral.append(f"RSI14 = {rsi_v}，中性区间（无明显超买超卖）")

    # 4. 量价配合
    ratio = candle.get("volume_ratio")
    if ratio is not None:
        if candle["is_up"] and candle["volume_state"] == "expand":
            bullish.append(f"放量上涨（量比 {ratio:.0f}%），资金推动")
        elif candle["is_up"] and candle["volume_state"] == "shrink":
            if trend["position_in_250_pct"] < 30:
                bullish.append(f"低位缩量上涨（量比 {ratio:.0f}%），抛压枯竭")
            else:
                neutral.append(f"缩量上涨（量比 {ratio:.0f}%），买盘不强")
        elif not candle["is_up"] and candle["volume_state"] == "shrink":
            bearish.append(f"缩量下跌（量比 {ratio:.0f}%），买盘消失而非抛压释放")
        elif not candle["is_up"] and candle["volume_state"] == "expand":
            bearish.append(f"放量下跌（量比 {ratio:.0f}%），抛压沉重")

    # 5. 收盘位置
    pos = candle["close_position"]
    if pos >= 80:
        bullish.append(f"收盘位于全日 {pos:.0f}% 分位（买盘承接到底）")
    elif pos <= 20:
        bearish.append(f"收盘位于全日 {pos:.0f}% 分位（尾盘承接不足）")

    # 6. 位置
    pct = trend["position_in_250_pct"]
    if pct >= 75:
        neutral.append(f"处于 250 日区间 {pct:.0f}% 分位（偏高，向上空间有限）")
    elif pct <= 25:
        neutral.append(f"处于 250 日区间 {pct:.0f}% 分位（偏低，但低位不等于便宜）")

    # 7. 头顶筹码
    above = chip.get("densest_above")
    if above and above["total_pct"] >= 25:
        bearish.append(
            f"上方 {above['low']:.2f}~{above['high']:.2f} 堆积 {above['total_pct']:.0f}% 筹码，反弹需消化套牢盘"
        )

    net = len(bullish) - len(bearish)
    if net >= 2:
        stance, label = "strong", "偏强"
    elif net <= -2:
        stance, label = "weak", "偏弱"
    else:
        stance, label = "unclear", "方向不明"

    return StrengthVerdict(
        stance=stance, label=label,
        bullish=bullish, bearish=bearish, neutral=neutral,
    )


# ── 汇总 ────────────────────────────────────────────────

def diagnose(df: pl.DataFrame) -> dict[str, Any]:
    """一次算出行动卡需要的全部诊断数据。

    ``df`` 需含 symbol/date/open/high/low/close/volume (按 date 升序)。
    """
    if df.height < MIN_BARS_REQUIRED:
        raise ValueError(
            f"日线只有 {df.height} 根, 不足以诊断 (需 >= {MIN_BARS_REQUIRED})"
        )
    close = df["close"].to_numpy().astype(float)
    candle = candle_profile(df)
    trend = trend_structure(close)
    ind = indicators(df)
    chip = chips(df)
    levels = key_levels(df)
    analogs = analog_stats(df)
    verdict = judge_strength(trend, ind, candle, chip)

    limitations: list[str] = [
        "指标由日线收盘价现算（本地 enriched 分区未物化 MA/RSI/MACD），与商业软件可能有小数差",
    ]
    if analogs["sample_size"] < 30:
        limitations.append("同形态样本不足 30 例，统计结论仅供参考")
    limitations.append("技术面只能反映价格与成交，不包含基本面、政策与未公告事项")

    return {
        "symbol": str(df["symbol"][0]),
        "bars": df.height,
        "range": [str(df["date"][0]), str(df["date"][-1])],
        "candle": candle,
        "trend": trend,
        "indicators": ind,
        "chips": chip,
        "levels": levels,
        "analogs": analogs,
        "strength": verdict.to_dict(),
        "limitations": limitations,
    }


__all__ = [
    "CHIP_DAYS",
    "MIN_BARS_REQUIRED",
    "StrengthVerdict",
    "analog_stats",
    "atr",
    "bollinger",
    "candle_profile",
    "chips",
    "diagnose",
    "ema",
    "indicators",
    "judge_strength",
    "key_levels",
    "macd",
    "moving_average",
    "rsi",
    "trend_structure",
]
