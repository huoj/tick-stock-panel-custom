"""行动卡 inference / 组合风控减仓建议 / 三态计划的**结论闭环与校准曲线**。

背景：`app/services/forecast_track.py` 已经实现了「冻结 as_of / generated_at /
data_cutoff / inputs_fingerprint、只增不改版本目录、事后用晚于 cutoff 的行情打分、
按 TIER_SOLID / TIER_SPECULATIVE 分组」，但只面向 event/factor 两类行情结论。
行动卡 inference、组合风控减仓建议、三态计划这三种结论既没有进这套闭环，也没有
「说 60% 上涨的样本实际有多少涨了」的校准曲线。本模块补上这一段。

────────────────────────────────────────────────────────────
为什么「只追加、不改历史行」
────────────────────────────────────────────────────────────
一条结论的可信度全靠「当时到底说了什么」：`as_of`（哪天做的判断）、
`data_cutoff`（当时数据只到哪天）、`inputs_fingerprint`（当时用了哪组输入/参数）
和 `payload`（结论正文）必须在写下的那一刻冻结。如果允许事后改写，就能把当初
说错的结论悄悄修成事后正确的版本，历史准确率和校准曲线会一起失真，也会把未来
信息泄漏进评测（cutoff 之后的数据反过来影响 cutoff 之前的结论）。所以账本用
JSONL：每行一个不可变事实，评估结果作为**新行**追加，绝不回头改结论行、不删除、
不覆盖；审计时按行重放即可还原任何时点的状态。

────────────────────────────────────────────────────────────
为什么校准曲线要给普通投资者看
────────────────────────────────────────────────────────────
页面上一个「系统历史准确率 62%」太笼统：它把「随口一说」和「高置信度判断」
混在一起。普通投资者真正需要的是：历史上系统说「上涨概率 60%」的那一批样本，
到底有多少真的涨了？如果 0.6 这一桶的 realized_rate 只有 0.40，说明系统在这个
档位明显过度自信，看到 60% 就该自己打个折；如果接近 0.60，说明概率口径可信。
分桶（calibration curve）+ Brier 分数把「置信度」和「兑现率」对齐，比单一命中率
更能校准信任；再按 tier（官方证据足 / 推测性主题关联）和 regime（市场状态）拆开，
避免用 solid 样本的表现替 speculative 样本背书。

────────────────────────────────────────────────────────────
为什么另用同目录 JSONL，而不是薄封装 forecast_track.record/evaluate
────────────────────────────────────────────────────────────
不是另造第二套「跟踪逻辑」，而是三处签名/语义与本任务的公共 API 不兼容，硬套会
破坏约束（CONTRIBUTING §1.1 要求优先复用、禁止平行实现，所以能复用的都复用）：

1. `forecast_track.record()` 每次 `next_version()` 都生成 vN 新版本、不幂等，且要求
   `dt.date` 并强制 `data_cutoff <= as_of`；本任务要求「两次同参数 → 同 id 且不产生
   两条历史」、四个时间字段全是 `str`。
2. `forecast_track.evaluate()` 用 `load_close_after()` 读 polars parquet 行情算
   `net_return` / `direction_hit`；本任务的 `realized: int` 是「说涨是否真涨、建议
   是否兑现」的 0/1 事后标记，不依赖行情文件。
3. `forecast_track.append_evaluation()` 用 `_atomic_write_json()` **覆盖整个 vN.json**，
   直接违反「不得改写原结论行 / 不改历史行」。

复用的部分（口径单一来源，不重新实现）：
- `forecast_track.fingerprint()`：规范化 JSON → `sha256(...)[:16]`，用于结论 id、
  评估 id 和缺省 `inputs_fingerprint`；
- `forecast_track.TIER_SOLID` / `TIER_SPECULATIVE`：tier 分组的标准取值；
- `forecast_track` 的「无样本时准确率取 None，不计入分母」口径。

存储：`data_dir/forecast_ledger/conclusions.jsonl`（逐行追加，only-append）。

────────────────────────────────────────────────────────────
幂等与统计语义（避免歧义）
────────────────────────────────────────────────────────────
- `record_conclusion`：id 由**全部冻结字段**规范化后取指纹；同一 id 已存在则只返回
  首次结果、不再追加第二行（幂等）。任一冻结字段变化即得到新 id、追加新结论。
- `evaluate_conclusion`：每次调用追加一行评估，不改任何已有行；同一结论被重复评估
  时，查询侧按 `conclusion_id` **取最后一条评估**生效（事件溯源折叠），历史行全留。
- `stated_probability` 采用**小数制 0~1**（0.6 = 60%）；按 CONTRIBUTING §3.1 不做
  「数值小于 1 就乘 100」的启发式换算，口径不符或缺字段的样本跳过并写进 limitations。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import threading
from pathlib import Path
from typing import Any

from app.services import forecast_track

__all__ = [
    "accuracy_summary",
    "calibration_curve",
    "evaluate_conclusion",
    "record_conclusion",
]

_LEDGER_SUBDIR = "forecast_ledger"
_LEDGER_FILENAME = "conclusions.jsonl"
_TYPE_CONCLUSION = "conclusion"
_TYPE_EVALUATION = "evaluation"

#: 与 `app/services/alert_store.py` 同样的进程内写锁：追加写串行化，避免并发撕行。
_lock = threading.Lock()


# ── 账本读写：只追加 ──────────────────────────────────────

def _ledger_path(data_dir: Path) -> Path:
    """``data_dir/forecast_ledger/conclusions.jsonl``。"""
    return data_dir / _LEDGER_SUBDIR / _LEDGER_FILENAME


def _read_rows(data_dir: Path) -> list[dict]:
    """读回全部历史行。文件不存在/不可读/坏行都按「无该行」处理，绝不改写文件。"""
    path = _ledger_path(data_dir)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            # 坏行原样留在文件里（不改历史），只是不参与统计。
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_locked(data_dir: Path, row: dict) -> None:
    """调用方须持有 `_lock`；只以 "a" 模式追加一行，不 truncate、不 replace。"""
    path = _ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _snapshot(data_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """把账本折叠成 (结论表, 每结论最后一条评估表)。后写覆盖只发生在内存视图里。"""
    conclusions: dict[str, dict] = {}
    evaluations: dict[str, dict] = {}
    for row in _read_rows(data_dir):
        row_type = row.get("type")
        if row_type == _TYPE_CONCLUSION and isinstance(row.get("id"), str):
            conclusions[row["id"]] = row
        elif row_type == _TYPE_EVALUATION and isinstance(row.get("conclusion_id"), str):
            evaluations[row["conclusion_id"]] = row
    return conclusions, evaluations


def _payload_of(conclusion: dict) -> dict:
    payload = conclusion.get("payload")
    return payload if isinstance(payload, dict) else {}


def _samples(
    conclusions: dict[str, dict], evaluations: dict[str, dict],
) -> tuple[list[dict], int]:
    """(有效评估样本, 孤立评估数)。样本 = {conclusion, evaluation, realized}。"""
    out: list[dict] = []
    orphans = 0
    for conclusion_id, evaluation in evaluations.items():
        conclusion = conclusions.get(conclusion_id)
        if conclusion is None:
            orphans += 1
            continue
        try:
            realized = int(evaluation.get("realized"))
        except (TypeError, ValueError):
            continue
        if realized not in (0, 1):
            continue
        out.append(
            {"conclusion": conclusion, "evaluation": evaluation, "realized": realized}
        )
    return out, orphans


def _as_probability(value: Any) -> float | None:
    """显式小数制口径：只接受能解析成 [0, 1] 有限浮点的值，其余一律跳过。

    特别注意：60（百分数口径）**不**被换算成 0.6 —— CONTRIBUTING §3.1 禁止
    「数值小于 1 就乘 100」这类启发式；口径不明就跳过并提示，而不是猜。
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return None
    return probability


