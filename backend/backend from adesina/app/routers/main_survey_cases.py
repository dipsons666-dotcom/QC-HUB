from __future__ import annotations

import logging
from urllib.parse import unquote

import requests as req_lib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.auth import AuthUser, EDIT_ROLES, get_current_user, require_roles
from backend.app.services.main_survey_cases import (
    apply_main_analysis_correction,
    bulk_push_to_callback,
    assign_main_accompaniment_photo_checks,
    bulk_update_main_case_status,
    create_callback,
    create_main_pending_change,
    bulk_unassign_audio_reviews,
    bulk_unassign_callbacks,
    get_or_create_verification_questions,
    get_callback_case_detail,
    get_main_accompaniment_photo_detail,
    get_main_case_detail,
    get_main_case_navigation,
    list_callbacks,
    list_main_accompaniment_photo_checks,
    list_main_cases,
    record_callback_outcome,
    review_main_pending_change,
    run_main_qc,
    save_verification_response,
    save_accompaniment_verification,
    submit_main_accompaniment_photo_decision,
    update_main_case_status,
    unassign_callback,
    unassign_audio_review,
)
from backend.app.services.surveycto_credentials import resolve_surveycto_credentials_for_media
from backend.app.settings import Settings, get_settings
from backend.app.activity_log import log_activity


router = APIRouter(prefix="/api", tags=["main-survey-cases"])
logger = logging.getLogger(__name__)


class StatusUpdateRequest(BaseModel):
    status: str
    note: str | None = None


class PendingChangeRequest(BaseModel):
    submissionKey: str
    caseId: str | None = None
    issueId: str | None = None
    tableName: str
    rowIdentifier: str | None = None
    fieldName: str
    proposedValue: str
    reason: str = Field(min_length=3)


class PendingChangeReviewRequest(BaseModel):
    decision: str
    note: str | None = None


class CreateCallbackRequest(BaseModel):
    case_id: str
    sampled_flag: bool = False


class CallbackOutcomeRequest(BaseModel):
    outcome_code: str
    outcome_note: str | None = None


class VerificationResponseRequest(BaseModel):
    callback_answer: str = ""
    is_correct: bool


class BulkStatusUpdateRequest(BaseModel):
    submission_keys: list[str]
    status: str
    note: str | None = None


class BulkCallbackRequest(BaseModel):
    submission_keys: list[str]
    assigned_to_role: str | None = None
    assigned_to_user_id: str | None = None


class AccompanimentVerificationRequest(BaseModel):
    verification_status: str
    verification_note: str | None = None


class BulkCaseSearchRequest(BaseModel):
    terms: list[str] = Field(min_length=1, max_length=10_000)
    status: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    category: str | None = None
    cities: str | None = None
    interviewers: str | None = None
    qc_rule: str | None = None
    queue: str | None = None
    assignment: str | None = None
    sort_by: str | None = None
    sort_dir: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100_000)


@router.get("/main-survey/cases")
def main_cases(
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    cities: str | None = None,
    interviewers: str | None = None,
    qc_rule: str | None = None,
    queue: str | None = None,
    assignment: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=2000),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    offset = (page - 1) * page_size
    try:
        return list_main_cases(settings, user, status, search, date_from, date_to, page_size, offset, category, cities, interviewers, qc_rule, queue, assignment, sort_by, sort_dir)
    except Exception as exc:
        logger.exception("Failed to list main survey cases.")
        raise HTTPException(status_code=500, detail=f"Failed to list main survey cases: {exc}") from exc


