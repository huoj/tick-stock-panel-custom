"""数据集目录与新鲜度 — `/api/datasets`。

只读接口：目录内容来自 `app.services.dataset_catalog`（代码侧声明 + 磁盘实况），
不发起网络请求、不写任何数据。目的是让前端能显示"这份数据来自哪里、什么时候
更新的、什么币种、每列什么单位"，而不是把口径知识留在文档和注释里。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.services.data_pit import (
    PitUnavailableError,
    declare_all,
    known_as_of,
)
from app.services.data_quality import ALL_KINDS, DEFAULT_LOOKBACK_DAYS, check_dataset
from app.services.dataset_catalog import (
    catalog_payload,
    dataset_freshness,
    get_dataset,
    session_days,
)

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

# `Query(...)` 直接写在参数默认值上会触发 flake8-bugbear 的 B008（本仓既有 109 处同
# 类写法，`ruff check app/api/regime.py` 即可复现）。按 ruff 提示的修法改用模块级单例，
# 与 `app/api/ext_data.py:610 _ROW_FILTER_QUERY` 的既有做法一致；只改本文件，不顺手
# 动其它文件的既有告警（CONTRIBUTING §1.3 最小范围）。
_KIND_QUERY = Query(
    None,
    description="只看某几类：missing / duplicate / halt / adjust / jump / conflict",
)
_LOOKBACK_QUERY = Query(DEFAULT_LOOKBACK_DAYS, ge=1, le=120, description="回看的交易日数")
_AS_OF_QUERY = Query(..., description="截止交易日 YYYY-MM-DD；只返回该日**已经可知**的行")
_ROWS_LIMIT_QUERY = Query(20, ge=0, le=500, description="预览行数，0 = 只要计数")
_USABLE_ONLY_QUERY = Query(False, description="只列能按「当时已知」取数的数据集")


@router.get("/catalog")
def list_catalog(
    include_freshness: bool = Query(True, description="是否附带磁盘新鲜度探测"),
    domain: str | None = Query(None, description="只看某个域, 如 kline / ext / financials"),
) -> dict:
    """全部数据集及其来源、币种、逐列单位、PIT 可用性与最新可用日期。"""
    items = catalog_payload(settings.data_dir, include_freshness=include_freshness)
    if domain:
        items = [it for it in items if it.get("domain") == domain]
    anchor = session_days(settings.data_dir)
    return {
        "count": len(items),
        "session_anchor": anchor[-1].isoformat() if anchor else None,
        "items": items,
    }


@router.get("/pit")
def list_pit_declarations(usable_only: bool = _USABLE_ONLY_QUERY) -> dict:
    """全部数据集的「当时已知」声明：口径、可知日列、是否可安全回测、有哪些 caveat。

    这是"能不能拿这张表做历史回测"的单一答案来源；`usable=false` 的条目 `reason`
    说明为什么拒绝，而不是含糊地返回全表。
    """
    items = [d.to_dict() for d in declare_all(settings.data_dir)]
    if usable_only:
        items = [it for it in items if it["usable"]]
    return {
        "count": len(items),
        "usable_count": sum(1 for it in items if it["usable"]),
        "items": items,
    }


@router.get("/{dataset_id}")
def get_dataset_detail(dataset_id: str) -> dict:
    """单个数据集的完整声明 + 新鲜度。"""
    spec = get_dataset(settings.data_dir, dataset_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知数据集: {dataset_id}")
    fresh = dataset_freshness(settings.data_dir, spec)
    return {**spec.to_dict(), "freshness": fresh.to_dict()}


@router.get("/{dataset_id}/freshness")
def get_dataset_freshness(dataset_id: str) -> dict:
    """最新可用日期、落盘更新时间、收盘定版状态与落后交易日数。"""
    spec = get_dataset(settings.data_dir, dataset_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知数据集: {dataset_id}")
    return dataset_freshness(settings.data_dir, spec).to_dict()


@router.get("/{dataset_id}/quality")
def get_dataset_quality(
    dataset_id: str,
    lookback_days: int = _LOOKBACK_QUERY,
    kind: list[str] | None = _KIND_QUERY,
) -> dict:
    """六类数据质量检查：缺失、重复、停牌、复权、异常跳变、跨源冲突。

    只报告不修数据；未覆盖到的口径会出现在 `limitations` 里，而不是被当成"没问题"。
    """
    spec = get_dataset(settings.data_dir, dataset_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知数据集: {dataset_id}")
    kinds = tuple(kind) if kind else None
    if kinds:
        unknown = [k for k in kinds if k not in ALL_KINDS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"未知检查类别 {unknown}；可用值: {list(ALL_KINDS)}",
            )
    return check_dataset(
        settings.data_dir, spec, lookback_days=lookback_days, kinds=kinds
    ).to_dict()


@router.get("/{dataset_id}/pit")
def get_dataset_pit(
    dataset_id: str,
    as_of: str = _AS_OF_QUERY,
    rows_limit: int = _ROWS_LIMIT_QUERY,
) -> dict:
    """截止 `as_of` **已经可知**的行（point-in-time），用于排查/复现历史回测。

    拿不到可知日的数据集返回 409 而不是全表：无法判定"当时是否已知"就等于无法排除
    未来信息，此时返回数据正是前视偏差的来源。
    """
    try:
        day = date.fromisoformat(as_of)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"as_of 需为 YYYY-MM-DD，收到: {as_of}") from None
    try:
        result = known_as_of(settings.data_dir, dataset_id, day)
    except PitUnavailableError as exc:
        message = str(exc)
        status = 404 if message.startswith("未知数据集") else 409
        raise HTTPException(status_code=status, detail=message) from None
    return result.to_dict(rows_limit=rows_limit)
