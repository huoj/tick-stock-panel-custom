# -*- coding: utf-8 -*-
"""验证腾讯 ETF 字段 [77]=溢价率 / [78]=净值，并核对 159682 跟踪指数跌幅。只读。"""
import os

import polars as pl
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 一批 ETF：含 QDII（常有溢价）与境内
CODES = ("sz159529,sz159682,sz159586,sz159920,sz159941,sz513050,sz513100,sz513500,"
         "sh513180,sh513330,sh510300,sh510500,sh512880,sh512480,sh515030,sh516160,"
         "sz159915,sz159949,sh588000,sz159509,sh513520,sh518880,sz159934,sh511990")

s = requests.Session()
s.trust_env = False
r = s.get("http://qt.gtimg.cn/q=" + CODES, timeout=20)
r.encoding = "gbk"

print("== 溢价率字段验证：[77] vs (现价/[78]-1) ==")
print(f"{'代码':<10}{'名称':<18}{'现价':>8}{'[78]净值':>10}{'算得溢价%':>11}{'[77]字段':>10}{'差':>8}")
ok = bad = 0
for line in r.text.strip().split(";"):
    line = line.strip()
    if "=" not in line:
        continue
    k, v = line.split("=", 1)
    p = v.strip('"').split("~")
    if len(p) < 88:
        continue
    try:
        price = float(p[3]); nav = float(p[78]); prem_f = float(p[77])
    except Exception:
        continue
    if nav == 0:
        continue
    calc = (price / nav - 1) * 100
    d = calc - prem_f
    flag = "OK" if abs(d) < 0.06 else "XX"
    if flag == "OK":
        ok += 1
    else:
        bad += 1
    print(f"{p[2]:<10}{p[1]:<18}{price:>8}{nav:>10}{calc:>11.2f}{prem_f:>10.2f}{d:>8.2f}  {flag}")
print(f"\n结论：完全吻合 {ok} 个 / 不吻合 {bad} 个")

# ---- 159682 跟踪指数跌幅核对 ----
print("\n== 创业板指(399006) 与 创业板50 相关序列 ==")
base = os.path.join(ROOT, "data", "kline_index_enriched")
if os.path.isdir(base):
    ds = sorted([d[5:] for d in os.listdir(base) if d.startswith("date=")])
    print("index dates last5:", ds[-5:])
    fr = []
    for d in ds[-200:]:
        pp = os.path.join(base, f"date={d}", "part.parquet")
        if os.path.exists(pp):
            fr.append(pl.read_parquet(pp))
    if fr:
        df = pl.concat(fr, how="vertical_relaxed")
        if df.schema.get("date") not in (pl.String, pl.Utf8):
            df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
        print("index symbols sample:", df["symbol"].unique().to_list()[:20])
        for sym in ["399006.SZ", "399673.SZ", "000001.SH"]:
            sub = df.filter(pl.col("symbol") == sym).sort("date")
            if sub.height == 0:
                continue
            cs = sub["close"].to_list()
            dsx = sub["date"].to_list()
            out = [sym, f"n={len(cs)}", f"last={dsx[-1]} C={cs[-1]}"]
            for w in (5, 20, 60, 120):
                if len(cs) > w:
                    out.append(f"{w}d={cs[-1]/cs[-1-w]*100-100:+.2f}%")
            print("  ", " ".join(out))
