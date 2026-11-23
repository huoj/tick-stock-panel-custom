"""平安数据源的「开箱即用」预置：价格分位因子 + 信号 + 监控规则模板。

## 为什么要这个模块

接完平安三个数据源（ETF 估值分位 / 热股 / 研报）之后，字段确实已经自动进了
`custom_signals.allowed_fields()`（`ext_factors` 把数值型 timeseries 表注册成
因子），但**用户得自己去界面里一条条拼条件**才算用上。这个模块把「已经验证过
口径」的那几条直接播种成可编辑的定义，省掉这一步。

## 播种什么

1. **价格分位因子** `uf_price_percentile_250d`（DSL，kind=custom）
   `(ts_rank(close, 250) - 1) * 100 / 249` —— 现价在近 250 个交易日收盘价中的位置
   （0=最低，100=最高）。这是 TSP 自有 K 线就能算的，与平安的**估值分位**互补：
   - 估值分位（`ext_etf_valuation_pe_percentile`）：PE 在历史上多贵，但只有 ETF、且平安从接入日起累积
   - 价格分位（本因子）：现价在近一年什么位置，全标的、有完整历史
   两个都低 = 真便宜；价格低但估值高 = 盈利在崩，是陷阱。

2. **信号**（`custom_signals`，可编辑）
   - `pa_etf_deep_value`：ETF 估值分位 ≤ 20 且 场内折价（`premium_rate < 0`）
   - `pa_etf_overheated`：ETF 估值分位 ≥ 85 且 近一年涨幅 > 30%（好资产坏价格）
   - `pa_hot_rising`：出现在热搜榜前 20（`ext_ext_hot_stock_rank <= 20`）
   - `pa_research_new_coverage`：研报覆盖数 ≥ 2（卖方开始覆盖）

3. **监控规则**（`monitor_rules`，signal 型，绑定播种时的持仓 ∪ 自选）
   把上面 4 条信号挂成规则的 `op="truth"` 条件。**标的清单是播种时的快照**，
   之后可自由增删；想跟着自选分组动态走，把 scope 改成 `watchlist_group` 即可。

## 不播种什么（避免误触）

- **不播种「热度跃升」规则**：`hot_value` 的跨期增量还没有历史数据验证过方向
  （是顺势还是反向未知）。等落够 2~4 周数据、回测出方向再定阈值。
- **不给个股播种估值分位信号**：`ext_valuation` 只有 11 个分区，算不出 3 年分位；
  平安的估值分位只覆盖 ETF。

## 口径注意

- 信号里的 `ext_ext_*` 是**双前缀**：`ext_` 是扩展列前缀，配置 id 本身也叫
  `ext_etf_valuation` 之类。这是既有约定（`ext_factors.ext_column_name`），不是笔误。
- 分位/涨跌幅/股息列在落库时已统一成**百分数**（`0.15` = 0.15%），所以阈值直接写
  20 / 85 / 30，不用换算。
- ETF 估值表里 `pe_percentile` 对 QDII（如标普消费 ETF）是 null —— 条件对 null
  恒为假，不会误触发。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 独立标记: 不能复用 custom_signals 的 `.seeded_v1`（那是 20 条基础信号的标记，
# 早已存在，复用会让新预置永远不播种）。
_SEED_MARKER = ".pingan_presets_v1"

PRICE_PERCENTILE_FACTOR_ID = "uf_price_percentile_250d"
PRICE_PERCENTILE_WINDOW = 250

# ── 1) 价格分位因子（DSL） ───────────────────────────────────────────
# ts_rank(x, n) 返回 1..n 的秩；归一到 0..100 百分位。
# 手工核对（159682.SZ 2026-11-19）：现价在近 250 日排第 2 位 → (2-1)*100/249 = 0.40，
# 与公式输出一致。窗口内不足 250 根时 min_samples 使其为 null（fail-closed）。
PRICE_PERCENTILE_FORMULA = (
    f"(ts_rank(close, {PRICE_PERCENTILE_WINDOW}) - 1) * 100 / {PRICE_PERCENTILE_WINDOW - 1}"
)


def price_percentile_formula() -> str:
    """价格分位公式本体 (测试与文档引用同一处, 避免抄两遍抄错)。"""
    return PRICE_PERCENTILE_FORMULA


def price_percentile_factor() -> dict:
    return {
        "id": PRICE_PERCENTILE_FACTOR_ID,
        "label": "价格分位(250日)",
        "kind": "custom",
        "lang": "dsl",
        "formula": PRICE_PERCENTILE_FORMULA,
        "group": "价格位置",
        "status": "active",
        "version": 1,
        "direction": "none",
        "desc": "现价在近 250 个交易日收盘价中的位置 (0=最低, 100=最高)",
    }


# ── 2) 信号 ─────────────────────────────────────────────────────────
def _cond(left: str, op: str, right: str | float, left_days: int = 0,
          right_days: int = 0) -> dict:
    """信号条件。right 为数字→字面量；字符串→字段/取值引用 (见 custom_signals._parse_right)。"""
    return {
        "left": left,
        "op": op,
        "right": str(right) if isinstance(right, float) else right,
        "leftDays": left_days,
        "rightDays": right_days,
    }


SIGNALS: list[dict] = [
    {
        "id": "pa_etf_deep_value",
        "name": "ETF 深度低估且折价",
        "kind": "entry",
        "timeframe": "daily",
        "enabled": True,
        "desc": "PE 历史分位 ≤ 20% 且场内折价 —— 判断依据是位置而不是绝对 PE",
        "conditions": [
            _cond("ext_ext_etf_valuation_pe_percentile", "<=", "20"),
            _cond("ext_ext_etf_valuation_premium_rate", "<", "0"),
        ],
        "column": "signal_pa_etf_deep_value",
    },
    {
        "id": "pa_etf_overheated",
        "name": "ETF 估值过热",
        "kind": "exit",
        "timeframe": "daily",
        "enabled": True,
        "desc": "PE 分位 ≥ 85% 且近一年涨幅 > 30% —— 好资产但坏价格",
        "conditions": [
            _cond("ext_ext_etf_valuation_pe_percentile", ">=", "85"),
            _cond("ext_ext_etf_valuation_rise_1y", ">", "30"),
        ],
        "column": "signal_pa_etf_overheated",
    },
    {
        "id": "pa_hot_rising",
        "name": "人气榜前列",
        "kind": "both",
        "timeframe": "daily",
        "enabled": True,
        "desc": "进入热搜榜前 20 名 (散户主动搜索, 意图最强的一类关注度)",
        "conditions": [
            _cond("ext_ext_hot_stock_rank", "<=", "20"),
        ],
        "column": "signal_pa_hot_rising",
    },
    {
        "id": "pa_research_new_coverage",
        "name": "机构新覆盖",
        "kind": "both",
        "timeframe": "daily",
        "enabled": True,
        "desc": "当日研报命中 ≥ 2 篇 —— 卖方开始覆盖, 是关注度的变化而不是观点",
        # 注意: 研报片段里没有机构名/评级/发布日期 (源端限制), 所以只能数篇数
        "conditions": [
            _cond("ext_ext_research_note_doc_count", ">=", "2"),
        ],
        "column": "signal_pa_research_new_coverage",
    },
]


# ── 3) 监控规则（signal 型，引用上面的信号列） ─────────────────────
def build_rules(symbols: list[str]) -> list[dict]:
    """按给定标的清单生成 signal 型监控规则（scope=symbols）。"""
    syms = [s for s in symbols if s]
    if not syms:
        return []
    common = {
        "enabled": True,
        "type": "signal",
        "scope": "symbols",
        "symbols": syms,
        "logic": "or",
        "cooldown_seconds": 21600,     # 6 小时: 日线级信号一天最多提醒一次
        "severity": "warn",
        "webhook_enabled": False,
    }
    return [
        {
            **common,
            "id": "pa_rule_etf_deep_value",
            "name": "ETF 深度低估且折价",
            "asset_type": "etf",
            "conditions": [{"field": "signal_pa_etf_deep_value", "op": "truth"}],
            "message": "ETF 估值分位处于低位且场内折价，可按计划加档",
        },
        {
            **common,
            "id": "pa_rule_etf_overheated",
            "name": "ETF 估值过热",
            "asset_type": "etf",
            "conditions": [{"field": "signal_pa_etf_overheated", "op": "truth"}],
            "severity": "critical",
            "message": "ETF 估值分位偏高且近一年涨幅大，考虑减仓兑现",
        },
        {
            **common,
            "id": "pa_rule_hot_rising",
            "name": "人气榜前列",
            "asset_type": "stock",
            # 用 symbols 而不是 watchlist_group: 播种时不知道用户想要哪个分组,
            # 且 watchlist_group 要求 group_id 非空 (validate 强制)。想动态跟随
            # 自选分组, 播种后把 scope 改成 watchlist_group 并选分组即可。
            "conditions": [{"field": "signal_pa_hot_rising", "op": "truth"}],
            "severity": "info",
            "message": "进入热搜榜前 20，关注度显著上升",
        },
        {
            **common,
            "id": "pa_rule_research_coverage",
            "name": "机构新覆盖",
            "asset_type": "stock",
            "conditions": [{"field": "signal_pa_research_new_coverage", "op": "truth"}],
            "severity": "info",
            "message": "当日出现 2 篇以上研报，卖方开始覆盖",
        },
    ]


# ── 播种 ────────────────────────────────────────────────────────────
def _tracked_symbols(data_dir: Path) -> list[str]:
    """播种时的持仓 ∪ 自选快照（失败返回空，规则就不播种）。"""
    found: list[str] = []
    try:
        from app.services import watchlist

        found += [str(r.get("symbol") or "") for r in watchlist.list_symbols()]
    except Exception as e:  # noqa: BLE001 — 读不到就少几个标, 不该阻断
        logger.warning("预置规则读取自选失败: %s", e)
    try:
        from app.services import manual_holdings

        found += list(manual_holdings.load_all(data_dir))
    except Exception as e:  # noqa: BLE001
        logger.warning("预置规则读取持仓失败: %s", e)
    return sorted({s.strip().upper() for s in found if s and s.strip()})


def _seed_factor(data_dir: Path) -> str | None:
    """写入价格分位因子并**立即注册**。

    启动顺序是 `load_runtime_registry` 在前、本预置在后 —— 只写文件不注册的话，
    这个因子要等下一次重启才会进注册表（信号条件引用它会报缺列）。所以这里
    写完就 register_definition，让当次启动即可用。
    """
    from app.factors import store as factor_store

    definition = price_percentile_factor()
    existing = {d.get("id") for d in factor_store.load_all(data_dir)}
    if definition["id"] in existing:
        return None
    factor_store.save_one(data_dir, definition)
    try:
        factor_store.register_definition(definition)
    except ValueError as e:
        logger.warning("价格分位因子已写入但注册失败 (重启后会重试): %s", e)
    return definition["id"]


def _seed_signals(data_dir: Path) -> list[str]:
    from app.strategy import custom_signals

    existing = {d.get("id") for d in custom_signals.load_all(data_dir)}
    seeded: list[str] = []
    for sig in SIGNALS:
        if sig["id"] in existing:
            continue
        custom_signals.save_one(data_dir, sig)
        seeded.append(sig["id"])
    return seeded


def _seed_rules(data_dir: Path) -> list[str]:
    from app.strategy import monitor_rules

    symbols = _tracked_symbols(data_dir)
    if not symbols:
        logger.info("预置规则跳过: 自选与持仓都为空")
        return []
    existing = {r.get("id") for r in monitor_rules.load_all(data_dir)}
    seeded: list[str] = []
    for rule in build_rules(symbols):
        if rule["id"] in existing:
            continue
        try:
            monitor_rules.validate(rule)
        except ValueError as e:
            logger.warning("预置规则 %s 未通过校验, 跳过: %s", rule["id"], e)
            continue
        monitor_rules.save_one(data_dir, rule)
        seeded.append(rule["id"])
    return seeded


def seed(data_dir: Path) -> dict:
    """播种全套预置（幂等：已存在的 id 一律跳过）。返回各处实际写入的清单。"""
    result: dict[str, object] = {"factor": None, "signals": [], "rules": []}
    try:
        result["factor"] = _seed_factor(data_dir)
    except Exception as e:  # noqa: BLE001 — 单块失败不阻断其余
        logger.warning("预置价格分位因子写入失败: %s", e)
    try:
        result["signals"] = _seed_signals(data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("预置信号写入失败: %s", e)
    try:
        result["rules"] = _seed_rules(data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("预置监控规则写入失败: %s", e)
    return result


async def ensure_presets(data_dir: Path) -> None:
    """启动时播种一次（写标记防重）；失败只 warning，不阻断启动。"""
    data_dir = Path(data_dir)
    marker = data_dir / "user_data" / _SEED_MARKER
    try:
        if marker.exists():
            return
        written = seed(data_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"factor: {written['factor']}\n"
            f"signals: {', '.join(written['signals']) or '-'}\n"
            f"rules: {', '.join(written['rules']) or '-'}\n",
            encoding="utf-8",
        )
        if written["factor"] or written["signals"] or written["rules"]:
            logger.info(
                "平安预置已播种: 因子=%s 信号=%d 条 规则=%d 条",
                written["factor"] or "-", len(written["signals"]), len(written["rules"]),
            )
    except Exception as e:  # noqa: BLE001 — 启动契约: 预置失败不阻断
        logger.warning("平安预置播种失败 (不影响启动): %s", e)
