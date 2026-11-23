# -*- coding: utf-8 -*-
"""核对 159682 跟踪指数 + 自选股近 6 日明细。只读。"""
import os

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dated(base, last_n=None, cols=None):
    """稳健读取 date= 分区（各文件 schema 可能不一致）。"""
    if not os.path.isdir(base):
        return None
    ds = sorted([d[5:] for d in os.listdir(base) if d.startswith("date=")])
    if last_n:
        ds = ds[-last_n:]
    fr = []
    for d in ds:
        p = os.path.join(base, f"date={d}", "part.parquet")
        if os.path.exists(p):
            fr.append(pl.read_parquet(p))
    if not fr:
        return None
    df = pl.concat(fr, how="diagonal_relaxed")
    if "date" in df.columns and df.schema["date"] not in (pl.String, pl.Utf8):
        df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
    if cols:
        keep = [c for c in cols if c in df.columns]
        df = df.select(keep)
    return df


# ---- 1. 指数序列 ----
print("== 指数数据 ==")
ib = os.path.join(ROOT, "data", "kline_index_enriched")
df = load_dated(ib, last_n=200)
if df is None:
    print("kline_index_enriched 不可用")
else:
    syms = sorted(set(df["symbol"].to_list()))
    print(f"共 {len(syms)} 个指数")
    for s in syms:
        if any(x in s for x in ("399001", "399006", "399673", "000001", "000300", "399005")):
            print("   候选:", s)
    for sym in ["399006.SZ", "399673.SZ", "000001.SH", "399001.SZ", "000300.SH"]:
        sub = df.filter(pl.col("symbol") == sym).sort("date")
        if sub.height == 0:
            continue
        cs = sub["close"].to_list()
        dsx = sub["date"].to_list()
        out = [f"{sym:<12}", f"n={len(cs)}", f"last={dsx[-1]} C={cs[-1]}"]
        for w in (5, 20, 60, 120):
            if len(cs) > w:
                out.append(f"{w}d={cs[-1]/cs[-1-w]*100-100:+.2f}%")
        print("   ", " ".join(out))

# ---- 2. 自选股近 6 日 ----
print("\n== 自选股（A股）近 6 个交易日 ==")
sb = os.path.join(ROOT, "data", "kline_daily_enriched")
STOCKS = ["601633.SH", "300059.SZ", "600779.SH", "000333.SZ", "000651.SZ", "605366.SH", "600941.SH"]
df = load_dated(sb, last_n=8, cols=["symbol", "date", "open", "high", "low", "close", "amount", "turnover_rate"])
for sym in STOCKS:
    sub = df.filter(pl.col("symbol") == sym).sort("date")
    cs = sub["close"].to_list()
    dsx = sub["date"].to_list()
    amt = sub["amount"].to_list()
    print(f"-- {sym}")
    for i in range(max(1, len(cs) - 5), len(cs)):
        chg = cs[i] / cs[i - 1] * 100 - 100
        print(f"     {dsx[i]} C={cs[i]:>8} {chg:+6.2f}%  amount={amt[i]/1e8:.2f}亿")

# ---- 3. 全市场：今日按市值/价格分布看风格 ----
print("\n== 11-05 风格剖面（全市场，按价格分档中位涨幅）==")
df = load_dated(sb, last_n=2, cols=["symbol", "date", "close", "amount"])
ds = sorted(set(df["date"].to_list()))
if len(ds) >= 2:
    cur = df.filter(pl.col("date") == ds[-1])
    prv = df.filter(pl.col("date") == ds[-2])
    m = {r[0]: (r[1], r[2]) for r in prv.select(["symbol", "close", "amount"]).iter_rows()}
    rows = []
    for r in cur.select(["symbol", "close", "amount"]).iter_rows():
        s, c, a = r
        if s in m and m[s][0]:
            rows.append((s, c / m[s][0] * 100 - 100, a or 0))
    # 按成交额分档
    rows.sort(key=lambda x: -x[2])
    n = len(rows)
    print(f"  全市场 n={n}  中位涨幅={sorted(x[1] for x in rows)[n//2]:+.2f}%")
    for label, lo, hi in [("成交额前 5%", 0, 0.05), ("5-20%", 0.05, 0.20), ("20-50%", 0.20, 0.50),
                          ("50-80%", 0.50, 0.80), ("后 20%", 0.80, 1.0)]:
        seg = rows[int(n * lo):int(n * hi)]
        if seg:
            v = sorted(x[1] for x in seg)
            print(f"    {label:<12} n={len(seg):<5} 中位={v[len(v)//2]:+6.2f}%")
