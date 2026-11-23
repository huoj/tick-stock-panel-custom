"""告警「已看过」状态的持久化。

为什么需要这个文件
------------------
``alert_digest.build_digest`` 靠 ``prior_state`` 判断「这条是不是新事实」。
但状态如果只放在一次请求里（曾经的 ``since_ts`` 做法），刷新页面、换设备、
第二天早上再打开，就等于从没看过 —— 所有旧事实又会重新报一遍，用户马上
回到"每天都一样、于是都不看"的状态。

所以把状态落到 ``data/user_data/alert_seen.json``：

.. code-block:: json

    {"version": 1, "updated_at": 1794479704775,
     "groups": {"evtgrp_xxx": {"verification": "reported", "mapping_level": "C",
                               "first_seen": 1794…, "last_seen": 1794…}}}

设计约束（都不是随手选的）
--------------------------
1. **只记状态，不记内容**。存的是 ``event_group_id`` 与两个会变化的枚举值，
   不存标题、不存正文 —— 告警正文已经被 ``alerts.jsonl`` 存着，重复存会让
   这个文件变成第二份真相源，两边不一致时无从判断谁对。
2. **键是事件组，不是标的**。同一只票一天可能挂 20 条不同事件；按标的记
   "看过"会把明天的新公告一起吞掉。
3. **写入失败不影响读**。状态文件的唯一作用是"少报一点"，读写异常时退化为
   "全部当新事实"（也就是修复前的行为），而不是让摘要接口 500。
4. **有上限**。事件组会无限增长，超过 ``MAX_GROUPS`` 时按 ``last_seen`` 淘汰
   最旧的，避免文件越写越大。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

#: 状态文件版本。字段含义变化时递增，读到不认识的版本直接丢弃重建。
VERSION = 1

#: 最多保留多少个事件组的"已看过"记录。按 ``last_seen`` 淘汰最旧的。
MAX_GROUPS = 5000

#: 超过这个天数的记录直接清掉。告警本身只留 7 天，状态留 30 天已经足够宽。
MAX_AGE_DAYS = 30

_lock = threading.RLock()


def _path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "alert_seen.json"


def load(data_dir: Path) -> dict[str, dict]:
    """读已看过状态。文件缺失/损坏/版本不符时返回空字典（不抛异常）。"""
    p = _path(data_dir)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 损坏的状态文件只该导致"多报几条"，不该让摘要接口挂掉。
        logger.warning("读取告警已看过状态失败，按空状态处理: %s", exc)
        return {}
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return {}
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        return {}
    return {
        str(k): v for k, v in groups.items()
        if isinstance(v, dict)
    }


def merge(
    existing: dict[str, dict],
    fresh: dict[str, dict],
    *,
    now_ms: int | None = None,
) -> dict[str, dict]:
    """把本次摘要的状态并进旧状态，返回新字典（不改动入参）。

    ``first_seen`` 保留最早的一次，``last_seen`` 取最新 —— 前者用来算"这条
    事件跟了多久"，后者用来淘汰。``verification`` / ``mapping_level`` 一律用
    本次的值：它们就是判定"风险变化"的依据，留旧的会让变化永远检测不到。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    out: dict[str, dict] = {k: dict(v) for k, v in existing.items()}
    for gid, raw in fresh.items():
        if not gid:
            continue
        prev = out.get(gid) or {}
        out[gid] = {
            "verification": raw.get("verification"),
            "mapping_level": raw.get("mapping_level"),
            "first_seen": int(prev.get("first_seen") or now),
            "last_seen": now,
        }
    return out


def prune(
    state: dict[str, dict],
    *,
    now_ms: int | None = None,
    max_groups: int = MAX_GROUPS,
    max_age_days: int = MAX_AGE_DAYS,
) -> dict[str, dict]:
    """按年龄与总量淘汰。``last_seen`` 缺失的记录视为最旧。"""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    cutoff = now - int(max_age_days * 86_400_000)
    kept = {
        gid: v for gid, v in state.items()
        if int(v.get("last_seen") or 0) >= cutoff
    }
    if len(kept) > max_groups:
        ordered = sorted(
            kept.items(),
            key=lambda kv: int(kv[1].get("last_seen") or 0),
            reverse=True,
        )
        kept = dict(ordered[:max_groups])
    return kept


def save(data_dir: Path, state: dict[str, dict], *, now_ms: int | None = None) -> bool:
    """原子写回状态。返回是否写成功（失败只记日志，不抛）。"""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    payload = {
        "version": VERSION,
        "updated_at": now,
        "groups": state,
    }
    try:
        with _lock:
            p = _path(data_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
        return True
    except OSError as exc:
        logger.warning("写入告警已看过状态失败，本次结果不受影响: %s", exc)
        return False


def record(
    data_dir: Path,
    fresh: dict[str, dict],
    *,
    now_ms: int | None = None,
) -> dict[str, dict]:
    """读旧状态 → 合并本次 → 淘汰 → 写回，返回写回后的状态。"""
    with _lock:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        state = prune(merge(load(data_dir), fresh, now_ms=now), now_ms=now)
        save(data_dir, state, now_ms=now)
    return state


__all__ = ["MAX_GROUPS", "VERSION", "load", "merge", "prune", "record", "save"]
