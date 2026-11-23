# -*- coding: utf-8 -*-
"""补齐自选股行业与关键行业的中位涨幅。只读。"""
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SB = os.path.join(DATA, "kline_daily_enriched")
WL = ["601633.SH", "300059.SZ", "600779.SH", "000333.SZ", "000651.SZ",
      "605366.SH", "600941.SH"]


def load(base, dates):
    fr = []
    for d in dates:
        p = os.path.join(base, f"date={d}", "part.parquet")
        if os.path.exists(p):
            fr.append(pl.read_parquet(p))
    df = pl.concat(fr, how="diagonal_relaxed")
    if df.schema["date"] not in (pl.String, pl.Utf8):
        df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
    return df


ds = sorted([d[5:] for d in os.listdir(SB) if d.startswith("date=")])[-2:]
df = load(SB, ds)
cur = df.filter(pl.col("date") == ds[-1]).select(["symbol", "close"])
prv = df.filter(pl.col("date") == ds[-2]).select(["symbol", "close"])
j = cur.join(prv, on="symbol", suffix="_p").with_columns(
    ((pl.col("close") / pl.col("close_p") - 1) * 100).alias("ret"))

hy = pl.read_parquet(os.path.join(DATA, "ext_data", "ext_hy_ths", "part.parquet"))
m2 = hy.select([pl.col("symbol"), pl.col("所属同花顺行业").alias("hy")])
k = j.join(m2, on="symbol", how="left").drop_nulls(["ret", "hy"])
g = (k.group_by("hy").agg(pl.len().alias("n"), pl.col("ret").median().alias("med"))
     .filter(pl.col("n") >= 3).sort("med", descending=True))
print(f"行业数(n>=3) = {g.height}")

print("\n自选股所在行业:")
for s in WL:
    row = k.filter(pl.col("symbol") == s)
    if row.height == 0:
        continue
    h = row["hy"][0]
    gr = g.filter(pl.col("hy") == h)
    if gr.height == 0:
        print(f"   {s} {h}  (样本<n=3)")
        continue
    med = gr["med"][0]
    rank = int((g["med"] > med).sum()) + 1
    print(f"   {s}  {h:<28} 中位 {med:+6.2f}%  排名 {rank:>3}/{g.height}")

print("\n关键行业（模糊匹配，含样本数最少的匹配项）:")
for pat in ["白色家电", "通信服务", "汽车整车", "白酒", "证券", "光伏", "有机硅",
            "银行", "煤炭", "电力", "贵金属", "半导体", "医药", "食品"]:
    seg = g.filter(pl.col("hy").str.contains(pat))
    if seg.height == 0:
        print(f"   {pat:<10} 无匹配")
        continue
    for r in seg.head(4).iter_rows(named=True):
        print(f"   {pat:<10} {r['hy'][:26]:<28} n={r['n']:<4} {r['med']:+6.2f}%")

print("\n涨停/跌停（自选股）：")
zt = os.path.join(DATA, "ext_data", "ext_ztlimit", "part.parquet")
if os.path.exists(zt):
    z = pl.read_parquet(zt)
    print("   cols:", z.columns)
    sc = [c for c in z.columns if c in ("symbol", "code")]
    if sc:
        h = z.filter(pl.col(sc[0]).is_in(WL))
        print("   自选股命中:", h.height)
        print(h.head(20) if h.height else "")
