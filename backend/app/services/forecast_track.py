"""前瞻跟踪：把每条结论**先冻结**，事后再用真实行情打分。

用户要求（2026-11-09）：
「建立历史回测与前瞻跟踪：因子和政策事件的结论都记录生成时间及当时可用的数据，
防止未来信息泄漏；评估扣除交易成本后的收益、回撤、换手率、不同市场状态下的表现；
分别评估事件方向判断和股票映射准确率；把『官方证据足、映射明确』与『推测性主题
关联』分开统计；保留版本和修改记录，策略变更后不能覆盖旧预测结果。」

这份模块只做三件事，每件都对应上面一句：

1. **冻结（record）**：写下结论时就固定 `as_of`（做判断的日期）、`generated_at`、
   `data_cutoff`（当时可用的数据到哪天）、以及一组 `inputs_fingerprint`
   （输入数据/参数/模型版本），并存成**只增不改**的版本目录。同一条结论被重新
   生成时会得到新版本号，旧版本**永远保留** —— 不允许覆盖。
2. **打分（evaluate）**：用**晚于 data_cutoff** 的行情算前瞻收益，扣掉交易成本，
   给出方向是否命中、映射是否赚钱、回撤、换手，并按市场状态分组。
3. **分组报表（report）**：把「官方证据足 / 映射明确」（`fact`/`reported` +
   A/B 级）与「推测性主题关联」（C/D 或 pending）**分开**统计准确率，
   不混成一个"命中率"。

为什么要防未来信息泄漏：`data_cutoff` 一旦晚于 `as_of`，评测就等于用未来数据
给自己打分。所以读取行情时**只取 cutoff 之后**的交易日；而且冻结时记下的
`data_cutoff` 不会被后续评测改写。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

import polars as pl

logger = logging.getLogger(__name__)

#: 交易成本：单边按万分之 N 计（佣金 + 滑点），卖出再加印花税。
DEFAULT_COMMISSION_BPS = 5.0
DEFAULT_STAMP_TAX_BPS = 5.0

#: 事件结论的两种证据等级（用于分组统计，用户要求分开看）。
TIER_SOLID = "solid"          # 官方证据足 + 映射明确
TIER_SPECULATIVE = "speculative"  # 推测性主题关联
TIER_LABELS = {TIER_SOLID: "官方证据足/映射明确", TIER_SPECULATIVE: "推测性主题关联"}

_VERIFICATION_RANK = {"pending": 0, "inferred": 1, "reported": 2, "fact": 3, "evidenced": 4}
_EVIDENCED_LEVELS = ("A", "B")


# ── 目录与版本：只增不改 ────────────────────────────────

def track_root(data_dir: Path, kind: str = "event") -> Path:
    """``data/forecast_track/<kind>/``。"""
    return data_dir / "forecast_track" / kind


def _slug(text: str) -> str:
    """把标识压成安全文件名（保留中文，去掉路径分隔符与空格）。"""
    cleaned = "".join(c for c in str(text) if c not in '\\/:*?"<>|' and not c.isspace())
    return cleaned[:80] or "unknown"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def fingerprint(payload: Any) -> str:
    """输入指纹：同一组输入得到同一串，变了就是变了。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def next_version(directory: Path) -> int:
    """下一个版本号（目录里已有的最大 vN + 1）。"""
    best = 0
    if directory.exists():
        for p in directory.glob("v*.json"):
            try:
                best = max(best, int(p.stem[1:]))
            except ValueError:
                continue
    return best + 1