def _group_rate(samples: list[dict], key_of) -> dict[str, dict]:
    """按调用方给的取值函数分组，组内算 count + realized_rate。"""
    groups: dict[str, list[int]] = {}
    for sample in samples:
        key = key_of(sample)
        if key is None:
            continue
        groups.setdefault(str(key), []).append(sample["realized"])
    return {
        name: {"count": len(values), "realized_rate": sum(values) / len(values)}
        for name, values in sorted(groups.items())
    }


def _base_limitations() -> list[str]:
    return [
        "stated_probability 采用小数制 0~1（0.6 表示 60%）；沿用 CONTRIBUTING §3.1，不做"
        "「数值小于 1 就乘 100」的启发式换算，口径不符或缺失的样本会被跳过并在下方说明。",
        "校准曲线是历史描述，不是未来收益保证；样本少时每个桶的 realized_rate 波动很大，"
        "brier 也会不稳定。",
        "分桶 / overall / brier 只统计带有效 stated_probability 的已评估结论；"
        "accuracy_summary 与 by_tier / by_regime 统计全部已评估结论。",
    ]


# ── 公共 API ──────────────────────────────────────────────

def record_conclusion(
    *,
    data_dir,
    kind: str,
    symbol: str,
    as_of: str,
    data_cutoff: str,
    payload: dict,
    inputs_fingerprint: str | None = None,
    tier: str = 'speculative',
) -> dict:
    """冻结一条结论并**只追加**一行，返回 `{id, kind, symbol, as_of, data_cutoff, tier, recorded}`。

    冻结的是 `as_of` / `data_cutoff` / `inputs_fingerprint` / `payload` 以及
    `generated_at`；`payload` 先做 JSON 规范化深拷贝，调用方之后改动原 dict 不会
    影响已写入的历史行。`inputs_fingerprint` 省略时复用
    `forecast_track.fingerprint(payload)`（同一组输入同一串）。

    id = `forecast_track.fingerprint(规范化后的冻结字段)`，即
    `sha256(json.dumps(sort_keys=True, ensure_ascii=False, default=str))[:16]`；
    字段含 kind|symbol|as_of|payload 以及 data_cutoff|tier|inputs_fingerprint，
    因此同参数必然同 id、任一冻结字段变化必然换 id，稳定可复现。

    **幂等语义**：同一 id 已在账本里时直接返回首次结果、不追加第二行；不删、不改
    任何历史行。这样重复生成/重试不会产生重复结论，也不会把旧结论覆盖掉。
    """
    root = Path(data_dir)
    frozen_payload = json.loads(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )
    effective_fp = (
        inputs_fingerprint
        if inputs_fingerprint is not None
        else forecast_track.fingerprint(frozen_payload)
    )
    identity = {
        "kind": kind,
        "symbol": symbol,
        "as_of": as_of,
        "data_cutoff": data_cutoff,
        "inputs_fingerprint": effective_fp,
        "tier": tier,
        "payload": frozen_payload,
    }
    conclusion_id = forecast_track.fingerprint(identity)
    result = {
        "id": conclusion_id,
        "kind": kind,
        "symbol": symbol,
        "as_of": as_of,
        "data_cutoff": data_cutoff,
        "tier": tier,
        "recorded": True,
    }
    row = {
        "type": _TYPE_CONCLUSION,
        "id": conclusion_id,
        "kind": kind,
        "symbol": symbol,
        "as_of": as_of,
        "generated_at": dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds"),
        "data_cutoff": data_cutoff,
        "inputs_fingerprint": effective_fp,
        "tier": tier,
        "immutable": True,
        "payload": frozen_payload,
    }
    with _lock:
        conclusions, _ = _snapshot(root)
        if conclusion_id in conclusions:
            return result
        _append_locked(root, row)
    return result


