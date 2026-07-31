from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class SurveyCTOImportRequest(BaseModel):
    source: str = Field(default="surveycto")
    project_id: str
    submission_id: str
    data: dict[str, Any]


class SurveyCTOImportResponse(BaseModel):
    status: str
    source: str
    submission_id: str
    message: str


class ProcessingResponse(BaseModel):
    status: str
    submission_id: str
    message: str


class TransformationResponse(BaseModel):
    status: str
    submission_id: str
    message: str


class RuleDefinitionCreate(BaseModel):
    name: str
    description: str
    field: str
    severity: str = "medium"
    operator: str = "is_empty"
    threshold: float | None = None
    target_table: str = "clean.main_case"
    rule_type: str = "simple"
    recommended_action: str | None = None


class RuleDefinitionResponse(BaseModel):
    rule_code: str
    instrument_code: str
    target_table: str
    target_field: str | None = None
    severity: str
    rule_type: str
    name: str
    description: str
    operator: str | None = None
    threshold: float | None = None
    recommended_action: str | None = None
    is_active: bool
    created_at: datetime


class QCResultResponse(BaseModel):
    status: str
    submission_id: str
    rule_name: str
    passed: bool
    message: str
    issue_count: int = 0


class ReviewQueueItem(BaseModel):
    issue_id: str
    submission_key: str | None = None
    case_id: str | None = None
    issue_status: str
    status: str
    issue_summary: str
    severity: str
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class ReviewQueueResponse(BaseModel):
    issues: list[ReviewQueueItem]
    count: int


class IssueActionRequest(BaseModel):
    status: Literal["pending_review", "in_progress", "resolved", "rejected"]
    resolution_note: str | None = None


class RawSurveyCTOItem(BaseModel):
    raw_submission_id: str
    instrument_code: str
    submission_key: str
    source_hash: str
    fetched_at: datetime
    raw_payload: dict[str, Any]


class RawSurveyCTOListResponse(BaseModel):
    items: list[RawSurveyCTOItem]
    count: int


class ReprocessResponse(BaseModel):
    status: str
    submission_key: str
    message: str
