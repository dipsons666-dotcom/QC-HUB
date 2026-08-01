from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Optional

import pandas as pd
import pyreadstat
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Header
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.auth import AuthUser, get_current_user, require_roles
from backend.app.database import db_connection
from backend.app.services.main_custom_table import (
    CustomTableRequest,
    QuestionSpecIn,
    get_custom_table_section_questions,
    run_custom_table,
)
from backend.app.services.main_survey import (
    _coerce_template_dataframe_for_sav,
    _write_dataframe_to_xlsx,
    build_main_survey_case_export_dataframe,
    build_bht_overview_kpi_export_dataframe,
    build_main_survey_wide_export_dataframe,
    create_main_export,
    clear_main_exports,
    get_filter_options,
    get_bht_map,
    get_bht_map_point_bau5a,
    get_bht_overview,
    get_main_export_file,
    get_main_overview_demographics,
    get_main_survey_ea_overview,
    get_main_survey_answer_breakdown,
    get_main_survey_overview,
    get_main_survey_section,
    get_main_survey_state_ea_summary,
    get_main_survey_verbatims,
    get_main_survey_verbatims_summary,
    list_main_exports,
    manual_main_survey_sync,
    queue_main_export,
    run_queued_main_export,
)
from backend.app.services.main_survey_cases import (
    get_main_qc_productivity,
    get_main_qc_productivity_by_date,
    get_main_qc_productivity_status_totals,
    list_enumerator_productivity_by_date,
    list_enumerator_stats,
    list_main_qc_pending_submission_keys,
    run_main_qc,
    update_main_ea_status,
)
from backend.app.services.surveycto_credentials import create_surveycto_session, resolve_surveycto_credentials
from backend.app.settings import Settings, get_settings
from backend.app.activity_log import log_activity


router = APIRouter(prefix="/api/main-survey", tags=["main-survey"])
logger = logging.getLogger(__name__)
_main_qc_lock = threading.Lock()
_main_qc_status_lock = threading.Lock()
_main_qc_status: dict[str, object] = {
    "status": "idle",
    "percent": 0,
    "message": "Main QC has not started.",
    "createdIssueCount": None,
    "autoApprovedCount": None,
}


def _set_main_qc_status(**updates: object) -> None:
    with _main_qc_status_lock:
        _main_qc_status.update(updates)


def _get_main_qc_status() -> dict[str, object]:
    with _main_qc_status_lock:
        return dict(_main_qc_status)

class QuestionSpecPayload(BaseModel):
    id: str = ""
    label: str = ""
    sectionId: str = ""
    sectionTitle: str = ""
    questionCodes: list[str] = Field(default_factory=list)
    codeLabels: dict[str, str] = Field(default_factory=dict)


class CustomTableBody(BaseModel):
    slug: str = "main"
    topQuestions: list[QuestionSpecPayload] = Field(default_factory=list)
    sideQuestions: list[QuestionSpecPayload] = Field(default_factory=list)
    displayMode: str = "row_pct"
    analysisOptions: list[str] = Field(default_factory=list)
    formatOptions: dict = Field(default_factory=dict)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    months: list[str] = Field(default_factory=list)


class OverviewDemographicsBody(BaseModel):
    months: list[str] = Field(default_factory=list)


class MainExportRequest(BaseModel):
    profile: str
    format: str
    statuses: list[str] | None = None
    finalOutcomeCodes: list[str] | None = None


class EaStatusUpdateRequest(BaseModel):
    status: str
    note: str | None = None


class SurveyCtoCredentialBody(BaseModel):
    surveyctoUsername: str | None = None
    surveyctoPassword: str | None = None
    surveyctoSessionToken: str | None = None
    formId: str | None = None


def _custom_table_body_to_request(body: CustomTableBody) -> CustomTableRequest:
    def conv(q: QuestionSpecPayload) -> QuestionSpecIn:
        return QuestionSpecIn(
            id=q.id,
            label=q.label,
            sectionId=q.sectionId,
            sectionTitle=q.sectionTitle,
            questionCodes=q.questionCodes,
            codeLabels=q.codeLabels,
        )

    return CustomTableRequest(
        slug=body.slug,
        topQuestions=[conv(x) for x in body.topQuestions],
        sideQuestions=[conv(x) for x in body.sideQuestions],
        displayMode=body.displayMode,
        analysisOptions=body.analysisOptions,
        formatOptions=body.formatOptions,
        filters=body.filters,
        months=body.months,
    )


