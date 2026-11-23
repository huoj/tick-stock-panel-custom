# -*- coding: utf-8 -*-
"""补充扫描：ETF 技术面 + 行业/概念当日强弱 + 资金流趋势。只读。"""
import glob
import json
import os

import polars as pl


def ext_path(name):
    """ext_data 表：snapshot 用 <t>/part.parquet，timeseries 用 <t>/timeseries/date=*/part.parquet。"""
    base = os.path.join(ROOT, "data", "ext_data", name)
    direct = os.path.join(base, "part.parquet")
    if os.path.exists(direct):
        return direct
    parts = sorted(glob.glob(os.path.join(base, "timeseries", "date=*", "part.parquet")))
    return parts[-1] if parts else None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
ASOF = "2026-11-05"
WATCH = ["159529.SZ", "159682.SZ", "159586.SZ", "601633.SH", "300059.SZ",
         "600779.SH", "000333.SZ", "000651.SZ", "605366.SH", "600941.SH"]
ETF = {"159529.SZ": 1.420, "159682.SZ": 1.127, "159586.SZ": 1.005}   # 外部源 11-05 收盘


def load_etf():
    base = os.path.join(DATA, "kline_etf_enriched")
    ds = sorted([d[5:] for d in os.listdir(base) if d.startswith("date=")])
    fr = []
    for d in ds[-300:]:
        p = os.path.join(base, f"date={d}", "part.parquet")
        if os.path.exists(p):
            fr.append(pl.read_parquet(p))
    df = pl.concat(fr, how="vertical_relaxed")
    if df.schema.get("date") not in (pl.String, pl.Utf8):
        df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
    return df


def ema(vals, n):
    k = 2.0 / (n + 1)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(vals, n):
    if len(vals) < n + 1:
        return None
    g, l = [], []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        g.append(max(ch, 0.0))
        l.append(max(-ch, 0.0))
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)


out = {"asof": ASOF, "etf": [], "industry_rank": [], "concept_top": [], "fundflow": []}

# ---- ETF 技术面 ----
etf = load_etf()
for s in ETF:
    sub = etf.filter(pl.col("symbol") == s).sort("date")
    cs = sub["close"].to_list() + [ETF[s]]
    ds = sub["date"].to_list() + [ASOF]
    vols = sub["volume"].to_list()
    rec = {"symbol": s, "last_local_date": ds[-2], "close_today": ETF[s],
           "prev_close": cs[-2], "chg_pct": round(ETF[s] / cs[-2] * 100 - 100, 2)}
    n = len(cs)
    for w in (5, 20, 60, 120, 250):
        if n > w:
            rec[f"ret_{w}d_pct"] = round(cs[-1] / cs[-1 - w] * 100 - 100, 2)
    for w in (5, 20, 60):
        if n >= w:
            rec[f"ma{w}"] = round(sum(cs[-w:]) / w, 4)
    if n >= 26:
        e12, e26 = ema(cs, 12), ema(cs, 26)
        dif = [a - b for a, b in zip(e12, e26)]
        dea = ema(dif, 9)
        rec["macd_hist"] = round((dif[-1] - dea[-1]) * 2, 4)
    if n >= 15:
        rec["rsi6"] = round(rsi(cs, 6) or 0, 2)
        rec["rsi14"] = round(rsi(cs, 14) or 0, 2)
    for w in (60, 120, 250):
        if n >= w:
            seg = cs[-w:]
            lo, hi = min(seg), max(seg)
            rec[f"pctile_{w}"] = round((cs[-1] - lo) / (hi - lo) * 100, 1) if hi > lo else None
            rec[f"lo_{w}"], rec[f"hi_{w}"] = round(lo, 3), round(hi, 3)
    if n >= 21 and sum(vols[-20:]):
        rec["vol_ratio20"] = round(vols[-1] / (sum(vols[-20:]) / 20), 2)
    out["etf"].append(rec)

# ---- 行业强弱（全市场，11-05）----
hy = pl.read_parquet(ext_path("ext_hy_ths"))
icol = [c for c in hy.columns if "行业" in c][0]
hy_map = {r[0]: r[1] for r in hy.select(["symbol", icol]).iter_rows()}

p5 = pl.read_parquet(os.path.join(DATA, "kline_daily_enriched", "date=2026-11-05", "part.parquet"))
p4 = pl.read_parquet(os.path.join(DATA, "kline_daily_enriched", "date=2026-11-04", "part.parquet"))
m5 = {r[0]: r[1] for r in p5.select(["symbol", "close"]).iter_rows()}
m4 = {r[0]: r[1] for r in p4.select(["symbol", "close"]).iter_rows()}
grp = {}
for s, c in m5.items():
    if s in m4 and m4[s]:
        ind = hy_map.get(s)
        if ind:
            grp.setdefault(ind, []).append(c / m4[s] - 1)
rows = []
for k, v in grp.items():
    if len(v) >= 5:
        v.sort()
        rows.append((k, len(v), round(v[len(v) // 2] * 100, 2)))
rows.sort(key=lambda x: -x[2])
out["industry_rank"] = {"n_industries": len(rows), "top15": rows[:15], "bottom15": rows[-15:]}

# 自选股所属行业排名
rank_of = {r[0]: i + 1 for i, r in enumerate(rows)}
for s in WATCH:
    ind = hy_map.get(s)
    if ind and ind in rank_of:
        out.setdefault("stock_industry_rank", []).append(
            {"symbol": s, "industry": ind, "rank": rank_of[ind], "of": len(rows)})

# ---- 资金流趋势（ext_fundflow 截至 11-04）----
ff = pl.read_parquet(ext_path("ext_fundflow"))
for s in WATCH:
    sub = ff.filter(pl.col("symbol") == s).sort("flow_date")
    if sub.height == 0:
        continue
    tail = sub.tail(5)
    out["fundflow"].append({
        "symbol": s,
        "rows": [{"d": r["flow_date"], "chg": r["chg_pct"],
                  "main_wan": round((r["main_net"] or 0) / 1e4, 1),
                  "main_pct": r["main_pct"],
                  "xl_wan": round((r["xl_net"] or 0) / 1e4, 1),
                  "md_wan": round((r["md_net"] or 0) / 1e4, 1)} for r in tail.iter_rows(named=True)],
    })

print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
