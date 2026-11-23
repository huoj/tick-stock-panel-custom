"""低估值因子 IC 检验 — 稳健性补充分析
回答三个问题：
  1. 效应在时间上是否稳定（分期 IC）
  2. 是不是只是"银行股"行情（行业剔除后是否还在）
  3. 为什么最便宜的 Q5 反而跑输 Q3/Q4
"""
import glob
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(r"D:\deepseek_path\tick-stock-panel\data")
panel = pl.read_parquet(ROOT / "logs" / "valuation_ic_panel.parquet")
print(f"面板: {panel.height} 行 / {panel['rebal_date'].n_unique()} 期\n")

# ---------- 1. 逐期 IC ----------
print("=" * 70)
print("1. 逐期 IC（账面市值比 BP）")
print("=" * 70)
ics = []
for d, g in panel.filter(pl.col("bp").is_not_null()).group_by("rebal_date"):
    if g.height < 50:
        continue
    r = g.select(pl.corr(pl.col("bp").rank(), pl.col("fwd_ret").rank()).alias("ic"))["ic"][0]
    if r is not None and np.isfinite(r):
        ics.append((str(d)[:7], float(r), g.height))
ics.sort()
pos = sum(1 for _, v, _ in ics if v > 0)
for m, v, n in ics:
    bar = "+" * int(abs(v) * 40)
    sign = "✓" if v > 0 else "✗"
    print(f"  {m}  IC={v:+.4f} {sign}  n={n:>5}  {bar}")
print(f"\n  为正: {pos}/{len(ics)} = {pos/len(ics)*100:.1f}%")

# 按年
byyear = {}
for m, v, _ in ics:
    byyear.setdefault(m[:4], []).append(v)
print("\n  分年:")
for y in sorted(byyear):
    a = np.array(byyear[y])
    print(f"    {y}: IC均值 {a.mean():+.4f}  期数 {len(a)}  胜率 {(a>0).mean()*100:.0f}%")

# ---------- 2. 剔除银行后是否还在 ----------
print("\n" + "=" * 70)
print("2. 行业中性检验：剔除银行/非银后，低估值效应是否仍存在")
print("=" * 70)
hy = pl.read_parquet(
    glob.glob(str(ROOT / "ext_data" / "ext_hy_ths" / "**" / "*.parquet"), recursive=True)[0]
)
print("  行业表列:", hy.columns)
ind_col = [c for c in hy.columns if "行业" in c or "industry" in c.lower()]
if ind_col:
    ind = hy.select(["symbol", ind_col[0]]).rename({ind_col[0]: "industry"})
    pj = panel.join(ind, on="symbol", how="left")
    fin_kw = "银行|保险|证券|信托|多元金融|租赁"
    pj = pj.with_columns(
        pl.col("industry").str.contains(fin_kw).fill_null(False).alias("is_fin")
    )
    for name, sub in [
        ("全样本", pj),
        ("剔除金融", pj.filter(~pl.col("is_fin"))),
        ("仅金融", pj.filter(pl.col("is_fin"))),
    ]:
        arr = []
        for d, g in sub.filter(pl.col("bp").is_not_null()).group_by("rebal_date"):
            if g.height < 50:
                continue
            r = g.select(pl.corr(pl.col("bp").rank(), pl.col("fwd_ret").rank()).alias("ic"))["ic"][0]
            if r is not None and np.isfinite(r):
                arr.append(float(r))
        if arr:
            a = np.array(arr)
            t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan
            print(
                f"  {name:<8} 期数{len(a):>4}  IC均值 {a.mean():+.4f}  IR {a.mean()/a.std(ddof=1):+.3f}  "
                f"胜率 {(a>0).mean()*100:>5.1f}%  t={t:+.2f}"
            )
else:
    print("  未找到行业列")

# ---------- 3. Q5 为何跑输 Q3/Q4 ----------
print("\n" + "=" * 70)
print("3. 最便宜组(Q5)的结构：为什么跑输 Q3/Q4")
print("=" * 70)
sub = panel.filter(pl.col("bp").is_not_null())
last = sub.filter(pl.col("rebal_date") == sub["rebal_date"].max())
last = last.with_columns(
    pl.col("bp").qcut(5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"], allow_duplicates=True).alias("grp")
)
print(f"  最新期 {sub['rebal_date'].max()}")
q5 = last.filter(pl.col("grp") == "Q5")
print(f"  Q5 组 {q5.height} 只，PB 中位数 {q5['pb'].median():.2f}")
if "is_fin" in last.columns:
    for gname in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
        g = last.filter(pl.col("grp") == gname)
        if g.height:
            finp = g.filter(pl.col("is_fin")).height / g.height * 100
            print(f"    {gname}: PB中位 {g['pb'].median():>6.2f}  金融占比 {finp:>5.1f}%  只数 {g.height}")

# 各组平均 PB 与收益对照（全期）
print("\n  全期分组：平均PB vs 平均月收益")
rows = []
for d, g in sub.group_by("rebal_date"):
    if g.height < 50:
        continue
    g = g.with_columns(
        pl.col("bp").qcut(5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"], allow_duplicates=True).alias("grp")
    )
    a = g.group_by("grp").agg([
        pl.col("pb").median().alias("pb_med"),
        pl.col("fwd_ret").mean().alias("ret"),
    ])
    for r in a.iter_rows(named=True):
        rows.append(r)
if rows:
    gg = pl.DataFrame(rows).group_by("grp").agg([
        pl.col("pb_med").mean().alias("PB中位"),
        pl.col("ret").mean().alias("月均收益"),
    ]).sort("grp")
    for r in gg.iter_rows(named=True):
        print(f"    {r['grp']}: PB中位 {r['PB中位']:>6.2f}   月均收益 {r['月均收益']*100:>+6.2f}%   年化 {r['月均收益']*1200:>+7.2f}%")
