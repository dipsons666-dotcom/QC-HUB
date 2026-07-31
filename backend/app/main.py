import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Base, create_schemas, get_db, get_engine
from .models import ImportJob, IssueQueue, MainCase, RawSurveyCTOSubmission, RuleDefinition, RuleResult
from .schemas import (
    IssueActionRequest,
    ProcessingResponse,
    QCResultResponse,
    ReviewQueueItem,
    ReviewQueueResponse,
    RuleDefinitionCreate,
    RuleDefinitionResponse,
    SurveyCTOImportRequest,
    SurveyCTOImportResponse,
    TransformationResponse,
)

app = FastAPI(title="QC Flags Platform", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event() -> None:
    create_schemas()
    Base.metadata.create_all(bind=get_engine())


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


def _append_jsonl(path_str: str, payload: dict[str, Any]) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _rewrite_jsonl(path_str: str, records: list[dict[str, Any]]) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _hash_payload(payload: Any) -> str:
    normalized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get_survey_platform_config() -> dict[str, str]:
    return {
        "server": os.getenv("SURVEYCTO_SERVER", ""),
        "username": os.getenv("SURVEYCTO_USERNAME", ""),
        "password": os.getenv("SURVEYCTO_PASSWORD", ""),
        "form_id": os.getenv("SURVEYCTO_MAIN_FORM_ID", ""),
        "instrument_code": os.getenv("SURVEYCTO_INSTRUMENT_CODE", "main"),
        "date": os.getenv("SURVEYCTO_DATE", datetime.now(timezone.utc).strftime("%Y%m%d")),
    }


def fetch_submission_payloads() -> list[dict[str, Any]]:
    config = get_survey_platform_config()
    if not config["server"] or not config["username"] or not config["password"] or not config["form_id"]:
        return []

    surveycto_date = str(config["date"]).strip()
    if len(surveycto_date) != 8 or not surveycto_date.isdigit():
        surveycto_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    url = f"https://{config['server']}.surveycto.com/api/v2/forms/data/wide/json/{config['form_id']}"
    response = requests.get(
        url,
        auth=(config["username"], config["password"]),
        params={"date": surveycto_date},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("items"), list):
            items = payload["items"]
        else:
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    normalized_items: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized_items.append(item)
    return normalized_items


def _evaluate_rule_against_case(case_record: dict[str, Any], rule_record: dict[str, Any]) -> tuple[bool, str]:
    field_name = rule_record.get("field", "")
    field_value = case_record.get(field_name)
    operator = rule_record.get("operator", "is_empty")
    threshold = rule_record.get("threshold")

    if operator == "is_empty":
        passed = field_value not in (None, "")
        message = "QC check passed" if passed else f"Field '{field_name}' is empty"
        return passed, message

    if operator == "min_value":
        if field_value is None:
            return False, f"Field '{field_name}' is missing"
        passed = field_value >= threshold if threshold is not None else True
        message = "QC check passed" if passed else f"Field '{field_name}' is below minimum {threshold}"
        return passed, message

    if operator == "max_value":
        if field_value is None:
            return False, f"Field '{field_name}' is missing"
        passed = field_value <= threshold if threshold is not None else True
        message = "QC check passed" if passed else f"Field '{field_name}' is above maximum {threshold}"
        return passed, message

    if operator == "equals":
        passed = field_value == threshold
        message = "QC check passed" if passed else f"Field '{field_name}' does not equal {threshold}"
        return passed, message

    passed = True
    message = "QC check passed"
    return passed, message


@app.post("/api/import/surveycto", response_model=SurveyCTOImportResponse, status_code=202)
def import_surveycto(payload: SurveyCTOImportRequest, db: Session = Depends(get_db)) -> SurveyCTOImportResponse:
    received_at = datetime.now(timezone.utc)
    payload_data = payload.data or {}
    source_hash = _hash_payload(payload_data)

    raw_submission = RawSurveyCTOSubmission(
        instrument_code=str(payload.project_id or "main"),
        submission_key=payload.submission_id,
        source_hash=source_hash,
        raw_payload=payload_data,
        fetched_at=received_at,
    )
    import_job = ImportJob(
        submission_key=payload.submission_id,
        instrument_code=str(payload.project_id or "main"),
        status="queued",
    )
    db.add(raw_submission)
    db.add(import_job)
    db.commit()
    db.refresh(raw_submission)
    db.refresh(import_job)

    record = {
        "source": payload.source,
        "project_id": payload.project_id,
        "submission_id": payload.submission_id,
        "received_at": received_at.isoformat(),
        "data": payload_data,
    }
    _append_jsonl(os.getenv("RAW_IMPORT_STORE_PATH", "./raw_imports.jsonl"), record)
    _append_jsonl(
        os.getenv("IMPORT_QUEUE_PATH", "./import_queue.jsonl"),
        {
            "submission_id": payload.submission_id,
            "project_id": payload.project_id,
            "status": "queued",
            "queued_at": received_at.isoformat(),
        },
    )

    return SurveyCTOImportResponse(
        status="accepted",
        source=payload.source,
        submission_id=payload.submission_id,
        message="SurveyCTO payload accepted and queued for downstream QC processing.",
    )


@app.post("/api/import/survey-platform/sync")
def sync_survey_platform_submissions(db: Session = Depends(get_db)) -> dict[str, Any]:
    batch_size = max(1, int(os.getenv("SURVEYCTO_BATCH_SIZE", "100")))
    items = fetch_submission_payloads()[:batch_size]
    stored = 0
    for item in items:
        submission_id = str(item.get("submission_id") or item.get("id") or item.get("KEY") or item.get("submissionkey") or "")
        if not submission_id:
            continue

        payload_data = item.get("data") or item.get("payload") or item
        if not isinstance(payload_data, dict):
            payload_data = {"value": payload_data}

        instrument_code = str(item.get("instrument_code") or get_survey_platform_config()["instrument_code"])
        source_hash = _hash_payload(payload_data)

        existing_submission = (
            db.execute(
                select(RawSurveyCTOSubmission)
                .where(RawSurveyCTOSubmission.submission_key == submission_id)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if existing_submission is None:
            raw_submission = RawSurveyCTOSubmission(
                instrument_code=instrument_code,
                submission_key=submission_id,
                source_hash=source_hash,
                raw_payload=payload_data,
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(raw_submission)
            db.flush()
            import_job = ImportJob(
                submission_key=submission_id,
                instrument_code=instrument_code,
                status="queued",
            )
            db.add(import_job)
            stored += 1
        else:
            existing_submission.instrument_code = instrument_code
            existing_submission.raw_payload = payload_data
            existing_submission.source_hash = source_hash
            existing_submission.fetched_at = datetime.now(timezone.utc)

    db.commit()
    return {"status": "synced", "source": "survey_platform", "stored": stored}


@app.post("/api/import/process-next", response_model=ProcessingResponse)
def process_next_import(db: Session = Depends(get_db)) -> ProcessingResponse:
    job = (
        db.execute(
            select(ImportJob)
            .where(ImportJob.status == "queued")
            .order_by(ImportJob.queued_at.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="No queued imports available")

    processed_at = datetime.now(timezone.utc)
    job.status = "processed"
    job.processed_at = processed_at
    db.commit()

    queue_path = os.getenv("IMPORT_QUEUE_PATH", "./import_queue.jsonl")
    if os.path.exists(queue_path):
        with open(queue_path, "r", encoding="utf-8") as handle:
            queue_records = [json.loads(line) for line in handle if line.strip()]
        remaining = [record for record in queue_records if record.get("submission_id") != job.submission_key]
        _rewrite_jsonl(queue_path, remaining)

    _append_jsonl(
        os.getenv("PROCESSED_IMPORT_STORE_PATH", "./processed_imports.jsonl"),
        {
            "submission_id": job.submission_key,
            "project_id": job.instrument_code,
            "status": "processed",
            "processed_at": processed_at.isoformat(),
        },
    )

    return ProcessingResponse(
        status="processed",
        submission_id=job.submission_key,
        message="Queued import has been moved into the processing stage.",
    )


@app.post("/api/import/transform-next", response_model=TransformationResponse)
def transform_next_import(db: Session = Depends(get_db)) -> TransformationResponse:
    job = (
        db.execute(
            select(ImportJob)
            .where(ImportJob.status == "processed")
            .where(ImportJob.transformed_at.is_(None))
            .order_by(ImportJob.processed_at.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="No processed imports available")

    raw_submission = (
        db.execute(
            select(RawSurveyCTOSubmission)
            .where(RawSurveyCTOSubmission.submission_key == job.submission_key)
            .order_by(RawSurveyCTOSubmission.fetched_at.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if raw_submission is None:
        raise HTTPException(status_code=404, detail="No raw submission found for the selected import")

    case_payload = raw_submission.raw_payload or {}
    if not isinstance(case_payload, dict):
        case_payload = {}

    answers = case_payload.get("answers") if isinstance(case_payload.get("answers"), dict) else {}
    gps = case_payload.get("gps") if isinstance(case_payload.get("gps"), dict) else {}
    transformed_payload = {
        "submission_id": job.submission_key,
        "project_id": job.instrument_code,
        "case_id": case_payload.get("case_id") or job.submission_key,
        "respondent_name": case_payload.get("respondent_name"),
        "age": answers.get("age"),
        "gender": answers.get("gender"),
        "gps_lat": gps.get("lat"),
        "gps_lon": gps.get("lon"),
        "raw_payload": case_payload,
    }

    canonical_case = MainCase(
        submission_key=job.submission_key,
        case_id=transformed_payload["case_id"],
        review_status="pending_review",
        record=transformed_payload,
    )
    db.add(canonical_case)
    job.transformed_at = datetime.now(timezone.utc)
    db.commit()

    _append_jsonl(os.getenv("TRANSFORMED_CASE_STORE_PATH", "./transformed_cases.jsonl"), transformed_payload)

    return TransformationResponse(
        status="transformed",
        submission_id=job.submission_key,
        message="Processed import has been transformed into a canonical case record.",
    )


@app.post("/api/qc/rules", response_model=RuleDefinitionResponse)
def create_rule_definition(payload: RuleDefinitionCreate, db: Session = Depends(get_db)) -> RuleDefinitionResponse:
    rule = RuleDefinition(
        name=payload.name,
        instrument_code=str(payload.target_table or "main"),
        target_table=payload.target_table or "clean.main_case",
        target_field=payload.field,
        severity=payload.severity,
        rule_type=payload.rule_type,
        description=payload.description,
        operator=payload.operator,
        threshold=payload.threshold,
        recommended_action=payload.recommended_action,
        is_active=True,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    _append_jsonl(
        os.getenv("RULE_DEFINITION_STORE_PATH", "./rule_definitions.jsonl"),
        {
            "id": rule.rule_code,
            "name": rule.name,
            "description": rule.description,
            "field": rule.target_field,
            "severity": rule.severity,
            "operator": rule.operator,
            "threshold": rule.threshold,
        },
    )

    return RuleDefinitionResponse(
        rule_code=rule.rule_code,
        instrument_code=rule.instrument_code,
        target_table=rule.target_table,
        target_field=rule.target_field,
        severity=rule.severity,
        rule_type=rule.rule_type,
        name=rule.name,
        description=rule.description,
        operator=rule.operator,
        threshold=rule.threshold,
        recommended_action=rule.recommended_action,
        is_active=rule.is_active,
        created_at=rule.created_at,
    )


@app.post("/api/qc/evaluate-next", response_model=QCResultResponse)
def evaluate_next_rule(db: Session = Depends(get_db)) -> QCResultResponse:
    case = (
        db.execute(select(MainCase).order_by(MainCase.created_at.asc()).limit(1)).scalars().first()
    )
    if case is None:
        raise HTTPException(status_code=404, detail="No transformed cases available")

    rules = db.execute(select(RuleDefinition).where(RuleDefinition.is_active.is_(True))).scalars().all()
    if not rules:
        raise HTTPException(status_code=404, detail="No QC rule definitions available")

    case_payload = case.record or {}
    if not isinstance(case_payload, dict):
        case_payload = {}

    issue_count = 0
    first_failing_rule_name = None
    for rule in rules:
        rule_payload = {
            "id": rule.rule_code,
            "name": rule.name,
            "field": rule.target_field,
            "severity": rule.severity,
            "operator": rule.operator,
            "threshold": rule.threshold,
        }
        passed, message = _evaluate_rule_against_case(case_payload, rule_payload)
        qc_result = RuleResult(
            rule_code=rule.rule_code,
            instrument_code=rule.instrument_code,
            submission_key=case.submission_key,
            case_id=case.case_id,
            table_name=rule.target_table,
            field_name=rule.target_field,
            severity=rule.severity,
            result_status="open" if not passed else "passed",
            result_message=message,
        )
        db.add(qc_result)
        db.commit()

        _append_jsonl(
            os.getenv("QC_RESULT_STORE_PATH", "./qc_results.jsonl"),
            {
                "submission_id": case.submission_key,
                "rule_id": rule.rule_code,
                "rule_name": rule.name,
                "severity": rule.severity,
                "passed": passed,
                "message": message,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        if not passed:
            issue_count += 1
            if first_failing_rule_name is None:
                first_failing_rule_name = rule.name
            issue = IssueQueue(
                rule_result_id=qc_result.rule_result_id,
                instrument_code=rule.instrument_code,
                submission_key=case.submission_key,
                case_id=case.case_id,
                issue_status="pending_review",
                issue_summary=message,
                severity=rule.severity,
            )
            db.add(issue)
            db.commit()
            _append_jsonl(
                os.getenv("ISSUE_QUEUE_STORE_PATH", "./issue_queue.jsonl"),
                {
                    "id": issue.issue_id,
                    "submission_id": case.submission_key,
                    "case_id": case.case_id,
                    "rule_id": rule.rule_code,
                    "rule_name": rule.name,
                    "severity": rule.severity,
                    "status": "pending_review",
                    "created_at": issue.created_at.isoformat() if issue.created_at else datetime.now(timezone.utc).isoformat(),
                    "message": message,
                },
            )

    return QCResultResponse(
        status="flagged" if issue_count > 0 else "passed",
        submission_id=case.submission_key,
        rule_name=first_failing_rule_name or rules[0].name,
        passed=issue_count == 0,
        message=f"Evaluated {len(rules)} rule(s); created {issue_count} issue(s)",
        issue_count=issue_count,
    )


@app.get("/api/qc/review-queue", response_model=ReviewQueueResponse)
def get_review_queue(db: Session = Depends(get_db)) -> ReviewQueueResponse:
    issues = (
        db.execute(select(IssueQueue).order_by(IssueQueue.created_at.asc())).scalars().all()
    )
    return ReviewQueueResponse(
        issues=[
            ReviewQueueItem(
                issue_id=issue.issue_id,
                submission_key=issue.submission_key,
                case_id=issue.case_id,
                issue_status=issue.issue_status,
                status=issue.issue_status,
                issue_summary=issue.issue_summary,
                severity=issue.severity if hasattr(issue, "severity") else "medium",
                created_at=issue.created_at,
                updated_at=issue.updated_at,
                resolved_at=issue.resolved_at,
                resolution_note=issue.resolution_note,
            )
            for issue in issues
        ],
        count=len(issues),
    )


@app.post("/api/qc/issues/{issue_id}/action", response_model=ReviewQueueItem)
def update_issue_action(issue_id: str, payload: IssueActionRequest, db: Session = Depends(get_db)) -> ReviewQueueItem:
    issue = db.get(IssueQueue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")

    issue.issue_status = payload.status
    issue.resolution_note = payload.resolution_note
    if payload.status in {"resolved", "rejected"}:
        issue.resolved_at = datetime.now(timezone.utc)
    issue.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(issue)

    return ReviewQueueItem(
        issue_id=issue.issue_id,
        submission_key=issue.submission_key,
        case_id=issue.case_id,
        issue_status=issue.issue_status,
        status=issue.issue_status,
        issue_summary=issue.issue_summary,
        severity=issue.severity if hasattr(issue, "severity") else "medium",
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        resolved_at=issue.resolved_at,
        resolution_note=issue.resolution_note,
    )
