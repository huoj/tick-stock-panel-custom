"""组合风险报告的数据质量与截止日评估（独立模块，仅依赖标准库）。

为什么单独成模块：`build_portfolio_report` 负责风险指标计算，本模块只回答
「这些指标建立在多新、多全的数据上」。两者解耦后，主报告可以把质量结论作为
一个键挂进结果，而不必把日期解析、覆盖率统计混进相关性/波动率的计算路径；
反向 import 主报告会形成循环依赖，因此本模块刻意不认识任何组合风险模块。

口径上的两个关键选择（其余细节见各函数 docstring）：

* 共同交易日取「所有标的都同时有收益数据」的日期交集，而不是任一标的的
  样本量。相关性/协方差矩阵要求每一行都落在同一组观测日上；若按各自样本
  量分别计算，样本窗口不同会让矩阵口径不一致、甚至不再半正定，组合波动率
  被系统性低估。因此以交集大小作为有效样本量，低于门槛就降级为参考结论。
* 新鲜度用自然日差而不是交易日差。数据变旧是日历时间问题：周末、节假日
  照样过去，行情停在原地就意味着持仓风险数字在失真。标准库没有交易日历，
  用自然日既能保证纯函数、确定性、零外部依赖，也比数交易日更保守，能更早
  提醒投资者去更新行情。

本模块是纯函数：不读文件、不联网、不访问数据库、不修改入参；遇到缺数据、
坏日期一律返回带 `limitations` 的保守结论，不抛异常（fail-closed）。
"""

from datetime import date

__all__ = ["assess_data_quality"]


def _parse_iso_date(value: object) -> date | None:
    """严格解析 YYYY-MM-DD；非法输入返回 None 而不是抛异常。

    为什么严格：日期是本模块唯一的坐标。若放任 '2026-1-5'、'20261105' 或
    任意字符串进入并集，字符串字典序就不再等于时间序，`data_through`、
    `first`/`last` 和自然日差会一起失真。宁可丢弃坏键并在 limitations 里
    记账，也不能让一个坏日期悄悄改变结论（fail-closed）。
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _normalize_symbols(raw: object) -> list[str]:
    """把 symbols 归一为保序去重的非空字符串列表；坏输入退化为 []。

    为什么去重：交集与 `per_symbol_coverage` 都以标的为键，重复标的会让同一
    只票被算两遍（交集不变但覆盖率口径混乱）。为什么保序：`flags` 要可复现，
    按调用方给出的顺序输出，排查问题时和持仓列表一一对得上。
    """
    if not isinstance(raw, list | tuple):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _dates_for(raw: object) -> list[str]:
    """取单个标的的合法收益日期，升序去重；非 dict 或坏键一律跳过。

    为什么只认日期键而不看收益数值：本模块评估的是「数据覆盖到哪里、齐不齐」，
    收益值是否正确、是否复权属于另一条校验链。dict 的键天然唯一，排序后即
    ISO 时间序，可直接用于交集与 first/last。
    """
    if not isinstance(raw, dict):
        return []
    return sorted(key for key in raw if _parse_iso_date(key) is not None)


def _safe_threshold(raw: object, default: int) -> int:
    """把门槛参数收敛为非负 int；坏输入回退默认值，避免比较时抛 TypeError。

    为什么：门槛参与 `common_days < min_common_days` 这类比较，调用方误传
    None 或字符串会让整条评估链崩掉；宁可用保守默认值继续给出 reference 结论。
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return default
    return raw


def _no_data_result(
    *, as_of_value: str, symbols: list[str], limitations: list[str]
) -> dict:
    """构造「无数据」的保守结论。

    为什么坚持 fail-closed：没有标的或没有收益序列时，共同交易日、截止日、
    覆盖率全部没有定义；与其返回 ok 让上层误用，不如明确标成 reference，并在
    flags/limitations 里写清原因，保证调用链不中断、不抛异常。
    """
    coverage = {
        symbol: {"bars": 0, "first": None, "last": None, "missing_recent": 0}
        for symbol in symbols
    }
    return {
        "as_of": as_of_value,
        "data_through": None,
        "trading_dates": [],
        "per_symbol_coverage": coverage,
        "common_days": 0,
        "flags": ["无数据：未提供标的或收益序列, 无法评估数据质量"],
        "stale": False,
        "insufficient": True,
        "verdict": "reference",
        "note": "暂无可用的行情数据, 本次风险测算结果仅供参考, 建议补齐数据后重算。",
        "limitations": limitations,
    }