def record(
    data_dir: Path,
    *,
    kind: str,
    key: str,
    payload: dict,
    as_of: dt.date,
    data_cutoff: dt.date | None = None,
    inputs: Any = None,
    generated_at: dt.datetime | None = None,
    model_version: str | None = None,
) -> dict:
    """冻结一条结论。返回写入的版本记录（含 version / path）。

    `data_cutoff` 是**做判断时数据可用到哪天**，默认等于 `as_of`；
    显式传更早的日期表示"只用了更旧的数据"。它一旦写下就不改。
    """
    cutoff = data_cutoff or as_of
    if cutoff > as_of:
        raise ValueError(
            f"data_cutoff({cutoff}) 晚于 as_of({as_of})：那等于用未来数据做判断"
        )
    directory = track_root(data_dir, kind) / _slug(key)
    version = next_version(directory)
    now = generated_at or dt.datetime.now(dt.timezone.utc).astimezone()
    body = {
        "kind": kind,
        "key": key,
        "version": version,
        "as_of": as_of.isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "data_cutoff": cutoff.isoformat(),
        "model_version": model_version,
        "inputs_fingerprint": fingerprint(inputs if inputs is not None else payload),
        "immutable": True,
        "payload": payload,
        # 评测结果只追加在这里，不回头改上面的冻结内容
        "evaluations": [],
    }
    path = directory / f"v{version}.json"
    _atomic_write_json(path, body)
    body["_path"] = str(path)
    return body


def list_versions(data_dir: Path, kind: str, key: str) -> list[dict]:
    directory = track_root(data_dir, kind) / _slug(key)
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("v*.json"), key=lambda x: int(x.stem[1:])):
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        body["_path"] = str(p)
        out.append(body)
    return out


def latest(data_dir: Path, kind: str, key: str) -> dict | None:
    versions = list_versions(data_dir, kind, key)
    return versions[-1] if versions else None


def list_keys(data_dir: Path, kind: str) -> list[str]:
    root = track_root(data_dir, kind)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# ── 行情读取：只取 cutoff 之后 ──────────────────────────

_PRICE_DIRS = ("kline_daily_enriched", "kline_etf_enriched", "kline_index_enriched")


