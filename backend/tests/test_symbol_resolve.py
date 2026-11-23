"""标的解析测试 —— 用户输入名称/裸代码/带前缀代码都要能落到标准 symbol。

背景（真实 bug）: 行动卡与持仓总览原来直接把输入 ``.upper()`` 当 symbol 查日线,
用户输入「美的」→ ``GET /api/action-card/美的`` 返回 404
``美的 没有可用日线数据 (asset_type=stock)``。数据其实是齐的, 缺的是"名称→代码"这一步。

设计取舍（写进实现, 这里锁住）:
- 「美的」同时命中 000527.SZ 美的电器 与 000333.SZ 美的集团 ⇒ **报歧义, 不擅自挑一个**。
  猜错的代价是用户看着别家公司的行动卡做决定。
- 维表没同步时不联网补全, 如实说"找不到"。
"""
from __future__ import annotations

import polars as pl
import pytest

from app.services import symbol_resolve as sr

ROWS = [
    ("000333.SZ", "美的集团", "stock"),
    ("000527.SZ", "美的电器", "stock"),
    ("605366.SH", "宏柏新材", "stock"),
    ("600941.SH", "中国移动", "stock"),
    ("510300.SH", "沪深300ETF华泰柏瑞", "etf"),
    ("159682.SZ", "中欧医疗健康混合A", "etf"),
]


class _FakeRepo:
    def __init__(self, rows=ROWS):
        self._frame = pl.DataFrame(
            {
                "symbol": [r[0] for r in rows],
                "name": [r[1] for r in rows],
                "type": [r[2] for r in rows],
            }
        )

    def get_instruments_asset(self, asset_type: str):
        """真实 repo 按资产类型返回各自的维表; 这里也必须过滤, 否则同一行会被两次命中。"""
        return self._frame.filter(pl.col("type") == asset_type)


@pytest.fixture()
def repo():
    return _FakeRepo()


# ── 代码形态 ────────────────────────────────────────────
def test_full_code_passes_through(repo):
    r = sr.resolve(repo, "000333.SZ")
    assert r.ok and r.symbol == "000333.SZ"
    assert r.name == "美的集团" and r.asset_type == "stock"


def test_bare_code_is_completed_with_exchange(repo):
    r = sr.resolve(repo, "000333")
    assert r.ok and r.symbol == "000333.SZ"


def test_prefixed_code_is_accepted(repo):
    """SZ000333 / SH600941 这种券商导出格式也要认。"""
    assert sr.resolve(repo, "SZ000333").symbol == "000333.SZ"
    assert sr.resolve(repo, "SH600941").symbol == "600941.SH"


def test_lowercase_and_padding_are_normalized(repo):
    assert sr.resolve(repo, "  000333.sz  ").symbol == "000333.SZ"


def test_etf_code_resolves_with_etf_asset_type(repo):
    r = sr.resolve(repo, "510300")
    assert r.ok and r.symbol == "510300.SH" and r.asset_type == "etf"


# ── 名称形态 ────────────────────────────────────────────
def test_exact_name_resolves(repo):
    r = sr.resolve(repo, "美的集团")
    assert r.ok and r.symbol == "000333.SZ" and r.reason == "name_exact"


def test_unique_prefix_matches_name(repo):
    r = sr.resolve(repo, "宏柏")
    assert r.ok and r.symbol == "605366.SH"


def test_ambiguous_name_is_rejected_not_guessed(repo):
    """「美的」命中两家 —— 必须报歧义, 不能默认取第一个。"""
    r = sr.resolve(repo, "美的")
    assert not r.ok and r.reason == "ambiguous"
    assert {c["symbol"] for c in r.candidates} == {"000333.SZ", "000527.SZ"}
    msg = r.error_message()
    assert "多只标的" in msg and "000333.SZ" in msg and "000527.SZ" in msg


def test_name_with_bracketed_code_is_cleaned(repo):
    """东财/同花顺常见「美的集团(000333)」写法。"""
    r = sr.resolve(repo, "美的集团(000333)")
    assert r.ok and r.symbol == "000333.SZ"


def test_full_width_characters_are_normalized(repo):
    r = sr.resolve(repo, "０００３３３")
    assert r.ok and r.symbol == "000333.SZ"


# ── 失败路径 ────────────────────────────────────────────
def test_empty_input_says_what_to_type(repo):
    r = sr.resolve(repo, "   ")
    assert not r.ok and r.reason == "empty"
    assert "请输入标的代码或名称" in r.error_message()


def test_unknown_name_is_not_found(repo):
    r = sr.resolve(repo, "不存在的名字")
    assert not r.ok and r.reason == "not_found"
    msg = r.error_message()
    assert "未找到" in msg
    # 不能假装是"数据缺失": 要给出三种可用输入形态
    assert "6 位代码" in msg and "股票名称" in msg


def test_resolve_or_404_raises_http_404_with_reason(repo):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        sr.resolve_or_404(repo, "美的")
    assert ei.value.status_code == 404
    assert "多只标的" in str(ei.value.detail)


def test_resolve_many_splits_ok_and_errors(repo):
    ok, errors = sr.resolve_many(repo, ["000333.SZ", "美的", "不存在的名字", "510300"])
    assert [r.symbol for r in ok] == ["000333.SZ", "510300.SH"]
    assert len(errors) == 2
    assert any("多只标的" in e for e in errors)
