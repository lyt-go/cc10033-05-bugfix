"""Schema匹配 → 质量抽检 → 数据导出 样本链路专用接口。

通用资源路由只承载列表/登记/动作；样本版本、领取、字段确认、幂等受理、
导出放行追溯等跨模块领域动作统一在这里分派到对应服务。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import SERVICES

router = APIRouter(prefix="/api/sample-flow", tags=["样本链路"])


class SamplePayload(BaseModel):
    sample: dict[str, Any] = Field(default_factory=dict)
    batch: str | None = None
    note: str | None = None
    operator: str = "运营"


class MappingPayload(BaseModel):
    # key=目标Schema字段，value=来源字段，例如 {"title": "headline"}
    mapping: dict[str, str] = Field(default_factory=dict)


class ClaimPayload(BaseModel):
    sample_id: str
    operator: str = "运营"


class ConfirmPayload(BaseModel):
    sample_id: str
    field: str
    expected_value: Any | None = None
    passed: bool = True
    remark: str | None = None
    request_id: str | None = None
    operator: str = "运营"


class SubmitReviewPayload(BaseModel):
    request_id: str | None = None
    operator: str = "运营"


class ExportApplyPayload(BaseModel):
    quality_check_id: int
    applicant: str = "导出负责人"
    export_format: str = "CSV"


@router.post("/schema/{schema_id}/mapping")
def define_mapping(schema_id: int, payload: MappingPayload) -> dict[str, Any]:
    entry = SERVICES["schema_match"].define_mapping(schema_id, payload.mapping)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"匹配配置 {schema_id} 不存在")
    return {
        "ok": True,
        "config_version": entry["_config_version"],
        "mapping": entry["_field_mapping"],
        "字段对应表": entry["字段对应表"],
    }


@router.post("/schema/{schema_id}/preview")
def preview_match(schema_id: int, payload: SamplePayload) -> dict[str, Any]:
    service = SERVICES["schema_match"]
    preview = service.preview_match(schema_id, payload.sample)
    if preview is None:
        raise HTTPException(status_code=404, detail=f"匹配配置 {schema_id} 不存在")
    return {"ok": True, "preview": preview}


@router.post("/schema/{schema_id}/submit")
def submit_sample(schema_id: int, payload: SamplePayload) -> dict[str, Any]:
    if not payload.batch:
        raise HTTPException(status_code=400, detail="送检必须携带数据批次 batch")
    service = SERVICES["schema_match"]
    sample = service.submit_sample(
        schema_id, payload.batch, payload.sample,
        note=payload.note or "", operator=payload.operator,
    )
    if sample is None:
        raise HTTPException(status_code=404, detail=f"匹配配置 {schema_id} 不存在")
    return {"ok": True, "sample": sample}


@router.get("/quality/{quality_id}/samples")
def available_samples(quality_id: int) -> dict[str, Any]:
    result = SERVICES["quality_check"].available_samples(quality_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {quality_id} 不存在")
    return {"ok": True, **result}


@router.post("/quality/{quality_id}/claim")
def claim_sample(quality_id: int, payload: ClaimPayload) -> dict[str, Any]:
    detail = SERVICES["quality_check"].claim_sample(
        quality_id, payload.sample_id, payload.operator)
    if detail is None:
        raise HTTPException(status_code=404, detail="抽检任务或样本不存在，或样本属于其他批次")
    return {"ok": True, "detail": detail}


@router.get("/quality/{quality_id}/detail")
def review_detail(quality_id: int) -> dict[str, Any]:
    detail = SERVICES["quality_check"].get_review_detail(quality_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {quality_id} 不存在")
    return {"ok": True, "detail": detail}


@router.post("/quality/{quality_id}/confirm")
def confirm_field(quality_id: int, payload: ConfirmPayload) -> dict[str, Any]:
    result = SERVICES["quality_check"].confirm_field(
        quality_id,
        payload.sample_id,
        payload.field,
        operator=payload.operator,
        expected_value=payload.expected_value,
        passed=payload.passed,
        remark=payload.remark or "",
        request_id=payload.request_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {quality_id} 不存在")
    return result


@router.post("/quality/{quality_id}/submit-review")
def submit_review(quality_id: int, payload: SubmitReviewPayload) -> dict[str, Any]:
    result = SERVICES["quality_check"].submit_review(
        quality_id, operator=payload.operator, request_id=payload.request_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"抽检任务 {quality_id} 不存在")
    return result


@router.post("/export/apply")
def apply_export(payload: ExportApplyPayload) -> dict[str, Any]:
    return SERVICES["export_dataset"].apply_for_export(
        payload.quality_check_id,
        applicant=payload.applicant,
        export_format=payload.export_format,
    )


@router.get("/export/{export_id}/trace")
def export_trace(export_id: int) -> dict[str, Any]:
    result = SERVICES["export_dataset"].get_export_trace(export_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"导出任务 {export_id} 不存在")
    return {"ok": True, "trace": result}
