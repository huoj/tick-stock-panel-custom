"""按日 point-in-time (PIT) 股票池 —— 回测里的「某天哪些票能买、哪些是 ST」。

为什么需要它
------------
`backtest/matrix.py` 的 ``exclude_st`` 原来用 ``market.names``（**当前**名称快照）
判断 ``ST/*ST/退``：那是"今天"的状态，不是历史某一天的状态。于是

- 一只票 2020 年是 ST、2023 年摘帽，回测 2020 年时它却被当成正常票放进来；
- 一只票今天叫 ``*STXX``、2019 年还不是 ST，回测 2019 年时它被整段剔除；
- 已经退市的票因为维表里没名字（或名字带"退"）被当成从未存在过 —— 幸存者偏差。

`evidence.py` 自己也把「历史股票池及静态维表的时点正确性需另行核实」列为限制。
`data_pit.py` 只解决了**财务快照**的 PIT，股票池/状态维表的 PIT 一直是空白。

本模块做什么
------------
把「上市日 / ST 区间 / 停牌日 / 退市日」这些**带日期的事件**展开成逐日的
point-in-time 股票池矩阵：每个交易日、每只票，给出

- ``is_st``：当天是不是 ST（用于 ``exclude_st``）；
- ``tradable``：当天能不能交易（已上市、未退市、未停牌、满足最少上市天数）；
- ``listing_age_days`` / ``suspended``：给风控与归因用的附加事实。

矩阵形状是 ``(len(dates), len(symbols))``，与 ``MarketDataMatrix`` 的
``(日期, 标的)`` 一致，可以直接作为 ``market.fields["is_st"]`` 喂给
``build_basic_filter_mask`` —— 那里会**逐时点**过滤，而不是拿一个静态
asset_mask 广播到所有日期。

诚实边界
--------
- 本模块只做「事件 → 逐日矩阵」的确定性展开，**不负责采集事件**。
  ST 区间、停牌日、上市/退市日必须由调用方从带日期的维表/公告给它；
  没有事件数据时它会明确说「退回当前名称快照，历史时点正确性未核实」，
  绝不假装已经做了 PIT。
- 只处理「已声明的事件」。供应商没给 ST 历史、没给历史股票池，
  这里也推不出来 —— 那是数据源的缺口，不是本模块能补的。

纯标准库、确定性、fail-closed：日期格式坏、区间倒置都不抛异常，跳过并记进
``limitations``。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

__all__ = [
    "build_pit_universe",
    "build_is_st_field",
    "st_snapshot_fallback",
    "describe_universe_coverage",
]


def _day(text: Any) -> dt.date | None:
    """把 YYYY-MM-DD（或带时间的 ISO 串前 10 位）解析成 date；坏值返回 None。"""
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def st_snapshot_fallback(names: list[str] | tuple[str, ...]) -> list[bool]:
    """现有 ``exclude_st`` 的静态逻辑（保留下来做回退对照）。

    只看**当前**名称里有没有 ``ST`` / ``退`` 字样。它是"今天"的快照，
    不能代表历史某一天；仅在拿不到 PIT 事件时兜底，并由
    :func:`describe_universe_coverage` 明确标注。
    """
    return [any(token in str(name).upper() for token in ("ST", "*ST", "退")) for name in names]


def build_pit_universe(
    *,
    dates: list[str],
    symbols: list[str],
    listing_dates: dict[str, str] | None = None,
    st_periods: dict[str, list[tuple[str, str]]] | None = None,
    suspended_dates: dict[str, set[str]] | None = None,
    delisting_dates: dict[str, str] | None = None,
    min_listing_days: int = 0,
) -> dict[str, Any]:
    """构建逐日 point-in-time 股票池。

    参数
    ----
    dates
        升序的交易日 ``YYYY-MM-DD``。
    symbols
        列顺序（与 ``MarketDataMatrix.symbols`` 一致）。
    listing_dates / delisting_dates
        ``{symbol: YYYY-MM-DD}``。上市日之前、退市日之后都不可交易。
    st_periods
        ``{symbol: [(start, end), ...]}``，区间**闭区间**、按交易日字符串比较，
        落在区间内即当天 ST。
    suspended_dates
        ``{symbol: {"YYYY-MM-DD", ...}}``，当天停牌即不可交易。
    min_listing_days
        上市不足 N 个自然日的新股不进池（0 = 不限制）。

    返回
    ----
    ``{dates, symbols, is_st, tradable, listing_age_days, suspended,
       source, limitations}``，其中四个矩阵都是 ``(日期, 标的)`` 的 list-of-list。
    """
    limitations: list[str] = []
    parsed_dates: list[dt.date | None] = []
    for text in dates:
        d = _day(text)
        if d is None:
            limitations.append(f"日期 {text!r} 无法解析为 YYYY-MM-DD，该日按全不可交易处理")
        parsed_dates.append(d)

    listing = {s: _day(v) for s, v in (listing_dates or {}).items()}
    delisting = {s: _day(v) for s, v in (delisting_dates or {}).items()}
    suspended = {s: {_day(x) for x in days} - {None} for s, days in (suspended_dates or {}).items()}

    st_ranges: dict[str, list[tuple[dt.date, dt.date]]] = {}
    for symbol, ranges in (st_periods or {}).items():
        good: list[tuple[dt.date, dt.date]] = []
        for pair in ranges:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                limitations.append(f"{symbol} 的 ST 区间 {pair!r} 不是 (start,end)，已跳过")
                continue
            start, end = _day(pair[0]), _day(pair[1])
            if start is None or end is None:
                limitations.append(f"{symbol} 的 ST 区间 {pair!r} 日期非法，已跳过")
                continue
            if start > end:
                limitations.append(f"{symbol} 的 ST 区间 {pair!r} 起晚于止，已跳过")
                continue
            good.append((start, end))
        st_ranges[symbol] = good

    has_events = any([listing_dates, st_periods, suspended_dates, delisting_dates])
    if not has_events:
        limitations.append(
            "没有任何带日期的上市/ST/停牌/退市事件：历史股票池时点正确性未核实，"
            "调用方应退回当前名称快照并如实披露"
        )

    is_st: list[list[bool]] = []
    tradable: list[list[bool]] = []
    listing_age: list[list[int | None]] = []
    suspended_matrix: list[list[bool]] = []

    for d in parsed_dates:
        st_row: list[bool] = []
        tradable_row: list[bool] = []
        age_row: list[int | None] = []
        susp_row: list[bool] = []
        for symbol in symbols:
            if d is None:
                st_row.append(False)
                tradable_row.append(False)
                age_row.append(None)
                susp_row.append(False)
                continue

            flagged = any(start <= d <= end for start, end in st_ranges.get(symbol, []))
            st_row.append(flagged)

            born = listing.get(symbol)
            age = (d - born).days if born is not None else None
            age_row.append(age)

            halted = d in suspended.get(symbol, set())
            susp_row.append(halted)

            ok = True
            if born is not None and d < born:
                ok = False
            gone = delisting.get(symbol)
            if gone is not None and d > gone:
                ok = False
            if halted:
                ok = False
            if age is not None and age < min_listing_days:
                ok = False
            if flagged:
                # ST 不是"不能交易"，而是"能交易但 exclude_st 要剔除"。
                # 是否可交易留给 exclude_st 决定，这里只标事实。
                pass
            tradable_row.append(ok)
        is_st.append(st_row)
        tradable.append(tradable_row)
        listing_age.append(age_row)
        suspended_matrix.append(susp_row)

    return {
        "dates": list(dates),
        "symbols": list(symbols),
        "is_st": is_st,
        "tradable": tradable,
        "listing_age_days": listing_age,
        "suspended": suspended_matrix,
        "source": "pit" if has_events else "snapshot_fallback",
        "limitations": list(dict.fromkeys(limitations)),
    }


def build_is_st_field(
    *,
    dates: list[str],
    symbols: list[str],
    st_periods: dict[str, list[tuple[str, str]]],
) -> list[list[bool]]:
    """只取 ``is_st`` 矩阵，方便直接塞进 ``market.fields["is_st"]``。

    ``build_basic_filter_mask`` 看到这个字段就会逐时点剔除 ST，
    不再用当前名称快照广播到全部历史日期。
    """
    return build_pit_universe(
        dates=dates, symbols=symbols, st_periods=st_periods
    )["is_st"]


def describe_universe_coverage(
    *,
    has_pit_field: bool,
    exclude_st: bool = True,
    n_dates: int = 0,
    n_symbols: int = 0,
) -> dict[str, Any]:
    """给 evidence / API 读的「股票池口径」说明。

    ``has_pit_field`` = ``market.fields`` 里有没有逐日 ``is_st`` 矩阵。
    有 → ST 过滤按 as_of 逐日判定；没有 → 仍是当前名称快照，必须明说。
    """
    if not exclude_st:
        return {
            "source": "not_applicable",
            "limitations": [],
            "note": "本次未启用 exclude_st，股票池时点口径不影响结果",
        }
    if has_pit_field:
        return {
            "source": "pit",
            "n_dates": n_dates,
            "n_symbols": n_symbols,
            "limitations": [],
            "note": "ST 过滤按每个交易日的 point-in-time 标记逐日判定（不是当前名称快照）",
        }
    return {
        "source": "snapshot_fallback",
        "n_dates": n_dates,
        "n_symbols": n_symbols,
        "limitations": [
            "exclude_st 用的是当前名称快照，历史某日的 ST/*ST/退市状态未做时点还原；"
            "历史股票池与状态维表的时点正确性需另行核实（幸存者偏差风险）"
        ],
        "note": "未加载逐日 PIT 标记，ST 过滤退回到当前名称快照",
    }
