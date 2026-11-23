"""前瞻跟踪的测试 —— 冻结、打分、分组统计。

守的四件事（都对应"模型到底有没有用"这个问题）：
1. 结论冻结后**不可改**：重新生成只增加版本号，旧版本永远可读；
2. `data_cutoff` 不得晚于 `as_of`（否则等于用未来数据做判断）；
3. 评测只用 cutoff **之后**的行情，且样本不足时**不打分**而不是拿不足的天数凑；
4. 方向准确率与映射胜率分开算，官方证据/推测性关联分开统计。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import polars as pl
import pytest

from app.services import forecast_track as ft

D0 = dt.date(2026, 1, 5)


def _write_prices(data_dir: Path, symbol: str, closes: list[float],
                  start: dt.date = D0, directory: str = "kline_daily_enriched") -> None:
    for i, close in enumerate(closes):
        day = start + dt.timedelta(days=i)
        part = data_dir / directory / f"date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame([{"symbol": symbol, "raw_close": close}]).write_parquet(part / "part.parquet")


def _payload(**over) -> dict:
    base = {"symbol": "600941.SH", "direction": "利好", "verification": "fact",
            "mapping_level": "B", "event_title": "某政策"}
    base.update(over)
    return base


# ── 冻结与版本 ─────────────────────────────────────────

def test_record_freezes_identity_and_metadata(tmp_path):
    body = ft.record(tmp_path, kind="event", key="evtgrp_1",
                     payload=_payload(), as_of=dt.date(2026, 1, 1),
                     model_version="v1")
    assert body["version"] == 1
    assert body["as_of"] == "2026-01-01"
    assert body["data_cutoff"] == "2026-01-01"
    assert body["immutable"] is True
    assert body["model_version"] == "v1"
    assert len(body["inputs_fingerprint"]) == 16
    assert Path(body["_path"]).exists()


def test_regenerating_creates_new_version_and_keeps_old(tmp_path):
    """策略变更后重新生成 → 新版本，旧预测**一字不改**。"""
    first = ft.record(tmp_path, kind="event", key="evtgrp_1",
                      payload=_payload(direction="利好"), as_of=dt.date(2026, 1, 1),
                      model_version="v1")
    second = ft.record(tmp_path, kind="event", key="evtgrp_1",
                       payload=_payload(direction="利空"), as_of=dt.date(2026, 1, 2),
                       model_version="v2")

    assert (first["version"], second["version"]) == (1, 2)
    versions = ft.list_versions(tmp_path, "event", "evtgrp_1")
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["payload"]["direction"] == "利好", "旧版本不能被覆盖"
    assert versions[0]["model_version"] == "v1"
    assert ft.latest(tmp_path, "event", "evtgrp_1")["model_version"] == "v2"


def test_fingerprint_changes_with_inputs(tmp_path):
    a = ft.record(tmp_path, kind="event", key="k1", payload=_payload(), as_of=dt.date(2026, 1, 1),
                  inputs={"n": 1})
    b = ft.record(tmp_path, kind="event", key="k2", payload=_payload(), as_of=dt.date(2026, 1, 1),
                  inputs={"n": 2})
    c = ft.record(tmp_path, kind="event", key="k3", payload=_payload(), as_of=dt.date(2026, 1, 1),
                  inputs={"n": 1})
    assert a["inputs_fingerprint"] != b["inputs_fingerprint"]
    assert a["inputs_fingerprint"] == c["inputs_fingerprint"]


def test_data_cutoff_cannot_exceed_as_of(tmp_path):
    with pytest.raises(ValueError, match="晚于 as_of"):
        ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                  as_of=dt.date(2026, 1, 1), data_cutoff=dt.date(2026, 1, 5))


def test_older_cutoff_is_allowed_and_recorded(tmp_path):
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 10), data_cutoff=dt.date(2026, 1, 3))
    assert body["data_cutoff"] == "2026-01-03"
    assert body["as_of"] == "2026-01-10"


# ── 评测：只用 cutoff 之后的数据 ────────────────────────

def test_evaluation_uses_only_prices_after_cutoff(tmp_path):
    """cutoff 之前的涨跌不许计入 —— 那就是未来信息泄漏。"""
    _write_prices(tmp_path, "600941.SH", [10.0, 20.0, 30.0, 40.0, 50.0, 60.0])  # D0..D0+5
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 7), data_cutoff=dt.date(2026, 1, 7))
    result = ft.evaluate(tmp_path, body, horizon_days=3)

    # cutoff=01-07 → 只用 01-08/09/10（价格 40/50/60）
    assert result["entry_date"] == (D0 + dt.timedelta(days=3)).isoformat()
    assert result["exit_date"] == (D0 + dt.timedelta(days=5)).isoformat()
    assert result["gross_return"] == pytest.approx(60.0 / 40.0 - 1.0, abs=1e-6)
    assert result["available_days"] == 3


def test_insufficient_history_is_not_scored(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0, 11.0])
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 10))
    result = ft.evaluate(tmp_path, body, horizon_days=5)
    assert result["net_return"] is None
    assert result["direction_hit"] is None
    assert "不足 5 天" in " ".join(result["limitations"])


def test_net_return_deducts_both_legs(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0, 11.0, 12.0], start=D0)
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 10), data_cutoff=dt.date(2026, 1, 4))
    result = ft.evaluate(tmp_path, body, horizon_days=2,
                         commission_bps=5.0, stamp_tax_bps=5.0)
    gross = 11.0 / 10.0 - 1.0
    assert result["gross_return"] == pytest.approx(gross, abs=1e-6)
    # 买卖各 5bp + 卖出印花税 5bp = 15bp
    assert result["net_return"] == pytest.approx(gross - 0.0015, abs=1e-6)
    assert result["net_return"] < result["gross_return"]


def test_direction_hit_follows_direction(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0])
    up = ft.record(tmp_path, kind="event", key="up", payload=_payload(direction="利好"),
                   as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    assert ft.evaluate(tmp_path, up, horizon_days=2)["direction_hit"] is True

    down = ft.record(tmp_path, kind="event", key="down", payload=_payload(direction="利空"),
                     as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    assert ft.evaluate(tmp_path, down, horizon_days=2)["direction_hit"] is False


def test_neutral_direction_is_excluded_from_accuracy(tmp_path):
    """『不确定』不计入方向准确率的分母 —— 它不是'猜错'。"""
    _write_prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0])
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(direction="不确定"),
                     as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    result = ft.evaluate(tmp_path, body, horizon_days=2)
    assert result["direction_hit"] is None
    assert result["mapping_profitable"] is True   # 映射赚钱与方向正确是两件事


def test_max_drawdown_survives_round_trip(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0, 8.0, 12.0])
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 1))
    result = ft.evaluate(tmp_path, body, horizon_days=3)
    assert result["max_drawdown"] == pytest.approx(8.0 / 10.0 - 1.0, abs=1e-6)


def test_append_evaluation_does_not_touch_frozen_fields(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0])
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    result = ft.evaluate(tmp_path, body, horizon_days=2)
    saved = ft.append_evaluation(tmp_path, body, result)

    assert saved["payload"] == body["payload"]
    assert saved["data_cutoff"] == body["data_cutoff"]
    assert saved["as_of"] == body["as_of"]
    assert len(saved["evaluations"]) == 1

    # 再评一次：追加而不是覆盖
    again = ft.append_evaluation(tmp_path, body, ft.evaluate(tmp_path, body, horizon_days=2))
    assert len(again["evaluations"]) == 2


# ── 分组统计 ───────────────────────────────────────────

@pytest.mark.parametrize("verification,level,expect", [
    ("fact", "A", ft.TIER_SOLID),
    ("fact", "B", ft.TIER_SOLID),
    ("reported", "B", ft.TIER_SOLID),
    ("inferred", "B", ft.TIER_SPECULATIVE),   # 映射有据但事件只是推断
    ("fact", "C", ft.TIER_SPECULATIVE),       # 事件硬但映射弱
    ("pending", "A", ft.TIER_SPECULATIVE),
    (None, None, ft.TIER_SPECULATIVE),
])
def test_evidence_tier_classification(verification, level, expect):
    assert ft.evidence_tier({"verification": verification, "mapping_level": level}) == expect


def test_market_state_is_read_from_regime_history(tmp_path):
    """市场状态取 regime 已算好的口径，不另算一套；取不到就 None 而不是猜。

    目录名是 `regime_history`（不是 `regime`），列名是 `state` —— 用真实布局，
    否则测试会"通过"却与生产读写不一致。
    """
    assert ft.market_state_for(tmp_path, dt.date(2026, 1, 10)) is None, "没有 regime 数据就该 None"

    regime_dir = tmp_path / "regime_history"
    regime_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([
        {"date": dt.date(2026, 1, 5), "state": "weak"},
        {"date": dt.date(2026, 1, 8), "state": "strong"},
    ]).write_parquet(regime_dir / "part.parquet")

    assert ft.market_state_for(tmp_path, dt.date(2026, 1, 9)) == "strong", "取该日或之前最近一天"
    assert ft.market_state_for(tmp_path, dt.date(2026, 1, 8)) == "strong"
    assert ft.market_state_for(tmp_path, dt.date(2026, 1, 5)) == "weak"
    assert ft.market_state_for(tmp_path, dt.date(2026, 1, 1)) is None, "早于所有记录就是未知"


def test_price_dir_can_point_at_real_data_root(tmp_path):
    """跟踪目录与行情目录分离时，显式 price_dir 才能评出结果。

    离线核查/探针常把跟踪记录写到临时目录，行情仍在真实数据根；不给 price_dir
    就会读到空目录，得出"cutoff 之后只有 0 个交易日"——看着像没数据，
    其实是读错目录。这个测试把两种情况都钉住。
    """
    track_dir = tmp_path / "track"
    price_dir = tmp_path / "prices"
    _write_prices(price_dir, "600941.SH", [10.0, 11.0, 12.0, 13.0])
    body = ft.record(track_dir, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 4), data_cutoff=dt.date(2026, 1, 2))

    without = ft.evaluate(track_dir, body, horizon_days=2)
    assert without["net_return"] is None
    assert "不足 2 天" in " ".join(without["limitations"])

    with_prices = ft.evaluate(track_dir, body, horizon_days=2, price_dir=price_dir)
    assert with_prices["net_return"] is not None
    assert with_prices["direction_hit"] is True


def test_report_separates_solid_from_speculative(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0, 12.0, 13.0, 14.0])
    solid = ft.record(tmp_path, kind="event", key="solid",
                      payload=_payload(direction="利好", verification="fact", mapping_level="B"),
                      as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    spec = ft.record(tmp_path, kind="event", key="spec",
                     payload=_payload(direction="利空", verification="inferred", mapping_level="C"),
                     as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    ft.append_evaluation(tmp_path, solid, ft.evaluate(tmp_path, solid, horizon_days=2, market_state="risk_on"))
    ft.append_evaluation(tmp_path, spec, ft.evaluate(tmp_path, spec, horizon_days=2, market_state="risk_off"))

    rep = ft.report(tmp_path, kind="event")
    solid_stats = rep["tiers"][ft.TIER_LABELS[ft.TIER_SOLID]]
    spec_stats = rep["tiers"][ft.TIER_LABELS[ft.TIER_SPECULATIVE]]

    assert solid_stats["scored"] == 1 and spec_stats["scored"] == 1
    # 利好 + 上涨 → 命中；利空 + 上涨 → 不命中。两组分开算，绝不合并
    assert solid_stats["direction_accuracy"] == 1.0
    assert spec_stats["direction_accuracy"] == 0.0
    assert rep["overall"]["direction_accuracy"] == 0.5
    assert set(rep["by_market_state"]) == {"risk_on", "risk_off"}
    assert rep["overall"]["avg_turnover"] == 2.0


def test_report_counts_unscored_separately(tmp_path):
    _write_prices(tmp_path, "600941.SH", [10.0])
    body = ft.record(tmp_path, kind="event", key="k", payload=_payload(),
                     as_of=dt.date(2026, 1, 20), data_cutoff=dt.date(2026, 1, 2))
    ft.append_evaluation(tmp_path, body, ft.evaluate(tmp_path, body, horizon_days=5))
    rep = ft.report(tmp_path, kind="event")
    overall = rep["overall"]
    assert overall["samples"] == 1
    assert overall["scored"] == 0
    assert overall["unscored"] == 1, "没打分必须与打分为 0 分开计数"
    assert overall["direction_accuracy"] is None


def test_report_on_empty_store(tmp_path):
    rep = ft.report(tmp_path, kind="event")
    assert rep["overall"]["samples"] == 0
    assert rep["overall"]["direction_accuracy"] is None
    assert ft.list_keys(tmp_path, "event") == []
