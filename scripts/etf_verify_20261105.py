# -*- coding: utf-8 -*-
"""ETF 数据可信性核查：份额折算断点 + 全字段行情（找 IOPV/净值）。只读。"""
import os

import polars as pl
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "data", "kline_etf_enriched")

ETF = ["159529.SZ", "159682.SZ", "159586.SZ"]

# ---- 1. 序列连续性：找单日 >15% 的跳变（疑似份额折算）----
print("=== 单日跳变 >12% 的日期（疑似份额折算/真实暴跌）===")
for s in ETF:
    frames = []
    for d in sorted([x[5:] for x in os.listdir(BASE) if x.startswith("date=")]):
        p = os.path.join(BASE, f"date={d}", "part.parquet")
        if os.path.exists(p):
            frames.append(pl.read_parquet(p))
    df = pl.concat(frames, how="vertical_relaxed")
    if df.schema.get("date") not in (pl.String, pl.Utf8):
        df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
    sub = df.filter(pl.col("symbol") == s).sort("date")
    cs = sub["close"].to_list()
    ds = sub["date"].to_list()
    amt = sub["amount"].to_list()
    vol = sub["volume"].to_list()
    print(f"-- {s} n={len(cs)} range {ds[0] if ds else None}..{ds[-1] if ds else None}")
    hits = 0
    for i in range(1, len(cs)):
        if cs[i - 1] and abs(cs[i] / cs[i - 1] - 1) > 0.12:
            print(f"   {ds[i]} {cs[i-1]} -> {cs[i]}  ({(cs[i]/cs[i-1]-1)*100:+.2f}%)  vol={vol[i]} amt={amt[i]}")
            hits += 1
    if hits == 0:
        print("   无 >12% 单日跳变")

# ---- 2. 159682 近 30 日明细 ----
print("\n=== 159682 近 30 个交易日 ===")
frames = []
for d in sorted([x[5:] for x in os.listdir(BASE) if x.startswith("date=")])[-30:]:
    p = os.path.join(BASE, f"date={d}", "part.parquet")
    if os.path.exists(p):
        frames.append(pl.read_parquet(p))
df = pl.concat(frames, how="vertical_relaxed")
if df.schema.get("date") not in (pl.String, pl.Utf8):
    df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
sub = df.filter(pl.col("symbol") == "159682.SZ").sort("date")
for r in sub.iter_rows(named=True):
    print(f"   {r['date']} O={r['open']} H={r['high']} L={r['low']} C={r['close']} vol={r['volume']} amt={r['amount']}")

# ---- 3. 全字段行情：找 IOPV/净值/溢价 ----
print("\n=== 腾讯全字段（159529 标普消费ETF / 159682 / 159586）===")
try:
    sess = requests.Session()
    sess.trust_env = False
    r = sess.get("http://qt.gtimg.cn/q=sz159529,sz159682,sz159586", timeout=15)
    r.encoding = "gbk"
    for line in r.text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        parts = v.strip('"').split("~")
        print(f"--- {k} 共 {len(parts)} 字段")
        for i, p in enumerate(parts):
            if p not in ("", "0.00"):
                print(f"    [{i}] {p}")
except Exception as e:
    print("ERR", type(e).__name__, e)
