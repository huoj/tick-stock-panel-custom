"""`app/services/forecast_calibration.py` 的定向测试。

覆盖：幂等冻结（同参数同 id 且只追加一次）、评估不改原结论、准确率与校准曲线
可算、空 data_dir 不抛异常、按 tier / regime 分组。全部用 tmp_path，不碰真实 data。
"""

from __future__ import annotations

import json

import pytest

from app.services import forecast_calibration as fc
from app.services import forecast_track

_LEDGER_RELATIVE = "forecast_ledger/conclusions.jsonl"


def _rows(tmp_path):
    """直接读回 JSONL 原始历史行，用于验证「只追加、不改历史行」。"""
    path = tmp_path / _LEDGER_RELATIVE
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _base_kwargs(tmp_path, **overrides):
    kwargs = {
        "data_dir": tmp_path,
        "kind": "action_card_inference",
        "symbol": "SH600000",
        "as_of": "2026-11-10",
        "data_cutoff": "2026-11-10",
        "payload": {"stated_probability": 0.6, "regime": "bull"},
        "inputs_fingerprint": "fp-001",
        "tier": "speculative",
    }
    kwargs.update(overrides)
    return kwargs


def test_record_conclusion_idempotent_and_stable(tmp_path):
    """同参数两次 → 同 id 且账本只有一行（幂等语义写在 record_conclusion docstring：
    同 id 已存在只返回首次结果、不再追加）；任一冻结字段变化 → 不同 id、追加新行。
    """
    first = fc.record_conclusion(**_base_kwargs(tmp_path))
    second = fc.record_conclusion(**_base_kwargs(tmp_path))

    assert first == {
        "id": first["id"],
        "kind": "action_card_inference",
        "symbol": "SH600000",
        "as_of": "2026-11-10",
        "data_cutoff": "2026-11-10",
        "tier": "speculative",
        "recorded": True,
    }
    assert len(first["id"]) == 16
    assert second == first
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["type"] == "conclusion"
    assert rows[0]["id"] == first["id"]
    assert rows[0]["immutable"] is True
    assert rows[0]["inputs_fingerprint"] == "fp-001"

    different_payload = fc.record_conclusion(
        **_base_kwargs(tmp_path, payload={"stated_probability": 0.7, "regime": "bull"})
    )
    different_symbol = fc.record_conclusion(**_base_kwargs(tmp_path, symbol="SZ000001"))
    different_cutoff = fc.record_conclusion(
        **_base_kwargs(tmp_path, data_cutoff="2026-11-09")
    )
    ids = {
        first["id"],
        different_payload["id"],
        different_symbol["id"],
        different_cutoff["id"],
    }
    assert len(ids) == 4
    assert len(_rows(tmp_path)) == 4


def test_evaluate_then_accuracy_summary_and_calibration_curve(tmp_path):
    """记录 → 评估 → accuracy_summary 的 total_evaluated / hit_rate 正确；
    calibration_curve 的 buckets.stated_avg / realized_rate 与 overall.brier 可算。
    """
    kinds = [
        "action_card_inference",
        "risk_budget",
        "position_plan",
        "action_card_inference",
    ]
    specs = [(0.6, 1), (0.6, 0), (0.6, 1), (0.2, 0)]
    for index, (probability, realized) in enumerate(specs):
        rec = fc.record_conclusion(
            **_base_kwargs(
                tmp_path,
                kind=kinds[index],
                symbol=f"SH60000{index}",
                payload={"stated_probability": probability, "regime": "bull"},
            )
        )
        evaluation = fc.evaluate_conclusion(
            data_dir=tmp_path,
            conclusion_id=rec["id"],
            realized=realized,
            realized_note=f"样本 {index}",
        )
        assert evaluation["conclusion_found"] is True
        assert evaluation["conclusion_id"] == rec["id"]
        assert evaluation["realized"] == realized
        assert evaluation["recorded"] is True

    summary = fc.accuracy_summary(data_dir=tmp_path)
    assert summary["total_recorded"] == 4
    assert summary["total_evaluated"] == 4
    assert summary["hit_rate"] == pytest.approx(0.5)
    assert set(summary["by_kind"]) == {
        "action_card_inference",
        "risk_budget",
        "position_plan",
    }
    assert summary["by_kind"]["action_card_inference"]["evaluated"] == 2
    assert summary["by_kind"]["risk_budget"]["hit_rate"] == pytest.approx(0.0)

    curve = fc.calibration_curve(data_dir=tmp_path)
    buckets = {round(bucket["lo"], 10): bucket for bucket in curve["buckets"]}
    assert set(buckets) == {0.2, 0.6}
    assert buckets[0.6]["count"] == 3
    assert buckets[0.6]["hi"] == pytest.approx(0.7)
    assert buckets[0.6]["stated_avg"] == pytest.approx(0.6)
    assert buckets[0.6]["realized_rate"] == pytest.approx(2 / 3)
    assert buckets[0.2]["count"] == 1
    assert buckets[0.2]["stated_avg"] == pytest.approx(0.2)
    assert buckets[0.2]["realized_rate"] == pytest.approx(0.0)

    assert curve["overall"]["count"] == 4
    assert curve["overall"]["stated_avg"] == pytest.approx(0.5)
    assert curve["overall"]["realized_rate"] == pytest.approx(0.5)
    # mean((p-r)^2) = (0.16 + 0.36 + 0.16 + 0.04) / 4
    assert curve["overall"]["brier"] == pytest.approx(0.18)
    assert curve["by_tier"]["speculative"]["count"] == 4
    assert curve["limitations"]


