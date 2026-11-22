"""保留期治理接口：分区台账、豁免、回收资格明细与回执。

这些是回收链路的领域接口，不套用通用资源路由：

    POST /api/retention/partitions                 登记分区台账
    GET  /api/retention/partitions                 查询分区台账
    POST /api/retention/exemptions                 批准分区豁免
    POST /api/retention/exemptions/{id}/lift       解除豁免
    GET  /api/retention/exemptions                 查询豁免记录
    GET  /api/reclaim_job/{id}/eligibility         查询回收资格明细
    GET  /api/reclaim_job/{id}/receipts            查询执行回执
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import retention_engine as engine
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["保留期治理"])


class PartitionPayload(BaseModel):
    asset_id: str = ""
    partition_key: str = ""
    partition_id: str | None = None
    data_until: str = ""
    retention_days: int | str | None = None
    period_start: str | None = None
    period_end: str | None = None
    declared_expiry: str | None = None


class ExemptionPayload(BaseModel):
    asset_id: str = ""
    partition_ids: list[str] = Field(default_factory=list)
    effective_at: str | None = None
    reason: str = ""
    operator: str = ""
    scope: str = "分区"


def _reclaim_service():
    service = service_for("reclaim_job")
    if service is None:  # pragma: no cover - 注册表固定包含该模块
        raise HTTPException(status_code=500, detail="回收清理服务未注册")
    return service


@router.post("/retention/partitions", response_model=ActionResult)
def register_partition(payload: PartitionPayload) -> ActionResult:
    try:
        row = engine.register_partition(
            asset_id=payload.asset_id,
            partition_key=payload.partition_key,
            data_until=payload.data_until,
            retention_days=payload.retention_days,
            period_start=payload.period_start,
            period_end=payload.period_end,
            declared_expiry=payload.declared_expiry,
            partition_id=payload.partition_id,
        )
    except ValueError as error:
        return ActionResult(ok=False, message=str(error))
    message = (f"分区 {row['分区编号']} 已登记：数据截止 {row['数据截止时间'][:10]}，"
               f"登记到期 {row['到期时间'][:10] or '待规则解析'}")
    return ActionResult(ok=True, message=message, entry=row)


@router.get("/retention/partitions", response_model=dict)
def list_partitions(asset_id: str | None = Query(default=None)) -> dict[str, Any]:
    rows = store.rows(engine.PARTITION_TABLE)
    if asset_id:
        rows = [row for row in rows if str(row.get("资产编号")) == asset_id]
    return {"items": [dict(row) for row in rows], "total": len(rows)}


@router.post("/retention/exemptions", response_model=ActionResult)
def approve_exemption(payload: ExemptionPayload) -> ActionResult:
    try:
        row = engine.approve_exemption(
            asset_id=payload.asset_id,
            partition_ids=payload.partition_ids,
            effective_at=payload.effective_at,
            reason=payload.reason,
            operator=payload.operator,
            scope=payload.scope,
        )
    except ValueError as error:
        return ActionResult(ok=False, message=str(error))
    targets = row.get("分区清单") or ["全部分区"]
    return ActionResult(
        ok=True,
        message=(f"豁免 {row['豁免编号']} 已批准，自 {row['生效时间']} 起保护 "
                 f"{'、'.join(str(item) for item in targets)}"),
        entry=row,
    )


@router.post("/retention/exemptions/{exemption_id}/lift", response_model=ActionResult)
def lift_exemption(exemption_id: int, as_of: str | None = Query(default=None)) -> ActionResult:
    row = engine.lift_exemption(exemption_id, as_of=as_of)
    if row is None:
        return ActionResult(ok=False, message=f"豁免 {exemption_id} 不存在")
    return ActionResult(
        ok=True,
        message=f"豁免 {row['豁免编号']} 已解除，失效时间 {row['失效时间']}",
        entry=row,
    )


@router.get("/retention/exemptions", response_model=dict)
def list_exemptions(asset_id: str | None = Query(default=None)) -> dict[str, Any]:
    rows = store.rows(engine.EXEMPTION_TABLE)
    if asset_id:
        rows = [row for row in rows if str(row.get("资产编号")) == asset_id]
    return {"items": [dict(row) for row in rows], "total": len(rows)}


@router.get("/reclaim_job/{entry_id}/eligibility", response_model=dict)
def list_eligibility(entry_id: int, batch: str | None = Query(default=None)) -> dict[str, Any]:
    service = _reclaim_service()
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"回收任务 {entry_id} 不存在")
    rows = service.eligibility(entry_id, batch=batch)
    return {"items": rows, "total": len(rows), "batch": batch}


@router.get("/reclaim_job/{entry_id}/receipts", response_model=dict)
def list_receipts(entry_id: int) -> dict[str, Any]:
    service = _reclaim_service()
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"回收任务 {entry_id} 不存在")
    rows = service.receipts(entry_id)
    return {"items": rows, "total": len(rows)}
