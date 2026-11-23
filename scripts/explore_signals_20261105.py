# -*- coding: utf-8 -*-
"""探索 龙虎榜 / regime / mainline / pools，找 11-05 的额外信号。只读。"""
import datetime
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
WL = ["159529.SZ", "159682.SZ", "159586.SZ", "601633.SH", "300059.SZ",
      "600779.SH", "000333.SZ", "000651.SZ", "605366.SH", "600941.SH"]


def walk_show(name, maxdepth=3):
    base = os.path.join(DATA, name)
    print(f"\n=== {name} ===")
    if not os.path.isdir(base):
        print("   MISSING")
        return
    n = 0
    for dirpath, dirnames, filenames in os.walk(base):
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > maxdepth:
            dirnames[:] = []
            continue
        if dirnames:
            print(f"   [d] {rel}  -> {sorted(dirnames)[:12]}")
        for f in sorted(filenames)[:12]:
            fp = os.path.join(dirpath, f)
            mt = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%m-%d %H:%M")
            print(f"   {os.path.join(rel, f) if rel != '.' else f:<50} {os.path.getsize(fp):>10}  {mt}")
        n += 1
        if n > 25:
            print("   ...(截断)")
            break


for t in ["dragon_tiger", "regime_history", "mainline_history", "pools", "depth5"]:
    walk_show(t)

# regime 最新状态
print("\n=== regime_history 最新内容 ===")
rp = os.path.join(DATA, "regime_history")
if os.path.isdir(rp):
    for f in sorted(os.listdir(rp))[-3:]:
        fp = os.path.join(rp, f)
        if os.path.isfile(fp) and f.endswith((".json", ".parquet")):
            print(f"-- {f}")
            try:
                if f.endswith(".parquet"):
                    print(pl.read_parquet(fp).tail(8))
                else:
                    txt = open(fp, encoding="utf-8").read()
                    print(txt[-2500:])
            except Exception as e:
                print("   ERR", type(e).__name__, e)

print("\n=== mainline_history 最新 ===")
mp = os.path.join(DATA, "mainline_history")
if os.path.isdir(mp):
    for f in sorted(os.listdir(mp))[-3:]:
        fp = os.path.join(mp, f)
        print(f"-- {f}")
        try:
            if f.endswith(".parquet"):
                print(pl.read_parquet(fp).tail(10))
            else:
                print(open(fp, encoding="utf-8").read()[-2000:])
        except Exception as e:
            print("   ERR", type(e).__name__, e)

# 龙虎榜里有没有自选股
print("\n=== 龙虎榜 / 自选股 ===")
dp = os.path.join(DATA, "dragon_tiger")
if os.path.isdir(dp):
    for dirpath, dirnames, filenames in os.walk(dp):
        for f in filenames:
            if not f.endswith(".parquet"):
                continue
            fp = os.path.join(dirpath, f)
            try:
                df = pl.read_parquet(fp)
            except Exception:
                continue
            sc = [c for c in df.columns if c in ("symbol", "code")]
            if sc and "date" in df.columns:
                hit = df.filter(pl.col(sc[0]).is_in(WL))
                if hit.height:
                    print(f"-- {os.path.relpath(fp, dp)} 命中 {hit.height} 行")
                    print(hit.tail(20))
