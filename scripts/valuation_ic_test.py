"""低估值因子 IC 检验
用 balance_sheet(净资产) + income(净利润, 累计口径) + 价格 + 股本 重建历史 PB/PE，
按 PIT(公告日)对齐后做横截面 IC 检验。
"""
import datetime as dt
import glob
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(r"D:\deepseek_path\tick-stock-panel\data")
FIN = ROOT / "financials"


def _load_prices() -> pl.DataFrame:
    files = glob.glob(str(ROOT / "kline_daily_enriched" / "**" / "*.parquet"), recursive=True)
    df = (
        pl.scan_parquet(files)
        .select(["symbol", "date", "close"])
        .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        .collect()
    )
    return df.with_columns(pl.col("date").cast(pl.Date))


def _load_shares() -> pl.DataFrame:
    inst = pl.read_parquet(ROOT / "instruments" / "instruments.parquet")
    return (
        inst.select(["symbol", "total_shares"])
        .filter(pl.col("total_shares").is_not_null() & (pl.col("total_shares") > 0))
    )


def _load_bs() -> pl.DataFrame:
    df = pl.read_parquet(FIN / "balance_sheet" / "part.parquet")
    return (
        df.select(["symbol", "period_end", "announce_date", "total_equity"])
        .with_columns(
            pl.col("period_end").str.slice(0, 10).str.to_date("%Y-%m-%d"),
            pl.col("announce_date").str.slice(0, 10).str.to_date("%Y-%m-%d"),
        )
        .filter(pl.col("total_equity").is_not_null())
    )


def _load_income() -> pl.DataFrame:
    """income 的 net_income 是年内累计口径(YTD)，需要还原单季再滚动 12 个月。"""
    df = pl.read_parquet(FIN / "income" / "part.parquet")
    out = (
        df.select(["symbol", "period_end", "announce_date", "net_income"])
        .with_columns(
            pl.col("period_end").str.slice(0, 10).str.to_date("%Y-%m-%d"),
            pl.col("announce_date").str.slice(0, 10).str.to_date("%Y-%m-%d"),
        )
        .filter(pl.col("net_income").is_not_null())
    )
    # 单季 = 本季累计 - 上季累计（Q1 即自身）
    out = out.with_columns(
        pl.col("period_end").dt.year().alias("_y"),
        pl.col("period_end").dt.quarter().alias("_q"),
    ).sort(["symbol", "period_end"])
    out = out.with_columns(
        pl.col("net_income").shift(1).over("symbol").alias("_prev_cum"),
        pl.col("_q").shift(1).over("symbol").alias("_prev_q"),
    )
    out = out.with_columns(
        pl.when(pl.col("_q") == 1)
        .then(pl.col("net_income"))
        .when((pl.col("_prev_q") == pl.col("_q") - 1))
        .then(pl.col("net_income") - pl.col("_prev_cum"))
        .otherwise(None)
        .alias("_q_income")
    )
    # TTM = 最近 4 个单季之和
    out = out.with_columns(
        pl.col("_q_income").rolling_sum(4, min_samples=4).over("symbol").alias("_ttm_income")
    )
    return out.select(["symbol", "period_end", "announce_date", "_ttm_income"])


def month_ends(dates: pl.Series) -> list:
    d = pl.DataFrame({"date": dates}).sort("date")
    return (
        d.group_by(pl.col("date").dt.truncate("1mo").alias("m"))
        .agg(pl.col("date").max())
        .sort("m")["date"]
        .to_list()
    )