def test_evaluate_does_not_rewrite_frozen_conclusion(tmp_path):
    """评估只追加新行；原结论的 payload / as_of / data_cutoff /
    inputs_fingerprint 读回后逐字不变（含调用方事后篡改原 dict 也不影响已冻结行）。
    """
    payload = {
        "stated_probability": 0.6,
        "regime": "bull",
        "note": "原始结论",
    }
    rec = fc.record_conclusion(
        **_base_kwargs(
            tmp_path,
            payload=payload,
            inputs_fingerprint="fp-frozen",
        )
    )
    payload["note"] = "调用方事后篡改"

    before = [row for row in _rows(tmp_path) if row["type"] == "conclusion"]
    assert len(before) == 1
    assert before[0]["payload"]["note"] == "原始结论"

    evaluation = fc.evaluate_conclusion(
        data_dir=tmp_path,
        conclusion_id=rec["id"],
        realized=1,
        realized_note="说涨真涨了",
    )

    rows = _rows(tmp_path)
    assert len(rows) == 2
    after = [row for row in rows if row["type"] == "conclusion"]
    evaluations = [row for row in rows if row["type"] == "evaluation"]
    assert len(after) == 1
    assert len(evaluations) == 1
    assert after[0] == before[0]
    assert after[0]["payload"]["note"] == "原始结论"
    assert after[0]["as_of"] == "2026-11-10"
    assert after[0]["data_cutoff"] == "2026-11-10"
    assert after[0]["inputs_fingerprint"] == "fp-frozen"
    assert evaluations[0]["id"] == evaluation["id"]
    assert evaluations[0]["realized_note"] == "说涨真涨了"
    assert evaluations[0]["immutable"] is True


def test_empty_data_dir_never_raises(tmp_path):
    """空 data_dir：三个查询入口（calibration_curve / accuracy_summary /
    evaluate_conclusion 缺结论）都不抛异常，返回带 limitations 的空结论；纯查询只读。
    """
    ledger = tmp_path / _LEDGER_RELATIVE
    assert not ledger.exists()

    curve = fc.calibration_curve(data_dir=tmp_path)
    summary = fc.accuracy_summary(data_dir=tmp_path)
    assert not ledger.exists()  # 查询函数不写文件

    assert curve["overall"]["count"] == 0
    assert curve["overall"]["stated_avg"] is None
    assert curve["overall"]["realized_rate"] is None
    assert curve["overall"]["brier"] is None
    assert curve["buckets"] == []
    assert curve["by_tier"] == {}
    assert curve["by_regime"] == {}
    assert curve["limitations"]

    assert summary["total_recorded"] == 0
    assert summary["total_evaluated"] == 0
    assert summary["hit_rate"] is None
    assert summary["by_kind"] == {}
    assert summary["limitations"]

    evaluation = fc.evaluate_conclusion(
        data_dir=tmp_path, conclusion_id="不存在的结论", realized=1
    )
    assert evaluation["conclusion_found"] is False
    assert evaluation["limitations"]
    # 孤立评估也只追加，不抛异常、不回头造一条结论。
    assert len(_rows(tmp_path)) == 1
    assert fc.accuracy_summary(data_dir=tmp_path)["total_evaluated"] == 0
    assert fc.calibration_curve(data_dir=tmp_path)["overall"]["count"] == 0


def test_calibration_curve_groups_by_tier_and_regime(tmp_path):
    """两条不同 tier + 不同 regime 的评估 → by_tier / by_regime 各自有键。"""
    cases = [
        (forecast_track.TIER_SOLID, "bull", 0.6, 1),
        (forecast_track.TIER_SPECULATIVE, "bear", 0.4, 0),
    ]
    for kind, tier, regime, probability, realized in [
        ("risk_budget", *cases[0]),
        ("position_plan", *cases[1]),
    ]:
        rec = fc.record_conclusion(
            **_base_kwargs(
                tmp_path,
                kind=kind,
                symbol=f"SH{600000 + realized}",
                tier=tier,
                payload={"stated_probability": probability, "regime": regime},
            )
        )
        fc.evaluate_conclusion(
            data_dir=tmp_path, conclusion_id=rec["id"], realized=realized
        )

    curve = fc.calibration_curve(data_dir=tmp_path)
    assert set(curve["by_tier"]) == {"solid", "speculative"}
    assert set(curve["by_regime"]) == {"bull", "bear"}
    assert curve["by_tier"]["solid"]["count"] == 1
    assert curve["by_tier"]["solid"]["realized_rate"] == pytest.approx(1.0)
    assert curve["by_tier"]["speculative"]["count"] == 1
    assert curve["by_tier"]["speculative"]["realized_rate"] == pytest.approx(0.0)
    assert curve["by_regime"]["bull"]["count"] == 1
    assert curve["by_regime"]["bull"]["realized_rate"] == pytest.approx(1.0)
    assert curve["by_regime"]["bear"]["count"] == 1
    assert curve["by_regime"]["bear"]["realized_rate"] == pytest.approx(0.0)
    assert curve["overall"]["count"] == 2
    assert curve["overall"]["stated_avg"] == pytest.approx(0.5)
