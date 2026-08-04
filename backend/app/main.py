import csv
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logging.basicConfig(level=logging.INFO)
from typing import Any

import requests
import threading
import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, create_schemas, get_db, get_engine, get_session_local
from .services.surveycto_credentials import (
    create_surveycto_session,
    resolve_surveycto_credentials,
)
from .models import (
    ImportJob,
    IssueQueue,
    MainCase,
    RawSurveyCTOSubmission,
    RuleDefinition,
    RuleResult,
    StaffMember,
)
from .decoded_questions import decode_submission_to_question_rows
from .services.table_engine import (
    PREFERRED_FILTER_FIELDS,
    available_filter_fields,
    build_question_catalog,
    build_tables,
    load_metadata,
    load_questionnaire_xlsform,
    load_template_registry,
    _answers,
    _answer_tokens,
    _choice_label,
    _question,
    _options,
)
from .services.table_export import write_analysis_workbook
from .services.main_survey_qc import evaluate_main_survey
from .services.passwords import hash_password, verify_password
from .schemas import (
    AdminDashboardResponse,
    AnalysisTableResponse,
    AnalysisTablesResponse,
    AnalysisFilterField,
    AnalysisQuestion,
    DecodedQuestionResponse,
    DecodedQuestionRow,
    InsightDistributionItem,
    InsightsOverviewResponse,
    IssueActionRequest,
    IssueAssignmentRequest,
    LoginRequest,
    LoginResponse,
    ProcessingResponse,
    QCResultResponse,
    RawSurveyCTOItem,
    RawSurveyCTOListResponse,
    ReviewQueueItem,
    ReviewQueueResponse,
    RuleDefinitionCreate,
    RuleDefinitionResponse,
    StaffMemberCreate,
    StaffMemberResponse,
    SurveyCTOImportRequest,
    SurveyCTOImportResponse,
    SurveyCTOSessionRequest,
    SurveyCTOSessionResponse,
    SurveyCTOStatusResponse,
    TransformationResponse,
    RawDataTableColumn,
    RawDataTableResponse,
)

SYNC_SCRIPT_PATH = Path(os.getenv("SYNC_SCRIPT_PATH", str(Path(__file__).resolve().parents[2] / "scripts" / "surveycto_duckdb_sync.py"))).resolve()
SYNC_RESULT_PATH = Path(os.getenv("SYNC_RESULT_PATH", str(Path(__file__).resolve().parents[2] / "sync_result.json"))).resolve()
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", str(Path(__file__).resolve().parents[2] / "exports"))).resolve()
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
PYTHON_BIN = os.getenv("PYTHON_BIN", sys.executable)
DUCKDB_PATH = Path(os.getenv("DUCKDB_PATH", str(Path(__file__).resolve().parents[2] / "current.duckdb"))).resolve()
DUCKDB_TEMP_DIRECTORY = Path(os.getenv("DUCKDB_TEMP_DIRECTORY", str(Path(__file__).resolve().parents[2] / "duckdb_tmp"))).resolve()
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "1536MB")
DUCKDB_MAX_TEMP_DIRECTORY_SIZE = os.getenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "7GB")
DUCKDB_THREADS = max(1, int(os.getenv("DUCKDB_THREADS", "2")))
QUESTIONNAIRE_WORKBOOK_PATH = Path(os.getenv("QUESTIONNAIRE_WORKBOOK_PATH", str(Path(__file__).resolve().parents[1] / "BHT+4+SEASONS+JULY+2026.xlsx"))).resolve()
ANALYSIS_TEMPLATE_PATH = Path(os.getenv("ANALYSIS_TEMPLATE_PATH", str(Path(__file__).resolve().parents[1] / "BHT_FULL_DATA_TABLES_MAY_2026_NOODLES_FIXED.xlsx"))).resolve()


# Optional external XLSForm metadata (from Seasons project) used to map coded answers to labels.
_XLSFORM_METADATA: dict[str, Any] | None = None
_XLSFORM_ALIAS_MAP: dict[str, dict[str, Any]] | None = None


def _load_xlsform_metadata() -> None:
    global _XLSFORM_METADATA, _XLSFORM_ALIAS_MAP
    if _XLSFORM_METADATA is not None:
        return
    # Load XLSForm metadata from this project's local backend/data folder.
    base = Path(__file__).resolve().parents[2]
    candidate = base / "backend" / "data" / "xlsform_metadata.json"
    if not candidate.exists():
        _XLSFORM_METADATA = None
        _XLSFORM_ALIAS_MAP = None
        return
    try:
        _XLSFORM_METADATA = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(_XLSFORM_METADATA, dict):
            raise ValueError("XLSForm metadata must be a JSON object")
        # Build alias map: alias (upper/lower) -> question metadata
        alias_map: dict[str, dict[str, Any]] = {}
        questions: dict[str, Any] = _XLSFORM_METADATA.get("questions", {})
        for qname, meta in questions.items():
            for alias in meta.get("aliases", []) + [qname]:
                alias_map[str(alias)] = meta
        _XLSFORM_ALIAS_MAP = alias_map
    except Exception:
        _XLSFORM_METADATA = None
        _XLSFORM_ALIAS_MAP = None


def _quote_sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _export_raw_data_table_from_duckdb(csv_path: Path) -> bool:
    if not DUCKDB_PATH.exists():
        return False
    try:
        con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
        sql = (
            "COPY (SELECT submission_key, instrument_code, fetched_at, source_hash, raw_payload "
            "FROM raw_surveycto_submission ORDER BY fetched_at DESC) TO "
            + _quote_sql(str(csv_path))
            + " (HEADER, DELIMITER ',', QUOTE '\"')"
        )
        con.execute(sql)
        con.close()
        return True
    except Exception:
        return False


def _map_cell_value(key: str, value: Any) -> Any:
    """Map a raw cell value to a human label using XLSForm metadata when possible.

    Falls back to the original value when no mapping exists.
    """
    _load_xlsform_metadata()
    if not _XLSFORM_METADATA or not _XLSFORM_ALIAS_MAP:
        return value

    meta = _XLSFORM_ALIAS_MAP.get(key) or _XLSFORM_ALIAS_MAP.get(key.upper())
    if not meta:
        return value

    list_name = meta.get("list_name")
    if not list_name:
        return value

    lists = _XLSFORM_METADATA.get("lists", {})
    choices = lists.get(list_name, {})
    if not choices:
        return value

    # Handle select_multiple (could be space-separated string of choice keys)
    kind = meta.get("kind", "")
    try:
        if kind == "select_multiple":
            # normalize value to list of tokens
            if value is None:
                return value
            if isinstance(value, list):
                tokens = [str(v) for v in value]
            else:
                tokens = str(value).split()
            labels = []
            for t in tokens:
                # keys in choices are stored as strings like '1.0' in this metadata
                k = str(t)
                # try k, k+'.0', float repr
                if k in choices:
                    labels.append(choices[k])
                else:
                    try:
                        kf = float(k)
                        kf_s = str(kf)
                        if kf_s in choices:
                            labels.append(choices[kf_s])
                        else:
                            labels.append(t)
                    except Exception:
                        labels.append(t)
            return ", ".join(labels)

        if kind == "select_one":
            if value is None:
                return value
            k = str(value)
            if k in choices:
                return choices[k]
            try:
                kf = float(k)
                kf_s = str(kf)
                if kf_s in choices:
                    return choices[kf_s]
            except Exception:
                pass
            return value
    except Exception:
        return value


