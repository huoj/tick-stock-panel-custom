"""标的维表同步: 任一交易所拉取失败时不得落盘半张表。

回归背景 (2026-11-09): 盘前同步 09:10 时上海/北京成功、深圳返回 503,
旧实现「只要拉到一些行就整表覆盖落盘」→ instruments.parquet 只剩 2823 行
(SH 2468 + BJ 355, 深市 3068 只蒸发), 而任务终态仍是 succeeded。

后果不是"少几只票":
- enriched 的 turnover_rate 按标的维表的 float_shares 计算, 52% 标的整列为空;
- 流通市值/换手率相关策略筛不出标的;
- instruments 搜索对所有深市代码返回空 ("加自选"搜不到美的/格力/东方财富);
- 模拟盘持仓里的深市标的估值缺股本。

维表是**全市场单表**, 因此"部分成功"必须整体放弃写入: 保留上一版完整维表
(陈旧一天, 可接受) 远好于一张残缺表 (静默错误的财务结果)。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import instrument_sync as isync


def _write_existing(data_dir: Path) -> None:
    """上一版完整维表 (含深市), 是失败时应当原样保留的内容。"""
    out = data_dir / "instruments"
    out.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "code": ["600000", "000001"],
        "exchange": ["SH", "SZ"],
        "region": ["CN", "CN"],
        "type": ["stock", "stock"],
        "listing_date": ["1999-11-10", "1991-04-03"],
        "total_shares": [2.9e10, 1.9e10],
        "float_shares": [2.9e10, 1.9e10],
        "tick_size": [0.01, 0.01],
        "limit_up": [11.0, 12.0],
        "limit_down": [9.0, 10.0],
    }).write_parquet(out / "instruments.parquet")


def _item(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "code": symbol.split(".")[0],
        "exchange": symbol.split(".")[1],
        "region": "CN",
        "type": "stock",
        "ext": {"float_shares": 1.0e9, "total_shares": 1.0e9},
    }


def _fake_client(per_exchange: dict[str, object]) -> SimpleNamespace:
    """per_exchange 的值可以是行列表, 也可以是待抛出的异常实例。"""

    def get_instruments(exchange: str, instrument_type: str = "stock") -> list[dict]:
        value = per_exchange[exchange]
        if isinstance(value, Exception):
            raise value
        return list(value)

    return SimpleNamespace(
        exchanges=SimpleNamespace(get_instruments=get_instruments)
    )


def _use_tickflow_path(
    monkeypatch, per_exchange: dict[str, object], mode: str = "free"
) -> None:
    monkeypatch.setattr(isync, "_fetch_instruments_via_provider", lambda: None)
    monkeypatch.setattr(isync, "get_client", lambda: _fake_client(per_exchange))
    monkeypatch.setattr(isync.tf_client, "current_mode", lambda: mode)


def test_partial_exchange_failure_keeps_existing_table(tmp_path, monkeypatch) -> None:
    """深圳 503 → 整体不写盘, 上一版维表(含深市)原样保留。"""
    _write_existing(tmp_path)
    _use_tickflow_path(
        monkeypatch,
        {
            "SH": [_item("600000.SH")],
            "SZ": RuntimeError("503 Service Unavailable"),
            "BJ": [_item("830799.BJ")],
        },
    )

    with pytest.raises(isync.InstrumentSyncError) as excinfo:
        isync.sync_instruments(tmp_path)

    assert "SZ" in str(excinfo.value)
    assert excinfo.value.failed_exchanges == ("SZ",)

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    # 关键断言: 旧表的深市行没有被「只拉到的沪市/北交所」覆盖掉
    assert df.height == 2
    assert set(df["exchange"].to_list()) == {"SH", "SZ"}
    assert df["float_shares"].is_not_null().sum() == 2


def test_empty_exchange_counts_as_failure(tmp_path, monkeypatch) -> None:
    """空列表与异常同罪: 少一整家交易所同样是残缺表。

    旧实现 `if items:` 会静默跳过空响应, 同样写出缺该市场的维表。
    """
    _write_existing(tmp_path)
    _use_tickflow_path(
        monkeypatch,
        {"SH": [_item("600000.SH")], "SZ": [], "BJ": [_item("830799.BJ")]},
    )

    with pytest.raises(isync.InstrumentSyncError) as excinfo:
        isync.sync_instruments(tmp_path)

    assert "SZ" in str(excinfo.value)
    assert pl.read_parquet(tmp_path / "instruments" / "instruments.parquet").height == 2


def test_first_sync_failure_writes_nothing(tmp_path, monkeypatch) -> None:
    """首次同步就失败: 不能写出半张表, 文件保持不存在。"""
    _use_tickflow_path(
        monkeypatch,
        {"SH": [_item("600000.SH")], "SZ": RuntimeError("timeout"), "BJ": []},
    )

    with pytest.raises(isync.InstrumentSyncError):
        isync.sync_instruments(tmp_path)

    assert not (tmp_path / "instruments" / "instruments.parquet").exists()


def test_all_exchanges_succeed_replaces_table(tmp_path, monkeypatch) -> None:
    """三家都成功才落盘, 且包含全部市场。"""
    _write_existing(tmp_path)
    _use_tickflow_path(
        monkeypatch,
        {
            "SH": [_item("600000.SH")],
            "SZ": [_item("000001.SZ"), _item("300059.SZ")],
            "BJ": [_item("830799.BJ")],
        },
        mode="api_key",
    )

    assert isync.sync_instruments(tmp_path) == 4

    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert set(df["exchange"].to_list()) == {"SH", "SZ", "BJ"}
    assert df["float_shares"].is_not_null().sum() == 4


def test_provider_path_is_not_subject_to_exchange_completeness(
    tmp_path, monkeypatch
) -> None:
    """自定义 provider 一次返回单表, 没有分交易所概念 → 行为不变。"""
    _write_existing(tmp_path)
    monkeypatch.setattr(
        isync, "_fetch_instruments_via_provider",
        lambda: [_item("000001.SZ")],
    )

    assert isync.sync_instruments(tmp_path) == 1
    df = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert df.height == 1
