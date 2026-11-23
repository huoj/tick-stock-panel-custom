"""行业强度选股 — 5均线6档强度打分 + 行业内聚合排名。

来源: 项目背景介绍《如何通过量化程序做到行业轮动跟踪》核心逻辑的矩阵化落地。
文档三步漏斗中, 本策略实现其中可在回测矩阵内落地的两步:
  - 个股强度: 5 条均线(5/13/21/34/60)把价格分成 6 个互斥区间, 打 1-6 分
    (分越高 = 站上的均线越多 = 越强), 与文档 np.select 口径一致;
  - 板块强弱: 行业内成分股强度取均值 = 板块得分, 供行业内选强票。
文档另外两步(两周稳定/涨停潮、缠论买点)分别由 TSP 现有的 sector_rotation
异动监控与(未实现的)缠论模块承担, 本策略不重复造轮子。

ma13/ma21/ma34 无需注册: 矩阵路径 matrix_feature(market,"ma<N>") 运行时现算
(见 backtest/matrix.py 的 ma<N> 模式匹配), 不涉及落盘列/缓存版本。
"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "industry_strength_rank",
    "name": "行业强度选股",
    "description": "5均线(5/13/21/34/60)6档强度打分, 站上全部均线+量能配合入选; 板块强弱=成分股强度均值",
    "tags": ["行业轮动", "多均线", "强度打分"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "min_strength",
            "label": "最低强度档位(6=站上全部均线)",
            "type": "float",
            "default": 6.0,
            "min": 4.0,
            "max": 6.0,
            "step": 1.0,
        },
        {"id": "require_volume", "label": "要求量能配合", "type": "bool", "default": True},
        {
            "id": "vol_ratio_min",
            "label": "最低量比",
            "type": "float",
            "default": 1.5,
            "min": 0.5,
            "max": 5.0,
            "step": 0.1,
        },
        {
            "id": "require_persistence",
            "label": "要求强度持续(排除一日游)",
            "type": "bool",
            "default": True,
        },
        {
            "id": "persistence_days",
            "label": "强度回溯窗口(天)",
            "type": "int",
            "default": 5,
            "min": 2,
            "max": 10,
            "step": 1,
        },
        {
            "id": "min_strong_days",
            "label": "窗口内强势天数下限",
            "type": "int",
            "default": 3,
            "min": 1,
            "max": 10,
            "step": 1,
        },
    ],
    "scoring": {"momentum_20d": 0.35, "vol_ratio_5d": 0.3, "ma60_bias": 0.35},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ma20_golden_60"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 20


def ma_strength_score(market: MarketDataMatrix) -> np.ndarray:
    """逐股逐日 5 均线 6 档强度分 (1=最弱, 6=站上全部均线)。

    互斥区间口径与文档 _calculate_code_strength_up 完全一致:
      close<=ma5→1, ma5<close<=ma13→2, ma13<close<=ma21→3,
      ma21<close<=ma34→4, ma34<close<=ma60→5, close>ma60→6。
    缺数(某均线为 nan)落 default=-1, 不误判为强。
    """
    close = market.close
    ma5 = matrix_feature(market, "ma5")
    ma13 = matrix_feature(market, "ma13")
    ma21 = matrix_feature(market, "ma21")
    ma34 = matrix_feature(market, "ma34")
    ma60 = matrix_feature(market, "ma60")
    return np.select(
        [
            close <= ma5,
            (close > ma5) & (close <= ma13),
            (close > ma13) & (close <= ma21),
            (close > ma21) & (close <= ma34),
            (close > ma34) & (close <= ma60),
            close > ma60,
        ],
        [1, 2, 3, 4, 5, 6],
        default=-1,
    )


def industry_strength_aggregate(
    strengths: dict[str, float],
    symbol_industry: dict[str, str],
    min_members: int = 2,
) -> list[dict]:
    """板块强弱 = 成分股强度均值 (文档核心聚合, 纯函数可独立测试)。

    strengths: symbol -> 最新强度分(1-6); symbol_industry: symbol -> 行业名。
    返回按均分降序的行业榜 [{industry, avg_strength, n_members, top_symbols}]。
    成员数 < min_members 的行业剔除(避免单只票代表一个板块)。
    """
    buckets: dict[str, list[tuple[str, float]]] = {}
    for sym, industry in symbol_industry.items():
        if sym in strengths and strengths[sym] > 0:
            buckets.setdefault(industry, []).append((sym, strengths[sym]))
    out: list[dict] = []
    for industry, members in buckets.items():
        if len(members) < min_members:
            continue
        ranked = sorted(members, key=lambda pair: -pair[1])
        avg = sum(score for _, score in members) / len(members)
        out.append(
            {
                "industry": industry,
                "avg_strength": round(avg, 4),
                "n_members": len(members),
                "top_symbols": [sym for sym, _ in ranked[:3]],
            }
        )
    return sorted(out, key=lambda row: -row["avg_strength"])


def is_stable_strong(history: list[float], max_cv: float = 0.05, min_days: int = 5) -> bool:
    """文档强势板块条件1: 过去两周得分平稳(变异系数 < max_cv)。

    history 为行业得分时间序列(由 industry_strength_aggregate 逐日产出)。
    变异系数 = 标准差/均值, 无量纲, 比绝对波动阈值更适合不同得分量级。
    """
    if len(history) < min_days:
        return False
    arr = np.asarray(history, dtype=float)
    mean = float(np.mean(arr))
    if mean <= 0:
        return False
    return float(np.std(arr)) / mean < max_cv


def strength_persistence_mask(
    strength: np.ndarray,
    strong_level: int,
    days: int,
    min_strong_days: int,
) -> np.ndarray:
    """个股强度持续性掩码: 排除"今天才冲上强势档"的一日游 (纯函数可独立测试)。

    strength: (T, N) 逐日逐股强度分 (ma_strength_score 输出)。
    返回 (T, N) 布尔: 每个时点 t, 过去 days 天(含 t)中强度 >= strong_level
    的天数达到 min_strong_days 才为 True。

    逐时点各用自己的历史(不只看最后一行), 因此可直接与同样 (T,N) 的 entry
    相乘接入矩阵引擎, 无未来函数, 回测口径正确。停牌日在 ma_strength_score
    落 -1(不算强势), 天然不被误计为持续。历史不足时窗口更短, 计数自然偏少。
    """
    arr = np.asarray(strength)
    strong = (arr >= strong_level).astype(np.int16)  # (T, N)
    window = max(2, int(days))
    need = min(max(1, int(min_strong_days)), window)
    count = np.zeros_like(strong)
    for k in range(window):  # t 时刻累加 t, t-1, ..., t-window+1
        if k == 0:
            count += strong
        else:
            count[k:] += strong[:-k]
    return count >= need


def stable_industry_leaders(
    strength_history: list[dict[str, float]],
    symbol_industry: dict[str, str],
    *,
    max_cv: float = 0.05,
    min_days: int = 5,
) -> list[dict]:
    """稳定强势行业榜 (文档"两周得分波动<5%"的完整承接, 纯函数可独立测试)。

    strength_history: 逐日的 {symbol: 强度分} 序列(由 ma_strength_score 逐日产出)。
    先按 symbol_industry 逐日聚合成各行业得分时间序列, 再用 is_stable_strong
    判定稳定, 返回均分降序的 [{industry, avg_strength, n_days, cv}]。

    与 industry_strength_aggregate(单日截面榜)互补: 那个看"今天谁最强",
    这个看"谁连续两周稳在高位"——过滤掉单日脉冲的一日游板块。
    """
    if not strength_history:
        return []
    per_industry: dict[str, list[float]] = {}
    for day_scores in strength_history:
        daily = industry_strength_aggregate(day_scores, symbol_industry, min_members=1)
        for row in daily:
            per_industry.setdefault(row["industry"], []).append(row["avg_strength"])
    out: list[dict] = []
    for industry, series in per_industry.items():
        if len(series) < min_days:
            continue
        arr = np.asarray(series, dtype=float)
        mean = float(np.mean(arr))
        if mean <= 0:
            continue
        cv = float(np.std(arr)) / mean
        if cv >= max_cv:
            continue
        out.append(
            {
                "industry": industry,
                "avg_strength": round(mean, 4),
                "n_days": len(series),
                "cv": round(cv, 4),
            }
        )
    return sorted(out, key=lambda row: -row["avg_strength"])


class IndustryStrengthRankMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        # ma60 需 60 根预热; 强度持续性过滤还要再往前看 persistence_days 天,
        # 那几天的强度分同样依赖 ma60, 故预热窗口一并加长。
        persist_days = int(params.get("persistence_days", 5)) if bool(params.get("require_persistence", True)) else 0
        return 60 + persist_days

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        strength = ma_strength_score(market)
        min_strength = int(float(params.get("min_strength", 6.0)))
        entry = strength >= min_strength
        # 站上季线(牛熊分界), 避免空头排列里短暂反弹被当成强势
        entry &= market.close > matrix_feature(market, "ma60")
        if bool(params.get("require_volume", True)):
            entry &= matrix_feature(market, "vol_ratio_5d") >= float(
                params.get("vol_ratio_min", 1.5)
            )
        # 第2点: 强度持续性过滤(排除一日游)——最近 N 天里至少 M 天在强势档。
        # 只在 min_strength > 1 时生效(1档是"跌破MA5"的弱势区, 做持续性无意义)。
        if bool(params.get("require_persistence", True)) and min_strength > 1:
            persist = strength_persistence_mask(
                strength,
                strong_level=min_strength,
                days=int(params.get("persistence_days", 5)),
                min_strong_days=int(params.get("min_strong_days", 3)),
            )
            entry &= persist
        # 出场: 强度档位跌破(收盘跌破 MA20 的当日)
        ma20 = matrix_feature(market, "ma20")
        exit_ = (market.close < ma20) & (shift(market.close, 1) >= shift(ma20, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ma20_golden_60",),
            exit_signal_ids=("signal_ma20_breakdown",),
        )


MATRIX_STRATEGY = IndustryStrengthRankMatrixStrategy()
