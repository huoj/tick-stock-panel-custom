"""ETF 估值分位 API — 全市场即时筛选。

为什么不复用扩展表的 `/rows`:
  `_apply_row_filters` 只有等值匹配 (`字段:值1|值2`), 表达不了「PE 分位 ≤ 30%」
  这类区间条件。

为什么不透传平安的服务端 wheres (踩过的坑):
  源把数值都以字符串返回, 服务端的比较与排序按**字符串语义**走 —— 实测
  `orderBy=pe, orderType=A` 的首条是 10.08 而不是 6.48 (`"1x"` 排在 `"6x"` 前),
  `pe < 20` 服务端算 676 只而本地算 717 只, 且 `pe<20 & 规模宽基` 服务端 201 只
  竟多于本地 180 只 (筛选用值与返回字段值不是同一口径)。
  所以本接口只在服务端做「按 code 游标翻页拿全市场」这一件事, 筛选与排序全部在
  本地对归一后的列做 —— 这样 `scope`(规模) 与 `amount_20d`(20日成交额) 也能筛,
  而它们在源端任何操作符都返回 0 条。

全市场一轮 18 次请求, 结果在进程内缓存 3 分钟 (`_UNIVERSE_TTL_S`)。
落库数据仍走通用扩展表 API (历史与序列查询):
  GET /api/ext-data/ext_etf_valuation/rows
  GET /api/ext-data/ext_etf_valuation/series?field=pe_percentile&granularity=daily&days=120
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.plugins.pingan.client import (
    PingAnAuthError,
    PingAnError,
    PingAnQuotaError,
    PingAnRateLimitError,
)
from app.services.etf_valuation import SCREEN_ORDER_BY, SCREEN_ORDER_BY_ALIASES, screen

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/etf-valuation", tags=["etf-valuation"])


@router.get("/screen")
def screen_etfs(
    fund_type1: str | None = Query(
        None, description="一级分类, 逗号分隔多值 (规模宽基/行业主题/风格策略/全球市场/债券资产/商品资产/货币基金)"
    ),
    fund_type2: str | None = Query(None, description="二级分类, 模糊匹配"),
    index_name: str | None = Query(None, description="跟踪指数名, 模糊匹配"),
    code: str | None = Query(None, description="单只代码 (510300 或 SH510300 或 510300.SH)"),
    pe_max: float | None = Query(None, description="PE ≤"),
    pe_min: float | None = Query(None, description="PE ≥"),
    pb_max: float | None = Query(None, description="PB ≤"),
    pb_min: float | None = Query(None, description="PB ≥"),
    pe_percentile_max: float | None = Query(
        None, description="PE 历史分位 ≤ (百分数, 传 30 表示分位 30%)"
    ),
    pb_percentile_max: float | None = Query(
        None, description="PB 历史分位 ≤ (百分数, 传 30 表示分位 30%)"
    ),
    dividend_yield_min: float | None = Query(None, description="股息率 ≥ (百分数)"),
    cost_max: float | None = Query(None, description="管理费率 ≤ (百分数, 传 0.5 表示 0.5%)"),
    rsi_max: float | None = Query(None, description="RSI(6) ≤"),
    rsi_min: float | None = Query(None, description="RSI(6) ≥"),
    roe_min: float | None = Query(None, description="ROE ≥ (百分数, 传 15 表示 15%)"),
    scope_min: float | None = Query(None, description="基金规模 ≥ (元)"),
    amount_20d_min: float | None = Query(None, description="20 日成交额 ≥ (元)"),
    order_by: str = Query(
        "pe_percentile",
        description="排序字段 — 本表列名 (pe_percentile/dividend_yield/scope) 或源字段名 (peFenWei/yielD) 均可",
    ),
    desc: bool = Query(False, description="true=降序"),
    limit: int = Query(50, ge=1, le=100, description="返回条数 (上限 100)"),
):
    """筛选全市场 ETF (本地过滤/排序, 底层全市场数据缓存 3 分钟)。

    分位/费率/股息/ROE 一律按**百分数**传参 (与落库列口径一致):
    `pe_percentile_max=30` 表示「PE 分位 ≤ 30%」。

    返回 `total` 是命中总数 (不是本页条数), `returned` 是本页实际返回数;
    `cached=true` 表示这一轮没有重新翻页取数。列值为空的行不满足任何条件
    (QDII 之类无 PE 的标的不会混进「分位 ≤ 30%」), 排序时也恒定排在最后。
    """
    order_by = SCREEN_ORDER_BY_ALIASES.get(order_by, order_by)
    if order_by not in SCREEN_ORDER_BY:
        raise HTTPException(
            400, f"不支持的排序字段: {order_by} (可用: {', '.join(sorted(SCREEN_ORDER_BY))})"
        )
    params = {
        "fund_type1": fund_type1,
        "fund_type2": fund_type2,
        "index_name": index_name,
        "code": code,
        "pe_max": pe_max,
        "pe_min": pe_min,
        "pb_max": pb_max,
        "pb_min": pb_min,
        "pe_percentile_max": pe_percentile_max,
        "pb_percentile_max": pb_percentile_max,
        "dividend_yield_min": dividend_yield_min,
        "cost_max": cost_max,
        "rsi_max": rsi_max,
        "rsi_min": rsi_min,
        "roe_min": roe_min,
        "scope_min": scope_min,
        "amount_20d_min": amount_20d_min,
    }
    try:
        return screen(params, order_by=order_by, descending=desc, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except PingAnAuthError as e:
        raise HTTPException(
            400, f"平安 API Key 无效或未配置: {e} — 请在 设置 → 数据源 → 平安证券 填写"
        ) from e
    except PingAnQuotaError as e:
        raise HTTPException(429, f"平安接口今日额度已用尽: {e}") from e
    except PingAnRateLimitError as e:
        raise HTTPException(429, f"平安接口限流, 请稍后重试: {e}") from e
    except PingAnError as e:
        raise HTTPException(502, f"平安接口异常: {e}") from e
