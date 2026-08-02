from __future__ import annotations

import requests as req_lib

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Header
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.auth import AuthUser, EDIT_ROLES, get_current_user, require_roles
from backend.app.services.listing import (
    apply_analysis_correction,
    apply_ea_coverage_approvals,
    bulk_assign_picture_check,
    bulk_delete_listing_cases,
    create_export,
    create_pending_change,
    get_export_file,
    get_interviewer_productivity,
    get_interviewer_productivity_by_date,
    get_listing_analysis,
    get_listing_answer_breakdown,
    get_listing_analysis_filter_options,
    get_listing_case_detail,
    get_listing_qc_productivity,
    get_listing_qc_productivity_by_date,
    get_listing_overview,
    get_map_features,
    get_picture_check_detail,
    get_picture_check_flagged_eas,
    get_state_boundaries,
    list_exports,
    queue_export,
    clear_exports,
    list_listing_cases,
    refresh_listing_qc,
    request_ea_review,
    review_pending_change,
    run_queued_export,
    submit_picture_check_decision,
    update_case_status,
)
from backend.app.services.main_survey import manual_main_survey_sync
from backend.app.services.surveycto_credentials import resolve_surveycto_credentials, resolve_surveycto_credentials_for_media
from backend.app.settings import Settings, get_settings
from backend.app.activity_log import log_activity


router = APIRouter(prefix="/api", tags=["listing"])


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


class EaReviewRequest(BaseModel):
    decision: str
    reason: str = Field(min_length=3)


class ExportRequest(BaseModel):
    dataset: str
    format: str
    statuses: list[str] | None = None