@router.post("/main-survey/cases/bulk-search")
def bulk_search_main_cases(
    payload: BulkCaseSearchRequest,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    terms = list(dict.fromkeys(term.strip() for term in payload.terms if term.strip()))
    if not terms:
        raise HTTPException(status_code=422, detail="Enter at least one non-empty bulk search value.")
    offset = (payload.page - 1) * payload.page_size
    try:
        return list_main_cases(
            settings,
            user,
            payload.status,
            "\n".join(terms),
            payload.date_from,
            payload.date_to,
            payload.page_size,
            offset,
            payload.category,
            payload.cities,
            payload.interviewers,
            payload.qc_rule,
            payload.queue,
            payload.assignment,
            payload.sort_by,
            payload.sort_dir,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to bulk search main survey cases.")
        raise HTTPException(status_code=500, detail=f"Failed to bulk search main survey cases: {exc}") from exc


@router.get("/main-survey/cases/export")
def export_main_cases(
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    cities: str | None = None,
    interviewers: str | None = None,
    qc_rule: str | None = None,
    queue: str | None = None,
    assignment: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    try:
        result = list_main_cases(settings, user, status, search, date_from, date_to, limit=100_000, offset=0, category=category, cities=cities, interviewers=interviewers, qc_rule=qc_rule, queue=queue, assignment=assignment, sort_by=sort_by, sort_dir=sort_dir)
    except Exception as exc:
        logger.exception("Failed to export main survey cases.")
        raise HTTPException(status_code=500, detail=f"Failed to export main survey cases: {exc}") from exc
    return {"items": result["items"], "total": result["total"]}


@router.get("/main-survey/cases/{submission_key}")
def main_case_detail(
    request: Request,
    submission_key: str,
    include_navigation: bool = True,
    include_audit: bool = True,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    result = get_main_case_detail(settings, user, submission_key, include_navigation=include_navigation, include_audit=include_audit)
    log_activity(
        settings,
        action="case_opened_review_workspace",
        module="main",
        user=user,
        description="Opened main survey case in review workspace.",
        entity_type="case",
        entity_id=submission_key,
        metadata={"workspace": "main_case_detail", "case_id": result.get("case", {}).get("case_id")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/cases/{submission_key}/navigation")
def main_case_navigation(
    submission_key: str,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_case_navigation(settings, submission_key)


@router.post("/main-survey/cases/{submission_key}/status")
def change_main_case_status(
    request: Request,
    submission_key: str,
    payload: StatusUpdateRequest,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    result = update_main_case_status(
        settings,
        user,
        submission_key,
        payload.status,
        payload.note,
        request.headers.get("x-device-id"),
    )
    if result.get("statusChanged"):
        background_tasks.add_task(
            log_activity,
            settings,
            action="case_status_changed",
            module="main",
            user=user,
            description="Changed main survey case status.",
            entity_type="case",
            entity_id=submission_key,
            before_value={"status": result.get("previousStatus")},
            after_value={"status": result.get("newStatus")},
            metadata={"note": payload.note},
            request=request,
        )
    if result.get("statusChanged") and result.get("newStatus") == "approved":
        background_tasks.add_task(log_activity, settings, action="case_approved", module="main", user=user, description="Approved main survey case.", entity_type="case", entity_id=submission_key, request=request)
    elif result.get("statusChanged") and result.get("newStatus") == "rejected":
        background_tasks.add_task(log_activity, settings, action="case_rejected", module="main", user=user, description="Rejected main survey case.", entity_type="case", entity_id=submission_key, request=request)
    elif result.get("statusChanged") and result.get("newStatus") == "corrected":
        background_tasks.add_task(log_activity, settings, action="case_corrected", module="main", user=user, description="Marked main survey case as corrected.", entity_type="case", entity_id=submission_key, request=request)
    elif result.get("statusChanged") and result.get("previousStatus") == "approved" and result.get("newStatus") != "approved":
        background_tasks.add_task(log_activity, settings, action="case_unapproved", module="main", user=user, description="Moved main survey case out of approved state.", entity_type="case", entity_id=submission_key, request=request)
    return result


@router.post("/main-survey/corrections")
def create_main_correction(
    request: Request,
    payload: PendingChangeRequest,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = create_main_pending_change(
        settings,
        user,
        payload.submissionKey,
        payload.caseId,
        payload.issueId,
        payload.tableName,
        payload.rowIdentifier,
        payload.fieldName,
        payload.proposedValue,
        payload.reason,
        request.headers.get("x-device-id"),
    )
    log_activity(
        settings,
        action="correction_submitted",
        module="main",
        user=user,
        description=f"Submitted correction for field {payload.fieldName}.",
        entity_type="case",
        entity_id=payload.submissionKey,
        before_value={"field_name": payload.fieldName},
        after_value={"proposed_value": payload.proposedValue},
        metadata={
            "change_id": result.get("change_id"),
            "case_id": payload.caseId,
            "issue_id": payload.issueId,
            "table_name": payload.tableName,
            "row_identifier": payload.rowIdentifier,
            "reason": payload.reason,
        },
        request=request,
    )
    return result


class AnalysisCorrectionRequest(BaseModel):
    submissionKey: str
    fieldName: str
    oldValue: str
    newValue: str
    questionLabel: str
    correctedByUsername: str | None = None


@router.post("/main-survey/analysis-corrections")
def apply_main_survey_analysis_correction(
    payload: AnalysisCorrectionRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = apply_main_analysis_correction(
        settings,
        user,
        payload.submissionKey,
        payload.fieldName,
        payload.oldValue,
        payload.newValue,
        payload.questionLabel,
        payload.correctedByUsername or "",
    )
    log_activity(
        settings,
        action="analysis_breakdown_use_value",
        module="main",
        user=user,
        description=f"Used replacement value for {payload.fieldName} from analysis breakdown.",
        entity_type="case",
        entity_id=payload.submissionKey,
        before_value={"field_name": payload.fieldName, "value": payload.oldValue},
        after_value={"field_name": payload.fieldName, "value": payload.newValue},
        metadata={
            "submission_key": payload.submissionKey,
            "field_name": payload.fieldName,
            "question_label": payload.questionLabel,
        },
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/main-survey/corrections/{change_id}/review")
def review_main_correction(
    request: Request,
    change_id: str,
    payload: PendingChangeReviewRequest,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = review_main_pending_change(
        settings,
        user,
        change_id,
        payload.decision,
        payload.note,
        request.headers.get("x-device-id"),
    )
    log_activity(
        settings,
        action="correction_approved" if payload.decision == "approved" else "correction_rejected",
        module="main",
        user=user,
        description=f"Reviewed correction request {change_id}.",
        entity_type="qc_issue",
        entity_id=change_id,
        after_value={"decision": payload.decision},
        metadata={"note": payload.note},
        request=request,
    )
    return result


@router.get("/main-survey/callbacks")
def get_callbacks(
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    return {"items": list_callbacks(settings, user)}


@router.post("/main-survey/callbacks")
def create_callback_record(
    payload: CreateCallbackRequest,
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    return create_callback(settings, user, payload.case_id, payload.sampled_flag)


@router.post("/main-survey/cases/bulk-status")
def bulk_change_case_status(
    request: Request,
    payload: BulkStatusUpdateRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    result = bulk_update_main_case_status(settings, user, payload.submission_keys, payload.status, payload.note)
    log_activity(
        settings,
        action="bulk_status_change",
        module="main",
        user=user,
        description="Applied bulk status change to main survey cases.",
        entity_type="case",
        before_value={"submission_keys": payload.submission_keys},
        after_value={"new_status": payload.status, "updated": result.get("updated")},
        metadata={"note": payload.note, "not_found": result.get("notFound")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/callbacks/{case_id}/detail")
def callback_case_detail(
    request: Request,
    case_id: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    result = get_callback_case_detail(settings, user, case_id)
    log_activity(
        settings,
        action="case_opened_review_workspace",
        module="main",
        user=user,
        description="Opened callback case in review workspace.",
        entity_type="case",
        entity_id=case_id,
        metadata={"workspace": "callback", "submission_key": result.get("submission_key")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/callbacks/{case_id}/verification-questions")
def callback_verification_questions(
    case_id: str,
    mode: str = Query("qc"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    return get_or_create_verification_questions(settings, user, case_id, mode)


@router.patch("/main-survey/callbacks/{case_id}/verification-questions/{position}")
def callback_verification_response(
    request: Request,
    case_id: str,
    position: int,
    payload: VerificationResponseRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    result = save_verification_response(
        settings,
        user,
        case_id,
        position,
        payload.callback_answer,
        payload.is_correct,
    )
    log_activity(
        settings,
        action="callback_verification_saved",
        module="main",
        user=user,
        description="Saved callback verification response.",
        entity_type="case",
        entity_id=case_id,
        after_value={"position": position, "callback_answer": payload.callback_answer, "is_correct": payload.is_correct},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/main-survey/callbacks/{case_id}/accompaniment-verification")
def submit_callback_accompaniment_verification(
    request: Request,
    case_id: str,
    payload: AccompanimentVerificationRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    result = save_accompaniment_verification(settings, user, "callback", case_id, None, payload.verification_status, payload.verification_note)
    log_activity(
        settings,
        action="accompaniment_verification_saved",
        module="main",
        user=user,
        description="Saved callback accompaniment verification.",
        entity_type="case",
        entity_id=case_id,
        after_value={"verification_status": payload.verification_status, "verification_note": payload.verification_note},
        metadata={"review_context": "callback", "submission_key": result.get("submission_key")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/main-survey/callbacks/bulk")
def bulk_push_cases_to_callback(
    payload: BulkCallbackRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = bulk_push_to_callback(settings, user, payload.submission_keys, payload.assigned_to_role, payload.assigned_to_user_id)
    log_activity(
        settings,
        action="pushed_to_callback",
        module="main",
        user=user,
        description="Pushed cases to callback queue.",
        entity_type="case",
        after_value={"created": result.get("created"), "already_flagged": result.get("alreadyFlagged")},
        metadata={"submission_keys": payload.submission_keys, "assigned_to_role": payload.assigned_to_role, "assigned_to_user_id": payload.assigned_to_user_id, "not_found": result.get("notFound")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/main-survey/callbacks/{callback_id}/outcome")
def update_callback_outcome(
    request: Request,
    callback_id: str,
    payload: CallbackOutcomeRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    result = record_callback_outcome(settings, user, callback_id, payload.outcome_code, payload.outcome_note)
    log_activity(
        settings,
        action="callback_outcome_saved",
        module="main",
        user=user,
        description="Saved callback outcome.",
        entity_type="case",
        entity_id=str(result.get("case_id") or callback_id),
        after_value={"outcome_code": payload.outcome_code, "outcome_note": payload.outcome_note},
        metadata={"callback_id": callback_id},
        request=request,
        device_id=x_device_id,
    )
    return result


class AudioReviewAssignRequest(BaseModel):
    case_id: str
    assigned_to_role: str | None = None
    assigned_to_user_id: str | None = None


class AudioReviewSubmitRequest(BaseModel):
    quality_rating: str
    reviewer_note: str | None = None


class MainAccompanimentPhotoAssignRequest(BaseModel):
    submissionKeys: list[str]
    assignedToUserId: str


class MainAccompanimentPhotoDecisionRequest(BaseModel):
    status: str
    reviewerNote: str | None = None




@router.post("/main-survey/callbacks/{case_id}/unassign")
def unassign_callback_case(
    request: Request,
    case_id: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = unassign_callback(settings, user, case_id)
    log_activity(
        settings,
        action="unassigned_from_callback",
        module="main",
        user=user,
        description="Unassigned callback case.",
        entity_type="case",
        entity_id=str(result.get("case_id") or case_id),
        after_value=result,
        request=request,
        device_id=x_device_id,
    )
    return result

@router.get("/main-survey/audio-listening")
def get_audio_listening(
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import list_audio_listening
    return {"items": list_audio_listening(settings, user)}


@router.post("/main-survey/audio-listening/assign")
def assign_audio_review(
    request: Request,
    payload: AudioReviewAssignRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import assign_audio_review
    result = assign_audio_review(settings, user, payload.case_id, payload.assigned_to_role, payload.assigned_to_user_id)
    log_activity(
        settings,
        action="pushed_to_audio",
        module="main",
        user=user,
        description="Assigned case to audio review queue.",
        entity_type="case",
        entity_id=str(result.get("case_id") or payload.case_id),
        after_value=result,
        request=request,
        device_id=x_device_id,
    )
    return result


class BulkAudioAssignRequest(BaseModel):
    submission_keys: list[str]
    assigned_to_role: str | None = None
    assigned_to_user_id: str | None = None


@router.post("/main-survey/audio-listening/bulk-assign")
def bulk_assign_audio_review(
    payload: BulkAudioAssignRequest,
    request: Request,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import assign_audio_review

    results = []
    already_assigned = 0
    
    for submission_key in payload.submission_keys:
        result = assign_audio_review(settings, user, submission_key, payload.assigned_to_role, payload.assigned_to_user_id)
        if result.get("audio_id"):
            results.append({"case_id": result.get("case_id"), "audio_id": result["audio_id"]})
        else:
            already_assigned += 1
    
    response = {
        "assigned": len(results),
        "already_assigned": already_assigned,
        "results": results
    }
    log_activity(
        settings,
        action="pushed_to_audio",
        module="main",
        user=user,
        description="Assigned cases to audio listening queue.",
        entity_type="case",
        after_value={"assigned": len(results), "already_assigned": already_assigned},
        metadata={"submission_keys": payload.submission_keys, "assigned_to_role": payload.assigned_to_role, "assigned_to_user_id": payload.assigned_to_user_id},
        request=request,
        device_id=x_device_id,
    )
    return response




@router.post("/main-survey/audio-listening/{audio_id}/unassign")
def unassign_audio_review_case(
    request: Request,
    audio_id: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = unassign_audio_review(settings, user, audio_id)
    log_activity(
        settings,
        action="unassigned_from_audio",
        module="main",
        user=user,
        description="Unassigned audio review case.",
        entity_type="case",
        entity_id=str(result.get("case_id") or audio_id),
        after_value=result,
        request=request,
        device_id=x_device_id,
    )
    return result

@router.post("/main-survey/audio-listening/{audio_id}/review")
def submit_audio_review(
    request: Request,
    audio_id: str,
    payload: AudioReviewSubmitRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import submit_audio_review
    result = submit_audio_review(settings, user, audio_id, payload.quality_rating, payload.reviewer_note)
    log_activity(
        settings,
        action="audio_review_outcome_saved",
        module="main",
        user=user,
        description="Saved audio review outcome.",
        entity_type="case",
        entity_id=audio_id,
        after_value={"quality_rating": payload.quality_rating, "reviewer_note": payload.reviewer_note},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/audio-listening/{audio_id}/detail")
def get_audio_review_detail(
    request: Request,
    audio_id: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import get_audio_review_detail
    result = get_audio_review_detail(settings, user, audio_id)
    log_activity(
        settings,
        action="case_opened_review_workspace",
        module="main",
        user=user,
        description="Opened audio review workspace.",
        entity_type="case",
        entity_id=str(result.get("case_id") or audio_id),
        metadata={"workspace": "audio", "submission_key": result.get("submission_key"), "audio_id": audio_id},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/audio-listening/cases/{case_id}/detail")
def get_audio_review_case_detail(
    case_id: str,
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import get_audio_review_case_detail
    return get_audio_review_case_detail(settings, user, case_id)


@router.post("/main-survey/audio-listening/{audio_id}/accompaniment-verification")
def submit_audio_accompaniment_verification(
    request: Request,
    audio_id: str,
    payload: AccompanimentVerificationRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    detail = get_audio_review_detail(settings, user, audio_id)
    result = save_accompaniment_verification(settings, user, "audio", str(detail.get("case_id") or ""), detail.get("submission_key"), payload.verification_status, payload.verification_note)
    log_activity(
        settings,
        action="accompaniment_verification_saved",
        module="main",
        user=user,
        description="Saved audio accompaniment verification.",
        entity_type="case",
        entity_id=str(detail.get("case_id") or audio_id),
        after_value={"verification_status": payload.verification_status, "verification_note": payload.verification_note},
        metadata={"review_context": "audio", "submission_key": result.get("submission_key"), "audio_id": audio_id},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/enumerator-stats")
def get_enumerator_stats(
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import list_enumerator_stats
    return {"items": list_enumerator_stats(settings, user)}


@router.delete("/main-survey/cases/{submission_key}")
def delete_main_case(
    request: Request,
    submission_key: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("admin")),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import delete_main_case
    result = delete_main_case(settings, user, submission_key)
    log_activity(
        settings,
        action="case_deleted",
        module="main",
        user=user,
        description="Deleted main survey case.",
        entity_type="case",
        entity_id=submission_key,
        after_value=result,
        request=request,
        device_id=x_device_id,
    )
    return result


class BulkDeleteRequest(BaseModel):
    submission_keys: list[str]
    reason: str = Field(min_length=3)


class BulkUnassignRequest(BaseModel):
    submission_keys: list[str]


@router.post("/main-survey/cases/bulk-delete")
def bulk_delete_main_cases(
    request: Request,
    payload: BulkDeleteRequest,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("admin")),
    settings: Settings = Depends(get_settings),
):
    from backend.app.services.main_survey_cases import bulk_delete_main_cases
    result = bulk_delete_main_cases(settings, user, payload.submission_keys, payload.reason)
    log_activity(
        settings,
        action="case_deleted",
        module="main",
        user=user,
        description="Bulk deleted main survey cases.",
        entity_type="case",
        after_value={"deleted": result.get("deleted")},
        metadata={"submission_keys": payload.submission_keys, "not_found": result.get("notFound"), "reason": payload.reason},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/main-survey/callbacks/bulk-unassign")
def bulk_unassign_callback_cases(
    payload: BulkUnassignRequest,
    request: Request,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    result = bulk_unassign_callbacks(settings, user, payload.submission_keys)
    log_activity(settings, action="unassigned_from_callback", module="main", user=user, description="Unassigned callback cases.", entity_type="case", after_value={"unassigned": result.get("unassigned")}, metadata={"submission_keys": payload.submission_keys}, request=request, device_id=x_device_id)
    return result


@router.post("/main-survey/audio-listening/bulk-unassign")
def bulk_unassign_audio_cases(
    payload: BulkUnassignRequest,
    request: Request,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    result = bulk_unassign_audio_reviews(settings, user, payload.submission_keys)
    log_activity(settings, action="unassigned_from_audio", module="main", user=user, description="Unassigned audio review cases.", entity_type="case", after_value={"unassigned": result.get("unassigned")}, metadata={"submission_keys": payload.submission_keys}, request=request, device_id=x_device_id)
    return result


@router.get("/main-survey/accompaniment")
@router.get("/main-survey/incidence-hh-photo")
def main_accompaniment_photo_list(
    show_history: bool = False,
    filter_status: str | None = None,
    filter_date_from: str | None = None,
    filter_date_to: str | None = None,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return {
        "items": list_main_accompaniment_photo_checks(
            settings,
            user,
            show_history=show_history,
            filter_status=filter_status,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
        )
    }


@router.post("/main-survey/accompaniment/assign")
@router.post("/main-survey/incidence-hh-photo/assign")
def main_accompaniment_photo_assign(
    payload: MainAccompanimentPhotoAssignRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    result = assign_main_accompaniment_photo_checks(settings, user, payload.submissionKeys, payload.assignedToUserId)
    log_activity(
        settings,
        action="assign_main_accompaniment_photo_check",
        module="main",
        user=user,
        description="Assigned main accompaniment/photo check cases.",
        entity_type="main_accompaniment_photo_check",
        metadata={"case_ids": payload.submissionKeys, "assigned_to_user_id": payload.assignedToUserId},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/accompaniment/{case_id}/detail")
@router.get("/main-survey/incidence-hh-photo/{case_id}/detail")
def main_accompaniment_photo_detail(
    case_id: str,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_accompaniment_photo_detail(settings, user, case_id)


@router.post("/main-survey/accompaniment/{check_id}/decision")
@router.post("/main-survey/incidence-hh-photo/{check_id}/decision")
def main_accompaniment_photo_decision(
    check_id: str,
    payload: MainAccompanimentPhotoDecisionRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    result = submit_main_accompaniment_photo_decision(settings, user, check_id, payload.status, payload.reviewerNote)
    log_activity(
        settings,
        action="main_accompaniment_photo_check_decision",
        module="main",
        user=user,
        description="Saved main accompaniment/photo check decision.",
        entity_type="main_accompaniment_photo_check",
        entity_id=check_id,
        after_value=result,
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/main-survey/media-proxy/{filename:path}")
def main_survey_media_proxy(
    filename: str,
    surveycto_session: str | None = None,
    settings: Settings = Depends(get_settings),
):
    surveycto_username, surveycto_password = resolve_surveycto_credentials_for_media(settings, surveycto_session)

    media_ref = unquote(filename).strip()
    if media_ref.startswith("http://") or media_ref.startswith("https://"):
        media_url = media_ref
    else:
        bare_filename = media_ref.split("/")[-1].split("\\")[-1]
        media_url = (
            f"https://{settings.surveycto_server}.surveycto.com"
            f"/api/v1/forms/{settings.surveycto_main_form_id}/files/multimedia/{bare_filename}"
        )

    try:
        resp = req_lib.get(
            media_url,
            auth=(surveycto_username, surveycto_password),
            timeout=30,
        )
    except req_lib.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach SurveyCTO: {exc}") from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Media file not found.")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"SurveyCTO returned HTTP {resp.status_code}.")

    return StreamingResponse(iter([resp.content]), media_type=resp.headers.get("content-type", "application/octet-stream"))
