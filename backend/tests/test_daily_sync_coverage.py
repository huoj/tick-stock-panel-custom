"""指数/ETF 日K同步: 必须校验"最新交易日的数据是否真的到位"。

回归背景 (2026-11-09 15:35 盘后管道):
ETF 全量 1763 只按 100/批 = 18 批, 与股票日K、复权因子同时打免费档 60rpm 通道,
11 批被 429 拒。被拒批次的标的当天数据缺失, 但函数只要 total_rows > 0 就返回,
日志写 "+88 rows" —— 那 88 行是拉取窗口里的历史行, 当天一行没进。

后果: 自选页 ETF 行情停在上一交易日, 而管道报成功, 没人会去查。

判据为什么不是「100% 覆盖」:
ETF 标的表里有一批从未上市/已退市的代码 (实测 1763 只里 44 只从无任何数据),
外加停牌股。卡 100% 会天天误报。基准取「上一有数据交易日的实际覆盖」,
覆盖数掉到基准 90% 以下才判异常 —— 那才是同步真出问题。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services.index_sync import daily_coverage

TODAY = date(2026, 11, 9)
PREV = date(2026, 11, 6)


def _frame(rows: list[tuple[str, date]]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [s for s, _ in rows],
        "date": [d for _, d in rows],
    })


def test_full_coverage_is_complete() -> None:
    df = _frame([("159529.SZ", TODAY), ("159682.SZ", TODAY), ("159586.SZ", TODAY)])

    cov = daily_coverage(df, expected=3, target_date=TODAY)

    assert cov.complete is True
    assert cov.covered == 3
    assert cov.missing == 0


def test_only_history_rows_is_incomplete() -> None:
    """关键回归: 只有历史行、当天一行没有 → 必须报异常。

    这正是 11-09 的真实情形 —— 旧实现 total_rows>0 就算成功。
    """
    df = _frame([("159529.SZ", PREV), ("159682.SZ", PREV)])

    cov = daily_coverage(df, expected=100, target_date=TODAY)

    assert cov.complete is False
    assert cov.covered == 0
    assert cov.missing == 100


def test_a_few_suspensions_are_tolerated() -> None:
    """停牌掉几只属正常, 不得误报 (100/100 中只到 95 仍算正常)。"""
    df = _frame([(f"S{i}.SZ", TODAY) for i in range(95)])

    cov = daily_coverage(df, expected=100, target_date=TODAY)

    assert cov.covered == 95
    assert cov.missing == 5
    assert cov.complete is True     # 95% >= 90%


def test_massive_gap_is_reported() -> None:
    """像 429 打掉大半批次那样的大面积缺失 → 必须报异常。"""
    df = _frame([(f"S{i}.SZ", TODAY) for i in range(40)])

    cov = daily_coverage(df, expected=100, target_date=TODAY)

    assert cov.complete is False
    assert cov.covered == 40
    assert cov.missing == 60


def test_empty_frame_is_incomplete() -> None:
    cov = daily_coverage(pl.DataFrame(), expected=10, target_date=TODAY)

    assert cov.complete is False
    assert cov.covered == 0


def test_failed_symbols_reduce_covered() -> None:
    """显式已知失败的标的不能算成已覆盖 (它们的价可能是旧的)。"""
    df = _frame([("159529.SZ", TODAY), ("159682.SZ", TODAY)])

    cov = daily_coverage(
        df, expected=2, target_date=TODAY, failed_symbols=["159682.SZ"],
    )

    assert cov.covered == 1
    assert cov.complete is False


def test_no_expected_symbols_degrades_gracefully() -> None:
    """expected=0 (无可同步标的) 时不误报失败。"""
    cov = daily_coverage(pl.DataFrame(), expected=0, target_date=TODAY)

    assert cov.complete is True
    assert cov.missing == 0


def test_missing_date_column_is_zero_coverage() -> None:
    """缺 date 列 (异常帧) 视为零覆盖, 不崩。"""
    df = pl.DataFrame({"symbol": ["159529.SZ"]})
    cov = daily_coverage(df, expected=100, target_date=TODAY)

    assert cov.complete is False
    assert cov.covered == 0


def test_describe_mentions_gap_and_threshold() -> None:
    cov = daily_coverage(
        _frame([("A.SZ", TODAY)]), expected=100, target_date=TODAY, asset_label="ETF",
    )
    text = cov.describe()

    assert "ETF" in text
    assert "1/100" in text
    assert "90%" in text
