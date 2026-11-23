"""信号风险画像与组合风险 API — `/api/risk`。

只读接口：信号统计读本地 enriched 历史，组合风险读本地行业表与日收益，
不发起网络请求、不写任何数据、不修改任何策略或持仓。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import manual_holdings, portfolio_positions, portfolio_risk, signal_risk
from app.strategy import custom_signals

router = APIRouter(prefix="/api/risk", tags=["risk"])

#: 持有周期回退值：查询参数没给、信号定义也没声明时用它。
DEFAULT_HORIZON_DAYS = 5

_SIGNAL_HORIZON_QUERY = Query(
    None, ge=1, le=60, description="前瞻收益的交易日数（也是默认持有周期）；省略则用信号自身声明，再退 5 天"
)
_LOOKBACK_QUERY = Query(
    250, ge=20, le=500, description="用于统计的信号历史交易日数"
)
_BUDGET_QUERY = Query(
    portfolio_risk.DEFAULT_DRAWDOWN_BUDGET_PCT, ge=1.0, le=60.0, description="组合回撤预算(%)"
)
_CORR_LOOKBACK_QUERY = Query(
    60, ge=portfolio_risk.MIN_OVERLAP_DAYS, le=250, description="相关性用的日收益天数"
)


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


class HoldingModel(BaseModel):
    symbol: str
    weight: float = Field(description="组合权重（百分比或小数均可，内部会归一化）")
    drawdown_pct: float | None = Field(
        default=None, description="潜在回撤幅度（正数表示百分比，如 0.05 = 5% 或 5）"
    )


class PortfolioRequest(BaseModel):
    holdings: list[HoldingModel]
    budget_pct: float | None = None
    lookback_days: int | None = None


class BuySimulationRequest(BaseModel):
    """买入前仓位模拟入参。

    基准持仓三选一：``account=``（模拟盘账户）、手工录入的真实持仓（``source=manual``
    或 ``auto`` 且已录入）、``holdings=``（调用方自报权重，返回值会标 ``basis``）。
    """

    symbol: str = Field(description="要买入的标的代码")
    new_weight_pct: float = Field(description="买入后该票占账户权益的百分比（不是占现有持仓）")
    account: str | None = Field(default=None, description="模拟盘账户；与 holdings 二选一")
    source: str | None = Field(
        default=None,
        description="持仓来源：auto（默认，有手工持仓就用手工）/ manual / paper",
    )
    holdings: list[HoldingModel] | None = Field(default=None, description="调用方自报的现有持仓")
    equity: float | None = Field(default=None, description="账户权益；holdings 口径时必须给")
    budget_pct: float | None = None
    lookback_days: int | None = None


def _normalize_drawdown(raw: float | None) -> float | None:
    """把 5 / 0.05 统一成 0.05 这样的小数比例。

    用户填「5」（表示 5%）和填「0.05」都可能；<=1 视为比例、>1 视为百分数。
    """
    if raw is None:
        return None
    value = abs(float(raw))
    return value / 100.0 if value > 1.0 else value


def _declared_horizon(sig: dict, *, explicit: int | None = None) -> int:
    """持有周期优先级：调用方显式指定 > 信号自身声明 > 默认 5 天。

    任何一步都不是合法正整数时继续往后退 —— 统计口径必须是一个确定的整数，
    不能因为定义里写了个脏值就整条算不出来。
    """
    for candidate in (explicit, sig.get("horizonDays"), DEFAULT_HORIZON_DAYS):
        if candidate is None or candidate == "":
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 250:
            return value
    return DEFAULT_HORIZON_DAYS


def _declared_cap(sig: dict) -> float | None:
    """信号里声明的仓位上限（%）；非法值按未声明处理。"""
    raw = sig.get("positionCapPct")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.0 < value <= 100.0 else None


@router.get("/signal/{signal_id}")
def get_signal_risk(
    signal_id: str,
    request: Request,
    horizon_days: int = _SIGNAL_HORIZON_QUERY,
    lookback_days: int = _LOOKBACK_QUERY,
    position_cap_pct: float | None = Query(
        None, ge=0.5, le=100.0, description="用户显式声明的仓位上限(%)；未填则由风险预算推导"
    ),
) -> dict:
    """单条信号的风险画像：理由、反证条件、持有周期、流动性、潜在回撤、仓位上限，
    以及**分开的**信号分数与胜率（含样本量和 Wilson 置信区间）。
    """
    data_dir = _data_dir(request)
    defined = {s.get("id"): s for s in custom_signals.load_all(data_dir)}
    sig = defined.get(signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail=f"未知信号: {signal_id}")
    column = custom_signals.resolve_column(sig)

    # 优先级：查询参数 > 信号定义里作者填的 > 默认 5 个交易日。
    declared_horizon = sig.get("horizonDays")
    horizon = _declared_horizon(sig, explicit=horizon_days)
    declared_cap = position_cap_pct if position_cap_pct is not None else _declared_cap(sig)

    history = signal_risk.load_enriched_history(data_dir, lookback_days=lookback_days)
    if history.is_empty():
        raise HTTPException(
            status_code=409,
            detail="本地 enriched 历史不可用（目录缺失或分区为空），无法计算信号统计",
        )

    stats = signal_risk.compute_signal_stats(
        signal_id=signal_id,
        column=column,
        history=history,
        horizon_days=horizon,
        reason=str(sig.get("reason") or ""),
        falsify=str(sig.get("falsify") or ""),
        declared_cap=declared_cap,
    )
    return {
        **stats.to_dict(),
        "name": sig.get("name"),
        "kind": sig.get("kind"),
        "timeframe": sig.get("timeframe", "daily"),
        "enabled": sig.get("enabled", True),
        # 作者声明 vs 实际采用，两者都可能不同（查询参数可覆盖），显式列出避免误读。
        "declared": {
            "horizon_days": declared_horizon,
            "position_cap_pct": sig.get("positionCapPct"),
        },
    }


@router.get("/signals")
def list_signal_risk(
    request: Request,
    horizon_days: int = _SIGNAL_HORIZON_QUERY,
    lookback_days: int = _LOOKBACK_QUERY,
) -> dict:
    """全部已启用信号的统计（一次读历史帧，避免逐信号重复读盘）。"""
    data_dir = _data_dir(request)
    history = signal_risk.load_enriched_history(data_dir, lookback_days=lookback_days)
    limits: list[str] = []
    if history.is_empty():
        limits.append("本地 enriched 历史不可用，全部信号只能给出空统计")
    rows: list[dict] = []
    for sig in custom_signals.load_all(data_dir):
        if sig.get("enabled") is False:
            continue
        # 每条信号按自己声明的持有周期算；没声明就用默认值。
        # 列表接口没有逐信号的查询参数，所以这里不能直接把 None 传下去。
        per_sig_horizon = _declared_horizon(sig, explicit=horizon_days)
        stats = signal_risk.compute_signal_stats(
            signal_id=str(sig.get("id")),
            column=custom_signals.resolve_column(sig),
            history=history,
            horizon_days=per_sig_horizon,
            reason=str(sig.get("reason") or ""),
            falsify=str(sig.get("falsify") or ""),
            declared_cap=_declared_cap(sig),
        )
        rows.append({**stats.to_dict(), "name": sig.get("name"), "kind": sig.get("kind")})
    rows.sort(key=lambda r: -r["signal_score"]["value"])
    return {
        "count": len(rows),
        "horizon_days": horizon_days or DEFAULT_HORIZON_DAYS,
        "lookback_days": lookback_days,
        "signals": rows,
        "limitations": limits,
        "note": "signal_score 是排序指标不是概率；win_rate 是历史频率，需配合 samples 与 confidence_interval 读",
    }


@router.get("/positions")
def list_actual_positions(
    request: Request,
    account: str | None = Query(
        None, description="模拟盘账户；省略则取权益最大的账户（返回值里说明选了哪个）"
    ),
    source: str = Query(
        "auto",
        description="持仓来源：auto（默认，有手工持仓就用手工）/ manual（手工录入）/ paper（模拟盘）",
    ),
) -> dict:
    """**实际持仓**：真实权重（市值/账户权益）、成本、持仓期限、浮动盈亏。

    与"自选股观察"分属两个口径：这里的数字来自模拟盘账户（``basis=actual_positions``）
    或用户手工录入（``basis=manual_positions``），都可以直接喂给组合风险模型；
    自选清单没有权重与成本，用 ``GET /api/risk/portfolio?symbols=...`` 时会在
    返回值里标明 ``basis="watchlist_observation"`` 并附提醒。
    """
    return portfolio_positions.load_actual_positions(
        _data_dir(request), account_id=account, source=source
    )


@router.get("/portfolio")
def get_portfolio_risk(
    request: Request,
    symbols: str | None = Query(None, description="逗号分隔的标的（观察口径）；与持仓口径互斥"),
    weights: str | None = Query(None, description="逗号分隔的权重；省略则等权（观察口径）"),
    drawdowns: str | None = Query(None, description="逗号分隔的潜在回撤(%)；省略则该笔不参与预算"),
    account: str | None = Query(None, description="用某个模拟盘账户的真实持仓做组合分析"),
    source: str = Query(
        "auto",
        description="持仓来源：auto（默认，有手工持仓就用手工）/ manual（手工录入）/ paper（模拟盘）",
    ),
    budget_pct: float = _BUDGET_QUERY,
    lookback_days: int = _CORR_LOOKBACK_QUERY,
) -> dict:
    """组合层风险。

    两种口径严格分开（用户明确要求：「不应把自选清单当作真实组合」）：

    - 持仓口径（``account=`` 或手工录入）→ **实际持仓分析**：权重 = 持仓市值 /
      权益，含成本与持仓期限；``basis`` 为 ``actual_positions``（模拟盘）或
      ``manual_positions``（手工录入）。
    - ``symbols=`` → **观察口径**：等权或调用方自报权重，返回值
      ``basis="watchlist_observation"``（或 ``caller_supplied_weights``）
      并附 ``warning``：它不是实际持仓组合。
    """
    if symbols and account:
        raise HTTPException(status_code=400, detail="account 与 symbols 必须且只能给一个")

    manual_exists = bool(manual_holdings.load_all(_data_dir(request)))
    kind = (source or "auto").strip().lower()
    use_manual = kind == "manual" or (kind == "auto" and manual_exists and not account)

    if symbols:
        if use_manual:
            raise HTTPException(
                status_code=400,
                detail="symbols 是观察口径，与手工持仓/账户口径不能同时使用",
            )
        return _portfolio_report(request, symbols, weights, drawdowns, budget_pct, lookback_days)

    actual = portfolio_positions.load_actual_positions(
        _data_dir(request),
        account_id=account,
        source=portfolio_positions.SOURCE_MANUAL if use_manual else (
            portfolio_positions.SOURCE_PAPER if account else source
        ),
    )
    holdings = portfolio_positions.holdings_for_risk(actual)
    if not holdings:
        where = "手工录入的持仓" if actual.get("basis") == portfolio_positions.MANUAL_BASIS else (
            f"账户 {actual.get('account_id')}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"{where} 没有可用于组合分析的持仓（可能未录入、未初始化或权益为 0）",
        )
    report = portfolio_risk.build_portfolio_report(
        _data_dir(request),
        holdings=holdings,
        budget_pct=budget_pct,
        lookback_days=lookback_days,
        equity=actual.get("total"),
        cash=actual.get("cash"),
        # 账户口径：权重分母是账户权益，合计 < 1 表示有现金没投，
        # 归一化会把「半仓」算成「满仓」（用户明确要求的诚实边界）。
        normalize=False,
    )
    # 手工录入没有净值序列，nav 层面的回撤/基准对比无从计算 —— 说明清楚而不是给 0
    nav_risk = (
        {
            "nav_series_size": 0,
            "max_drawdown": None,
            "current_drawdown": None,
            "nav_limitation": "手工录入的持仓没有历史净值序列，账户级回撤与基准对比无法计算；"
            "上面的回撤是「持仓层面按日线算的」，不是账户走过的净值",
        }
        if actual.get("basis") == portfolio_positions.MANUAL_BASIS
        else _account_nav_risk(_data_dir(request), actual["account_id"])
    )
    report.update({
        "basis": actual["basis"],
        "basis_label": actual["basis_label"],
        # 估值来源: live=盘中快照 / daily=最近日线收盘 / cost=按成本(盈亏不可信)
        "price_source": actual.get("price_source"),
        "account_id": actual["account_id"],
        "account_name": actual.get("account_name"),
        "weight_basis": actual["weight_basis"],
        "positions_fed": len(holdings),
        "account_equity": actual.get("total"),
        "account_cash": actual.get("cash"),
        "account_market_value": actual.get("market_value"),
        "total_pnl": actual.get("total_pnl"),
        **nav_risk,
        "limitations": [*(report.get("limitations") or []), *actual["limitations"]],
    })
    return report


def _account_nav_risk(data_dir, account_id: str) -> dict:
    """账户净值层面的回撤与基准对比（只用已定版的 nav 序列）。

    ``max_drawdown`` / ``current_drawdown`` 是**账户真实走过的**峰谷跌幅，
    和 ``portfolio_risk.drawdown_budget`` 的"压力假设"不是一回事：前者是历史，
    后者是配置约束。两个都返回，但字段名分开，不混读。

    基准用沪深300（``_index_close`` 写进 nav 行的 ``benchmark_close``）。
    没有净值序列时如实给 None，不拿"0% 回撤"冒充"没亏过"。
    """
    from app.strategy import paper

    try:
        lines = paper.load_nav(data_dir, account_id)
    except Exception as exc:  # noqa: BLE001 - 净值读失败不该拖垮整个组合报告
        return {"nav_series_size": 0, "nav_risk_limitation": f"净值序列读取失败: {exc}"}

    if not lines:
        return {
            "nav_series_size": 0,
            "max_drawdown": None,
            "current_drawdown": None,
            "nav_limitation": "账户还没有定版净值（未结算过），组合回撤与基准对比无法计算",
        }

    navs = [float(n.get("nav") or 0.0) for n in lines if n.get("nav")]
    if not navs:
        return {
            "nav_series_size": len(lines),
            "max_drawdown": None,
            "current_drawdown": None,
            "nav_limitation": "净值行里没有可用 nav 值",
        }

    peak = navs[0]
    worst = 0.0
    for value in navs:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    current_dd = ((peak - navs[-1]) / peak) if peak > 0 else None

    # 基准对比：只用 nav 行里真有的 benchmark_close，缺就整块不给。
    bench_pairs = [
        (float(n["nav"]), float(n["benchmark_close"]))
        for n in lines
        if n.get("nav") and n.get("benchmark_close")
    ]
    benchmark = None
    if len(bench_pairs) >= 2:
        first_nav, first_bench = bench_pairs[0]
        last_nav, last_bench = bench_pairs[-1]
        if first_nav > 0 and first_bench > 0:
            account_ret = last_nav / first_nav - 1.0
            bench_ret = last_bench / first_bench - 1.0
            benchmark = {
                "index": "000300.SH",
                "days": len(bench_pairs),
                "account_return_pct": round(account_ret * 100, 2),
                "benchmark_return_pct": round(bench_ret * 100, 2),
                "excess_pct": round((account_ret - bench_ret) * 100, 2),
            }

    return {
        "nav_series_size": len(navs),
        "nav_from": lines[0].get("date"),
        "nav_to": lines[-1].get("date"),
        "max_drawdown": round(worst * 100, 2),
        "current_drawdown": None if current_dd is None else round(current_dd * 100, 2),
        "benchmark": benchmark,
    }


@router.post("/buy-simulation")
def post_buy_simulation(req: BuySimulationRequest, request: Request) -> dict:
    """买入前仓位模拟：加这只票之后集中度/回撤预算变成什么样。

    基准持仓的取法：``account=`` → 模拟盘账户；``source=manual`` 或（``auto``
    且有手工持仓）→ 你手工录入的真实持仓；``holdings=`` → 调用方自报权重
    （返回值会标明 ``basis``，不当成已验证的组合）。
    """
    data_dir = _data_dir(request)
    budget = req.budget_pct if req.budget_pct is not None else portfolio_risk.DEFAULT_DRAWDOWN_BUDGET_PCT
    lookback = req.lookback_days or 60
    if not 1.0 <= budget <= 60.0:
        raise HTTPException(status_code=400, detail="budget_pct 需在 1~60 之间")
    if not 0.0 < req.new_weight_pct <= 100.0:
        raise HTTPException(status_code=400, detail="new_weight_pct 需在 (0, 100] 之间")

    manual_exists = bool(manual_holdings.load_all(data_dir))
    kind = (req.source or portfolio_positions.SOURCE_AUTO).strip().lower()
    from_manual = kind == portfolio_positions.SOURCE_MANUAL or (
        kind == portfolio_positions.SOURCE_AUTO and manual_exists and not req.account
    )
    use_store = bool(req.account) or (from_manual and not req.holdings)

    if use_store:
        actual = portfolio_positions.load_actual_positions(
            data_dir,
            account_id=req.account,
            source=portfolio_positions.SOURCE_MANUAL if from_manual else (
                portfolio_positions.SOURCE_PAPER if req.account else kind
            ),
        )
        if not actual.get("initialized"):
            who = "你的手工持仓" if from_manual else f"账户 {req.account}"
            raise HTTPException(status_code=400, detail=f"{who} 还没有持仓，无法做买入模拟")
        existing = portfolio_positions.holdings_for_risk(actual)
        if not existing:
            raise HTTPException(status_code=400, detail="当前没有可用于模拟的持仓（权益为 0 或权重缺失）")
        equity = float(actual.get("total") or 0.0)
        basis = {
            "basis": actual["basis"],
            "basis_label": actual["basis_label"],
            "price_source": actual.get("price_source"),
            "account_id": actual["account_id"],
            "weight_basis": actual["weight_basis"],
            "extra_limitations": actual["limitations"],
        }
    else:
        existing = [{"symbol": h.symbol, "weight": h.weight} for h in (req.holdings or [])]
        equity = float(req.equity or 0.0)
        obs = portfolio_positions.observation([h["symbol"] for h in existing])
        basis = {
            "basis": obs["basis"],
            "basis_label": obs["basis_label"],
            "weight_basis": obs["weight_basis"],
            "warning": obs["warning"],
            "extra_limitations": obs["limitations"],
        }

    if equity <= 0:
        raise HTTPException(
            status_code=400,
            detail="缺少账户权益，无法把权重换算成金额；请传 account= 或 equity=",
        )

    report = portfolio_risk.simulate_buy(
        data_dir,
        existing=existing,
        new_symbol=req.symbol,
        new_weight_pct=req.new_weight_pct,
        equity=equity,
        budget_pct=budget,
        lookback_days=lookback,
    )
    report.update(basis)
    report.setdefault("price_source", basis.get("price_source"))
    report["limitations"] = list(dict.fromkeys([
        *(report.get("limitations") or []),
        *basis.pop("extra_limitations", []),
    ]))
    return report


@router.post("/portfolio")
def post_portfolio_risk(req: PortfolioRequest, request: Request) -> dict:
    """组合层风险（POST 形式，适合前端传结构化持仓）。

    权重由调用方提供，**系统无法核对它是否等于真实持仓**，所以返回值带
    ``basis="caller_supplied_weights"`` 与提醒；要"实际持仓分析"请用
    ``GET /api/risk/portfolio?account=...``。
    """
    budget = req.budget_pct if req.budget_pct is not None else portfolio_risk.DEFAULT_DRAWDOWN_BUDGET_PCT
    lookback = req.lookback_days or 60
    if not 1.0 <= budget <= 60.0:
        raise HTTPException(status_code=400, detail="budget_pct 需在 1~60 之间")
    holdings = [
        {
            "symbol": h.symbol,
            "weight": h.weight,
            "drawdown_pct": _normalize_drawdown(h.drawdown_pct),
        }
        for h in req.holdings
    ]
    report = portfolio_risk.build_portfolio_report(
        _data_dir(request), holdings=holdings, budget_pct=budget, lookback_days=lookback
    )
    basis = portfolio_positions.observation([h["symbol"] for h in holdings], weights=[h["weight"] for h in holdings])
    report.update({
        "basis": basis["basis"],
        "basis_label": basis["basis_label"],
        "weight_basis": basis["weight_basis"],
        "warning": basis["warning"],
        "limitations": [*(report.get("limitations") or []), *basis["limitations"]],
    })
    return report


def _portfolio_report(
    request: Request,
    symbols: str,
    weights: str | None,
    drawdowns: str | None,
    budget_pct: float,
    lookback_days: int,
) -> dict:
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not syms:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    weight_values: list[float] | None = None
    if weights:
        try:
            weight_values = [float(w) for w in weights.split(",") if w.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="weights 必须是逗号分隔的数字") from None
        if len(weight_values) != len(syms):
            raise HTTPException(
                status_code=400,
                detail=f"weights 数量 {len(weight_values)} 与 symbols 数量 {len(syms)} 不一致",
            )
    drawdown_values: list[float | None] = [None] * len(syms)
    if drawdowns:
        parts = [d.strip() for d in drawdowns.split(",")]
        if len(parts) != len(syms):
            raise HTTPException(
                status_code=400,
                detail=f"drawdowns 数量 {len(parts)} 与 symbols 数量 {len(syms)} 不一致",
            )
        for i, part in enumerate(parts):
            drawdown_values[i] = _normalize_drawdown(float(part)) if part else None

    holdings = [
        {
            "symbol": sym,
            "weight": weight_values[i] if weight_values else 1.0,
            "drawdown_pct": drawdown_values[i],
        }
        for i, sym in enumerate(syms)
    ]
    report = portfolio_risk.build_portfolio_report(
        _data_dir(request),
        holdings=holdings,
        budget_pct=budget_pct,
        lookback_days=lookback_days,
    )
    # 观察口径：这些符号可能只是自选清单，不是真实持仓 —— 必须在返回值里说清楚。
    basis = portfolio_positions.observation(syms, weights=weight_values)
    report.update({
        "basis": basis["basis"],
        "basis_label": basis["basis_label"],
        "is_portfolio": basis["is_portfolio"],
        "weight_basis": basis["weight_basis"],
        "warning": basis["warning"],
        "limitations": [*(report.get("limitations") or []), *basis["limitations"]],
    })
    return report