def assess_data_quality(
    *,
    symbols: list[str],
    returns_by_symbol: dict[str, dict[str, float]],
    as_of: str | None = None,
    min_common_days: int = 20,
    freshness_days: int = 5,
) -> dict:
    """评估组合收益序列的截止日、覆盖完整度与新鲜度，返回纯 dict 结论。

    为什么用「所有标的共同交易日」作分母：相关性、协方差与组合波动率要求每只
    标的在同一组日期上都有观测；用任一标的的样本量当分母，等于拿不同窗口拼
    矩阵，样本越参差越容易低估共振风险。因此 `common_days` 取日期交集大小，
    低于 `min_common_days` 就把结论降级为 reference，并写明「相关性仅按 N 个
    共同交易日计算」。

    为什么 stale 用自然日：`as_of - data_through` 的自然日差直接反映「这份行情
    离评估日过去了多久」，周末和节假日同样计入，因为停更的行情在休市日一样在
    变旧；标准库无交易日历，自然日差无需外部数据即可确定性计算，且比交易日数
    更保守，能更早触发提醒。严格大于 `freshness_days` 才置 stale，与阈值语义一致。

    为什么 `missing_recent` 以并集交易日全长为窗口：并集本身就是输入样本里出现
    过的全部交易日，「最近 len(trading_dates) 个」即整个样本窗口；用「并集长度
    − 该标的 bars」度量缺口，不引入交易日历假设，也不会把样本外的未知交易日误
    算成缺失。

    返回键固定：as_of / data_through / trading_dates / per_symbol_coverage /
    common_days / flags / stale / insufficient / verdict / note / limitations。
    """
    as_of_value = as_of if isinstance(as_of, str) and as_of else "unknown"
    min_days = _safe_threshold(min_common_days, 20)
    fresh_days = _safe_threshold(freshness_days, 5)

    base_limitations = [
        "仅统计 symbols 列出标的的收益序列, 其余输入不参与评估",
        "共同交易日按所有标的同时有收益的日期计算, 未含完整交易日历, 缺失根数仅为样本内缺口",
        "新鲜度按自然日差计算, 不区分周末、节假日与停牌",
        "不校验收益数值是否正确、是否复权或是否被填充",
    ]

    normalized = _normalize_symbols(symbols)
    raw_returns = returns_by_symbol if isinstance(returns_by_symbol, dict) else {}
    dates_by_symbol = {s: _dates_for(raw_returns.get(s)) for s in normalized}

    trading_dates = sorted({d for dates in dates_by_symbol.values() for d in dates})
    if not normalized or not trading_dates:
        no_data_limitations = [
            *base_limitations,
            "无标的或无收益数据, 共同交易日、覆盖率与新鲜度口径均不成立",
        ]
        if _parse_iso_date(as_of) is None:
            no_data_limitations.append("未提供合法评估日, 新鲜度未判定")
        return _no_data_result(
            as_of_value=as_of_value, symbols=normalized, limitations=no_data_limitations
        )

    data_through = trading_dates[-1]
    window = len(trading_dates)
    per_symbol_coverage = {}
    for symbol in normalized:
        own = dates_by_symbol[symbol]
        per_symbol_coverage[symbol] = {
            "bars": len(own),
            "first": own[0] if own else None,
            "last": own[-1] if own else None,
            "missing_recent": window - len(own),
        }

    date_sets = {s: set(dates_by_symbol[s]) for s in normalized}
    common_days = sum(1 for d in trading_dates if all(d in date_sets[s] for s in normalized))

    as_of_date = _parse_iso_date(as_of)
    through_date = _parse_iso_date(data_through)
    gap_days = (
        (as_of_date - through_date).days
        if as_of_date is not None and through_date is not None
        else None
    )
    stale = gap_days is not None and gap_days > fresh_days
    insufficient = common_days < min_days or any(
        per_symbol_coverage[s]["bars"] == 0 for s in normalized
    )

    flags: list[str] = []
    for symbol in normalized:
        missing = per_symbol_coverage[symbol]["missing_recent"]
        if missing > 0:
            flags.append(f"「{symbol} 近 {window} 个交易日缺 {missing} 根 K 线」")
    if common_days < min_days:
        flags.append(f"「相关性仅按 {common_days} 个共同交易日计算, 低于 {min_days} 日门槛」")
    if stale:
        flags.append(f"「数据截至 {data_through}, 距今 {gap_days} 个自然日, 可能不新鲜」")

    limitations = list(base_limitations)
    if as_of_date is None:
        reason = "未提供评估日(as_of)" if as_of_value == "unknown" else "评估日不是 YYYY-MM-DD"
        limitations.append(f"{reason}, 新鲜度未判定")
    if gap_days is not None and gap_days < 0:
        limitations.append(
            f"评估日 {as_of_value} 早于数据截止日 {data_through}, 新鲜度未判定, 请核对日期口径"
        )
    for symbol in normalized:
        if per_symbol_coverage[symbol]["bars"] == 0:
            limitations.append(f"「{symbol} 无收益序列, 共同交易日按 0 处理」")

    if stale and insufficient:
        note = (
            f"数据截至 {data_through}, 距今 {gap_days} 个自然日, 且只有 {common_days} "
            "天是所有标的都有的行情, 结果仅供参考, 建议更新数据后重算。"
        )
    elif stale:
        note = (
            f"数据截至 {data_through}, 距今 {gap_days} 个自然日, 行情可能已过时, "
            "风险数字先别当真, 建议更新数据。"
        )
    elif insufficient:
        note = f"只有 {common_days} 天是所有标的都有的行情, 样本偏短, 风险测算仅供参考。"
    else:
        note = "数据完整且新鲜, 组合风险测算可作为参考。"

    verdict = "ok" if not stale and not insufficient else "reference"
    return {
        "as_of": as_of_value,
        "data_through": data_through,
        "trading_dates": trading_dates,
        "per_symbol_coverage": per_symbol_coverage,
        "common_days": common_days,
        "flags": flags,
        "stale": stale,
        "insufficient": insufficient,
        "verdict": verdict,
        "note": note,
        "limitations": limitations,
    }
