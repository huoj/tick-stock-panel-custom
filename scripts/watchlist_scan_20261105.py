# -*- coding: utf-8 -*-
"""自选股当日扫描（2026-11-05 收盘）。

只读：不改任何数据。输出 UTF-8 JSON 到 stdout。
"""
import json
import os
import sys
from datetime import date

import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

WATCH = ["159529.SZ", "159682.SZ", "159586.SZ", "601633.SH", "300059.SZ",
         "600779.SH", "000333.SZ", "000651.SZ", "605366.SH", "600941.SH"]

ASOF = "2026-11-05"
LOOKBACK = 300  # 交易日


def load_daily_panel(from_date=None, to_date=None):
    """读取 kline_daily_enriched 全市场面板（date, symbol, close, volume, amount, turnover_rate）。"""
    base = os.path.join(DATA, "kline_daily_enriched")
    dates = sorted([d[5:] for d in os.listdir(base) if d.startswith("date=")])
    dates = [d for d in dates if (from_date is None or d >= from_date) and (to_date is None or d <= to_date)]
    frames = []
    for d in dates:
        p = os.path.join(base, f"date={d}", "part.parquet")
        if not os.path.exists(p):
            continue
        try:
            frames.append(pl.read_parquet(p))
        except Exception:
            continue
    if not frames:
        return pl.DataFrame()
    df = pl.concat(frames, how="vertical_relaxed")
    if df.schema.get("date") not in (pl.String, pl.Utf8):
        df = df.with_columns(pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"))
    return df


def find_col(df, *keys):
    """按子串候选找列名；找不到返回 None。"""
    for k in keys:
        for c in df.columns:
            if k in c:
                return c
    return None


def ext(name):
    p = os.path.join(DATA, "ext_data", name, "part.parquet")
    if not os.path.exists(p):
        return None
    try:
        return pl.read_parquet(p)
    except Exception:
        return None


def rsi(series, n):
    """Wilder RSI。"""
    import math
    vals = series.to_list()
    if len(vals) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def ema(vals, n):
    k = 2.0 / (n + 1)
    out = []
    prev = None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def main():
    # ---- 全市场面板 ----
    panel = load_daily_panel()
    if panel.is_empty():
        print(json.dumps({"error": "no kline data"}, ensure_ascii=False))
        return
    panel = panel.sort(["symbol", "date"])
    dates = sorted(panel["date"].unique().to_list())
    dates = dates[-LOOKBACK:]
    panel = panel.filter(pl.col("date").is_in(dates))

    # ---- 市场基准：等权中位数日收益 ----
    piv = panel.select(["date", "symbol", "close"]).pivot(index="date", columns="symbol", values="close", aggregate_function="last")
    syms = [c for c in piv.columns if c != "date"]
    mat = {c: piv[c].to_list() for c in syms}
    dlist = piv["date"].to_list()
    idx = {d: i for i, d in enumerate(dlist)}

    mkt_ret = {}      # date -> median pct
    mkt_lvl = {}      # date -> equal-weight index level
    breadth = {}
    lvl = 1.0
    prev_close = {}
    for i, d in enumerate(dlist):
        rs = []
        up = dn = flat = 0
        for s in syms:
            c = mat[s][i]
            pc = mat[s][i - 1] if i > 0 else None
            if c is None or pc is None or pc == 0:
                continue
            if s not in WATCH and (c is None):
                continue
            r = c / pc - 1.0
            rs.append(r)
            if r > 0.0001:
                up += 1
            elif r < -0.0001:
                dn += 1
            else:
                flat += 1
        if rs:
            rs.sort()
            med = rs[len(rs) // 2] if len(rs) % 2 else (rs[len(rs) // 2 - 1] + rs[len(rs) // 2]) / 2
            mkt_ret[d] = med
            lvl *= (1 + rs[len(rs) // 2])
            mkt_lvl[d] = med
        breadth[d] = {"up": up, "down": dn, "flat": flat, "n": len(rs)}

    def mkt_cum(d, n):
        """市场等权中位数累乘收益 n 日。"""
        i = idx.get(d)
        if i is None or i - n < 0:
            return None
        p = 1.0
        for j in range(i - n + 1, i + 1):
            r = mkt_ret.get(dlist[j])
            if r is None:
                return None
            p *= (1 + r)
        return p - 1.0

    # ---- 行业 / 概念 ----
    hy = ext("ext_hy_ths")
    gn = ext("ext_gn_ths")
    hy_map, gn_map = {}, {}
    if hy is not None:
        icol = find_col(hy, "行业")
        if icol:
            for r in hy.select(["symbol", icol]).iter_rows():
                hy_map[r[0]] = r[1]
    if gn is not None:
        ccol = find_col(gn, "概念")
        if ccol:
            for r in gn.select(["symbol", ccol]).iter_rows():
                gn_map[r[0]] = r[1]

    # 行业当日中位数
    today_rows = panel.filter(pl.col("date") == ASOF)
    prev_date = dates[-2] if len(dates) > 1 else None
    prev_rows = panel.filter(pl.col("date") == prev_date)
    tmap = {r["symbol"]: r for r in today_rows.iter_rows(named=True)}
    pmap = {r["symbol"]: r for r in prev_rows.iter_rows(named=True)}

    ind_ret = {}
    for s, r in tmap.items():
        if s not in pmap:
            continue
        pc = pmap[s]["close"]
        if not pc:
            continue
        ind = hy_map.get(s)
        if not ind:
            continue
        ind_ret.setdefault(ind, []).append(r["close"] / pc - 1.0)
    ind_med = {}
    for k, v in ind_ret.items():
        if len(v) >= 3:
            v.sort()
            ind_med[k] = v[len(v) // 2]

    # ---- 各表 ----
    fundflow = ext("ext_fundflow")
    val = ext("ext_valuation")
    gw = ext("ext_goodwill")
    pl_ = ext("ext_pledge")
    sh = ext("ext_shareholders")
    gd = ext("ext_gdhs_ts")
    ins = ext("ext_insider")
    unl = ext("ext_unlock")
    fc = ext("ext_forecast")
    bt = ext("ext_blocktrade")
    rs_ = ext("ext_research")
    mg = ext("ext_margin")
    div = ext("ext_dividend")
    fh = ext("ext_fundhold")
    zt = ext("ext_ztlimit")

    def pick(df, s):
        if df is None or "symbol" not in df.columns:
            return []
        return df.filter(pl.col("symbol") == s).to_dicts()

    out = {"asof": ASOF, "market": {}, "stocks": []}

    mb = breadth.get(ASOF, {})
    out["market"] = {
        "date": ASOF,
        "median_ret_pct": round(mkt_ret.get(ASOF, 0) * 100, 2),
        "breadth": mb,
        "prev_date": prev_date,
    }

    for s in WATCH:
        rec = {"symbol": s}
        t = tmap.get(s)
        p = pmap.get(s)
        if t is None:
            rec["error"] = "no data for asof"
            out["stocks"].append(rec)
            continue
        rec["name"] = None
        if val is not None:
            row = pick(val, s)
            if row:
                rec["name"] = row[0].get("name")
        if rec["name"] is None and hy is not None:
            row = pick(hy, s)
            ncol = find_col(hy, "名称", "简称")
            if row and ncol:
                rec["name"] = row[0].get(ncol)

        closes = panel.filter(pl.col("symbol") == s).sort("date")
        cs = closes["close"].to_list()
        ds = closes["date"].to_list()
        vols = closes["volume"].to_list()
        amts = closes["amount"].to_list()
        tos = closes["turnover_rate"].to_list()

        rec["close"] = t["close"]
        rec["raw_close"] = t.get("raw_close")
        rec["chg_pct"] = round(t["close"] / p["close"] * 100 - 100, 2) if p and p["close"] else None
        rec["amount_yi"] = round((t["amount"] or 0) / 1e8, 2)
        rec["turnover_rate"] = t.get("turnover_rate")
        rec["quote_ts"] = t.get("quote_ts")

        n = len(cs)
        for w in (5, 20, 60, 120, 250):
            if n > w:
                rec[f"ret_{w}d_pct"] = round(cs[-1] / cs[-1 - w] * 100 - 100, 2)
                mc = mkt_cum(ASOF, w)
                if mc is not None:
                    rec[f"excess_{w}d_pp"] = round(rec[f"ret_{w}d_pct"] - mc * 100, 2)
        for w in (5, 20, 60):
            if n >= w:
                rec[f"ma{w}"] = round(sum(cs[-w:]) / w, 3)
        if n >= 26:
            e12 = ema(cs, 12)
            e26 = ema(cs, 26)
            dif = [a - b for a, b in zip(e12, e26)]
            dea = ema(dif, 9)
            rec["macd_dif"] = round(dif[-1], 3)
            rec["macd_dea"] = round(dea[-1], 3)
            rec["macd_hist"] = round((dif[-1] - dea[-1]) * 2, 3)
        if n >= 15:
            rec["rsi6"] = round(rsi(closes["close"], 6) or 0, 2)
            rec["rsi14"] = round(rsi(closes["close"], 14) or 0, 2)
        # 区间分位
        for w in (60, 120, 250):
            if n >= w:
                seg = cs[-w:]
                lo, hi = min(seg), max(seg)
                rec[f"pctile_{w}"] = round((cs[-1] - lo) / (hi - lo) * 100, 1) if hi > lo else None
                rec[f"hi_{w}"] = round(hi, 2)
                rec[f"lo_{w}"] = round(lo, 2)
        # 量比
        if n >= 21 and vols[-21:-1]:
            av = sum(vols[-21:-1]) / 20
            rec["vol_ratio20"] = round(vols[-1] / av, 2) if av else None
        rec["amt_ratio20"] = round(amts[-1] / (sum(amts[-21:-1]) / 20), 2) if n >= 21 and sum(amts[-21:-1]) else None

        # 相对强弱
        if rec.get("chg_pct") is not None:
            rec["vs_mkt_today_pp"] = round(rec["chg_pct"] - out["market"]["median_ret_pct"], 2)
        ind = hy_map.get(s)
        rec["industry"] = ind
        if ind and ind in ind_med:
            rec["industry_med_pct"] = round(ind_med[ind] * 100, 2)
            if rec.get("chg_pct") is not None:
                rec["vs_industry_pp"] = round(rec["chg_pct"] - ind_med[ind] * 100, 2)
        rec["concepts"] = gn_map.get(s)

        # 估值
        for r in pick(val, s):
            rec["pe_ttm"] = r.get("pe_ttm")
            rec["pb"] = r.get("pb")
            rec["total_mv_yi"] = round((r.get("total_mv") or 0) / 1e8, 1)
            rec["circ_mv_yi"] = round((r.get("circ_mv") or 0) / 1e8, 1)
            rec["val_price"] = r.get("price")

        # 资金流
        ff = pick(fundflow, s)
        if ff:
            ff.sort(key=lambda x: str(x.get("flow_date")))
            r = ff[-1]
            rec["fundflow_date"] = r.get("flow_date")
            rec["main_net_wan"] = round((r.get("main_net") or 0) / 1e4, 1)
            rec["main_pct"] = r.get("main_pct")
            rec["xl_net_wan"] = round((r.get("xl_net") or 0) / 1e4, 1)
            rec["lg_net_wan"] = round((r.get("lg_net") or 0) / 1e4, 1)
            rec["md_net_wan"] = round((r.get("md_net") or 0) / 1e4, 1)
            rec["sm_net_wan"] = round((r.get("sm_net") or 0) / 1e4, 1)

        # 商誉
        g = pick(gw, s)
        if g:
            g.sort(key=lambda x: str(x.get("report_date")))
            r = g[-1]
            rec["goodwill"] = {
                "report_date": r.get("report_date"),
                "goodwill_yi": round((r.get("goodwill") or 0) / 1e8, 2),
                "ratio_pct": r.get("goodwill_ratio"),
                "chg_pct": r.get("goodwill_chg_pct"),
                "risk_flag": r.get("risk_flag"),
                "np_yoy_pct": r.get("np_yoy_pct"),
            }
        # 质押
        pg = pick(pl_, s)
        if pg:
            r = pg[0]
            rec["pledge"] = {"ratio_pct": r.get("pledge_ratio"), "date": r.get("trade_date"),
                             "risk_flag": r.get("risk_flag")}
        # 股东户数
        sm = pick(sh, s)
        if sm:
            sm.sort(key=lambda x: str(x.get("stat_date")))
            r = sm[-1]
            rec["holders"] = {"stat_date": r.get("stat_date"), "holders": r.get("holders"),
                              "chg_pct": r.get("holders_chg_pct")}
        gm = pick(gd, s)
        if gm:
            r = gm[-1]
            rec["gdhs_ts"] = {"holders": r.get("holders"), "chg": r.get("gdhs_chg")}
        # 内部人
        ii = pick(ins, s)
        if ii:
            ii.sort(key=lambda x: str(x.get("trade_date")))
            rec["insider"] = [{"date": r.get("trade_date"), "who": r.get("insider"),
                               "dir": r.get("direction"), "amount_wan": round((r.get("delta_amount") or 0) / 1e4, 1),
                               "pct": r.get("delta_pct")} for r in ii[-4:]]
        # 解禁
        ul = pick(unl, s)
        if ul:
            r = ul[0]
            rec["unlock"] = {"next": r.get("next_unlock"), "max_pct_float": r.get("max_pct_float"),
                             "mv_yi": round((r.get("total_mv") or 0) / 1e8, 2)}
        # 业绩预告
        fo = pick(fc, s)
        if fo:
            rec["forecast"] = [{"period": r.get("period"), "indicator": r.get("indicator"),
                                "change_pct": r.get("change_pct"), "desc": r.get("change_desc")} for r in fo[:3]]
        # 大宗
        b = pick(bt, s)
        if b:
            r = b[0]
            rec["blocktrade"] = {"cnt": r.get("trade_cnt"), "amount_yi": round((r.get("total_amount") or 0) / 1e8, 2),
                                 "premium": r.get("avg_premium"), "last": r.get("last_date")}
        # 研报评级
        rr = pick(rs_, s)
        if rr:
            r = rr[0]
            rec["research"] = {"buy": r.get("buy_cnt"), "add": r.get("add_cnt"),
                               "neutral": r.get("neutral_cnt"), "reduce": r.get("reduce_cnt"),
                               "sell": r.get("sell_cnt"), "rating": r.get("rating")}
        # 两融
        mm = pick(mg, s)
        if mm:
            mm.sort(key=lambda x: str(x.get("margin_date")))
            r = mm[-1]
            rec["margin"] = {"date": r.get("margin_date"),
                             "balance_yi": round((r.get("total_balance") or 0) / 1e8, 2),
                             "net_buy_wan": round((r.get("fin_net_buy") or 0) / 1e4, 1),
                             "net_ratio_pct": r.get("net_ratio_pct")}
        # 分红
        dd = pick(div, s)
        if dd:
            rec["dividend"] = [{"cash_per10": r.get("cash_per10"), "yield": r.get("yield_pct"),
                                "ex_date": r.get("ex_date"), "progress": r.get("progress")} for r in dd]
        # 基金持仓
        f = pick(fh, s)
        if f:
            r = f[0]
            rec["fundhold"] = {"count": r.get("fund_count"), "shares_yi": round((r.get("hold_shares") or 0) / 1e8, 2),
                               "mv_yi": round((r.get("hold_mv") or 0) / 1e8, 2),
                               "change_pct": r.get("change_pct")}
        # 涨停
        z = pick(zt, s)
        if z:
            rec["ztlimit"] = [{"date": r.get("zt_date"), "boards": r.get("boards"), "reason": r.get("reason")} for r in z[:5]]

        out["stocks"].append(rec)

    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
