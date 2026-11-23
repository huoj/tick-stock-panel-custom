"""Tier A 开放契约快照 — 开放面的任何变化必须是有意识的。

契约即承诺: /api/openapi.json?tier=a 列出的端点集合是二开方依赖的稳定面。
本测试把该集合冻结成常量 — 新增/移除/改方法都会失败, 失败者须同步更新
快照并在 PR 里说明契约变更 (对外破坏性变更需 major 版本)。

快照来源与 /api/openapi.json?tier=a 完全同源: app.openapi() 的 paths
逐 (method, path) 过 api_gateway.required_scope — 规则表是唯一源。
"""
from __future__ import annotations

import pytest

from app.services import api_gateway

# 冻结的开放面 (method + OpenAPI 路径模板), 按字母序。
# 变更此表 = 契约变更, 请在 commit message 与 changelog 中明示。
EXPECTED_OPEN_ENDPOINTS: set[str] = {
    "GET /api/alerts",
    # 事件级统计与归组 (同一政策多家转载算一个事件; 成交日志与政策新闻分开计数)。
    # 放在 /api/alerts 前缀下, scope 同样是 read:analysis —— 只读聚合, 不新增写面。
    "GET /api/alerts/event-groups",
    "GET /api/alerts/summary",
    # 2026-11-14 新增: 「少而重要」告警摘要。只读聚合, 与上面三个同一 scope
    # （read:analysis）, 不新增写面。
    "GET /api/alerts/digest",
    "GET /api/backtest/candidates",
    "POST /api/backtest/factor/batch",
    "GET /api/backtest/factor/columns",
    "POST /api/backtest/factor/run",
    "POST /api/backtest/run",
    "GET /api/backtest/status",
    "POST /api/backtest/strategy/run",
    "GET /api/events",
    "POST /api/events/ticket",
    "GET /api/ext-data",
    "POST /api/ext-data/{config_id}/ingest",
    "GET /api/ext-data/{config_id}/rows",
    "GET /api/ext-data/{config_id}/series",
    "GET /api/ext-data/{config_id}/values",
    "GET /api/ext-data/schema-all",
    "GET /api/ext-data/schema/{config_id}",
    "GET /api/ext-data/{config_id}/dimension-intraday",
    "GET /api/ext-data/{config_id}/dimension-members",
    "GET /api/index/daily",
    "GET /api/index/minute",
    "GET /api/intraday/indices",
    "GET /api/intraday/status",
    "GET /api/kline/daily",
    "GET /api/kline/daily/latest",
    "GET /api/kline/instruments/search",
    "GET /api/kline/minute",
    "GET /api/kline/minute-range",
    "GET /api/overview/market",
    "DELETE /api/paper/account",
    "GET /api/paper/account",
    "POST /api/paper/account",
    "GET /api/paper/accounts",
    "POST /api/paper/arena/batch_create",
    "POST /api/paper/auto_rules",
    "DELETE /api/paper/auto_rules/{rule_id}",
    "GET /api/paper/auto_rules",
    "POST /api/paper/auto_rules/{rule_id}/enabled",
    "GET /api/paper/compare",
    "POST /api/paper/freeze",
    "GET /api/paper/nav",
    "POST /api/paper/orders",
    "DELETE /api/paper/orders/{order_id}",
    "GET /api/paper/orders",
    "GET /api/paper/overview",
    "GET /api/paper/positions",
    "POST /api/paper/rebuild",
    "GET /api/paper/rounds",
    "POST /api/paper/settings",
    "GET /api/paper/stats",
    "GET /api/paper/trades",
    "GET /api/regime/coverage",
    "GET /api/regime/history",
    "GET /api/regime/latest",
    "GET /api/regime/mainline",
    "GET /api/regime/phases",
    "GET /api/regime/states",
    "GET /api/screener/cached",
    "GET /api/screener/limit-ladder",
    "GET /api/screener/market-snapshot",
    "POST /api/screener/run",
    "POST /api/screener/run_all",
    "POST /api/screener/run_preset",
    "GET /api/screener/strategies",
    "GET /api/strategies",
    "GET /api/strategies/ai/status",
    "GET /api/strategies/{strategy_id}",
    "GET /api/strategies/{strategy_id}/source",
    # 策略包导出 (bundle): 与 /source 同敏感级, 同为开放层
    "GET /api/strategies/{strategy_id}/bundle",
    # ── 只读数据面扩展 (自选/财务/数据湖/数据集/因子/风险/复盘等) ──
    # 这批的共同口径: 纯读取, 不触发重算、不落盘、不改结构。
    # read:market — 行情与标的的基础数据
    "GET /api/abnormal/intraday",
    "GET /api/abnormal/overview",
    "GET /api/capabilities",
    "GET /api/data/integrity",
    "GET /api/data/schema/{table}",
    "GET /api/data/status",
    "GET /api/data/version",
    "GET /api/data/workload",
    "GET /api/datasets/catalog",
    "GET /api/datasets/pit",
    "GET /api/datasets/{dataset_id}",
    "GET /api/datasets/{dataset_id}/freshness",
    "GET /api/datasets/{dataset_id}/pit",
    "GET /api/datasets/{dataset_id}/quality",
    "GET /api/financials/balance-sheet",
    "GET /api/financials/cash-flow",
    "GET /api/financials/income",
    "GET /api/financials/metrics",
    "GET /api/financials/reports",
    "GET /api/financials/shares",
    "GET /api/financials/status",
    "GET /api/kline/minute-gaps",
    # 批量读: POST 方法但只读语义
    "POST /api/kline/daily-batch",
    "POST /api/kline/instruments/names",
    "POST /api/kline/minute-batch",
    # read:analysis — 分析面只读
    "GET /api/analysis-menus",
    "GET /api/analysis-menus/{menu_id}",
    "GET /api/backtest/guardian/config",
    "GET /api/backtest/guardian/overview",
    "GET /api/backtest/guardian/runs",
    "GET /api/backtest/guardian/runs/{run_id}",
    "GET /api/backtest/lab/report",
    "GET /api/backtest/mining/availability",
    "GET /api/backtest/mining/config",
    "GET /api/backtest/mining/lock",
    "GET /api/backtest/mining/runs",
    "GET /api/backtest/mining/runs/{run_id}",
    "GET /api/backtest/mining/runs/{run_id}/result",
    "GET /api/backtest/optimize-robust/preview",
    "GET /api/backtest/position-policies",
    "GET /api/backtest/position-policies/templates",
    "GET /api/backtest/position-policies/{policy_id}",
    "GET /api/backtest/strategy/runs/{run_id}/evidence",
    "GET /api/backtest/walkforward/preview",
    "GET /api/custom-signals",
    "GET /api/custom-signals/options",
    "GET /api/custom/assistant/sessions",
    "GET /api/custom/assistant/status",
    "GET /api/custom/assistant/suggests",
    "GET /api/custom/paper-execution/bindings",
    "GET /api/custom/paper-execution/state",
    "GET /api/definitions",
    "GET /api/definitions/resources",
    "GET /api/factors",
    "GET /api/lots",
    "GET /api/market-recap/auction-benchmark",
    "GET /api/market-recap/dragon-tiger",
    "GET /api/market-recap/reports",
    "GET /api/monitor-rules",
    "GET /api/monitor-rules/options",
    "GET /api/pipeline/jobs",
    "GET /api/pipeline/jobs/{job_id}",
    # 2026-11-09 新增: 实际持仓（真实权重/成本/持仓期限）。与
    # GET /api/risk/portfolio 的观察口径分开 —— 自选清单不再被当成真实组合。
    "GET /api/risk/positions",
    "GET /api/risk/portfolio",
    "GET /api/risk/signal/{signal_id}",
    "GET /api/risk/signals",
    "GET /api/rps/rotation",
    "GET /api/screener/cached-result/{strategy_id}",
    "GET /api/screener/cached-summary",
    "GET /api/sector-rotation",
    "GET /api/stock-analysis/levels",
    "GET /api/stock-analysis/reports",
    "GET /api/task-registry",
    "GET /api/watchlist",
    "GET /api/watchlist/enriched",
    "GET /api/watchlist/groups",
    "GET /api/watchlist/ocr-status",
}


def _collect_open_endpoints() -> set[str]:
    """与 /api/openapi.json?tier=a 同源收集 (app.openapi paths 过 required_scope)。"""
    from app.main import app

    spec = app.openapi()
    found: set[str] = set()
    for path, ops in spec.get("paths", {}).items():
        for method in ops:
            if method in ("get", "post", "put", "delete", "patch") and api_gateway.required_scope(
                method.upper(), path,
            ):
                found.add(f"{method.upper()} {path}")
    return found


def test_open_contract_snapshot():
    found = _collect_open_endpoints()
    added = found - EXPECTED_OPEN_ENDPOINTS
    removed = EXPECTED_OPEN_ENDPOINTS - found
    if added or removed:
        pytest.fail(
            "Tier A 开放契约发生变化 — 这是对外承诺面, 须有意识地更新快照:\n"
            f"  新增: {sorted(added) or '无'}\n"
            f"  移除: {sorted(removed) or '无'}\n"
            "确认无误后, 同步更新本文件的 EXPECTED_OPEN_ENDPOINTS 并在 commit 中说明。",
        )