@router.get("/overview")
def main_survey_overview(
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_survey_overview(settings, user)


@router.get("/state-ea-summary")
def main_survey_state_ea_summary(
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_survey_state_ea_summary(settings, user)


@router.get("/eas/{ea_id}")
def main_survey_ea_overview(
    ea_id: str,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_survey_ea_overview(settings, user, ea_id)


@router.post("/eas/{ea_id}/status")
def main_survey_ea_status_update(
    request: Request,
    ea_id: str,
    payload: EaStatusUpdateRequest,
    user: AuthUser = Depends(require_roles("admin", "qc_reviewer")),
    settings: Settings = Depends(get_settings),
):
    result = update_main_ea_status(
        settings,
        user,
        ea_id,
        payload.status,
        payload.note,
        request.headers.get("x-device-id"),
    )
    log_activity(
        settings,
        action="ea_status_changed",
        module="main",
        user=user,
        description="Changed all main survey case statuses within an EA.",
        entity_type="ea",
        entity_id=result.get("eaId"),
        before_value={"submission_keys": result.get("submissionKeys"), "previous_statuses": result.get("previousStatuses")},
        after_value={"status": result.get("newStatus"), "updated": result.get("updated")},
        metadata={"ea_name": result.get("eaName"), "note": payload.note},
        request=request,
    )
    if result.get("newStatus") == "approved":
        log_activity(settings, action="ea_approved", module="main", user=user, description="Approved all cases in EA.", entity_type="ea", entity_id=result.get("eaId"), metadata={"updated": result.get("updated")}, request=request)
    elif result.get("newStatus") == "rejected":
        log_activity(settings, action="ea_rejected", module="main", user=user, description="Rejected all cases in EA.", entity_type="ea", entity_id=result.get("eaId"), metadata={"updated": result.get("updated")}, request=request)
    return result


@router.get("/answer-breakdown")
def main_survey_answer_breakdown(
    slug: str = Query(...),
    variable: str = Query(...),
    code: str = Query(...),
    is_multi: bool = Query(False),
    statuses: Optional[str] = Query(None, description="Comma-separated approval stage values"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    status_list = [item.strip() for item in statuses.split(",") if item.strip()] if statuses else []
    return {"rows": get_main_survey_answer_breakdown(settings, user, slug, variable, code, is_multi, status_list)}


@router.get("/verbatims")
def main_survey_verbatims(
    categories: Optional[str] = Query(None, description="Comma-separated BHT category slugs"),
    questions: Optional[str] = Query(None, description="Comma-separated variable names"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_survey_verbatims(
        settings,
        user,
        categories=categories,
        questions=questions,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/verbatims/summary")
def main_survey_verbatims_summary(
    categories: Optional[str] = Query(None, description="Comma-separated BHT category slugs"),
    questions: Optional[str] = Query(None, description="Comma-separated variable names"),
    search: Optional[str] = Query(None),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_survey_verbatims_summary(
        settings,
        user,
        categories=categories,
        questions=questions,
        search=search,
    )


@router.get("/filter-options")
def main_survey_filter_options(
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_filter_options(settings, user)


@router.get("/bht-overview")
def bht_overview(
    category: str = Query("omnibus", description="BHT category slug"),
    months: Optional[str] = Query(None, description="Comma-separated YYYY-MM values"),
    regions: Optional[str] = Query(None, description="Comma-separated region names"),
    sectors: Optional[str] = Query(None, description="Comma-separated sector names"),
    categories: Optional[str] = Query(None, description="Comma-separated BHT category slugs"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    mlist = [x.strip() for x in months.split(",") if x.strip()] if months else []
    rlist = [x.strip() for x in regions.split(",") if x.strip()] if regions else []
    slist = [x.strip() for x in sectors.split(",") if x.strip()] if sectors else []
    clist = [x.strip() for x in categories.split(",") if x.strip()] if categories else []
    return get_bht_overview(settings, user, category, mlist, rlist, slist, clist)


@router.get("/bht-overview/export")
def bht_overview_kpi_export(
    kpi: str = Query(..., description="KPI export key: total_synced, approved, pending_approval, cancelled_rejected"),
    category: str = Query("all", description="BHT category slug"),
    months: Optional[str] = Query(None, description="Comma-separated YYYY-MM values"),
    regions: Optional[str] = Query(None, description="Comma-separated region names"),
    sectors: Optional[str] = Query(None, description="Comma-separated sector names"),
    categories: Optional[str] = Query(None, description="Comma-separated BHT category slugs"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    mlist = [x.strip() for x in months.split(",") if x.strip()] if months else []
    rlist = [x.strip() for x in regions.split(",") if x.strip()] if regions else []
    slist = [x.strip() for x in sectors.split(",") if x.strip()] if sectors else []
    clist = [x.strip() for x in categories.split(",") if x.strip()] if categories else []
    df = build_bht_overview_kpi_export_dataframe(settings, kpi, category, mlist, rlist, slist, clist)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        path = Path(tmp.name)
    _write_dataframe_to_xlsx(path, df, sheet_name="Overview KPI")
    data = path.read_bytes()
    path.unlink(missing_ok=True)
    safe_kpi = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in kpi.lower()).strip("-") or "overview"
    headers = {"Content-Disposition": f'attachment; filename="bht-overview-{safe_kpi}.xlsx"'}
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


@router.get("/map")
def bht_map(
    category: str = Query("omnibus", description="BHT category slug"),
    months: Optional[str] = Query(None, description="Comma-separated YYYY-MM values"),
    sectors: Optional[str] = Query(None, description="Comma-separated sector names"),
    categories: Optional[str] = Query(None, description="Comma-separated BHT category slugs"),
    limit: int = Query(5000, ge=1, le=10000),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    mlist = [x.strip() for x in months.split(",") if x.strip()] if months else []
    slist = [x.strip() for x in sectors.split(",") if x.strip()] if sectors else []
    clist = [x.strip() for x in categories.split(",") if x.strip()] if categories else []
    return get_bht_map(settings, user, category, mlist, slist, limit, clist)


@router.get("/map-points/{case_id}/bau5a")
def bht_map_point_bau5a(
    case_id: str,
    category: str = Query("omnibus", description="BHT category slug"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_bht_map_point_bau5a(settings, user, case_id, category)


@router.get("/overview-demographics")
def main_survey_overview_demographics_get(
    months: Optional[str] = Query(None, description="Comma-separated YYYY-MM values"),
    states: Optional[str] = Query(None, description="Comma-separated state names"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    mlist = [x.strip() for x in months.split(",") if x.strip()] if months else []
    slist = [x.strip() for x in states.split(",") if x.strip()] if states else []
    return get_main_overview_demographics(settings, user, mlist, slist)


@router.post("/overview-demographics")
def main_survey_overview_demographics_post(
    body: OverviewDemographicsBody = OverviewDemographicsBody(),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return get_main_overview_demographics(settings, user, body.months)


@router.post("/custom-table")
def main_survey_custom_table(
    body: CustomTableBody,
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    try:
        return run_custom_table(settings, user, _custom_table_body_to_request(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/custom-table/sections/{slug}")
def main_survey_custom_table_section(
    slug: str,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    try:
        return get_custom_table_section_questions(settings, user, slug)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/surveycto-session")
def create_main_surveycto_session(
    payload: SurveyCtoCredentialBody,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    return create_surveycto_session(
        settings,
        user,
        payload.surveyctoUsername or "",
        payload.surveyctoPassword or "",
        payload.formId or settings.surveycto_main_form_id,
    )


@router.post("/sync/manual")
def trigger_main_survey_sync(
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
        description="Started manual main survey sync.",
        entity_type="sync",
        metadata={"forwarded_for": request.headers.get("x-forwarded-for")},
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
            description="Manual main survey sync failed.",
            entity_type="sync",
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
        description="Completed manual main survey sync.",
        entity_type="sync",
        after_value={"message": result.get("message")},
        metadata={"sync": result.get("sync")},
        request=request,
        device_id=x_device_id,
    )
    return result


def _run_main_qc_background(
    settings: Settings,
    submission_key: str | None,
    user: AuthUser,
    device_id: str | None,
    client_ip: str | None,
    only_pending: bool,
    batch_limit: int | None,
) -> None:
    try:
        if only_pending and not submission_key:
            pending_keys = list_main_qc_pending_submission_keys(settings)
            total = len(pending_keys)
            if total == 0:
                result = {"createdIssueCount": 0, "autoApprovedCount": 0}
                _set_main_qc_status(
                    status="completed",
                    percent=100,
                    message="Main QC completed. No pending cases needed QC.",
                    createdIssueCount=0,
                    autoApprovedCount=0,
                )
            else:
                size = max(50, min(int(batch_limit or 500), 2_000))
                created_total = 0
                approved_total = 0
                processed = 0
                _set_main_qc_status(status="running", percent=1, message=f"Main QC queued {total:,} pending case(s). Processing in batches of {size:,}.")
                for start in range(0, total, size):
                    batch = pending_keys[start:start + size]
                    batch_number = (start // size) + 1
                    batch_count = (total + size - 1) // size

                    def progress(percent: int, message: str, *, start=start, batch_len=len(batch)) -> None:
                        batch_done = int((max(1, min(99, percent)) / 100) * batch_len)
                        overall_done = min(total, start + batch_done)
                        overall_percent = max(1, min(99, round((overall_done / total) * 100)))
                        _set_main_qc_status(
                            status="running",
                            percent=overall_percent,
                            message=f"Batch {batch_number:,} of {batch_count:,}: {message}",
                        )

                    batch_result = run_main_qc(
                        settings,
                        submission_keys=batch,
                        user=user,
                        device_id=device_id,
                        progress_callback=progress,
                        only_pending=False,
                        batch_limit=None,
                    )
                    created_total += int(batch_result.get("createdIssueCount") or 0)
                    approved_total += int(batch_result.get("autoApprovedCount") or 0)
                    processed += len(batch)
                    _set_main_qc_status(
                        status="running",
                        percent=max(1, min(99, round((processed / total) * 100))),
                        message=f"Processed {processed:,} of {total:,} pending case(s).",
                    )
                result = {"createdIssueCount": created_total, "autoApprovedCount": approved_total}
        else:
            def progress(percent: int, message: str) -> None:
                _set_main_qc_status(status="running", percent=percent, message=message)

            result = run_main_qc(settings, submission_key, user=user, device_id=device_id, progress_callback=progress, only_pending=only_pending, batch_limit=batch_limit)
        _set_main_qc_status(
            status="completed",
            percent=100,
            message="Main QC completed. Refresh the Overview page to see updated figures.",
            createdIssueCount=result.get("createdIssueCount"),
            autoApprovedCount=result.get("autoApprovedCount"),
        )
        log_activity(
            settings,
            action="qc_rule_run_completed",
            module="main",
            user=user,
            description="Completed main survey QC rule run.",
            entity_type="qc_issue",
            entity_id=submission_key,
            after_value={
                "created_issue_count": result.get("createdIssueCount"),
                "auto_approved_count": result.get("autoApprovedCount"),
            },
            metadata={"submission_key": submission_key, "background": True},
            device_id=device_id,
            client_ip=client_ip,
        )
    except Exception as exc:
        logger.exception("Background main survey QC run failed.")
        _set_main_qc_status(status="failed", percent=100, message=f"Main QC failed: {exc}")
        log_activity(
            settings,
            action="qc_rule_run_failed",
            module="main",
            user=user,
            success=False,
            description="Main survey QC rule run failed.",
            entity_type="qc_issue",
            entity_id=submission_key,
            metadata={"submission_key": submission_key, "background": True},
            error_message=str(exc),
            device_id=device_id,
            client_ip=client_ip,
        )
    finally:
        _main_qc_lock.release()


@router.post("/qc/run")
def run_main_survey_qc(
    background_tasks: BackgroundTasks,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    submissionKey: str | None = None,
    fullRun: bool = False,
    batchSize: int = 500,
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer")),
    settings: Settings = Depends(get_settings),
):
    if not _main_qc_lock.acquire(blocking=False):
        current_status = _get_main_qc_status()
        return {
            "status": "already_running",
            "percent": current_status.get("percent", 1),
            "message": "Main QC is already running. Wait for the current run to finish, then refresh the dashboard.",
        }

    client_ip = request.client.host if request.client else None
    _set_main_qc_status(
        status="running",
        percent=1,
        message="Main QC has started. Pending cases will be processed in batches.",
        createdIssueCount=None,
        autoApprovedCount=None,
    )
    log_activity(
        settings,
        action="qc_rule_run_started",
        module="main",
        user=user,
        description="Started main survey QC rule run.",
        entity_type="qc_issue",
        entity_id=submissionKey,
        metadata={"submission_key": submissionKey, "scope": "all" if fullRun else "pending", "batch_size": batchSize},
        request=request,
        device_id=x_device_id,
    )
    safe_batch_size = max(50, min(int(batchSize or 500), 2_000))
    batch_limit = None if fullRun else safe_batch_size
    background_tasks.add_task(_run_main_qc_background, settings, submissionKey, user, x_device_id, client_ip, not fullRun, batch_limit)
    scope_label = "all cases" if fullRun else f"all pending cases in batches of {safe_batch_size:,}"
    return {
        "status": "started",
        "percent": 1,
        "message": f"Main QC has started in the background for {scope_label}. It will auto-approve eligible cases when the run completes.",
    }


@router.get("/qc/status")
def main_survey_qc_status():
    return _get_main_qc_status()


@router.get("/enumerator-stats")
def enumerator_stats(
    group_by: str = Query("enumerator", pattern="^(enumerator|city)$"),
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "supervisor", "client")),
    settings: Settings = Depends(get_settings),
):
    return {"items": list_enumerator_stats(settings, user, group_by)}


@router.get("/enumerator-productivity-by-date")
def enumerator_productivity_by_date(
    group_by: str = Query("enumerator", pattern="^(enumerator|city)$"),
    user: AuthUser = Depends(require_roles("admin", "data_engineer", "qc_reviewer", "supervisor", "client")),
    settings: Settings = Depends(get_settings),
):
    return list_enumerator_productivity_by_date(settings, user, group_by)


@router.get("/qc-productivity")
def main_qc_productivity(
    queue: str = Query("all", description="Queue filter: all, audio, callback"),
    group_by: str = Query("qc_user", description="Group by: qc_user, interviewer, city"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    return {
        "items": get_main_qc_productivity(settings, user, queue, group_by),
        "totals": get_main_qc_productivity_status_totals(settings, queue),
    }


@router.get("/qc-productivity-by-date")
def main_qc_productivity_by_date(
    queue: str = Query("all", description="Queue filter: all, audio, callback"),
    group_by: str = Query("qc_user", description="Group by: qc_user, interviewer, city"),
    user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    return get_main_qc_productivity_by_date(settings, user, queue, group_by)


def _load_xlsform_labels_for_main(settings: Settings) -> tuple[dict, dict]:
    """Load value labels and variable labels for the main survey instrument."""
    value_labels: dict[str, dict[str, str]] = {}
    variable_labels: dict[str, str] = {}
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT list_name, choice_code, choice_label
                FROM reference.xlsform_choice
                WHERE instrument_code = 'main'
                ORDER BY sort_order NULLS LAST
                """
            )
            for row in cur.fetchall():
                lname = str(row["list_name"] or "")
                code = str(row["choice_code"] or "")
                label = str(row["choice_label"] or "")
                if lname and code:
                    value_labels.setdefault(lname, {})[code] = label
            cur.execute(
                """
                SELECT variable_name, question_label
                FROM reference.xlsform_question
                WHERE instrument_code = 'main' AND question_label IS NOT NULL
                """
            )
            for row in cur.fetchall():
                if row["variable_name"]:
                    variable_labels[str(row["variable_name"])] = str(row["question_label"] or "")
    return value_labels, variable_labels


@router.get("/export")
def export_main_survey(
    format: str = Query("xlsx", description="Export format: xlsx, csv, or sav"),
    status: Optional[str] = Query(None, description="Comma-separated approval stages to include"),
    profile: str = Query(
        "case",
        description="Export shape: 'case' (metadata columns) or 'wide' (dictionary-ordered variables from main_case.record)",
    ),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    if format not in ("xlsx", "csv", "sav"):
        raise HTTPException(status_code=400, detail="Format must be 'xlsx', 'csv', or 'sav'.")
    if profile not in ("case", "wide"):
        raise HTTPException(status_code=400, detail="profile must be 'case' or 'wide'.")

    allowed_statuses = ["approved"]
    if profile == "wide":
        df = build_main_survey_wide_export_dataframe(settings, user, allowed_statuses)
    else:
        df = build_main_survey_case_export_dataframe(settings, user, allowed_statuses)
    # Coerce timestamp columns to strings for compatibility
    for col in ("submitted_at", "reviewed_at", "approved_at"):
        if col in df.columns:
            df[col] = df[col].astype(str).replace("None", "").replace("NaT", "")

    if format == "sav":
        # Use template file for exact column structure
        template_path = settings.root_dir / "Main_Survey_Export_Template.sav"

        if not template_path.exists():
            raise HTTPException(status_code=500, detail="Export template not found. Please contact administrator.")
        
        # Read template to get exact column names and structure
        _, meta = pyreadstat.read_sav(str(template_path), row_limit=0)
        template_cols: list[str] = list(meta.column_names)
        
        # Create output dataframe with only columns from template
        out = df.reindex(columns=template_cols).copy()

        # Append status as the last column
        out["status"] = df["approval_stage"] if "approval_stage" in df.columns else None
        
        # Get column labels from template
        raw_labels = list(meta.column_labels) if meta.column_labels else []
        col_labels: list[str] = []
        for i, col in enumerate(template_cols):
            col_labels.append(raw_labels[i] if i < len(raw_labels) else col)
        col_labels.append("Approval Status")
        
        # Get variable-view metadata from template
        raw_vvl = dict(meta.variable_value_labels or {})
        var_measure = dict(meta.variable_measure or {})
        var_display_width = dict(meta.variable_display_width or {})
        original_types = dict(getattr(meta, "original_variable_types", {}) or {})
        var_format = dict(original_types)
        missing_ranges = dict(getattr(meta, "missing_ranges", {}) or {})
        raw_vvl.setdefault(
            "status",
            {
                "approved": "Approved",
                "pending_review": "Pending Review",
                "in_review": "In Review",
                "corrected": "Corrected",
                "rejected": "Rejected",
                "submitted": "Submitted",
            },
        )
        var_measure.setdefault("status", "nominal")
        var_display_width.setdefault("status", 16)
        out, vvl = _coerce_template_dataframe_for_sav(out, list(out.columns), original_types, raw_vvl)
        
        # Normalize dtypes for pyreadstat
        for col in out.columns:
            series = out[col]
            if pd.api.types.is_bool_dtype(series):
                out[col] = series.astype("int64")
            elif pd.api.types.is_object_dtype(series):
                out[col] = series.astype(str).replace({"nan": "", "None": "", "NaT": ""})
        
        # Write to temp SAV file
        with tempfile.NamedTemporaryFile(suffix=".sav", delete=False) as tmp:
            tmp_path = tmp.name
        
        try:
            write_kwargs = {
                "column_labels": col_labels,
                "variable_value_labels": vvl if vvl else None,
                "variable_measure": {col: value for col, value in var_measure.items() if col in out.columns},
                "variable_display_width": {col: value for col, value in var_display_width.items() if col in out.columns},
                "variable_format": {col: value for col, value in var_format.items() if col in out.columns},
                "missing_ranges": {col: value for col, value in missing_ranges.items() if col in out.columns and value},
            }
            write_kwargs = {k: v for k, v in write_kwargs.items() if v}
            pyreadstat.write_sav(out, tmp_path, **write_kwargs)
            
            # Create ZIP file
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
                zip_path = tmp_zip.name
            
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                sav_name = "main_survey_cases.sav"
                zf.write(tmp_path, sav_name)
            
            with open(zip_path, "rb") as f:
                zip_bytes = f.read()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            if 'zip_path' in locals():
                Path(zip_path).unlink(missing_ok=True)
        
        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="main_survey_cases.zip"'},
        )

    buf = io.BytesIO()
    if format == "xlsx":
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            sheet = "Main Survey Wide" if profile == "wide" else "Main Survey Cases"
            _write_dataframe_to_xlsx(tmp_path, df, sheet)
            buf.write(tmp_path.read_bytes())
        finally:
            tmp_path.unlink(missing_ok=True)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "main_survey_wide.xlsx" if profile == "wide" else "main_survey_cases.xlsx"
    else:
        buf.write(df.to_csv(index=False).encode())
        media_type = "text/csv"
        filename = "main_survey_wide.csv" if profile == "wide" else "main_survey_cases.csv"

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/exports")
def main_exports_list(
    user: AuthUser = Depends(require_roles("admin", "client")),
    settings: Settings = Depends(get_settings),
):
    return {"items": list_main_exports(settings, user)}


@router.delete("/exports")
def delete_main_survey_export_history(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("admin", "client")),
    settings: Settings = Depends(get_settings),
):
    result = clear_main_exports(settings, user)
    log_activity(settings, action="export_history_cleared", module="main", user=user, description="Cleared main survey export history.", entity_type="export_history", after_value={"deleted": result.get("deleted")}, request=request, device_id=x_device_id)
    return result


@router.post("/exports")
def create_main_survey_export(
    payload: MainExportRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: AuthUser = Depends(require_roles("admin", "client")),
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    log_activity(
        settings,
        action="export_requested",
        module="main",
        user=user,
        description="Requested main survey export generation.",
        entity_type="export",
        metadata={"profile": payload.profile, "format": payload.format, "statuses": payload.statuses, "finalOutcomeCodes": payload.finalOutcomeCodes},
        request=request,
        device_id=x_device_id,
    )
    try:
        result = queue_main_export(settings, user, payload.profile, payload.format, payload.statuses, payload.finalOutcomeCodes)
    except HTTPException as exc:
        log_activity(
            settings,
            action="export_generation_failure",
            module="main",
            user=user,
            status="failed",
            success=False,
            description="Main survey export queueing failed.",
            entity_type="export",
            metadata={"profile": payload.profile, "format": payload.format, "statuses": payload.statuses, "finalOutcomeCodes": payload.finalOutcomeCodes},
            request=request,
            device_id=x_device_id,
            error_message=str(exc.detail),
        )
        raise

    if result.get("queued") and not result.get("alreadyRunning"):
        background_tasks.add_task(
            run_queued_main_export,
            settings,
            result["exportJobId"],
            user.id,
            user,
            payload.profile,
            payload.format,
            result["statuses"],
            result.get("finalOutcomeCodes") or [],
        )
    log_activity(
        settings,
        action="export_generation_queued",
        module="main",
        user=user,
        description="Queued main survey export generation.",
        entity_type="export",
        entity_id=str(result.get("exportJobId") or ""),
        after_value=result,
        metadata={"profile": payload.profile, "format": payload.format, "statuses": result.get("statuses"), "finalOutcomeCodes": result.get("finalOutcomeCodes")},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.get("/exports/{file_id}/download")
def download_main_survey_export(
    request: Request,
    file_id: str,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(require_roles("admin", "client")),
    settings: Settings = Depends(get_settings),
):
    file_info = get_main_export_file(settings, user, file_id)
    log_activity(
        settings,
        action="export_downloaded",
        module="main",
        user=user,
        description="Downloaded main survey export.",
        entity_type="export",
        entity_id=file_id,
        after_value={"file_name": file_info.get("file_name")},
        request=request,
        device_id=x_device_id,
    )
    return FileResponse(path=file_info["file_path"], filename=file_info["file_name"])


@router.get("/sections/{slug}")
def main_survey_section(
    slug: str,
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    states: Optional[str] = Query(None, description="Comma-separated state names"),
    genders: Optional[str] = Query(None, description="Comma-separated gender values"),
    marital_statuses: Optional[str] = Query(None, description="Comma-separated marital status values"),
    education_levels: Optional[str] = Query(None, description="Comma-separated education level values"),
    statuses: Optional[str] = Query(None, description="Comma-separated approval stage values"),
):
    filters = {
        "states": [v.strip() for v in states.split(",") if v.strip()] if states else [],
        "genders": [v.strip() for v in genders.split(",") if v.strip()] if genders else [],
        "marital_statuses": [v.strip() for v in marital_statuses.split(",") if v.strip()] if marital_statuses else [],
        "education_levels": [v.strip() for v in education_levels.split(",") if v.strip()] if education_levels else [],
        "statuses": [v.strip() for v in statuses.split(",") if v.strip()] if statuses else [],
    }
    try:
        return get_main_survey_section(settings, user, slug, filters=filters)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Main Survey section not found: {slug}") from exc
