"""样本外(OOS)检验 —— 「d5 收盘位置判据写反」这个结论稳健还是过拟合。

归因脚本发现: d5 投+1(收盘≥80%分位)后续 5/10 日显著跑输、投-1 显著跑赢,
翻转 d5 符号能把 strong 档从反向(-6.9pp)翻成正向(+3.2pp)。但这是在同一批数据上
"发现规则 + 验证规则", 完全可能只是过拟合。本脚本做两次真正的留出检验:

  - **按标的切**: 代码排序后奇偶分成 A/B 两组票, A 组用来"发现", B 组完全不参与;
  - **按时间切**: 前半段日期用来"发现", 后半段日期完全不参与。

检验内容(每个留出集单独算):
  1. d5 投 +1 组的超额胜率是否仍为负 (5d/10d);
  2. d5 投 -1 组的超额胜率是否仍为正;
  3. 翻转 d5 后重算的 strong 档, 是否仍由负(原始)变正(翻转)。

判据: 若方向在留出集里一致复现, 才算稳健; 否则就是过拟合, 不该改生产规则。
只读本地 parquet, 不改任何生产代码。
用法: cd backend; .venv\\Scripts\\python.exe scripts/oos_strength_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import diagnose_strength_inversion as dsi

FW = dsi.FW


def win(vals: list[float]) -> float:
    a = [v for v in vals if v is not None and np.isfinite(v)]
    return float(np.mean([v > 0 for v in a]) * 100) if a else float("nan")


def excess(group: list[dict], pool: list[dict], w: int) -> float:
    """组内胜率 - 池内(基准)胜率, 单位 pp。"""
    if not group or not pool:
        return float("nan")
    return round(win([r[f"fwd{w}"] for r in group]) - win([r[f"fwd{w}"] for r in pool]), 1)


def restance(r: dict, flip_d5: bool = False) -> str:
    net = 0
    for k, v in r["votes"].items():
        if k == "d6_position250":
            continue
        net += -v if (flip_d5 and k == "d5_closepos") else v
    return "strong" if net >= 2 else ("weak" if net <= -2 else "unclear")


def evaluate(rows: list[dict], tag: str) -> dict:
    out: dict = {"tag": tag, "n": len(rows)}
    # 1) d5 单变量方向
    plus = [r for r in rows if r["votes"]["d5_closepos"] == 1]
    minus = [r for r in rows if r["votes"]["d5_closepos"] == -1]
    out["d5_plus_n"] = len(plus)
    out["d5_minus_n"] = len(minus)
    out["d5_plus_excess_pp"] = {str(w): excess(plus, rows, w) for w in FW}
    out["d5_minus_excess_pp"] = {str(w): excess(minus, rows, w) for w in FW}
    # 2) 翻转 d5 后 strong 档表现 vs 原始 strong 档
    strong_orig = [r for r in rows if restance(r) == "strong"]
    strong_flip = [r for r in rows if restance(r, flip_d5=True) == "strong"]
    out["strong_orig_n"] = len(strong_orig)
    out["strong_flip_n"] = len(strong_flip)
    out["strong_orig_excess_pp"] = {str(w): excess(strong_orig, rows, w) for w in FW}
    out["strong_flip_excess_pp"] = {str(w): excess(strong_flip, rows, w) for w in FW}
    # 3) 翻转带来的改善 (flip - orig), 应为正
    out["flip_improvement_pp"] = {
        str(w): round(out["strong_flip_excess_pp"][str(w)] - out["strong_orig_excess_pp"][str(w)], 1)
        for w in FW
    }
    return out


def main() -> None:
    panels, _all, _sample = dsi.load_panels()
    rows, mismatch = dsi.collect(panels)

    result: dict = {
        "meta": {
            "sampled_points": len(rows),
            "vote_recompute_mismatch": mismatch,
            "note": "按标的/按时间各切两半, 互相作为发现集与留出集; 留出集不参与任何规则选择",
        },
    }

    # ── 按标的切 (奇偶) ──────────────────────────────────
    syms = sorted({r["symbol"] for r in rows})
    half = len(syms) // 2
    set_a = set(syms[:half])
    rows_a = [r for r in rows if r["symbol"] in set_a]
    rows_b = [r for r in rows if r["symbol"] not in set_a]
    result["by_symbol"] = {
        "group_A_symbols": len(set_a),
        "group_B_symbols": len(syms) - len(set_a),
        "A_discovery": evaluate(rows_a, "A(发现集)"),
        "B_holdout": evaluate(rows_b, "B(留出集)"),
    }

    # ── 按时间切 (日期中位数) ────────────────────────────
    dts = sorted(r["date"] for r in rows)
    mid = dts[len(dts) // 2]
    rows_first = [r for r in rows if r["date"] < mid]
    rows_second = [r for r in rows if r["date"] >= mid]
    result["by_time"] = {
        "mid_date": mid,
        "first_discovery": evaluate(rows_first, "前半(发现集)"),
        "second_holdout": evaluate(rows_second, "后半(留出集)"),
    }

    # ── 裁决: 方向是否在留出集一致复现 ──────────────────
    def robust(discovery: dict, holdout: dict) -> dict:
        # d5+: 发现集与留出集 5/10 日都应为负;  d5-: 都应为正; flip 改善: 都为正
        checks = {
            "d5_plus_negative_in_both": all(
                discovery["d5_plus_excess_pp"][str(w)] < 0 and holdout["d5_plus_excess_pp"][str(w)] < 0
                for w in (5, 10)
            ),
            "d5_minus_positive_in_both": all(
                discovery["d5_minus_excess_pp"][str(w)] > 0 and holdout["d5_minus_excess_pp"][str(w)] > 0
                for w in (5, 10)
            ),
            "flip_improves_in_holdout_5d": holdout["flip_improvement_pp"]["5"] > 0,
            "flip_improves_in_holdout_10d": holdout["flip_improvement_pp"]["10"] > 0,
        }
        return {**checks, "all_pass": all(checks.values())}

    result["verdict"] = {
        "by_symbol": robust(result["by_symbol"]["A_discovery"], result["by_symbol"]["B_holdout"]),
        "by_time": robust(result["by_time"]["first_discovery"], result["by_time"]["second_holdout"]),
    }

    dest = Path(__file__).resolve().parents[2] / "docs" / "强弱规则反向_OOS.json"
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n[written] {dest}")


if __name__ == "__main__":
    main()
