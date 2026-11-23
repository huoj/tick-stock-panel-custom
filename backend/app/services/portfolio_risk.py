"""组合层风险 —— 行业集中度 / 主题重复 / 相关性 / 单笔风险 / 回撤预算 / 情景冲击。

## 为什么这些必须一起看

单笔仓位上限解决不了「十只票同属一个行业」这种风险：每笔都在上限内，组合却
极度集中。本模块把这些放在同一份报告里：

1. **行业集中度**：等权持仓也按行业聚合。用 HHI（Herfindahl 指数）与最大行业
   占比给出可判读的数，而不是只报「有 3 个行业」。
2. **主题重复暴露**：多只票共享同一个概念标签（`ext_gn_ths`），意味着它们可能
   受同一个故事驱动。通用标签（融资融券、深股通）先剔除，否则永远报警。
3. **相关性**：用日收益的相关系数矩阵，报平均相关、最高相关对，以及
   **有效独立注数**（1 / Σw_i w_j ρ_ij 的近似）——它才是「实际分散了几只」。
4. **单笔风险**：每只的「潜在回撤 × 权重」= 它对组合的贡献风险，排序后给出
   最该减的那一笔。
5. **整体回撤预算**：把预算按「权重 × 潜在回撤」分摊，算出现在超没超；
   超了就给建议削减目标。
6. **情景冲击**：单只跌 10% / 20% 时账户权益损失多少（线性近似，假设其余不动）。
7. **买入前模拟**（``simulate_buy``）：加这只票之后集中度/回撤预算变成什么样。

## 诚实边界

- 相关性只用**共同交易日**的重叠样本；重叠 < MIN_OVERLAP_DAYS 时**不给数**，
  记 limit，而不是用稀疏样本算一个看似精确的相关系数。
- 行业归属取 `ext_hy_ths` 的同花顺二级行业（细分到「电池-电池化学品」这种粒度），
  与 `docs/` 里「低毛利+散户接盘」那套分析同一口径。缺失行业的标的单独计入
  `unclassified`，**不合并进某个行业**。
- 回撤预算是**配置约束**，不是预测；超预算只给削减建议，不承诺能避免亏损。
- ``equity`` / ``cash`` 未提供时只给权重百分点，**不编造"亏多少钱"** ——
  没有账户资金就不该假装知道绝对金额。
- 情景冲击是线性近似，不含相关性放大；「最坏能亏多少」看 ``drawdown_budget``
  的压力回撤，两者不要混读。
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

#: 相关系数至少需要这么多重叠交易日才给数。
MIN_OVERLAP_DAYS = 20

#: 默认组合级回撤预算（占权益比例）。超过即视为超配。
DEFAULT_DRAWDOWN_BUDGET_PCT = 15.0

#: 单个行业占比超过它就算集中（等权 10 只时每只 10%，故 35% 已经很集中）。
INDUSTRY_CONCENTRATION_WARN = 0.35

#: HHI 折成「有效行业数」后的警戒线：低于 3 说明实际只分散在少数几个行业。
MIN_EFFECTIVE_INDUSTRIES = 3.0

#: 概念标签覆盖率超过它就不再算「主题」，而是制度/通道属性。
#: 实测同花顺概念表：融资融券覆盖 3893/5582 = 69.8%、深股通 1895 = 34%、
#: 沪股通 1646 = 29.5%。这些若参与「主题重复」，任意两只票都会命中。
DEFAULT_CONCEPT_MAX_COVERAGE_PCT = 0.25

#: 情景冲击的默认跌幅档（负数表示下跌 10% / 20%）。
DEFAULT_SCENARIO_SHOCKS: tuple[float, ...] = (-0.10, -0.20)

#: 概念标签归并：只有覆盖标的数 ≥ 这个值的标签才参与「谁包含谁」的判断。
#: 太冷门的标签（例如只有 6 只票的概念）样本太小，两三只票重叠就接近 100%，
#: 会把不相干的概念误并成父子关系。
DEFAULT_CLUSTER_MIN_COVERAGE = 30

#: 子标签的标的落在父标签里的最低比例。实测真实同义标签：
#: 人形机器人→机器人概念 86%、AI应用→人工智能 81%、AI智能体→人工智能 83%、
#: 存储芯片→芯片概念 93%。取 0.75 既能收下这些，又不会把只沾边的并进来。
DEFAULT_CLUSTER_CONTAIN_THRESHOLD = 0.75

#: 第一父与第二父的包含度差小于它就判定「归属歧义」，不并。
#: 实测「AI视频」对 AIGC概念 88.0%、对 人工智能 85.3%、对 AI应用 84.0% ——
#: 三条都过阈值、只差几个百分点，并到任一条都会让另外两条的成员数凭空消失。
DEFAULT_CLUSTER_MARGIN = 0.10


@dataclass(frozen=True, slots=True)
class PositionInput:
    """组合里的一笔持仓。``drawdown_pct`` 是**正数**表示的潜在回撤幅度。"""

    symbol: str
    weight: float                     # 占组合权益（0-1）
    drawdown_pct: float | None = None  # 潜在回撤幅度（0.05 = 5%）
    industry: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "weight": round(self.weight, 4),
            "drawdown_pct": None if self.drawdown_pct is None else round(self.drawdown_pct, 4),
            "industry": self.industry,
        }


def normalize_weights(weights: list[float]) -> list[float]:
    """把权重归一化到总和 1；全零/负数时退回等权。"""
    positive = [max(0.0, w) for w in weights]
    total = sum(positive)
    if total <= 0:
        n = len(weights)
        return [1.0 / n] * n if n else []
    return [w / total for w in positive]


def industry_concentration(positions: list[PositionInput]) -> dict[str, Any]:
    """行业集中度：各行业权重、最大行业、HHI 与有效行业数。"""
    buckets: dict[str, float] = {}
    unclassified = 0.0
    for pos in positions:
        if pos.industry:
            buckets[pos.industry] = buckets.get(pos.industry, 0.0) + pos.weight
        else:
            unclassified += pos.weight
    total = sum(buckets.values()) + unclassified
    shares = {name: (w / total if total > 0 else 0.0) for name, w in buckets.items()}
    hhi = sum(s * s for s in shares.values())
    effective = (1.0 / hhi) if hhi > 0 else 0.0
    top = max(shares.items(), key=lambda kv: kv[1]) if shares else (None, 0.0)
    warnings: list[str] = []
    if top[0] is not None and top[1] > INDUSTRY_CONCENTRATION_WARN:
        warnings.append(
            f"最大行业「{top[0]}」占 {top[1]:.1%}，超过警戒线 {INDUSTRY_CONCENTRATION_WARN:.0%}"
        )
    if effective and effective < MIN_EFFECTIVE_INDUSTRIES:
        warnings.append(
            f"有效行业数仅 {effective:.1f}（HHI {hhi:.3f}），低于 {MIN_EFFECTIVE_INDUSTRIES:g}，分散不足"
        )
    if unclassified > 0:
        warnings.append(f"{unclassified:.1%} 的权重没有行业归属，集中度结论不完整")
    return {
        "by_industry": dict(sorted(shares.items(), key=lambda kv: -kv[1])),
        "unclassified_weight": round(unclassified, 4),
        "hhi": round(hhi, 4),
        "effective_industries": round(effective, 2),
        "top_industry": top[0],
        "top_industry_share": round(top[1], 4),
        "warnings": warnings,
    }


def correlation_matrix(
    returns: dict[str, list[float]],
) -> tuple[dict[str, dict[str, float | None]], list[str]]:
    """日收益的 Pearson 相关矩阵。

    调用方保证各序列已按**共同日期**对齐；长度不足 ``MIN_OVERLAP_DAYS`` 的对
    直接给 None 并记 limitation —— 稀疏样本上算出来的相关系数没有解释力。
    """
    limits: list[str] = []
    symbols = sorted(returns)
    matrix: dict[str, dict[str, float | None]] = {}
    for a in symbols:
        matrix[a] = {}
        for b in symbols:
            if a == b:
                matrix[a][b] = 1.0
                continue
            xs, ys = returns[a], returns[b]
            n = min(len(xs), len(ys))
            if n < MIN_OVERLAP_DAYS:
                matrix[a][b] = None
                limits.append(f"{a} ↔ {b} 仅 {n} 个重叠交易日（< {MIN_OVERLAP_DAYS}），未计算相关")
                continue
            xs, ys = xs[-n:], ys[-n:]
            mx, my = sum(xs) / n, sum(ys) / n
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
            vx = sum((x - mx) ** 2 for x in xs)
            vy = sum((y - my) ** 2 for y in ys)
            if vx <= 0 or vy <= 0:
                matrix[a][b] = None
                limits.append(f"{a} ↔ {b} 有一侧收益无波动，相关系数无定义")
                continue
            matrix[a][b] = cov / math.sqrt(vx * vy)
    return matrix, list(dict.fromkeys(limits))


def correlation_summary(
    matrix: dict[str, dict[str, float | None]],
    weights: dict[str, float],
) -> dict[str, Any]:
    """平均相关、最高相关对，以及有效独立注数。"""
    symbols = sorted(matrix)
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            value = matrix.get(a, {}).get(b)
            if value is not None:
                pairs.append((a, b, value))
    avg = sum(v for _, _, v in pairs) / len(pairs) if pairs else None
    highest = max(pairs, key=lambda p: p[2]) if pairs else None
    lowest = min(pairs, key=lambda p: p[2]) if pairs else None

    # 有效独立注数近似: 1 / Σ_i Σ_j w_i w_j ρ_ij（ρ_ii = 1）。
    # denom 是组合方差的相对量。denom <= 0 意味着权重组合近似完全对冲，
    # 此时 1/denom 无定义（数学上是无穷），如实报 None 而不是给一个巨大的假数。
    denom = 0.0
    for a in symbols:
        wa = weights.get(a, 0.0)
        for b in symbols:
            wb = weights.get(b, 0.0)
            rho = matrix.get(a, {}).get(b)
            if rho is None:
                rho = 0.0  # 未计算的对按不相关处理 —— 会高估分散，故在 warnings 里说明
            denom += wa * wb * rho
    effective_bets = 1.0 / denom if denom > 1e-12 else None
    warnings: list[str] = []
    if denom <= 1e-12 and len(symbols) > 1:
        warnings.append(
            f"组合方差相对量 {denom:.4f} ≤ 0：权重组合近似完全对冲，"
            "「有效独立注数」无定义（1/0），不给出数字"
        )
    if highest is not None and highest[2] > 0.7:
        warnings.append(
            f"{highest[0]} 与 {highest[1]} 相关系数 {highest[2]:.2f}，接近同一笔风险"
        )
    if effective_bets is not None and len(symbols) > 1 and effective_bets < 3.0:
        warnings.append(
            f"有效独立注数仅 {effective_bets:.1f}（名义 {len(symbols)} 笔），分散度不足"
        )
    if pairs and len(pairs) < len(symbols) * (len(symbols) - 1) / 2:
        warnings.append("部分相关对缺少足够重叠样本，有效独立注数可能被高估")
    return {
        "average_correlation": None if avg is None else round(avg, 4),
        "highest_pair": None if highest is None else {
            "a": highest[0], "b": highest[1], "correlation": round(highest[2], 4),
        },
        "lowest_pair": None if lowest is None else {
            "a": lowest[0], "b": lowest[1], "correlation": round(lowest[2], 4),
        },
        "effective_bets": None if effective_bets is None else round(effective_bets, 2),
        "pair_count": len(pairs),
        "warnings": warnings,
    }


def per_position_risk(positions: list[PositionInput]) -> dict[str, Any]:
    """单笔风险：每只对组合的风险贡献（权重 × 潜在回撤）。"""
    rows: list[dict[str, Any]] = []
    total = 0.0
    missing: list[str] = []
    for pos in positions:
        if pos.drawdown_pct is None:
            missing.append(pos.symbol)
            contribution = None
        else:
            contribution = pos.weight * abs(pos.drawdown_pct)
            total += contribution
        rows.append({
            "symbol": pos.symbol,
            "industry": pos.industry,
            "weight": round(pos.weight, 4),
            "drawdown_pct": None if pos.drawdown_pct is None else round(pos.drawdown_pct, 4),
            "risk_contribution": None if contribution is None else round(contribution, 4),
        })
    rows.sort(key=lambda r: (r["risk_contribution"] is None, -(r["risk_contribution"] or 0)))
    warnings: list[str] = []
    if missing:
        warnings.append(f"{len(missing)} 笔没有潜在回撤数据，单笔风险合计被低估：{'、'.join(missing[:6])}")
    if rows and rows[0]["risk_contribution"]:
        share = rows[0]["risk_contribution"] / total if total > 0 else 0.0
        if share > 0.30:
            warnings.append(
                f"{rows[0]['symbol']} 单独贡献 {share:.1%} 的组合风险，过度集中"
            )
    return {
        "positions": rows,
        "total_risk_contribution": round(total, 4),
        "warnings": warnings,
    }


def drawdown_budget(
    positions: list[PositionInput],
    *,
    budget_pct: float = DEFAULT_DRAWDOWN_BUDGET_PCT,
) -> dict[str, Any]:
    """整体回撤预算：当前「所有持仓同时打到潜在回撤」的组合损失 vs 预算。

    这是**压力假设**：所有持仓在同一时间都走到各自的潜在回撤位。
    它不代表真实相关性下的亏损上限（真实中部分回撤会被对冲），
    但对"最坏能亏多少"给出了可判读的上界。
    """
    stressed = sum(pos.weight * abs(pos.drawdown_pct or 0.0) for pos in positions)
    budget = budget_pct / 100.0
    used = (stressed / budget) if budget > 0 else None
    over = stressed > budget
    warnings: list[str] = []
    cuts: list[dict[str, Any]] = []
    if over:
        excess = stressed - budget
        warnings.append(
            f"压力回撤 {stressed:.2%} 超预算 {budget:.2%}（超出 {excess:.2%}）"
        )
        # 按风险贡献从大到小削减，直到回到预算内。
        ranked = sorted(
            [p for p in positions if p.drawdown_pct],
            key=lambda p: -p.weight * abs(p.drawdown_pct or 0.0),
        )
        remaining = excess
        for pos in ranked:
            if remaining <= 0:
                break
            per_unit = abs(pos.drawdown_pct or 0.0)
            if per_unit <= 0:
                continue
            reduce_weight = min(pos.weight, remaining / per_unit)
            cuts.append({
                "symbol": pos.symbol,
                "current_weight": round(pos.weight, 4),
                "reduce_by": round(reduce_weight, 4),
                "new_weight": round(pos.weight - reduce_weight, 4),
            })
            remaining -= reduce_weight * per_unit
    if budget > 0 and stressed > budget * 0.8 and not over:
        warnings.append(f"压力回撤 {stressed:.2%} 已用掉预算的 {used:.0%}，接近上限")
    return {
        "budget_pct": round(budget_pct, 4),
        "stressed_drawdown_pct": round(stressed * 100, 4),
        "budget_usage_pct": None if used is None else round(used * 100, 2),
        "over_budget": over,
        "suggested_cuts": cuts,
        "warnings": warnings,
    }


def build_concept_clusters(
    concept_map: dict[str, list[str]],
    coverage: dict[str, int],
    tags_of_interest: list[str] | set[str],
    *,
    total_universe: int | None = None,
    max_coverage_pct: float = DEFAULT_CONCEPT_MAX_COVERAGE_PCT,
    min_coverage: int = DEFAULT_CLUSTER_MIN_COVERAGE,
    contain_threshold: float = DEFAULT_CLUSTER_CONTAIN_THRESHOLD,
    margin: float = DEFAULT_CLUSTER_MARGIN,
    max_tags: int = 200,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """把「同一件事的不同层级标签」并成一个聚类。

    ## 为什么需要

    同花顺的概念标签是多层级的，同一批票会被打上一串近义标签。实测（5582 只票）
    ``人形机器人`` 有 86% 落在 ``机器人概念`` 里面；``AI应用`` 有 81%、
    ``AI智能体`` 有 83% 落在 ``人工智能`` 里；``存储芯片`` 有 93% 落在
    ``芯片概念`` 里。不归并的话，持仓里 3 只机器人票会被拆成
    「机器人概念 2 只」「人形机器人 1 只」两条，每条都不够集中，
    **真实集中度被这两条一起隐瞒了**。

    ## 通用标签必须先排除（踩过的坑）

    第一版没排除就开始求包含度，结果 ``融资融券``（覆盖 3893 只）把所有 20 多只
    持仓的主题标签全"包含"了进去 —— 面板顶部变成「10 只持仓共享融资融券，
    合计权重 97%」，这就是剔除高覆盖标签之前的那个废话警报又回来了。
    同时 ``人形机器人`` 被判成"机器人概念 86.3% vs 融资融券 82.3%，归属歧义"，
    于是该并的没并。

    所以：覆盖率超过 ``max_coverage_pct`` 的标签**既不做子、也不做父**。

    ## 判定规则（保守，宁可少并不错并）

    对两个标签 A、B，算 ``contain_b_in_a = |A∩B| / |B|``：

    1. 只看 ``coverage >= min_coverage`` 的标签（太冷门的标签样本不足，容易
       因为两三只票重叠就 100% 包含）；
    2. ``contain_b_in_a >= contain_threshold``（默认 0.75）才认为 B ⊂ A；
    3. 取包含度最高的那个 A 作为父；**若第二名的差距小于 ``margin``（默认
       0.10），判定为归属歧义、不并**。实测 ``AI视频`` 对 ``AIGC概念`` 是
       88.0%、对 ``人工智能`` 是 85.3%、对 ``AI应用`` 是 84.0% —— 三条都过
       阈值、只差几个百分点，并到任一条都会让另外两条的成员数凭空消失；
    4. 聚类名取簇内 ``coverage`` 最大的那个标签（最通用的那个）。

    注意 ``contain_threshold`` 本身已经挡掉了大部分"半包含"：实测 ``减速器``
    对 ``机器人概念`` 是 0.769（过阈值）、对 ``新能源汽车`` 是 0.741（不过），
    于是它干净地并进 ``机器人概念``，不需要靠 margin 兜底。

    返回 ``(tag → 聚类名, overlaps)``。``overlaps`` 记录被判定为"高度重叠但
    因歧义/阈值未归并"的对，供界面说明，不参与分组。
    """
    universe = total_universe or len(concept_map) or (max(coverage.values()) if coverage else 0)
    interest = [
        t for t in dict.fromkeys(tags_of_interest)
        if coverage.get(t, 0) >= min_coverage
        and (not universe or coverage.get(t, 0) / universe <= max_coverage_pct)
    ]
    interest = interest[:max_tags]
    if len(interest) < 2:
        return {}, []

    # tag → 覆盖的标的集合。只对 candidate 标签建索引，避免 390 个标签两两求交。
    tag_sets: dict[str, set[str]] = {t: set() for t in interest}
    for symbol, tags in concept_map.items():
        for tag in tags:
            bucket = tag_sets.get(tag)
            if bucket is not None:
                bucket.add(symbol)

    parents: dict[str, str] = {}
    overlaps: list[dict[str, Any]] = []
    for tag in interest:
        own = tag_sets[tag]
        if not own:
            continue
        scores: list[tuple[float, str]] = []
        for other in interest:
            if other == tag:
                continue
            other_set = tag_sets[other]
            if len(other_set) <= len(own):
                # 父必须更"大"，否则平级互含会来回并。
                continue
            contain = len(own & other_set) / len(own)
            if contain >= contain_threshold:
                scores.append((contain, other))
        if not scores:
            continue
        scores.sort(key=lambda s: (-s[0], -coverage.get(s[1], 0)))
        best_contain, best_parent = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else 0.0
        if best_contain - runner_up < margin:
            overlaps.append({
                "tag": tag,
                "candidates": [
                    {"parent": p, "containment_pct": round(c * 100, 1)}
                    for c, p in scores[:3]
                ],
                "reason": "同时高度落在多个概念里，归属歧义，未归并",
            })
            continue
        parents[tag] = best_parent

    # union-find：把 tag 挂到最通用的祖先上。
    cluster_of: dict[str, str] = {}
    for tag in interest:
        root = tag
        seen = {tag}
        while root in parents:
            root = parents[root]
            if root in seen:  # 理论上不会成环（只指向更大的集合），防御性退出
                break
            seen.add(root)
        cluster_of[tag] = root

    # 簇名统一成簇内 coverage 最大的标签（root 已满足，但并列时按名字稳定排序）。
    for tag in interest:
        members = [t for t, c in cluster_of.items() if c == cluster_of[tag]]
        if len(members) > 1:
            best = max(members, key=lambda t: (coverage.get(t, 0), t))
            for m in members:
                cluster_of[m] = best
    # 只返回**真的被并了**的标签。把自己映射到自己放进结果里，调用方（尤其
    # 是界面上"已归并 N 组"的说明）就要额外判断 self-mapping，容易出错。
    return {t: c for t, c in cluster_of.items() if c != t}, overlaps


def concept_exposure(
    positions: list[PositionInput],
    concept_map: dict[str, list[str]],
    coverage: dict[str, int],
    *,
    total_universe: int | None = None,
    max_coverage_pct: float = DEFAULT_CONCEPT_MAX_COVERAGE_PCT,
    min_symbols: int = 2,
    clusters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """主题重复暴露：多只持仓共享同一个概念标签。

    ## 为什么必须先剔除通用标签

    同花顺概念表里 ``融资融券`` 覆盖 3893 只（全市场约 70%）、``深股通`` 1895 只、
    ``沪股通`` 1646 只 —— 它们是**制度/通道属性**，不是主题。不剔除的话任意两只
    股票都会"共享三个概念"，这个面板会永远报警，等于没信息。

    所以：覆盖率超过 ``max_coverage_pct`` 的标签先丢掉，再只保留被 **≥2 只持仓**
    共享的标签。剩下的才是"多只票其实受同一个故事驱动"。

    ``clusters``（``build_concept_clusters`` 的输出）用来把近义标签并成一条：
    一只票同时带 ``机器人概念`` 和 ``人形机器人`` 只算进同一组一次，不会因为
    标签数量多而虚增集中度。归并后 ``groups[].members`` 保留被并进来的原始标签。
    """
    limits: list[str] = []
    # 覆盖率的分母必须是**概念表覆盖的全部标的数**，不是"覆盖最多的那个标签的
    # 标的数"。用后者的写法在真实数据上碰巧接近（融资融券 3893 / 全市场 5582），
    # 但概念表只覆盖一部分标的时会失真。优先用调用方给的 universe，其次用
    # concept_map 的行数（就是这张表覆盖了多少只票）。
    universe = total_universe or len(concept_map) or (max(coverage.values()) if coverage else 0)
    excluded: list[dict[str, Any]] = []
    usable: dict[str, list[str]] = {}

    for tag, count in coverage.items():
        share = (count / universe) if universe else 0.0
        if share > max_coverage_pct:
            excluded.append({"concept": tag, "coverage_pct": round(share * 100, 2)})
            continue
        usable[tag] = []

    if not concept_map:
        limits.append("没有概念标签数据，主题重复暴露无法计算")
        return {
            "groups": [], "excluded_generic": [], "concept_count": 0,
            "warnings": [], "limitations": limits, "clusters": [], "overlaps": [],
        }

    cluster_of = clusters or {}
    # 聚类名 → 该聚类下所有被并进来的原始标签（用于界面说明"这条合并了哪些词"）。
    cluster_members: dict[str, set[str]] = {}
    for tag, label in cluster_of.items():
        cluster_members.setdefault(label, set()).add(tag)
    for label in list(cluster_members):
        cluster_members[label].add(label)

    for pos in positions:
        # 一只票的多个同义标签先去重成聚类名，再记一次 —— 否则同一只票会把
        # 同一组重复计数。
        labels = {
            cluster_of.get(tag, tag)
            for tag in set(concept_map.get(pos.symbol) or [])
            if tag in usable
        }
        for label in labels:
            usable.setdefault(label, []).append(pos.symbol)

    groups: list[dict[str, Any]] = []
    for tag, members in usable.items():
        if len(members) < min_symbols:
            continue
        weight = sum(p.weight for p in positions if p.symbol in members)
        merged = sorted(cluster_members.get(tag, set()) - {tag})
        groups.append({
            "concept": tag,
            "symbols": sorted(members),
            "symbol_count": len(members),
            "weight": round(weight, 4),
            "members": merged,
        })
    groups.sort(key=lambda g: (-g["weight"], -g["symbol_count"], g["concept"]))

    missing = [p.symbol for p in positions if not concept_map.get(p.symbol)]
    if missing:
        limits.append(f"{len(missing)} 只持仓没有概念标签：{'、'.join(missing[:6])}")

    # 归并数量按**聚类本身**统计，而不是按已展示的分组统计 —— 一个被并掉的
    # 标签可能因为所在簇还不够 2 只票而不出现在 groups 里，但那确实是发生过
    # 的归并，隐瞒它会让「这组数字是归并后的」失去提示。
    merged_count = sum(len(members) - 1 for members in cluster_members.values() if len(members) > 1)
    if merged_count:
        limits.append(
            f"{merged_count} 个近义概念标签已并进更通用的一档（如「人形机器人」并入「机器人概念」）；"
            "归并依据是标签之间的标的包含度，不是语义理解"
        )

    warnings: list[str] = []
    if groups:
        top = groups[0]
        if top["symbol_count"] >= 2:
            extra = f"（含 {'、'.join(top['members'])}）" if top["members"] else ""
            warnings.append(
                f"{top['symbol_count']} 只持仓共享「{top['concept']}」{extra}，"
                f"合计权重 {top['weight']:.1%}"
                " —— 它们很可能受同一个风险驱动，分散度低于标的数量暗示的水平"
            )
    return {
        "groups": groups,
        "excluded_generic": sorted(excluded, key=lambda e: -e["coverage_pct"])[:12],
        "concept_count": len(groups),
        "warnings": warnings,
        "limitations": limits,
        "clusters": [
            # members 排除 label 自己：调用方要展示的是"这一档还包含了哪些更细的标签"
            {"label": label, "members": sorted(members - {label})}
            for label, members in sorted(cluster_members.items())
            if len(members) > 1
        ],
        "overlaps": [],
    }


def load_price_series(
    data_dir: Path,
    symbols: list[str],
    *,
    lookback_days: int = 60,
) -> tuple[dict[str, list[float]], list[str]]:
    """**逐标的**的收盘价序列（不要求共同交易日）。

    ## 为什么不能复用 ``load_daily_returns``

    那个函数只保留所有标的都有行情的**共同日期**，这对相关性是必须的，
    但对「单只票自己跌过多少」「单只票自己多颠」是**错的**：

    拿它算回撤/波动，会造成「多买一只票 → 共同日期变少 → 其它票的历史回撤
    跟着变」。买入模拟里 before/after 只差一个标的，基线却漂了 —— 那样
    「买入后集中度变成多少」的对比就没有可比性。

    所以回撤与波动走这条逐标的路径；相关性与有效独立注数仍走共同日期。
    """
    limits: list[str] = []
    want = set(symbols)
    per_symbol: dict[str, dict[dt.date, float]] = {s: {} for s in symbols}

    for name in ("kline_daily_enriched", "kline_etf_enriched", "kline_index_enriched"):
        base = data_dir / name
        if not base.is_dir():
            continue
        parts = sorted(base.glob("date=*"))[-lookback_days:]
        for part in parts:
            path = part / "part.parquet"
            if not path.is_file():
                continue
            try:
                frame = pl.read_parquet(path, columns=["symbol", "raw_close"])
            except Exception as exc:  # noqa: BLE001
                limits.append(f"{name}/{part.name} 读取失败: {exc}")
                continue
            try:
                day = dt.date.fromisoformat(part.name[len("date="):][:10])
            except ValueError:
                continue
            for row in frame.iter_rows(named=True):
                symbol = row.get("symbol")
                price = row.get("raw_close")
                if symbol in want and price:
                    per_symbol[str(symbol)][day] = float(price)

    out: dict[str, list[float]] = {}
    missing: list[str] = []
    for symbol, values in per_symbol.items():
        if len(values) < MIN_OVERLAP_DAYS:
            missing.append(symbol)
            continue
        out[symbol] = [price for _, price in sorted(values.items())]
    if missing:
        limits.append(
            f"{len(missing)} 只标的行情不足 {MIN_OVERLAP_DAYS} 天：{'、'.join(missing[:6])}"
        )
    return out, list(dict.fromkeys(limits))


def symbol_volatility(
    data_dir: Path,
    symbols: list[str],
    *,
    lookback_days: int = 60,
) -> tuple[dict[str, float], list[str]]:
    """每只标的的年化波动率（日收益标准差 × √252）。

    **逐标的独立计算**（见 ``load_price_series``）：一只票的波动率不该因为它
    被和另一只票一起分析而改变。
    """
    series, limits = load_price_series(data_dir, symbols, lookback_days=lookback_days)
    if not series:
        return {}, limits or ["没有足够行情，波动率无法计算"]
    out: dict[str, float] = {}
    for symbol, prices in series.items():
        returns = [
            prices[i] / prices[i - 1] - 1.0
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if len(returns) < 2:
            continue
        n = len(returns)
        mean = sum(returns) / n
        var = sum((x - mean) ** 2 for x in returns) / (n - 1)
        out[symbol] = round(math.sqrt(max(var, 0.0)) * math.sqrt(252.0), 4)
    if out:
        limits.append("波动率基于窗口内日收益年化（×√252）")
    return out, list(dict.fromkeys(limits))


def scenario_impact(
    positions: list[PositionInput],
    *,
    shocks: tuple[float, ...] = DEFAULT_SCENARIO_SHOCKS,
    equity: float | None = None,
) -> dict[str, Any]:
    """情景冲击：某只（或某组）跌 X% 时，账户权益损失多少。

    这是**线性近似**：假设其它持仓不动、且跌幅是相对当前市值的。它回答
    「单只票跌 10% 我账户掉多少」，不是「市场崩了我亏多少」——后者要看
    ``drawdown_budget`` 的压力回撤。
    """
    rows: list[dict[str, Any]] = []
    totals_pct: dict[int, float] = {round(abs(s) * 100): 0.0 for s in shocks}
    totals_amount: dict[int, float] = {round(abs(s) * 100): 0.0 for s in shocks}
    for pos in positions:
        item: dict[str, Any] = {"symbol": pos.symbol, "weight": round(pos.weight, 4)}
        for shock in shocks:
            size = round(abs(shock) * 100)
            item[f"equity_impact_pct_{size}"] = round(pos.weight * shock * 100, 4)
            # 没有账户权益时**显式给 None**而不是省掉这个键：调用方（前端表格）
            # 直接读 amount_10 / amount_20，缺键会 KeyError，None 只是显示成「—」。
            item[f"amount_{size}"] = (
                None if equity is None else round(pos.weight * shock * equity, 2)
            )
            totals_pct[size] += pos.weight * shock * 100
            if equity is not None:
                totals_amount[size] += pos.weight * shock * equity
        rows.append(item)
    rows.sort(key=lambda r: -r["weight"])
    totals: dict[str, Any] = {}
    for size in totals_pct:
        totals[f"total_equity_impact_pct_{size}"] = round(totals_pct[size], 4)
        totals[f"total_amount_{size}"] = (
            None if equity is None else round(totals_amount[size], 2)
        )
    return {
        "shocks_pct": [round(abs(s) * 100) for s in shocks],
        "equity": None if equity is None else round(equity, 2),
        "positions": rows,
        **totals,
        "note": "线性近似：假设其余持仓不动，跌幅相对当前市值；不含相关性放大。",
    }


def simulate_buy(
    data_dir: Path,
    *,
    existing: list[dict[str, Any]],
    new_symbol: str,
    new_weight_pct: float,
    equity: float,
    budget_pct: float = DEFAULT_DRAWDOWN_BUDGET_PCT,
    lookback_days: int = 60,
    normalize: bool = False,
) -> dict[str, Any]:
    """买入前仓位模拟：加这只票之后，集中度/回撤预算会变成什么样。

    ``existing`` 是**当前持仓**（``{symbol, weight, drawdown_pct?}``），
    ``new_weight_pct`` 是**买入后**这只票占账户权益的比例（不是买入金额占现有
    持仓的比例）。返回「前 vs 后」两组数，让"这笔要不要买"有可比读数。

    ## 权重口径

    权重分母是**账户权益**，所以新仓位挤占的是现金，existing 的权重不变；
    这不是"把总仓位重新归一化"那种算法 —— 满仓时再买必须减别的票，那是另一件事。
    如果 ``new_weight_pct`` 超过可用现金占比，如实报 ``over_cash=True``。
    """
    limits: list[str] = []
    cash_weight = max(0.0, 1.0 - sum(float(h.get("weight") or 0.0) for h in existing))
    new_w = new_weight_pct / 100.0
    over_cash = new_w > cash_weight + 1e-9
    if over_cash:
        limits.append(
            f"买入后该票占权益 {new_w:.1%}，超过可用现金占比 {cash_weight:.1%}"
            " —— 差额必须来自卖出其它持仓，本模拟未替你决定卖哪只"
        )

    before = build_portfolio_report(
        data_dir, holdings=existing, budget_pct=budget_pct,
        lookback_days=lookback_days, equity=equity, normalize=normalize,
    )
    after_holdings = [*existing, {"symbol": new_symbol, "weight": new_w}]
    after = build_portfolio_report(
        data_dir, holdings=after_holdings, budget_pct=budget_pct,
        lookback_days=lookback_days, equity=equity, normalize=normalize,
    )

    def _digest(report: dict[str, Any]) -> dict[str, Any]:
        industry = report.get("industry_concentration") or {}
        concept = report.get("concept") or {}
        corr = report.get("correlation") or {}
        budget = report.get("drawdown_budget") or {}
        top_concept = (concept.get("groups") or [{}])[0]
        return {
            "top_industry": industry.get("top_industry"),
            "top_industry_share": industry.get("top_industry_share"),
            "effective_industries": industry.get("effective_industries"),
            "top_concept": top_concept.get("concept"),
            "top_concept_symbols": top_concept.get("symbol_count"),
            "top_concept_weight": top_concept.get("weight"),
            "effective_bets": corr.get("effective_bets"),
            "max_position_weight": max(
                (float(p.get("weight") or 0.0) for p in report.get("positions") or []), default=0.0
            ),
            "stressed_drawdown_pct": budget.get("stressed_drawdown_pct"),
            "budget_usage_pct": budget.get("budget_usage_pct"),
            "over_budget": budget.get("over_budget"),
        }

    before_d = _digest(before)
    after_d = _digest(after)
    worst = after_d.get("stressed_drawdown_pct")
    return {
        "new_symbol": new_symbol,
        "new_weight_pct": round(new_weight_pct, 4),
        "equity": round(equity, 2),
        "cash_weight_before_pct": round(cash_weight * 100, 2),
        "over_cash": over_cash,
        "before": before_d,
        "after": after_d,
        "worst_case_loss": None if worst is None else round(-equity * worst / 100.0, 2),
        "deltas": {
            key: (
                None if before_d.get(key) is None or after_d.get(key) is None
                else round(float(after_d[key]) - float(before_d[key]), 4)
            )
            for key in (
                "top_industry_share", "effective_industries", "effective_bets",
                "max_position_weight", "stressed_drawdown_pct", "budget_usage_pct",
            )
        },
        "limitations": list(dict.fromkeys([
            *limits,
            *(before.get("limitations") or []),
            *(after.get("limitations") or []),
        ])),
        "warnings": list(dict.fromkeys([
            *(before.get("warnings") or []),
            *(after.get("warnings") or []),
        ])),
    }


def load_industry_map(data_dir: Path) -> tuple[dict[str, str], list[str]]:
    """symbol → 同花顺二级行业。读不到时返回空映射 + limitation。"""
    path = data_dir / "ext_data" / "ext_hy_ths" / "part.parquet"
    if not path.is_file():
        return {}, ["缺少 ext_hy_ths 行业表，行业集中度无法计算"]
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"ext_hy_ths 读取失败: {exc}"]
    column = next((c for c in frame.columns if "行业" in c), None)
    if column is None or "symbol" not in frame.columns:
        return {}, ["ext_hy_ths 缺少 symbol 或行业列"]
    return (
        {str(s): str(v) for s, v in zip(frame["symbol"].to_list(), frame[column].to_list(), strict=True)
         if v is not None},
        [],
    )


def load_concept_map(data_dir: Path) -> tuple[dict[str, list[str]], dict[str, int], list[str]]:
    """symbol → 同花顺概念标签列表，外加「概念 → 全市场覆盖数」用于剔除通用标签。

    同花顺概念表 ``ext_gn_ths`` 的「所属概念」是分号分隔的多标签字符串
    （实测 5582 只票、390 个不同标签、平均每票 12.7 个）。

    **必须同时返回覆盖数**：`融资融券` 覆盖 3893 只（69.8%）、`深股通` 1895 只，
    这类是制度/通道属性而不是主题。不剔除的话，任意两只股票都会"共享概念"，
    「主题重复暴露」立刻变成永远成立的废话。
    """
    path = data_dir / "ext_data" / "ext_gn_ths" / "part.parquet"
    if not path.is_file():
        return {}, {}, ["缺少 ext_gn_ths 概念表，主题重复暴露无法计算"]
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        return {}, {}, [f"ext_gn_ths 读取失败: {exc}"]
    column = next((c for c in frame.columns if "概念" in c), None)
    if column is None or "symbol" not in frame.columns:
        return {}, {}, ["ext_gn_ths 缺少 symbol 或概念列"]

    mapping: dict[str, list[str]] = {}
    coverage: dict[str, int] = {}
    for symbol, raw in zip(frame["symbol"].to_list(), frame[column].to_list(), strict=True):
        if symbol is None or raw is None:
            continue
        tags = [t.strip() for t in str(raw).split(";") if t.strip()]
        if not tags:
            continue
        mapping[str(symbol)] = tags
        for tag in set(tags):
            coverage[tag] = coverage.get(tag, 0) + 1
    return mapping, coverage, []


def symbol_drawdown_stats(
    data_dir: Path,
    symbols: list[str],
    *,
    lookback_days: int = 60,
) -> tuple[dict[str, float], list[str]]:
    """每只标的在回看窗口内**实际发生过**的最大回撤（正数小数）。

    ## 为什么用它当「潜在回撤」

    回撤预算需要每笔的"潜在回撤"。原来这个值只能由调用方传，而前端从来没传，
    于是单笔风险与回撤预算整块是空的（limitation 里写着「N 笔缺少潜在回撤」）。

    这里用**过去窗口内实际实现的最大回撤**做代理：它不是预测，但它是这只票
    真实跌过的幅度，比拍一个 10% 更诚实。返回值会给调用方，写明口径是
    「历史实现值」，不要当未来损失上限。

    窗口内数据不足 ``MIN_OVERLAP_DAYS`` 的标的**不给数**（宁可缺，不编）。

    **逐标的独立计算**（见 ``load_price_series``），这样换一组标的不会让
    某只票的回撤数字跟着变 —— 买入模拟的 before/after 才有可比性。
    """
    series, limits = load_price_series(data_dir, symbols, lookback_days=lookback_days)
    if not series:
        return {}, limits or ["没有足够行情，历史回撤无法计算"]

    out: dict[str, float] = {}
    for symbol, prices in series.items():
        peak = prices[0]
        worst = 0.0
        for price in prices:
            peak = max(peak, price)
            if peak > 0:
                worst = max(worst, (peak - price) / peak)
        out[symbol] = round(worst, 4)
    if out:
        limits.append("潜在回撤取「过去窗口内的实际最大回撤」，是历史实现值，不是未来损失上限")
    return out, list(dict.fromkeys(limits))


def load_daily_returns(
    data_dir: Path,
    symbols: list[str],
    *,
    lookback_days: int = 60,
) -> tuple[dict[str, list[float]], list[dt.date], list[str]]:
    """按**共同交易日**对齐的日收益序列（用于相关性）。

    只保留所有标的都有行情的交易日 —— 相关性必须在同一组日期上算。

    股票、ETF、指数分属三个 enriched 目录；只读 `kline_daily_enriched` 会让
    组合里的 ETF 永远"没有共同交易日"，相关性整块算不出来（自选股就踩过）。
    所以按标的前缀选目录，三个目录都读。
    """
    limits: list[str] = []
    want = set(symbols)
    per_date: dict[dt.date, dict[str, float]] = {}

    for name in ("kline_daily_enriched", "kline_etf_enriched", "kline_index_enriched"):
        base = data_dir / name
        if not base.is_dir():
            # 目录缺失不是问题：纯股票组合本来就不需要 ETF/指数 enriched。
            # 只有"某个标的在所有目录里都找不到行情"才值得报。
            continue
        parts = sorted(base.glob("date=*"))[-lookback_days:]
        if not parts:
            limits.append(f"{name} 无分区")
            continue
        for part in parts:
            path = part / "part.parquet"
            if not path.is_file():
                continue
            try:
                frame = pl.read_parquet(path, columns=["symbol", "raw_close"])
            except Exception as exc:  # noqa: BLE001
                limits.append(f"{name}/{part.name} 读取失败: {exc}")
                continue
            values = {
                str(row["symbol"]): float(row["raw_close"])
                for row in frame.iter_rows(named=True)
                if row.get("symbol") in want and row.get("raw_close")
            }
            if values:
                try:
                    day = dt.date.fromisoformat(part.name[len("date="):][:10])
                except ValueError:
                    continue
                per_date.setdefault(day, {}).update(values)

    common_dates = sorted(d for d, v in per_date.items() if set(v) >= want)
    if len(common_dates) < MIN_OVERLAP_DAYS:
        return {}, [], [
            f"共同交易日仅 {len(common_dates)} 天（< {MIN_OVERLAP_DAYS}），相关性无法可靠计算"
        ]
    returns: dict[str, list[float]] = {s: [] for s in symbols}
    prev = None
    for day in common_dates:
        values = per_date[day]
        if prev is not None:
            for sym in symbols:
                before = prev.get(sym)
                now = values.get(sym)
                if before and now and before > 0:
                    returns[sym].append(now / before - 1.0)
        prev = values
    return returns, common_dates, limits


def _empty_enrichment(reason: str) -> dict[str, Any]:
    """可选「专家增强」整体不可用时的最小降级结构。

    前端永远拿得到这 6 个新键，不会因为某个可选模块没装/读失败就整屏空白；
    每个键都带 limitations 把「为什么没有数」如实说出来。
    """
    return {
        "data_quality": {"verdict": "reference", "flags": [], "note": reason,
                         "limitations": [reason]},
        "actionability": {"suggested_cuts": [], "market_state_symbols": [], "note": reason},
        "tail_scenarios": {"correlation_amplified": {}, "historical_stress": [],
                           "limitations": [reason]},
        "fee_passthrough": {"sell_cost_pct": None, "limitations": [reason]},
        "relative_performance": {"limitations": [reason]},
        "factor_attribution": {"limitations": [reason]},
    }


def _load_market_state(data_dir: Path, symbols: list[str]) -> dict[str, dict]:
    """从最新 enriched 分区读当日市场状态，供「今天能减多少」判定。

    fixture / 老数据只有 symbol,date,raw_close 时读不到 amount/volume/跌停列，
    一律 fail-closed 返回空或字段不全的 state —— annotate_executability 会据此
    标注「未做可执行性校验」，而不是假装能减、也不是抛异常打断整份报告。
    """
    want = {str(s).upper() for s in symbols}
    state: dict[str, dict] = {}
    for folder in ("kline_daily_enriched", "kline_etf_enriched", "kline_index_enriched"):
        base = Path(data_dir) / folder
        if not base.is_dir():
            continue
        parts = sorted(base.glob("date=*"))
        if not parts:
            continue
        path = parts[-1] / "part.parquet"
        if not path.is_file():
            continue
        try:
            cols = set(pl.read_parquet_schema(path).keys())
            selected = [
                c for c in ("symbol", "volume", "amount", "turnover_rate",
                            "consecutive_limit_downs")
                if c in cols
            ]
            if "symbol" not in selected:
                continue
            df = pl.read_parquet(path, columns=selected)
        except Exception as exc:  # noqa: BLE001 — 读不到就诚实降级
            logger.debug("market_state read skipped for %s: %s", folder, exc)
            continue
        for row in df.iter_rows(named=True):
            sym = str(row.get("symbol") or "").upper()
            if sym not in want or sym in state:
                continue
            item: dict[str, Any] = {}
            vol = row.get("volume")
            amount = row.get("amount")
            down = row.get("consecutive_limit_downs")
            if vol is not None:
                item["is_suspended"] = float(vol) == 0.0
            if down is not None:
                item["is_limit_down"] = float(down) > 0
            if amount is not None:
                item["turnover"] = float(amount)
            state[sym] = item
    return state


def _load_benchmark_returns(
    data_dir: Path, *, dates: list[str], symbol: str = "000300.SH",
) -> dict[str, float]:
    """读沪深300指数日线算日收益（小数制），按 dates 对齐。

    没有指数维表时返回 {}，relative_performance 会 fail-closed 给「基准数据缺失」，
    不编造超额/β/α。前一分区的收盘作为 prev，故相邻分区即相邻交易日。
    """
    wanted = set(dates)
    if not wanted:
        return {}
    base = Path(data_dir) / "kline_index_enriched"
    if not base.is_dir():
        return {}
    prev: float | None = None
    out: dict[str, float] = {}
    for part in sorted(base.glob("date=*"))[-120:]:
        day = part.name.split("=", 1)[-1]
        path = part / "part.parquet"
        if not path.is_file():
            continue
        try:
            df = pl.read_parquet(path, columns=["symbol", "raw_close"])
            sub = df.filter(pl.col("symbol") == symbol)
            close = float(sub["raw_close"][0]) if sub.height else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("benchmark read skipped for %s: %s", day, exc)
            continue
        if close is None:
            continue
        if day in wanted and prev is not None and prev > 0:
            out[day] = close / prev - 1.0
        prev = close
    return out


def _expert_enrichment(
    *,
    data_dir: Path,
    positions: list[PositionInput],
    returns: dict[str, list[float]],
    common_dates: list[dt.date],
    correlation: dict[str, Any],
    budget: dict[str, Any],
    weights: dict[str, float],
    equity: float | None,
) -> dict[str, Any]:
    """把「眼前这一屏」的专业补强接进风控报告，只返回**新键**。

    设计约束（来自 CONTRIBUTING 与既有测试）：
    - 不改 drawdown_budget / scenario_impact 等纯函数签名与返回值；
    - 不往 report["limitations"] 追加任何文本（test_portfolio_risk 对它有
      白名单遍历断言 + 概念条数断言），所有 flag 只留在新键里；
    - 新模块函数内延迟 import，任一可选能力异常由调用方兜底降级，绝不 500。
    """
    from app.fees import resolve_cost_model
    from app.services import actionability, portfolio_benchmark, risk_data_quality

    symbols = [p.symbol for p in positions]
    # P0-1：returns[sym] 比 common_dates 少 1 根（首日只作 prev），从 [1:] 对齐。
    by_date: dict[str, dict[str, float]] = {
        sym: {
            d.isoformat(): float(series[i])
            for i, d in enumerate(common_dates[1:])
            if i < len(series)
        }
        for sym, series in returns.items()
    }
    data_quality = risk_data_quality.assess_data_quality(
        symbols=symbols,
        returns_by_symbol=by_date,
        as_of=dt.date.today().isoformat(),
        min_common_days=MIN_OVERLAP_DAYS,
    )

    # P0-3：拆「建议减多少」与「今天能减多少」+ 相关性放大/历史压力窗口。
    market_state = _load_market_state(data_dir, symbols)
    executable_cuts = actionability.annotate_executability(
        suggested_cuts=list(budget.get("suggested_cuts") or []),
        market_state=market_state,
    )
    tail = actionability.tail_scenarios(
        weights=weights,
        avg_correlation=correlation.get("average_correlation"),
    )

    # P2-8：用 fees.py 同一套卖出费率给「全部变现到手」的事前估算。
    cost_pct = resolve_cost_model(asset_type="stock").sell_cost_pct()
    mv: float | None = None
    if equity is not None:
        try:
            mv = float(equity) * sum(weights.values())
        except (TypeError, ValueError):
            mv = None
    fee_passthrough = {
        "sell_cost_pct": round(cost_pct, 6),
        "market_value_estimate": None if mv is None else round(mv, 2),
        "net_after_fees_estimate": None if mv is None else round(mv * (1.0 - cost_pct), 2),
        "note": "按 fees.py 卖出综合费率对持仓市值打折的事前估算，"
                "不含单笔最低 5 元佣金；实际到手以成交为准。",
        "limitations": ["sell_cost_pct 不含单笔最低佣金；实际账目走 CostModel.sell_amount"],
    }

    # P1-5：组合日收益（按权重加权）对齐沪深300基准；因子暴露暂无数据源，诚实留空。
    benchmark_dates = [d.isoformat() for d in common_dates[1:]]
    portfolio_returns = {
        day: sum(
            w * float(returns[sym][i])
            for sym, w in weights.items()
            if i < len(returns.get(sym) or [])
        )
        for i, day in enumerate(benchmark_dates)
    }
    relative = portfolio_benchmark.relative_performance(
        portfolio_returns=portfolio_returns,
        benchmark_returns=_load_benchmark_returns(data_dir, dates=benchmark_dates),
    )
    attribution = portfolio_benchmark.factor_attribution(
        weights=weights,
        factor_exposures={},  # 暂无截面因子暴露数据源，不编造
        factor_returns={},
    )

    return {
        "data_quality": data_quality,
        "actionability": {
            "suggested_cuts": executable_cuts,
            "market_state_symbols": sorted(market_state),
            "note": "reduce_by 是风险目标；executable_reduce_by 才是叠加停牌/T+1/跌停/流动性后今日能减的量。",
        },
        "tail_scenarios": tail,
        "fee_passthrough": fee_passthrough,
        "relative_performance": relative,
        "factor_attribution": attribution,
    }


def build_portfolio_report(
    data_dir: Path,
    *,
    holdings: list[dict[str, Any]],
    budget_pct: float = DEFAULT_DRAWDOWN_BUDGET_PCT,
    lookback_days: int = 60,
    equity: float | None = None,
    cash: float | None = None,
    include_concepts: bool = True,
    scenario_shocks: tuple[float, ...] = DEFAULT_SCENARIO_SHOCKS,
    normalize: bool = True,
) -> dict[str, Any]:
    """组合层完整报告。

    ``holdings`` 每项形如 ``{symbol, weight, drawdown_pct?}``；
    行业归属从本地行业表补齐，回撤缺省时按 None 处理并记 limitation。

    ``equity`` / ``cash`` 给了就能把「跌 10% 亏多少钱」与现金比例算成人话；
    不给则只给权重百分点 —— **没有账户资金时不该假装知道亏多少钱**。

    ``normalize``：账户口径要传 False —— 那里的权重分母是**账户权益**，
    合计 < 1 表示"有现金没投"，归一化会把"半仓"算成"满仓"。调用方自报权重
    时保持 True（此时权重的语义只是相对比例）。
    """
    limits: list[str] = []
    industry_map, industry_limits = load_industry_map(data_dir)
    limits.extend(industry_limits)

    positions: list[PositionInput] = []
    for item in holdings:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            limits.append("holdings 里有一项缺少 symbol，已跳过")
            continue
        weight = item.get("weight")
        try:
            weight_value = float(weight) if weight is not None else 0.0
        except (TypeError, ValueError):
            limits.append(f"{symbol} 的 weight 无法解析为数字，按 0 处理")
            weight_value = 0.0
        drawdown = item.get("drawdown_pct")
        try:
            drawdown_value = abs(float(drawdown)) if drawdown is not None else None
        except (TypeError, ValueError):
            limits.append(f"{symbol} 的 drawdown_pct 无法解析为数字，按缺失处理")
            drawdown_value = None
        positions.append(PositionInput(
            symbol=symbol,
            weight=weight_value,
            drawdown_pct=drawdown_value,
            industry=industry_map.get(symbol),
        ))

    cash_ratio = None
    if cash is not None and equity:
        try:
            cash_ratio = max(0.0, float(cash) / float(equity))
        except (TypeError, ValueError, ZeroDivisionError):
            limits.append("cash / equity 无法解析，现金比例未计算")

    if not positions:
        return {
            "positions": [],
            "industry_concentration": industry_concentration([]),
            "concept": concept_exposure([], {}, {}),
            "correlation": {"average_correlation": None, "effective_bets": None, "warnings": []},
            "per_position_risk": {"positions": [], "total_risk_contribution": 0.0, "warnings": []},
            "drawdown_budget": drawdown_budget([], budget_pct=budget_pct),
            "scenario": scenario_impact([], shocks=scenario_shocks, equity=equity),
            "volatility": {},
            "cash_ratio": None if cash_ratio is None else round(cash_ratio, 4),
            "limitations": [*limits, "组合为空，未做任何风险计算"],
            **_empty_enrichment("组合为空，未做数据质量门禁/可执行性/基准归因/费用穿透"),
        }

    # 权重口径：账户口径（normalize=False）下权重合计 < 1 是正常的（现金未投资），
    # 不能归一化掉；调用方自报权重（normalize=True）时归一化，因为那时权重的
    # 语义只是相对比例（前端可能直接传市值，等权假设则传全 1）。
    total_weight = sum(p.weight for p in positions)
    if normalize:
        weights = normalize_weights([p.weight for p in positions])
        positions = [
            PositionInput(symbol=p.symbol, weight=w, drawdown_pct=p.drawdown_pct, industry=p.industry)
            for p, w in zip(positions, weights, strict=True)
        ]
    elif total_weight <= 0:
        limits.append("持仓权重合计为 0，集中度与预算无法计算")
    elif total_weight > 1.0 + 1e-6:
        limits.append(
            f"权重合计 {total_weight:.3f} > 1，超过账户权益；请确认权重口径（应为市值/权益）"
        )

    missing_drawdown = [p.symbol for p in positions if p.drawdown_pct is None]
    derived_drawdown: dict[str, float] = {}
    if missing_drawdown:
        # 调用方没给潜在回撤时，用该票在窗口内的**实际最大回撤**兜底。
        # 理由：没有这个数，单笔风险与回撤预算整块是空的（前端从来不给），
        # 用户看到的是"组合风险已评估"其实什么都没算。口径写进 limitations。
        derived_drawdown, dd_limits = symbol_drawdown_stats(
            data_dir, missing_drawdown, lookback_days=lookback_days
        )
        limits.extend(dd_limits)
        if derived_drawdown:
            positions = [
                PositionInput(
                    symbol=p.symbol,
                    weight=p.weight,
                    drawdown_pct=(p.drawdown_pct if p.drawdown_pct is not None
                                  else derived_drawdown.get(p.symbol)),
                    industry=p.industry,
                )
                for p in positions
            ]
            limits.append(
                f"{len(derived_drawdown)} 笔的潜在回撤取历史实现最大回撤（未由调用方提供）"
            )

    still_missing = [p.symbol for p in positions if p.drawdown_pct is None]
    if still_missing:
        limits.append(
            f"{len(still_missing)} 笔缺少潜在回撤（{ '、'.join(still_missing[:6]) }），"
            "回撤预算只能按已知部分计算"
        )

    returns, common_dates, return_limits = load_daily_returns(
        data_dir, [p.symbol for p in positions], lookback_days=lookback_days
    )
    limits.extend(return_limits)
    if returns:
        matrix, corr_limits = correlation_matrix(returns)
        limits.extend(corr_limits)
        weight_map = {p.symbol: p.weight for p in positions}
        correlation = correlation_summary(matrix, weight_map)
        correlation["matrix"] = {
            a: {b: (None if v is None else round(v, 4)) for b, v in row.items()}
            for a, row in matrix.items()
        }
        correlation["common_days"] = len(common_dates)
    else:
        correlation = {
            "average_correlation": None, "highest_pair": None, "lowest_pair": None,
            "effective_bets": None, "pair_count": 0, "warnings": [],
        }

    concept: dict[str, Any] = {
        "groups": [], "excluded_generic": [], "concept_count": 0,
        "warnings": [], "limitations": [], "clusters": [], "overlaps": [],
    }
    if include_concepts:
        concept_map, coverage, concept_limits = load_concept_map(data_dir)
        limits.extend(concept_limits)
        # 只对**持仓实际带到的标签**做归并：给 390 个标签两两求交没必要，
        # 而且会让冷门标签的包含度噪音进入判定。
        held_tags: set[str] = set()
        for p in positions:
            held_tags.update(concept_map.get(p.symbol) or [])
        clusters, overlaps = build_concept_clusters(concept_map, coverage, held_tags)
        concept = concept_exposure(
            positions, concept_map, coverage, clusters=clusters,
        )
        concept["overlaps"] = overlaps
        # 概念表缺失时 load_concept_map 与 concept_exposure 会各说一句同义的话，
        # 用户看到两条重复的「没有概念数据」只会困惑。表已缺就把后一句丢掉。
        table_missing = any("概念表" in t for t in concept_limits)
        for text in concept.get("limitations") or []:
            if table_missing and "没有概念标签数据" in text:
                continue
            limits.append(text)

    volatilities, vol_limits = symbol_volatility(
        data_dir, [p.symbol for p in positions], lookback_days=lookback_days
    )
    limits.extend(vol_limits)

    budget = drawdown_budget(positions, budget_pct=budget_pct)
    # 可选「专家增强」整体降级：任一新模块异常都只让这块显示「参考」，
    # 不让一份本来能用的风控报告变成 500，也不污染上面的 limitations 白名单。
    try:
        enrichment = _expert_enrichment(
            data_dir=data_dir,
            positions=positions,
            returns=returns,
            common_dates=common_dates,
            correlation=correlation,
            budget=budget,
            weights={p.symbol: p.weight for p in positions},
            equity=equity,
        )
    except Exception as exc:  # noqa: BLE001 — 增强层永远不让主报告失败
        logger.warning("portfolio expert enrichment degraded: %s", exc)
        enrichment = _empty_enrichment(f"增强层不可用：{exc}")

    return {
        "positions": [p.to_dict() for p in positions],
        "industry_concentration": industry_concentration(positions),
        "concept": concept,
        "correlation": correlation,
        "per_position_risk": per_position_risk(positions),
        "drawdown_budget": budget,
        "scenario": scenario_impact(positions, shocks=scenario_shocks, equity=equity),
        "volatility": volatilities,
        "cash_ratio": None if cash_ratio is None else round(cash_ratio, 4),
        "invested_ratio": round(sum(p.weight for p in positions), 4),
        "limitations": list(dict.fromkeys(limits)),
        **enrichment,
    }


__all__ = [
    "DEFAULT_CLUSTER_CONTAIN_THRESHOLD",
    "DEFAULT_CLUSTER_MARGIN",
    "DEFAULT_CLUSTER_MIN_COVERAGE",
    "DEFAULT_CONCEPT_MAX_COVERAGE_PCT",
    "DEFAULT_DRAWDOWN_BUDGET_PCT",
    "DEFAULT_SCENARIO_SHOCKS",
    "MIN_OVERLAP_DAYS",
    "PositionInput",
    "build_concept_clusters",
    "build_portfolio_report",
    "concept_exposure",
    "correlation_matrix",
    "correlation_summary",
    "drawdown_budget",
    "industry_concentration",
    "load_concept_map",
    "load_daily_returns",
    "load_industry_map",
    "load_price_series",
    "normalize_weights",
    "per_position_risk",
    "scenario_impact",
    "simulate_buy",
    "symbol_drawdown_stats",
    "symbol_volatility",
]
