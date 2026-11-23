# -*- coding: utf-8 -*-
"""龙虎榜 JSON、depth5、regime 全字段、screener 结果。只读。"""
import json
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
WL = {"159529", "159682", "159586", "601633", "300059", "600779",
      "000333", "000651", "605366", "600941"}

# ---- 1. 龙虎榜 JSON ----
print("=== 龙虎榜最新一日结构 + 自选股命中 ===")
dp = os.path.join(DATA, "dragon_tiger")
files = sorted([f for f in os.listdir(dp) if f.endswith(".json")])
for f in files[-2:]:
    obj = json.load(open(os.path.join(dp, f), encoding="utf-8"))
    print(f"-- {f}  type={type(obj).__name__}")
    if isinstance(obj, dict):
        print("   keys:", list(obj.keys())[:20])
        items = obj.get("items") or obj.get("data") or obj.get("list")
    else:
        items = obj
    if isinstance(items, list) and items:
        print("   样本字段:", list(items[0].keys()) if isinstance(items[0], dict) else items[0])
        print("   条数:", len(items))
        hits = [it for it in items if isinstance(it, dict)
                and any(str(it.get(k, ""))[:6] in WL for k in ("code", "symbol", "ts_code"))]
        print("   自选股命中:", len(hits))
        for h in hits[:5]:
            print("     ", h)

# ---- 2. depth5 11-05 ----
print("\n=== depth5 2026-11-05 ===")
p = os.path.join(DATA, "depth5", "date=2026-11-05", "part.parquet")
if os.path.exists(p):
    df = pl.read_parquet(p)
    print("schema:", df.schema)
    print("rows:", df.height)
    print(df.head(40))

# ---- 3. regime 全字段 ----
print("\n=== regime_history 近 4 日全字段 ===")
rp = os.path.join(DATA, "regime_history", "part.parquet")
df = pl.read_parquet(rp)
print("cols:", df.columns)
sub = df.tail(4)
for r in sub.iter_rows(named=True):
    print("--", r.get("date"))
    for k, v in r.items():
        if k != "date":
            print(f"     {k} = {v}")

# ---- 4. screener_results ----
print("\n=== screener_results ===")
sp = os.path.join(DATA, "screener_results")
if os.path.isdir(sp):
    for dirpath, dirnames, filenames in os.walk(sp):
        for f in sorted(filenames)[-6:]:
            fp = os.path.join(dirpath, f)
            import datetime
            mt = datetime.datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%m-%d %H:%M")
            print(f"   {os.path.relpath(fp, sp):<55} {os.path.getsize(fp):>9} {mt}")
