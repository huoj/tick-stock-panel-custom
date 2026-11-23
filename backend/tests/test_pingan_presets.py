"""平安预置（价格分位因子 + 信号 + 规则）的契约测试。

三条硬约束值得写用例：
  1. `ext_ext_*` 双前缀字段必须真在 `allowed_fields()` 里 —— 写错一个字就会
     在保存时被业务校验拒掉，而不是运行时静默不触发。
  2. 播种出来的信号/规则必须能通过各自的 `validate()` —— 我第一版就把
     `hot_rising` 写成 `scope=watchlist_group` 却给了 `group_id=None`，被拒。
  3. 播种必须幂等、且在「自选与持仓都为空」时不写规则（否则规则 scope=symbols
     但 symbols=[]，validate 同样会拒）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import pingan_presets as pp


# ── 价格分位因子 ──────────────────────────────────────────────────
def test_price_percentile_factor_definition() -> None:
    definition = pp.price_percentile_factor()
    assert definition["id"] == pp.PRICE_PERCENTILE_FACTOR_ID
    assert definition["id"].startswith("uf_"), "自定义因子 id 必须以 uf_ 开头"
    assert definition["kind"] == "custom"
    assert definition["lang"] == "dsl"
    assert definition["status"] == "active", "draft 状态不会进注册表"
    assert "ts_rank(close, 250)" in definition["formula"]


def test_price_percentile_formula_compiles() -> None:
    from app.factors.dsl import compile_formula

    compiled = compile_formula(pp.price_percentile_formula())
    assert compiled.ok
    assert compiled.dependencies == frozenset({"close"})
    assert compiled.warmup_bars >= pp.PRICE_PERCENTILE_WINDOW, "预热必须覆盖整个窗口"


def test_price_percentile_normalizes_to_0_100() -> None:
    """秩 1..250 归一到 0..100 —— 首尾必须落边界, 否则阈值 20/85 全都不对。"""
    formula = pp.price_percentile_formula()
    n = pp.PRICE_PERCENTILE_WINDOW
    # 把秩代入手算: 秩 1 → 0, 秩 250 → 100
    assert eval(formula.replace("ts_rank(close, 250)", "1")) == pytest.approx(0.0)
    assert eval(formula.replace("ts_rank(close, 250)", str(n))) == pytest.approx(100.0)


def test_price_percentile_matches_manual_rank() -> None:
    """真实 ETF 数据上：公式输出必须与手工排位一致（159682.SZ，2026-11-19 实测 0.4016）。"""
    import polars as pl

    from app.config import settings
    from app.factors.dsl import compile_formula

    parquet = Path(settings.data_dir) / "kline_etf_daily"
    if not parquet.exists():
        pytest.skip("无本地 ETF 日K")
    df = (
        pl.scan_parquet(str(parquet / "**" / "*.parquet"), extra_columns="ignore")
        .filter(pl.col("symbol") == "159682.SZ")
        .select("symbol", "date", "close")
        .sort("date")
        .collect()
    )
    if df.height < pp.PRICE_PERCENTILE_WINDOW:
        pytest.skip("历史不足 250 根")
    compiled = compile_formula(pp.price_percentile_formula())
    out = compiled.frame_transform(df)
    value = out.get_column(out.columns[-1]).drop_nulls()
    assert value.len() > 0
    closes = df["close"].tail(pp.PRICE_PERCENTILE_WINDOW).to_list()
    rank = sum(1 for c in closes if c <= closes[-1])
    expected = (rank - 1) * 100 / (pp.PRICE_PERCENTILE_WINDOW - 1)
    assert value[-1] == pytest.approx(expected, abs=1e-6)


# ── 信号 ────────────────────────────────────────────────────────
def test_signal_fields_are_all_whitelisted() -> None:
    """双前缀写错一个字符, 保存时才会被拒 —— 这里先拦。"""
    from app.strategy.custom_signals import allowed_fields

    whitelist = allowed_fields()
    for sig in pp.SIGNALS:
        for cond in sig["conditions"]:
            assert cond["left"] in whitelist, f"{sig['id']} 引用了未知字段 {cond['left']}"


def test_signals_pass_validation() -> None:
    from app.strategy.custom_signals import validate

    for sig in pp.SIGNALS:
        validate(sig)


def test_signals_have_unique_ids_and_columns() -> None:
    ids = [s["id"] for s in pp.SIGNALS]
    columns = [s["column"] for s in pp.SIGNALS]
    assert len(ids) == len(set(ids))
    assert len(columns) == len(set(columns))
    for sig in pp.SIGNALS:
        assert sig["column"].startswith("signal_"), "规则用 op=truth 时认这个前缀"


def test_no_signal_relies_on_unreliable_research_metadata() -> None:
    """研报源端不给机构/评级/发布日期 (实测 1/27、0/27、1/27), 预置里不许出现。"""
    for sig in pp.SIGNALS:
        for cond in sig["conditions"]:
            for banned in ("institution", "rating", "note_date"):
                assert banned not in cond["left"]


# ── 规则 ────────────────────────────────────────────────────────
def test_rules_reference_seeded_signals() -> None:
    rules = pp.build_rules(["600941.SH"])
    signal_columns = {s["column"] for s in pp.SIGNALS}
    assert rules
    for rule in rules:
        for cond in rule["conditions"]:
            assert cond["op"] == "truth"
            assert cond["field"] in signal_columns, f"{rule['id']} 引用了未播种的信号"


def test_rules_pass_validation() -> None:
    from app.strategy.monitor_rules import validate

    for rule in pp.build_rules(["600941.SH", "159682.SZ"]):
        validate(rule)


def test_rules_empty_without_symbols() -> None:
    """scope=symbols 且 symbols 为空会被校验拒 —— 所以干脆不生成。"""
    assert pp.build_rules([]) == []
    assert pp.build_rules(["", None]) == []  # type: ignore[list-item]


def test_no_hot_value_delta_rule_yet() -> None:
    """热度增量的方向还没回测验证过, 预置里不许出现「热度跃升」这类猜阈值的规则。"""
    joined = " ".join(str(r.get("name", "")) + str(r.get("message", ""))
                      for r in pp.build_rules(["600941.SH"]))
    assert "跃升" not in joined


# ── 播种 ────────────────────────────────────────────────────────
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbols: list[str]) -> None:
    """把持仓/自选读取替换成固定清单 (真实读取会碰全局 data 目录)。"""
    monkeypatch.setattr(pp, "_tracked_symbols", lambda data_dir: symbols)


def test_seed_writes_all_three_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch, ["600941.SH", "159682.SZ"])
    written = pp.seed(tmp_path)
    assert written["factor"] == pp.PRICE_PERCENTILE_FACTOR_ID
    assert len(written["signals"]) == len(pp.SIGNALS)
    assert len(written["rules"]) == len(pp.build_rules(["x"]))

    from app.factors import store as factor_store
    from app.strategy import custom_signals, monitor_rules

    assert [d["id"] for d in factor_store.load_all(tmp_path)] == [pp.PRICE_PERCENTILE_FACTOR_ID]
    assert {d["id"] for d in custom_signals.load_all(tmp_path)} == {s["id"] for s in pp.SIGNALS}
    assert {r["id"] for r in monitor_rules.load_all(tmp_path)} == {
        r["id"] for r in pp.build_rules(["x"])
    }


def test_seed_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch, ["600941.SH"])
    pp.seed(tmp_path)
    again = pp.seed(tmp_path)
    assert again == {"factor": None, "signals": [], "rules": []}


def test_seed_skips_rules_when_no_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate(tmp_path, monkeypatch, [])
    written = pp.seed(tmp_path)
    assert written["rules"] == []
    assert written["signals"], "信号与标的无关, 仍应播种"


def test_seed_keeps_user_edited_definitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户改过的信号不许被覆盖 (按 id 跳过, 不比内容)。"""
    from app.strategy import custom_signals

    _isolate(tmp_path, monkeypatch, ["600941.SH"])
    pp.seed(tmp_path)
    mine = custom_signals.load_one(tmp_path, "pa_etf_deep_value")
    assert mine is not None
    mine["name"] = "我改过的名字"
    mine["enabled"] = False
    custom_signals.save_one(tmp_path, mine)

    pp.seed(tmp_path)
    after = custom_signals.load_one(tmp_path, "pa_etf_deep_value")
    assert after is not None
    assert after["name"] == "我改过的名字"
    assert after["enabled"] is False