def main() -> None:
    print("加载数据 ...")
    px = _load_prices()
    shares = _load_shares()
    bs = _load_bs()
    inc = _load_income()

    print(f"  价格 {px.height} 行 / {px['symbol'].n_unique()} 股")
    print(f"  净资产 {bs.height} 行 / {bs['period_end'].n_unique()} 期")
    print(f"  净利润TTM {inc.height} 行")

    # 每股净资产 BPS 与 每股收益 TTM EPS
    bs2 = bs.join(shares, on="symbol", how="inner").with_columns(
        (pl.col("total_equity") / pl.col("total_shares")).alias("bps")
    ).select(["symbol", "period_end", "announce_date", "bps"])

    inc2 = inc.join(shares, on="symbol", how="inner").with_columns(
        (pl.col("_ttm_income") / pl.col("total_shares")).alias("eps_ttm")
    ).select(["symbol", "period_end", "announce_date", "eps_ttm"])

    # 抽样核对
    chk = bs2.filter(pl.col("symbol") == "600519.SH").sort("period_end").tail(2)
    print("\n[核对] 茅台 BPS:", chk["bps"].to_list())
    chk2 = inc2.filter(pl.col("symbol") == "600519.SH").sort("period_end").tail(2)
    print("[核对] 茅台 EPS(TTM):", chk2["eps_ttm"].to_list())

    # 月末调仓日（只取有足够历史的）
    mes = [d for d in month_ends(px["date"]) if d >= dt.date(2025, 1, 1)]
    print(f"\n调仓日 {len(mes)} 个: {mes[0]} ~ {mes[-1]}")

    rows = []
    for i, d in enumerate(mes[:-1]):
        d_next = mes[i + 1]
        # 当日截面价格
        snap = px.filter(pl.col("date") == d).select(["symbol", "close"])
        if snap.height < 100:
            continue
        # 下一期价格（用于前视收益）
        nxt = px.filter(pl.col("date") == d_next).select(
            ["symbol", pl.col("close").alias("close_next")]
        )
        # PIT 取公告日 <= d 的最近一期
        b = bs2.filter(pl.col("announce_date") <= d).sort(["symbol", "period_end"]).group_by("symbol").last()
        e = inc2.filter(pl.col("announce_date") <= d).sort(["symbol", "period_end"]).group_by("symbol").last()
        if b.height < 100:
            continue
        m = (
            snap.join(b.select(["symbol", "bps"]), on="symbol", how="inner")
            .join(e.select(["symbol", "eps_ttm"]), on="symbol", how="left")
            .join(nxt, on="symbol", how="inner")
        )
        m = m.filter(
            (pl.col("bps") > 0)
            & (pl.col("close_next") > 0)
            & (pl.col("close") > 0)
        ).with_columns(
            (pl.col("close_next") / pl.col("close") - 1).alias("fwd_ret"),
            (pl.col("bps") / pl.col("close")).alias("bp"),      # 账面市值比（越高越便宜）
            (pl.col("close") / pl.col("bps")).alias("pb"),      # 市净率
        )
        m = m.with_columns(
            pl.when(pl.col("eps_ttm") > 0)
            .then(pl.col("eps_ttm") / pl.col("close"))
            .otherwise(None)
            .alias("ep")
        )
        m = m.with_columns(pl.lit(d).alias("rebal_date"))
        rows.append(m)

    if not rows:
        print("无有效调仓样本")
        return

    panel = pl.concat(rows)
    print(f"面板 {panel.height} 行, {panel['rebal_date'].n_unique()} 期")
    panel.write_parquet(ROOT / "logs" / "valuation_ic_panel.parquet")

    def ic_stats(factor: str) -> dict:
        sub = panel.filter(pl.col(factor).is_not_null() & pl.col("fwd_ret").is_not_null())
        ics = []
        for d, g in sub.group_by("rebal_date"):
            if g.height < 50:
                continue
            r = g.select(
                pl.corr(pl.col(factor).rank(), pl.col("fwd_ret").rank()).alias("ic")
            )["ic"][0]
            if r is not None and np.isfinite(r):
                ics.append(float(r))
        if not ics:
            return {"n": 0}
        a = np.array(ics)
        return {
            "n": len(a),
            "ic_mean": a.mean(),
            "ic_std": a.std(ddof=1) if len(a) > 1 else np.nan,
            "ir": a.mean() / a.std(ddof=1) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan,
            "win_rate": (a > 0).mean(),
            "t": a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 and a.std(ddof=1) > 0 else np.nan,
        }

    print("\n" + "=" * 62)
    print("IC 检验结果（Spearman 秩相关，月末调仓，前视 1 个月收益）")
    print("=" * 62)
    print(f"{'因子':<10}{'期数':>5}{'IC均值':>10}{'IC标准差':>11}{'IR':>9}{'IC胜率':>9}{'t值':>8}")
    for f, label in [("bp", "账面市值比"), ("ep", "盈利收益率"), ("pb", "市净率"), ("close", "价格")]:
        s = ic_stats(f)
        if s.get("n", 0) == 0:
            print(f"{label:<10} 无样本")
            continue
        print(
            f"{label:<10}{s['n']:>5}{s['ic_mean']:>10.4f}{s['ic_std']:>11.4f}"
            f"{s['ir']:>9.3f}{s['win_rate']*100:>8.1f}%{s['t']:>8.2f}"
        )

    # 分组收益（按 BP 分 5 组）
    print("\n" + "=" * 62)
    print("分组年化收益（按「账面市值比」分 5 组，Q1=最贵 Q5=最便宜）")
    print("=" * 62)
    grp = []
    for d, g in panel.filter(pl.col("bp").is_not_null()).group_by("rebal_date"):
        if g.height < 50:
            continue
        g = g.with_columns(
            pl.col("bp").qcut(5, labels=[f"Q{i}" for i in range(1, 6)], allow_duplicates=True).alias("grp")
        )
        agg = g.group_by("grp").agg(pl.col("fwd_ret").mean().alias("ret"))
        for r in agg.iter_rows(named=True):
            grp.append({"rebal_date": d, "grp": r["grp"], "ret": r["ret"]})
    if grp:
        gdf = pl.DataFrame(grp)
        summ = (
            gdf.group_by("grp")
            .agg([
                (pl.col("ret").mean() * 12 * 100).alias("年化%"),
                (pl.col("ret").std(ddof=1) * np.sqrt(12) * 100).alias("年化波动%"),
                (pl.col("ret") > 0).mean().alias("胜率"),
            ])
            .sort("grp")
        )
        for r in summ.iter_rows(named=True):
            print(
                f"  {r['grp']}  年化 {r['年化%']:>7.2f}%   波动 {r['年化波动%']:>6.2f}%   "
                f"月胜率 {r['胜率']*100:>5.1f}%"
            )
        q1 = summ.filter(pl.col("grp") == "Q1")["年化%"][0]
        q5 = summ.filter(pl.col("grp") == "Q5")["年化%"][0]
        print(f"\n  多空(Q5-Q1) 年化差: {q5 - q1:+.2f} 个百分点/年")


if __name__ == "__main__":
    main()
