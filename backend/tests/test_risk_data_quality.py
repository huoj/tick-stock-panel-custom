"""`risk_data_quality.assess_data_quality` 的纯内存单测。

为什么只造内存 dict：被测函数是零 IO 的纯函数，用真实日期字符串拼收益序列即可
覆盖「完整性 / 覆盖缺口 / 共同交易日不足 / 过期 / 空输入 / 返回类型」六类分支，
不需要数据库、网络或行情夹具，测试确定性且跑得快。
"""

from datetime import date, timedelta

from app.services.risk_data_quality import assess_data_quality

REQUIRED_KEYS = {
    "as_of",
    "data_through",
    "trading_dates",
    "per_symbol_coverage",
    "common_days",
    "flags",
    "stale",
    "insufficient",
    "verdict",
    "note",
    "limitations",
}


def _dates(start: str, count: int) -> list[str]:
    """从 start 起生成 count 个连续 ISO 日期。

    为什么用连续自然日：被测模块只把「出现过的日期」当交易日、不内置交易日历，
    连续日期足以稳定构造并集/交集；新鲜度另由 as_of 的自然日差控制，互不干扰。
    """
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _returns(dates: list[str], base: float = 0.001) -> dict[str, float]:
    """给日期列表配一组确定性的收益数值。

    为什么数值本身不重要：本模块只统计日期覆盖，不校验收益值；这里只为满足
    dict[str, float] 的入参形状，取固定小数值便于人工核对。
    """
    return {day: base + i * 0.0001 for i, day in enumerate(dates)}


def test_full_complete_data_is_ok():
    """两只在 22 个交易日上都有数据的标的 → ok，且核心计数精确。"""
    dates = _dates("2026-11-01", 22)
    result = assess_data_quality(
        symbols=["600001.SH", "000002.SZ"],
        returns_by_symbol={
            "600001.SH": _returns(dates),
            "000002.SZ": _returns(dates, base=0.002),
        },
        as_of=dates[-1],
    )
    assert result.keys() >= REQUIRED_KEYS
    assert result["verdict"] == "ok"
    assert result["stale"] is False
    assert result["insufficient"] is False
    assert result["common_days"] == 22
    assert result["data_through"] == dates[-1]
    assert result["trading_dates"] == dates
    assert result["per_symbol_coverage"]["600001.SH"]["bars"] == 22
    assert result["per_symbol_coverage"]["000002.SZ"]["bars"] == 22
    assert result["per_symbol_coverage"]["600001.SH"]["missing_recent"] == 0
    assert result["as_of"] == dates[-1]


def test_missing_bars_reports_gap_flag():
    """某只票缺 3 根 K 线 → flags 含「缺」且 missing_recent > 0。"""
    dates = _dates("2026-11-01", 22)
    result = assess_data_quality(
        symbols=["600001.SH", "000002.SZ"],
        returns_by_symbol={
            "600001.SH": _returns(dates),
            "000002.SZ": _returns(dates[:-3]),
        },
        as_of=dates[-1],
    )
    coverage = result["per_symbol_coverage"]["000002.SZ"]
    assert coverage["bars"] == 19
    assert coverage["missing_recent"] == 3
    assert coverage["last"] == dates[-4]
    assert any("缺" in flag and "000002.SZ" in flag for flag in result["flags"])
    assert not any("缺" in flag and "600001.SH" in flag for flag in result["flags"])


def test_common_days_below_threshold_is_insufficient():
    """交集仅 6 天 < 20 日门槛 → insufficient / reference / 共同交易日说明。"""
    a_dates = _dates("2026-11-01", 10)
    b_dates = _dates("2026-11-05", 10)
    result = assess_data_quality(
        symbols=["A", "B"],
        returns_by_symbol={"A": _returns(a_dates), "B": _returns(b_dates)},
        as_of="2026-11-14",
        min_common_days=20,
    )
    assert result["common_days"] == 6
    assert result["stale"] is False
    assert result["insufficient"] is True
    assert result["verdict"] == "reference"
    assert any("共同交易日" in flag for flag in result["flags"])
    assert any("低于 20 日门槛" in flag for flag in result["flags"])


def test_stale_data_is_reference():
    """评估日比 data_through 晚 30 个自然日 → stale / reference / 自然日提醒。"""
    dates = _dates("2026-10-01", 22)
    as_of = (date.fromisoformat(dates[-1]) + timedelta(days=30)).isoformat()
    result = assess_data_quality(
        symbols=["A", "B"],
        returns_by_symbol={"A": _returns(dates), "B": _returns(dates, base=0.002)},
        as_of=as_of,
        freshness_days=5,
    )
    assert result["common_days"] == 22
    assert result["insufficient"] is False
    assert result["stale"] is True
    assert result["verdict"] == "reference"
    assert f"「数据截至 {dates[-1]}, 距今 30 个自然日, 可能不新鲜」" in result["flags"]


def test_empty_inputs_are_fail_closed():
    """无标的 / 无收益 / 空序列都不抛异常，一律 reference 且标注「无数据」。"""
    cases = [
        {"symbols": [], "returns_by_symbol": {}},
        {"symbols": ["A"], "returns_by_symbol": {}},
        {"symbols": [], "returns_by_symbol": {"A": _returns(_dates("2026-11-01", 3))}},
        {"symbols": ["A"], "returns_by_symbol": {"A": {}}},
        {"symbols": ["A", "B"], "returns_by_symbol": {"A": {}, "B": {}}},
    ]
    for kwargs in cases:
        result = assess_data_quality(**kwargs)
        assert result.keys() >= REQUIRED_KEYS
        assert result["verdict"] == "reference"
        assert result["insufficient"] is True
        assert result["common_days"] == 0
        assert result["data_through"] is None
        assert result["trading_dates"] == []
        assert any("无数据" in flag for flag in result["flags"])
        assert result["limitations"]


def test_note_and_container_types():
    """note 为非空字符串，flags/limitations/trading_dates 等容器类型稳定。"""
    dates = _dates("2026-11-01", 22)
    results = [
        assess_data_quality(
            symbols=["A"], returns_by_symbol={"A": _returns(dates)}, as_of=dates[-1]
        ),
        assess_data_quality(symbols=[], returns_by_symbol={}),
    ]
    for result in results:
        assert isinstance(result["note"], str) and result["note"].strip()
        assert isinstance(result["flags"], list)
        assert isinstance(result["limitations"], list)
        assert isinstance(result["trading_dates"], list)
        assert isinstance(result["per_symbol_coverage"], dict)
        assert isinstance(result["verdict"], str)
        assert result["verdict"] in {"ok", "reference"}
