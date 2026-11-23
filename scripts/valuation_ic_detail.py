"""低估值因子 IC 检验 — 分年 / 半程 / 分组构成"""
import glob
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(r"D:\deepseek_path\tick-stock-panel\data")
panel = pl.read_parquet(ROOT / "logs" / "valuation_ic_panel.parquet")

ics = []
for key, g in panel.filter(pl.col("bp").is_not_null()).group_by("rebal_date"):
    d = key[0] if isinstance(key, tuple) else key
    if g.height < 50:
        continue
    r = g.select(pl.corr(pl.col("bp").rank(), pl.col("fwd_ret").rank()).alias("ic"))["ic"][0]
    if r is not None and np.isfinite(r):
        ics.append((d, float(r)))
ics.sort()

print("=" * 66)
print("分年 IC（账面市值比 BP）")
print("=" * 66)
by = {}
for d, v in ics:
    by.setdefault(d.year, []).append(v)
for y in sorted(by):
    a = np.array(by[y])
    t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 and a.std(ddof=1) > 0 else float("nan")
    print(f"  {y}: IC均值 {a.mean():+.4f}  IR {a.mean()/a.std(ddof=1):+.3f}  期数 {len(a):>3}  胜率 {(a>0).mean()*100:>5.1f}%  t={t:+.2f}")

print()
print("=" * 66)
print("半程对比")
print("=" * 66)
a = np.array([v for _, v in ics])
h = len(a) // 2
for name, s in [("前半程", a[:h]), ("后半程", a[h:])]:
    t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if s.std(ddof=1) > 0 else float("nan")
    print(f"  {name}: IC均值 {s.mean():+.4f}  IR {s.mean()/s.std(ddof=1):+.3f}  胜率 {(s>0).mean()*100:>5.1f}%  t={t:+.2f}")

print()
print("=" * 66)
print("最新期各组行业构成 TOP3")
print("=" * 66)
files = glob.glob(str(ROOT / "ext_data" / "ext_hy_ths" / "**" / "*.parquet"), recursive=True)
hy = pl.read_parquet(files[0])
ind = hy.select(["symbol", hy.columns[2]]).rename({hy.columns[2]: "industry"})
pj = panel.join(ind, on="symbol", how="left")
last = pj.filter((pl.col("rebal_date") == pj["rebal_date"].max()) & pl.col("bp").is_not_null())
last = last.with_columns(
    pl.col("bp").qcut(5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"], allow_duplicates=True).alias("grp")
)
for gname in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
    g = last.filter(pl.col("grp") == gname)
    if g.height == 0:
        continue
    top = g.group_by("industry").len().sort("len", descending=True).head(3)
    parts = [f"{r['industry']}({r['len']})" for r in top.iter_rows(named=True)]
    print(f"  {gname}  PB中位 {g['pb'].median():>5.2f}  {g.height:>4}只  {' | '.join(parts)}")
