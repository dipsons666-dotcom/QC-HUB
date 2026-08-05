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
    assigned_to_user_id: str | None = None
    assigned_to_name: str | None = None
    assignment_remark: str | None = None
    flag_name: str | None = None
    interviewer: str | None = None
    evidence: str | None = None
    context: dict[str, str] = Field(default_factory=dict)


class ReviewQueueResponse(BaseModel):
    issues: list[ReviewQueueItem]
    count: int


class IssueActionRequest(BaseModel):
    status: Literal["pending_review", "in_progress", "approved", "rejected", "needs_investigation", "resolved"]
    resolution_note: str | None = None


class IssueAssignmentRequest(BaseModel):
    staff_id: str | None = None
    assignment_remark: str | None = Field(default=None, max_length=1000)


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


class RawDataTableColumn(BaseModel):
    name: str
    type: str


class RawDataTableResponse(BaseModel):
    columns: list[RawDataTableColumn]
    rows: list[dict[str, Any]]
    count: int
    limit: int
    offset: int
    has_more: bool


class DecodedQuestionRow(BaseModel):
    category: str
    question: str
    response: str


class DecodedQuestionResponse(BaseModel):
    submission_key: str
    rows: list[DecodedQuestionRow]


class InsightDistributionItem(BaseModel):
    label: str
    count: int
    pct: float


class InsightsOverviewResponse(BaseModel):
    respondent_count: int
    categories: list[InsightDistributionItem]
    sectors: list[InsightDistributionItem]


class AnalysisTableRow(BaseModel):
    label: str
    count: int
    pct: float


class AnalysisTableResponse(BaseModel):
    id: str
    title: str
    question: str
    base: int
    rows: list[AnalysisTableRow]
    cuts: list[dict[str, Any]]


class AnalysisFilterField(BaseModel):
    field: str
    label: str


class AnalysisQuestion(BaseModel):
    id: str
    label: str


class AnalysisTablesResponse(BaseModel):
    category: str
    respondent_count: int
    tables: list[AnalysisTableResponse]
    filters: list[AnalysisFilterField]
    filter_field: str | None = None
    questions: list[AnalysisQuestion]
    question_id: str | None = None


class StaffMemberCreate(BaseModel):
    username: str
    email: str
    role: str = "reviewer"
    password: str = Field(min_length=6)


class StaffMemberResponse(BaseModel):
    staff_id: str
    username: str
    email: str
    role: str
    created_at: datetime


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    staff_id: str
    username: str
    role: str


class AdminDashboardResponse(BaseModel):
    total_survey_count: int
    good_survey_count: int
    outlier_survey_count: int
    raw_submission_count: int
    issue_count: int
    pending_review_count: int
    high_severity_count: int
    medium_severity_count: int
    staff_count: int
    total_reviewed_count: int
    last_sync_at: datetime | None = None


class SurveyCTOSessionRequest(BaseModel):
    surveyctoServer: str | None = None
    surveyctoUsername: str | None = None
    surveyctoPassword: str | None = None
    surveyctoSessionToken: str | None = None
    formId: str | None = None


class SurveyCTOSessionResponse(BaseModel):
    token: str
    expiresInSeconds: int


class SurveyCTOStatusResponse(BaseModel):
    surveycto_server: str
    surveycto_username: str
    surveycto_main_form_id: str
    surveycto_dataset_id: str | None = None
    surveycto_instrument_code: str
    surveycto_date: str
    surveycto_password_configured: bool
    surveycto_endpoint: str
    raw_submission_count: int
    last_sync_at: datetime | None = None
    surveycto_connection_ok: bool
    surveycto_connection_message: str | None = None
    surveycto_pull_ok: bool
    surveycto_pull_message: str | None = None
    surveycto_raw_items: int
    surveycto_normalized_items: int
    surveycto_first_item_keys: list[str] | None = None
    surveycto_sync_stored: int
    surveycto_sync_updated: int
    surveycto_sync_skipped_submission_id: int
    surveycto_sync_message: str | None = None


class ReprocessResponse(BaseModel):
    status: str
    submission_key: str
    message: str
