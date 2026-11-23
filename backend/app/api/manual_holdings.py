"""手工录入的真实持仓 API —— `/api/manual-holdings`。

不玩模拟盘、也不接券商接口的人，用这三个端点录数量/成本/建仓日，就能让
「持仓总览」「行动卡」「组合风险」拿到真实权重（见 services/manual_holdings.py）。

设计要点：
- ``PUT`` 是 upsert（录入与修改同一个动作，用户不用先判断"这条存不存在"）；
- 校验失败抛 400 并给中文原因，**不静默修正数字**；
- 删一条要用 DELETE，把数量改成 0 不算删除（0 股不是持仓，会让人误以为还留着记录）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import holding_ledger, manual_holdings, portfolio_positions, symbol_resolve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/manual-holdings", tags=["manual-holdings"])


def _data_dir(request: Request):
    from app.config import settings

    return settings.data_dir


def _repo(request: Request):
    repo = getattr(request.app.state, "repo", None)
    return repo


def _resolve(request: Request, text: str) -> symbol_resolve.Resolution:
    """名称/代码都接受；解析不了直接 404 并说清原因。"""
    repo = _repo(request)
    if repo is None:
        # 没有仓库时仍允许用代码形态录入（离线场景），但不给名称解析
        raise HTTPException(503, "行情仓库未初始化，暂时无法解析名称，请改用 6 位代码")
    return symbol_resolve.resolve_or_404(repo, text)


class HoldingModel(BaseModel):
    qty: float = Field(..., description="持仓数量（股/份，股票必须整数）")
    avg_cost: float = Field(..., description="平均成本价（元/股，可为负：做 T 摊薄后的正常结果）")
    entry_date: str | None = Field(None, description="建仓日期 YYYY-MM-DD，可空")
    asset_type: str = Field("stock", description="stock / etf")
    name: str | None = Field(None, description="名称，留空则用维表里的名字")
    note: str = Field("", description="备注")
    cost_basis: str = Field(
        "broker_display",
        description="成本口径：broker_display=券商展示价(不复权，与现价同尺度) / "
                    "forward_adjusted=前复权价(需复权因子才能与现价对齐)",
    )


class AccountModel(BaseModel):
    cash: float | None = Field(None, description="账户现金余额；不填则权重合计会等于 1")


@router.get("")
def list_holdings(request: Request) -> dict[str, Any]:
    """全部手工持仓 + 一份按日线估值的汇总（可直接当「我的持仓」看）。"""
    data_dir = _data_dir(request)
    rows = manual_holdings.load_all(data_dir)
    acct = manual_holdings.load_account(data_dir)
    # 汇总走与风险面板同一条口径，避免两处算出不同的浮盈亏
    summary = portfolio_positions.load_actual_positions(data_dir, source=portfolio_positions.SOURCE_MANUAL)
    return {
        "holdings": sorted(rows.values(), key=lambda r: str(r.get("symbol") or "")),
        "count": len(rows),
        "cash": acct.get("cash"),
        "summary": {
            "market_value": summary.get("market_value"),
            "total": summary.get("total"),
            "total_pnl": summary.get("total_pnl"),
            "price_source": summary.get("price_source"),
            "position_count": summary.get("position_count"),
            "weight_basis": summary.get("weight_basis"),
        },
        "positions": summary.get("positions") or [],
        "limitations": summary.get("limitations") or [],
    }


@router.get("/ledger")
def ledger_summary(request: Request) -> dict[str, Any]:
    """P1-6：把只追加台账读回来 —— 版本历史 + 回放持仓 + TWR/MWR。

    手工录入没有逐日市值流水，估值序列只能用「每次事件后的累计投入成本」近似，
    TWR/MWR 因此会在 limitations 里如实标注「按成本估值，非真实市值，仅供参考」。
    """
    data_dir = _data_dir(request)
    events = holding_ledger.load_events(data_dir=data_dir)
    snapshot = holding_ledger.replay_positions(events=list(events))
    history = {
        sym: holding_ledger.version_history(events=list(events), symbol=sym)
        for sym in snapshot
    }
    # 用事件的累计成本构造一个「估值序列 + 现金流序列」，喂给 twr_mwr。
    # cash 事件视作入金；持仓类事件的 qty*avg_cost 变动视作当期现金流。
    valuations: list[dict] = []
    cashflows: list[dict] = []
    cumulative = 0.0
    for ev in events:
        date = str(ev.get("ts") or "")[:10]
        if not date:
            continue
        op = ev.get("op")
        # 任何会改变持仓的事件都要重算累计成本 —— 不能只看事件自带 qty/avg_cost：
        # delete / undo 事件本身不带这两个字段，漏掉就会在清仓后继续沿用旧成本，
        # 估值序列永远停在建仓那天（TWR/MWR 全部错）。
        if op in ("upsert", "delete", "corp_action", "undo"):
            # replay_positions 返回 {symbol: {qty, avg_cost, ...}}；
            # 直接迭代 dict 得到的是 symbol 字符串，必须取 .values()。
            cumulative = sum(
                float(row.get("qty") or 0.0) * float(row.get("avg_cost") or 0.0)
                for row in _replay_up_to(events, ev["seq"]).values()
                if isinstance(row, dict)
            )
        elif op == "cash" and isinstance(ev.get("amount"), (int, float)):
            cashflows.append({"date": date, "amount": float(ev["amount"])})
        valuations.append({"date": date, "equity": max(cumulative, 0.0)})
    perf = holding_ledger.twr_mwr(valuations=valuations, cashflows=cashflows)
    perf.setdefault("limitations", []).append(
        "估值序列按累计投入成本构造、非真实市值：TWR/MWR 仅供参考，"
        "真实时间加权收益需接入每日市值流水"
    )
    return {
        "event_count": len(events),
        "positions": snapshot,
        "version_history": history,
        "twr_mwr": perf,
        "limitations": list(getattr(events, "limitations", []) or []),
    }


def _replay_up_to(events, seq: int) -> dict[str, dict]:
    """按 seq 截取前缀回放，供 ledger_summary 构造累计成本。

    返回的是 ``{symbol: {qty, avg_cost, ...}}``（``holding_ledger._PositionResult``），
    调用方遍历持仓行必须取 ``.values()``；直接迭代 dict 只会拿到 symbol 字符串。
    """
    prefix = [ev for ev in events if int(ev.get("seq") or 0) <= seq]
    return holding_ledger.replay_positions(events=prefix)


@router.get("/account")
def get_account(request: Request) -> dict[str, Any]:
    return manual_holdings.load_account(_data_dir(request))


@router.put("/account")
def put_account(req: AccountModel, request: Request) -> dict[str, Any]:
    """设置账户现金。它是权重分母的一部分（权益 = 现金 + 持仓市值）。"""
    return manual_holdings.save_account(_data_dir(request), cash=req.cash)


@router.put("/{symbol}")
def upsert_holding(symbol: str, req: HoldingModel, request: Request) -> dict[str, Any]:
    """录入或修改一条持仓（upsert）。"""
    resolved = _resolve(request, symbol)
    sym = resolved.symbol or symbol.strip().upper()
    payload = {
        "symbol": sym,
        "asset_type": resolved.asset_type or req.asset_type or "stock",
        "name": req.name or resolved.name or "",
        "qty": req.qty,
        "avg_cost": req.avg_cost,
        "entry_date": req.entry_date,
        "note": req.note,
        "cost_basis": req.cost_basis,
    }
    data_dir = _data_dir(request)
    try:
        saved = manual_holdings.save_one(data_dir, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # P1-6：只追加台账（影子写入）。落盘成功但台账写失败不能让 PUT 变成 500，
    # 否则用户会重复提交；仅记日志并把 recorded=False 随响应带回。
    ledger = holding_ledger.append_event(
        data_dir=data_dir,
        event={"op": "upsert", "symbol": sym, **payload},
    )
    return {"ok": True, "holding": saved, "ledger_recorded": bool(ledger.get("recorded", True))}


@router.delete("/{symbol}")
def delete_holding(symbol: str, request: Request) -> dict[str, Any]:
    resolved = _resolve(request, symbol)
    sym = resolved.symbol or symbol.strip().upper()
    data_dir = _data_dir(request)
    deleted = manual_holdings.delete_one(data_dir, sym)
    if not deleted:
        raise HTTPException(404, f"没有「{symbol}」的手工持仓记录")
    ledger = holding_ledger.append_event(
        data_dir=data_dir,
        event={"op": "delete", "symbol": sym},
    )
    return {"ok": True, "symbol": sym, "ledger_recorded": bool(ledger.get("recorded", True))}