def evaluate_conclusion(
    *,
    data_dir,
    conclusion_id: str,
    realized: int,
    realized_note: str = '',
) -> dict:
    """追加一条事后评估结果，返回 `{id, conclusion_id, realized, ...}`。

    `realized=1` 表示「说涨真涨了 / 建议兑现了」，`0` 表示没兑现。**不改写原结论行**：
    评估是账本里新增的一行，原结论的 as_of / data_cutoff / inputs_fingerprint /
    payload 原封不动。评估 id 由 (conclusion_id, realized, realized_note) 取指纹，
    稳定可复现。找不到对应结论时仍按只追加写入（不抛异常），并在 `limitations` 里
    说明该评估是孤立的、统计时会被跳过。
    """
    if isinstance(realized, bool) or realized not in (0, 1):
        raise ValueError("realized 只能是 0（未兑现）或 1（兑现）")
    root = Path(data_dir)
    evaluation_id = forecast_track.fingerprint(
        {
            "type": _TYPE_EVALUATION,
            "conclusion_id": conclusion_id,
            "realized": realized,
            "realized_note": realized_note,
        }
    )
    row = {
        "type": _TYPE_EVALUATION,
        "id": evaluation_id,
        "conclusion_id": conclusion_id,
        "realized": realized,
        "realized_note": realized_note,
        "evaluated_at": dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds"),
        "immutable": True,
    }
    with _lock:
        conclusions, _ = _snapshot(root)
        found = conclusion_id in conclusions
        _append_locked(root, row)
    limitations = (
        []
        if found
        else ["账本里找不到该 conclusion_id；评估行仍按只追加写入，统计时跳过孤立评估。"]
    )
    return {
        "id": evaluation_id,
        "conclusion_id": conclusion_id,
        "realized": realized,
        "realized_note": realized_note,
        "conclusion_found": found,
        "recorded": True,
        "limitations": limitations,
    }