def _get_cors_origins() -> list[str]:
    configured_origins = os.getenv("CORS_ALLOWED_ORIGINS", "")
    if configured_origins.strip():
        return [origin.strip() for origin in configured_origins.split(",") if origin.strip()]
    return ["http://127.0.0.1:3000", "http://localhost:3000"]


def _get_cors_origin_regex() -> str | None:
    configured_regex = os.getenv("CORS_ALLOWED_ORIGIN_REGEX", "")
    if configured_regex.strip():
        return configured_regex.strip()
    return r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$"


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_schemas()
    Base.metadata.create_all(bind=get_engine())
    # Bootstrap the initial local administrator once. Change this password
    # immediately after first deployment through the staff-management flow.
    session = get_session_local()()
    try:
        admin = session.execute(select(StaffMember).where(StaffMember.username == "Superadmin")).scalars().first()
        if admin is None:
            session.add(StaffMember(username="Superadmin", email="superadmin@qc-hub.local", role="admin", password_hash=hash_password("admin123"), is_active=True))
            session.commit()
    finally:
        session.close()
    yield


app = FastAPI(title="QC Flags Platform", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_origin_regex=_get_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/admin/xlsform-metadata")
def get_xlsform_metadata():
    _load_xlsform_metadata()
    if not _XLSFORM_METADATA:
        raise HTTPException(status_code=404, detail="XLSForm metadata not available on server")
    # Return a minimal shape useful to the frontend: questions and lists
    return {
        "questions": _XLSFORM_METADATA.get("questions", {}),
        "lists": _XLSFORM_METADATA.get("lists", {}),
    }


_last_surveycto_sync: dict[str, Any] = {
    "stored": 0,
    "updated": 0,
    "skipped_submission_id": 0,
    "message": None,
    "timestamp": None,
}

# Import worker thread control
_import_worker_thread: threading.Thread | None = None
_import_worker_stop_event: threading.Event | None = None


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    username = payload.username.strip()
    member = db.execute(select(StaffMember).where(StaffMember.username == username)).scalars().first()
    if member is None or not member.is_active or not verify_password(payload.password, member.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return LoginResponse(staff_id=str(member.staff_id), username=member.username, role=member.role)


@app.get("/api/import/sync-status")
def import_sync_status():
    """Return last surveycto sync status recorded by the server."""
    return _last_surveycto_sync


@app.get("/api/import/queued")
def list_queued_imports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_payload: bool = Query(False, description="Include each queued job's raw payload in the response"),
    db: Session = Depends(get_db),
):
    """Return queued ImportJob entries without loading the full payload set by default."""
    total_count = db.execute(select(func.count()).select_from(ImportJob).where(ImportJob.status == "queued")).scalar_one()
    jobs = (
        db.execute(
            select(ImportJob, RawSurveyCTOSubmission)
            .join(
                RawSurveyCTOSubmission,
                RawSurveyCTOSubmission.submission_key == ImportJob.submission_key,
                isouter=True,
            )
            .where(ImportJob.status == "queued")
            .order_by(ImportJob.queued_at.asc())
            .offset(offset)
            .limit(limit)
        )
        .all()
    )

    results = []
    for job, raw in jobs:
        raw_payload = None
        fetched_at = None
        if include_payload and raw is not None:
            raw_payload = raw.raw_payload if isinstance(raw.raw_payload, dict) else {"value": raw.raw_payload}
            fetched_at = raw.fetched_at.isoformat() if raw.fetched_at else None
        elif raw is not None:
            fetched_at = raw.fetched_at.isoformat() if raw.fetched_at else None

        results.append(
            {
                "job_id": job.job_id,
                "submission_key": job.submission_key,
                "instrument_code": job.instrument_code,
                "queued_at": job.queued_at.isoformat() if job.queued_at else None,
                "raw_payload": raw_payload,
                "fetched_at": fetched_at,
            }
        )

    return {"items": results, "count": total_count}


def _import_worker_loop(stop_event: threading.Event):
    """Background loop that processes queued imports continuously until stopped."""
    SessionLocal = get_session_local()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            job = (
                db.execute(
                    select(ImportJob).where(ImportJob.status == "queued").order_by(ImportJob.queued_at.asc()).limit(1)
                )
                .scalars()
                .first()
            )
            if job is None:
                db.close()
                # sleep briefly before polling again
                stop_event.wait(2.0)
                continue

            # process job
            processed_at = datetime.now(timezone.utc)
            job.status = "processed"
            job.processed_at = processed_at
            db.commit()

            # rewrite import queue JSONL if present
            queue_path = os.getenv("IMPORT_QUEUE_PATH", "./import_queue.jsonl")
            if os.path.exists(queue_path):
                queue_file = Path(queue_path)
                temp_path = queue_file.with_suffix(queue_file.suffix + ".tmp")
                with queue_file.open("r", encoding="utf-8") as handle, temp_path.open(
                    "w", encoding="utf-8"
                ) as out_handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        if record.get("submission_id") != job.submission_key:
                            out_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                temp_path.replace(queue_file)

            _append_jsonl(
                os.getenv("PROCESSED_IMPORT_STORE_PATH", "./processed_imports.jsonl"),
                {
                    "submission_id": job.submission_key,
                    "project_id": job.instrument_code,
                    "status": "processed",
                    "processed_at": processed_at.isoformat(),
                },
            )

            # transform
            raw_submission = (
                db.execute(
                    select(RawSurveyCTOSubmission).where(RawSurveyCTOSubmission.submission_key == job.submission_key).limit(1)
                )
                .scalars()
                .first()
            )
            if raw_submission:
                case_payload = raw_submission.raw_payload or {}
                if not isinstance(case_payload, dict):
                    case_payload = {}

                answers_candidate = case_payload.get("answers")
                answers = answers_candidate if isinstance(answers_candidate, dict) else {}
                gps_candidate = case_payload.get("gps")
                gps = gps_candidate if isinstance(gps_candidate, dict) else {}
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

                # evaluate rules
                rules = db.execute(select(RuleDefinition).where(RuleDefinition.is_active.is_(True))).scalars().all()
                for rule in rules:
                    case_payload_local = canonical_case.record or {}
                    if not isinstance(case_payload_local, dict):
                        case_payload_local = {}
                    passed, message = _evaluate_rule_against_case(case_payload_local, {
                        "id": rule.rule_code,
                        "name": rule.name,
                        "field": rule.target_field,
                        "severity": rule.severity,
                        "operator": rule.operator,
                        "threshold": rule.threshold,
                    })
                    qc_result = RuleResult(
                        rule_code=rule.rule_code,
                        instrument_code=rule.instrument_code,
                        submission_key=canonical_case.submission_key,
                        case_id=canonical_case.case_id,
                        table_name=rule.target_table,
                        field_name=rule.target_field,
                        severity=rule.severity,
                        result_status="open" if not passed else "passed",
                        result_message=message,
                    )
                    db.add(qc_result)
                    db.commit()

                    if not passed:
                        issue = IssueQueue(
                            rule_result_id=qc_result.rule_result_id,
                            instrument_code=rule.instrument_code,
                            submission_key=canonical_case.submission_key,
                            case_id=canonical_case.case_id,
                            issue_status="pending_review",
                            issue_summary=message,
                            severity=rule.severity,
                        )
                        db.add(issue)
                        db.commit()

            db.close()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
            stop_event.wait(2.0)
            continue


@app.post("/api/import/start-worker")
def start_import_worker() -> dict[str, Any]:
    """Start a background import worker that continuously processes queued imports."""
    global _import_worker_thread, _import_worker_stop_event
    if _import_worker_thread and _import_worker_thread.is_alive():
        return {"status": "running"}
    stop_event = threading.Event()
    thread = threading.Thread(target=_import_worker_loop, args=(stop_event,), daemon=True)
    _import_worker_thread = thread
    _import_worker_stop_event = stop_event
    thread.start()
    return {"status": "started"}


@app.post("/api/import/stop-worker")
def stop_import_worker() -> dict[str, Any]:
    """Stop the background import worker if running."""
    global _import_worker_thread, _import_worker_stop_event
    if not _import_worker_thread or not _import_worker_thread.is_alive() or _import_worker_stop_event is None:
        return {"status": "not_running"}
    _import_worker_stop_event.set()
    _import_worker_thread.join(timeout=5.0)
    _import_worker_thread = None
    _import_worker_stop_event = None
    return {"status": "stopped"}


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


def get_survey_platform_config(
    session_token: str | None = None,
    request_username: str | None = None,
    request_password: str | None = None,
    request_server: str | None = None,
    request_form_id: str | None = None,
) -> dict[str, str]:
    username, password = resolve_surveycto_credentials(
        session_token=session_token,
        request_username=request_username,
        request_password=request_password,
    )
    return {
        "server": request_server or os.getenv("SURVEYCTO_SERVER", ""),
        "username": username,
        "password": password,
        "form_id": request_form_id or os.getenv("SURVEYCTO_MAIN_FORM_ID", ""),
        "dataset_id": os.getenv("SURVEYCTO_DATASET_ID", ""),
        "instrument_code": os.getenv("SURVEYCTO_INSTRUMENT_CODE", "main"),
        "date": os.getenv("SURVEYCTO_DATE", datetime.now(timezone.utc).strftime("%Y%m%d")),
    }


def fetch_submission_payloads_from_form(config: dict[str, str], limit: int | None = None) -> list[dict[str, Any]]:
    if not config["server"] or not config["username"] or not config["password"] or not config["form_id"]:
        return []

    surveycto_date = str(config["date"]).strip()
    if len(surveycto_date) != 8 or not surveycto_date.isdigit():
        surveycto_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    url = f"https://{config['server']}.surveycto.com/api/v2/forms/data/wide/json/{config['form_id']}"
    response = requests.post(
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

    if limit is not None:
        normalized_items = normalized_items[:limit]

    logging.info(
        "SurveyCTO fetch: url=%s date=%s raw_items=%d normalized_items=%d",
        url,
        surveycto_date,
        len(items),
        len(normalized_items),
    )
    if normalized_items:
        first_item = normalized_items[0]
        logging.info(
            "SurveyCTO first item keys: %s",
            ", ".join(sorted(str(k) for k in first_item.keys())),
        )

    return normalized_items


def fetch_surveycto_dataset_records(config: dict[str, str], limit: int | None = None) -> list[dict[str, Any]]:
    if not config["server"] or not config["username"] or not config["password"] or not config["dataset_id"]:
        return []

    url = f"https://{config['server']}.surveycto.com/api/v2/datasets/{config['dataset_id']}/records"
    all_items: list[dict[str, Any]] = []
    next_cursor: str | None = None

    while True:
        params: dict[str, Any] = {"limit": min(1000, limit) if limit is not None else 1000}
        if next_cursor:
            params["cursor"] = next_cursor

        response = requests.get(
            url,
            auth=(config["username"], config["password"]),
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            if isinstance(payload.get("data"), list):
                page_items = payload["data"]
            elif isinstance(payload.get("items"), list):
                page_items = payload["items"]
            else:
                page_items = []
            next_cursor = payload.get("nextCursor")
        elif isinstance(payload, list):
            page_items = payload
            next_cursor = None
        else:
            page_items = []
            next_cursor = None

        normalized_page_items = [item for item in page_items if isinstance(item, dict)]
        all_items.extend(normalized_page_items)

        if limit is not None and len(all_items) >= limit:
            break
        if not next_cursor or not page_items:
            break

    if limit is not None:
        all_items = all_items[:limit]

    logging.info(
        "SurveyCTO dataset fetch: url=%s dataset_id=%s raw_items=%d",
        url,
        config["dataset_id"],
        len(all_items),
    )
    if all_items:
        first_item = all_items[0]
        logging.info(
            "SurveyCTO dataset first item keys: %s",
            ", ".join(sorted(str(k) for k in first_item.keys())),
        )

    return all_items


def _fetch_submission_payloads(config: dict[str, str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    if config is None:
        config = get_survey_platform_config()
    if config["dataset_id"]:
        return fetch_surveycto_dataset_records(config, limit=limit)
    return fetch_submission_payloads_from_form(config, limit=limit)


def fetch_submission_payloads(config: dict[str, str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    return _fetch_submission_payloads(config, limit=limit)


def _maybe_limited_fetch_submission_payloads(
    config: dict[str, str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if limit is None:
        return fetch_submission_payloads(config)

    try:
        return fetch_submission_payloads(config, limit=limit)
    except TypeError:
        try:
            return fetch_submission_payloads(config)
        except TypeError:
            return fetch_submission_payloads()


def _flatten_payload_for_table(payload: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(payload, dict):
        flattened: dict[str, Any] = {}
        for key, value in payload.items():
            field_name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                flattened.update(_flatten_payload_for_table(value, field_name))
            elif isinstance(value, list):
                flattened[field_name] = json.dumps(value, ensure_ascii=False, default=str)
            else:
                flattened[field_name] = value
        return flattened

    if isinstance(payload, list):
        return {prefix or "value": json.dumps(payload, ensure_ascii=False, default=str)}

    return {prefix or "value": payload}


def fetch_submission_payloads_status() -> tuple[list[dict[str, Any]], bool, str | None]:
    config = get_survey_platform_config()
    if not config["server"] or not config["username"] or not config["password"] or (
        not config["dataset_id"] and not config["form_id"]
    ):
        return [], False, "Missing SurveyCTO credentials or form/dataset configuration"

    try:
        items = _fetch_submission_payloads(config, limit=1)
        return items, True, None
    except requests.exceptions.RequestException as exc:
        logging.error("SurveyCTO connection failed: %s", exc)
        return [], False, str(exc)
    except ValueError as exc:
        logging.error("SurveyCTO payload parsing failed: %s", exc)
        return [], False, str(exc)


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
    run_main_survey_qc(db)
    _refresh_analysis_workbook(db)
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


@app.post("/api/surveycto/session", response_model=SurveyCTOSessionResponse)
def create_surveycto_session_endpoint(payload: SurveyCTOSessionRequest) -> SurveyCTOSessionResponse:
    config = get_survey_platform_config(
        request_server=payload.surveyctoServer,
        request_username=payload.surveyctoUsername,
        request_password=payload.surveyctoPassword,
        request_form_id=payload.formId,
    )
    return create_surveycto_session(
        server=config["server"],
        surveycto_username=config["username"],
        surveycto_password=config["password"],
        target_form_id=config["form_id"],
    )


@app.post("/api/import/survey-platform/sync")
def sync_survey_platform_submissions(
    payload: SurveyCTOSessionRequest | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    batch_size = max(1, int(os.getenv("SURVEYCTO_BATCH_SIZE", "100")))
    if payload is not None:
        config = get_survey_platform_config(
            session_token=payload.surveyctoSessionToken,
            request_username=payload.surveyctoUsername,
            request_password=payload.surveyctoPassword,
            request_server=payload.surveyctoServer,
            request_form_id=payload.formId,
        )
        items = _maybe_limited_fetch_submission_payloads(config, limit=batch_size)
    else:
        items = _maybe_limited_fetch_submission_payloads(limit=batch_size)
    fetched = len(items)
    stored = 0
    updated = 0
    skipped_submission_id = 0

    for item in items:
        submission_id = str(item.get("submission_id") or item.get("id") or item.get("KEY") or item.get("submissionkey") or "")
        if not submission_id:
            skipped_submission_id += 1
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
            updated += 1

    db.commit()
    if stored or updated:
        run_main_survey_qc(db)
        _refresh_analysis_workbook(db)

    _last_surveycto_sync["stored"] = stored
    _last_surveycto_sync["updated"] = updated
    _last_surveycto_sync["skipped_submission_id"] = skipped_submission_id
    _last_surveycto_sync["message"] = "Sync completed"
    _last_surveycto_sync["timestamp"] = datetime.now(timezone.utc).isoformat()

    return {
        "status": "synced",
        "source": "survey_platform",
        "fetched": fetched,
        "stored": stored,
        "updated": updated,
        "skipped_submission_id": skipped_submission_id,
        "analysis_refreshed": bool(stored or updated),
    }


def _run_surveycto_sync_in_thread(payload: SurveyCTOSessionRequest | None = None) -> None:
    """Background worker to invoke the external sync process."""
    global _last_surveycto_sync
    _last_surveycto_sync["message"] = "Sync process starting"
    _last_surveycto_sync["timestamp"] = datetime.now(timezone.utc).isoformat()

    env = os.environ.copy()
    env["SYNC_RESULT_PATH"] = str(SYNC_RESULT_PATH)
    if payload is not None:
        if payload.surveyctoServer:
            env["SURVEYCTO_SERVER"] = payload.surveyctoServer
        if payload.surveyctoUsername:
            env["SURVEYCTO_USERNAME"] = payload.surveyctoUsername
        if payload.surveyctoPassword:
            env["SURVEYCTO_PASSWORD"] = payload.surveyctoPassword
        if payload.formId:
            env["SURVEYCTO_MAIN_FORM_ID"] = payload.formId

    env["SYNC_RESULT_PATH"] = str(SYNC_RESULT_PATH)
    env["DUCKDB_PATH"] = str(DUCKDB_PATH)
    env["DUCKDB_TEMP_DIRECTORY"] = str(DUCKDB_TEMP_DIRECTORY)
    env["DUCKDB_MEMORY_LIMIT"] = DUCKDB_MEMORY_LIMIT
    env["DUCKDB_MAX_TEMP_DIRECTORY_SIZE"] = DUCKDB_MAX_TEMP_DIRECTORY_SIZE
    env["DUCKDB_THREADS"] = str(DUCKDB_THREADS)

    process = subprocess.run(
        [
            PYTHON_BIN,
            str(SYNC_SCRIPT_PATH),
            "--status-file",
            str(SYNC_RESULT_PATH),
            "--duckdb-path",
            str(DUCKDB_PATH),
            "--temp-directory",
            str(DUCKDB_TEMP_DIRECTORY),
            "--memory-limit",
            DUCKDB_MEMORY_LIMIT,
            "--max-temp-directory-size",
            DUCKDB_MAX_TEMP_DIRECTORY_SIZE,
            "--threads",
            str(DUCKDB_THREADS),
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
    )

    try:
        result = {}
        if SYNC_RESULT_PATH.exists():
            try:
                result = json.loads(SYNC_RESULT_PATH.read_text(encoding="utf-8"))
            except Exception:
                result = {}

        if process.returncode != 0:
            error_message = result.get("error") or process.stderr.strip() or process.stdout.strip() or f"Sync process failed with exit code {process.returncode}"
            _last_surveycto_sync["message"] = f"Async sync failed: {error_message}"
            return

        if result.get("ok") is False:
            _last_surveycto_sync["message"] = f"Async sync failed: {result.get('error', 'unknown error')}"
            return

        # The external worker deliberately writes a durable raw DuckDB snapshot.
        # Mirror that snapshot into the application's raw-response table before
        # declaring the sync complete; the analysis API and workbook both read
        # from this table, so this makes async syncs reportable automatically.
        mirrored_stored, mirrored_updated = _mirror_duckdb_raw_submissions()
        _last_surveycto_sync["stored"] = mirrored_stored
        _last_surveycto_sync["updated"] = mirrored_updated
        _last_surveycto_sync["skipped_submission_id"] = result.get("skipped", _last_surveycto_sync["skipped_submission_id"])
        _last_surveycto_sync["message"] = "Async sync completed"
        _last_surveycto_sync["timestamp"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        _last_surveycto_sync["message"] = f"Async sync failed: {exc}"


def _mirror_duckdb_raw_submissions() -> tuple[int, int]:
    """Copy the completed SurveyCTO DuckDB snapshot into the reporting store."""
    if not DUCKDB_PATH.exists():
        return 0, 0
    connection = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        rows = connection.execute(
            "SELECT submission_key, instrument_code, source_hash, raw_payload FROM raw_surveycto_submission"
        ).fetchall()
    finally:
        connection.close()
    session = get_session_local()()
    stored = updated = 0
    try:
        for submission_key, instrument_code, source_hash, raw_payload in rows:
            try:
                answers = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            except json.JSONDecodeError:
                answers = {"value": raw_payload}
            existing = session.execute(
                select(RawSurveyCTOSubmission).where(
                    RawSurveyCTOSubmission.instrument_code == str(instrument_code),
                    RawSurveyCTOSubmission.submission_key == str(submission_key),
                )
            ).scalars().first()
            if existing:
                if existing.source_hash != str(source_hash):
                    existing.source_hash = str(source_hash)
                    existing.raw_payload = answers
                    existing.fetched_at = datetime.now(timezone.utc)
                    updated += 1
                continue
            session.add(RawSurveyCTOSubmission(
                instrument_code=str(instrument_code), form_id=None, formdef_version=None,
                survey_month=None, submission_key=str(submission_key), submission_version=1,
                submission_date=None, completion_date=None, interviewer_username=None,
                device_id=None, source_hash=str(source_hash), raw_payload=answers,
            ))
            stored += 1
        session.commit()
        if stored or updated:
            # Background imports write through a separate path; give them the
            # same automatic QC treatment as request-based imports.
            run_main_survey_qc(session)
            _refresh_analysis_workbook(session)
        return stored, updated
    finally:
        session.close()


@app.post("/api/import/survey-platform/sync-async")
def trigger_async_survey_platform_sync(payload: SurveyCTOSessionRequest | None = None) -> dict[str, Any]:
    """Trigger a background sync; returns immediately while the external sync process runs."""
    thread = threading.Thread(target=_run_surveycto_sync_in_thread, args=(payload,), daemon=True)
    thread.start()
    return {"status": "started", "message": "Sync started in background"}


@app.get("/api/import/survey-platform/raw", response_model=RawSurveyCTOListResponse)
def list_raw_survey_platform_submissions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_payload: bool = Query(False, description="Include the full raw payload for each submission"),
    db: Session = Depends(get_db),
) -> RawSurveyCTOListResponse:
    total_count = db.execute(select(func.count()).select_from(RawSurveyCTOSubmission)).scalar_one()
    submissions = (
        db.execute(
            select(RawSurveyCTOSubmission)
            .order_by(RawSurveyCTOSubmission.fetched_at.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    items = [
        RawSurveyCTOItem(
            raw_submission_id=str(submission.raw_submission_id),
            instrument_code=submission.instrument_code,
            submission_key=submission.submission_key,
            source_hash=submission.source_hash,
            fetched_at=submission.fetched_at,
            raw_payload=(submission.raw_payload if isinstance(submission.raw_payload, dict) else {"value": submission.raw_payload})
            if include_payload
            else {},
        )
        for submission in submissions
    ]
    return RawSurveyCTOListResponse(items=items, count=total_count)


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
        queue_file = Path(queue_path)
        temp_path = queue_file.with_suffix(queue_file.suffix + ".tmp")
        with queue_file.open("r", encoding="utf-8") as handle, temp_path.open("w", encoding="utf-8") as out_handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("submission_id") != job.submission_key:
                    out_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        temp_path.replace(queue_file)

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

    answers_candidate = case_payload.get("answers")
    answers = answers_candidate if isinstance(answers_candidate, dict) else {}
    gps_candidate = case_payload.get("gps")
    gps = gps_candidate if isinstance(gps_candidate, dict) else {}
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
def get_review_queue(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    assignee_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ReviewQueueResponse:
    base_query = select(IssueQueue)
    count_query = select(func.count()).select_from(IssueQueue)
    if assignee_id:
        base_query = base_query.where(IssueQueue.assigned_to_user_id == assignee_id)
        count_query = count_query.where(IssueQueue.assigned_to_user_id == assignee_id)
    total_count = db.execute(count_query).scalar_one()
    issues = (
        db.execute(
            base_query
            .order_by(IssueQueue.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    staff_by_id = {member.staff_id: member.username for member in db.execute(select(StaffMember)).scalars().all()}
    results_by_id = {result.rule_result_id: result for result in db.execute(select(RuleResult)).scalars().all()}
    raw_by_key = {raw.submission_key: raw for raw in db.execute(select(RawSurveyCTOSubmission)).scalars().all()}
    def serialize_issue(issue: IssueQueue) -> ReviewQueueItem:
        result = results_by_id.get(issue.rule_result_id) if issue.rule_result_id else None
        raw_submission = raw_by_key.get(issue.submission_key) if issue.submission_key else None
        raw_payload = raw_submission.raw_payload if raw_submission is not None else {}
        interviewer = raw_payload.get("Interviewer") or raw_payload.get("username") or "Unknown"
        return ReviewQueueItem(
            issue_id=str(issue.issue_id), submission_key=issue.submission_key, case_id=issue.case_id,
            issue_status=issue.issue_status, status=issue.issue_status, issue_summary=issue.issue_summary,
            severity=issue.severity if hasattr(issue, "severity") else "medium", created_at=issue.created_at,
            updated_at=issue.updated_at, resolved_at=issue.resolved_at, resolution_note=issue.resolution_note,
            assigned_to_user_id=str(issue.assigned_to_user_id) if issue.assigned_to_user_id else None,
            assigned_to_name=staff_by_id.get(issue.assigned_to_user_id) if issue.assigned_to_user_id else None,
            flag_name=result.rule_code if result else None, interviewer=str(interviewer),
            evidence=result.result_message if result else issue.issue_summary,
        )

    return ReviewQueueResponse(issues=[serialize_issue(issue) for issue in issues], count=total_count)


@app.get("/api/admin/staff", response_model=list[StaffMemberResponse])
def get_staff_members(db: Session = Depends(get_db)) -> list[StaffMemberResponse]:
    staff_members = (
        db.execute(select(StaffMember).order_by(StaffMember.created_at.asc())).scalars().all()
    )
    return [
        StaffMemberResponse(
            staff_id=member.staff_id,
            username=member.username,
            email=member.email,
            role=member.role,
            created_at=member.created_at,
        )
        for member in staff_members
    ]


@app.post("/api/admin/staff", response_model=StaffMemberResponse, status_code=201)
def create_staff_member(payload: StaffMemberCreate, db: Session = Depends(get_db)) -> StaffMemberResponse:
    staff_member = StaffMember(
        username=payload.username.strip(),
        email=payload.email.strip(),
        role=payload.role.strip() or "reviewer",
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    db.add(staff_member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="A staff member with that email already exists.")
    db.refresh(staff_member)
    return StaffMemberResponse(
        staff_id=staff_member.staff_id,
        username=staff_member.username,
        email=staff_member.email,
        role=staff_member.role,
        created_at=staff_member.created_at,
    )


@app.get("/api/admin/raw-data-table", response_model=RawDataTableResponse)
def get_admin_raw_data_table(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: str | None = Query(default=None),
    interpret: bool = Query(True, description="If true, map coded answers to labels when possible"),
    db: Session = Depends(get_db),
) -> RawDataTableResponse:
    total_count = db.execute(select(func.count()).select_from(RawSurveyCTOSubmission)).scalar_one()
    submissions = (
        db.execute(
            select(RawSurveyCTOSubmission)
            .order_by(RawSurveyCTOSubmission.fetched_at.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    core_columns = [
        ("submission_key", "string"),
        ("instrument_code", "string"),
        ("fetched_at", "datetime"),
        ("raw_submission_id", "string"),
        ("source_hash", "string"),
    ]
    column_names = [name for name, _ in core_columns]
    rows: list[dict[str, Any]] = []

    normalized_search = (search or "").strip().lower()
    for submission in submissions:
        payload = submission.raw_payload if isinstance(submission.raw_payload, dict) else {"value": submission.raw_payload}
        flattened_payload = _flatten_payload_for_table(payload)
        row = {
            "submission_key": submission.submission_key,
            "instrument_code": submission.instrument_code,
            "fetched_at": submission.fetched_at.isoformat() if submission.fetched_at else None,
            "raw_submission_id": submission.raw_submission_id,
            "source_hash": submission.source_hash,
        }
        # optionally map values using XLSForm metadata
        if interpret:
            mapped = {k: _map_cell_value(k, v) for k, v in flattened_payload.items()}
            row.update(mapped)
        else:
            row.update(flattened_payload)

        if normalized_search:
            searchable_values = [str(value).lower() for value in row.values() if value is not None]
            if not any(normalized_search in value for value in searchable_values):
                continue

        rows.append(row)
        for key in flattened_payload.keys():
            if key not in column_names:
                column_names.append(key)

    columns = [RawDataTableColumn(name=name, type="string") for name in column_names]
    return RawDataTableResponse(
        columns=columns,
        rows=rows,
        count=total_count,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total_count,
    )


@app.get("/api/admin/decoded-questions/{submission_key}", response_model=DecodedQuestionResponse)
def get_decoded_questions(submission_key: str, db: Session = Depends(get_db)) -> DecodedQuestionResponse:
    submission = (
        db.execute(select(RawSurveyCTOSubmission).where(RawSurveyCTOSubmission.submission_key == submission_key).limit(1))
        .scalars()
        .first()
    )
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    payload = submission.raw_payload if isinstance(submission.raw_payload, dict) else {"value": submission.raw_payload}
    answers_payload = payload.get("answers") or payload
    question_rows = decode_submission_to_question_rows(
        answers_payload,
        metadata_path=Path(__file__).resolve().parents[1] / "data" / "xlsform_metadata.json",
        limit=250,
    )
    return DecodedQuestionResponse(
        submission_key=submission.submission_key,
        rows=[DecodedQuestionRow(category=row["category"], question=row["question"], response=row["response"]) for row in question_rows],
    )


@app.get("/api/insights/overview", response_model=InsightsOverviewResponse)
def get_insights_overview(db: Session = Depends(get_db)) -> InsightsOverviewResponse:
    """Return presentation-ready distributions instead of exposing submission payloads."""
    metadata_path = Path(__file__).resolve().parents[1] / "data" / "xlsform_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    choice_lists = metadata.get("lists", {}) if isinstance(metadata, dict) else {}
    questions = metadata.get("questions", {}) if isinstance(metadata, dict) else {}

    def answer_label(question_name: str, value: Any) -> str:
        question = questions.get(question_name, {}) if isinstance(questions, dict) else {}
        question_type = str(question.get("type", "")) if isinstance(question, dict) else ""
        list_name = question_type.replace("select_one", "", 1).replace("select_multiple", "", 1).strip()
        options = choice_lists.get(list_name, {}) if isinstance(choice_lists, dict) else {}
        raw = str(value).strip()
        return str(options.get(raw) or options.get(f"{raw}.0") or raw)

    sector_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    submissions = db.execute(select(RawSurveyCTOSubmission.raw_payload)).scalars().all()
    for payload in submissions:
        answers = payload.get("answers", payload) if isinstance(payload, dict) else {}
        if not isinstance(answers, dict):
            continue
        if answers.get("Sector") not in (None, ""):
            sector_counts[answer_label("Sector", answers["Sector"])] += 1
        # N_QC1 is the questionnaire's "ever consumed" category-selection block.
        for key, value in answers.items():
            if not re.fullmatch(r"N_QC1\.\d+", str(key)) or value in (None, ""):
                continue
            values = value if isinstance(value, list) else str(value).split()
            for selected_value in values:
                label = answer_label(str(key), selected_value)
                if label and label.lower() not in {"none", "none of these", "not applicable"}:
                    category_counts[label] += 1

    respondent_count = len(submissions)
    def make_items(counts: Counter[str]) -> list[InsightDistributionItem]:
        return [
            InsightDistributionItem(label=label, count=count, pct=round((count / respondent_count) * 100, 1) if respondent_count else 0)
            for label, count in counts.most_common(12)
        ]

    return InsightsOverviewResponse(
        respondent_count=respondent_count,
        categories=make_items(category_counts),
        sectors=make_items(sector_counts),
    )


def _analysis_tables_for_category(
    db: Session,
    category: str,
    filter_field: str | None = None,
    question_id: str | None = None,
    response_filter_question: str | None = None,
    response_filter_value: str | None = None,
) -> tuple[int, list[dict[str, Any]], list[dict[str, str]], str | None, list[dict[str, str]], str | None]:
    metadata_path = Path(__file__).resolve().parents[1] / "data" / "xlsform_metadata.json"
    if QUESTIONNAIRE_WORKBOOK_PATH.exists():
        metadata = load_questionnaire_xlsform(QUESTIONNAIRE_WORKBOOK_PATH)
    elif metadata_path.exists():
        metadata = load_metadata(metadata_path)
    else:
        raise HTTPException(status_code=503, detail="Questionnaire decoding source is not available")
    payloads = db.execute(select(RawSurveyCTOSubmission.raw_payload)).scalars().all()
    # Optionally filter payloads to those where a given question contains a specific response label
    if response_filter_question and response_filter_value:
        filtered = []
        for payload in payloads:
            answers = _answers(payload)
            # resolve key case-insensitively
            match_key = next((k for k in answers.keys() if k.lower() == response_filter_question.lower()), None)
            if not match_key:
                continue
            metadata_key = next(
                (key for key in metadata.get("questions", {}) if key.lower() == match_key.lower()),
                match_key,
            )
            qmeta = _question(metadata, metadata_key)
            qtype = str(qmeta.get("type", "")) if qmeta else ""
            tokens = _answer_tokens(answers.get(match_key), qtype)
            options = _options(metadata, metadata_key)
            labels = [_choice_label(options, t) for t in tokens]
            if any(str(l) == str(response_filter_value) for l in labels):
                filtered.append(payload)
        payloads = filtered
    filters = available_filter_fields(payloads, metadata)
    available_fields = {item["field"].lower(): item["field"] for item in filters}
    selected_filter = available_fields.get((filter_field or "").lower())
    if selected_filter is None:
        selected_filter = next((item["field"] for item in filters if item["field"].lower() == "gender"), None)
    if selected_filter is None and filters:
        selected_filter = filters[0]["field"]
    registry = load_template_registry(ANALYSIS_TEMPLATE_PATH, metadata, category) if ANALYSIS_TEMPLATE_PATH.exists() else None
    registry = registry or []
    available_question_fields = {
        str(field_name)
        for payload in payloads
        for field_name in _answers(payload)
    }
    catalog = build_question_catalog(
        metadata,
        excluded_names=PREFERRED_FILTER_FIELDS,
        available_fields=available_question_fields,
    )


    if catalog:
        questions = [{"id": name, "label": label} for name, label in catalog]
    else:
        questions = [{"id": name, "label": str(metadata.get("questions", {}).get(name, {}).get("label") or title)} for name, title in registry]
    selected_question = next((item["id"] for item in questions if item["id"].lower() == (question_id or "").lower()), None)
    if selected_question is None and questions:
        selected_question = questions[0]["id"]
    selected_registry = [item for item in registry if item[0].lower() == selected_question.lower()] if selected_question else registry
    if selected_question and catalog:
        # The catalog is intentionally broader than the legacy report template.
        # Use the label for the question the analyst actually selected, not the
        # first catalog entry (which made every selected question appear as the
        # same table and obscured its counts).
        selected_label = next(item["label"] for item in questions if item["id"] == selected_question)
        selected_registry = [(selected_question, selected_label)]
    tables = build_tables(
        payloads, metadata, category=category, registry=selected_registry,
        cut_fields=[selected_filter] if selected_filter else [],
    )
    if not tables:
        raise HTTPException(status_code=404, detail=f"No table registry is configured for category '{category}'")
    return len(payloads), tables, filters, selected_filter, questions, selected_question


@app.post("/api/qc/run-main-survey")
def run_main_survey_qc(db: Session = Depends(get_db)) -> dict[str, int]:
    """Evaluate the documented batch QC flags and add new outliers to review."""
    submissions = db.execute(select(RawSurveyCTOSubmission)).scalars().all()
    flags = evaluate_main_survey((row.submission_key, row.raw_payload) for row in submissions)
    # Rule results have a database foreign key to the rule catalogue.  The
    # batch rules are code-defined, so seed any missing catalogue entries
    # before persisting their outcomes (including on existing deployments).
    flag_codes = {flag["code"] for flag in flags}
    defined_codes = set(db.execute(select(RuleDefinition.rule_code).where(RuleDefinition.rule_code.in_(flag_codes))).scalars().all()) if flag_codes else set()
    for code in flag_codes - defined_codes:
        db.add(RuleDefinition(
            rule_code=code,
            name=code.replace("MAIN_", "").replace("_", " ").title(),
            instrument_code="main",
            target_table="raw.surveycto_submission",
            severity="high",
            rule_type="batch",
            description="Automatically evaluated Main Survey quality-control rule.",
            recommended_action="Review the affected submission and its supporting evidence.",
            is_active=True,
        ))
    if flag_codes - defined_codes:
        db.flush()
    submissions_by_key = {row.submission_key: row for row in submissions}
    existing_results = set(db.execute(select(RuleResult.rule_code, RuleResult.submission_key)).all())
    created = 0
    for flag in flags:
        if (flag["code"], flag["submission_key"]) in existing_results:
            continue
        raw = submissions_by_key.get(flag["submission_key"])
        result = RuleResult(rule_code=flag["code"], instrument_code=raw.instrument_code if raw else "main", submission_key=flag["submission_key"], table_name="raw.surveycto_submission", severity=flag["severity"], result_status="open", result_message=flag["message"])
        db.add(result); db.flush()
        db.add(IssueQueue(rule_result_id=result.rule_result_id, instrument_code=result.instrument_code, submission_key=result.submission_key, issue_status="pending_review", severity=result.severity, issue_summary=f"{flag['code']}: {flag['message']}"))
        created += 1
    db.commit()
    return {"evaluated": len(submissions), "flags_found": len(flags), "issues_created": created}


@app.put("/api/qc/issues/{issue_id}/assignment", response_model=ReviewQueueItem)
def assign_issue(issue_id: str, payload: IssueAssignmentRequest, db: Session = Depends(get_db)) -> ReviewQueueItem:
    issue = db.get(IssueQueue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    assignee = db.get(StaffMember, payload.staff_id) if payload.staff_id else None
    if payload.staff_id and assignee is None:
        raise HTTPException(status_code=404, detail="Staff member not found")
    issue.assigned_to_user_id = assignee.staff_id if assignee else None
    issue.assigned_to_role = assignee.role if assignee else None
    issue.updated_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(issue)
    return ReviewQueueItem(issue_id=str(issue.issue_id), submission_key=issue.submission_key, case_id=issue.case_id, issue_status=issue.issue_status, status=issue.issue_status, issue_summary=issue.issue_summary, severity=issue.severity, created_at=issue.created_at, updated_at=issue.updated_at, resolved_at=issue.resolved_at, resolution_note=issue.resolution_note, assigned_to_user_id=str(issue.assigned_to_user_id) if issue.assigned_to_user_id else None, assigned_to_name=assignee.username if assignee else None)


def _refresh_analysis_workbook(db: Session, category: str = "noodles") -> None:
    """Regenerate the latest analysis output after new SurveyCTO data is saved."""
    try:
        _, tables, _, _, _, _ = _analysis_tables_for_category(db, category)
        write_analysis_workbook(
            EXPORT_DIR / f"bht-{category.lower()}-analysis.xlsx",
            category,
            tables,
            template_path=ANALYSIS_TEMPLATE_PATH,
        )
    except Exception as exc:
        # A reporting failure must not roll back a successful SurveyCTO import.
        logging.exception("Analysis workbook refresh failed: %s", exc)


@app.get("/api/analytics/tables", response_model=AnalysisTablesResponse)
def get_analysis_tables(
    category: str = Query("noodles"),
    filter_field: str | None = Query(default=None),
    question_id: str | None = Query(default=None),
    filter_question: str | None = Query(default=None),
    filter_value: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> AnalysisTablesResponse:
    """Serve the same analysis model used to create the downloadable workbook."""
    respondent_count, tables, filters, selected_filter, questions, selected_question = _analysis_tables_for_category(
        db, category, filter_field, question_id, response_filter_question=filter_question, response_filter_value=filter_value
    )
    # Convert raw dicts into typed response models to satisfy static type checkers
    filters_models = [AnalysisFilterField(field=item["field"], label=item["label"]) for item in filters]
    questions_models = [AnalysisQuestion(id=item["id"], label=item["label"]) for item in questions]
    return AnalysisTablesResponse(
        category=category.title(),
        respondent_count=respondent_count,
        tables=[AnalysisTableResponse.model_validate(table) for table in tables],
        filters=filters_models,
        filter_field=selected_filter,
        questions=questions_models,
        question_id=selected_question,
    )


@app.get("/api/analytics/tables/export")
def export_analysis_tables(category: str = Query("noodles"), db: Session = Depends(get_db)) -> FileResponse:
    respondent_count, tables, _, _, _, _ = _analysis_tables_for_category(db, category)
    _ = respondent_count
    filename = f"bht-{category.lower()}-analysis.xlsx"
    export_path = EXPORT_DIR / filename
    try:
        write_analysis_workbook(export_path, category, tables, template_path=ANALYSIS_TEMPLATE_PATH)
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Excel export support is not installed") from exc
    return FileResponse(export_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=filename)


@app.get("/api/admin/raw-data-export")
def export_raw_data_table(
    search: str | None = Query(default=None),
    interpret: bool = Query(True, description="If true, map coded answers to labels when possible"),
    db: Session = Depends(get_db),
):
    query = db.execute(
        select(RawSurveyCTOSubmission).order_by(RawSurveyCTOSubmission.fetched_at.desc())
    )

    jsonl_path = EXPORT_DIR / f"raw-data-{uuid4().hex}.jsonl"
    csv_path = EXPORT_DIR / f"raw-data-{uuid4().hex}.csv"
    column_names: set[str] = {"submission_key", "instrument_code", "fetched_at", "raw_submission_id", "source_hash"}
    normalized_search = search.strip().lower() if search else None

    try:
        if not interpret and normalized_search is None and _export_raw_data_table_from_duckdb(csv_path):
            return FileResponse(
                path=str(csv_path),
                media_type="text/csv",
                filename="surveycto-raw-data.csv",
            )

        with jsonl_path.open("w", encoding="utf-8") as jsonl_handle:
            for submission in query.scalars():
                payload = submission.raw_payload if isinstance(submission.raw_payload, dict) else {"value": submission.raw_payload}
                flattened_payload = _flatten_payload_for_table(payload)
                row = {
                    "submission_key": submission.submission_key,
                    "instrument_code": submission.instrument_code,
                    "fetched_at": submission.fetched_at.isoformat() if submission.fetched_at else None,
                    "raw_submission_id": submission.raw_submission_id,
                    "source_hash": submission.source_hash,
                }
                if interpret:
                    mapped = {k: _map_cell_value(k, v) for k, v in flattened_payload.items()}
                    row.update(mapped)
                else:
                    row.update(flattened_payload)
                column_names.update(row.keys())

                if normalized_search:
                    searchable_values = [str(value).lower() for value in row.values() if value is not None]
                    if not any(normalized_search in value for value in searchable_values):
                        continue

                jsonl_handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        columns = sorted(column_names)
        with csv_path.open("w", encoding="utf-8", newline="") as csv_handle:
            writer = csv.writer(csv_handle, quoting=csv.QUOTE_ALL)
            writer.writerow(columns)
            with jsonl_path.open("r", encoding="utf-8") as jsonl_handle:
                for line in jsonl_handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    values = []
                    for column in columns:
                        value = row.get(column)
                        if value is None:
                            values.append("")
                        elif isinstance(value, (dict, list)):
                            values.append(json.dumps(value, ensure_ascii=False, default=str))
                        else:
                            values.append(str(value))
                    writer.writerow(values)

        try:
            jsonl_path.unlink()
        except OSError:
            pass

        return FileResponse(
            path=str(csv_path),
            media_type="text/csv",
            filename="surveycto-raw-data.csv",
        )
    except Exception:
        try:
            jsonl_path.unlink()
        except OSError:
            pass
        try:
            csv_path.unlink()
        except OSError:
            pass
        raise


@app.get("/api/admin/dashboard", response_model=AdminDashboardResponse)
def get_admin_dashboard(db: Session = Depends(get_db)) -> AdminDashboardResponse:
    raw_count = db.execute(select(func.count()).select_from(RawSurveyCTOSubmission)).scalar_one()
    issue_count = db.execute(select(func.count()).select_from(IssueQueue)).scalar_one()
    pending_review_count = db.execute(
        select(func.count()).select_from(IssueQueue).where(IssueQueue.issue_status == "pending_review")
    ).scalar_one()
    high_severity_count = db.execute(
        select(func.count()).select_from(IssueQueue).where(IssueQueue.severity == "high")
    ).scalar_one()
    medium_severity_count = db.execute(
        select(func.count()).select_from(IssueQueue).where(IssueQueue.severity == "medium")
    ).scalar_one()
    staff_count = db.execute(select(func.count()).select_from(StaffMember)).scalar_one()
    last_sync_at = db.execute(select(func.max(RawSurveyCTOSubmission.fetched_at))).scalar_one()

    return AdminDashboardResponse(
        raw_submission_count=raw_count,
        issue_count=issue_count,
        pending_review_count=pending_review_count,
        high_severity_count=high_severity_count,
        medium_severity_count=medium_severity_count,
        staff_count=staff_count,
        last_sync_at=last_sync_at,
    )


@app.get("/api/admin/surveycto-status", response_model=SurveyCTOStatusResponse)
def get_surveycto_status(db: Session = Depends(get_db)) -> SurveyCTOStatusResponse:
    config = get_survey_platform_config()
    raw_count = db.execute(select(func.count()).select_from(RawSurveyCTOSubmission)).scalar_one()
    last_sync_at = db.execute(select(func.max(RawSurveyCTOSubmission.fetched_at))).scalar_one()

    fetched_items, connection_ok, connection_message = fetch_submission_payloads_status()
    surveycto_raw_items = len(fetched_items)
    surveycto_normalized_items = len(fetched_items)
    first_item_keys = None
    if fetched_items:
        first_item_keys = sorted(str(k) for k in fetched_items[0].keys())

    pull_ok = connection_ok and surveycto_raw_items > 0
    if not connection_ok:
        pull_message = "Unable to connect to SurveyCTO"
    elif surveycto_raw_items == 0:
        pull_message = "Connected successfully, but no items were returned"
    else:
        pull_message = f"Connected and fetched {surveycto_raw_items} item(s)"

    surveycto_endpoint = (
        f"https://{config['server']}.surveycto.com/api/v2/datasets/{config['dataset_id']}/records"
        if config["dataset_id"]
        else f"https://{config['server']}.surveycto.com/api/v2/forms/data/wide/json/{config['form_id']}"
    )

    return SurveyCTOStatusResponse(
        surveycto_server=config["server"],
        surveycto_username=config["username"],
        surveycto_main_form_id=config["form_id"],
        surveycto_dataset_id=config["dataset_id"],
        surveycto_instrument_code=config["instrument_code"],
        surveycto_date=config["date"],
        surveycto_password_configured=bool(config["password"]),
        surveycto_endpoint=surveycto_endpoint,
        raw_submission_count=raw_count,
        last_sync_at=last_sync_at,
        surveycto_connection_ok=connection_ok,
        surveycto_connection_message=connection_message,
        surveycto_pull_ok=pull_ok,
        surveycto_pull_message=pull_message,
        surveycto_raw_items=surveycto_raw_items,
        surveycto_normalized_items=surveycto_normalized_items,
        surveycto_first_item_keys=first_item_keys,
        surveycto_sync_stored=_last_surveycto_sync["stored"],
        surveycto_sync_updated=_last_surveycto_sync["updated"],
        surveycto_sync_skipped_submission_id=_last_surveycto_sync["skipped_submission_id"],
        surveycto_sync_message=_last_surveycto_sync["message"],
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
        issue_id=str(issue.issue_id),
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
