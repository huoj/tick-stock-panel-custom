"""前瞻结论账本 API —— `/api/forecast-ledger`。

P1-4：把「给用户看过的结论」纳入前瞻跟踪闭环。行动卡 inference、组合风控
减仓建议、三态计划此前都没有冻结/事后打分，页面只能给一个笼统的「历史准确率」。
本模块暴露四个端点，底层全部复用 ``app/services/forecast_calibration.py``
的只追加 JSONL 账本（``data_dir/forecast_ledger/conclusions.jsonl``）：

- ``POST /record``    冻结一条结论（幂等：同冻结字段同 id，不追加第二行）；
- ``POST /evaluate``  事后给结论打 0/1（评估作为新行追加，绝不改结论行）；
- ``GET  /accuracy``  总体命中率 / 按类别拆分；
- ``GET  /calibration`` 分桶校准曲线 + Brier（说 60% 的样本实际多少涨了）。

契约说明：``GET /api/risk/portfolio`` 的模块 docstring 明写「只读接口…不写任何
数据」，所以减仓建议的冻结**不放进 GET**，而由前端在用户确认「我采纳了这条建议」
时显式调 ``POST /api/forecast-ledger/record``；行动卡一侧采用「影子写入 + 失败
只记日志」的旁路（见 ``action_card.py`` 末尾），不因账本失败让整张卡 500。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import forecast_calibration

router = APIRouter(prefix="/api/forecast-ledger", tags=["forecast-ledger"])


def _data_dir(request: Request):
    # 与 manual_holdings / action_card 同源；risk.py 走 repo.store.data_dir，
    # 两者在当前部署指向同一个 data 目录，用 settings 保证账本落点与读侧一致。
    from app.config import settings

    return settings.data_dir


#: 只允许这三类结论进闭环（与专家分析报告 P1-4 一一对应）。
KINDS = ("action_card_inference", "risk_budget_cut", "position_plan")


class RecordRequest(BaseModel):
    kind: str = Field(..., description="action_card_inference / risk_budget_cut / position_plan")
    symbol: str = Field(..., min_length=1, description="标的代码")
    as_of: str = Field(..., description="判断日 YYYY-MM-DD")
    data_cutoff: str = Field(..., description="当时数据只到哪天 YYYY-MM-DD")
    payload: dict = Field(default_factory=dict, description="结论正文；可带 stated_probability(0~1) 与 regime")
    tier: str = Field("speculative", description="solid / speculative")
    inputs_fingerprint: str | None = Field(None, description="输入指纹；省略则由 payload 推导")


class EvaluateRequest(BaseModel):
    conclusion_id: str = Field(..., min_length=1)
    realized: int = Field(..., ge=0, le=1, description="1=兑现 / 0=未兑现")
    realized_note: str = Field("", description="事后说明")


@router.post("/record")
def record(req: RecordRequest, request: Request) -> dict[str, Any]:
    if req.kind not in KINDS:
        raise HTTPException(422, f"未知类别 {req.kind!r}，可选 {list(KINDS)}")
    if req.tier not in ("solid", "speculative"):
        raise HTTPException(422, "tier 只能是 solid / speculative")
    try:
        return forecast_calibration.record_conclusion(
            data_dir=_data_dir(request),
            kind=req.kind,
            symbol=req.symbol.strip().upper(),
            as_of=req.as_of,
            data_cutoff=req.data_cutoff,
            payload=req.payload,
            inputs_fingerprint=req.inputs_fingerprint,
            tier=req.tier,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/evaluate")
def evaluate(req: EvaluateRequest, request: Request) -> dict[str, Any]:
    try:
        return forecast_calibration.evaluate_conclusion(
            data_dir=_data_dir(request),
            conclusion_id=req.conclusion_id,
            realized=req.realized,
            realized_note=req.realized_note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/accuracy")
def accuracy(request: Request) -> dict[str, Any]:
    return forecast_calibration.accuracy_summary(data_dir=_data_dir(request))


@router.get("/calibration")
def calibration(
    request: Request,
    bucket_size: float = Query(0.1, gt=0, le=1.0),
) -> dict[str, Any]:
    return forecast_calibration.calibration_curve(
        data_dir=_data_dir(request), bucket_size=bucket_size
    )
