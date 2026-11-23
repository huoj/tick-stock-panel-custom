"""``alert_seen`` —— 「已看过」状态的持久化。

这里锁的是四条设计约束，不是实现细节：

1. **键是事件组而不是标的**（按标的记会把明天的新公告一起吞掉）；
2. **只记状态不记内容**（``alerts.jsonl`` 已是正文唯一真相源）；
3. **坏文件不让摘要接口挂掉**（退化成"全部当新事实"）；
4. **有上限**（``MAX_GROUPS`` / ``MAX_AGE_DAYS``，按 ``last_seen`` 淘汰）。

另外锁一条时间语义：``first_seen`` 永远保留最早的一次，``last_seen`` 取最新 ——
前者用来回答"这条跟了多久"，后者只用于淘汰。
"""
from __future__ import annotations

import json

from app.services import alert_seen as seen

DAY_MS = 86_400_000
T0 = 1_790_000_000_000


def test_load_missing_file_returns_empty(tmp_path):
    assert seen.load(tmp_path) == {}


def test_load_corrupt_file_degrades_to_empty(tmp_path):
    p = tmp_path / "user_data" / "alert_seen.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    # 坏文件只该导致"多报几条"，不该抛
    assert seen.load(tmp_path) == {}


def test_load_unknown_version_returns_empty(tmp_path):
    p = tmp_path / "user_data" / "alert_seen.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"version": 999, "groups": {"g1": {"last_seen": T0}}}), encoding="utf-8")
    assert seen.load(tmp_path) == {}


def test_merge_keeps_earliest_first_seen_and_latest_last_seen():
    existing = {"g1": {"verification": "inferred", "mapping_level": "C",
                       "first_seen": T0, "last_seen": T0}}
    fresh = {"g1": {"verification": "reported", "mapping_level": "C"}}
    out = seen.merge(existing, fresh, now_ms=T0 + 5 * DAY_MS)
    assert out["g1"]["first_seen"] == T0           # 最早的那次不能丢
    assert out["g1"]["last_seen"] == T0 + 5 * DAY_MS


def test_merge_updates_enum_values_to_latest():
    """留旧枚举值会让「风险变化」永远检测不到 —— 必须用本次的值。"""
    existing = {"g1": {"verification": "inferred", "mapping_level": "D",
                       "first_seen": T0, "last_seen": T0}}
    fresh = {"g1": {"verification": "fact", "mapping_level": "A"}}
    out = seen.merge(existing, fresh, now_ms=T0 + 1)
    assert out["g1"]["verification"] == "fact"
    assert out["g1"]["mapping_level"] == "A"


def test_merge_ignores_empty_group_id():
    out = seen.merge({}, {"": {"verification": "fact"}}, now_ms=T0)
    assert out == {}


def test_merge_does_not_mutate_inputs():
    existing = {"g1": {"first_seen": T0, "last_seen": T0}}
    fresh = {"g2": {"verification": "fact"}}
    seen.merge(existing, fresh, now_ms=T0 + 1)
    assert set(existing) == {"g1"}
    assert set(fresh) == {"g2"}


def test_merge_new_group_gets_first_seen_now():
    out = seen.merge({}, {"g9": {"verification": "fact"}}, now_ms=T0)
    assert out["g9"]["first_seen"] == T0
    assert out["g9"]["last_seen"] == T0


def test_prune_drops_records_older_than_max_age():
    state = {
        "old": {"first_seen": T0, "last_seen": T0},
        "new": {"first_seen": T0, "last_seen": T0 + 40 * DAY_MS},
    }
    kept = seen.prune(state, now_ms=T0 + 40 * DAY_MS, max_age_days=30)
    assert set(kept) == {"new"}


def test_prune_caps_group_count_keeping_newest():
    state = {f"g{i}": {"first_seen": T0, "last_seen": T0 + i} for i in range(10)}
    kept = seen.prune(state, now_ms=T0 + 10, max_groups=3)
    assert set(kept) == {"g7", "g8", "g9"}


def test_prune_treats_missing_last_seen_as_oldest():
    state = {"broken": {"first_seen": T0}, "ok": {"first_seen": T0, "last_seen": T0}}
    kept = seen.prune(state, now_ms=T0, max_groups=1)
    assert set(kept) == {"ok"}


def test_record_roundtrip_writes_and_reads(tmp_path):
    out = seen.record(tmp_path, {"g1": {"verification": "fact", "mapping_level": "A"}}, now_ms=T0)
    assert out["g1"]["verification"] == "fact"
    assert seen.load(tmp_path)["g1"]["verification"] == "fact"


def test_record_is_incremental(tmp_path):
    seen.record(tmp_path, {"g1": {"verification": "fact"}}, now_ms=T0)
    seen.record(tmp_path, {"g2": {"verification": "inferred"}}, now_ms=T0 + 1)
    state = seen.load(tmp_path)
    # g1 不能被第二轮覆盖掉 —— 否则每天都会把昨天看过的重报一遍
    assert set(state) == {"g1", "g2"}


def test_record_survives_unwritable_directory(tmp_path):
    """写失败只该退化成功率下降，不该让调用方崩。"""
    (tmp_path / "user_data").write_text("not a dir", encoding="utf-8")
    out = seen.record(tmp_path, {"g1": {"verification": "fact"}}, now_ms=T0)
    assert out["g1"]["verification"] == "fact"  # 返回值仍然可用（内存里是对的）
