"""热股风向标 API — 即时榜 + 榜单元信息。

落库数据走通用扩展表 API (四张表一榜一张):
  GET /api/ext-data/ext_hot_stock/rows
  GET /api/ext-data/ext_hot_stock/series?field=hot_value&granularity=daily&days=30
  (ext_hot_select / ext_hot_watch / ext_hot_surge 同构)

为什么即时接口不复用扩展表的 `/rows`:
  落库是每日 17:10 收盘后一次快照, 而热股榜单的 `updateTime` 是**每小时**刷新的
  (实测 `2026-11-19 06:00:00`)。盘中要看「此刻在热什么」必须回源。

为什么参数要前置校验:
  平安对非法 `time_range` / `module_code` **不报错**, 而是返回
  `{status: 1, total: 0, list: []}` —— 空榜单与参数错误在协议上不可区分。
  所以取值白名单在服务层 (`hot_stock.normalize_*`) 拦, 不靠上游。

`hot_value`(`cnt`) 的可比性: 同一榜单同一时间窗口内可比, 跨榜/跨窗口不可比
(实测加自选 1h 的热度值只有 300 上下, 而热搜 24h 首条是 66121)。
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
from app.services.hot_stock import (
    ASSET_KINDS,
    BOARDS,
    DEFAULT_SIZE,
    LANDED_ASSET_KINDS,
    MAX_SIZE,
    TIME_RANGES,
    live,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hot-stock", tags=["hot-stock"])


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
    return HTTPException(500, f"热股榜单取数失败: {exc}")


@router.get("/boards")
def list_boards():
    """榜单/类别/时间区间与对应扩展表的元信息 (给前端下拉框用)。"""
    return {
        "boards": [
            {
                "id": board,
                "label": label,
                "table_id": table_id,
                "landed": list(LANDED_ASSET_KINDS),
            }
            for board, (table_id, label) in BOARDS.items()
        ],
        "asset_kinds": [
            {"id": kind, "label": label, "landed": kind in LANDED_ASSET_KINDS}
            for kind, label in ASSET_KINDS.items()
        ],
        "time_ranges": [
            {"id": value, "label": label} for value, label in TIME_RANGES.items()
        ],
        "default_size": DEFAULT_SIZE,
        "max_size": MAX_SIZE,
        "note": (
            "hot_value 在同一榜单同一时间窗口内可比, 跨榜/跨窗口不可比; "
            "落库表每日 17:10 各拉 aStock + etf 一次"
        ),
    }


@router.get("/list")
def list_hot(
    board: str = Query("hot_stock", description="榜单: hot_stock/hot_select/hot_watch/hot_surge"),
    asset_kind: str = Query("aStock", description="类别: aStock/etf/block/hkStock/bond"),
    time_range: str = Query("24h", description="时间区间: 1h/1/1小时 或 24h/24/1d/24小时"),
    size: int = Query(50, ge=1, le=MAX_SIZE, description=f"条数 (上限 {MAX_SIZE})"),
    page: int = Query(1, ge=1, description="页码 (榜单深度上限 100 名)"),
):
    """即时拉取一个榜单组合 (不落库), 返回归一后的条目。

    `rank` 是本次返回内的名次 (从 1 开始, 配合 `page` 时按返回顺序);
    `chg` 是百分数; `tags` 用 `|` 分隔; `calc_date` 是源给的统计时点
    (`2026111906` 这种 yyyymmddhh), `updated_at` 是榜单更新时间。
    """
    try:
        return live(
            board, asset_kind, time_range=time_range, size=size, page=page
        )
    except Exception as e:
        raise _wrap(e) from e
