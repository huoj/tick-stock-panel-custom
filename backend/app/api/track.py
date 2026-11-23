"""前瞻跟踪 API — `/api/track`。

用户要求「建立历史回测与前瞻跟踪 … 保留版本和修改记录，策略变更后不能覆盖旧预测」。
这里只暴露读接口 + 一个"冻结"写接口；评测结果只追加，不提供任何删除/覆盖端点。

口径（与 `app/services/forecast_track.py` 一致）：
- 每条结论一份**只增不改**的版本目录 `data/forecast_track/<kind>/<key>/vN.json`；
- `data_cutoff` 必须 ≤ `as_of`，评测只用 cutoff **之后**的行情；
- 报表把「官方证据足/映射明确」与「推测性主题关联」分开统计。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import forecast_track

router = APIRouter(prefix="/api/track", tags=["track"])

#: 只允许这两类结论进入跟踪（用户要求里明确提到的两种）。
KINDS = ("event", "factor")


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _kind(value: str) -> str:
    if value not in KINDS:
        raise HTTPException(status_code=422, detail=f"未知类别 {value!r}，可选 {list(KINDS)}")
    return value


class RecordRequest(BaseModel):
    key: str = Field(min_length=1, description="结论标识（事件组 ID / 因子 ID）")
    payload: dict[str, Any] = Field(description="结论正文（含 symbol / direction / verification / mapping_level）")
    as_of: str = Field(description="做判断的日期 YYYY-MM-DD")
    data_cutoff: str | None = Field(
        default=None, description="当时数据可用的截止日；必须 ≤ as_of，省略则等于 as_of"
    )
    inputs: Any = Field(default=None, description="输入快照（参数/数据指纹），用于记录口径")
    model_version: str | None = Field(default=None, description="策略/模型版本，便于区分变更前后")


def _date(value: str, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} 不是合法日期: {value!r}") from None


@router.post("/{kind}")
def freeze_record(kind: str, req: RecordRequest, request: Request) -> dict:
    """冻结一条结论（只增不改：同一 key 再冻一次会得到新版本，旧版本保留）。"""
    kind = _kind(kind)
    as_of = _date(req.as_of, "as_of")
    cutoff = _date(req.data_cutoff, "data_cutoff") if req.data_cutoff else None
    try:
        body = forecast_track.record(
            _data_dir(request), kind=kind, key=req.key, payload=req.payload,
            as_of=as_of, data_cutoff=cutoff, inputs=req.inputs,
            model_version=req.model_version,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True,
        "kind": kind,
        "key": req.key,
        "version": body["version"],
        "as_of": body["as_of"],
        "data_cutoff": body["data_cutoff"],
        "inputs_fingerprint": body["inputs_fingerprint"],
        "note": "已冻结；旧版本不会被覆盖，评测结果只追加",
    }


@router.get("/{kind}")
def list_tracked(
    kind: str,
    request: Request,
    key: str | None = Query(None, description="只看某个 key 的版本历史"),
) -> dict:
    """列出已跟踪的结论（含版本号与冻结时的口径）。"""
    kind = _kind(kind)
    data_dir = _data_dir(request)
    keys = [key] if key else forecast_track.list_keys(data_dir, kind)
    rows = []
    for k in keys:
        versions = forecast_track.list_versions(data_dir, kind, k)
        if not versions:
            continue
        rows.append({
            "key": k,
            "versions": [v["version"] for v in versions],
            "latest_version": versions[-1]["version"],
            "as_of": versions[-1]["as_of"],
            "data_cutoff": versions[-1]["data_cutoff"],
            "model_version": versions[-1].get("model_version"),
            "evaluations": sum(len(v.get("evaluations") or []) for v in versions),
            "payload": versions[-1].get("payload"),
        })
    return {"kind": kind, "count": len(rows), "items": rows,
            "note": "版本只增不改；要对比策略变更前后，看 model_version 与 version"}


@router.get("/{kind}/report")
def tracking_report(
    kind: str,
    request: Request,
    evaluate_pending: bool = Query(
        True, description="是否先对尚未评测的版本补做评测（需要 cutoff 之后有足够行情）"
    ),
    horizon_days: int = Query(5, ge=1, le=60, description="前瞻收益的交易日数"),
) -> dict:
    """评测 + 分组报表：方向准确率与映射胜率分开，官方证据/推测性关联分开。"""
    kind = _kind(kind)
    data_dir = _data_dir(request)
    freshly = 0
    skipped: list[str] = []

    if evaluate_pending:
        for k in forecast_track.list_keys(data_dir, kind):
            for version in forecast_track.list_versions(data_dir, kind, k):
                # 只跳过**已成功打分**的 horizon。样本不足的那次也记下来（用于把
                # unscored 显式暴露出来），但不视为"已完成"，等行情够了再重试。
                done = {
                    e.get("horizon_days")
                    for e in (version.get("evaluations") or [])
                    if e.get("net_return") is not None
                }
                if horizon_days in done:
                    continue
                result = forecast_track.evaluate(data_dir, version, horizon_days=horizon_days)
                # 市场状态用 regime 已算好的口径补齐（用户要求按市场状态分组）；
                # 取 entry_date 当天或之前最近一天的状态，取不到就留 None 不猜。
                if result.get("market_state") is None and result.get("entry_date"):
                    try:
                        entry_day = dt.date.fromisoformat(result["entry_date"])
                        result["market_state"] = forecast_track.market_state_for(data_dir, entry_day)
                    except ValueError:
                        pass
                forecast_track.append_evaluation(data_dir, version, result)
                if result.get("net_return") is None:
                    skipped.append(
                        f"{k} v{version['version']}: " + "；".join(result["limitations"])
                    )
                    continue
                freshly += 1

    rep = forecast_track.report(data_dir, kind)
    rep["newly_evaluated"] = freshly
    rep["skipped"] = skipped[:20]
    rep["note"] = (
        "评测只用 data_cutoff 之后的行情；样本不足的版本不会被打分，"
        "会计入 unscored 而不是按 0 处理；"
        "官方证据足/映射明确与推测性主题关联两组的命中率分开统计，不合并"
    )
    return rep


@router.get("/{kind}/{key}")
def version_history(kind: str, key: str, request: Request) -> dict:
    """某个结论的全部版本 + 每次评测（用于核对"旧预测有没有被改写"）。"""
    kind = _kind(kind)
    versions = forecast_track.list_versions(_data_dir(request), kind, key)
    if not versions:
        raise HTTPException(status_code=404, detail=f"没有找到 {kind}/{key} 的跟踪记录")
    return {
        "kind": kind,
        "key": key,
        "versions": [
            {
                "version": v["version"],
                "as_of": v["as_of"],
                "generated_at": v["generated_at"],
                "data_cutoff": v["data_cutoff"],
                "model_version": v.get("model_version"),
                "inputs_fingerprint": v["inputs_fingerprint"],
                "payload": v.get("payload"),
                "evaluations": v.get("evaluations") or [],
            }
            for v in versions
        ],
    }