@router.get("/settings/surveycto-status")
def surveycto_status(
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return {"configured": bool(settings.surveycto_username)}


@router.get("/listing/surveycto-config-meta")
def surveycto_config_meta(
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Return just the SurveyCTO server name so the frontend can build sign-in links."""
    return {"surveycto_server": settings.surveycto_server}


@router.get("/listing/surveycto-config")
def surveycto_config(
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """Return SurveyCTO multimedia base URLs for both listing and main survey forms.
    The frontend uses these to build direct media URLs, letting the browser handle
    HTTP 401 challenges (same pattern as audio_url fields already stored in the DB).
    """
    base = f"https://{settings.surveycto_server}.surveycto.com/api/v1/forms"
    return {
        "listing_multimedia_base_url": (
            f"{base}/{settings.surveycto_listing_form_id}/files/multimedia"
        ),
        "main_multimedia_base_url": (
            f"{base}/{settings.surveycto_main_form_id}/files/multimedia"
            if settings.surveycto_main_form_id
            else None
        ),
    }


@router.get("/dashboard/overview")
def dashboard_overview(
    state: list[str] | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_listing_overview(settings, user, state)


@router.get("/listing/analysis/filter-options")
def listing_analysis_filter_options(
    state: list[str] | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    # Pass first state for EA population (single-state EA filter)
    single_state = state[0] if state and len(state) == 1 else (state[0] if state else None)
    return get_listing_analysis_filter_options(settings, single_state)


@router.get("/listing/analysis")
def listing_analysis(
    state: list[str] | None = Query(default=None),
    ea_id: list[str] | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_listing_analysis(settings, user, state, ea_id)


class ListingBulkDeleteRequest(BaseModel):
    submission_keys: list[str]


@router.get("/listing/cases")
def listing_cases(
    status: list[str] | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return {"items": list_listing_cases(settings, user, status, search, date_from, date_to)}


@router.get("/listing/answer-breakdown")
def listing_answer_breakdown(
    variable: str,
    code: str,
    state: str | None = None,
    ea_id: str | None = None,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return {"rows": get_listing_answer_breakdown(settings, user, variable, code, state, ea_id)}


@router.post("/listing/cases/bulk-delete")
def listing_bulk_delete(
    body: ListingBulkDeleteRequest,
    user: AuthUser = Depends(require_roles("admin")),
    settings: Settings = Depends(get_settings),
):
    deleted = bulk_delete_listing_cases(settings, body.submission_keys)
    return {"deleted": deleted}


@router.get("/listing/cases/{submission_key}")
def listing_case_detail(
    submission_key: str,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_listing_case_detail(settings, user, submission_key)


@router.post("/listing/sync/manual")
def trigger_manual_sync(
    request: Request,
    payload: SurveyCtoCredentialBody | None = None,
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer")),
    settings: Settings = Depends(get_settings),
):
    x_device_id = request.headers.get("x-device-id")
    log_activity(
        settings,
        action="main_sync_started",
        module="main",
        user=user,
        description="Started manual main sync through legacy listing sync endpoint.",
        entity_type="sync",
        metadata={
            "forwarded_for": request.headers.get("x-forwarded-for"),
            "legacy_endpoint": "/api/listing/sync/manual",
        },
        request=request,
        device_id=x_device_id,
    )
    try:
        sync_username, sync_password = resolve_surveycto_credentials(
            settings,
            user,
            payload.surveyctoSessionToken if payload else None,
            payload.surveyctoUsername if payload else None,
            payload.surveyctoPassword if payload else None,
        )
        result = manual_main_survey_sync(
            settings,
            user,
            x_device_id,
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            sync_username,
            sync_password,
        )
    except HTTPException as exc:
        log_activity(
            settings,
            action="main_sync_failed",
            module="main",
            user=user,
            status="failed",
            success=False,
            description="Manual main sync failed through legacy listing sync endpoint.",
            entity_type="sync",
            metadata={"legacy_endpoint": "/api/listing/sync/manual"},
            request=request,
            device_id=x_device_id,
            error_message=str(exc.detail),
        )
        raise
    log_activity(
        settings,
        action="main_sync_completed",
        module="main",
        user=user,
        description="Completed manual main sync through legacy listing sync endpoint.",
        entity_type="sync",
        after_value={"message": result.get("message")},
        metadata={
            "sync": result.get("sync"),
            "legacy_endpoint": "/api/listing/sync/manual",
        },
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/listing/qc/run")
def run_qc(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    submissionKey: str | None = None,
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "supervisor")),
    settings: Settings = Depends(get_settings),
):
    log_activity(
        settings,
        action="qc_refresh_started",
        module="listing",
        user=user,
        description="Started listing QC refresh.",
        entity_type="qc_issue",
        entity_id=submissionKey,
        metadata={"submission_key": submissionKey},
        request=request,
        device_id=x_device_id,
    )
    qc_result = refresh_listing_qc(settings, submissionKey)
    coverage_approval_result = apply_ea_coverage_approvals(settings, submissionKey)
    result = {
        **qc_result,
        "coverageApproval": coverage_approval_result,
    }
    log_activity(
        settings,
        action="qc_refresh_completed",
        module="listing",
        user=user,
        description="Completed listing QC refresh.",
        entity_type="qc_issue",
        entity_id=submissionKey,
        after_value={"created_issue_count": qc_result.get("createdIssueCount")},
        metadata={"submission_key": submissionKey, "coverage_approval": coverage_approval_result},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/listing/cases/{submission_key}/status")
def change_case_status(
    request: Request,
    submission_key: str,
    payload: StatusUpdateRequest,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    result = update_case_status(
        settings,
        user,
        submission_key,
        payload.status,
        payload.note,
        request.headers.get("x-device-id"),
    )
    log_activity(
        settings,
        action="case_status_changed",
        module="listing",
        user=user,
        description="Changed listing case status.",
        entity_type="case",
        entity_id=submission_key,
        before_value={"status": result.get("previousStatus")},
        after_value={"status": result.get("newStatus")},
        metadata={"note": payload.note},
        request=request,
    )
    return result


@router.post("/listing/eas/{ea_id}/review-request")
def create_ea_review_request(
    request: Request,
    ea_id: str,
    payload: EaReviewRequest,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    return request_ea_review(
        settings,
        user,
        ea_id,
        payload.decision,
        payload.reason,
        request.headers.get("x-device-id"),
    )


@router.post("/listing/corrections")
def create_correction(
    request: Request,
    payload: PendingChangeRequest,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = create_pending_change(
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
        module="listing",
        user=user,
        description=f"Submitted listing correction for field {payload.fieldName}.",
        entity_type="case",
        entity_id=payload.submissionKey,
        before_value={"field_name": payload.fieldName},
        after_value={"proposed_value": payload.proposedValue},
        metadata={"change_id": result.get("change_id"), "issue_id": payload.issueId, "table_name": payload.tableName, "reason": payload.reason},
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


@router.post("/listing/analysis-corrections")
def apply_listing_analysis_correction(
    payload: AnalysisCorrectionRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = apply_analysis_correction(
        settings,
        user,
        payload.submissionKey,
        payload.fieldName,
        payload.oldValue,
        payload.newValue,
        payload.questionLabel,
        payload.correctedByUsername or (user.username if hasattr(user, "username") else ""),
    )
    log_activity(
        settings,
        action="analysis_breakdown_use_value",
        module="listing",
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


@router.post("/listing/corrections/{change_id}/review")
def review_correction(
    request: Request,
    change_id: str,
    payload: PendingChangeReviewRequest,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
    settings: Settings = Depends(get_settings),
):
    result = review_pending_change(
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
        module="listing",
        user=user,
        description=f"Reviewed listing correction request {change_id}.",
        entity_type="qc_issue",
        entity_id=change_id,
        after_value={"decision": payload.decision},
        metadata={"note": payload.note},
        request=request,
    )
    return result


@router.get("/listing/map")
def listing_map(
    state: str | None = None,
    offset: int = 0,
    limit: int = 750,
    north: float | None = None,
    south: float | None = None,
    east: float | None = None,
    west: float | None = None,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_map_features(
        settings,
        user,
        state,
        offset=offset,
        limit=limit,
        north=north,
        south=south,
        east=east,
        west=west,
    )


@router.get("/listing/state-boundaries")
def listing_state_boundaries(
    state: str | None = None,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_state_boundaries(settings, state)


@router.get("/exports")
def exports_list(
    user: AuthUser = Depends(require_roles("admin", "client")),
    settings: Settings = Depends(get_settings),
):
    return {"items": list_exports(settings, user)}


@router.delete("/exports")
def delete_export_history(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("admin", "client")),
    settings: Settings = Depends(get_settings),
):
    result = clear_exports(settings, user)
    log_activity(settings, action="export_history_cleared", module="listing", user=user, description="Cleared listing export history.", entity_type="export_history", after_value={"deleted": result.get("deleted")}, request=request, device_id=x_device_id)
    return result


@router.post("/exports")
def export_dataset(
    payload: ExportRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    user: AuthUser = Depends(require_roles("admin", "client")),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    log_activity(
        settings,
        action="export_requested",
        module="listing",
        user=user,
        description="Requested listing export generation.",
        entity_type="export",
        metadata={"dataset": payload.dataset, "format": payload.format, "statuses": payload.statuses},
        request=request,
        device_id=x_device_id,
    )
    try:
        result = queue_export(settings, user, payload.dataset, payload.format, payload.statuses)
    except HTTPException as exc:
        log_activity(
            settings,
            action="export_generation_failure",
            module="listing",
            user=user,
            status="failed",
            success=False,
            description="Listing export queueing failed.",
            entity_type="export",
            metadata={"dataset": payload.dataset, "format": payload.format, "statuses": payload.statuses},
            request=request,
            device_id=x_device_id,
            error_message=str(exc.detail),
        )
        raise

    background_tasks.add_task(
        run_queued_export,
        settings,
        result["exportJobId"],
        user.id,
        payload.dataset,
        payload.format,
        result["statuses"],
    )
    log_activity(
        settings,
        action="export_generation_queued",
        module="listing",
        user=user,
        description="Queued listing export generation.",
        entity_type="export",
        entity_id=str(result.get("exportJobId") or ""),
        after_value=result,
        metadata={"dataset": payload.dataset, "format": payload.format, "statuses": result.get("statuses")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/exports/{file_id}/download")
def download_export(
    request: Request,
    file_id: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("admin", "client")),
    settings: Settings = Depends(get_settings),
):
    file_info = get_export_file(settings, user, file_id)
    log_activity(
        settings,
        action="export_downloaded",
        module="listing",
        user=user,
        description="Downloaded listing export.",
        entity_type="export",
        entity_id=file_id,
        after_value={"file_name": file_info.get("file_name")},
        request=request,
        device_id=x_device_id,
    )
    return FileResponse(path=file_info["file_path"], filename=file_info["file_name"])


@router.get("/listing/interviewers/stats")
def interviewer_stats(
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "supervisor", "client")),
    settings: Settings = Depends(get_settings),
):
    return {"items": get_interviewer_productivity(settings, user)}


@router.get("/listing/interviewers/productivity-by-date")
def interviewer_productivity_by_date(
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "supervisor", "client")),
    settings: Settings = Depends(get_settings),
):
    return get_interviewer_productivity_by_date(settings, user)


@router.get("/listing/qc-productivity")
def listing_qc_productivity(
    queue: str = Query("all", description="Queue filter: all, audio, callback"),
    user: AuthUser = Depends(require_roles("admin", "qc_reviewer")),
    settings: Settings = Depends(get_settings),
):
    return {"items": get_listing_qc_productivity(settings, user, queue)}


@router.get("/listing/qc-productivity-by-date")
def listing_qc_productivity_by_date(
    queue: str = Query("all", description="Queue filter: all, audio, callback"),
    user: AuthUser = Depends(require_roles("admin", "qc_reviewer")),
    settings: Settings = Depends(get_settings),
):
    return get_listing_qc_productivity_by_date(settings, user, queue)


# ─── Picture Check ─────────────────────────────────────────────────────────────

class PictureCheckAssignRequest(BaseModel):
    submissionKeys: list[str]
    assignedToUserId: str


class PictureCheckDecisionRequest(BaseModel):
    status: str
    reviewerNote: str | None = None


class SurveyCtoCredentialBody(BaseModel):
    surveyctoUsername: str | None = None
    surveyctoPassword: str | None = None
    surveyctoSessionToken: str | None = None


@router.get("/listing/picture-check")
def picture_check_list(
    show_history: bool = False,
    filter_status: str | None = None,
    filter_date_from: str | None = None,
    filter_date_to: str | None = None,
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "client")),
    settings: Settings = Depends(get_settings),
):
    return {
        "items": get_picture_check_flagged_eas(
            settings,
            user,
            show_history=show_history,
            filter_status=filter_status,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
        )
    }


@router.post("/listing/picture-check/assign")
def picture_check_assign(
    payload: PictureCheckAssignRequest,
    request: Request,
    user: AuthUser = Depends(require_roles("admin")),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    result = bulk_assign_picture_check(settings, user, payload.submissionKeys, payload.assignedToUserId)
    log_activity(settings, action="assign_picture_check", module="listing", user=user, description="Assigned picture check cases.", entity_type="picture_check", metadata={"submission_keys": payload.submissionKeys, "assigned_to_user_id": payload.assignedToUserId}, request=request, device_id=x_device_id)
    return result


@router.get("/listing/picture-check/{submission_key}/detail")
def picture_check_detail(
    submission_key: str,
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "client")),
    settings: Settings = Depends(get_settings),
):
    return get_picture_check_detail(settings, user, submission_key)


@router.post("/listing/picture-check/{check_id}/decision")
def picture_check_decision(
    check_id: str,
    payload: PictureCheckDecisionRequest,
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer")),
    settings: Settings = Depends(get_settings),
):
    return submit_picture_check_decision(settings, user, check_id, payload.status, payload.reviewerNote)


# ─── Media Proxy (SurveyCTO attachments) ────────────────────────────────────────

@router.get("/listing/media/{filename:path}")
def listing_media_proxy(
    filename: str,
    surveycto_session: str | None = None,
    settings: Settings = Depends(get_settings),
):
    """Proxy a SurveyCTO media attachment for listing forms (building photos, etc.)."""
    surveycto_username, surveycto_password = resolve_surveycto_credentials_for_media(settings, surveycto_session)

    # Strip any directory prefix (e.g. "media/filename.jpg" → "filename.jpg")
    bare_filename = filename.split("/")[-1].split("\\")[-1]
    media_url = (
        f"https://{settings.surveycto_server}.surveycto.com"
        f"/api/v1/forms/{settings.surveycto_listing_form_id}/files/multimedia/{bare_filename}"
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
        raise HTTPException(status_code=502, detail=f"SurveyCTO returned HTTP {resp.status_code}.")

    content_type = resp.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(
        iter([resp.content]),
        media_type=content_type,
    )
