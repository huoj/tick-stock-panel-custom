# -*- coding: utf-8 -*-
"""本地 kline 与外部分发行情交叉验证（只读）。"""
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = ["601633.SH", "300059.SZ", "600779.SH", "000333.SZ", "000651.SZ", "605366.SH", "600941.SH"]

for d in ["2026-11-04", "2026-11-05"]:
    p = os.path.join(ROOT, "data", "kline_daily_enriched", "date=" + d, "part.parquet")
    df = pl.read_parquet(p)
    sub = df.filter(pl.col("symbol").is_in(W)).sort("symbol")
    print("===", d)
    for r in sub.iter_rows(named=True):
        print("  {} O={} H={} L={} C={} rawC={} vol={} amt={}".format(
            r["symbol"], r["open"], r["high"], r["low"], r["close"],
            r["raw_close"], r["volume"], r["amount"]))
