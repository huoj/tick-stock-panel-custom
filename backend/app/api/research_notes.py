"""研报检索 API — 即时多文档浏览 + 跟踪清单。

落库数据走通用扩展表 API:
  GET /api/ext-data/ext_research_note/rows
  GET /api/ext-data/ext_research_note/series?field=top_score&granularity=daily&days=30

为什么要即时接口:
  扩展表的去重键是 `[symbol]`, 每个标的每日只留**一篇** (当日命中最优)。
  要看「这个票这一周都有谁在写、都说了什么」, 必须回源拿完整列表 ——
  一次 `top_k=20` 通常能得到 10~19 篇去重后的文档。

为什么阈值在本地做:
  请求体里的 `score_threshhold` (服务端拼错的历史字段名) **不生效** ——
  同一 query 传 0.05 与 0.6 返回完全相同的记录集 (实测 44 条一字不差)。
  所以 `threshold` 是本地过滤参数, 不是透传给源端的筛选项。

为什么返回里没有机构/评级/发布日期:
  源端每条 record 只有 content/metadata/score/title 四个键, 没有这些元数据;
  而片段本身是骨架 (实测 27 篇去重文档 30% 不到 200 字), 正文里抽不出可信的
  机构与日期: 机构 1/27 且是错的, 评级 0/27, 可信发布日期 1/27。
  宁可只给片段与 path, 让用户回原文看, 也不返回猜出来的值。
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
from app.services.research_notes import (
    CONFIG_ID,
    DEFAULT_THRESHOLD,
    DEFAULT_TOP_K,
    MAX_SYMBOLS,
    MAX_TOP_K,
    live_docs,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/research-notes", tags=["research-notes"])


def _wrap(exc: Exception) -> HTTPException:
    """平安三类错误 → 对前端有意义的 HTTP 状态。"""
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    if isinstance(exc, PingAnAuthError):
        return HTTPException(
            400, f"平安 API Key 无效或未配置: {exc} — 请在 设置 → 数据源 → 平安证券 填写"
        )
    if isinstance(exc, PingAnQuotaError):
        return HTTPException(429, f"平安接口今日额度已用尽: {exc}")
    if isinstance(exc, PingAnRateLimitError):
        return HTTPException(429, f"平安接口限流, 请稍后重试: {exc}")
    if isinstance(exc, PingAnError):
        return HTTPException(502, f"平安接口异常: {exc}")
    return HTTPException(500, f"研报检索失败: {exc}")


@router.get("/search")
def search_notes(
    keyword: str = Query(..., min_length=1, description="检索词: 6 位代码召回最好, 也支持公司名"),
    top_k: int = Query(DEFAULT_TOP_K, ge=1, le=MAX_TOP_K, description=f"召回片段数 (上限 {MAX_TOP_K})"),
    threshold: float = Query(
        DEFAULT_THRESHOLD, ge=0.0, le=1.0,
        description="本地相关度阈值 (服务端不认这个参数, 是本服务端过滤)",
    ),
):
    """即时检索研报库, 按文档去重后返回。

    `raw_count` 是源端返回的片段数 (同一篇研报会被切成多段), `doc_count` 是
    去重后的文档数 —— 两者差异大是正常的。
    """
    try:
        return live_docs(keyword, top_k=top_k, threshold=threshold)
    except Exception as e:
        raise _wrap(e) from e


@router.get("/tracked")
def list_tracked():
    """落库时会跟踪的标的 (= 自选 ∪ 持仓), 以及对应的扩展表 id。"""
    from app.config import settings
    from app.services.research_notes import tracked_symbols

    try:
        symbols = tracked_symbols(settings.data_dir)
    except Exception as e:
        raise HTTPException(500, f"读取跟踪清单失败: {e}") from e
    return {
        "table_id": CONFIG_ID,
        "count": len(symbols),
        "max_symbols": MAX_SYMBOLS,
        "default_top_k": DEFAULT_TOP_K,
        "default_threshold": DEFAULT_THRESHOLD,
        "symbols": symbols,
        "note": (
            "每个标的每日只落一篇 (当日命中最优), 多篇请用 /search; "
            "可在扩展表的「拉取」页把 adapter.symbols 配成固定清单覆盖自选+持仓"
        ),
    }
