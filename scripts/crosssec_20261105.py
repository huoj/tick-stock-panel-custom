# -*- coding: utf-8 -*-
"""自选股当日横截面分位 + 组合合成收益 + 龙虎榜命中 + 风格对冲。只读。"""
import json
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SB = os.path.join(DATA, "kline_daily_enriched")
WL = ["601633.SH", "300059.SZ", "600779.SH", "000333.SZ", "000651.SZ",
      "605366.SH", "600941.SH"]
WL_ALL = WL + ["159529.SZ", "159682.SZ", "159586.SZ"]


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


ds = sorted([d[5:] for d in os.listdir(SB) if d.startswith("date=")])[-2:]
df = load(SB, ds)
cur = df.filter(pl.col("date") == ds[-1]).select(["symbol", "close", "amount", "turnover_rate"])
prv = df.filter(pl.col("date") == ds[-2]).select(["symbol", "close"])
j = cur.join(prv, on="symbol", suffix="_p")
j = j.with_columns(((pl.col("close") / pl.col("close_p") - 1) * 100).alias("ret"))
n = j.height
ret_sorted = sorted(j["ret"].to_list())

print(f"=== 11-05 全市场 n={n} ===")
for q in (5, 10, 25, 50, 75, 90, 95):
    print(f"   P{q:<3} = {ret_sorted[int(n*q/100)]:+.2f}%")

print("\n=== 自选股（A股）当日横截面 ===")
tot = []
for s in WL:
    r = j.filter(pl.col("symbol") == s)
    if r.height == 0:
        print(f"   {s} 无当日数据")
        continue
    v = r["ret"][0]
    pct_rank = sum(1 for x in ret_sorted if x < v) / n * 100
    amt = r["amount"][0] / 1e8
    to = r["turnover_rate"][0]
    tot.append(v)
    print(f"   {s}  {v:+6.2f}%  分位 {pct_rank:5.1f}%  额 {amt:6.2f}亿  换手 {to}")

if tot:
    print(f"\n   A股 7 只等权当日 = {sum(tot)/len(tot):+.2f}%   （市场中位 {ret_sorted[n//2]:+.2f}%）")

# ETF 用外部行情
ETF_RET = {"159529.SZ": 5.34, "159682.SZ": 0.27, "159586.SZ": 2.87}
allr = tot + [ETF_RET[x] for x in ["159529.SZ", "159682.SZ", "159586.SZ"]]
print(f"   10 只等权当日 = {sum(allr)/len(allr):+.2f}%")

# 风格对冲：行业组
print("\n=== 11-05 风格组（按行业分类聚合，中位涨幅）===")
HY = os.path.join(DATA, "ext_data", "ext_hy_ths", "part.parquet")
if os.path.exists(HY):
    hy = pl.read_parquet(HY)
    print("   ext_hy_ths cols:", hy.columns)
    sc = [c for c in hy.columns if c in ("symbol", "code")]
    nc = [c for c in hy.columns if c in ("hy", "industry", "name", "hy_name")]
    if sc and nc:
        m = hy.select([sc[0], nc[0]]).rename({sc[0]: "symbol", nc[0]: "hy"})
        k = j.join(m, on="symbol", how="left")
        g = (k.group_by("hy").agg(pl.len().alias("n"), pl.col("ret").median().alias("med"))
             .filter(pl.col("n") >= 3).sort("med", descending=True))
        print("   最强 15 个行业:")
        for r in g.head(15).iter_rows(named=True):
            print(f"      {r['hy']:<24} n={r['n']:<4} 中位 {r['med']:+.2f}%")
        print("   最弱 15 个行业:")
        for r in g.tail(15).iter_rows(named=True):
            print(f"      {r['hy']:<24} n={r['n']:<4} 中位 {r['med']:+.2f}%")
        print("   自选股所属行业:")
        for s in WL:
            row = k.filter(pl.col("symbol") == s)
            if row.height:
                print(f"      {s} -> {row['hy'][0]}")

# 龙虎榜
print("\n=== 龙虎榜（近 3 日）自选股命中 ===")
dp = os.path.join(DATA, "dragon_tiger")
for f in sorted(os.listdir(dp))[-3:]:
    obj = json.load(open(os.path.join(dp, f), encoding="utf-8"))
    alls = obj.get("all") or []
    hits = [x for x in alls if isinstance(x, dict)
            and any(str(x.get(k, ""))[:6] in {s[:6] for s in WL_ALL}
                    for k in ("code", "symbol", "ts_code"))]
    print(f"   {f}  all={len(alls)}  命中={len(hits)}")
    for h in hits:
        print("      ", {k: v for k, v in h.items() if k in
                          ("code", "name", "pct", "turnover", "reason", "net_buy", "buy", "sell")})
    if alls and f == sorted(os.listdir(dp))[-1]:
        print("   样本:", {k: v for k, v in list(alls[0].items())[:12]})
