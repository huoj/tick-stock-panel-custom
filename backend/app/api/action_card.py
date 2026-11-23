"""行动卡与持仓计划 API —— `/api/action-card`、`/api/position-plans`。

三件事分开暴露, 对应三种用户状态 (见 services/position_plan.py):
- **自选观察**: 只有跟踪, 不计盈亏;
- **计划买入**: 用户填的分批价格/资金安排, **不参与会计**;
- **实际持仓**: 来自 paper 台账, 参与权重与浮盈亏。

行动卡 (`GET /api/action-card/{symbol}`) 把「事实 / 系统推断 / 用户计划」
三段分开返回, 并回答四个问题 —— **不产出买入/卖出分数**。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import (
    action_card,
    fundamentals_digest,
    portfolio_positions,
    position_plan,
    stock_diagnostics,
    strength_validation,
    symbol_resolve,
)
from app.strategy import paper
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/action-card", tags=["action-card"])
plans_router = APIRouter(prefix="/api/position-plans", tags=["position-plans"])

# 行动卡需要的历史长度 (够算 MA120 + 250 日分位 + 同形态统计)
LOOKBACK_BARS = 300


# ── 依赖 ────────────────────────────────────────────────

def _repo(request: Request) -> KlineRepository:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "行情仓库未初始化")
    return repo


def _data_dir(request: Request):
    from app.config import settings
    return settings.data_dir


def _resolve_symbol(repo: KlineRepository, text: str) -> symbol_resolve.Resolution:
    """把用户输入（代码/裸代码/名称）解析成标准 symbol；失败直接 404 并说清原因。"""
    return symbol_resolve.resolve_or_404(repo, text)


def _resolve_asset_type(repo: KlineRepository, symbol: str) -> str:
    try:
        return repo.resolve_asset_type(symbol)
    except Exception:  # noqa: BLE001 - 解析失败按股票处理, 不阻断
        return "stock"


def _load_bars(repo: KlineRepository, symbol: str, asset_type: str) -> pl.DataFrame:
    """读够诊断用的日线 (按日期升序)。

    按 asset_type 走 ``get_daily_asset`` —— ETF/指数与股票存在不同分区,
    用错方法会读到空表 (CONTRIBUTING §3.3: 不得凭代码格式猜资产类型)。
    """
    from datetime import timedelta

    end = date.today()
    start = end - timedelta(days=LOOKBACK_BARS * 2)   # 日历日冗余, 保证交易日够数
    df = repo.get_daily_asset(asset_type, symbol, start, end)
    if df is None or df.is_empty():
        raise HTTPException(404, f"{symbol} 没有可用日线数据 (asset_type={asset_type})")
    keep = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume") if c in df.columns]
    df = df.select(keep).sort("date")
    if df.height > LOOKBACK_BARS:
        df = df.tail(LOOKBACK_BARS)
    return df


def _load_positions(request: Request) -> dict[str, Any]:
    """当前实际持仓（auto：有手工录入就用手工，否则用模拟盘）。"""
    try:
        return portfolio_positions.load_actual_positions(_data_dir(request))
    except Exception as exc:  # noqa: BLE001
        logger.warning("load actual positions failed: %s", exc)
        return {"positions": [], "basis": None, "limitations": []}


def _find_holding(actual: dict[str, Any], symbol: str) -> dict | None:
    """真实持仓里找这只票 —— 找不到就是没有持仓 (自选/计划)。"""
    for pos in actual.get("positions") or []:
        if str(pos.get("symbol", "")).upper() == symbol.upper():
            return pos
    return None


def _resolve_account_id(request: Request) -> str:
    """取当前账户 id（用于读成交台账）。失败时回退默认账户。"""
    from app.config import settings
    try:
        acc_id, _note = portfolio_positions.resolve_account(settings.data_dir)
        return acc_id
    except Exception:  # noqa: BLE001
        return paper.DEFAULT_ACCOUNT_ID


def _symbol_name(repo: KlineRepository, symbol: str) -> str:
    try:
        return str(repo.get_name_map([symbol]).get(symbol) or "")
    except Exception:  # noqa: BLE001
        return ""


# ── 行动卡 ──────────────────────────────────────────────

@router.get("/{symbol}")
def get_action_card(
    symbol: str,
    request: Request,
    account: str | None = Query(None, description="指定模拟盘账户; 缺省取权益最大的有持仓账户"),
) -> dict[str, Any]:
    """一只票一张行动卡: 事实 / 系统推断 / 用户计划三段 + 四个问题。

    **不返回买入/卖出分数** —— 分数不可解释、无法追溯、掩盖分歧。
    """
    repo = _repo(request)
    resolved = _resolve_symbol(repo, symbol)
    sym = resolved.symbol or symbol
    asset_type = resolved.asset_type or _resolve_asset_type(repo, sym)
    df = _load_bars(repo, sym, asset_type)

    holding = _find_holding(_load_positions(request), sym)
    data_dir = _data_dir(request)
    plans = position_plan.load_all(data_dir)
    plan = plans.get(sym)

    progress = None
    if plan:
        last_price = float(df["close"][-1]) if df.height else None
        prog = position_plan.assess_plan(plan, last_price)
        progress = prog.to_dict() if prog else None

    # 财报与公告的自动解析 (PIT: 只用已公告数据)
    try:
        digest = fundamentals_digest.build_fundamental_digest(data_dir, sym)
    except Exception as exc:  # noqa: BLE001 - 摘要失败不该让整张卡 500
        logger.warning("fundamental digest failed for %s: %s", sym, exc)
        digest = None

    # 计划 vs 实际成交的自动对账 (只读, 不改写计划)
    recon = None
    try:
        account_id = _resolve_account_id(request)
        fills = paper.load_fills(data_dir, account_id)
        rep = position_plan.reconcile_with_fills(plan, fills, symbol=sym)
        recon = rep.to_dict() if rep else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile failed for %s: %s", sym, exc)

    card = action_card.build_action_card(
        df=df,
        symbol=sym,
        # 解析结果里已经带了名字; 维表查名失败时用它兜底, 不要退化成显示代码
        name=resolved.name or _symbol_name(repo, sym),
        holding=holding,
        plan=plan,
        plan_progress=progress,
        digest=digest,
    )
    card["reconciliation"] = recon

    # P1-4：把「推断」冻结进只追加账本，事后才能算校准曲线（说 60% 实际多少涨了）。
    # 这是影子写入：GET 仍以返回卡片为准，账本失败只记日志，绝不让整张卡 500。
    # 与 /api/risk 的「只读接口不写数据」契约不冲突 —— 这里写的是独立结论账本，
    # 不改行情、不改持仓、不改计划，也不会被重复 GET 追加重复行（record 幂等）。
    try:
        from app.services import forecast_calibration

        inference = card.get("inference") or {}
        as_of = str(card.get("as_of") or "")[:10] or date.today().isoformat()
        # forward_stats 的 win_rate 是 0~100 的百分数；账本要小数制 0~1。
        fwd = inference.get("forward_stats") or {}
        first = next((v for v in fwd.values() if isinstance(v, dict) and v.get("win_rate") is not None), None)
        stated = float(first["win_rate"]) / 100.0 if first else None
        payload = {
            "confidence": inference.get("confidence"),
            "strength_stance": inference.get("strength_stance"),
            "confidence_basis": inference.get("confidence_basis"),
            "analog_sample_size": inference.get("analog_sample_size"),
            "rule_validation_verdict": (inference.get("rule_validation") or {}).get("verdict"),
        }
        if stated is not None and 0.0 <= stated <= 1.0:
            payload["stated_probability"] = stated
        frozen = forecast_calibration.record_conclusion(
            data_dir=data_dir,
            kind="action_card_inference",
            symbol=sym,
            as_of=as_of,
            data_cutoff=as_of,
            payload=payload,
            tier="solid" if inference.get("confidence") == "medium" else "speculative",
        )
        card["forecast_ledger_id"] = frozen.get("id")
    except Exception as exc:  # noqa: BLE001 - 账本不可用时不影响卡片
        logger.warning("forecast ledger shadow write failed for %s: %s", sym, exc)
        card["forecast_ledger_id"] = None

    return card


@router.get("/{symbol}/diagnosis")
def get_diagnosis(symbol: str, request: Request) -> dict[str, Any]:
    """只返回诊断原始数据 (不含计划与行动建议), 供高级用户自行判读。"""
    repo = _repo(request)
    resolved = _resolve_symbol(repo, symbol)
    sym = resolved.symbol or symbol
    df = _load_bars(repo, sym, resolved.asset_type or _resolve_asset_type(repo, sym))
    try:
        return stock_diagnostics.diagnose(df)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{symbol}/validate-strength")
def validate_strength(
    symbol: str,
    request: Request,
    step: int = Query(3, ge=1, le=60, description="采样步长(交易日); 越小越慢越准"),
) -> dict[str, Any]:
    """检验「强弱判定」在这只票的历史上到底有没有预测力。

    **这是行动卡诚实性的来源**: 如果规则无效, 这个端点会如实说出来,
    而不是让用户以为卡片上的"偏强"有统计支持。

    结果带 ``verdict``: validated / weak_signal / inverted / no_edge / insufficient。
    其中 ``inverted`` 表示"一致地反着走" —— 反向指标, 比无效更值得注意。
    """
    repo = _repo(request)
    resolved = _resolve_symbol(repo, symbol)
    sym = resolved.symbol or symbol
    df = _load_bars(repo, sym, resolved.asset_type or _resolve_asset_type(repo, sym))
    report = strength_validation.validate_strength_rules(df, symbol=sym, step=step)
    return report.to_dict()


@router.get("/validate-strength-market")
def validate_strength_market(
    request: Request,
    limit: int = Query(60, ge=5, le=200, description="抽样标的最大数量"),
    step: int = Query(10, ge=1, le=60, description="采样步长(交易日)"),
) -> dict[str, Any]:
    """跨多只标的汇总检验强弱规则 —— 单票样本常常不足 30，汇总才有统计意义。

    抽样方式: 按代码排序后等间隔取 ``limit`` 只，避免只覆盖某一代码段。
    耗时随 limit 线性增长（60 只约 1 分钟）。
    """
    repo = _repo(request)
    symbols = _sample_symbols(repo, limit)
    panels: dict[str, pl.DataFrame] = {}
    for sym in symbols:
        try:
            panels[sym] = _load_bars(repo, sym, _resolve_asset_type(repo, sym))
        except HTTPException:
            continue
    if not panels:
        raise HTTPException(503, "没有可用的日线样本")
    return strength_validation.validate_across_symbols(panels, step=step, max_symbols=limit)


def _sample_symbols(repo: KlineRepository, limit: int) -> list[str]:
    """等间隔抽样，覆盖全部代码段。"""
    try:
        frame = repo.get_instruments()
        symbols = sorted(str(s) for s in frame["symbol"].to_list())
    except Exception:  # noqa: BLE001
        return []
    if not symbols:
        return []
    stride = max(1, len(symbols) // limit)
    return symbols[::stride][:limit]


@plans_router.get("/capital-check")
def capital_check(request: Request, account: str | None = Query(None)) -> dict[str, Any]:
    """所有计划的分批金额合计 vs 账户可用现金。

    加总的是**尚未执行**金额（已成交的已经花掉现金，再加一遍会重复计算）。
    只做校验，不拦截下单，也不自动调整计划。
    """
    data_dir = _data_dir(request)
    plans = position_plan.load_all(data_dir)
    cash: float | None = None
    try:
        acc_id, _note = portfolio_positions.resolve_account(data_dir, account)
        ov = paper.overview(data_dir, account_id=acc_id)
        if ov.get("initialized"):
            cash = float(ov.get("cash") or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("capital-check: 账户现金不可用: %s", exc)
    return position_plan.check_aggregate_capital(plans, available_cash=cash).to_dict()


@plans_router.get("/{symbol}/reconcile")
def reconcile_plan(symbol: str, request: Request, account: str | None = Query(None)) -> dict[str, Any]:
    """计划 vs 实际成交台账的自动对账。

    **只读** —— 只报告匹配结果与差异，不自动改写计划（计划是用户的意图记录）。
    """
    repo = _repo(request)
    sym = _resolve_symbol(repo, symbol).symbol or symbol.strip().upper()
    data_dir = _data_dir(request)
    plan = position_plan.load_one(data_dir, sym)
    if plan is None:
        raise HTTPException(404, f"{sym} 没有买入计划")
    try:
        acc_id = account or _resolve_account_id(request)
        fills = paper.load_fills(data_dir, acc_id)
    except Exception as exc:
        raise HTTPException(503, f"成交台账不可用: {exc}") from exc
    report = position_plan.reconcile_with_fills(plan, fills, symbol=sym)
    if report is None:
        raise HTTPException(404, f"{sym} 没有买入计划")
    return report.to_dict()


# ── 持仓计划 ────────────────────────────────────────────

class BatchModel(BaseModel):
    price: float = Field(..., gt=0, description="这一批的计划买入价")
    amount: float = Field(0, ge=0, description="这一批计划投入金额(元)")
    label: str = ""
    done: bool = False


class PlanModel(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    planned_hold_days: int | None = Field(None, description="计划持有天数; 到期提醒重新评估")
    acceptable_loss_pct: float | None = Field(None, description="可接受的亏损百分比上限")
    batches: list[BatchModel] = Field(default_factory=list)
    note: str = ""


@plans_router.get("")
def list_plans(request: Request) -> dict[str, Any]:
    """全部计划 + 每只票的兑现度对照。"""
    plans = position_plan.load_all(_data_dir(request))
    return {
        "plans": list(plans.values()),
        "count": len(plans),
        "note": "计划买入不参与盈亏计算; 已成交的计划应在模拟盘下单后从计划里移除或标记完成",
    }


@plans_router.get("/{symbol}")
def get_plan(symbol: str, request: Request) -> dict[str, Any]:
    sym = _resolve_symbol(_repo(request), symbol).symbol or symbol.strip().upper()
    plan = position_plan.load_one(_data_dir(request), sym)
    if plan is None:
        raise HTTPException(404, f"{sym} 没有买入计划")
    return plan


@plans_router.put("/{symbol}")
def upsert_plan(symbol: str, body: PlanModel, request: Request) -> dict[str, Any]:
    """新建或更新计划。校验失败返回 400 (fail-closed, 不静默修正)。"""
    repo = _repo(request)
    sym = _resolve_symbol(repo, symbol).symbol or symbol.strip().upper()
    body_sym = _resolve_symbol(repo, body.symbol).symbol or body.symbol.strip().upper()
    if body_sym != sym:
        raise HTTPException(400, f"路径里的 symbol（{sym}）与请求体（{body_sym}）不一致")
    payload = {
        "symbol": sym,
        "planned_hold_days": body.planned_hold_days,
        "acceptable_loss_pct": body.acceptable_loss_pct,
        "batches": [b.model_dump() for b in body.batches],
        "note": body.note,
    }
    try:
        return position_plan.save_one(_data_dir(request), payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@plans_router.delete("/{symbol}")
def delete_plan(symbol: str, request: Request) -> dict[str, Any]:
    sym = _resolve_symbol(_repo(request), symbol).symbol or symbol.strip().upper()
    ok = position_plan.delete_one(_data_dir(request), sym)
    if not ok:
        raise HTTPException(404, f"{sym} 没有买入计划")
    return {"deleted": sym}


@plans_router.get("/{symbol}/progress")
def get_plan_progress(symbol: str, request: Request) -> dict[str, Any]:
    """计划 vs 现价的对照 —— 只陈述事实, 不给建议。"""
    repo = _repo(request)
    resolved = _resolve_symbol(repo, symbol)
    sym = resolved.symbol or symbol.strip().upper()
    plan = position_plan.load_one(_data_dir(request), sym)
    if plan is None:
        raise HTTPException(404, f"{sym} 没有买入计划")
    df = _load_bars(repo, sym, resolved.asset_type or _resolve_asset_type(repo, sym))
    price = float(df["close"][-1]) if df.height else None
    prog = position_plan.assess_plan(plan, price)
    return prog.to_dict() if prog else {"symbol": sym, "state": position_plan.STATE_PLANNING}


# ── 三态总览 ────────────────────────────────────────────

@plans_router.get("/classify/{symbols}")
def classify_symbols(symbols: str, request: Request) -> dict[str, Any]:
    """把一串标的分类成 自选观察 / 计划买入 / 实际持仓, 并标明是否计入盈亏。

    ``symbols`` 逗号分隔。这是防「把提示误认为已持仓」的总闸门。
    """
    raw = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw:
        raise HTTPException(400, "symbols 不能为空")
    if len(raw) > 500:
        raise HTTPException(400, "一次最多 500 个标的")

    # 名称/裸代码先解析成标准 symbol; 解析不了的不整体 400, 而是逐条如实报告
    resolved, unresolved = symbol_resolve.resolve_many(_repo(request), raw)
    syms = [r.symbol for r in resolved if r.symbol]
    if not syms:
        return {
            "items": [],
            "counts": {state: 0 for state in position_plan.STATE_LABELS},
            "labels": position_plan.STATE_LABELS,
            "unresolved": unresolved,
            "note": "只有「实际持仓」参与盈亏计算；计划买入与自选观察都不计入",
        }

    data_dir = _data_dir(request)
    plans = position_plan.load_all(data_dir)
    try:
        actual = portfolio_positions.load_actual_positions(data_dir)
        holding = {
            str(p.get("symbol", "")).upper()
            for p in actual.get("positions") or []
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("classify: actual positions unavailable: %s", exc)
        holding = set()

    rows = position_plan.classify_all(syms, holding_symbols=holding, plans=plans)
    counts = {state: sum(1 for r in rows if r["state"] == state) for state in position_plan.STATE_LABELS}
    return {
        "items": rows,
        "counts": counts,
        "labels": position_plan.STATE_LABELS,
        "unresolved": unresolved,
        "note": "只有「实际持仓」参与盈亏计算；计划买入与自选观察都不计入",
    }
