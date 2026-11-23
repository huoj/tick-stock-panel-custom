"""手工持仓 append-only 台账、版本历史与 TWR/MWR。

为什么只追加：台账是唯一事实源，录入、修改、删除、除权、冲正都以新增事件表达，
历史行保持原样。这样版本历史可审计，当前持仓只由事件序列重放得出；修复旧数据也
通过 ``corp_action`` / ``undo`` 追加记录，覆盖式改写会让旧快照永久丢失。

并发边界：本模块用 Windows 下 ``open(..., 'a')`` 的一次单行 ``write`` 追加 JSONL，
同一进程内建议由调用方串行调用；跨进程同时写可能产生相同 ``seq`` 或行交错，
跨进程互斥由调用方负责。文件无锁、无事务、无历史行重写。

为什么 TWR 能剔除入金干扰、MWR 不能：TWR 逐期先减去该期外部现金流，再与期初
权益比较，连乘的是资金管理之外的投资回报；入金规模与时点被逐期隔离，因此不
污染管理能力评价。MWR 不能剔除入金干扰，因为 ``amount>0`` 入金对组合是流入、
对投资者是追加流出；以投资者现金流 ``CF_t=-amount_t`` 记，期初 ``-E_0``、每期
``CF_t``、期末 ``+E_last`` 一起按同一个期收益率折现，入金恰好落在高收益期前后
会改变 IRR。MWR 适合评价投资者实际资金体验，而非单纯评价持仓管理能力。

为什么简化 IRR 用二分：这里只用标准库、按估值期序号折现；在常见单一变号现金流下
NPV 连续且单调，固定区间 ``[-0.99, 10]`` 上做 60 次二分即可得到稳定近似，
无需第三方求解器。端点未变号、端点同为 0、NPV 非有限时，返回 ``None`` 并写明
limitation，保持 fail-closed。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path

__all__ = [
    "append_event",
    "load_events",
    "replay_positions",
    "twr_mwr",
    "version_history",
]

_ALLOWED_OPS = frozenset({"upsert", "delete", "cash", "corp_action", "dividend", "undo"})
_RESERVED_EVENT_FIELDS = frozenset({"seq", "event_id", "ts", "op", "symbol"})
_MISSING = object()


class _EventList(list):
    """事件列表，附带被跳过的坏行说明。"""

    def __init__(self, rows=None, limitations=None):
        super().__init__(rows or [])
        self.limitations = list(limitations or [])


class _PositionResult(dict):
    """持仓快照；``limitations`` 既可用属性，也可用同名键读取。"""

    def __init__(self, positions=None, limitations=None):
        super().__init__(positions or {})
        self.limitations = list(limitations or [])

    def __getitem__(self, key):
        if key == "limitations" and not super().__contains__(key):
            return self.limitations
        return super().__getitem__(key)

    def __contains__(self, key):
        if key == "limitations" and not super().__contains__(key):
            return True
        return super().__contains__(key)

    def get(self, key, default=None):
        if key == "limitations" and not super().__contains__(key):
            return self.limitations
        return super().get(key, default)


def _safe_text(value, default=""):
    if value is None:
        return default
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - 外部数据字符串化失败时降级为空值
        return default


def _canonical_symbol(value):
    if value is None:
        return ""
    return _safe_text(value).strip().upper()


def _ledger_path(data_dir):
    return Path(os.fsdecode(os.fspath(data_dir))) / "manual_holdings_ledger" / "events.jsonl"


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _event_seq(record):
    seq = _as_int(record.get("seq"))
    return seq if seq is not None and seq >= 1 else None


def _is_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _optional_text(value):
    return None if value is None else _safe_text(value)


def _position_from_event(event):
    qty = event.get("qty")
    avg_cost = event.get("avg_cost")
    if not _is_finite_number(qty) or not _is_finite_number(avg_cost):
        return None
    if float(qty) <= 0 or float(avg_cost) == 0:
        return None
    return {
        "qty": qty,
        "avg_cost": avg_cost,
        "entry_date": _optional_text(event.get("entry_date")),
        "note": _safe_text(event.get("note", "")),
    }


def _row_from_snapshot(snapshot, symbol):
    """把 undo 快照解释成目标 symbol 的动作，其它 symbol 永不参与。"""
    if snapshot is None:
        return "remove", None
    if not isinstance(snapshot, dict):
        return "invalid", None
    if isinstance(snapshot.get("positions"), dict):
        raw = snapshot["positions"].get(symbol, _MISSING)
    elif symbol in snapshot:
        raw = snapshot[symbol]
    elif all(value is None or isinstance(value, dict) for value in snapshot.values()):
        raw = _MISSING
    else:
        raw = snapshot
    if raw is _MISSING or raw is None or raw == {}:
        return "remove", None
    if not isinstance(raw, dict):
        return "invalid", None
    row = _position_from_event(raw)
    return ("row", row) if row is not None else ("invalid", None)


def _ordered_events(events):
    limitations = []
    try:
        limitations.extend(list(getattr(events, "limitations", []) or []))
    except Exception:  # noqa: BLE001 - 附带 limitation 的自定义列表对象异常时降级
        limitations.append("events 的附带 limitation 读取失败，已忽略")
    if not isinstance(events, (list, tuple)):
        limitations.append("events 不是列表，轨迹按空集处理")
        return [], limitations
    pairs = []
    for index, event in enumerate(events, start=1):
        try:
            if not isinstance(event, dict):
                raise TypeError("事件不是 dict")
            seq = _event_seq(event)
            if seq is None:
                raise ValueError("seq 缺失或非正整数")
        except Exception as exc:  # noqa: BLE001 - 单条坏事件隔离，保持其它事件可用
            limitations.append(f"第 {index} 条事件已跳过: {type(exc).__name__}")
            continue
        pairs.append((seq, index, event))
    pairs.sort(key=lambda item: (item[0], item[1]))
    return pairs, limitations


def append_event(*, data_dir, event: dict) -> dict:
    """追加一条事件到 ``data_dir/manual_holdings_ledger/events.jsonl``。

    ``seq`` 取当前物理行数 + 1；未提供 ``ts`` 时用秒精度本地时间；
    ``event_id`` 为 ``sha256(seq|ts|op|symbol)[:16]``。缺字段、非法 op、
    序列化失败、磁盘失败均返回 ``{"recorded": False, "reason": str}``。
    """
    if not isinstance(event, dict):
        return {"recorded": False, "reason": "event 必须是 dict"}
    op = _safe_text(event.get("op", "")).strip().lower()
    if not op:
        return {"recorded": False, "reason": "缺少 op"}
    if op not in _ALLOWED_OPS:
        return {"recorded": False, "reason": f"非法 op: {op}"}
    symbol = _canonical_symbol(event.get("symbol"))
    if not symbol:
        return {"recorded": False, "reason": "缺少有效 symbol"}

    try:
        path = _ledger_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        seq = 1
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                seq = sum(1 for _ in handle) + 1
    except Exception as exc:  # noqa: BLE001 - 路径/读取异常一律拒绝写入，fail-closed
        return {"recorded": False, "reason": f"台账读取失败: {type(exc).__name__}"}

    ts_value = event.get("ts")
    ts = (
        _safe_text(ts_value)
        if ts_value is not None and _safe_text(ts_value)
        else datetime.now().isoformat(timespec="seconds")
    )
    digest_src = f"{seq}|{ts}|{op}|{symbol}".encode()
    event_id = hashlib.sha256(digest_src).hexdigest()[:16]
    payload = {
        key: value
        for key, value in event.items()
        if key not in _RESERVED_EVENT_FIELDS
    }
    record = {"seq": seq, "event_id": event_id, "ts": ts, "op": op, **payload}
    if symbol:
        record["symbol"] = symbol

    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    except Exception as exc:  # noqa: BLE001 - 任意不可序列化值都按拒绝写入处理
        return {"recorded": False, "reason": f"event 序列化失败: {type(exc).__name__}"}
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    except Exception as exc:  # noqa: BLE001 - 追加失败不抛异常，统一返回拒绝原因
        return {"recorded": False, "reason": f"台账追加失败: {type(exc).__name__}"}
    return record


def load_events(*, data_dir, symbol: str | None = None) -> list[dict]:
    """按 ``seq`` 升序读回事件；坏行跳过，文件缺失返回空列表。

    ``symbol`` 给定时只返回该标的事件；无 symbol 的全局 cash / corp_action /
    dividend 在过滤时排除。返回的列表带 ``limitations`` 属性记录跳过原因。
    """
    limitations = []
    try:
        path = _ledger_path(data_dir)
    except Exception as exc:  # noqa: BLE001 - 路径解析失败也返回空列表，不抛异常
        return _EventList([], [f"台账路径解析失败: {type(exc).__name__}"])
    if not path.exists():
        return _EventList([], limitations)

    pairs = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("JSON 行不是 dict")
                    seq = _event_seq(record)
                    if seq is None:
                        raise ValueError("seq 缺失或非正整数")
                except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
                    limitations.append(f"第 {line_no} 行坏行已跳过: {type(exc).__name__}")
                    continue
                except Exception as exc:  # noqa: BLE001 - 单条坏行隔离
                    limitations.append(f"第 {line_no} 行坏行已跳过: {type(exc).__name__}")
                    continue
                pairs.append((seq, line_no, record))
    except OSError as exc:
        limitations.append(f"台账打开失败: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - 读取阶段任何异常都保留已解析结果
        limitations.append(f"台账读取中断: {type(exc).__name__}")

    pairs.sort(key=lambda item: (item[0], item[1]))
    target = None if symbol is None else _canonical_symbol(symbol)
    rows = []
    for _seq, _line_no, record in pairs:
        if target is not None and _canonical_symbol(record.get("symbol")) != target:
            continue
        rows.append(record)
    return _EventList(rows, limitations)


def replay_positions(*, events: list[dict]) -> dict[str, dict]:
    """把事件重放成 ``{symbol: {qty, avg_cost, entry_date, note}}``。

    ``upsert`` 覆盖该 symbol 的四个标准字段；``delete`` 移除；``corp_action``
    只采用事件里已算好的 qty / avg_cost；cash / dividend 保持持仓不变。

    ``undo`` 带 ``snapshot`` 时直接恢复目标 symbol 的快照；缺少 snapshot 时，
    ``ref_seq`` 命中本 symbol 的历史事件后按简化规则移除该 symbol，并在
    ``result.limitations`` / ``result["limitations"]`` 写明。快照结构可疑时
    以跳过该事件收尾，其它 symbol 保持原样。
    """
    pairs, limitations = _ordered_events(events)
    positions: dict[str, dict] = {}
    for seq, _index, event in pairs:
        try:
            op = _safe_text(event.get("op", "")).strip().lower()
            symbol = _canonical_symbol(event.get("symbol"))
            if op in {"cash", "dividend"}:
                continue
            if op == "upsert":
                if not symbol:
                    raise ValueError("upsert 缺少 symbol")
                row = _position_from_event(event)
                if row is None:
                    raise ValueError("upsert 的 qty / avg_cost 无效")
                positions[symbol] = row
            elif op == "delete":
                if not symbol:
                    raise ValueError("delete 缺少 symbol")
                positions.pop(symbol, None)
            elif op == "corp_action":
                if not symbol:
                    raise ValueError("corp_action 缺少 symbol")
                old = positions.get(symbol, {})
                merged = {
                    "qty": event.get("qty"),
                    "avg_cost": event.get("avg_cost"),
                    "entry_date": event["entry_date"] if "entry_date" in event else old.get("entry_date"),
                    "note": event["note"] if "note" in event else old.get("note", ""),
                }
                row = _position_from_event(merged)
                if row is None:
                    raise ValueError("corp_action 的 qty / avg_cost 无效")
                positions[symbol] = row
            elif op == "undo":
                if not symbol:
                    raise ValueError("undo 缺少 symbol")
                if "snapshot" in event:
                    action, row = _row_from_snapshot(event.get("snapshot"), symbol)
                    if action == "remove":
                        positions.pop(symbol, None)
                    elif action == "row":
                        positions[symbol] = row
                    else:
                        raise ValueError("undo snapshot 结构可疑")
                    continue
                ref_seq = _as_int(event.get("ref_seq"))
                if ref_seq is None or ref_seq < 1:
                    raise ValueError("undo 缺少有效 ref_seq")
                hit = any(
                    other_seq == ref_seq
                    and _canonical_symbol(other_event.get("symbol")) == symbol
                    for other_seq, _other_index, other_event in pairs
                )
                if not hit:
                    raise ValueError("undo ref_seq 未命中本 symbol 事件")
                positions.pop(symbol, None)
                limitations.append(
                    f"undo seq={seq} 缺少 snapshot，按简化规则移除 symbol={symbol}；其它 symbol 保持原样"
                )
            else:
                raise ValueError(f"未知 op: {op}")
        except Exception as exc:  # noqa: BLE001 - 单条异常事件跳过，保留其余重放结果
            limitations.append(f"seq={seq} 事件已跳过: {type(exc).__name__}")
    if "limitations" in positions:
        limitations.append("symbol=limitations 与元数据键冲突，请改用 .limitations 读取说明")
    return _PositionResult(positions, limitations)


def version_history(*, events: list[dict], symbol: str) -> list[dict]:
    """返回指定 symbol 的按 ``seq`` 升序变更轨迹。

    全局 cash / corp_action / dividend 没有 symbol，因此不会进入单标的轨迹；
    带该 symbol 的现金类事件仍作为该标的的留痕出现。
    """
    target = _canonical_symbol(symbol)
    if not target:
        return []
    pairs, _limitations = _ordered_events(events)
    history = []
    for seq, _index, event in pairs:
        if _canonical_symbol(event.get("symbol")) != target:
            continue
        row = {
            "seq": seq,
            "ts": _safe_text(event.get("ts", "")),
            "op": _safe_text(event.get("op", "")).strip().lower(),
            "symbol": target,
            "qty": event.get("qty"),
            "avg_cost": event.get("avg_cost"),
            "entry_date": event.get("entry_date"),
            "note": _safe_text(event.get("note", "")),
            "event_id": _safe_text(event.get("event_id", "")),
        }
        if "ref_seq" in event:
            row["ref_seq"] = event["ref_seq"]
        history.append(row)
    return history


def _date_text(value):
    if value is None:
        return ""
    try:
        if hasattr(value, "isoformat"):
            return str(value.isoformat())
        return str(value)
    except Exception:  # noqa: BLE001 - 日期对象异常时按空值跳过
        return ""


def _finite_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _clean_valuations(valuations, limitations):
    rows = []
    if not isinstance(valuations, (list, tuple)):
        limitations.append("valuations 不是列表，估值序列按空集处理")
        return rows
    for index, raw in enumerate(valuations, start=1):
        try:
            if not isinstance(raw, dict):
                raise TypeError("估值不是 dict")
            date = _date_text(raw.get("date"))
            equity = _finite_float(raw.get("equity"))
            if not date or equity is None or equity < 0:
                raise ValueError("date / equity 无效")
            rows.append({"date": date, "equity": equity, "_index": index})
        except Exception as exc:  # noqa: BLE001 - 单条坏估值隔离
            limitations.append(f"第 {index} 条估值已跳过: {type(exc).__name__}")
    rows.sort(key=lambda row: (row["date"], row["_index"]))
    if any(rows[i]["date"] == rows[i - 1]["date"] for i in range(1, len(rows))):
        limitations.append("估值日期重复，按输入序号分期")
    for row in rows:
        row.pop("_index", None)
    return rows


def _clean_cashflows(cashflows, limitations):
    rows = []
    if not isinstance(cashflows, (list, tuple)):
        limitations.append("cashflows 不是列表，现金流序列按空集处理")
        return rows
    for index, raw in enumerate(cashflows, start=1):
        try:
            if not isinstance(raw, dict):
                raise TypeError("现金流不是 dict")
            date = _date_text(raw.get("date"))
            amount = _finite_float(raw.get("amount"))
            if not date or amount is None:
                raise ValueError("date / amount 无效")
            rows.append((date, amount, index))
        except Exception as exc:  # noqa: BLE001 - 单条坏现金流隔离
            limitations.append(f"第 {index} 条现金流已跳过: {type(exc).__name__}")
    rows.sort(key=lambda row: (row[0], row[2]))
    return rows


def _assign_cashflows(rows, cashflows, limitations):
    period_cf = [0.0] * (len(rows) - 1)
    initial_extra = 0.0
    for date, amount, index in cashflows:
        if date <= rows[0]["date"]:
            initial_extra += amount
            limitations.append(f"第 {index} 条现金流日期不晚于期初估值，已并入期初投入")
            continue
        if date > rows[-1]["date"]:
            limitations.append(f"第 {index} 条现金流日期晚于期末估值，已忽略")
            continue
        for period in range(1, len(rows)):
            if date <= rows[period]["date"]:
                period_cf[period - 1] += amount
                break
    return period_cf, initial_extra


def _safe_npv(npv, rate, limitations):
    try:
        value = float(npv(rate))
    except Exception as exc:  # noqa: BLE001 - NPV 计算异常按非有限值处理
        limitations.append(f"IRR NPV 计算异常: {type(exc).__name__}")
        return float("nan")
    return value if math.isfinite(value) else float("nan")


def _bisect_irr(npv, limitations):
    lo, hi = -0.99, 10.0
    flo = _safe_npv(npv, lo, limitations)
    fhi = _safe_npv(npv, hi, limitations)
    if not math.isfinite(flo) or not math.isfinite(fhi):
        limitations.append("IRR 二分端点 NPV 非有限值，mwr 返回 None")
        return None
    if flo == 0.0 and fhi == 0.0:
        limitations.append("IRR 端点 NPV 均为 0，根不唯一，mwr 返回 None")
        return None
    if flo == 0.0:
        return lo
    if fhi == 0.0:
        return hi
    if (flo > 0.0) == (fhi > 0.0):
        limitations.append("二分区间 [-0.99, 10] 内 NPV 未变号，mwr 返回 None")
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        fmid = _safe_npv(npv, mid, limitations)
        if not math.isfinite(fmid):
            limitations.append("IRR 二分中点 NPV 非有限值，mwr 返回 None")
            return None
        if fmid == 0.0:
            return mid
        if (flo > 0.0) == (fmid > 0.0):
            lo, flo = mid, fmid
        else:
            hi, fhi = mid, fmid
    return (lo + hi) / 2.0


def twr_mwr(
    *,
    valuations: list[dict],    # [{date, equity}]，按 date 升序
    cashflows: list[dict],     # [{date, amount}]，amount>0 入金、<0 出金
) -> dict:
    """计算逐期 TWR 与简化期 IRR（MWR）。

    TWR：每期 ``r_t = (E_t - amount_t) / E_{t-1} - 1``，连乘 ``(1+r_t)-1``。
    MWR：现金流按估值期序号折现；投资者现金流 ``CF_t=-amount_t``，即入金为正时
    ``CF_t`` 为负，出金为正。NPV 使用 ``-E_0 + Σ CF_t/(1+r)^t +
    E_last/(1+r)^(n-1)``。这里按期序号而非自然日计息，是刻意保持输入契约简单。

    少于两期有效估值、期初权益为 0、非有限收益或二分端点未变号时，相应结果为
    ``None`` 并写入 ``limitations``；任何输入异常都以明确 limitation 收尾。
    """
    limitations = []
    rows = _clean_valuations(valuations, limitations)
    flows = _clean_cashflows(cashflows, limitations)
    periods = []
    if len(rows) < 2:
        limitations.append("有效估值少于 2 期，TWR/MWR 均为 None")
        return {"twr": None, "mwr": None, "periods": periods, "limitations": limitations}

    period_cf, initial_extra = _assign_cashflows(rows, flows, limitations)
    chain = 1.0
    twr_ok = True
    for period in range(1, len(rows)):
        prev_equity = rows[period - 1]["equity"]
        equity = rows[period]["equity"]
        cashflow = period_cf[period - 1]
        rate = None
        if prev_equity == 0:
            twr_ok = False
            limitations.append(f"第 {period} 期期初权益为 0，该期 TWR 置为 None")
        else:
            rate = (equity - cashflow) / prev_equity - 1.0
            if not math.isfinite(rate):
                twr_ok = False
                rate = None
                limitations.append(f"第 {period} 期 TWR 非有限值，已置为 None")
            else:
                chain *= 1.0 + rate
                if not math.isfinite(chain):
                    twr_ok = False
                    limitations.append("TWR 连乘结果非有限值，已置为 None")
        periods.append(
            {
                "date": rows[period]["date"],
                "from_date": rows[period - 1]["date"],
                "equity": equity,
                "prev_equity": prev_equity,
                "cashflow": cashflow,
                "r": rate,
            }
        )
    twr = chain - 1.0 if twr_ok and math.isfinite(chain) else None

    n_periods = len(rows) - 1
    e0 = rows[0]["equity"]
    e_last = rows[-1]["equity"]

    def _npv(rate):
        base = 1.0 + rate
        if base <= 0:
            return float("nan")
        try:
            total = -e0 - initial_extra
            for t, cf in enumerate(period_cf, start=1):
                total += (-cf) / (base**t)
            total += e_last / (base**n_periods)
        except (OverflowError, ZeroDivisionError):
            return float("nan")
        return total if math.isfinite(total) else float("nan")

    mwr = _bisect_irr(_npv, limitations)
    return {"twr": twr, "mwr": mwr, "periods": periods, "limitations": limitations}
