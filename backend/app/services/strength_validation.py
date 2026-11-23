"""强弱判定的历史有效性检验 —— 规则投票到底有没有信息量。

## 为什么要这个

行动卡的 ``judge_strength`` 是**规则投票**（7 个维度、净票差定档），
不是统计模型。它的每一条判据都来自交易常识，但**常识不等于有效**：
"MACD 柱为正所以偏强"这句话，在 A 股历史上到底有没有预测力？

本模块用**滚动前推**（walk-forward）检验它：

1. 对历史每一天，用**当天及以前**的数据算一次强弱判定（严格无前视）；
2. 统计判定为 `strong` / `weak` / `unclear` 之后 N 日的真实收益；
3. 与**同期全样本基准**对比 —— 只有显著跑赢基准才算有信息量。

## 关键设计（都是为了防止自欺欺人）

- **每一天都重算判定**，复用 `stock_diagnostics` 的同一套函数 —— 保证
  "回测里的判定"与"行动卡上的判定"是同一个东西，不是两套逻辑；
- **必须有无条件基准**：A 股任意一天之后 N 日上涨概率本来就接近 50%，
  strong 组若非明显高于这个数就是无效；
- **样本量门槛**：分组样本 < 30 时明确标记 `insufficient`，
  不给"胜率 75%"这种基于 4 个样本的假结论；
- **结果可能是负的**，本模块如实报告 —— 如果规则无效，行动卡就该说明它无效。

## 输出怎么用

`verdict` 有五个取值：
- ``validated``: 该档方向正确且显著优于基准（超出 ≥ 5pp 且样本充足）
- ``weak_signal``: 方向对但幅度小（2~5pp）
- ``no_edge``: 与基准无差异 —— **这一档的判定不该被当决策依据**
- ``inverted``: 方向系统性写反（显著跑输基准）—— **比无效更糟，必须显式披露**
- ``insufficient``: 分组样本 < 30，无法判断 —— **"没检验出效果"不等于"有效"**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from app.services import stock_diagnostics as diag

logger = logging.getLogger(__name__)

# 检验用的前瞻窗口
FORWARD_WINDOWS = (5, 10, 20)
# 每组最少样本数 —— 低于此不给胜率结论
MIN_GROUP_SAMPLES = 30
# 判定"有信息量"的最小超额胜率 (百分点)
EDGE_THRESHOLD_PP = 5.0
# 判定"弱信号"的最小超额胜率
WEAK_EDGE_PP = 2.0
# 每步至少需要多少根历史才能算判定 (MA120 + 250 日分位)
MIN_HISTORY = 130
# 步长: 每天算一次会很慢 (5000 根 × 7 指标), 每 3 天采样一次足够
STEP = 3


@dataclass
class GroupStats:
    """一个强弱档位的前瞻表现。"""

    stance: str
    label: str
    samples: int = 0
    forward: dict[str, dict[str, float | None]] = field(default_factory=dict)
    verdict: str = "insufficient"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stance": self.stance,
            "label": self.label,
            "samples": self.samples,
            "forward": self.forward,
            "verdict": self.verdict,
            "note": self.note,
        }


@dataclass
class ValidationReport:
    symbol: str
    bars: int = 0
    range: list[str] = field(default_factory=list)
    sampled_days: int = 0
    baseline: dict[str, dict[str, float | None]] = field(default_factory=dict)
    groups: list[GroupStats] = field(default_factory=list)
    windows: list[int] = field(default_factory=lambda: list(FORWARD_WINDOWS))
    conclusions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "range": self.range,
            "sampled_days": self.sampled_days,
            "windows": self.windows,
            "baseline": self.baseline,
            "groups": [g.to_dict() for g in self.groups],
            "conclusions": self.conclusions,
            "limitations": self.limitations,
            "note": "滚动前推检验；判定与行动卡使用同一个 judge_strength，不是两套逻辑",
        }


def _forward_returns(close: np.ndarray, idx: int, window: int) -> float | None:
    if idx + window >= len(close) or close[idx] <= 0:
        return None
    return float(close[idx + window] / close[idx] - 1) * 100


def _summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean_pct": None, "median_pct": None, "win_rate": None, "samples": 0}
    arr = np.array(values, dtype=float)
    return {
        "mean_pct": round(float(arr.mean()), 2),
        "median_pct": round(float(np.median(arr)), 2),
        "win_rate": round(float((arr > 0).mean() * 100), 1),
        "samples": int(arr.size),
    }


def validate_strength_rules(
    df: pl.DataFrame,
    *,
    symbol: str = "",
    step: int = STEP,
    min_history: int = MIN_HISTORY,
) -> ValidationReport:
    """滚动检验 judge_strength 的历史有效性。

    对每个采样日 i：
    1. 用 ``df[:i+1]``（**只含当天及以前**）算一次诊断 → 拿到强弱档位；
    2. 记下之后 5/10/20 日的实际收益；
    3. 按档位分组统计，与全样本基准对比。
    """
    n = df.height
    report = ValidationReport(
        symbol=symbol or (str(df["symbol"][0]) if "symbol" in df.columns else ""),
        bars=n,
        range=[str(df["date"][0]), str(df["date"][-1])],
    )

    if n < min_history + max(FORWARD_WINDOWS) + 1:
        report.limitations.append(
            f"仅 {n} 根日线，不足以做滚动检验（需 ≥ {min_history + max(FORWARD_WINDOWS) + 1}）"
        )
        return report

    close = df["close"].to_numpy().astype(float)
    buckets: dict[str, dict[int, list[float]]] = {
        "strong": {w: [] for w in FORWARD_WINDOWS},
        "weak": {w: [] for w in FORWARD_WINDOWS},
        "unclear": {w: [] for w in FORWARD_WINDOWS},
    }
    baseline: dict[int, list[float]] = {w: [] for w in FORWARD_WINDOWS}
    sampled = 0

    for i in range(min_history, n - max(FORWARD_WINDOWS), step):
        window = df.slice(0, i + 1)          # 严格只用当天及以前
        try:
            diagnosis = diag.diagnose(window)
        except ValueError as exc:
            # 数据不足的早期段直接跳过, 不计入分母
            logger.debug("skip day %d: %s", i, exc)
            continue
        sampled += 1
        stance = diagnosis["strength"]["stance"]

        for w in FORWARD_WINDOWS:
            ret = _forward_returns(close, i, w)
            if ret is None:
                continue
            buckets[stance][w].append(ret)
            baseline[w].append(ret)

    report.sampled_days = sampled
    report.baseline = {str(w): _summarize(baseline[w]) for w in FORWARD_WINDOWS}

    labels = {"strong": "偏强", "weak": "偏弱", "unclear": "方向不明"}
    for stance in ("strong", "weak", "unclear"):
        group = GroupStats(stance=stance, label=labels[stance])
        group.forward = {str(w): _summarize(buckets[stance][w]) for w in FORWARD_WINDOWS}
        # 样本数按最短窗口计（最保守）
        group.samples = min(
            (v.get("samples") or 0) for v in group.forward.values()
        ) if group.forward else 0
        report.groups.append(group)

    _judge_verdicts(report)
    _write_conclusions(report)
    report.limitations.append(
        "检验的是「判定之后的平均表现」，不构成对未来的承诺；"
        "单只股票的历史样本天然有限，跨股票汇总另需全市场回测"
    )
    return report


def _judge_verdicts(report: ValidationReport) -> None:
    """按"相对基准的超额"给每一档定性。

    strong 组**期望**高于基准（涨得多）；weak 组**期望**低于基准。
    必须区分三种失败模式, 它们的含义完全不同:

    | 模式 | 超额表现 | 含义 |
    |---|---|---|
    | 方向一致且正确 | strong 全正 / weak 全负, 幅度够 | 规则有效 |
    | 方向一致但相反 | strong 全负 / weak 全正, 幅度够 | **反向指标** —— 反过来用可能更好 |
    | 方向不一致 | 各期正负混跳 | 噪声, 无预测力 |
    | 幅度太小 | 方向对但 < 2pp | 与抛硬币无区别 |

    "反向指标"必须单独标出: 把它报成"无预测力"会掩盖一个更强的结论。
    """
    base = report.baseline
    for group in report.groups:
        if group.samples < MIN_GROUP_SAMPLES:
            group.verdict = "insufficient"
            group.note = f"样本仅 {group.samples} 例（少于 {MIN_GROUP_SAMPLES}），不给结论"
            continue

        edges: list[float] = []
        for w in FORWARD_WINDOWS:
            g = group.forward.get(str(w)) or {}
            b = base.get(str(w)) or {}
            gw, bw = g.get("win_rate"), b.get("win_rate")
            if gw is None or bw is None:
                continue
            edges.append(gw - bw)
        if not edges:
            group.verdict = "insufficient"
            group.note = "缺少可比基准"
            continue

        edge_text = "、".join(f"{e:+.1f}pp" for e in edges)

        # 方向不明档: 期望是"没有方向性偏离", 越接近 0 越符合定位
        if group.stance == "unclear":
            drift = max(abs(e) for e in edges)
            if drift <= EDGE_THRESHOLD_PP:
                group.verdict = "validated"
                group.note = f"与基准最大偏离 {drift:.1f}pp，符合「方向不明」的定位"
            else:
                group.verdict = "no_edge"
                group.note = (
                    f"标称「方向不明」却有 {drift:.1f}pp 的方向性偏离（{edge_text}）—— "
                    "这一档实际混入了有方向的样本，档位划分不够锐利"
                )
            continue

        # strong 期望正超额; weak 期望负超额
        expected_sign = 1 if group.stance == "strong" else -1
        signed = [e * expected_sign for e in edges]     # >0 表示符合预期方向

        all_expected = all(s > 0 for s in signed)
        all_reversed = all(s < 0 for s in signed)
        magnitude = min(abs(s) for s in signed)

        if all_expected and magnitude >= EDGE_THRESHOLD_PP:
            group.verdict = "validated"
            group.note = (
                f"各期方向一致且超额 ≥{EDGE_THRESHOLD_PP}pp（最小 {magnitude:.1f}pp）：{edge_text}"
            )
        elif all_expected and magnitude >= WEAK_EDGE_PP:
            group.verdict = "weak_signal"
            group.note = f"方向一致但幅度偏小（{magnitude:.1f}pp）：{edge_text}"
        elif all_reversed and magnitude >= WEAK_EDGE_PP:
            # 一致地反着走 —— 这是反向指标, 不是噪声
            group.verdict = "inverted"
            group.note = (
                f"各期方向一致但**与预期相反**（{edge_text}）："
                f"被标为「{group.label}」的样本反而{'涨得更多' if group.stance == 'weak' else '跌得更多'}，"
                "该档在这批样本上是反向指标"
            )
        elif all_expected or all_reversed:
            group.verdict = "no_edge"
            group.note = f"方向一致但幅度不足 {WEAK_EDGE_PP}pp，与抛硬币无区别：{edge_text}"
        else:
            group.verdict = "no_edge"
            group.note = f"各前瞻期相对基准方向不一致（{edge_text}），规则没有稳定预测力"


def _write_conclusions(report: ValidationReport) -> None:
    """把结论写成能直接给用户看的一句话。

    必须区分三种情况, 否则会把「样本不够」误报成「规则无效」:
    - insufficient: 样本太少, **不能下结论**(既不能说有效也不能说无效)
    - no_edge: 样本够但没有超额 —— 规则确实无效
    - validated / weak_signal: 有超额
    """
    by_stance = {g.stance: g for g in report.groups}
    strong, weak = by_stance.get("strong"), by_stance.get("weak")

    def _verdict(g: GroupStats | None) -> str:
        return g.verdict if g else "insufficient"

    states = {_verdict(strong), _verdict(weak)}
    insufficient_all = states <= {"insufficient"}
    no_edge_any = "no_edge" in states
    inverted_only = states <= {"inverted"}
    good = {"validated", "weak_signal"} & states

    if insufficient_all:
        report.conclusions.append(
            "样本不足，无法判断这套规则有没有预测力 —— "
            "「没有结论」不等于「规则无效」，只是本次数据量不够"
        )
    elif inverted_only:
        report.conclusions.append(
            "重要：「偏强」与「偏弱」两档都**一致地反向** —— 被这套规则判为偏强的标的"
            "反而跑输基准，判为偏弱反而跑赢。要么规则写反了，要么这批样本里存在反向机制。"
            "在查清原因之前，强弱判定不能当交易依据。"
        )
    elif "inverted" in states:
        report.conclusions.append("部分档位出现反向信号：" + "、".join(sorted(states)))
    elif good and not no_edge_any:
        strength = "有效" if states == {"validated"} else "弱有效"
        report.conclusions.append(
            f"「偏强」与「偏弱」两档相对基准均有稳定超额（{strength}），"
            "可作为筛选参考，仍需结合个人风险承受度"
        )
    elif good:
        report.conclusions.append("部分档位有信号：" + "、".join(sorted(good)))
    else:
        report.conclusions.append(
            "样本充足但各档位均未跑赢基准 —— 这套规则在本标的上没有可靠预测力，"
            "行动卡里的强弱判定只能当作「事实摘要」，不能当交易依据"
        )

    for g in report.groups:
        if g.verdict in ("no_edge", "inverted"):
            tag = "反向" if g.verdict == "inverted" else "无有效信号"
            report.conclusions.append(f"「{g.label}」档{tag}：{g.note}")
        elif g.verdict == "insufficient":
            report.conclusions.append(f"「{g.label}」档样本不足：{g.note}")


# ── 全市场汇总 (跨标的检验, 样本量才够) ────────────────

def validate_across_symbols(
    panels: dict[str, pl.DataFrame],
    *,
    step: int = STEP * 3,
    max_symbols: int = 60,
) -> dict[str, Any]:
    """跨多只票汇总检验 —— 单票样本往往不足 30，汇总后才有统计意义。

    ``panels`` 是 {symbol: 日线}。只取前 ``max_symbols`` 只以控制耗时。
    """
    total_buckets: dict[str, dict[int, list[float]]] = {
        "strong": {w: [] for w in FORWARD_WINDOWS},
        "weak": {w: [] for w in FORWARD_WINDOWS},
        "unclear": {w: [] for w in FORWARD_WINDOWS},
    }
    baseline: dict[int, list[float]] = {w: [] for w in FORWARD_WINDOWS}
    per_symbol: dict[str, dict[str, Any]] = {}
    used = 0

    for symbol, df in list(panels.items())[:max_symbols]:
        rep = validate_strength_rules(df, symbol=symbol, step=step)
        if not rep.sampled_days:
            continue
        used += 1
        per_symbol[symbol] = {
            "sampled_days": rep.sampled_days,
            "verdicts": {g.stance: g.verdict for g in rep.groups},
        }
        # 汇总: 重新按组累加 (报告里只有统计量, 这里需要原始值, 故重跑一遍)
        close = df["close"].to_numpy().astype(float)
        n = df.height
        for i in range(MIN_HISTORY, n - max(FORWARD_WINDOWS), step):
            try:
                diagnosis = diag.diagnose(df.slice(0, i + 1))
            except ValueError:
                continue
            stance = diagnosis["strength"]["stance"]
            for w in FORWARD_WINDOWS:
                ret = _forward_returns(close, i, w)
                if ret is None:
                    continue
                total_buckets[stance][w].append(ret)
                baseline[w].append(ret)

    labels = {"strong": "偏强", "weak": "偏弱", "unclear": "方向不明"}
    groups: list[dict[str, Any]] = []
    base_stats = {str(w): _summarize(baseline[w]) for w in FORWARD_WINDOWS}
    for stance in ("strong", "weak", "unclear"):
        stats = {str(w): _summarize(total_buckets[stance][w]) for w in FORWARD_WINDOWS}
        samples = min((v.get("samples") or 0) for v in stats.values()) if stats else 0
        group = GroupStats(stance=stance, label=labels[stance], samples=samples, forward=stats)
        groups.append(group)

    pseudo = ValidationReport(symbol="全样本", baseline=base_stats, groups=groups)
    _judge_verdicts(pseudo)
    _write_conclusions(pseudo)

    return {
        "symbols_used": used,
        "symbols_total": len(panels),
        "baseline": base_stats,
        "groups": [g.to_dict() for g in groups],
        "conclusions": pseudo.conclusions,
        "per_symbol": per_symbol,
        "note": (
            f"跨 {used} 只标的汇总；步长 {step} 天采样。"
            "汇总样本量足够时，结论比单票可靠得多"
        ),
    }


__all__ = [
    "EDGE_THRESHOLD_PP",
    "FORWARD_WINDOWS",
    "MIN_GROUP_SAMPLES",
    "MIN_HISTORY",
    "WEAK_EDGE_PP",
    "GroupStats",
    "ValidationReport",
    "validate_across_symbols",
    "validate_strength_rules",
]
