# -*- coding: utf-8 -*-
"""行业强弱 + 60日动量分档 + 反转/动量判定。只读。"""
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
    if not fr:
        return None
    df = pl.concat(fr, how="diagonal_relaxed")
    if df.schema["date"] not in (pl.String, pl.Utf8):
        df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
    return df


ds = sorted([d[5:] for d in os.listdir(SB) if d.startswith("date=")])
WINDOW = 62
df = load(SB, ds[-WINDOW:])
# 最新收盘与 60 日前收盘
last = df.filter(pl.col("date") == ds[-1]).select(["symbol", "close", "amount"])
d60 = ds[-1 - 60] if len(ds) > 61 else ds[0]
old = df.filter(pl.col("date") == d60).select(["symbol", "close"])
j = last.join(old, on="symbol", suffix="_o")
j = j.with_columns((pl.col("close") / pl.col("close_o") - 1).alias("r60"))
# 前 60 日收益（截止昨收）用于分档：用 ds[-2] 与 ds[-62]
prev = df.filter(pl.col("date") == ds[-2]).select(["symbol", "close"])
o2 = df.filter(pl.col("date") == ds[-62] if len(ds) > 62 else ds[0]).select(["symbol", "close"])
p = prev.join(o2, on="symbol", suffix="_o")
p = p.with_columns(((pl.col("close") / pl.col("close_o") - 1) * 100).alias("mom60_prev"))
j = j.join(p.select(["symbol", "mom60_prev"]), on="symbol", how="left")
j = j.with_columns(((pl.col("close") / pl.col("close_o") - 1) * 100).alias("ret20"))

# 当日收益
d_prev = df.filter(pl.col("date") == ds[-2]).select(["symbol", "close"])
jj = last.join(d_prev, on="symbol", suffix="_p")
jj = jj.with_columns(((pl.col("close") / pl.col("close_p") - 1) * 100).alias("ret"))
m = j.join(jj.select(["symbol", "ret"]), on="symbol", how="left")

print("=== 11-05：按「前60日涨幅」分档 → 当日中位收益（反转 or 动量？）===")
sub = m.filter(pl.col("mom60_prev").is_not_null() & pl.col("ret").is_not_null())
srt = sub.sort("mom60_prev")
n = srt.height
labels = ["最弱20%", "20-40%", "40-60%", "60-80%", "最强20%"]
print(f"   n={n}")
for i, lab in enumerate(labels):
    seg = srt[int(n * i / 5):int(n * (i + 1) / 5)]
    v = sorted(seg["ret"].to_list())
    mv = sorted(seg["mom60_prev"].to_list())
    print(f"   {lab:<10} 前60日区间[{mv[0]:+7.1f}%,{mv[-1]:+7.1f}%]  当日中位={v[len(v)//2]:+6.2f}%")

print("\n=== 11-05：按「前20日涨幅」分档 → 当日中位收益 ===")
sub2 = m.filter(pl.col("mom60_prev").is_not_null() & pl.col("ret").is_not_null())
srt2 = m.drop_nulls(["r60"]).sort("r60")
n2 = srt2.height
for i, lab in enumerate(labels):
    seg = srt2[int(n2 * i / 5):int(n2 * (i + 1) / 5)]
    v = sorted(seg["ret"].to_list())
    print(f"   {lab:<10} 当日中位={v[len(v)//2]:+6.2f}%")

print("\n=== 行业强弱 ===")
HY = os.path.join(DATA, "ext_data", "ext_hy_ths", "part.parquet")
hy = pl.read_parquet(HY)
m2 = hy.select([pl.col("symbol"), pl.col("所属同花顺行业").alias("hy")])
k = m.join(m2, on="symbol", how="left").drop_nulls(["ret", "hy"])
g = (k.group_by("hy").agg(pl.len().alias("n"), pl.col("ret").median().alias("med"))
     .filter(pl.col("n") >= 8).sort("med", descending=True))
print(f"   行业数 {g.height}")
print("   最强 12:")
for r in g.head(12).iter_rows(named=True):
    print(f"      {r['hy'][:30]:<32} n={r['n']:<4} {r['med']:+.2f}%")
print("   最弱 12:")
for r in g.tail(12).iter_rows(named=True):
    print(f"      {r['hy'][:30]:<32} n={r['n']:<4} {r['med']:+.2f}%")

print("\n   自选股所在行业当日位置:")
for s in WL:
    row = k.filter(pl.col("symbol") == s)
    if row.height:
        h = row["hy"][0]
        gr = g.filter(pl.col("hy") == h)
        rank = g.height - (gr.height and (g["med"] > gr["med"][0]).sum() or 0)
        print(f"      {s} {h[:24]:<26} 行业内中位 {gr['med'][0]:+.2f}%  行业排名 {rank}/{g.height}")

print("\n=== 关键红利/防御行业 vs 成长行业 ===")
KEY = {
    "银行": ["银行"], "煤炭": ["煤炭"], "石油": ["石油", "油气"], "电力": ["电力", "水电", "火电"],
    "高速公路": ["高速公路"], "白酒": ["白酒"], "光伏": ["光伏"], "证券": ["证券"],
    "白色家电": ["白色家电"], "汽车整车": ["汽车整车"], "通信服务": ["通信服务"], "半导体": ["半导体"],
}
for name, pats in KEY.items():
    seg = k.filter(pl.col("hy").str.contains("|".join(pats)))
    if seg.height:
        v = sorted(seg["ret"].drop_nulls().to_list())
        print(f"   {name:<10} n={len(v):<5} 中位={v[len(v)//2]:+6.2f}%")
