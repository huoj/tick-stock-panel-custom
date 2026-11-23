"""个股分析数据采集器 —— 一次跑出 `docs/个股分析标准框架.md` 需要的全部原始数据。

用法:
    cd backend
    .venv\\Scripts\\python.exe scripts/analyze_stock.py 600941.SH
    .venv\\Scripts\\python.exe scripts/analyze_stock.py 600941.SH --capital 500000
    .venv\\Scripts\\python.exe scripts/analyze_stock.py 510300.SH --asset-type etf
    .venv\\Scripts\\python.exe scripts/analyze_stock.py 600941.SH --json

设计:
- 算法全部来自 `app/services/stock_diagnostics.py` —— **本脚本不含平行实现**,
  只负责读数据与渲染文本。
- **只出数据, 不出结论**。结论由分析者基于数据给出。
- 一切数字来自本地 parquet, 不联网, 不猜测。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import stock_diagnostics as diag

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
FORWARD_WINDOWS = diag.FORWARD_WINDOWS


# ── 数据装载 ────────────────────────────────────────────

def load_daily(symbol: str, asset_type: str = "stock") -> pl.DataFrame:
    table = {
        "stock": "kline_daily",
        "etf": "kline_etf_daily",
        "index": "kline_index_daily",
    }[asset_type]
    files = sorted(glob.glob(str(DATA_DIR / table / "date=*" / "part*.parquet")))
    if not files:
        raise SystemExit(f"没有找到 {table} 数据: {DATA_DIR / table}")

    cols = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    frames = []
    for path in files:
        try:
            df = pl.read_parquet(path)
        except Exception:  # noqa: BLE001 - 坏分区跳过, 不中断整体分析
            continue
        keep = [c for c in cols if c in df.columns]
        hit = df.filter(pl.col("symbol") == symbol).select(keep)
        if hit.height:
            frames.append(hit)
    if not frames:
        raise SystemExit(f"所有分区里都没有 {symbol}")
    return pl.concat(frames, how="diagonal_relaxed").unique(subset=["date"]).sort("date")


# ── 资金换算 (大白话版) ─────────────────────────────────

def money_scenarios(df: pl.DataFrame, capital: float, levels: dict, ind: dict) -> dict:
    """把钱换算成"赚/亏多少元"。情景按价位方位自动选动词。"""
    close = df["close"].to_numpy().astype(float)
    current = float(close[-1])
    shares = int(capital / current / 100) * 100
    if shares <= 0:
        one_lot = current * 100
        return {
            "error": f"资金 {capital:,.0f} 元不足以买一手 (现价 {current:.2f} × 100 股 = {one_lot:,.0f} 元)",
            "suggested_capital": round(one_lot * 1.05),
        }
    cost = shares * current

    ma = {n: float(diag.moving_average(close, n)[-1]) for n in (5, 10, 20, 60, 120) if len(close) >= n}
    candidates = [(f"MA{n}", v) for n, v in sorted(ma.items(), key=lambda kv: -kv[0])] + [
        ("近20日低", levels["low_20"]["price"]),
        ("近60日低", levels["low_60"]["price"]),
        ("近20日高", levels["high_20"]["price"]),
        ("近60日高", levels["high_60"]["price"]),
    ]

    seen: set[float] = set()
    filtered: list[tuple[str, float, float]] = []
    for label, price in candidates:
        if price <= 0:
            continue
        key = round(price, 2)
        if key in seen:
            continue
        seen.add(key)
        filtered.append((label, price, (price / current - 1) * 100))

    down = sorted([x for x in filtered if x[2] < -0.3], key=lambda x: -x[2])[:3]
    up = sorted([x for x in filtered if x[2] > 0.3], key=lambda x: x[2])[:3]

    def row(label: str, price: float, direction: str) -> dict:
        return {
            "scenario": ("跌到 " if direction == "down" else "涨到 ") + label,
            "direction": direction,
            "price": round(price, 2),
            "per_share": round(price - current, 2),
            "pnl": round((price - current) * shares),
            "pnl_pct": round((price / current - 1) * 100, 2),
        }

    rows = [row(lb, p, "down") for lb, p, _ in down]
    rows.append({"scenario": "原地不动", "direction": "flat", "price": round(current, 2),
                 "per_share": 0.0, "pnl": 0, "pnl_pct": 0.0})
    rows += [row(lb, p, "up") for lb, p, _ in up]

    return {
        "capital": capital,
        "shares": shares,
        "cost": round(cost),
        "daily_swing": round(ind["atr14"] * shares),
        "annual_swing_20d": (
            round(capital * ind["vol_20d_annual"] / 100) if ind.get("vol_20d_annual") else None
        ),
        "scenarios": rows,
    }


# ── 输出 ────────────────────────────────────────────────

def render(result: dict) -> str:
    lines: list[str] = []
    cp, tr, ind = result["candle"], result["trend"], result["indicators"]
    ch, kl, an, mo = result["chips"], result["levels"], result["analogs"], result["money"]
    st = result["strength"]

    lines.append(f"=== {result['symbol']}  截止 {cp['date']}  ({result['bars']} 根日线) ===")
    lines.append(f"K线: 开{cp['open']:.2f} 高{cp['high']:.2f} 低{cp['low']:.2f} 收{cp['close']:.2f}  ({cp['change_pct']:+.2f}%)")
    lines.append(f"  实体占比 {cp['body_ratio']:.0f}%  收盘分位 {cp['close_position']:.0f}%  振幅 {cp['amplitude_pct']:.2f}%")
    lines.append(f"  量 {cp['volume']/10000:.2f}万手 / 20日均 {cp['volume_ma20']/10000:.2f}万手 = {cp['volume_ratio']:.0f}%  [{cp['volume_state']}]")
    lines.append("")
    lines.append(f"结构: {tr['structure']}   (250日区间 {tr['low_250']:.2f}~{tr['high_250']:.2f}, 现价处于 {tr['position_in_250_pct']:.0f}% 分位)")
    for k, v in tr["mas"].items():
        lines.append(f"  {k:6} {v:8.2f}   偏离 {tr['deviation_pct'][k]:+6.2f}%")
    lines.append("")
    lines.append(f"指标: RSI14 {ind['rsi14']} (前值 {ind['rsi14_prev']})")
    lines.append(f"  MACD DIF{ind['macd_dif']} DEA{ind['macd_dea']} 柱{ind['macd_hist']} "
                 f"({'扩张' if ind['macd_hist_expanding'] else '收敛'}, 前值 {ind['macd_hist_prev']})")
    lines.append(f"  布林 上{ind['boll_upper']} 中{ind['boll_mid']} 下{ind['boll_lower']}")
    lines.append(f"  ATR14 {ind['atr14']} 元 ({ind['atr14_pct']}%)   年化波动 20日{ind['vol_20d_annual']}% / 60日{ind['vol_60d_annual']}%")
    lines.append("")
    lines.append(f"筹码 (近{ch['window']}日):")
    for r in sorted(ch["bins"], key=lambda x: -x["high"]):
        if r["pct"] <= 0:
            continue
        bar = "#" * int(r["pct"] / 2)
        mark = ""
        if ch["densest_above"] and r["low"] == ch["densest_above"]["low"]:
            mark = f"  <- 上方最密(阻力) 该方向共 {ch['densest_above']['total_pct']:.0f}%"
        if ch["densest_below"] and r["high"] == ch["densest_below"]["high"]:
            mark = f"  <- 下方最密(支撑) 该方向共 {ch['densest_below']['total_pct']:.0f}%"
        lines.append(f"  {r['low']:7.2f}~{r['high']:7.2f}: {r['pct']:5.1f}% {bar}{mark}")
    lines.append("")
    lines.append("关键位:")
    for label in ("high_60", "high_20", "low_20", "low_60"):
        lv = kl[label]
        lines.append(f"  {label:8} {lv['price']:8.2f}  {lv['distance_pct']:+6.2f}%")
    if kl["pivots_high_recent"]:
        lines.append(f"  摆动高点: {'  '.join(f'{v:.2f}' for v in kl['pivots_high_recent'])}")
    if kl["pivots_low_recent"]:
        lines.append(f"  摆动低点: {'  '.join(f'{v:.2f}' for v in kl['pivots_low_recent'])}")
    for g in kl["gaps_30d"]:
        arrow = "向上" if g["type"] == "up" else "向下"
        lines.append(f"  {g['date']} {arrow}跳空 {g['from']} -> {g['to']}")
    lines.append("")
    lines.append(f"强弱判定: {st['label']}  (看多 {len(st['bullish_evidence'])} 条 / 看空 {len(st['bearish_evidence'])} 条)")
    for e in st["bullish_evidence"]:
        lines.append(f"  + {e}")
    for e in st["bearish_evidence"]:
        lines.append(f"  - {e}")
    for e in st["neutral_evidence"]:
        lines.append(f"  · {e}")
    lines.append("")
    lines.append(f"同形态统计 ({an['pattern']}): 样本 {an['sample_size']} 例")
    lines.append("  前瞻期   形态胜率   形态均值   无条件胜率   无条件均值")
    for w in FORWARD_WINDOWS:
        p = an["pattern_stats"].get(str(w), {})
        b = an["baseline_stats"].get(str(w), {})
        lines.append(
            f"  {w:>2}日     {p.get('win_rate','-'):>6}%    {p.get('mean_pct','-'):>7}%    "
            f"{b.get('win_rate','-'):>8}%    {b.get('mean_pct','-'):>8}%"
        )
    for warning in an["warnings"]:
        lines.append(f"  ! {warning}")
    if an["recent_cases"]:
        lines.append("  最近同形态样本:")
        for c in an["recent_cases"]:
            fwd = "  ".join(f"{w}日{v:+5.2f}%" for w, v in c["forward"].items())
            lines.append(f"    {c['date']}: {fwd}")
    lines.append("")
    if "error" in mo:
        lines.append(mo["error"])
        if mo.get("suggested_capital"):
            lines.append(f"  建议: --capital {mo['suggested_capital']:,} (一手约需 {mo['suggested_capital']:,} 元)")
    else:
        lines.append(f"资金换算 (本金 {mo['capital']:,.0f} 元 = {mo['shares']} 股, 花费 {mo['cost']:,.0f} 元):")
        lines.append("  情景            目标价    每股     本金盈亏")
        for s in mo["scenarios"]:
            lines.append(f"  {s['scenario']:14} {s['price']:8.2f} {s['per_share']:+7.2f}  {s['pnl']:+10,d} 元")
        lines.append(f"  日均波动 ≈ {mo['daily_swing']:,} 元/天", )
        if mo.get("annual_swing_20d"):
            lines.append(f"  一年正常波动 ≈ ±{mo['annual_swing_20d']:,} 元")
    lines.append("")
    lines.append("限制:")
    for lim in result["limitations"]:
        lines.append(f"  - {lim}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="个股分析数据采集 (只出数据, 不出结论)")
    ap.add_argument("symbol", help="标的代码, 如 600941.SH")
    ap.add_argument("--asset-type", default="stock", choices=["stock", "etf", "index"])
    ap.add_argument("--capital", type=float, default=100_000.0, help="本金(元), 默认 10 万")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    args = ap.parse_args()

    df = load_daily(args.symbol, args.asset_type)
    if df.height < diag.MIN_BARS_REQUIRED:
        raise SystemExit(
            f"{args.symbol} 只有 {df.height} 根日线, 不足以分析 (需 >= {diag.MIN_BARS_REQUIRED})"
        )

    result = diag.diagnose(df)
    result["money"] = money_scenarios(df, args.capital, result["levels"], result["indicators"])

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(result))


if __name__ == "__main__":
    main()