def calibration_curve(
    *,
    data_dir,
    bucket_size: float = 0.1,
) -> dict:
    """校准曲线：把「系统当时说的概率」和「事后是否兑现」对齐给普通投资者看。

    例：系统一共说过 20 次「上涨概率 60%」，其中只有 8 次真涨了 ⇒ 0.6 这一桶的
    `realized_rate = 0.40`，说明这个档位过度自信；普通投资者据此决定该把 60% 当
    60% 还是打个折。单一「历史准确率」会把高置信度和随口一说混在一起，校准曲线
    拆开它们，`brier = mean((p - realized)²)` 再给一个整体偏离度。

    `stated_probability` 从冻结的 `payload` 里取（小数制 0~1），取不到或口径不符
    就跳过该样本的**分桶与 overall**；`by_tier`（按 tier）和 `by_regime`（按
    payload.regime）仍覆盖全部有效评估样本，各算 count + realized_rate。缺 regime
    的样本不进 by_regime 并计入 limitations。无数据时不抛异常：
    `overall.count == 0`、`buckets == []`，`limitations` 说明原因。
    """
    if not math.isfinite(bucket_size) or bucket_size <= 0:
        raise ValueError("bucket_size 必须是有限的正数")
    limitations = _base_limitations()
    conclusions, evaluations = _snapshot(Path(data_dir))
    samples, orphans = _samples(conclusions, evaluations)

    if not conclusions:
        limitations.append("账本为空：暂无任何冻结结论，请先调用 record_conclusion。")
    if not samples:
        limitations.append("暂无有效评估数据：请先调用 evaluate_conclusion；此处返回空结论。")
    if orphans:
        limitations.append(f"{orphans} 条评估找不到对应结论，已跳过。")

    tier_keys = {str(conclusion.get("tier") or "unknown") for conclusion in conclusions.values()}
    known_tiers = (forecast_track.TIER_SOLID, forecast_track.TIER_SPECULATIVE)
    unexpected = sorted(tier_keys - set(known_tiers)) if samples else []
    if unexpected:
        limitations.append(
            f"出现非 forecast_track 标准 tier 取值 {unexpected}，仍按原值分组。"
        )

    missing_regime = 0

    def regime_of(sample: dict) -> str | None:
        nonlocal missing_regime
        value = _payload_of(sample["conclusion"]).get("regime")
        if value is None or str(value) == "":
            missing_regime += 1
            return None
        return str(value)

    by_tier = _group_rate(samples, lambda s: s["conclusion"].get("tier") or "unknown")
    by_regime = _group_rate(samples, regime_of)
    if missing_regime:
        limitations.append(f"{missing_regime} 条样本缺 payload.regime，未纳入 by_regime。")

    cohort: list[tuple[float, int]] = []
    skipped_probability = 0
    for sample in samples:
        probability = _as_probability(
            _payload_of(sample["conclusion"]).get("stated_probability")
        )
        if probability is None:
            skipped_probability += 1
            continue
        cohort.append((probability, sample["realized"]))
    if skipped_probability:
        limitations.append(
            f"{skipped_probability} 条已评估结论缺有效 payload.stated_probability（或口径非"
            " 0~1 小数制），未进入分桶 / overall / brier；仍计入 accuracy_summary 与分组。"
        )

    # -1e-9 抵消 1.0 / 0.1 这类浮点误差，避免多出一个空桶；math.ceil 已返回 int。
    n_buckets = max(1, math.ceil(1.0 / bucket_size - 1e-9))
    bucket_map: dict[int, list[tuple[float, int]]] = {}
    for probability, realized in cohort:
        # +1e-9 抵消 0.6 / 0.1 这类浮点误差，避免把 0.6 错分到 [0.5, 0.6)。
        index = min(math.floor(probability / bucket_size + 1e-9), n_buckets - 1)
        bucket_map.setdefault(index, []).append((probability, realized))
    buckets = [
        {
            "lo": round(index * bucket_size, 10),
            "hi": round(min((index + 1) * bucket_size, 1.0), 10),
            "count": len(items),
            "stated_avg": sum(p for p, _ in items) / len(items),
            "realized_rate": sum(r for _, r in items) / len(items),
        }
        for index, items in sorted(bucket_map.items())
    ]

    if cohort:
        overall = {
            "count": len(cohort),
            "stated_avg": sum(p for p, _ in cohort) / len(cohort),
            "realized_rate": sum(r for _, r in cohort) / len(cohort),
            "brier": sum((p - r) ** 2 for p, r in cohort) / len(cohort),
        }
    else:
        overall = {
            "count": 0,
            "stated_avg": None,
            "realized_rate": None,
            "brier": None,
        }

    return {
        "buckets": buckets,
        "by_tier": by_tier,
        "by_regime": by_regime,
        "overall": overall,
        "limitations": limitations,
    }


