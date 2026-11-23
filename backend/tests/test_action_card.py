"""行动卡与持仓计划 —— 口径分离与三段式契约。

用户 2026-11-13 的两条要求:
1. 自选观察 / 计划买入 / 实际持仓必须分开, **计划买入不参与盈亏计算**;
2. 每只票一张行动卡, 回答四个问题, 并把
   **事实 / 系统推断 / 用户计划** 三段明显区分 (不许只给一个买入/卖出分数)。

这组测试锁住这两条, 防止后续改动把三段合并或让计划混进会计。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from app.services import action_card, position_plan
from app.services import stock_diagnostics as diag

# ── 造数据 ──────────────────────────────────────────────

def make_bars(
    n: int = 320,
    *,
    start_price: float = 10.0,
    drift: float = 0.0,
    vol: float = 0.02,
    seed: int = 7,
    end: str = "2026-11-13",
) -> pl.DataFrame:
    """造一段可控的日线 —— 让趋势/波动可预测, 便于断言。"""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start_price * np.cumprod(1 + rets)
    high = close * (1 + rng.uniform(0.002, 0.02, n))
    low = close * (1 - rng.uniform(0.002, 0.02, n))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.integers(80_000, 200_000, n).astype(float)
    end_date = dt.date.fromisoformat(end)
    dates = sorted(
        (end_date - dt.timedelta(days=i) for i in range(n * 2)),
        reverse=False,
    )[-n:]
    return pl.DataFrame({
        "symbol": ["TEST.SH"] * n,
        "date": dates,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume,
    }).with_columns(pl.col("date").cast(pl.Utf8))


@pytest.fixture()
def bars() -> pl.DataFrame:
    return make_bars()


@pytest.fixture()
def uptrend_bars() -> pl.DataFrame:
    return make_bars(drift=0.004, vol=0.012, seed=11)


@pytest.fixture()
def downtrend_bars() -> pl.DataFrame:
    return make_bars(drift=-0.005, vol=0.02, seed=13)


# ── 1. 三态互斥 ─────────────────────────────────────────

def test_holding_takes_precedence_over_plan():
    """同一标的既在持仓又填了计划时, 状态必须是「实际持仓」。

    已成交的计划不再是计划 —— 否则会出现"同一只票既算持仓又算计划"的双重身份。
    """
    state = position_plan.resolve_state(
        "600941.SH",
        holding_symbols={"600941.SH"},
        planned_symbols={"600941.SH"},
    )
    assert state == position_plan.STATE_HOLDING


def test_plan_without_holding_is_planning_not_holding():
    state = position_plan.resolve_state(
        "605366.SH", holding_symbols=set(), planned_symbols={"605366.SH"}
    )
    assert state == position_plan.STATE_PLANNING
    assert state != position_plan.STATE_HOLDING


def test_plain_watchlist_symbol_is_watch():
    state = position_plan.resolve_state(
        "000001.SZ", holding_symbols=set(), planned_symbols=set()
    )
    assert state == position_plan.STATE_WATCH


def test_only_holding_counts_toward_pnl():
    """counts_toward_pnl 是防「把提示误认为已持仓」的总闸门。"""
    rows = position_plan.classify_all(
        ["600941.SH", "605366.SH", "000001.SZ"],
        holding_symbols={"600941.SH"},
        plans={"605366.SH": {"symbol": "605366.SH"}},
    )
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["600941.SH"]["counts_toward_pnl"] is True
    assert by_symbol["605366.SH"]["counts_toward_pnl"] is False
    assert by_symbol["000001.SZ"]["counts_toward_pnl"] is False
    # 状态标签必须可区分
    assert len({r["state_label"] for r in rows}) == 3


# ── 2. 计划校验 ─────────────────────────────────────────

def test_plan_validation_rejects_bad_hold_days():
    with pytest.raises(ValueError, match="持有期"):
        position_plan.validate_plan({"symbol": "A.SH", "planned_hold_days": 0})
    with pytest.raises(ValueError, match="持有期"):
        position_plan.validate_plan({"symbol": "A.SH", "planned_hold_days": 99_999})


def test_plan_validation_rejects_bad_loss_pct():
    with pytest.raises(ValueError, match="可接受亏损"):
        position_plan.validate_plan({"symbol": "A.SH", "acceptable_loss_pct": 0})
    with pytest.raises(ValueError, match="可接受亏损"):
        position_plan.validate_plan({"symbol": "A.SH", "acceptable_loss_pct": 150})


def test_plan_validation_rejects_non_positive_batch_price():
    with pytest.raises(ValueError, match="计划价格"):
        position_plan.validate_plan({
            "symbol": "A.SH",
            "batches": [{"price": 0, "amount": 1000}],
        })


def test_plan_roundtrip_and_delete(tmp_path):
    saved = position_plan.save_one(tmp_path, {
        "symbol": "605366.sh",           # 小写应被归一成大写
        "planned_hold_days": 30,
        "acceptable_loss_pct": 8,
        "batches": [
            {"price": 7.0, "amount": 30000, "label": "第一档"},
            {"price": 6.6, "amount": 20000, "label": "第二档"},
        ],
        "note": "等回调",
    })
    assert saved["symbol"] == "605366.SH"

    loaded = position_plan.load_one(tmp_path, "605366.SH")
    assert loaded is not None
    assert loaded["planned_hold_days"] == 30
    assert len(loaded["batches"]) == 2

    assert position_plan.delete_one(tmp_path, "605366.SH") is True
    assert position_plan.load_one(tmp_path, "605366.SH") is None


# ── 3. 计划兑现度: 只陈述事实 ───────────────────────────

def test_plan_progress_separates_executed_and_remaining():
    plan = {
        "symbol": "605366.SH",
        "batches": [
            {"price": 7.0, "amount": 30_000, "done": True},
            {"price": 6.5, "amount": 20_000, "done": False},
        ],
        "acceptable_loss_pct": 8,
        "planned_hold_days": 30,
    }
    prog = position_plan.assess_plan(plan, current_price=7.17)
    data = prog.to_dict()
    assert data["planned_amount"] == pytest.approx(50_000)
    assert data["executed_amount"] == pytest.approx(30_000)
    assert data["remaining_amount"] == pytest.approx(20_000)
    assert data["done_batches"] == 1
    # 未执行且低于现价的那一批要能被识别出来
    assert data["batches_below_current"] == 1


def test_plan_missing_risk_fields_are_warnings_not_defaults():
    """缺可接受亏损/持有期时必须报警, 不能编个默认值糊过去。"""
    prog = position_plan.assess_plan({"symbol": "X.SH", "batches": []}, current_price=10.0)
    data = prog.to_dict()
    joined = " ".join(data["warnings"])
    assert "可接受亏损" in joined
    assert "计划持有期" in joined


def test_plan_without_batches_is_not_a_plan():
    prog = position_plan.assess_plan({"symbol": "X.SH", "batches": []}, current_price=10.0)
    assert any("不是买入计划" in w for w in prog.warnings)


# ── 4. 行动卡三段式 ─────────────────────────────────────

def test_action_card_has_three_distinct_sections(bars):
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")
    # 三段都在, 且各有标签
    assert set(card["sections"]) == {"facts", "inference", "user_plan"}
    for label in card["sections"].values():
        assert label  # 每段都必须有人话标签
    assert card["facts"]["price"] > 0
    assert card["user_plan"]["counts_toward_pnl"] is False


def test_action_card_never_returns_a_buy_sell_score(bars):
    """核心禁令: 不许把三段合并成一个买入/卖出分数。"""
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")
    flat = str(card).lower()
    for banned in ("buy_score", "sell_score", "recommendation", "rating", "signal_score"):
        assert banned not in flat
    # strength 只能是三档之一, 不是连续分数
    assert card["strength"]["stance"] in ("strong", "weak", "unclear")


def test_action_card_answers_four_questions(bars):
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")
    assert card["strength"]["label"] in ("偏强", "偏弱", "方向不明")
    assert isinstance(card["checks"], list) and card["checks"]
    # 无持仓时 invalidation 可以空 (没有"原判断"可言), 但 entry_conditions 必须给
    assert isinstance(card["entry_conditions"], list) and card["entry_conditions"]


def test_action_card_watch_state_says_no_pnl(bars):
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")
    assert card["state"] == position_plan.STATE_WATCH
    assert any("不计盈亏" in c or "没有持仓" in c for c in card["caveats"])


def test_action_card_planning_state_warns_not_holding(bars):
    card = action_card.build_action_card(
        df=bars, symbol="TEST.SH",
        plan={"symbol": "TEST.SH", "batches": [{"price": 9.0, "amount": 10000}]},
    )
    assert card["state"] == position_plan.STATE_PLANNING
    assert any("计划买入" in c and "不参与" in c for c in card["caveats"])


def test_action_card_holding_state_includes_position_facts(bars):
    holding = {
        "symbol": "TEST.SH", "qty": 1000, "avg_cost": 9.5,
        "market_value": 9500, "weight": 0.1, "pnl": 500, "pnl_pct": 5.3,
        "entry_date": "2026-10-01", "holding_days": 43,
    }
    card = action_card.build_action_card(df=bars, symbol="TEST.SH", holding=holding)
    assert card["state"] == position_plan.STATE_HOLDING
    assert card["user_plan"]["counts_toward_pnl"] is True
    assert card["facts"]["holding"]["avg_cost"] == 9.5


# ── 5. 失效条件必须带价位 ───────────────────────────────

def test_invalidation_from_user_loss_tolerance_has_price(bars):
    """用户设的亏损容忍是唯一"必须遵守"的纪律线, 必须换算成具体价位。"""
    diagnosis = diag.diagnose(bars)
    holding = {"symbol": "TEST.SH", "avg_cost": 10.0, "qty": 100, "market_value": 1000}
    rules = action_card.build_invalidation(diagnosis, holding, {"acceptable_loss_pct": 8})
    priced = [r for r in rules if r.price is not None]
    assert priced, "必须给出带价位的失效条件"
    stop = [r for r in priced if r.price == pytest.approx(9.2)]
    assert stop, f"成本 10.0 × (1-8%) 应得 9.20, 实际: {[r.price for r in priced]}"
    assert stop[0].severity == "critical"

    # 反向: 没设亏损容忍时必须警告, 而不是静默给个默认值
    rules_no_loss = action_card.build_invalidation(diagnosis, holding, None)
    assert any(r.price is None and "可接受亏损" in r.condition for r in rules_no_loss)


def test_entry_conditions_get_worse_guidance_in_downtrend(downtrend_bars):
    """下降趋势里不能给"回踩均线买"这种条件, 只给放量止跌。"""
    diagnosis = diag.diagnose(downtrend_bars)
    for cond in action_card.build_entry_conditions(diagnosis):
        if cond.kind == "pullback":
            # 允许出现(若结构判成上升), 但下降趋势里不该有
            assert diagnosis["trend"]["structure"] != "downtrend"


# ── 6. 诊断算法的关键不变量 ─────────────────────────────

def test_diagnose_rejects_insufficient_bars():
    with pytest.raises(ValueError, match="不足以诊断"):
        diag.diagnose(make_bars(30))


def test_analog_stats_match_today_direction():
    """同形态统计必须按当日实际涨跌方向匹配 —— 写死"上涨"会把结论反过来。"""
    down = make_bars(drift=-0.003, vol=0.02, seed=21)
    # 强制最后一天收跌
    close = down["close"].to_numpy().copy()
    close[-1] = close[-2] * 0.98
    down = down.with_columns(pl.Series("close", close))
    res = diag.analog_stats(down)
    assert res["today_is_up"] is False
    assert "前一日涨 + 当日跌" in res["pattern"]


def test_analog_stats_always_reports_baseline_and_warning(bars):
    """没有无条件基准的胜率是骗人的; 样本不足必须显式警告。"""
    res = diag.analog_stats(bars)
    assert res["baseline_stats"], "必须给无条件基准"
    assert res["warnings"], "必须给样本量或口径警告"


def test_trend_structure_classifies_uptrend(uptrend_bars):
    diagnosis = diag.diagnose(uptrend_bars)
    assert diagnosis["trend"]["structure"] in ("uptrend", "uptrend_pullback")


def test_strength_verdict_is_evidence_not_score(uptrend_bars):
    diagnosis = diag.diagnose(uptrend_bars)
    st = diagnosis["strength"]
    assert st["stance"] in ("strong", "weak", "unclear")
    # 每条证据都必须是非空字符串 (可读的理由, 不是数字)
    for key in ("bullish_evidence", "bearish_evidence", "neutral_evidence"):
        for item in st[key]:
            assert isinstance(item, str) and len(item) > 4


def test_chips_densest_above_counts_total_overhead():
    """上方筹码必须给"该方向总量" —— 只给单档会低估要消化的套牢盘。"""
    bars = make_bars(seed=31)
    res = diag.chips(bars)
    if res["densest_above"]:
        assert "total_pct" in res["densest_above"]
        assert res["densest_above"]["total_pct"] >= res["densest_above"]["pct"]


# ── 7. 财报/公告自动解析 ────────────────────────────────

def test_fundamental_digest_is_pit_safe(tmp_path):
    """财报必须按公告日过滤 —— 用报告期会前视。空目录要如实报不可用, 不编造。"""
    import datetime as dt

    from app.services import fundamentals_digest as fd

    digest = fd.build_fundamental_digest(tmp_path, "NODATA.SH", as_of=dt.date(2026, 11, 13))
    assert digest.available is False
    assert digest.limitations
    checks = fd.checks_from_digest(digest)
    assert any(c["status"] == "unavailable" for c in checks)


def test_checks_include_unknown_items_when_no_digest():
    """没有 digest 时退回「去哪查」, 但必须标 unverified, 不能假装查过。"""
    bars = make_bars()
    diagnosis = diag.diagnose(bars)
    checks = action_card.build_checks(diagnosis, digest=None)
    statuses = {c.status for c in checks}
    assert "unverified" in statuses
    items = " ".join(c.item for c in checks)
    assert "行业" in items
    assert "未公告" in items


# ── 8. 规则有效性检验 ───────────────────────────────────

def _trending_bars(n=320, slope=0.003, seed=5):
    return make_bars(n=n, drift=slope, vol=0.015, seed=seed)


def test_strength_validation_reports_insufficient_for_short_history():
    from app.services import strength_validation as sv

    short = make_bars(100)
    rep = sv.validate_strength_rules(short, symbol="T.SH")
    assert rep.sampled_days == 0
    assert rep.limitations
    # 不能把「样本不足」报成「无效」
    assert any("不足以" in x for x in rep.limitations)


def test_strength_validation_produces_verdicts():
    from app.services import strength_validation as sv

    bars = _trending_bars()
    rep = sv.validate_strength_rules(bars, symbol="T.SH", step=5)
    assert rep.sampled_days > 0
    assert len(rep.groups) == 3
    for g in rep.groups:
        assert g.verdict in ("validated", "weak_signal", "inverted", "no_edge", "insufficient")
    assert rep.conclusions


def test_inverted_is_not_reported_as_no_edge():
    """「一致地反向」必须单独标记 —— 报成 no_edge 会掩盖更强的结论。"""
    from app.services import strength_validation as sv

    rep = sv.ValidationReport(symbol="T.SH")
    rep.baseline = {str(w): {"win_rate": 50.0} for w in sv.FORWARD_WINDOWS}
    strong = sv.GroupStats(stance="strong", label="偏强", samples=100)
    strong.forward = {str(w): {"win_rate": 40.0} for w in sv.FORWARD_WINDOWS}
    weak = sv.GroupStats(stance="weak", label="偏弱", samples=100)
    weak.forward = {str(w): {"win_rate": 60.0} for w in sv.FORWARD_WINDOWS}
    rep.groups = [strong, weak]
    sv._judge_verdicts(rep)
    assert strong.verdict == "inverted"
    assert weak.verdict == "inverted"


def test_insufficient_group_never_gets_a_verdict():
    from app.services import strength_validation as sv

    rep = sv.ValidationReport(symbol="T.SH")
    rep.baseline = {str(w): {"win_rate": 50.0} for w in sv.FORWARD_WINDOWS}
    tiny = sv.GroupStats(stance="strong", label="偏强", samples=5)
    tiny.forward = {str(w): {"win_rate": 80.0} for w in sv.FORWARD_WINDOWS}
    rep.groups = [tiny]
    sv._judge_verdicts(rep)
    assert tiny.verdict == "insufficient"


# ── 9. 计划与成交自动对账 ───────────────────────────────

def test_reconcile_matches_by_price_tolerance():
    plan = {
        "symbol": "A.SH",
        "batches": [
            {"price": 7.00, "amount": 30000, "label": "一档", "done": False},
            {"price": 6.50, "amount": 20000, "label": "二档", "done": False},
        ],
    }
    fills = [
        # 偏离 0.3% 在 2% 容差内 → 应匹配
        {"symbol": "A.SH", "side": "buy", "price": 7.02, "qty": 4200, "date": "2026-11-10", "kind": "fill"},
        # 偏离 20% → 不匹配, 记为计划外
        {"symbol": "A.SH", "side": "buy", "price": 9.00, "qty": 1000, "date": "2026-11-11", "kind": "fill"},
        # 卖出不参与匹配
        {"symbol": "A.SH", "side": "sell", "price": 7.30, "qty": 500, "date": "2026-11-12", "kind": "fill"},
    ]
    rep = position_plan.reconcile_with_fills(plan, fills)
    assert rep.matched_count == 1
    assert rep.unmatched_batches == 1
    assert rep.orphan_fills == 1     # 2 笔买入 - 1 笔已匹配
    assert rep.facts
    assert rep.warnings


def test_reconcile_does_not_mutate_plan():
    """对账只读 —— 绝不自动改写计划（计划是用户的意图记录）。"""
    plan = {"symbol": "A.SH", "batches": [{"price": 7.0, "amount": 1000, "done": False}]}
    snapshot = [dict(b) for b in plan["batches"]]
    fills = [{"symbol": "A.SH", "side": "buy", "price": 7.0, "qty": 100, "date": "2026-11-10", "kind": "fill"}]
    position_plan.reconcile_with_fills(plan, fills)
    assert plan["batches"] == snapshot


def test_reconcile_respects_done_flag():
    plan = {"symbol": "A.SH", "batches": [{"price": 7.0, "amount": 1000, "done": True}]}
    rep = position_plan.reconcile_with_fills(plan, [])
    assert rep.matched_count == 1
    assert rep.unmatched_batches == 0


# ── 10. 组合级资金校验 ──────────────────────────────────

def test_capital_check_sums_remaining_not_total():
    """加总必须用「待执行」金额 —— 已成交的已花掉现金，再加一遍会重复。"""
    plans = {
        "A.SH": {"batches": [
            {"price": 10, "amount": 50000, "done": True},
            {"price": 9, "amount": 50000, "done": False},
        ]},
        "B.SH": {"batches": [{"price": 20, "amount": 30000, "done": False}]},
    }
    check = position_plan.check_aggregate_capital(plans, available_cash=100000)
    assert check.total_planned == pytest.approx(130000)
    assert check.total_executed == pytest.approx(50000)
    assert check.total_remaining == pytest.approx(80000)
    assert check.to_dict()["over_budget"] is False


def test_capital_check_flags_shortfall():
    plans = {"A.SH": {"batches": [{"price": 10, "amount": 150000, "done": False}]}}
    check = position_plan.check_aggregate_capital(plans, available_cash=100000)
    data = check.to_dict()
    assert data["over_budget"] is True
    assert data["shortfall"] == pytest.approx(50000)
    assert check.warnings


def test_capital_check_without_account_reports_but_does_not_judge():
    plans = {"A.SH": {"batches": [{"price": 10, "amount": 150000}]}}
    check = position_plan.check_aggregate_capital(plans, available_cash=None)
    assert check.total_remaining == pytest.approx(150000)
    assert check.to_dict()["over_budget"] is False
    assert any("无法判断" in w for w in check.warnings)


# ── 11. 规则检验结果必须影响置信度 ──────────────────────

def test_inverted_rule_lowers_confidence_and_warns():
    """规则被证伪时, 行动卡必须降置信度并显式警告 —— 这是诚实性的底线。"""
    bars = _trending_bars()
    diagnosis = diag.diagnose(bars)
    stance = diagnosis["strength"]["stance"]
    validation = {
        "groups": [{"stance": stance, "verdict": "inverted", "samples": 300,
                    "note": "一致地反着走"}],
        "conclusions": ["「偏强」档反向"],
    }
    inf = action_card.build_inference(diagnosis, validation=validation)
    assert inf["confidence"] == "very_low"
    assert any("反向" in c for c in inf["caveats"])
    assert inf["rule_validation"]["verdict"] == "inverted"


def test_validated_rule_keeps_confidence():
    bars = _trending_bars()
    diagnosis = diag.diagnose(bars)
    stance = diagnosis["strength"]["stance"]
    validation = {
        "groups": [{"stance": stance, "verdict": "validated", "samples": 300, "note": "有效"}],
        "conclusions": [],
    }
    inf = action_card.build_inference(diagnosis, validation=validation)
    assert inf["confidence"] != "very_low"


# ── 12. 默认卡片必须带「已知反向」披露（修复 validation 死代码） ─────

def test_default_action_card_carries_dated_market_disclosure(bars):
    """不传 validation 时, 卡片本身必须注入冻结的全市场研究结论。

    历史 bug: build_inference 支持 validation, 但 get_action_card 从不传,
    于是「检验 → 降置信 → 前端红字」在默认路径是死代码。修复点在
    build_action_card 内默认注入, 覆盖所有调用方。
    """
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")
    rv = card["inference"]["rule_validation"]
    assert rv is not None, "默认卡片必须带规则有效性披露"
    assert rv["verdict"] in ("validated", "weak_signal", "inverted", "no_edge", "insufficient")
    basis = rv["basis"]
    assert basis is not None, "冻结结论必须带研究日期与范围"
    assert basis["study_date"] == "2026-11-13"
    assert basis["symbols_used"] == 74
    assert basis["sampled_points"] == 2660
    assert basis["scope"] == "market_research"
    assert "全市场" in rv["note"] and "不是本只股票单独检验" in rv["note"]


def test_default_card_discloses_strong_as_inverted(bars, monkeypatch):
    """被判「偏强」的默认卡片: 置信度必须被压到 very_low 且插入反向警告。

    用闭包捕获替换前的真 diagnose, 避免 monkeypatch 后自递归。
    """
    from app.services import strength_research as sr

    orig = action_card.diag.diagnose

    def force_strong(df):
        d = dict(orig(df))
        d["strength"] = dict(d["strength"])
        d["strength"]["stance"] = "strong"
        return d

    monkeypatch.setattr(action_card.diag, "diagnose", force_strong)
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")  # 不传 validation
    inf = card["inference"]
    assert inf["confidence"] == "very_low"
    assert any("反向" in c for c in inf["caveats"])
    rv = inf["rule_validation"]
    assert rv["verdict"] == "inverted"
    assert rv["samples"] == sr.load_market_validation()["groups"][0]["samples"]
    assert rv["basis"]["study_date"] == "2026-11-13"


def test_explicit_validation_overrides_default(bars):
    """显式传 validation 时覆盖冻结结论（留未来接单票 live 检验的口子）。"""
    custom = {"groups": [], "conclusions": ["自定义单票检验"], "basis": None}
    card = action_card.build_action_card(df=bars, symbol="TEST.SH", validation=custom)
    rv = card["inference"]["rule_validation"]
    assert rv["conclusions"] == ["自定义单票检验"]
    assert rv["basis"] is None, "显式覆盖时不应残留冻结结论的 basis"


def test_market_validation_shape_matches_online_report():
    """冻结结论与线上 validate_across_symbols 同形, 且数字如实纠正旧结论。"""
    from app.services import strength_research as sr

    v = sr.load_market_validation()
    assert {g["stance"] for g in v["groups"]} == {"strong", "weak", "unclear"}
    strong = next(g for g in v["groups"] if g["stance"] == "strong")
    assert strong["verdict"] == "inverted"
    assert strong["forward"]["5"] == -6.9
    assert strong["forward"]["10"] == -7.3
    # 早期宣称「20 日 -9.1pp、三期一致」在最终样本上不成立 —— 这里如实记 -2.8
    assert strong["forward"]["20"] == -2.8
    assert any("20 日" in c and "不成立" in c for c in v["conclusions"])
    assert any("不翻转判据" in c for c in v["conclusions"])
    # 每次调用返回新 dict, 就地修改不污染后续卡片
    v["groups"][0]["verdict"] = "MUTATED"
    assert sr.load_market_validation()["groups"][0]["verdict"] == "inverted"


def test_unclear_stance_not_marked_inverted(bars):
    """「方向不明」档不该被误标成反向指标。"""
    card = action_card.build_action_card(df=bars, symbol="TEST.SH")  # make_bars → unclear
    assert card["strength"]["stance"] == "unclear"
    assert card["inference"]["rule_validation"]["verdict"] == "validated"
    assert card["inference"]["confidence"] != "very_low"