def load_close_after(
    data_dir: Path,
    symbol: str,
    *,
    after: dt.date,
    limit_days: int = 60,
) -> list[tuple[dt.date, float]]:
    """取 ``after`` **之后**的收盘价序列（升序）。

    只读 cutoff 之后的数据，是防未来信息泄漏的关键一步：评测用的价格必须晚于
    结论生成时的数据截止日，否则等于拿答案给自己打分。
    """
    out: list[tuple[dt.date, float]] = []
    for name in _PRICE_DIRS:
        base = data_dir / name
        if not base.is_dir():
            continue
        parts = sorted(base.glob("date=*"))
        for part in parts:
            try:
                day = dt.date.fromisoformat(part.name[len("date="):][:10])
            except ValueError:
                continue
            if day <= after:
                continue
            path = part / "part.parquet"
            if not path.is_file():
                continue
            try:
                frame = pl.read_parquet(path, columns=["symbol", "raw_close"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("读 %s 失败: %s", path, exc)
                continue
            hit = frame.filter(pl.col("symbol") == symbol)
            if hit.is_empty():
                continue
            value = hit["raw_close"][0]
            if value is None:
                continue
            out.append((day, float(value)))
            if len(out) >= limit_days:
                return out
        if out:
            break
    return out


def _max_drawdown(values: Iterable[float]) -> float | None:
    peak = None
    mdd = 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd if peak is not None else None


def _horizon_return(
    series: list[tuple[dt.date, float]], horizon_days: int
) -> tuple[float | None, float | None]:
    """(区间收益, 区间内最大回撤)。样本不足返回 (None, None) —— 不拿不足的天数凑。"""
    if not series:
        return None, None
    entry = series[0][1]
    if entry <= 0:
        return None, None
    if len(series) < horizon_days:
        return None, None
    window = series[:horizon_days]
    exit_price = window[-1][1]
    return exit_price / entry - 1.0, _max_drawdown([p for _, p in window])


def net_return(
    gross: float | None,
    *,
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    stamp_tax_bps: float = DEFAULT_STAMP_TAX_BPS,
) -> float | None:
    """扣费后收益：买一次佣金，卖一次佣金 + 印花税。"""
    if gross is None:
        return None
    cost = (commission_bps + commission_bps + stamp_tax_bps) / 10000.0
    return gross - cost


# ── 打分 ──────────────────────────────────────────────

def _direction_hit(direction: str | None, ret: float | None) -> bool | None:
    """方向是否命中。`不确定`/缺数据 → None（不计入分母，不是"错"）。"""
    if direction not in ("利好", "利空") or ret is None:
        return None
    return ret > 0 if direction == "利好" else ret < 0


def market_state_for(data_dir: Path, day: dt.date) -> str | None:
    """取 `day` 当天（或之前最近一天）的市场状态，用于按状态分组。

    用户要求「评估…不同市场状态下的表现」。这里复用 regime 模块已经算好的
    市场状态（`regime_builder.load_regime_history`），**不自己另算一套** ——
    同一个概念在系统里只能有一个口径。

    找不到就返回 None（报表里该样本不进任何状态分组），不猜一个状态。
    """
    try:
        from app.services import regime_builder
    except Exception:                                     # pragma: no cover
        return None
    try:
        frame = regime_builder.load_regime_history(data_dir)
    except Exception as e:                                # noqa: BLE001
        logger.debug("读 regime 历史失败，市场状态按未知处理: %s", e)
        return None
    if frame is None or frame.is_empty() or "date" not in frame.columns:
        return None
    state_col = next(
        (c for c in ("state", "regime", "phase", "market_state") if c in frame.columns), None
    )
    if state_col is None:
        return None
    try:
        usable = frame.filter(pl.col("date") <= day).sort("date")
    except Exception:                                     # noqa: BLE001
        return None
    if usable.is_empty():
        return None
    value = usable[state_col][-1]
    return str(value) if value is not None else None


def evaluate(
    data_dir: Path,
    record_body: dict,
    *,
    horizon_days: int = 5,
    commission_bps: float = DEFAULT_COMMISSION_BPS,
    stamp_tax_bps: float = DEFAULT_STAMP_TAX_BPS,
    market_state: str | None = None,
    price_dir: Path | None = None,
) -> dict:
    """对一条**已冻结**的结论打分（不修改冻结内容，结果作为一次 evaluation 追加）。

    `market_state` 由调用方给（例如 regime_builder 算出的 risk_on/risk_off），
    我们只按它分组，不在这里猜市场状态。

    `price_dir` 默认等于 `data_dir`（生产里跟踪记录与行情同根，口径不会错位）；
    离线核查或临时跟踪目录场景可显式指向真实数据根，避免"拿空目录算出 0 个交易日"
    这种看起来像"没数据"、实际是"读错目录"的假象。
    """
    payload = record_body.get("payload") or {}
    symbol = payload.get("symbol")
    direction = payload.get("direction")
    cutoff = dt.date.fromisoformat(str(record_body.get("data_cutoff"))[:10])
    prices_root = price_dir or data_dir

    result: dict[str, Any] = {
        "evaluated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "as_of": record_body.get("as_of"),
        "data_cutoff": record_body.get("data_cutoff"),
        "horizon_days": horizon_days,
        "symbol": symbol,
        "direction": direction,
        "market_state": market_state,
        "entry_date": None,
        "exit_date": None,
        "gross_return": None,
        "net_return": None,
        "max_drawdown": None,
        "turnover": None,
        "direction_hit": None,
        "mapping_profitable": None,
        "available_days": 0,
        "limitations": [],
    }
    if not symbol:
        result["limitations"].append("payload 缺 symbol，无法评测")
        return result

    series = load_close_after(prices_root, symbol, after=cutoff, limit_days=horizon_days + 1)
    result["available_days"] = len(series)
    if len(series) < horizon_days:
        result["limitations"].append(
            f"cutoff({cutoff}) 之后只有 {len(series)} 个交易日，不足 {horizon_days} 天，暂不打分"
        )
        return result

    gross, mdd = _horizon_return(series, horizon_days)
    net = net_return(gross, commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps)
    result.update({
        "entry_date": series[0][0].isoformat(),
        "exit_date": series[horizon_days - 1][0].isoformat(),
        "gross_return": None if gross is None else round(gross, 6),
        "net_return": None if net is None else round(net, 6),
        "max_drawdown": None if mdd is None else round(mdd, 6),
        # 换手：一次开仓 + 一次平仓 = 2；这里只评一个持有期，所以恒为 2。
        # 记下来是为了让"换手率"这一项有明确的计法，而不是各页各算一套。
        "turnover": 2.0,
        "direction_hit": _direction_hit(direction, net),
        "mapping_profitable": None if net is None else net > 0,
    })
    return result


def append_evaluation(data_dir: Path, record_body: dict, evaluation: dict) -> dict:
    """把一次评测追加到版本记录里（**不改冻结字段**）。"""
    path = Path(record_body.get("_path") or "")
    if not path.is_file():
        raise FileNotFoundError("版本记录不存在，无法追加评测")
    body = json.loads(path.read_text(encoding="utf-8"))
    body.setdefault("evaluations", []).append(evaluation)
    _atomic_write_json(path, body)
    return body


# ── 分组统计 ──────────────────────────────────────────

def evidence_tier(payload: dict) -> str:
    """把一条结论分到「官方证据足/映射明确」或「推测性主题关联」。

    判据（两条都要满足才算 solid）：
      * 核实状态 >= reported（官方原文 / 公司公告 / 权威媒体）；
      * 映射等级 A 或 B（字面命中或公告自述）。
    其余一律 speculative —— 宁可把边界情况归到"推测"，也不抬高可信度。
    """
    verification = str(payload.get("verification") or "pending")
    level = str(payload.get("mapping_level") or "").upper()
    if _VERIFICATION_RANK.get(verification, 0) >= _VERIFICATION_RANK["reported"] and level in _EVIDENCED_LEVELS:
        return TIER_SOLID
    return TIER_SPECULATIVE


def summarize(evaluations: list[dict]) -> dict:
    """给一组评测结果做统计（方向准确率、映射胜率分开算）。"""
    scored = [e for e in evaluations if e.get("net_return") is not None]
    direction_cases = [e for e in scored if e.get("direction_hit") is not None]
    hits = sum(1 for e in direction_cases if e["direction_hit"])
    nets = [float(e["net_return"]) for e in scored]
    mdds = [float(e["max_drawdown"]) for e in scored if e.get("max_drawdown") is not None]
    return {
        "samples": len(evaluations),
        "scored": len(scored),
        "unscored": len(evaluations) - len(scored),
        "direction_samples": len(direction_cases),
        "direction_hits": hits,
        # 方向准确率的分母只含"给了明确方向且已打分"的样本
        "direction_accuracy": round(hits / len(direction_cases), 4) if direction_cases else None,
        "mapping_samples": len(scored),
        "mapping_win_rate": round(sum(1 for e in scored if e.get("mapping_profitable")) / len(scored), 4)
        if scored else None,
        "avg_net_return": round(sum(nets) / len(nets), 6) if nets else None,
        "worst_net_return": round(min(nets), 6) if nets else None,
        "avg_max_drawdown": round(sum(mdds) / len(mdds), 6) if mdds else None,
        "worst_max_drawdown": round(min(mdds), 6) if mdds else None,
        "avg_turnover": round(sum(float(e.get("turnover") or 0) for e in scored) / len(scored), 4)
        if scored else None,
    }


def report(data_dir: Path, kind: str = "event") -> dict:
    """按证据等级 / 市场状态分组统计（用户要求：两者分开看，不能混成一个命中率）。"""
    rows: list[dict] = []
    for key in list_keys(data_dir, kind):
        for version in list_versions(data_dir, kind, key):
            payload = version.get("payload") or {}
            tier = evidence_tier(payload)
            for ev in version.get("evaluations") or []:
                rows.append({**ev, "tier": tier, "key": key,
                             "version": version.get("version"),
                             "model_version": version.get("model_version")})

    def _bucket(predicate) -> list[dict]:
        return [r for r in rows if predicate(r)]

    by_tier = {t: summarize(_bucket(lambda r, t=t: r["tier"] == t)) for t in TIER_LABELS}
    states = sorted({str(r.get("market_state")) for r in rows if r.get("market_state")})
    by_state = {s: summarize(_bucket(lambda r, s=s: str(r.get("market_state")) == s)) for s in states}
    return {
        "kind": kind,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "tiers": {TIER_LABELS[t]: by_tier[t] for t in TIER_LABELS},
        "by_market_state": by_state,
        "overall": summarize(rows),
        "notes": [
            "方向准确率的分母只含「给了明确方向且已打分」的样本；判为『不确定』的不计入",
            "映射胜率看扣费后收益是否为正，与方向正确是两件事",
            "官方证据足/映射明确的样本与推测性主题关联的样本**分开统计**，不合并成一个命中率",
        ],
    }
