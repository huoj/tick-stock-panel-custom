# -*- coding: utf-8 -*-
"""核对 000333 / 600941 的 11-04「60日新高」长上影。只读。"""
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB = os.path.join(ROOT, "data", "kline_daily_enriched")
COLS = ["symbol", "date", "open", "high", "low", "close", "amount"]
ds = sorted([d[5:] for d in os.listdir(SB) if d.startswith("date=")])[-7:]
fr = [pl.read_parquet(os.path.join(SB, f"date={d}", "part.parquet")) for d in ds]
df = pl.concat(fr, how="diagonal_relaxed").select(COLS)
if df.schema["date"] not in (pl.String, pl.Utf8):
    df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))

for s in ["000333.SZ", "600941.SH", "000651.SZ", "601633.SH", "600779.SH", "300059.SZ", "605366.SH"]:
    sub = df.filter(pl.col("symbol") == s).sort("date")
    print(f"-- {s}")
    pc = None
    for r in sub.iter_rows(named=True):
        chg = "" if pc is None else f"{(r['close']/pc-1)*100:+6.2f}%"
        upsh = ""
        if r["open"] and r["high"] and r["low"] and r["close"]:
            rng = r["high"] - r["low"]
            if rng > 0:
                pos = (r["close"] - r["low"]) / rng
                upsh = f"收盘位于当日区间 {pos*100:5.1f}%"
        print(f"     {r['date']} O={r['open']:>7} H={r['high']:>7} L={r['low']:>7} C={r['close']:>7} {chg}  {upsh}")
        pc = r["close"]