def accuracy_summary(*, data_dir) -> dict:
    """给页面显示「系统历史准确率」用；无数据不抛异常、返回带 limitations 的空结论。

    `total_recorded` 数冻结结论，`total_evaluated` 数**已评估的结论**（按
    conclusion_id 去重，重复评估取最后一条），`hit_rate = 兑现数 / 已评估数`。
    无样本时 `hit_rate` 为 `None` —— 沿用 `forecast_track` 的口径：无样本不计入
    分母，不是「0% 命中」。`by_kind` 按三类结论分别给
    `{recorded, evaluated, hits, hit_rate}`。
    """
    conclusions, evaluations = _snapshot(Path(data_dir))
    samples, orphans = _samples(conclusions, evaluations)

    by_kind: dict[str, dict] = {}
    for conclusion in conclusions.values():
        kind = str(conclusion.get("kind") or "unknown")
        group = by_kind.setdefault(
            kind, {"recorded": 0, "evaluated": 0, "hits": 0, "hit_rate": None}
        )
        group["recorded"] += 1
    hits = 0
    for sample in samples:
        kind = str(sample["conclusion"].get("kind") or "unknown")
        group = by_kind.setdefault(
            kind, {"recorded": 0, "evaluated": 0, "hits": 0, "hit_rate": None}
        )
        group["evaluated"] += 1
        group["hits"] += sample["realized"]
        hits += sample["realized"]
    for group in by_kind.values():
        if group["evaluated"]:
            group["hit_rate"] = group["hits"] / group["evaluated"]

    total_evaluated = len(samples)
    limitations = [
        "hit_rate 无样本时为 None（沿用 forecast_track 口径：不计入分母，不是 0% 命中）。",
        "同一 conclusion_id 多次评估时按最后一条生效；历史评估行全部保留、不删不改。",
        "realized 是事后 0/1 兑现标记而非收益；要看扣费后收益请用 forecast_track.evaluate。",
    ]
    if not conclusions:
        limitations.append("账本为空：暂无任何冻结结论。")
    if not samples:
        limitations.append("暂无有效评估数据：准确率需先调用 evaluate_conclusion。")
    if orphans:
        limitations.append(f"{orphans} 条评估找不到对应结论，已跳过。")

    return {
        "total_recorded": len(conclusions),
        "total_evaluated": total_evaluated,
        "hit_rate": hits / total_evaluated if total_evaluated else None,
        "by_kind": by_kind,
        "limitations": limitations,
    }
