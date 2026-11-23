"""强弱判定「反向」归因诊断 —— 逐维度拆解预测力。

背景: ``strength_validation.validate_across_symbols`` 在 74 只票 / 2660 采样点上
发现「偏强」档 5 日 / 10 日跑输基准 6.9pp / 7.3pp (inverted)。按标的整块
bootstrap: 5/10 日显著 (p=0.015 / 0.008)，20 日 -2.8pp / p=0.345 **不显著**。
早期宣称的「20 日跑输 9.1pp 且三期方向一致」在本批样本上不成立，本脚本
回答**为什么**，并把这条纠正一并留在结论里。

做法 (不改任何生产代码, 只读本地 parquet):
1. 用与线上完全相同的 ``diag.diagnose(df[:i+1])`` 逐点算判定 (严格无前视);
2. 同时把 7 个维度各自的投票 (+1/-1/0) 显式重算一遍, 并用
   ``net == len(bullish)-len(bearish)`` 自校验, 保证拆解忠实于生产规则;
3. 逐维度检验单变量预测力 (该维度投 +1 的样本前瞻收益 vs 全样本基准);
4. 检验四个竞争假说: 短期反转 / 样本期偏差 / 维度内部冲突 / 统计显著性。

用法: cd backend; .venv\\Scripts\\python.exe scripts/diagnose_strength_inversion.py
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import stock_diagnostics as diag

DATA = Path(__file__).resolve().parents[2] / "data"
MIN_HISTORY = 130
STEP = 10
FW = (5, 10, 20)
LIMIT = 76


# ── 1. 数据装载 (与线上同批面板) ─────────────────────────

def load_panels(limit: int = LIMIT) -> dict[str, pl.DataFrame]:
    inst = pl.read_parquet(DATA / "instruments" / "instruments.parquet")
    all_syms = sorted(str(s) for s in inst["symbol"].to_list())
    stride = max(1, len(all_syms) // limit)
    sample = all_syms[::stride][:limit]

    files = sorted(glob.glob(str(DATA / "kline_daily" / "date=*" / "part*.parquet")))
    frames = []
    for f in files:
        try:
            frames.append(
                pl.read_parquet(f).select(
                    ["symbol", "date", "open", "high", "low", "close", "volume"]
                )
            )
        except Exception:  # noqa: BLE001 - 单个坏分区不该拖垮整批归因
            continue
    big = pl.concat(frames, how="diagonal_relaxed")
    panels: dict[str, pl.DataFrame] = {}
    for sym in sample:
        d = (
            big.filter(pl.col("symbol") == sym)
            .unique(subset=["date"])
            .sort("date")
        )
        if d.height >= MIN_HISTORY + max(FW) + 1:
            panels[sym] = d
    return panels, all_syms, sample


# ── 2. 7 个维度的显式投票 (与 judge_strength 同阈值) ─────

def dimension_votes(candle: dict, trend: dict, ind: dict, chip: dict) -> dict[str, int]:
    """返回 7 维各自的净票; 净票之和应等于 len(bullish)-len(bearish)。"""
    votes: dict[str, int] = {}

    # 1 结构
    s = trend["structure"]
    votes["d1_structure"] = 1 if s == "uptrend" else (-1 if s == "downtrend" else 0)
    # uptrend_pullback 同时给 bullish+bearish → 净 0

    # 2 MACD 动能
    votes["d2_macd"] = 1 if ind["macd_hist"] > 0 else -1

    # 3 RSI 极值 (反向判据)
    r = ind["rsi14"]
    votes["d3_rsi"] = -1 if r >= 70 else (1 if r <= 30 else 0)

    # 4 量价配合
    ratio = candle.get("volume_ratio")
    v = 0
    if ratio is not None:
        if candle["is_up"] and candle["volume_state"] == "expand":
            v = 1
        elif candle["is_up"] and candle["volume_state"] == "shrink":
            v = 1 if trend["position_in_250_pct"] < 30 else 0
        elif not candle["is_up"] and candle["volume_state"] in ("shrink", "expand"):
            v = -1
    votes["d4_volprice"] = v

    # 5 收盘位置
    pos = candle["close_position"]
    votes["d5_closepos"] = 1 if pos >= 80 else (-1 if pos <= 20 else 0)

    # 6 250 日区间位置 (生产规则里只给 neutral, 不投方向票)
    votes["d6_position250"] = 0

    # 7 头顶筹码
    above = chip.get("densest_above")
    votes["d7_chips"] = -1 if (above and above["total_pct"] >= 25) else 0

    return votes


# ── 3. 逐采样点采集 ─────────────────────────────────────

def collect(panels: dict[str, pl.DataFrame]) -> tuple[list[dict], int]:
    rows: list[dict] = []
    mismatch = 0
    for sym, d in panels.items():
        close = d["close"].to_numpy().astype(float)
        dates = d["date"].to_list()
        n = d.height
        for i in range(MIN_HISTORY, n - max(FW), STEP):
            window = d.slice(0, i + 1)
            try:
                dia = diag.diagnose(window)
            except ValueError:
                continue
            strength = dia["strength"]
            candle, trend, ind, chip = (
                dia["candle"], dia["trend"], dia["indicators"], dia["chips"]
            )
            votes = dimension_votes(candle, trend, ind, chip)
            net_recomputed = sum(votes.values())
            net_official = len(strength["bullish_evidence"]) - len(strength["bearish_evidence"])
            if net_recomputed != net_official:
                mismatch += 1

            rec: dict = {
                "symbol": sym,
                "date": str(dates[i]),
                "i": i,
                "stance": strength["stance"],
                "net": net_official,
                "votes": votes,
                "change_pct": candle["change_pct"],
                "volume_state": candle["volume_state"],
                "rsi14": ind["rsi14"],
                "macd_hist": ind["macd_hist"],
                "close_position": candle["close_position"],
                "position_in_250_pct": trend["position_in_250_pct"],
                "structure": trend["structure"],
            }
            for w in FW:
                rec[f"fwd{w}"] = float(close[i + w] / close[i] - 1) * 100
            for w in (5, 10, 20):
                rec[f"past{w}"] = (
                    float(close[i] / close[i - w] - 1) * 100 if i - w >= 0 else None
                )
            rows.append(rec)
    return rows, mismatch


# ── 4. 统计工具 ─────────────────────────────────────────

def stats(vals: list[float]) -> dict:
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    if not a.size:
        return {"n": 0}
    return {
        "n": int(a.size),
        "mean": round(float(a.mean()), 2),
        "win": round(float((a > 0).mean() * 100), 1),
    }


def edge(vals: list[float], base: list[float]) -> dict:
    g, b = stats(vals), stats(base)
    if not g["n"] or not b["n"]:
        return {"n": 0}
    return {
        "n": g["n"],
        "mean": g["mean"],
        "win": g["win"],
        "d_mean": round(g["mean"] - b["mean"], 2),
        "d_win": round(g["win"] - b["win"], 1),
    }


def pearson(x: list[float], y: list[float]) -> float:
    pairs = [(a, b) for a, b in zip(x, y, strict=True)
             if a is not None and b is not None and np.isfinite(a) and np.isfinite(b)]
    if len(pairs) < 10:
        return float("nan")
    xa = np.array([p[0] for p in pairs])
    ya = np.array([p[1] for p in pairs])
    if xa.std() == 0 or ya.std() == 0:
        return float("nan")
    return round(float(np.corrcoef(xa, ya)[0, 1]), 3)


# ── 5. 主分析 ───────────────────────────────────────────

def main() -> None:
    panels, all_syms, sample = load_panels()
    rows, mismatch = collect(panels)
    fwd = {w: [r[f"fwd{w}"] for r in rows] for w in FW}
    base = {w: stats(fwd[w]) for w in FW}

    out: dict = {
        "meta": {
            "instruments_total": len(all_syms),
            "symbols_sampled": len(sample),
            "symbols_with_data": len(panels),
            "sampled_points": len(rows),
            "step": STEP,
            "min_history": MIN_HISTORY,
            "date_range": [min(r["date"] for r in rows), max(r["date"] for r in rows)],
            "vote_recompute_mismatch": mismatch,
        },
        "baseline": {str(w): base[w] for w in FW},
    }

    # (A) 复现分档反向
    out["A_stance"] = {}
    for stance in ("strong", "weak", "unclear"):
        sub = [r for r in rows if r["stance"] == stance]
        out["A_stance"][stance] = {
            str(w): edge([r[f"fwd{w}"] for r in sub], fwd[w]) for w in FW
        }
        out["A_stance"][stance]["past20_mean"] = stats([r["past20"] for r in sub]).get("mean")
        out["A_stance"][stance]["change_pct_mean"] = stats([r["change_pct"] for r in sub]).get("mean")

    # (B) 逐维度单变量预测力 (vote=+1 组 / -1 组 相对全样本)
    out["B_dimensions"] = {}
    for dim in ("d1_structure", "d2_macd", "d3_rsi", "d4_volprice",
                "d5_closepos", "d6_position250", "d7_chips"):
        plus = [r for r in rows if r["votes"][dim] == 1]
        minus = [r for r in rows if r["votes"][dim] == -1]
        out["B_dimensions"][dim] = {
            "plus": {str(w): edge([r[f"fwd{w}"] for r in plus], fwd[w]) for w in FW},
            "minus": {str(w): edge([r[f"fwd{w}"] for r in minus], fwd[w]) for w in FW},
        }

    # (C) 反转效应检验: 前期涨幅 → 前瞻收益; 净票数 → 前期涨幅
    out["C_reversal"] = {
        "corr_past20_fwd20": pearson([r["past20"] for r in rows], [r["fwd20"] for r in rows]),
        "corr_past5_fwd5": pearson([r["past5"] for r in rows], [r["fwd5"] for r in rows]),
        "corr_change_fwd5": pearson([r["change_pct"] for r in rows], [r["fwd5"] for r in rows]),
        "corr_net_past20": pearson([float(r["net"]) for r in rows], [r["past20"] for r in rows]),
        "corr_net_change": pearson([float(r["net"]) for r in rows], [r["change_pct"] for r in rows]),
    }
    # 按前期 20 日涨幅五分组
    p20 = sorted(r["past20"] for r in rows if r["past20"] is not None)
    qs = [np.percentile(p20, q) for q in (20, 40, 60, 80)]
    out["C_reversal"]["quintile_fwd20"] = []
    bounds = [-1e9, *qs, 1e9]
    for k in range(5):
        sub = [r for r in rows if r["past20"] is not None
               and bounds[k] <= r["past20"] < bounds[k + 1]]
        out["C_reversal"]["quintile_fwd20"].append({
            "bucket": k + 1,
            "past20_range": [round(bounds[k], 1), round(bounds[k + 1], 1)],
            **edge([r["fwd20"] for r in sub], fwd[20]),
        })

    # (D) 时间稳定性: 样本期前后半
    dts = sorted(r["date"] for r in rows)
    mid = dts[len(dts) // 2]
    out["D_time_split"] = {"mid_date": mid}
    for tag, sub in (("first_half", [r for r in rows if r["date"] < mid]),
                     ("second_half", [r for r in rows if r["date"] >= mid])):
        bf = {w: [r[f"fwd{w}"] for r in sub] for w in FW}
        strong = [r for r in sub if r["stance"] == "strong"]
        out["D_time_split"][tag] = {
            "n": len(sub),
            "strong_n": len(strong),
            "strong_d_win": {
                str(w): edge([r[f"fwd{w}"] for r in strong], bf[w]).get("d_win") for w in FW
            },
        }

    # (E) 显著性: 按标的 block bootstrap (三窗口都算, 双侧 p 值)
    # 早期版本把 p 值写成"离 bootstrap 均值的距离", 因 observed≈均值会得到 ≈1,
    # 那是错的定义。正确做法: H0 = 超额为 0, p = 2*min(P(boot<=0), P(boot>=0))。
    rng = np.random.default_rng(20261113)
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    syms = list(by_sym)
    boot: dict[int, list[float]] = {w: [] for w in FW}
    for _ in range(2000):
        pick = rng.choice(syms, size=len(syms), replace=True)
        pool = [r for s in pick for r in by_sym[s]]
        srows = [r for r in pool if r["stance"] == "strong"]
        if not srows:
            continue
        for w in FW:
            sw = float(np.mean([r[f"fwd{w}"] > 0 for r in srows]) * 100)
            aw = float(np.mean([r[f"fwd{w}"] > 0 for r in pool]) * 100)
            boot[w].append(sw - aw)
    out["E_significance"] = {}
    for w in FW:
        arr = np.array(boot[w])
        observed = out["A_stance"]["strong"][str(w)]["d_win"]
        p_two = 2.0 * min(float((arr <= 0).mean()), float((arr >= 0).mean()))
        out["E_significance"][str(w)] = {
            "observed_d_win": observed,
            "boot_mean": round(float(arr.mean()), 2),
            "ci95": [round(float(np.percentile(arr, 2.5)), 2),
                     round(float(np.percentile(arr, 97.5)), 2)],
            "p_two_sided": round(min(p_two, 1.0), 4),
            "significant_at_5pct": bool(np.percentile(arr, 2.5) > 0
                                        or np.percentile(arr, 97.5) < 0),
        }
    out["E_significance"]["note"] = (
        "按标的整块重采样 2000 次, 保留同一票内部重叠窗口的相关性; "
        "p_two_sided = 2*min(P(boot<=0), P(boot>=0))"
    )

    dims = ("d1_structure", "d2_macd", "d3_rsi", "d4_volprice",
            "d5_closepos", "d6_position250", "d7_chips")

    # (F) 消融: 逐个维度剔除后重新分档, 看 strong 超额如何变化。
    # drop=None 用 7 维净票重算, 应与官方 stance 完全一致 (校验拆解忠实度)。
    def restance(r: dict, drop: str | None = None, flip: str | None = None) -> str:
        net = 0
        for k, v in r["votes"].items():
            if k == drop:
                continue
            net += -v if k == flip else v
        return "strong" if net >= 2 else ("weak" if net <= -2 else "unclear")

    agrees = sum(1 for r in rows if restance(r) == r["stance"])
    out["F_ablation"] = {
        "_recompute_agreement": f"{agrees}/{len(rows)}",
        "none": None,
    }
    for dim in (*dims, None):
        strong = [r for r in rows if restance(r, dim) == "strong"]
        out["F_ablation"][dim or "none"] = {
            "strong_n": len(strong),
            "d_win": {
                str(w): edge([r[f"fwd{w}"] for r in strong], fwd[w]).get("d_win")
                for w in FW
            },
        }
    flipped = [r for r in rows if restance(r, flip="d5_closepos") == "strong"]
    out["F_ablation"]["flip_d5_closepos"] = {
        "strong_n": len(flipped),
        "d_win": {
            str(w): edge([r[f"fwd{w}"] for r in flipped], fwd[w]).get("d_win")
            for w in FW
        },
    }

    # (G) 控制当日涨幅后, d5 是否仍反向 —— 排除"收盘高位只是当天大涨的代理"
    out["G_d5_controlled"] = []
    for lo, hi in ((-100, -2), (-2, 0), (0, 2), (2, 5), (5, 100)):
        sub = [r for r in rows if lo <= r["change_pct"] < hi]
        local5 = [r["fwd5"] for r in sub]
        hi_cp = [r for r in sub if r["votes"]["d5_closepos"] == 1]
        lo_cp = [r for r in sub if r["votes"]["d5_closepos"] == -1]
        out["G_d5_controlled"].append({
            "change_bucket": [lo, hi],
            "n": len(sub),
            "hi_close_n": len(hi_cp),
            "hi_close_d_win5": edge([r["fwd5"] for r in hi_cp], local5).get("d_win"),
            "lo_close_n": len(lo_cp),
            "lo_close_d_win5": edge([r["fwd5"] for r in lo_cp], local5).get("d_win"),
        })

    # (H) 各维度触发率 + strong 组由哪些维度推上去的
    strong_rows = [r for r in rows if r["stance"] == "strong"]
    out["H_dimension_profile"] = {}
    for dim in dims:
        out["H_dimension_profile"][dim] = {
            "all_pct_plus": round(100 * float(np.mean([r["votes"][dim] == 1 for r in rows])), 1),
            "all_pct_minus": round(100 * float(np.mean([r["votes"][dim] == -1 for r in rows])), 1),
            "strong_pct_plus": round(100 * float(np.mean([r["votes"][dim] == 1 for r in strong_rows])), 1),
            "strong_pct_minus": round(100 * float(np.mean([r["votes"][dim] == -1 for r in strong_rows])), 1),
        }

    # (I) 与线上 validate_across_symbols 在同一批面板上对照, 确认拆解未偏离实现
    from app.services import strength_validation as sv
    online = sv.validate_across_symbols(panels, step=STEP, max_symbols=len(panels))
    out["I_online_crosscheck"] = {
        "symbols_used": online["symbols_used"],
        "baseline": online["baseline"],
        "groups": [
            {
                "stance": g["stance"],
                "samples": g["samples"],
                "verdict": g["verdict"],
                "d_win": {
                    str(w): (round(g["forward"][str(w)]["win_rate"]
                                   - online["baseline"][str(w)]["win_rate"], 1)
                             if g["forward"][str(w)].get("win_rate") is not None
                             and online["baseline"][str(w)].get("win_rate") is not None
                             else None)
                    for w in FW
                },
            }
            for g in online["groups"]
        ],
    }

    dest = Path(__file__).resolve().parents[2] / "docs" / "强弱规则反向归因.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n[written] {dest}")


if __name__ == "__main__":
    main()