def test_ensure_presets_writes_marker_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    _isolate(tmp_path, monkeypatch, ["600941.SH"])
    asyncio.run(pp.ensure_presets(tmp_path))
    marker = tmp_path / "user_data" / pp._SEED_MARKER
    assert marker.exists()
    body = marker.read_text(encoding="utf-8")
    assert pp.PRICE_PERCENTILE_FACTOR_ID in body

    # 二次启动: 标记存在 → 直接返回, 不再改任何存储
    asyncio.run(pp.ensure_presets(tmp_path))
    assert marker.read_text(encoding="utf-8") == body


def test_ensure_presets_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pp, "seed", _boom)
    asyncio.run(pp.ensure_presets(tmp_path))   # 不抛即通过


def test_marker_distinct_from_signal_seed_marker() -> None:
    """不能复用 custom_signals 的 .seeded_v1 —— 那个早已存在, 复用会让预置永不播种。"""
    assert pp._SEED_MARKER != ".seeded_v1"

def test_seed_factor_registers_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """播种后必须**当次启动就能用** —— 预置排在 load_runtime_registry 之后,
    只写文件不注册的话, 引用该因子的信号会报缺列, 要等下次重启才自愈。"""
    from app.factors.registry import get_factor, unregister_factor

    _isolate(tmp_path, monkeypatch, [])
    written = pp._seed_factor(tmp_path)
    assert written == pp.PRICE_PERCENTILE_FACTOR_ID
    try:
        spec = get_factor(pp.PRICE_PERCENTILE_FACTOR_ID)
        assert spec is not None, "写完没进注册表"
        assert spec.kind == "custom"
        assert spec.warmup_bars >= pp.PRICE_PERCENTILE_WINDOW
    finally:
        unregister_factor(pp.PRICE_PERCENTILE_FACTOR_ID)


def test_existing_factor_file_is_not_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.factors.registry import unregister_factor

    _isolate(tmp_path, monkeypatch, [])
    pp._seed_factor(tmp_path)
    unregister_factor(pp.PRICE_PERCENTILE_FACTOR_ID)
    assert pp._seed_factor(tmp_path) is None, "已存在就不该再写/再注册"
