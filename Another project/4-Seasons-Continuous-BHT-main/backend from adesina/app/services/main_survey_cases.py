from __future__ import annotations

import json
import base64
import logging
import math
import random
import re
import time
from collections import Counter, defaultdict
from statistics import median
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from uuid import uuid4

import pandas as pd
import pyreadstat
import psycopg
from fastapi import HTTPException
from psycopg import sql

from backend.app.auth import AuthUser, EDIT_ROLES
from backend.app.database import db_connection
from backend.app.services.qc_productivity import (
    build_qc_productivity_by_date,
    normalize_qc_productivity_queue,
    summarize_qc_task_rows,
)
from backend.app.services.main_custom_table import _apply_label, _clean_label_text, _load_all_value_label_maps
from backend.app.services.main_data_scope import main_case_effective_datetime_sql, main_case_scope_clause, main_row_effective_datetime_sql, main_row_scope_clause
from backend.app.services.main_survey import BHT_CATEGORY_BAU5A_PREFIX, BHT_CATEGORY_PANEL_MAP, BHT_PANEL_LABEL_BY_CODE, SECTOR_LABELS as MAIN_SECTOR_LABELS, _is_helper_variable, _load_dictionary
from backend.app.settings import Settings


INSTRUMENT = "main"

MAIN_STATUSES = ["submitted", "pending_review", "in_review", "corrected", "approved", "rejected"]
MAIN_REVIEW_DECISION_ROLES = EDIT_ROLES
MAIN_FINAL_STATUSES = {"approved", "rejected"}
AUTO_APPROVED_CORRECTION_NOTE = "Correction submitted and automatically applied."
DISABLED_AUTO_APPROVAL_NOTE_PREFIX = "Automatically approved for export:"
AUTO_APPROVAL_ROLLBACK_NOTE = "Returned to pending review because automatic case approval is disabled."

logger = logging.getLogger(__name__)
MAIN_CASE_LIST_CACHE_TTL_SECONDS = 60
MAIN_CASE_LIST_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
MAIN_CASE_DETAIL_CACHE_TTL_SECONDS = 120
MAIN_CASE_DETAIL_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
MAIN_CASE_NAV_CACHE_TTL_SECONDS = 300
MAIN_CASE_NAV_CACHE: tuple[float, dict[str, dict[str, Any]]] | None = None
MAIN_CASE_QUEUE_OPTIONS_CACHE_TTL_SECONDS = 300
MAIN_CASE_QUEUE_OPTIONS_CACHE: tuple[float, dict[str, list[str]]] | None = None


def _clear_main_case_list_cache() -> None:
    global MAIN_CASE_NAV_CACHE, MAIN_CASE_QUEUE_OPTIONS_CACHE
    MAIN_CASE_LIST_CACHE.clear()
    MAIN_CASE_DETAIL_CACHE.clear()
    MAIN_CASE_NAV_CACHE = None
    MAIN_CASE_QUEUE_OPTIONS_CACHE = None


def _clear_main_status_dependent_caches(settings: Settings | None = None) -> None:
    _clear_main_case_list_cache()
    try:
        from backend.app.services.main_survey import clear_bht_analytics_caches

        clear_bht_analytics_caches(settings)
    except Exception:
        logger.exception("Unable to clear BHT analytics caches after Main case status change.")


def _is_transient_db_lock_error(exc: BaseException) -> bool:
    """Return True for PostgreSQL lock/deadlock errors that are safe to retry."""
    return isinstance(exc, psycopg.Error) and getattr(exc, "sqlstate", None) in {"40P01", "55P03", "57014"}


def _sleep_before_db_retry(attempt: int) -> None:
    # Import locally to keep module startup lightweight and avoid changing existing import order.
    import time

    time.sleep(min(0.25 * (2 ** attempt), 2.0))


def normalize_main_interviewer_id(value: Any) -> str:
    """Return the app-facing interviewer code without SurveyCTO auth suffixes."""
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    return text.lower() or "Unknown"


def _main_interviewer_sql(*expressions: str, fallback: str = "'Unknown'") -> str:
    source = "COALESCE(" + ", ".join(f"NULLIF(TRIM({expr}), '')" for expr in expressions) + f", {fallback})"
    return f"COALESCE(NULLIF(LOWER(TRIM(REGEXP_REPLACE({source}, '\\s*\\([^)]*\\)\\s*$', ''))), ''), 'Unknown')"


NIGERIA_GPS_BOUNDS = {"lat_min": 4.0, "lat_max": 14.5, "lon_min": 2.5, "lon_max": 15.0}

MAIN_CASE_LIST_SUPPORT_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS mart",
    "CREATE SCHEMA IF NOT EXISTS qc",
    "ALTER TABLE IF EXISTS app.user_account ADD COLUMN IF NOT EXISTS username text",
    "ALTER TABLE IF EXISTS app.user_account ADD COLUMN IF NOT EXISTS full_name text",
    """
    CREATE TABLE IF NOT EXISTS mart.main_case_dim (
        case_id text PRIMARY KEY,
        submission_key text,
        approval_stage text,
        state_name text,
        gender text,
        age_group text,
        sec_class text,
        interview_month text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE IF EXISTS mart.main_case_dim ADD COLUMN IF NOT EXISTS state_name text",
    """
    CREATE TABLE IF NOT EXISTS clean.deleted_main_cases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        submission_key text NOT NULL UNIQUE,
        case_id text,
        deleted_by text,
        deleted_at timestamptz DEFAULT now(),
        reason text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS qc.issue_queue (
        issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        rule_result_id uuid,
        instrument_code text NOT NULL,
        submission_key text,
        case_id text,
        issue_status text NOT NULL DEFAULT 'pending_review',
        assigned_to_user_id uuid,
        assigned_to_role text,
        issue_summary text NOT NULL DEFAULT '',
        resolution_note text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        resolved_at timestamptz
    )
    """,
    "ALTER TABLE IF EXISTS qc.issue_queue ADD COLUMN IF NOT EXISTS issue_status text NOT NULL DEFAULT 'pending_review'",
    """
    CREATE TABLE IF NOT EXISTS qc.pending_change (
        change_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        instrument_code text NOT NULL DEFAULT 'main',
        submission_key text,
        case_id text,
        table_name text NOT NULL DEFAULT '',
        row_identifier text,
        field_name text NOT NULL DEFAULT '',
        current_value text,
        proposed_value text,
        change_reason text NOT NULL DEFAULT '',
        change_status text NOT NULL DEFAULT 'pending',
        issue_id uuid,
        requested_by_user_id uuid,
        reviewed_by_user_id uuid,
        requested_device_id text,
        reviewed_device_id text,
        requested_at timestamptz NOT NULL DEFAULT now(),
        reviewed_at timestamptz,
        review_note text
    )
    """,
    "ALTER TABLE IF EXISTS qc.pending_change ADD COLUMN IF NOT EXISTS case_id text",
    """
    CREATE TABLE IF NOT EXISTS qc.case_status_history (
        status_history_id uuid DEFAULT gen_random_uuid(),
        instrument_code text NOT NULL,
        submission_key text,
        case_id text,
        previous_status text,
        new_status text NOT NULL,
        changed_by_user_id uuid,
        change_note text,
        changed_at timestamptz NOT NULL DEFAULT now(),
        device_id text
    )
    """,
    "ALTER TABLE IF EXISTS qc.case_status_history ADD COLUMN IF NOT EXISTS status_history_id uuid DEFAULT gen_random_uuid()",
    "ALTER TABLE IF EXISTS qc.case_status_history ADD COLUMN IF NOT EXISTS device_id text",
    """
    CREATE TABLE IF NOT EXISTS qc.callback_outcome (
        callback_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id text NOT NULL,
        sampled_flag boolean NOT NULL DEFAULT false,
        attempt_no integer NOT NULL DEFAULT 1,
        outcome_code text NOT NULL DEFAULT 'pending',
        outcome_note text,
        assigned_to_user_id uuid,
        completed_by_user_id uuid,
        completed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE IF EXISTS qc.callback_outcome ADD COLUMN IF NOT EXISTS outcome_code text NOT NULL DEFAULT 'pending'",
    """
    CREATE TABLE IF NOT EXISTS clean.audio_listening (
        audio_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        case_id text NOT NULL,
        assigned_to_user_id text,
        assigned_to_role text,
        status text DEFAULT 'pending',
        quality_rating text,
        reviewer_note text,
        audio_url text,
        created_at timestamptz DEFAULT now(),
        reviewed_at timestamptz
    )
    """,
    "ALTER TABLE IF EXISTS clean.audio_listening ADD COLUMN IF NOT EXISTS status text DEFAULT 'pending'",
)

# Expected section names â€” cases missing all of these trigger MAIN_SECTION_MISSING
REQUIRED_SECTIONS = [
    "B. PARTICULARS OF VISIT",
    "C. INTRODUCTION AND SCREENING QUESTIONS",
    "D. HOUSEHOLD QUESTIONS",
    "E. DEMOGRAPHICS",
    "F. FINANCIAL CAPABILITY",
]

MAIN_INTERVIEW_MIN_MINUTES = 10
MAIN_INTERVIEW_MAX_MINUTES = 180
MAIN_MIN_GAP_BETWEEN_INTERVIEWS_MINUTES = 5
MAIN_EXPORT_TEMPLATE_FILE = "Main_Survey_Data Dictionary_latest.sav"
MAIN_ENUMERATOR_MATRIX_MIN_CASES = 10
MAIN_ENUMERATOR_MATRIX_THRESHOLDS = {
    "Gender": 0.90,
    "Age_cal": 0.80,
    "Sector": 0.85,
    "selected_panel": 0.90,
}

MAIN_PHONE_FIELDS = (
    "Mobile",
)

MAIN_NUMERIC_FLAG_FIELDS: dict[str, str] = {
    "C3a_female": "MAIN_INVALID_C3A_FEMALE",
    "C3a_male": "MAIN_INVALID_C3A_MALE",
    "C5": "MAIN_INVALID_C5",
    "HHTotal": "MAIN_INVALID_HHTOTAL",
    "LC5c": "MAIN_INVALID_LC5C",
    "IE1a": "MAIN_INVALID_IE1A",
}

EDITABLE_TABLES: dict[str, str] = {
    "clean.main_case": "submission_key",
    "clean.main_case_section": "section_row_id",
    "clean.main_case_roster": "roster_id",
}

STRUCTURED_FIELDS: dict[str, set[str]] = {
    "clean.main_case": {"approval_stage", "case_id", "ea_id"},
    "clean.main_case_section": {"section_name", "row_no"},
    "clean.main_case_roster": {"roster_type", "row_no"},
}

CALLBACK_OUTCOME_CODES = [
    "pending",
    "completed",
    "no_answer",
    "refused",
    "wrong_number",
    "rescheduled",
    "phone_switched_off",
    "unreachable",
    "temporary_network_failure",
]

VERIFICATION_ELIGIBLE_SECTIONS = [
    "F. FINANCIAL CAPABILITY",
    "CONSUMER PROTECTION & FRAUD",
    "QF: QUALITY OF FINANCIAL SERVICES",
    "BA: COMMERCIAL BANKS",
    "MF: MICROFINANCE & DIGITAL MICROFINANCE",
    "NB: NON-INTEREST BANKING",
    "PY: PAYMENT",
    "MM: MOBILE MONEY",
    "MT: MONEY TRANSFER",
    "SA: SAVINGS",
    "LC: LOANS & CREDIT",
    "RM: RISK MANAGEMENT AND INSURANCE",
    "GOVERNMENT POLICES",
    "INF: INFORMAL SERVICE PROVIDERS",
    "PC: POTENTIAL CHANNELS FOR CONDUCTING FINANCIAL TRANSCATIONS",
    "IE: INCOME AND EXPENDITURE",
    "GEN: GENDER ROLES/NORMS",
]

OMNIBUS_VERIFICATION_SECTION_TOKENS = ("omnibus",)
OMNIBUS_VERIFICATION_PREFIXES = ("OB_",)
PANEL_VERIFICATION_SECTION_LABELS = {
    "Panel_1": {"noodles"},
    "Panel_2": {"toothpaste"},
    "Panel_3": {"edible oil"},
    "Panel_4": {"bleach"},
    "Panel_5": {"toilet cleaner"},
    "Panel_6": {"snacks products", "snacks"},
    "Panel_7": {"breakfast cereal", "breakfast cereals"},
    "Panel_8": {"condiment mixes"},
    "Panel_9": {"wet hair"},
    "Panel_10": {"dry hair"},
    "Panel_11": {"malt beverage", "malt"},
}

RULE_DEFINITIONS: list[tuple[str, str, str, str | None, str, str, str, str]] = [
    ("MAIN_LOW_LOI", INSTRUMENT, "clean.main_case", "record", "high", "python", "Interview duration is below 50% of main median LOI.", "flag_for_review"),
    ("MAIN_HIGH_LOI", INSTRUMENT, "clean.main_case", "record", "high", "python", "Interview duration is above 150% of main median LOI.", "flag_for_review"),
    ("MAIN_START_TIME", INSTRUMENT, "clean.main_case", "record", "high", "python", "Interview occurred during odd hours (7:00 PM to 6:59 AM).", "flag_for_review"),
    ("MAIN_DUPLICATE_PHONE_NUMBER", INSTRUMENT, "clean.main_case", "record", "high", "python", "Respondent phone number is duplicated within the same interviewer.", "flag_for_review"),
    ("MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL", INSTRUMENT, "clean.main_case", "record", "high", "python", "Respondent phone number is duplicated across the active dataset.", "flag_for_review"),
    ("MAIN_DUPLICATE_GPS", INSTRUMENT, "clean.main_case", "record", "high", "python", "Identical GPS coordinates appear in another main interview by the same interviewer.", "flag_for_review"),
    ("MAIN_GAP_BETWEEN_2_INTERVIEWS", INSTRUMENT, "clean.main_case", "record", "high", "python", "Gap between consecutive interviews by the same interviewer is below 5 minutes.", "flag_for_review"),
    ("MAIN_TIME_INTERWOVEN", INSTRUMENT, "clean.main_case", "record", "high", "python", "Two interviews by the same interviewer overlap by more than 1 minute.", "flag_for_review"),
    ("MAIN_ENUMERATOR_MATRIX_ANOMALY", INSTRUMENT, "clean.main_case", "record", "high", "python", "Interviewer-level matrix/profile distribution is unusually concentrated.", "flag_for_review"),
    ("MAIN_LC5C", INSTRUMENT, "clean.main_case_section", "LC5c", "medium", "python", "LC5c exceeds 5000, is below 100, or does not end with 0.", "flag_for_review"),
    ("MAIN_C3A_FEMALE", INSTRUMENT, "clean.main_case_section", "C3a_female", "medium", "python", "C3a_female is greater than 5.", "flag_for_review"),
    ("MAIN_C3A_MALE", INSTRUMENT, "clean.main_case_section", "C3a_male", "medium", "python", "C3a_male is greater than 5.", "flag_for_review"),
    ("MAIN_C5", INSTRUMENT, "clean.main_case_section", "C5", "medium", "python", "C5 is greater than 10.", "flag_for_review"),
    ("MAIN_HHTOTAL", INSTRUMENT, "clean.main_case_section", "HHTotal", "medium", "python", "HHTotal is greater than 15.", "flag_for_review"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_record(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=True)


def _ensure_main_case_list_support(cur: Any) -> None:
    for statement in MAIN_CASE_LIST_SUPPORT_STATEMENTS:
        cur.execute(statement)


def _normalize_json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _looks_explicitly_numeric(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    text = str(value).strip().lower()
    return "." in text or "e" in text


def _canonical_numeric_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return str(int(number)) if number == int(number) else str(number)


def _answer_values_match(current_value: Any, expected_value: Any) -> bool:
    force_numeric = _looks_explicitly_numeric(current_value) or _looks_explicitly_numeric(expected_value)

    def _variants(value: Any) -> set[str]:
        if value is None:
            return set()
        variants = {str(value).strip()}
        if force_numeric:
            normalized = _canonical_numeric_text(value)
            if normalized is not None:
                variants.add(normalized)
        return {variant for variant in variants if variant != ""}

    current_variants = _variants(current_value)
    expected_variants = _variants(expected_value)
    return bool(current_variants and expected_variants and current_variants.intersection(expected_variants))


def _coerce_structured_value(field_name: str, value: str) -> Any:
    return value


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


SECTOR_LABELS = MAIN_SECTOR_LABELS


def _main_choice_label(settings: Settings, variable_name: str, value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if variable_name == "Sector":
        normalized = raw[:-2] if raw.endswith(".0") else raw
        return MAIN_SECTOR_LABELS.get(raw) or MAIN_SECTOR_LABELS.get(normalized) or MAIN_SECTOR_LABELS.get(f"{normalized}.0") or raw

    fallback_labels = _choice_label_map_from_xlsform(str(settings.root_dir), variable_name)
    fallback = fallback_labels.get(raw) or fallback_labels.get(raw[:-2] if raw.endswith(".0") else raw) or fallback_labels.get(f"{raw}.0")
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT choice_list_name
                    FROM reference.xlsform_question
                    WHERE instrument_code = 'main'
                      AND variable_name = %s
                    LIMIT 1
                    """,
                    (variable_name,),
                )
                question = cur.fetchone() or {}
                list_name = str(question.get("choice_list_name") or "").strip()
                if not list_name:
                    return raw
                normalized = raw[:-2] if raw.endswith(".0") else raw
                cur.execute(
                    """
                    SELECT choice_label
                    FROM reference.xlsform_choice
                    WHERE instrument_code = 'main'
                      AND list_name = %s
                      AND choice_code = ANY(%s)
                    LIMIT 1
                    """,
                    (list_name, [raw, normalized, f"{normalized}.0"]),
                )
                row = cur.fetchone() or {}
                return str(row.get("choice_label") or fallback or raw)
    except Exception:
        logger.debug("Unable to load %s label for value %s", variable_name, raw, exc_info=True)
        return fallback or raw


def _normalize_phone(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    digits = re.sub(r"\D+", "", str(value))
    if len(digits) < 7:
        return None
    return digits


def _extract_phone_candidates(case_record: dict[str, Any], sections: list[dict[str, Any]]) -> list[str]:
    phones: list[str] = []
    for key in MAIN_PHONE_FIELDS:
        normalized = _normalize_phone(case_record.get(key))
        if normalized:
            phones.append(normalized)
    for section in sections:
        rec = section.get("record") or {}
        if not isinstance(rec, dict):
            continue
        for key in MAIN_PHONE_FIELDS:
            normalized = _normalize_phone(rec.get(key))
            if normalized:
                phones.append(normalized)
    return phones


@lru_cache(maxsize=1)
def _main_parse_numeric_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0 or int(num) != num:
        return None
    return int(num)


def _main_find_value(records: list[dict[str, Any]], field_name: str) -> Any:
    for rec in records:
        if isinstance(rec, dict) and field_name in rec and rec.get(field_name) not in {None, ""}:
            return rec.get(field_name)
    return None


def _main_parse_numeric(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0:
        return None
    return num


def _main_validate_integer_in_range(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int | None:
    parsed = _main_parse_numeric(value)
    if parsed is None or int(parsed) != parsed:
        return None
    integer = int(parsed)
    if integer < minimum:
        return None
    if maximum is not None and integer > maximum:
        return None
    return integer


def _main_validate_amount(value: Any, *, minimum: float = 0.0, maximum: float = 1_000_000_000.0) -> float | None:
    parsed = _main_parse_numeric(value)
    if parsed is None:
        return None
    if parsed < minimum or parsed > maximum:
        return None
    return parsed


MAIN_NUMERIC_VALIDATORS: dict[str, tuple[str, Any]] = {
    "C3a_female": ("0-30 household members", lambda value: _main_validate_integer_in_range(value, minimum=0, maximum=30)),
    "C3a_male": ("0-30 household members", lambda value: _main_validate_integer_in_range(value, minimum=0, maximum=30)),
    "C5": ("0-60 household members", lambda value: _main_validate_integer_in_range(value, minimum=0, maximum=60)),
    "HHTotal": ("1-60 household members", lambda value: _main_validate_integer_in_range(value, minimum=1, maximum=60)),
    "LC5c": ("0-1,000,000,000", lambda value: _main_validate_amount(value)),
    "IE1a": ("0-1,000,000,000", lambda value: _main_validate_amount(value)),
}


def _main_create_numeric_field_issues(cur: Any, submission_key: str, records_to_scan: list[dict[str, Any]]) -> int:
    created = 0
    parsed_values: dict[str, float] = {}
    for field_name, rule_code in MAIN_NUMERIC_FLAG_FIELDS.items():
        raw_value = _main_find_value(records_to_scan, field_name)
        if raw_value in {None, ""}:
            continue
        threshold_label, validator = MAIN_NUMERIC_VALIDATORS.get(
            field_name,
            ("valid non-negative number", lambda value: _main_validate_amount(value)),
        )
        parsed = validator(raw_value)
        if parsed is None:
            created += _create_issue(
                cur,
                submission_key,
                rule_code,
                "high" if field_name == "HHTotal" else "medium",
                f"{field_name} has invalid numeric value: {raw_value}. Expected range/format: {threshold_label}.",
                None,
                field_name,
                "clean.main_case",
            )
            continue
        parsed_values[field_name] = parsed

    female = parsed_values.get("C3a_female")
    male = parsed_values.get("C3a_male")
    hh_total = parsed_values.get("HHTotal")
    if hh_total is not None and female is not None and male is not None and int(hh_total) != int(female + male):
        created += _create_issue(
            cur, submission_key, "MAIN_HHTOTAL_MISMATCH", "high",
            f"HHTotal ({int(hh_total)}) does not match C3a_female + C3a_male ({int(female + male)}).",
            None, "HHTotal", "clean.main_case",
        )

    return created


def _main_template_numeric_fields(template_path: str) -> set[str]:
    _, meta = pyreadstat.read_sav(template_path, row_limit=0)
    numeric_cols: set[str] = set()
    original_types = getattr(meta, "original_variable_types", {}) or {}
    for col_name in (meta.column_names or []):
        raw_type = str(original_types.get(col_name, "")).lower()
        if any(token in raw_type for token in ("f", "n", "num", "double", "float")):
            numeric_cols.add(str(col_name))
    return numeric_cols


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for fmt in ("%b %d, %Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(str(value).strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _ensure_status_allowed(new_status: str) -> None:
    if new_status not in MAIN_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unsupported status '{new_status}'.")


def _enforce_case_visibility(user: AuthUser, case_status: str) -> None:
    return None


def _extract_gps(record: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return the BHT outlet GPS point as (lat, lon), supporting split and combined SurveyCTO fields."""
    lat = _safe_float(record.get("outlet_gps_Latitude"))
    lon = _safe_float(record.get("outlet_gps_Longitude"))
    if lat is not None and lon is not None:
        return lat, lon

    for field in ("outlet_gps",):
        raw = record.get(field)
        if raw and isinstance(raw, str):
            parts = raw.split()
            if len(parts) >= 2:
                lat = _safe_float(parts[0])
                lon = _safe_float(parts[1])
                if lat is not None and lon is not None:
                    return lat, lon
    return None, None


def _first_nonblank_record_value(record: dict[str, Any], field_names: tuple[str, ...]) -> str | None:
    lowered = {str(key).lower(): key for key in record.keys()}
    for field in field_names:
        value = record.get(field)
        if value is None:
            key = lowered.get(field.lower())
            value = record.get(key) if key else None
        text = str(value or "").strip()
        if text:
            return text
    return None


def _main_respondent_title_label(settings: Settings, value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    label = _main_choice_label(settings, "Resp_Title", raw)
    return label or raw


def _duplicate_phone_affected_case_labels(
    cur: Any,
    settings: Settings,
    *,
    current_submission_key: str,
    current_case_id: str,
    interviewer_id: str,
    phone_values: list[str],
    issue_summary: str,
    global_scope: bool = False,
) -> list[dict[str, str]]:
    if not issue_summary:
        return []
    if not global_scope and not interviewer_id:
        return []
    interviewer_sql = "" if global_scope else "AND mc.interviewer_id = %s"
    params: list[Any] = ["MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL" if global_scope else "MAIN_DUPLICATE_PHONE_NUMBER", issue_summary]
    if not global_scope:
        params.append(interviewer_id)
    params.extend([current_submission_key, current_case_id])
    cur.execute(
        f"""
        WITH matched_cases AS (
            SELECT
                mc.submission_key,
                mc.case_id,
                mq.region_label,
                mq.region_respondent_ordinal
            FROM clean.main_case mc
            INNER JOIN mart.main_case_queue mq ON mq.case_id = mc.case_id
            INNER JOIN qc.issue_queue iq
                ON iq.instrument_code = 'main'
               AND iq.submission_key = mc.submission_key
            INNER JOIN qc.rule_result rr
                ON rr.rule_result_id = iq.rule_result_id
               AND rr.instrument_code = 'main'
               AND rr.rule_code = %s
            WHERE COALESCE(iq.issue_summary, rr.result_message, '') = %s
              {interviewer_sql}
              AND COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), '')) NOT IN (%s, %s)
              AND NOT EXISTS (
                  SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key
              )
            ORDER BY mq.submitted_at DESC NULLS LAST, mq.case_id DESC
            LIMIT 20
        )
        SELECT
            matched_cases.submission_key,
            matched_cases.region_label,
            matched_cases.region_respondent_ordinal
        FROM matched_cases
        """,
        tuple(params),
    )
    city_labels = _city_choice_label_map(str(settings.root_dir))
    affected: list[dict[str, str]] = []
    for row in cur.fetchall():
        region = str(row.get("region_label") or "").strip()
        label_region = city_labels.get(region, region) or "Region"
        ordinal = row.get("region_respondent_ordinal") or 0
        affected.append(
            {
                "submission_key": str(row.get("submission_key") or ""),
                "case_label": f"{label_region}_Resp._{ordinal}",
            }
        )
    return affected


def _insert_case_status_history(
    cur: Any,
    submission_key: str,
    case_id: str | None,
    previous_status: str | None,
    new_status: str,
    user: AuthUser | None,
    note: str | None = None,
    device_id: str | None = None,
) -> None:
    changed_by_user_id = user.id if user is not None else None
    cur.execute(
        """
        INSERT INTO qc.case_status_history (
            instrument_code,
            submission_key,
            case_id,
            previous_status,
            new_status,
            changed_by_user_id,
            change_note,
            device_id
        )
        VALUES ('main', %s, %s, %s, %s, %s, %s, %s)
        """,
        (submission_key, case_id, previous_status, new_status, changed_by_user_id, note or "", device_id),
    )


def bootstrap_main_case_status_reconciliation(settings: Settings) -> None:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest_status_action AS (
                    SELECT DISTINCT ON (h.submission_key)
                        h.submission_key,
                        h.new_status,
                        h.change_note
                    FROM qc.case_status_history h
                    WHERE h.instrument_code = 'main'
                    ORDER BY h.submission_key, h.changed_at DESC, h.status_history_id DESC
                ),
                auto_approved_cases AS (
                    SELECT
                        mc.submission_key,
                        mc.case_id,
                        mc.approval_stage AS previous_status
                    FROM clean.main_case mc
                    LEFT JOIN latest_status_action latest
                      ON latest.submission_key = mc.submission_key
                    WHERE LOWER(COALESCE(mc.approval_stage, '')) = 'auto_approved'
                       OR (
                            LOWER(COALESCE(mc.approval_stage, '')) = 'approved'
                        AND LOWER(COALESCE(latest.new_status, '')) = 'approved'
                        AND COALESCE(latest.change_note, '') ILIKE %s
                       )
                ),
                reverted AS (
                    UPDATE clean.main_case mc
                    SET approval_stage = 'pending_review',
                        approved_at = NULL,
                        is_callback_required = false,
                        updated_at = now()
                    FROM auto_approved_cases candidate
                    WHERE mc.submission_key = candidate.submission_key
                    RETURNING mc.submission_key, mc.case_id, candidate.previous_status
                )
                INSERT INTO qc.case_status_history (
                    instrument_code,
                    submission_key,
                    case_id,
                    previous_status,
                    new_status,
                    changed_by_user_id,
                    change_note
                )
                SELECT
                    'main',
                    reverted.submission_key,
                    reverted.case_id,
                    reverted.previous_status,
                    'pending_review',
                    NULL,
                    %s
                FROM reverted
                """,
                (f"{DISABLED_AUTO_APPROVAL_NOTE_PREFIX}%", AUTO_APPROVAL_ROLLBACK_NOTE),
            )
            cur.execute(
                f"""
                WITH latest_qc_decision AS (
                    SELECT DISTINCT ON (h.submission_key)
                        h.submission_key,
                        h.new_status
                    FROM qc.case_status_history h
                    JOIN app.user_role ur
                      ON ur.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'main'
                      AND LOWER(COALESCE(h.new_status, '')) IN ('approved', 'rejected')
                      AND COALESCE(h.change_note, '') NOT ILIKE %s
                      AND ur.role_code = ANY(%s)
                    ORDER BY h.submission_key, h.changed_at DESC
                )
                UPDATE clean.main_case mc
                SET approval_stage = latest_qc_decision.new_status,
                    is_callback_required = false,
                    updated_at = now()
                FROM latest_qc_decision
                WHERE mc.submission_key = latest_qc_decision.submission_key
                  AND LOWER(COALESCE(mc.approval_stage, '')) IN ('submitted', 'pending_review', 'in_review', 'corrected')
                  AND LOWER(COALESCE(mc.approval_stage, '')) <> LOWER(COALESCE(latest_qc_decision.new_status, ''))
                """,
                (f"{DISABLED_AUTO_APPROVAL_NOTE_PREFIX}%", list(MAIN_REVIEW_DECISION_ROLES)),
            )
        conn.commit()


def _create_issue(
    cur: Any,
    submission_key: str,
    rule_code: str,
    severity: str,
    message: str,
    row_identifier: str | None,
    field_name: str | None,
    table_name: str,
) -> int:
    cur.execute(
        """
        INSERT INTO qc.rule_result (
            rule_code,
            instrument_code,
            submission_key,
            table_name,
            row_identifier,
            field_name,
            severity,
            result_status,
            result_message
        )
        VALUES (%s, 'main', %s, %s, %s, %s, %s, 'open', %s)
        RETURNING rule_result_id
        """,
        (rule_code, submission_key, table_name, row_identifier, field_name, severity, message),
    )
    rule_result_id = cur.fetchone()["rule_result_id"]
    cur.execute(
        """
        INSERT INTO qc.issue_queue (
            rule_result_id,
            instrument_code,
            submission_key,
            issue_status,
            issue_summary
        )
        VALUES (%s, 'main', %s, 'pending_review', %s)
        """,
        (rule_result_id, submission_key, message),
    )
    return 1


# ---------------------------------------------------------------------------
# Rule bootstrap
# ---------------------------------------------------------------------------

def bootstrap_main_rule_definitions(settings: Settings) -> None:
    rule_codes = [rule[0] for rule in RULE_DEFINITIONS]
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM qc.issue_queue
                WHERE instrument_code = 'main'
                  AND rule_result_id IN (
                      SELECT rule_result_id
                      FROM qc.rule_result
                      WHERE instrument_code = 'main'
                        AND (
                            rule_code LIKE 'MAIN_AGE%'
                            OR rule_code IN ('MAIN_E7', 'MAIN_INVALID_E7', 'MAIN_E13B', 'MAIN_INVALID_E13B', 'MAIN_STRAIGHTLINING')
                        )
                  )
                """
            )
            cur.execute(
                """
                DELETE FROM qc.rule_result
                WHERE instrument_code = 'main'
                  AND (
                      rule_code LIKE 'MAIN_AGE%'
                      OR rule_code IN ('MAIN_E7', 'MAIN_INVALID_E7', 'MAIN_E13B', 'MAIN_INVALID_E13B', 'MAIN_STRAIGHTLINING')
                  )
                """
            )
            cur.execute(
                """
                DELETE FROM qc.rule_definition
                WHERE instrument_code = 'main'
                  AND (
                      rule_code LIKE 'MAIN_AGE%'
                      OR rule_code IN ('MAIN_E7', 'MAIN_INVALID_E7', 'MAIN_E13B', 'MAIN_INVALID_E13B', 'MAIN_STRAIGHTLINING')
                  )
                """
            )
            cur.execute(
                """
                UPDATE qc.rule_definition
                SET is_active = false
                WHERE instrument_code = 'main'
                  AND rule_code <> ALL(%s)
                """,
                (rule_codes,),
            )
            for rule_code, instrument_code, target_table, target_field, severity, rule_type, description, action in RULE_DEFINITIONS:
                cur.execute(
                    """
                    INSERT INTO qc.rule_definition (
                        rule_code,
                        instrument_code,
                        target_table,
                        target_field,
                        severity,
                        rule_type,
                        description,
                        recommended_action,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)
                    ON CONFLICT (rule_code) DO UPDATE SET
                        target_table = EXCLUDED.target_table,
                        target_field = EXCLUDED.target_field,
                        severity = EXCLUDED.severity,
                        rule_type = EXCLUDED.rule_type,
                        description = EXCLUDED.description,
                        recommended_action = EXCLUDED.recommended_action,
                        is_active = true
                    """,
                    (rule_code, instrument_code, target_table, target_field, severity, rule_type, description, action),
                )
        conn.commit()




def _ensure_main_qc_generated(settings: Settings) -> None:
    """Generate main QC findings on demand when the case table has data but QC rows are still empty."""
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*)::int AS total FROM clean.main_case")
            total_cases = int((cur.fetchone() or {}).get("total", 0) or 0)
            if total_cases <= 0:
                return
            cur.execute("SELECT COUNT(*)::int AS total FROM qc.rule_result WHERE instrument_code = 'main'")
            total_qc = int((cur.fetchone() or {}).get("total", 0) or 0)
    if total_qc == 0:
        run_main_qc(settings)


def refresh_main_operational_marts(settings: Settings) -> dict[str, Any]:
    """Refresh operational marts used by high-traffic Main dashboard pages."""
    rule_codes = [rule[0] for rule in RULE_DEFINITIONS]
    city_expr = _city_label_sql("mc.record", str(settings.root_dir))
    city_expr_all = _city_label_sql("mc_all.record", str(settings.root_dir))
    panel_label_expr = _panel_label_sql("mcp.panel_code")
    rule_count_pairs = ", ".join(
        f"'{rule_code.lower()}', COALESCE(MAX(rf.flag_count) FILTER (WHERE rf.rule_code = '{rule_code}'), 0)"
        for rule_code in rule_codes
    )
    mc_scope_clause, mc_scope_params = main_case_scope_clause(settings, "mc")
    mc_all_scope_clause, mc_all_scope_params = main_case_scope_clause(settings, "mc_all")
    mc_productivity_scope_clause, mc_productivity_scope_params = main_case_scope_clause(settings, "mc")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # Operational marts are rebuilt by the ETL worker after a successful
            # sync, not by an interactive page request.
            cur.execute("SET LOCAL statement_timeout = '10min'")
            cur.execute("DELETE FROM mart.main_case_queue")
            cur.execute(
                f"""
                WITH all_case_ordinals AS (
                    SELECT
                        ranked.case_id,
                        ranked.region_label,
                        ranked.region_respondent_ordinal
                    FROM (
                        SELECT
                            mc_all.case_id,
                            COALESCE(
                                NULLIF(TRIM({city_expr_all}), ''),
                                NULLIF(TRIM(mcd_all.state_name), ''),
                                NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                                NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                                'Region'
                            ) AS region_label,
                            ROW_NUMBER() OVER (
                                PARTITION BY COALESCE(
                                    NULLIF(TRIM({city_expr_all}), ''),
                                    NULLIF(TRIM(mcd_all.state_name), ''),
                                    NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                                    NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                                    'Region'
                                )
                                ORDER BY mc_all.submitted_at ASC NULLS LAST, mc_all.created_at ASC NULLS LAST, mc_all.case_id ASC
                            )::int AS region_respondent_ordinal
                        FROM clean.main_case mc_all
                        LEFT JOIN mart.main_case_dim mcd_all ON mcd_all.case_id = mc_all.case_id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc_all.submission_key
                        )
                        {mc_all_scope_clause}
                    ) ranked
                ),
                section_counts AS (
                    SELECT case_id, COUNT(DISTINCT section_name)::int AS section_count
                    FROM clean.main_case_section
                    GROUP BY case_id
                ),
                issue_counts AS (
                    SELECT
                        COALESCE(NULLIF(TRIM(submission_key), ''), NULLIF(TRIM(case_id), '')) AS join_key,
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issue_count
                    FROM qc.issue_queue
                    WHERE instrument_code = 'main'
                    GROUP BY COALESCE(NULLIF(TRIM(submission_key), ''), NULLIF(TRIM(case_id), ''))
                ),
                pending_counts AS (
                    SELECT
                        COALESCE(NULLIF(TRIM(submission_key), ''), NULLIF(TRIM(case_id), '')) AS join_key,
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(change_status), ''), 'pending') = 'pending')::int AS pending_change_count
                    FROM qc.pending_change
                    WHERE instrument_code = 'main'
                    GROUP BY COALESCE(NULLIF(TRIM(submission_key), ''), NULLIF(TRIM(case_id), ''))
                ),
                selected_panels AS (
                    SELECT
                        mcp.case_id,
                        ARRAY_AGG(DISTINCT mcp.panel_code ORDER BY mcp.panel_code) AS selected_panel_codes,
                        STRING_AGG(DISTINCT {panel_label_expr}, ', ') AS selected_panel_labels
                    FROM clean.main_case_panel mcp
                    WHERE COALESCE(mcp.is_selected, TRUE)
                    GROUP BY mcp.case_id
                ),
                callback_assignment AS (
                    SELECT DISTINCT ON (cb.case_id)
                        cb.case_id,
                        cb.assigned_to_user_id::text AS callback_assigned_to_user_id,
                        COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS callback_assigned_to_name
                    FROM qc.callback_outcome cb
                    LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
                    WHERE COALESCE(cb.outcome_code, 'pending') = 'pending'
                    ORDER BY cb.case_id, cb.updated_at DESC NULLS LAST, cb.created_at DESC NULLS LAST
                ),
                audio_assignment AS (
                    SELECT DISTINCT ON (al.case_id)
                        al.case_id,
                        al.assigned_to_user_id::text AS audio_assigned_to_user_id,
                        COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS audio_assigned_to_name
                    FROM clean.audio_listening al
                    LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                    WHERE COALESCE(al.status, 'pending') = 'pending'
                    ORDER BY al.case_id, al.created_at DESC NULLS LAST
                )
                INSERT INTO mart.main_case_queue (
                    case_id, submission_key, ea_id, interviewer_id, supervisor_id,
                    approval_stage, submitted_at, start_time, updated_at, ea_name,
                    lga_name, state_name, region_label, region_respondent_ordinal,
                    supacc_confirm, slot_type, username, approved_by, is_auto_approved,
                    final_outcome_code, section_count, open_issue_count, qc_flag_count,
                    pending_change_count, has_callback_history, has_audio_history,
                    callback_assigned_to_user_id, callback_assigned_to_name,
                    audio_assigned_to_user_id, audio_assigned_to_name,
                    selected_panel_codes, selected_panel_labels, search_text, updated_mart_at
                )
                SELECT
                    mc.case_id,
                    mc.submission_key,
                    mc.ea_id,
                    {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")},
                    mc.supervisor_id,
                    mc.approval_stage,
                    mc.submitted_at,
                    COALESCE(NULLIF(TRIM(mc.record->>'starttime'), ''), NULLIF(TRIM(mc.record->>'start_time'), ''), NULLIF(TRIM(mc.record->>'StartTime'), ''), NULLIF(TRIM(mc.record->>'start'), '')) AS start_time,
                    mc.updated_at,
                    mc.record->>'ea_name',
                    mc.record->>'lga_name',
                    COALESCE(NULLIF(TRIM(mcd.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), '')),
                    COALESCE(aco.region_label, 'Region'),
                    COALESCE(aco.region_respondent_ordinal, 1),
                    COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), '')),
                    mc.record->>'slot_type',
                    {_main_interviewer_sql("mc.record->>'username'", "mc.interviewer_id")},
                    NULL,
                    false,
                    mc.record->>'final_outcome_code',
                    COALESCE(sc.section_count, 0),
                    COALESCE(ic.open_issue_count, 0),
                    COALESCE(ic.open_issue_count, 0),
                    COALESCE(pc.pending_change_count, 0),
                    COALESCE(mc.is_callback_required, false) OR ca.case_id IS NOT NULL,
                    aa.case_id IS NOT NULL,
                    ca.callback_assigned_to_user_id,
                    ca.callback_assigned_to_name,
                    aa.audio_assigned_to_user_id,
                    aa.audio_assigned_to_name,
                    COALESCE(sp.selected_panel_codes, '{{}}'::text[]),
                    COALESCE(sp.selected_panel_labels, 'Omnibus'),
                    CONCAT_WS(' ',
                        mc.submission_key, mc.case_id, mc.ea_id, {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")}, mc.supervisor_id,
                        mc.approval_stage, mc.record->>'ea_name', mc.record->>'lga_name',
                        COALESCE(mcd.state_name, mc.record->>'state_name'), COALESCE(aco.region_label, 'Region'),
                        COALESCE(sp.selected_panel_labels, 'Omnibus'), ca.callback_assigned_to_name, aa.audio_assigned_to_name
                    ),
                    now()
                FROM clean.main_case mc
                LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
                LEFT JOIN all_case_ordinals aco ON aco.case_id = mc.case_id
                LEFT JOIN section_counts sc ON sc.case_id = mc.case_id
                LEFT JOIN issue_counts ic ON ic.join_key = COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), ''))
                LEFT JOIN pending_counts pc ON pc.join_key = COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), ''))
                LEFT JOIN selected_panels sp ON sp.case_id = mc.case_id
                LEFT JOIN callback_assignment ca ON ca.case_id = mc.case_id
                LEFT JOIN audio_assignment aa ON aa.case_id = mc.case_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key
                )
                {mc_scope_clause}
                """
                ,
                [*mc_all_scope_params, *mc_scope_params],
            )

            cur.execute("DELETE FROM mart.enumerator_performance")
            cur.execute(
                f"""
                WITH case_base AS (
                    SELECT
                        {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                        mc.approval_stage,
                        lower(trim(COALESCE(mc.record->>'consent_obtained', ''))) AS consent_value,
                        CASE
                            WHEN COALESCE(mc.record->>'duration', '') ~ '^[0-9]+(\\.[0-9]+)?$'
                                THEN CASE
                                    WHEN (mc.record->>'duration')::numeric > {MAIN_INTERVIEW_MAX_MINUTES}
                                        THEN ((mc.record->>'duration')::numeric / 60.0)
                                    ELSE (mc.record->>'duration')::numeric
                                END
                            ELSE NULL
                        END AS duration_minutes
                    FROM clean.main_case mc
                    WHERE {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} <> 'Unknown'
                      AND NOT EXISTS (
                          SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key
                      )
                      {mc_scope_clause}
                ),
                issue_counts AS (
                    SELECT
                        {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issues,
                        COUNT(*)::int AS total_issues
                    FROM qc.issue_queue iq
                    INNER JOIN clean.main_case mc
                      ON COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), '')) =
                         COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    WHERE iq.instrument_code = 'main'
                      {mc_scope_clause}
                    GROUP BY {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")}
                ),
                rule_flags AS (
                    SELECT
                        {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                        NULLIF(TRIM(rr.rule_code), '') AS rule_code,
                        COUNT(*)::int AS flag_count
                    FROM qc.issue_queue iq
                    INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                    INNER JOIN clean.main_case mc
                      ON COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), '')) =
                         COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    WHERE iq.instrument_code = 'main'
                      AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                      AND NULLIF(TRIM(rr.rule_code), '') IS NOT NULL
                      {mc_scope_clause}
                    GROUP BY {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")}, NULLIF(TRIM(rr.rule_code), '')
                ),
                rule_json AS (
                    SELECT enumerator_id, jsonb_object_agg(lower(rule_code), flag_count) AS rule_counts
                    FROM rule_flags
                    GROUP BY enumerator_id
                ),
                case_stats AS (
                    SELECT
                        enumerator_id,
                        enumerator_id AS enumerator_name,
                        COUNT(*)::int AS total_cases,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'approved')::int AS approved_count,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'rejected')::int AS rejected_count,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) IN ('submitted', 'pending_review', 'in_review', 'corrected'))::int AS pending_count,
                        COUNT(*) FILTER (WHERE consent_value IN ('true', 'yes', '1'))::int AS consent_obtained,
                        COUNT(*) FILTER (WHERE consent_value NOT IN ('true', 'yes', '1'))::int AS consent_refused,
                        ROUND(COALESCE(AVG(duration_minutes), 0), 2) AS avg_duration_minutes
                    FROM case_base
                    GROUP BY enumerator_id
                )
                INSERT INTO mart.enumerator_performance (
                    enumerator_id, enumerator_name, total_cases, approved_count,
                    rejected_count, pending_count, consent_obtained, consent_refused,
                    avg_duration_minutes, avg_sections_completed, open_issues,
                    total_issues, rule_counts, updated_at
                )
                SELECT
                    cs.enumerator_id,
                    cs.enumerator_name,
                    cs.total_cases,
                    cs.approved_count,
                    cs.rejected_count,
                    cs.pending_count,
                    cs.consent_obtained,
                    cs.consent_refused,
                    cs.avg_duration_minutes,
                    0,
                    COALESCE(ic.open_issues, 0),
                    COALESCE(ic.total_issues, 0),
                    COALESCE(rj.rule_counts, '{{}}'::jsonb),
                    now()
                FROM case_stats cs
                LEFT JOIN issue_counts ic ON ic.enumerator_id = cs.enumerator_id
                LEFT JOIN rule_json rj ON rj.enumerator_id = cs.enumerator_id
                """
                ,
                [*mc_scope_params, *mc_scope_params, *mc_scope_params],
            )

            cur.execute("DELETE FROM mart.enumerator_productivity_by_date")
            cur.execute(
                f"""
                INSERT INTO mart.enumerator_productivity_by_date (
                    enumerator_id, date_key, case_count, updated_at
                )
                SELECT
                    {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                    (mc.submitted_at AT TIME ZONE 'UTC')::date AS date_key,
                    COUNT(*)::int AS case_count,
                    now()
                FROM clean.main_case mc
                WHERE {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} <> 'Unknown'
                  AND mc.submitted_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = mc.submission_key
                  )
                  {mc_productivity_scope_clause}
                GROUP BY {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")}, (mc.submitted_at AT TIME ZONE 'UTC')::date
                """
                ,
                mc_productivity_scope_params,
            )

            cur.execute("DELETE FROM mart.city_performance")
            cur.execute(
                f"""
                WITH case_base AS (
                    SELECT
                        mc.case_id,
                        mc.submission_key,
                        COALESCE(NULLIF(TRIM({city_expr}), ''), 'Unknown') AS city_id,
                        COALESCE(NULLIF(TRIM({city_expr}), ''), 'Unknown') AS city_name,
                        mc.approval_stage,
                        lower(trim(COALESCE(mc.record->>'consent_obtained', ''))) AS consent_value,
                        CASE
                            WHEN COALESCE(mc.record->>'duration', '') ~ '^[0-9]+(\\.[0-9]+)?$'
                                THEN CASE
                                    WHEN (mc.record->>'duration')::numeric > {MAIN_INTERVIEW_MAX_MINUTES}
                                        THEN ((mc.record->>'duration')::numeric / 60.0)
                                    ELSE (mc.record->>'duration')::numeric
                                END
                            ELSE NULL
                        END AS duration_minutes
                    FROM clean.main_case mc
                    WHERE NOT EXISTS (
                        SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key
                    )
                    {mc_scope_clause}
                ),
                issue_counts AS (
                    SELECT
                        cb.city_id,
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issues,
                        COUNT(*)::int AS total_issues
                    FROM qc.issue_queue iq
                    INNER JOIN case_base cb
                      ON COALESCE(NULLIF(TRIM(cb.submission_key), ''), NULLIF(TRIM(cb.case_id), '')) =
                         COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    WHERE iq.instrument_code = 'main'
                    GROUP BY cb.city_id
                ),
                rule_flags AS (
                    SELECT
                        cb.city_id,
                        NULLIF(TRIM(rr.rule_code), '') AS rule_code,
                        COUNT(*)::int AS flag_count
                    FROM qc.issue_queue iq
                    INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                    INNER JOIN case_base cb
                      ON COALESCE(NULLIF(TRIM(cb.submission_key), ''), NULLIF(TRIM(cb.case_id), '')) =
                         COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    WHERE iq.instrument_code = 'main'
                      AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                      AND NULLIF(TRIM(rr.rule_code), '') IS NOT NULL
                    GROUP BY cb.city_id, NULLIF(TRIM(rr.rule_code), '')
                ),
                rule_json AS (
                    SELECT city_id, jsonb_object_agg(lower(rule_code), flag_count) AS rule_counts
                    FROM rule_flags
                    GROUP BY city_id
                ),
                case_stats AS (
                    SELECT
                        city_id,
                        city_name,
                        COUNT(*)::int AS total_cases,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'approved')::int AS approved_count,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'rejected')::int AS rejected_count,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) IN ('submitted', 'pending_review', 'in_review', 'corrected'))::int AS pending_count,
                        COUNT(*) FILTER (WHERE consent_value IN ('true', 'yes', '1'))::int AS consent_obtained,
                        COUNT(*) FILTER (WHERE consent_value NOT IN ('true', 'yes', '1'))::int AS consent_refused,
                        ROUND(COALESCE(AVG(duration_minutes), 0), 2) AS avg_duration_minutes
                    FROM case_base
                    GROUP BY city_id, city_name
                )
                INSERT INTO mart.city_performance (
                    city_id, city_name, total_cases, approved_count,
                    rejected_count, pending_count, consent_obtained, consent_refused,
                    avg_duration_minutes, avg_sections_completed, open_issues,
                    total_issues, rule_counts, updated_at
                )
                SELECT
                    cs.city_id,
                    cs.city_name,
                    cs.total_cases,
                    cs.approved_count,
                    cs.rejected_count,
                    cs.pending_count,
                    cs.consent_obtained,
                    cs.consent_refused,
                    cs.avg_duration_minutes,
                    0,
                    COALESCE(ic.open_issues, 0),
                    COALESCE(ic.total_issues, 0),
                    COALESCE(rj.rule_counts, '{{}}'::jsonb),
                    now()
                FROM case_stats cs
                LEFT JOIN issue_counts ic ON ic.city_id = cs.city_id
                LEFT JOIN rule_json rj ON rj.city_id = cs.city_id
                """
                ,
                mc_scope_params,
            )

            cur.execute("DELETE FROM mart.city_productivity_by_date")
            cur.execute(
                f"""
                INSERT INTO mart.city_productivity_by_date (
                    city_id, date_key, case_count, updated_at
                )
                SELECT
                    COALESCE(NULLIF(TRIM({city_expr}), ''), 'Unknown') AS city_id,
                    (mc.submitted_at AT TIME ZONE 'UTC')::date AS date_key,
                    COUNT(*)::int AS case_count,
                    now()
                FROM clean.main_case mc
                WHERE mc.submitted_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = mc.submission_key
                  )
                  {mc_productivity_scope_clause}
                GROUP BY COALESCE(NULLIF(TRIM({city_expr}), ''), 'Unknown'), (mc.submitted_at AT TIME ZONE 'UTC')::date
                """
                ,
                mc_productivity_scope_params,
            )

            cur.execute("DELETE FROM mart.callback_queue")
            cur.execute(
                """
                INSERT INTO mart.callback_queue (
                    callback_id, case_id, submission_key, case_label, region_label,
                    interviewer_id, assigned_to_user_id, assigned_to_name, outcome_code,
                    created_at, updated_at, search_text, updated_mart_at
                )
                SELECT
                    cb.callback_id,
                    cb.case_id,
                    mq.submission_key,
                    CONCAT(COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Region'), '_Resp._', COALESCE(mq.region_respondent_ordinal, 1)) AS case_label,
                    mq.region_label,
                    mq.interviewer_id,
                    cb.assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS assigned_to_name,
                    COALESCE(NULLIF(TRIM(cb.outcome_code), ''), 'pending') AS outcome_code,
                    cb.created_at,
                    cb.updated_at,
                    CONCAT_WS(' ', mq.search_text, cb.outcome_code, ua.username, ua.full_name),
                    now()
                FROM qc.callback_outcome cb
                INNER JOIN mart.main_case_queue mq ON mq.case_id = cb.case_id
                LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
                """
            )

            cur.execute("DELETE FROM mart.audio_listening_queue")
            cur.execute(
                """
                INSERT INTO mart.audio_listening_queue (
                    audio_id, case_id, submission_key, case_label, region_label,
                    interviewer_id, assigned_to_user_id, assigned_to_name, status,
                    created_at, reviewed_at, search_text, updated_mart_at
                )
                SELECT
                    al.audio_id,
                    al.case_id,
                    mq.submission_key,
                    CONCAT(COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Region'), '_Resp._', COALESCE(mq.region_respondent_ordinal, 1)) AS case_label,
                    mq.region_label,
                    mq.interviewer_id,
                    al.assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS assigned_to_name,
                    COALESCE(NULLIF(TRIM(al.status), ''), 'pending') AS status,
                    al.created_at,
                    al.reviewed_at,
                    CONCAT_WS(' ', mq.search_text, al.status, ua.username, ua.full_name),
                    now()
                FROM clean.audio_listening al
                INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
                LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                """
            )

            cur.execute("DELETE FROM mart.accompaniment_interviewer")
            cur.execute(
                f"""
                WITH media AS (
                    SELECT case_id, COUNT(*)::int AS photo_count
                    FROM clean.main_case_media
                    WHERE variable_name = 'Take_pictures'
                      AND media_type = 'image'
                    GROUP BY case_id
                ),
                check_row AS (
                    SELECT DISTINCT ON (case_id)
                        case_id,
                        status,
                        assigned_to_user_id,
                        created_at
                    FROM qc.main_accompaniment_photo_check
                    ORDER BY case_id, created_at DESC
                ),
                base AS (
                    SELECT
                        COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown') AS state_name,
                        {_main_interviewer_sql("mq.interviewer_id")} AS interviewer_id,
                        COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), '')) AS accompanied_value,
                        COALESCE(media.photo_count, 0)::int AS photo_count,
                        mq.submitted_at,
                        check_row.status,
                        check_row.assigned_to_user_id,
                        check_row.created_at AS check_created_at,
                        COALESCE(
                            CASE WHEN NULLIF(TRIM(mq.start_time), '') ~ '^\\d{4}-\\d{2}-\\d{2}' THEN NULLIF(TRIM(mq.start_time), '')::timestamptz ELSE NULL END,
                            mq.submitted_at
                        ) AS interview_start_at
                    FROM mart.main_case_queue mq
                    INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
                    LEFT JOIN media ON media.case_id = mq.case_id
                    LEFT JOIN check_row ON check_row.case_id = mq.case_id
                ),
                grouped AS (
                    SELECT
                        state_name,
                        interviewer_id,
                        COUNT(*)::int AS total_interviews,
                        COUNT(*) FILTER (WHERE LOWER(COALESCE(NULLIF(TRIM(accompanied_value), ''), '')) IN ('1', '1.0', '2', '2.0', 'yes', 'true'))::int AS accompanied_interviews,
                        SUM(photo_count)::int AS photo_count,
                        MAX(submitted_at) AS latest_submitted_at,
                        MAX(interview_start_at) AS latest_start_at,
                        CASE
                            WHEN COUNT(*) FILTER (WHERE status = 'rejected') > 0 THEN 'rejected'
                            WHEN COUNT(*) FILTER (WHERE status = 'pending') > 0 THEN 'pending'
                            WHEN COUNT(status) = 0 THEN NULL
                            WHEN COUNT(*) FILTER (WHERE status = 'approved') = COUNT(status) THEN 'approved'
                            WHEN COUNT(*) FILTER (WHERE status = 'checked') = COUNT(status) THEN 'checked'
                            ELSE 'pending'
                        END AS check_status,
                        MAX(assigned_to_user_id::text) FILTER (WHERE assigned_to_user_id IS NOT NULL) AS assigned_to_user_id
                    FROM base
                    GROUP BY state_name, interviewer_id
                )
                INSERT INTO mart.accompaniment_interviewer (
                    state_name, interviewer_id, total_interviews, accompanied_interviews,
                    pct_accompanied, photo_count, status, assigned_to, assigned_to_user_id,
                    assigned_to_username, latest_submitted_at, latest_start_at, updated_at
                )
                SELECT
                    grouped.state_name,
                    grouped.interviewer_id,
                    grouped.total_interviews,
                    grouped.accompanied_interviews,
                    ROUND(grouped.accompanied_interviews::numeric / NULLIF(grouped.total_interviews, 0) * 100, 2),
                    grouped.photo_count,
                    grouped.check_status,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')),
                    grouped.assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')),
                    grouped.latest_submitted_at,
                    grouped.latest_start_at,
                    now()
                FROM grouped
                LEFT JOIN app.user_account ua ON ua.user_id::text = grouped.assigned_to_user_id
                """
            )

            cur.execute("DELETE FROM mart.qc_productivity")
            cur.execute(
                """
                WITH task_rows AS (
                    SELECT
                        'audio'::text AS queue,
                        COALESCE(NULLIF(TRIM(ua.username), ''), NULLIF(TRIM(al.assigned_to_user_id), ''), 'Unknown') AS username,
                        COALESCE(NULLIF(TRIM(ua.full_name), ''), '') AS full_name,
                        al.created_at AS assigned_at,
                        al.reviewed_at AS completed_at
                    FROM clean.audio_listening al
                    INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
                    LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                    WHERE COALESCE(NULLIF(TRIM(al.assigned_to_user_id), ''), '') <> ''
                    UNION ALL
                    SELECT
                        'callback'::text AS queue,
                        COALESCE(NULLIF(TRIM(ua.username), ''), 'Unknown') AS username,
                        COALESCE(NULLIF(TRIM(ua.full_name), ''), '') AS full_name,
                        cb.created_at AS assigned_at,
                        cb.completed_at AS completed_at
                    FROM qc.callback_outcome cb
                    INNER JOIN mart.main_case_queue mq ON mq.case_id = cb.case_id
                    LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
                    WHERE cb.assigned_to_user_id IS NOT NULL
                ),
                expanded AS (
                    SELECT * FROM task_rows
                    UNION ALL
                    SELECT 'all'::text AS queue, username, full_name, assigned_at, completed_at
                    FROM task_rows
                )
                INSERT INTO mart.qc_productivity (
                    queue, username, full_name, total_pushed, completed, pending, updated_at
                )
                SELECT
                    queue,
                    username,
                    MAX(full_name) FILTER (WHERE full_name <> '') AS full_name,
                    COUNT(*)::int AS total_pushed,
                    COUNT(*) FILTER (WHERE completed_at IS NOT NULL)::int AS completed,
                    COUNT(*) FILTER (WHERE completed_at IS NULL)::int AS pending,
                    now()
                FROM expanded
                GROUP BY queue, username
                """
            )

            cur.execute("DELETE FROM mart.qc_productivity_by_date")
            cur.execute(
                """
                WITH task_rows AS (
                    SELECT
                        'audio'::text AS queue,
                        COALESCE(NULLIF(TRIM(ua.username), ''), NULLIF(TRIM(al.assigned_to_user_id), ''), 'Unknown') AS username,
                        al.created_at AS assigned_at
                    FROM clean.audio_listening al
                    INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
                    LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                    WHERE COALESCE(NULLIF(TRIM(al.assigned_to_user_id), ''), '') <> ''
                    UNION ALL
                    SELECT
                        'callback'::text AS queue,
                        COALESCE(NULLIF(TRIM(ua.username), ''), 'Unknown') AS username,
                        cb.created_at AS assigned_at
                    FROM qc.callback_outcome cb
                    INNER JOIN mart.main_case_queue mq ON mq.case_id = cb.case_id
                    LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
                    WHERE cb.assigned_to_user_id IS NOT NULL
                ),
                expanded AS (
                    SELECT * FROM task_rows
                    UNION ALL
                    SELECT 'all'::text AS queue, username, assigned_at
                    FROM task_rows
                )
                INSERT INTO mart.qc_productivity_by_date (
                    queue, username, date_key, case_count, updated_at
                )
                SELECT
                    queue,
                    username,
                    (assigned_at AT TIME ZONE 'UTC')::date AS date_key,
                    COUNT(*)::int AS case_count,
                    now()
                FROM expanded
                WHERE assigned_at IS NOT NULL
                GROUP BY queue, username, (assigned_at AT TIME ZONE 'UTC')::date
                """
            )

            cur.execute("SELECT COUNT(*)::int AS c FROM mart.main_case_queue")
            case_queue_count = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.enumerator_performance")
            enumerator_count = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.enumerator_productivity_by_date")
            productivity_rows = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.city_performance")
            city_count = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.city_productivity_by_date")
            city_productivity_rows = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.callback_queue")
            callback_rows = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.audio_listening_queue")
            audio_rows = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.accompaniment_interviewer")
            accompaniment_rows = int((cur.fetchone() or {}).get("c") or 0)
            cur.execute("SELECT COUNT(*)::int AS c FROM mart.qc_productivity")
            qc_productivity_rows = int((cur.fetchone() or {}).get("c") or 0)
        conn.commit()
    _clear_main_case_list_cache()
    return {
        "status": "success",
        "mainCaseQueueRows": case_queue_count,
        "enumeratorRows": enumerator_count,
        "enumeratorProductivityRows": productivity_rows,
        "cityRows": city_count,
        "cityProductivityRows": city_productivity_rows,
        "callbackRows": callback_rows,
        "audioRows": audio_rows,
        "accompanimentRows": accompaniment_rows,
        "qcProductivityRows": qc_productivity_rows,
    }


def _sync_main_case_queue_rows(cur: Any, case_ids: list[str]) -> None:
    """Keep mart.main_case_queue status/assignment flags current after review mutations."""
    normalized_ids = sorted({str(case_id or "").strip() for case_id in case_ids if str(case_id or "").strip()})
    if not normalized_ids:
        return
    try:
        cur.execute(
            """
            WITH callback_assignment AS (
                SELECT DISTINCT ON (cb.case_id)
                    cb.case_id,
                    cb.assigned_to_user_id::text AS callback_assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS callback_assigned_to_name
                FROM qc.callback_outcome cb
                LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
                WHERE cb.case_id = ANY(%s)
                  AND COALESCE(cb.outcome_code, 'pending') = 'pending'
                ORDER BY cb.case_id, cb.updated_at DESC NULLS LAST, cb.created_at DESC NULLS LAST
            ),
            audio_assignment AS (
                SELECT DISTINCT ON (al.case_id)
                    al.case_id,
                    al.assigned_to_user_id::text AS audio_assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS audio_assigned_to_name
                FROM clean.audio_listening al
                LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                WHERE al.case_id = ANY(%s)
                  AND COALESCE(al.status, 'pending') = 'pending'
                ORDER BY al.case_id, al.created_at DESC NULLS LAST
            )
            UPDATE mart.main_case_queue mq
            SET
                approval_stage = mc.approval_stage,
                updated_at = mc.updated_at,
                has_callback_history = COALESCE(mc.is_callback_required, false) OR ca.case_id IS NOT NULL,
                has_audio_history = aa.case_id IS NOT NULL,
                callback_assigned_to_user_id = ca.callback_assigned_to_user_id,
                callback_assigned_to_name = ca.callback_assigned_to_name,
                audio_assigned_to_user_id = aa.audio_assigned_to_user_id,
                audio_assigned_to_name = aa.audio_assigned_to_name,
                updated_mart_at = now()
            FROM clean.main_case mc
            LEFT JOIN callback_assignment ca ON ca.case_id = mc.case_id
            LEFT JOIN audio_assignment aa ON aa.case_id = mc.case_id
            WHERE mq.case_id = mc.case_id
              AND mq.case_id = ANY(%s)
            """,
            (normalized_ids, normalized_ids, normalized_ids),
        )
    except Exception:
        logger.exception("Unable to incrementally sync mart.main_case_queue rows.")

# ---------------------------------------------------------------------------
# Case list
# ---------------------------------------------------------------------------

def list_main_cases(
    settings: Settings,
    user: AuthUser,
    status_filter: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    cities: str | None = None,
    interviewers: str | None = None,
    qc_rule: str | None = None,
    queue: str | None = None,
    assignment: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
) -> dict[str, Any]:
    def normalize_status_filters(raw_status_filter: str | None) -> list[str]:
        mapped = {
            "reviewed_approved": "approved",
            "reviewed_rejected": "rejected",
        }
        return [
            mapped.get(item.strip(), item.strip())
            for item in str(raw_status_filter or "").split(",")
            if item.strip()
        ]

    category_keys = [
        item.strip()
        for item in str(category or "").split(",")
        if item.strip() and item.strip() != "all" and item.strip() in BHT_CATEGORY_PANEL_MAP and item.strip() != "omnibus"
    ]
    category_key = ",".join(category_keys) if category_keys else "all"
    city_filters = [item.strip() for item in str(cities or "").split(",") if item.strip()]
    interviewer_filters = [normalize_main_interviewer_id(item) for item in str(interviewers or "").split(",") if item.strip()]
    qc_rule_filters = [item.strip() for item in str(qc_rule or "").split(",") if item.strip()]
    queue_filters = [item.strip().lower() for item in str(queue or "").split(",") if item.strip()]
    queue_filters = [item for item in queue_filters if item in {"callback", "recontact", "audio"}]
    assignment_filters = [item.strip().lower() for item in str(assignment or "").split(",") if item.strip()]
    assignment_filters = [
        item for item in assignment_filters
        if item in {"assigned", "unassigned", "callback_assigned", "callback_unassigned", "audio_assigned", "audio_unassigned"}
    ]
    status_filters = normalize_status_filters(status_filter)
    normalized_sort = str(sort_by or "submitted_at").strip()
    normalized_sort_dir = "ASC" if str(sort_dir or "").lower() == "asc" else "DESC"
    safe_limit = max(1, min(limit, 100_000))
    safe_offset = max(offset, 0)
    cache_key = (
        category_key,
        ",".join(status_filters),
        search or "",
        date_from or "",
        date_to or "",
        ",".join(city_filters),
        ",".join(interviewer_filters),
        ",".join(qc_rule_filters),
        ",".join(queue_filters),
        ",".join(assignment_filters),
        normalized_sort,
        normalized_sort_dir,
        safe_limit,
        safe_offset,
    )
    cached = MAIN_CASE_LIST_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < MAIN_CASE_LIST_CACHE_TTL_SECONDS:
        return cached[1]

    panel_codes_for_mart = [
        str(BHT_CATEGORY_PANEL_MAP[key]["panelCode"])
        for key in category_keys
        if BHT_CATEGORY_PANEL_MAP[key].get("panelCode")
    ]
    mart_sector_label_expr = _sector_label_sql("mc_src.record")
    if True:
        global MAIN_CASE_QUEUE_OPTIONS_CACHE
        mart_where: list[str] = ["1 = 1"]
        mart_params: list[Any] = []
        active_scope_sql, active_scope_params = main_row_scope_clause(settings, "mq", prefix="AND")
        if active_scope_sql:
            mart_where.append(active_scope_sql.removeprefix("AND ").strip())
            mart_params.extend(active_scope_params)
        if panel_codes_for_mart:
            mart_where.append("selected_panel_codes && %s::text[]")
            mart_params.append(panel_codes_for_mart)
        if status_filters:
            mart_where.append("approval_stage = ANY(%s)")
            mart_params.append(status_filters)
        if city_filters:
            mart_where.append("region_label = ANY(%s)")
            mart_params.append(city_filters)
        if interviewer_filters:
            mart_where.append(f"{_main_interviewer_sql('interviewer_id', 'username')} = ANY(%s)")
            mart_params.append(interviewer_filters)
        if qc_rule_filters:
            mart_where.append(
                """
                EXISTS (
                    SELECT 1
                    FROM qc.issue_queue iq_filter
                    INNER JOIN qc.rule_result rr_filter
                        ON rr_filter.rule_result_id = iq_filter.rule_result_id
                    WHERE iq_filter.instrument_code = 'main'
                      AND COALESCE(NULLIF(TRIM(iq_filter.issue_status), ''), 'pending_review') <> 'resolved'
                      AND rr_filter.rule_code = ANY(%s)
                      AND COALESCE(NULLIF(TRIM(iq_filter.submission_key), ''), NULLIF(TRIM(iq_filter.case_id), '')) =
                          COALESCE(NULLIF(TRIM(mq.submission_key), ''), NULLIF(TRIM(mq.case_id), ''))
                )
                """
            )
            mart_params.append(qc_rule_filters)
        if queue_filters:
            queue_parts: list[str] = []
            if "callback" in queue_filters or "recontact" in queue_filters:
                queue_parts.append("COALESCE(has_callback_history, false)")
            if "audio" in queue_filters:
                queue_parts.append("COALESCE(has_audio_history, false)")
            if queue_parts:
                mart_where.append("(" + " OR ".join(queue_parts) + ")")
        if assignment_filters:
            assignment_parts: list[str] = []
            if "assigned" in assignment_filters:
                assignment_parts.append("(callback_assigned_to_user_id IS NOT NULL OR audio_assigned_to_user_id IS NOT NULL)")
            if "unassigned" in assignment_filters:
                assignment_parts.append("(callback_assigned_to_user_id IS NULL AND audio_assigned_to_user_id IS NULL)")
            if "callback_assigned" in assignment_filters:
                assignment_parts.append("callback_assigned_to_user_id IS NOT NULL")
            if "callback_unassigned" in assignment_filters:
                assignment_parts.append("COALESCE(has_callback_history, false) AND callback_assigned_to_user_id IS NULL")
            if "audio_assigned" in assignment_filters:
                assignment_parts.append("audio_assigned_to_user_id IS NOT NULL")
            if "audio_unassigned" in assignment_filters:
                assignment_parts.append("COALESCE(has_audio_history, false) AND audio_assigned_to_user_id IS NULL")
            if assignment_parts:
                mart_where.append("(" + " OR ".join(assignment_parts) + ")")
        search_terms = list(dict.fromkeys(term.strip() for term in (search or "").splitlines() if term.strip()))
        if search_terms:
            if len(search_terms) == 1:
                term = search_terms[0]
                like = f"%{term}%"
                mart_where.append(
                    """(
                        search_text ILIKE %s
                        OR submission_key ILIKE %s
                        OR case_id ILIKE %s
                        OR CONCAT(REPLACE(region_label, ' ', '_'), '_Resp._', region_respondent_ordinal::text) ILIKE %s
                        OR to_tsvector('simple', COALESCE(search_text, '')) @@ plainto_tsquery('simple', %s)
                    )"""
                )
                mart_params.extend([like, like, like, like, term])
            else:
                # Evaluate the searchable row once against an array of patterns.
                # Expanding the full expression once per pasted term causes very
                # large query plans and timed out for realistic bulk lists.
                uuid_pattern = re.compile(
                    r"^(?:uuid:)?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                    re.IGNORECASE,
                )
                exact_source_terms: set[str] = set()
                exact_id_terms: list[str] = []
                for term in search_terms:
                    uuid_match = uuid_pattern.fullmatch(term)
                    if not uuid_match:
                        continue
                    exact_source_terms.add(term)
                    canonical_uuid = uuid_match.group(1).lower()
                    exact_id_terms.extend([term, canonical_uuid, f"uuid:{canonical_uuid}"])
                exact_id_terms = list(dict.fromkeys(exact_id_terms))
                text_terms = [term for term in search_terms if term not in exact_source_terms]
                bulk_match_parts: list[str] = []
                if exact_id_terms:
                    bulk_match_parts.extend(["submission_key = ANY(%s)", "case_id = ANY(%s)"])
                    mart_params.extend([exact_id_terms, exact_id_terms])
                if text_terms:
                    like_patterns = [f"%{term}%" for term in text_terms]
                    bulk_match_parts.extend([
                        "search_text ILIKE ANY(%s)",
                        "submission_key ILIKE ANY(%s)",
                        "case_id ILIKE ANY(%s)",
                        "CONCAT(REPLACE(region_label, ' ', '_'), '_Resp._', region_respondent_ordinal::text) ILIKE ANY(%s)",
                    ])
                    mart_params.extend([like_patterns, like_patterns, like_patterns, like_patterns])
                mart_where.append(
                    "(" + " OR ".join(bulk_match_parts) + ")"
                )
        mart_start_filter_expr = main_row_effective_datetime_sql("", start_column="start_time")
        if date_from:
            mart_where.append(
                f"{mart_start_filter_expr} >= %s::date"
            )
            mart_params.append(date_from)
        if date_to:
            mart_where.append(
                f"{mart_start_filter_expr} < (%s::date + interval '1 day')"
            )
            mart_params.append(date_to)
        mart_where_sql = " AND ".join(mart_where)
        sort_expr_map = {
            "region_sort": "region_label",
            "status_sort": "approval_stage",
            "qc_load": "GREATEST(COALESCE(open_issue_count, 0), COALESCE(qc_flag_count, 0))",
            "submitted_at": "submitted_at",
            "submission_key": "submission_key",
            "ea_name": "ea_name",
            "state_name": "state_name",
            "approved_by": "approved_by",
            "approval_stage": "approval_stage",
            "section_count": "section_count",
            "supacc_confirm": "supacc_confirm",
            "slot_type": "slot_type",
            "username": "username",
            "final_outcome_code": "final_outcome_code",
        }
        sort_expr = sort_expr_map.get(normalized_sort, "submitted_at")
        nulls = "NULLS FIRST" if normalized_sort_dir == "ASC" else "NULLS LAST"
        mart_order_sql = f"{sort_expr} {normalized_sort_dir} {nulls}, submitted_at DESC NULLS LAST, case_id DESC"

        try:
            with db_connection(settings) as conn:
                with conn.cursor() as cur:
                    cached_options = MAIN_CASE_QUEUE_OPTIONS_CACHE
                    if cached_options and (time.monotonic() - cached_options[0]) < MAIN_CASE_QUEUE_OPTIONS_CACHE_TTL_SECONDS:
                        option_payload = cached_options[1]
                    else:
                        options_where = ["1 = 1"]
                        options_params: list[Any] = []
                        options_scope_sql, options_scope_params = main_row_scope_clause(settings, "mq", prefix="AND")
                        if options_scope_sql:
                            options_where.append(options_scope_sql.removeprefix("AND ").strip())
                            options_params.extend(options_scope_params)
                        options_where_sql = " AND ".join(options_where)
                        cur.execute(
                            f"""
                            SELECT
                                ARRAY_REMOVE(ARRAY_AGG(DISTINCT region_label ORDER BY region_label), NULL) AS cities,
                                ARRAY_REMOVE(ARRAY_AGG(DISTINCT {_main_interviewer_sql('interviewer_id', 'username')} ORDER BY {_main_interviewer_sql('interviewer_id', 'username')}), NULL) AS interviewers
                            FROM mart.main_case_queue mq
                            WHERE {options_where_sql}
                            """,
                            tuple(options_params),
                        )
                        option_row = cur.fetchone() or {}
                        option_payload = {
                            "cities": option_row.get("cities") or [],
                            "interviewers": option_row.get("interviewers") or [],
                        }
                        MAIN_CASE_QUEUE_OPTIONS_CACHE = (time.monotonic(), option_payload)

                    cur.execute(
                        f"""
                        SELECT
                            COUNT(*)::int AS total,
                            COALESCE(SUM(GREATEST(COALESCE(open_issue_count, 0), COALESCE(qc_flag_count, 0))), 0)::int AS total_open_issues
                        FROM mart.main_case_queue mq
                        WHERE {mart_where_sql}
                        """,
                        mart_params,
                    )
                    total_row = cur.fetchone() or {}
                    total = int(total_row.get("total") or 0)
                    total_open_issues = int(total_row.get("total_open_issues") or 0)
                    cur.execute(
                        f"""
                        SELECT
                            mq.submission_key, mq.case_id, mq.ea_id, mq.interviewer_id, mq.supervisor_id,
                            mq.approval_stage, mq.submitted_at, mq.start_time, mq.updated_at, mq.ea_name,
                            mq.lga_name, mq.state_name, mq.region_label, mq.region_respondent_ordinal,
                            {mart_sector_label_expr} AS sector_label,
                            mc_src.gps_lat,
                            mc_src.gps_long,
                            mq.supacc_confirm, mq.slot_type, mq.username, mq.approved_by,
                            mq.is_auto_approved, mq.final_outcome_code, mq.section_count,
                            mq.open_issue_count, mq.qc_flag_count, mq.pending_change_count,
                            mq.has_callback_history, mq.has_audio_history,
                            mq.callback_assigned_to_user_id, mq.callback_assigned_to_name,
                            mq.audio_assigned_to_user_id, mq.audio_assigned_to_name,
                            mq.selected_panel_labels,
                            (
                                SELECT STRING_AGG(DISTINCT rr.rule_code, ', ' ORDER BY rr.rule_code)
                                FROM qc.issue_queue iq
                                INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                                WHERE iq.instrument_code = 'main'
                                  AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                                  AND COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) =
                                      COALESCE(NULLIF(TRIM(mq.submission_key), ''), NULLIF(TRIM(mq.case_id), ''))
                            ) AS auto_flagged_qc_issue_codes,
                            (
                                SELECT STRING_AGG(DISTINCT COALESCE(NULLIF(TRIM(iq.issue_summary), ''), NULLIF(TRIM(rr.result_message), ''), rr.rule_code), ' | ' ORDER BY COALESCE(NULLIF(TRIM(iq.issue_summary), ''), NULLIF(TRIM(rr.result_message), ''), rr.rule_code))
                                FROM qc.issue_queue iq
                                INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                                WHERE iq.instrument_code = 'main'
                                  AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                                  AND COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) =
                                      COALESCE(NULLIF(TRIM(mq.submission_key), ''), NULLIF(TRIM(mq.case_id), ''))
                            ) AS auto_flagged_qc_issues
                        FROM (
                            SELECT *
                            FROM mart.main_case_queue mq
                            WHERE {mart_where_sql}
                            ORDER BY {mart_order_sql}
                            LIMIT %s OFFSET %s
                        ) mq
                        LEFT JOIN clean.main_case mc_src ON mc_src.case_id = mq.case_id
                        """,
                        [*mart_params, safe_limit, safe_offset],
                    )
                    items = cur.fetchall()
                    payload = {
                        "items": items,
                        "total": total,
                        "limit": safe_limit,
                        "offset": safe_offset,
                        "has_more": safe_offset + len(items) < total,
                        "totalOpenIssues": total_open_issues,
                        "filterOptions": option_payload,
                    }
                    MAIN_CASE_LIST_CACHE[cache_key] = (time.monotonic(), payload)
                    return payload
        except Exception as exc:
            logger.exception("Main Data Explorer active mart read failed; unscoped fallback is disabled.")
            raise HTTPException(
                status_code=503,
                detail=f"Main Data Explorer active mart is unavailable; refusing unscoped fallback. {type(exc).__name__}: {exc}",
            ) from exc

    params: list[Any] = []
    where_parts: list[str] = [
        "NOT EXISTS (SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key)"
    ]
    panel_codes = [
        str(BHT_CATEGORY_PANEL_MAP[key]["panelCode"])
        for key in category_keys
        if BHT_CATEGORY_PANEL_MAP[key].get("panelCode")
    ]
    city_filter_expr = _city_label_sql("mc.record", str(settings.root_dir))
    case_city_expr = f"""
        COALESCE(
            NULLIF(TRIM({city_filter_expr}), ''),
            NULLIF(TRIM(mcd.state_name), ''),
            NULLIF(TRIM(mc.record->>'state_name'), ''),
            NULLIF(TRIM(mc.record->>'lga_name'), ''),
            'Region'
        )
    """
    case_interviewer_expr = _main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")

    if panel_codes:
        where_parts.append(
            """
            EXISTS (
                SELECT 1
                FROM clean.main_case_panel mcp
                WHERE mcp.case_id = mc.case_id
                  AND mcp.panel_code = ANY(%s)
                  AND COALESCE(mcp.is_selected, TRUE)
            )
            """
        )
        params.append(panel_codes)

    if status_filters:
        where_parts.append("mc.approval_stage = ANY(%s)")
        params.append(status_filters)

    if city_filters:
        where_parts.append(f"{case_city_expr} = ANY(%s)")
        params.append(city_filters)

    if interviewer_filters:
        where_parts.append(f"{case_interviewer_expr} = ANY(%s)")
        params.append(interviewer_filters)

    if qc_rule_filters:
        where_parts.append(
            """
            EXISTS (
                SELECT 1
                FROM qc.issue_queue iq_filter
                INNER JOIN qc.rule_result rr_filter
                    ON rr_filter.rule_result_id = iq_filter.rule_result_id
                WHERE iq_filter.instrument_code = 'main'
                  AND COALESCE(NULLIF(TRIM(iq_filter.issue_status), ''), 'pending_review') <> 'resolved'
                  AND rr_filter.rule_code = ANY(%s)
                  AND COALESCE(NULLIF(TRIM(iq_filter.submission_key), ''), NULLIF(TRIM(iq_filter.case_id), '')) =
                      COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), ''))
            )
            """
        )
        params.append(qc_rule_filters)

    search_terms = list(dict.fromkeys(term.strip() for term in (search or "").splitlines() if term.strip()))
    if search_terms:
        search_parts: list[str] = []
        for term in search_terms:
            search_parts.append(
                f"""
            (
                mc.submission_key ILIKE %s
                OR
                mc.case_id ILIKE %s
                OR mc.ea_id ILIKE %s
                OR mc.record->>'ea_name' ILIKE %s
                OR mc.record->>'lga_name' ILIKE %s
                OR COALESCE(NULLIF(TRIM(mcd.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), '')) ILIKE %s
                OR {case_city_expr} ILIKE %s
                OR COALESCE(NULLIF(TRIM(mc.approval_stage), ''), '') ILIKE %s
                OR COALESCE(NULLIF(TRIM(mc.record->>'slot_type'), ''), '') ILIKE %s
                OR COALESCE(NULLIF(TRIM(mc.record->>'final_outcome_code'), ''), '') ILIKE %s
                OR {_main_interviewer_sql("mc.record->>'username'", "mc.interviewer_id")} ILIKE %s
                OR {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} ILIKE %s
                OR COALESCE(NULLIF(TRIM(mc.supervisor_id), ''), '') ILIKE %s
                OR COALESCE(TO_CHAR(mc.submitted_at, 'YYYY-MM-DD HH24:MI:SS'), '') ILIKE %s
                OR (
                    CASE
                        WHEN LOWER(COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), ''), '')) IN ('yes', '1', '1.0', '2', '2.0', 'true') THEN 'Yes'
                        ELSE 'No'
                    END
                ) ILIKE %s
                OR mc.case_id IN (
                    SELECT mcs.case_id
                    FROM clean.main_case_section mcs
                    GROUP BY mcs.case_id
                    HAVING CAST(COUNT(DISTINCT mcs.section_name)::int AS text) ILIKE %s
                )
                OR EXISTS (
                    SELECT 1
                    FROM clean.main_case_panel search_panel
                    WHERE search_panel.case_id = mc.case_id
                      AND COALESCE(search_panel.is_selected, TRUE)
                      AND {_panel_label_sql("search_panel.panel_code")} ILIKE %s
                )
                OR CONCAT(
                    REPLACE({case_city_expr}, ' ', '_'),
                    '_Resp._',
                    (
                        SELECT COUNT(*)::int
                        FROM clean.main_case ordinal_mc
                        LEFT JOIN mart.main_case_dim ordinal_mcd ON ordinal_mcd.case_id = ordinal_mc.case_id
                        WHERE COALESCE(
                            NULLIF(TRIM({_city_label_sql("ordinal_mc.record", str(settings.root_dir))}), ''),
                            NULLIF(TRIM(ordinal_mcd.state_name), ''),
                            NULLIF(TRIM(ordinal_mc.record->>'state_name'), ''),
                            NULLIF(TRIM(ordinal_mc.record->>'lga_name'), ''),
                            'Region'
                        ) = {case_city_expr}
                          AND (
                            COALESCE(ordinal_mc.submitted_at, 'infinity'::timestamptz),
                            COALESCE(ordinal_mc.created_at, 'infinity'::timestamptz),
                            ordinal_mc.case_id
                          ) <= (
                            COALESCE(mc.submitted_at, 'infinity'::timestamptz),
                            COALESCE(mc.created_at, 'infinity'::timestamptz),
                            mc.case_id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = ordinal_mc.submission_key
                          )
                    )::text
                ) ILIKE %s
                OR mc.submission_key IN (
                    SELECT COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    FROM qc.issue_queue iq
                    WHERE iq.instrument_code = 'main'
                    GROUP BY COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    HAVING CAST(
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int
                    AS text) ILIKE %s
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc.case_status_history h
                    INNER JOIN app.user_account u ON u.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'main'
                      AND h.submission_key = mc.submission_key
                      AND h.new_status = mc.approval_stage
                      AND (
                        CONCAT(COALESCE(NULLIF(TRIM(u.username), ''), ''), ': ', COALESCE(NULLIF(TRIM(u.full_name), ''), '')) ILIKE %s
                        OR COALESCE(NULLIF(TRIM(u.username), ''), '') ILIKE %s
                        OR COALESCE(NULLIF(TRIM(u.full_name), ''), '') ILIKE %s
                      )
                )
            )
            """
            )
            like = f"%{term}%"
            params.extend([like] * 22)
        where_parts.append("(" + " OR ".join(search_parts) + ")")

    main_case_start_expr = main_case_effective_datetime_sql("mc")
    if date_from:
        where_parts.append(f"{main_case_start_expr} >= %s::date")
        params.append(date_from)

    if date_to:
        where_parts.append(f"{main_case_start_expr} < (%s::date + interval '1 day')")
        params.append(date_to)
    if queue_filters:
        queue_exists: list[str] = []
        if "callback" in queue_filters or "recontact" in queue_filters:
            queue_exists.append(
                """
                EXISTS (
                    SELECT 1
                    FROM qc.callback_outcome cbq
                    WHERE cbq.case_id = mc.case_id
                      AND COALESCE(cbq.outcome_code, 'pending') = 'pending'
                )
                OR COALESCE(mc.is_callback_required, false) = true
                """
            )
        if "audio" in queue_filters:
            queue_exists.append(
                """
                EXISTS (
                    SELECT 1
                    FROM clean.audio_listening alq
                    WHERE alq.case_id = mc.case_id
                      AND COALESCE(alq.status, 'pending') = 'pending'
                )
                """
            )
        if queue_exists:
            where_parts.append("(" + " OR ".join(queue_exists) + ")")

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    count_query = f"""
        SELECT COUNT(*)::int AS total
        FROM clean.main_case mc
        LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
        {where_sql}
    """

    panel_label_expr = _panel_label_sql("mcp.panel_code")
    city_label_expr = _city_label_sql("mc_all.record", str(settings.root_dir))
    sector_label_expr = _sector_label_sql("mc.record")
    filter_options_query = f"""
        SELECT
            ARRAY_REMOVE(ARRAY_AGG(DISTINCT city_name ORDER BY city_name), NULL) AS cities,
            ARRAY_REMOVE(ARRAY_AGG(DISTINCT interviewer_name ORDER BY interviewer_name), NULL) AS interviewers
        FROM (
            SELECT
                {case_city_expr} AS city_name,
                {case_interviewer_expr} AS interviewer_name
            FROM clean.main_case mc
            LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
            WHERE NOT EXISTS (
                SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mc.submission_key
            )
        ) options
    """
    query = f"""
        WITH filtered_cases AS (
            SELECT
                mc.submission_key,
                mc.case_id,
                mc.ea_id,
                mc.interviewer_id,
                mc.supervisor_id,
                mc.approval_stage,
                mc.submitted_at,
                COALESCE(
                    NULLIF(TRIM(mc.record->>'starttime'), ''),
                    NULLIF(TRIM(mc.record->>'start_time'), ''),
                    NULLIF(TRIM(mc.record->>'StartTime'), ''),
                    NULLIF(TRIM(mc.record->>'start'), '')
                ) AS start_time,
                mc.updated_at,
                mc.record->>'ea_name' AS ea_name,
                mc.record->>'lga_name' AS lga_name,
                COALESCE(NULLIF(TRIM(mcd.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), '')) AS state_name,
                {sector_label_expr} AS sector_label,
                mc.gps_lat,
                mc.gps_long,
                COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), '')) AS supacc_confirm,
                mc.record->>'slot_type' AS slot_type,
                {_main_interviewer_sql("mc.record->>'username'", "mc.interviewer_id")} AS username,
                mc.record->>'final_outcome_code' AS final_outcome_code
            FROM clean.main_case mc
            LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
            {where_sql}
            ORDER BY mc.submitted_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        ),
        all_case_ordinals AS (
            SELECT
                ranked.case_id,
                ranked.region_label,
                ranked.region_respondent_ordinal
            FROM (
                SELECT
                    mc_all.case_id,
                    COALESCE(
                        NULLIF(TRIM({city_label_expr}), ''),
                        NULLIF(TRIM(mcd_all.state_name), ''),
                        NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                        NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                        'Region'
                    ) AS region_label,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            NULLIF(TRIM({city_label_expr}), ''),
                            NULLIF(TRIM(mcd_all.state_name), ''),
                            NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                            NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                            'Region'
                        )
                        ORDER BY mc_all.submitted_at ASC NULLS LAST, mc_all.created_at ASC NULLS LAST, mc_all.case_id ASC
                    )::int AS region_respondent_ordinal
                FROM clean.main_case mc_all
                LEFT JOIN mart.main_case_dim mcd_all ON mcd_all.case_id = mc_all.case_id
            ) ranked
        ),
        section_counts AS (
            SELECT mcs.case_id, COUNT(DISTINCT mcs.section_name)::int AS section_count
            FROM clean.main_case_section mcs
            INNER JOIN filtered_cases fc ON fc.case_id = mcs.case_id
            GROUP BY mcs.case_id
        ),
        issue_counts AS (
            SELECT
                COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) AS join_key,
                COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issue_count
            FROM qc.issue_queue iq
            INNER JOIN filtered_cases fc
                ON COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) = COALESCE(NULLIF(TRIM(fc.submission_key), ''), NULLIF(TRIM(fc.case_id), ''))
            WHERE iq.instrument_code = 'main'
            GROUP BY COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
        ),
        pending_counts AS (
            SELECT
                COALESCE(NULLIF(TRIM(pc.submission_key), ''), NULLIF(TRIM(pc.case_id), '')) AS join_key,
                COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(pc.change_status), ''), 'pending') = 'pending')::int AS pending_change_count
            FROM qc.pending_change pc
            INNER JOIN filtered_cases fc
                ON COALESCE(NULLIF(TRIM(pc.submission_key), ''), NULLIF(TRIM(pc.case_id), '')) = COALESCE(NULLIF(TRIM(fc.submission_key), ''), NULLIF(TRIM(fc.case_id), ''))
            WHERE pc.instrument_code = 'main'
            GROUP BY COALESCE(NULLIF(TRIM(pc.submission_key), ''), NULLIF(TRIM(pc.case_id), ''))
        ),
        latest_status_actors AS (
            SELECT DISTINCT ON (h.submission_key, h.new_status)
                h.submission_key,
                h.new_status,
                CASE
                    WHEN NULLIF(TRIM(u.username), '') IS NOT NULL AND NULLIF(TRIM(u.full_name), '') IS NOT NULL THEN CONCAT(u.username, ': ', u.full_name)
                    WHEN NULLIF(TRIM(u.username), '') IS NOT NULL THEN u.username
                    WHEN NULLIF(TRIM(u.full_name), '') IS NOT NULL THEN u.full_name
                    ELSE NULL
                END AS approved_by,
                COALESCE(h.change_note, '') ILIKE 'Automatically approved for export:%%' AS is_auto_approved
            FROM qc.case_status_history h
            INNER JOIN filtered_cases fc
                ON fc.submission_key = h.submission_key
            LEFT JOIN app.user_account u
                ON u.user_id = h.changed_by_user_id
            WHERE h.instrument_code = 'main'
            ORDER BY h.submission_key, h.new_status, h.changed_at DESC
        ),
        callback_flags AS (
            SELECT fc.case_id, true AS has_callback_history
            FROM filtered_cases fc
            WHERE EXISTS (
                SELECT 1
                FROM qc.callback_outcome cb
                WHERE cb.case_id = fc.case_id
                  AND COALESCE(cb.outcome_code, 'pending') = 'pending'
            )
               OR EXISTS (
                SELECT 1
                FROM clean.main_case mc
                WHERE mc.case_id = fc.case_id
                  AND COALESCE(mc.is_callback_required, false) = true
            )
        ),
        audio_flags AS (
            SELECT fc.case_id, true AS has_audio_history
            FROM filtered_cases fc
            WHERE EXISTS (
                SELECT 1
                FROM clean.audio_listening al
                WHERE al.case_id = fc.case_id
                  AND COALESCE(al.status, 'pending') = 'pending'
            )
        ),
        latest_callback_assignment AS (
            SELECT DISTINCT ON (cb.case_id)
                cb.case_id,
                cb.assigned_to_user_id::text AS callback_assigned_to_user_id,
                CASE
                    WHEN NULLIF(TRIM(ua.full_name), '') IS NOT NULL THEN ua.full_name
                    WHEN NULLIF(TRIM(ua.username), '') IS NOT NULL THEN ua.username
                    ELSE NULL
                END AS callback_assigned_to_name
            FROM qc.callback_outcome cb
            INNER JOIN filtered_cases fc ON fc.case_id = cb.case_id
            LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
            WHERE COALESCE(cb.outcome_code, 'pending') = 'pending'
            ORDER BY cb.case_id, cb.updated_at DESC NULLS LAST, cb.created_at DESC NULLS LAST
        ),
        latest_audio_assignment AS (
            SELECT DISTINCT ON (al.case_id)
                al.case_id,
                al.assigned_to_user_id::text AS audio_assigned_to_user_id,
                CASE
                    WHEN NULLIF(TRIM(ua.full_name), '') IS NOT NULL THEN ua.full_name
                    WHEN NULLIF(TRIM(ua.username), '') IS NOT NULL THEN ua.username
                    ELSE NULL
                END AS audio_assigned_to_name
            FROM clean.audio_listening al
            INNER JOIN filtered_cases fc ON fc.case_id = al.case_id
            LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
            WHERE COALESCE(al.status, 'pending') = 'pending'
            ORDER BY al.case_id, al.created_at DESC NULLS LAST
        ),
        selected_panels AS (
            SELECT
                fc.case_id,
                STRING_AGG(
                    DISTINCT {panel_label_expr},
                    ', '
                ) AS selected_panel_labels
            FROM filtered_cases fc
            INNER JOIN clean.main_case_panel mcp
                ON mcp.case_id = fc.case_id
               AND COALESCE(mcp.is_selected, TRUE)
            GROUP BY fc.case_id
        )
        SELECT
            fc.submission_key,
            fc.case_id,
            fc.ea_id,
            fc.interviewer_id,
            fc.supervisor_id,
            fc.approval_stage,
            fc.submitted_at,
            fc.start_time,
            fc.updated_at,
            fc.ea_name,
            fc.lga_name,
            fc.state_name,
            aco.region_label,
            COALESCE(aco.region_respondent_ordinal, 1)::int AS region_respondent_ordinal,
            fc.sector_label,
            fc.gps_lat,
            fc.gps_long,
            fc.supacc_confirm,
            fc.slot_type,
            fc.username,
            lsa.approved_by,
            COALESCE(lsa.is_auto_approved, false) AS is_auto_approved,
            fc.final_outcome_code,
            COALESCE(sc.section_count, 0)::int AS section_count,
            COALESCE(ic.open_issue_count, 0)::int AS open_issue_count,
            COALESCE(ic.open_issue_count, 0)::int AS qc_flag_count,
            COALESCE(pc.pending_change_count, 0)::int AS pending_change_count,
            COALESCE(cb.has_callback_history, false) AS has_callback_history,
            COALESCE(af.has_audio_history, false) AS has_audio_history,
            lca.callback_assigned_to_user_id,
            lca.callback_assigned_to_name,
            laa.audio_assigned_to_user_id,
            laa.audio_assigned_to_name,
            COALESCE(sp.selected_panel_labels, 'Omnibus') AS selected_panel_labels
        FROM filtered_cases fc
        LEFT JOIN section_counts sc ON sc.case_id = fc.case_id
        LEFT JOIN issue_counts ic ON ic.join_key = COALESCE(NULLIF(TRIM(fc.submission_key), ''), NULLIF(TRIM(fc.case_id), ''))
        LEFT JOIN pending_counts pc ON pc.join_key = COALESCE(NULLIF(TRIM(fc.submission_key), ''), NULLIF(TRIM(fc.case_id), ''))
        LEFT JOIN latest_status_actors lsa ON lsa.submission_key = fc.submission_key AND lsa.new_status = fc.approval_stage
        LEFT JOIN callback_flags cb ON cb.case_id = fc.case_id
        LEFT JOIN audio_flags af ON af.case_id = fc.case_id
        LEFT JOIN latest_callback_assignment lca ON lca.case_id = fc.case_id
        LEFT JOIN latest_audio_assignment laa ON laa.case_id = fc.case_id
        LEFT JOIN selected_panels sp ON sp.case_id = fc.case_id
        LEFT JOIN all_case_ordinals aco ON aco.case_id = fc.case_id
        ORDER BY fc.submitted_at DESC NULLS LAST
    """

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # Do not run schema DDL from this read endpoint. Startup/bootstrap owns
            # MAIN_CASE_LIST_SUPPORT_STATEMENTS; running CREATE/ALTER here can
            # deadlock under concurrent dashboard traffic because ALTER TABLE
            # takes AccessExclusiveLock.
            cur.execute(filter_options_query)
            option_row = cur.fetchone() or {}
            cur.execute(count_query, params)
            total = int((cur.fetchone() or {}).get("total", 0))
            cur.execute(query, [*params, safe_limit, safe_offset])
            items = cur.fetchall()
            total_open_issues = sum(
                max(int(row.get("open_issue_count") or 0), int(row.get("qc_flag_count") or 0))
                for row in items
            )
            payload = {
                "items": items,
                "total": total,
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": safe_offset + len(items) < total,
                "totalOpenIssues": total_open_issues,
                "filterOptions": {
                    "cities": option_row.get("cities") or [],
                    "interviewers": option_row.get("interviewers") or [],
                },
            }
            MAIN_CASE_LIST_CACHE[cache_key] = (time.monotonic(), payload)
            return payload



# ---------------------------------------------------------------------------
# Case detail
# ---------------------------------------------------------------------------

def _get_main_case_navigation_cache(settings: Settings) -> dict[str, dict[str, Any]]:
    global MAIN_CASE_NAV_CACHE
    if MAIN_CASE_NAV_CACHE and (time.monotonic() - MAIN_CASE_NAV_CACHE[0]) < MAIN_CASE_NAV_CACHE_TTL_SECONDS:
        return MAIN_CASE_NAV_CACHE[1]

    query = f"""
        WITH base_cases AS (
            SELECT
                mc.submission_key,
                mc.case_id,
                mc.submitted_at,
                mc.created_at,
                COALESCE(
                    NULLIF(NULLIF(TRIM(mcd.state_name), ''), 'Unknown'),
                    NULLIF(NULLIF(TRIM(mc.record->>'state_name'), ''), 'Unknown'),
                    NULLIF(TRIM(mc.record->>'City_1'), ''),
                    NULLIF(TRIM(mc.record->>'lga_name'), ''),
                    'Region'
                ) AS region_label
            FROM clean.main_case mc
            LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM clean.deleted_main_cases dmc
                WHERE dmc.submission_key = mc.submission_key
            )
        ),
        ranked AS (
            SELECT
                bc.submission_key,
                bc.region_label,
                ROW_NUMBER() OVER (
                    PARTITION BY bc.region_label
                    ORDER BY bc.submitted_at ASC NULLS LAST, bc.created_at ASC NULLS LAST, bc.case_id ASC
                )::int AS region_respondent_ordinal,
                ROW_NUMBER() OVER (
                    ORDER BY bc.submitted_at DESC NULLS LAST, bc.created_at DESC NULLS LAST, bc.case_id DESC
                )::int AS overall_case_ordinal,
                COUNT(*) OVER ()::int AS overall_case_count,
                LAG(bc.submission_key) OVER (
                    ORDER BY bc.submitted_at DESC NULLS LAST, bc.created_at DESC NULLS LAST, bc.case_id DESC
                ) AS previous_submission_key,
                LEAD(bc.submission_key) OVER (
                    ORDER BY bc.submitted_at DESC NULLS LAST, bc.created_at DESC NULLS LAST, bc.case_id DESC
                ) AS next_submission_key
            FROM base_cases bc
        )
        SELECT *
        FROM ranked
    """
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            city_labels = _city_choice_label_map(str(settings.root_dir))
            nav = {}
            for row in cur.fetchall():
                if not row.get("submission_key"):
                    continue
                item = dict(row)
                region_label = str(item.get("region_label") or "").strip()
                item["region_label"] = city_labels.get(region_label, region_label) or "Region"
                nav[str(row["submission_key"])] = item
    MAIN_CASE_NAV_CACHE = (time.monotonic(), nav)
    return nav


def get_main_case_navigation(settings: Settings, submission_key: str) -> dict[str, Any]:
    scoped_queue_sql, scoped_queue_params = main_row_scope_clause(settings, "mq", prefix="AND")
    queue_query = f"""
        WITH ranked AS (
            SELECT
                mq.submission_key,
                mq.region_label,
                mq.region_respondent_ordinal,
                ROW_NUMBER() OVER (
                    ORDER BY mq.submitted_at DESC NULLS LAST, mq.updated_at DESC NULLS LAST, mq.case_id DESC
                )::int AS overall_case_ordinal,
                COUNT(*) OVER ()::int AS overall_case_count,
                LAG(mq.submission_key) OVER (
                    ORDER BY mq.submitted_at DESC NULLS LAST, mq.updated_at DESC NULLS LAST, mq.case_id DESC
                ) AS previous_submission_key,
                LEAD(mq.submission_key) OVER (
                    ORDER BY mq.submitted_at DESC NULLS LAST, mq.updated_at DESC NULLS LAST, mq.case_id DESC
                ) AS next_submission_key
            FROM mart.main_case_queue mq
            WHERE NOT EXISTS (
                SELECT 1 FROM clean.deleted_main_cases dmc WHERE dmc.submission_key = mq.submission_key
            )
            {scoped_queue_sql}
        )
        SELECT *
        FROM ranked
        WHERE submission_key = %s
        LIMIT 1
    """
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(queue_query, (*scoped_queue_params, submission_key))
                queue_row = cur.fetchone()
            except Exception as exc:
                logger.exception("Main case navigation active mart read failed; unscoped fallback is disabled.")
                raise HTTPException(
                    status_code=503,
                    detail=f"Main case navigation active mart is unavailable; refusing unscoped fallback. {type(exc).__name__}: {exc}",
                ) from exc
    if queue_row:
        item = dict(queue_row)
        item["region_label"] = str(item.get("region_label") or "").strip() or "Region"
        return item
    return {}


def _get_case_activity_timeline(cur: Any, *, case_id: str, submission_key: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    cur.execute(
        """
        SELECT h.previous_status, h.new_status, h.change_note, h.changed_at,
               h.changed_by_user_id::text AS actor_user_id,
               COALESCE(NULLIF(TRIM(u.full_name), ''), NULLIF(TRIM(u.username), '')) AS actor_name
        FROM qc.case_status_history h
        LEFT JOIN app.user_account u ON u.user_id = h.changed_by_user_id
        WHERE h.instrument_code = 'main' AND h.submission_key = %s
        ORDER BY h.changed_at DESC NULLS LAST
        LIMIT 50
        """,
        (submission_key,),
    )
    for row in cur.fetchall():
        events.append(
            {
                "event_type": "case_status",
                "title": "Case status changed",
                "queue": "Main QC",
                "status": row.get("new_status") or "status_update",
                "event_time": row.get("changed_at"),
                "actor_user_id": row.get("actor_user_id"),
                "actor_name": row.get("actor_name"),
                "assignee_user_id": None,
                "assignee_name": None,
                "note": row.get("change_note"),
                "metadata": {"previousStatus": row.get("previous_status"), "newStatus": row.get("new_status")},
            }
        )

    cur.execute(
        """
        SELECT co.callback_id::text, co.attempt_no,
               COALESCE(NULLIF(TRIM(co.outcome_code), ''), 'pending') AS outcome_code,
               co.outcome_note, co.sampled_flag,
               co.assigned_to_user_id::text AS assignee_user_id,
               COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS assignee_name,
               co.created_at, co.completed_at
        FROM qc.callback_outcome co
        LEFT JOIN app.user_account ua ON ua.user_id = co.assigned_to_user_id
        WHERE co.case_id = %s
        ORDER BY co.created_at DESC NULLS LAST, co.attempt_no DESC
        LIMIT 50
        """,
        (case_id,),
    )
    for row in cur.fetchall():
        status = str(row.get("outcome_code") or "pending")
        base = {
            "queue": "Respondent Recontact",
            "status": status,
            "actor_user_id": None,
            "actor_name": None,
            "assignee_user_id": row.get("assignee_user_id"),
            "assignee_name": row.get("assignee_name"),
            "note": row.get("outcome_note"),
            "metadata": {
                "attemptNo": row.get("attempt_no"),
                "sampledFlag": row.get("sampled_flag"),
                "callbackId": row.get("callback_id"),
            },
        }
        events.append({**base, "event_type": "callback_pushed", "title": "Pushed to Respondent Recontact", "event_time": row.get("created_at")})
        if row.get("completed_at") and status != "pending":
            events.append(
                {
                    **base,
                    "event_type": "callback_completed",
                    "title": "Respondent Recontact outcome recorded",
                    "event_time": row.get("completed_at"),
                    "actor_user_id": row.get("assignee_user_id"),
                    "actor_name": row.get("assignee_name"),
                }
            )

    cur.execute(
        """
        SELECT al.audio_id::text,
               COALESCE(NULLIF(TRIM(al.status), ''), 'pending') AS status,
               al.quality_rating, al.reviewer_note,
               al.assigned_to_user_id AS assignee_user_id,
               COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), '')) AS assignee_name,
               al.created_at, al.reviewed_at
        FROM clean.audio_listening al
        LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
        WHERE al.case_id = %s
        ORDER BY al.created_at DESC NULLS LAST
        LIMIT 50
        """,
        (case_id,),
    )
    for row in cur.fetchall():
        status = str(row.get("status") or "pending")
        base = {
            "queue": "Silent Listening",
            "status": status,
            "actor_user_id": None,
            "actor_name": None,
            "assignee_user_id": row.get("assignee_user_id"),
            "assignee_name": row.get("assignee_name"),
            "note": row.get("reviewer_note"),
            "metadata": {"audioId": row.get("audio_id"), "qualityRating": row.get("quality_rating")},
        }
        events.append({**base, "event_type": "audio_pushed", "title": "Pushed to Silent Listening", "event_time": row.get("created_at")})
        if row.get("reviewed_at"):
            events.append(
                {
                    **base,
                    "event_type": "audio_reviewed",
                    "title": "Silent Listening review completed",
                    "event_time": row.get("reviewed_at"),
                    "actor_user_id": row.get("assignee_user_id"),
                    "actor_name": row.get("assignee_name"),
                }
            )

    def sort_key(event: dict[str, Any]) -> datetime:
        value = event.get("event_time")
        return value if isinstance(value, datetime) else datetime.min.replace(tzinfo=timezone.utc)

    events.sort(key=sort_key, reverse=True)
    return events[:50]


def get_main_case_detail(
    settings: Settings,
    user: AuthUser,
    submission_key: str,
    include_navigation: bool = True,
    include_audit: bool = True,
) -> dict[str, Any]:
    cache_key = (submission_key, user.role, include_navigation, include_audit)
    cached = MAIN_CASE_DETAIL_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < MAIN_CASE_DETAIL_CACHE_TTL_SECONDS:
        return cached[1]

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            panel_label_expr = _panel_label_sql("mcp.panel_code")
            cur.execute(
                f"""
                SELECT
                    mc.*,
                    mq.region_label,
                    mq.region_respondent_ordinal,
                    mq.start_time,
                    COALESCE(sp.selected_panel_codes, ARRAY[]::text[]) AS selected_panel_codes,
                    COALESCE(NULLIF(TRIM(mq.selected_panel_labels), ''), sp.selected_panel_labels, 'Omnibus') AS selected_panel_labels
                FROM clean.main_case mc
                INNER JOIN mart.main_case_queue mq ON mq.case_id = mc.case_id
                LEFT JOIN LATERAL (
                    SELECT
                        ARRAY_AGG(mcp.panel_code ORDER BY mcp.panel_code) AS selected_panel_codes,
                        STRING_AGG(DISTINCT {panel_label_expr}, ', ') AS selected_panel_labels
                    FROM clean.main_case_panel mcp
                    WHERE mcp.case_id = mc.case_id
                      AND COALESCE(mcp.is_selected, TRUE)
                ) sp ON TRUE
                WHERE mc.submission_key = %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM clean.deleted_main_cases dmc
                    WHERE dmc.submission_key = mc.submission_key
                  )
                LIMIT 1
                """,
                (submission_key,),
            )
            case_header = cur.fetchone()
            if not case_header:
                raise HTTPException(status_code=404, detail="Main Survey case not found.")
            case_header = dict(case_header)
            if include_navigation:
                case_header.update(get_main_case_navigation(settings, submission_key))
            else:
                city_labels = _city_choice_label_map(str(settings.root_dir))
                record_for_label = case_header.get("record") or {}
                raw_region = (
                    str(record_for_label.get("City_1") or "").strip()
                    or str(record_for_label.get("state_name") or "").strip()
                    or str(record_for_label.get("lga_name") or "").strip()
                )
                case_header["region_label"] = city_labels.get(raw_region, raw_region) or "Region"

            _enforce_case_visibility(user, case_header["approval_stage"])
            selected_panel_codes = [str(code) for code in (case_header.get("selected_panel_codes") or []) if code]

            sections: list[dict[str, Any]] = []

            section_by_id = {str(section.get("section_row_id") or ""): section for section in sections}
            section_by_name: dict[str, dict[str, Any]] = {}
            for section in sections:
                name = str(section.get("section_name") or "").strip()
                if name and name not in section_by_name:
                    section_by_name[name] = section

            current_record = case_header.get("record") or {}
            selected_panel_bau_snapshot = _selected_panel_bau_snapshot(str(settings.root_dir), current_record, selected_panel_codes)
            case_header["record"] = {
                key: current_record.get(key)
                for key in (
                    "City_1",
                    "city",
                    "state",
                    "state_name",
                    "lga_name",
                    "survey_month",
                    "submission_date",
                    "interviewer_id",
                )
                if isinstance(current_record, dict) and current_record.get(key) not in {None, ""}
            }

            cur.execute(
                """
                SELECT
                    iq.issue_id::text AS issue_id,
                    COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') AS issue_status,
                    COALESCE(iq.issue_summary, rr.result_message, rd.description, rr.rule_code, 'QC issue') AS issue_summary,
                    iq.resolution_note,
                    COALESCE(iq.created_at, rr.created_at) AS created_at,
                    iq.resolved_at,
                    rr.rule_code,
                    COALESCE(rr.severity, rd.severity, 'medium') AS severity,
                    rr.table_name,
                    rr.row_identifier,
                    rr.field_name,
                    COALESCE(NULLIF(TRIM(rr.case_id), ''), NULLIF(TRIM(iq.case_id), ''), NULLIF(TRIM(%s), ''), NULLIF(TRIM(%s), '')) AS case_id
                FROM qc.issue_queue iq
                LEFT JOIN qc.rule_result rr
                  ON rr.rule_result_id = iq.rule_result_id
                 AND rr.instrument_code = 'main'
                 AND COALESCE(NULLIF(TRIM(rr.result_status), ''), 'open') <> 'resolved'
                LEFT JOIN qc.rule_definition rd
                  ON rd.instrument_code = 'main'
                 AND rd.rule_code = rr.rule_code
                WHERE iq.instrument_code = 'main'
                  AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                  AND (
                    NULLIF(TRIM(iq.submission_key), '') = NULLIF(TRIM(%s), '')
                    OR NULLIF(TRIM(iq.case_id), '') = NULLIF(TRIM(%s), '')
                  )
                ORDER BY COALESCE(iq.created_at, rr.created_at) DESC
                """,
                (
                    str(case_header.get("case_id") or ""),
                    str(case_header.get("submission_key") or ""),
                    str(case_header.get("submission_key") or ""),
                    str(case_header.get("case_id") or ""),
                ),
            )
            issues = cur.fetchall()

            field_names = sorted({str(issue.get("field_name") or "").strip() for issue in issues if issue.get("field_name")})
            variable_labels: dict[str, str] = {}
            if field_names:
                cur.execute(
                    """
                    SELECT variable_name, question_label
                    FROM reference.xlsform_question
                    WHERE instrument_code = 'main' AND variable_name = ANY(%s)
                    """,
                    (field_names,),
                )
                variable_labels = {str(row.get("variable_name") or ""): str(row.get("question_label") or "") for row in cur.fetchall()}

            interviewer_id = normalize_main_interviewer_id(case_header.get("interviewer_id") or record.get("username"))
            phone_values = sorted({p for p in (_normalized_phone_key(v) for v in _extract_phone_candidates(current_record, sections)) if p})
            gps_point = _extract_gps(current_record)

            for issue in issues:
                row_identifier = str(issue.get("row_identifier") or "").strip()
                field_name = str(issue.get("field_name") or "").strip()
                table_name = str(issue.get("table_name") or "").strip()
                rule_code = str(issue.get("rule_code") or "").strip()

                if table_name == 'clean.main_case_section':
                    section = section_by_id.get(row_identifier) or section_by_name.get(row_identifier)
                    if section:
                        issue["case_label"] = str(section.get("section_name") or row_identifier)
                        record = section.get("record") or {}
                        if field_name and isinstance(record, dict) and not issue.get("current_value") and record.get(field_name) is not None:
                            issue["current_value"] = str(record.get(field_name))
                    else:
                        issue["case_label"] = row_identifier or str(case_header.get("submission_key") or "")
                else:
                    issue["case_label"] = str(case_header.get("submission_key") or issue.get("case_id") or "")
                    if field_name and isinstance(current_record, dict) and not issue.get("current_value") and current_record.get(field_name) is not None:
                        issue["current_value"] = str(current_record.get(field_name))

                if field_name and not issue.get("variable_label"):
                    label = variable_labels.get(field_name)
                    if label:
                        issue["variable_label"] = label

                if not issue.get("variable_label"):
                    if rule_code in {"MAIN_DUPLICATE_PHONE_NUMBER", "MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL"}:
                        issue["variable_label"] = "Respondent phone number"
                    elif rule_code == "MAIN_DUPLICATE_GPS":
                        issue["variable_label"] = "GPS coordinates"

                if rule_code in {"MAIN_DUPLICATE_PHONE_NUMBER", "MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL"} and not issue.get("current_value") and phone_values:
                    issue["current_value"] = ", ".join(phone_values)
                    issue["matching_cases"] = _duplicate_phone_affected_case_labels(
                        cur,
                        settings,
                        current_submission_key=str(case_header.get("submission_key") or submission_key),
                        current_case_id=str(case_header.get("case_id") or ""),
                        interviewer_id=interviewer_id,
                        phone_values=phone_values,
                        issue_summary=str(issue.get("issue_summary") or ""),
                        global_scope=rule_code == "MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL",
                    )
                    issue["matching_case_keys"] = [case["submission_key"] for case in issue["matching_cases"]]
                elif rule_code == "MAIN_DUPLICATE_GPS" and not issue.get("current_value") and gps_point[0] is not None and gps_point[1] is not None:
                    lat, lon = gps_point
                    issue["current_value"] = f"({lat}, {lon})"

            pending_changes = []
            history = []
            activity_timeline = _get_case_activity_timeline(
                cur,
                case_id=str(case_header.get("case_id") or ""),
                submission_key=str(case_header.get("submission_key") or submission_key),
            )
            if include_audit:
                cur.execute(
                    """
                    SELECT
                        change_id::text AS change_id,
                        issue_id::text AS issue_id,
                        case_id,
                        table_name,
                        row_identifier,
                        field_name,
                        current_value,
                        proposed_value,
                        change_reason,
                        change_status,
                        pc.requested_by_user_id::text AS requested_by_user_id,
                        pc.reviewed_by_user_id::text AS reviewed_by_user_id,
                        requester.full_name AS requested_by_name,
                        reviewer.full_name AS reviewed_by_name,
                        requested_device_id,
                        reviewed_device_id,
                        requested_at,
                        reviewed_at,
                        review_note
                    FROM qc.pending_change pc
                    LEFT JOIN app.user_account requester
                        ON requester.user_id = pc.requested_by_user_id
                    LEFT JOIN app.user_account reviewer
                        ON reviewer.user_id = pc.reviewed_by_user_id
                    WHERE pc.instrument_code = 'main' AND pc.submission_key = %s
                    ORDER BY requested_at DESC
                    """,
                    (submission_key,),
                )
                pending_changes = cur.fetchall()

                cur.execute(
                    """
                    SELECT
                        status_history_id::text AS status_history_id,
                        previous_status,
                        new_status,
                        change_note,
                        changed_at,
                        h.device_id,
                        h.changed_by_user_id::text AS changed_by_user_id,
                        u.full_name AS changed_by_name,
                        u.email AS changed_by_email
                    FROM qc.case_status_history h
                    LEFT JOIN app.user_account u
                        ON u.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'main' AND h.submission_key = %s
                    ORDER BY h.changed_at DESC
                    """,
                    (submission_key,),
                )
                history = cur.fetchall()

    payload = {
        "case": case_header,
        "sections": sections,
        "issues": issues,
        "pendingChanges": pending_changes,
        "history": history,
        "activity_timeline": activity_timeline,
        "selectedPanelBauSnapshot": selected_panel_bau_snapshot,
    }
    MAIN_CASE_DETAIL_CACHE[cache_key] = (time.monotonic(), payload)
    return payload


# ---------------------------------------------------------------------------
# Status update
# ---------------------------------------------------------------------------

def update_main_case_status(
    settings: Settings,
    user: AuthUser,
    submission_key: str,
    new_status: str,
    note: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    _ensure_status_allowed(new_status)
    if user.role not in MAIN_REVIEW_DECISION_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to update case status.")

    # Keep interactive decisions responsive. Long retry loops make the browser look
    # frozen and encourage duplicate clicks while another transaction holds a lock.
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            with db_connection(settings) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '2s'")
                    cur.execute("SET LOCAL statement_timeout = '10s'")
                    cur.execute(
                        """
                        SELECT approval_stage, case_id
                        FROM clean.main_case
                        WHERE submission_key = %s
                        FOR UPDATE
                        """,
                        (submission_key,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="Main Survey case not found.")
                    previous_status = row["approval_stage"]
                    case_id = row.get("case_id") or submission_key
                    status_changed = previous_status != new_status

                    # A repeated request after a client timeout is idempotent: do not
                    # create duplicate status history, but still close its review queue.
                    if status_changed and new_status in MAIN_FINAL_STATUSES:
                        cur.execute(
                            "UPDATE clean.main_case SET approval_stage = %s, is_callback_required = false, updated_at = now() WHERE submission_key = %s",
                            (new_status, submission_key),
                        )
                        cur.execute(
                            """
                            UPDATE clean.audio_listening
                            SET status = 'reviewed',
                                reviewed_at = COALESCE(reviewed_at, now())
                            WHERE case_id = %s
                              AND COALESCE(NULLIF(TRIM(status), ''), 'pending') = 'pending'
                            """,
                            (case_id,),
                        )
                    elif status_changed:
                        cur.execute(
                            "UPDATE clean.main_case SET approval_stage = %s, updated_at = now() WHERE submission_key = %s",
                            (new_status, submission_key),
                        )
                    elif new_status in MAIN_FINAL_STATUSES:
                        cur.execute(
                            """
                            UPDATE clean.audio_listening
                            SET status = 'reviewed',
                                reviewed_at = COALESCE(reviewed_at, now())
                            WHERE case_id = %s
                              AND COALESCE(NULLIF(TRIM(status), ''), 'pending') = 'pending'
                            """,
                            (case_id,),
                        )

                    if status_changed:
                        _insert_case_status_history(cur, submission_key, case_id, previous_status, new_status, user, note, device_id)
                conn.commit()

            # The case decision is authoritative and has already committed. Mart
            # synchronization is best-effort and must never delay or roll it back.
            try:
                with db_connection(settings) as sync_conn:
                    with sync_conn.cursor() as sync_cur:
                        sync_cur.execute("SET LOCAL lock_timeout = '1s'")
                        sync_cur.execute("SET LOCAL statement_timeout = '5s'")
                        _sync_main_case_queue_rows(sync_cur, [case_id])
                    sync_conn.commit()
            except Exception:
                logger.exception("Unable to sync Main case queue after status update for %s.", submission_key)

            _clear_main_status_dependent_caches(settings)
            return {
                "submissionKey": submission_key,
                "previousStatus": previous_status,
                "newStatus": new_status,
                "statusChanged": status_changed,
            }
        except HTTPException:
            raise
        except Exception as exc:
            is_transient = _is_transient_db_lock_error(exc)
            if not is_transient:
                raise
            if attempt >= max_attempts - 1:
                raise HTTPException(
                    status_code=503,
                    detail="This case is temporarily busy. Please wait a moment and try again.",
                ) from exc
            logger.warning(
                "Retrying main case status update after transient database lock for submission %s (attempt %s/%s): %s",
                submission_key,
                attempt + 1,
                max_attempts,
                exc,
            )
            _sleep_before_db_retry(attempt)

    raise HTTPException(status_code=503, detail="This case is temporarily busy. Please wait a moment and try again.")


def update_main_ea_status(
    settings: Settings,
    user: AuthUser,
    ea_id: str,
    new_status: str,
    note: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    _ensure_status_allowed(new_status)
    if new_status not in MAIN_FINAL_STATUSES:
        raise HTTPException(status_code=400, detail="EA status action must be approved or rejected.")
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to update EA status.")

    normalized_ea_id = str(ea_id or "").strip()
    if not normalized_ea_id:
        raise HTTPException(status_code=400, detail="EA ID is required.")

    updated_keys: list[str] = []
    previous_statuses: dict[str, str | None] = {}
    resolved_ea_name: str | None = None

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    mc.submission_key,
                    mc.case_id,
                    mc.approval_stage,
                    COALESCE(
                        NULLIF(TRIM(mc.record->>'ea_name'), ''),
                        NULLIF(TRIM(g.properties->>'sd_EA_NAME'), ''),
                        NULLIF(TRIM(mc.record->>'sd_EA_NAME'), ''),
                        NULLIF(TRIM(mc.record->>'name'), '')
                    ) AS ea_name
                FROM clean.main_case mc
                LEFT JOIN reference.geo_boundaries_ea g
                    ON g.ea_id = COALESCE(NULLIF(TRIM(mc.ea_id), ''), NULLIF(TRIM(mc.record->>'ea_id'), ''))
                WHERE REGEXP_REPLACE(COALESCE(NULLIF(TRIM(mc.ea_id), ''), NULLIF(TRIM(mc.record->>'ea_id'), ''), ''), '\\.0+$', '') =
                      REGEXP_REPLACE(%s, '\\.0+$', '')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = mc.submission_key
                  )
                ORDER BY mc.submitted_at DESC NULLS LAST
                """,
                (normalized_ea_id,),
            )
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail="No main survey cases found for this EA.")

            for row in rows:
                submission_key = str(row.get("submission_key") or "").strip()
                if not submission_key:
                    continue
                previous_status = row.get("approval_stage")
                case_id = row.get("case_id") or submission_key
                if resolved_ea_name is None and row.get("ea_name"):
                    resolved_ea_name = str(row.get("ea_name"))
                previous_statuses[submission_key] = previous_status
                cur.execute(
                    "UPDATE clean.main_case SET approval_stage = %s, is_callback_required = false, updated_at = now() WHERE submission_key = %s",
                    (new_status, submission_key),
                )
                _insert_case_status_history(
                    cur,
                    submission_key,
                    case_id,
                    previous_status,
                    new_status,
                    user,
                    note,
                    device_id,
                )
                updated_keys.append(submission_key)
        conn.commit()

    return {
        "eaId": normalized_ea_id,
        "eaName": resolved_ea_name,
        "updated": len(updated_keys),
        "newStatus": new_status,
        "submissionKeys": updated_keys,
        "previousStatuses": previous_statuses,
    }


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

def create_main_pending_change(
    settings: Settings,
    user: AuthUser,
    submission_key: str,
    case_id: str | None,
    issue_id: str | None,
    table_name: str,
    row_identifier: str | None,
    field_name: str,
    proposed_value: str,
    reason: str,
    device_id: str | None = None,
) -> dict[str, Any]:
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to submit corrections.")
    if table_name not in EDITABLE_TABLES:
        raise HTTPException(status_code=400, detail="Unsupported target table for correction.")

    key_column = EDITABLE_TABLES[table_name]
    lookup_value = row_identifier if key_column != "submission_key" else submission_key
    if not lookup_value:
        raise HTTPException(status_code=400, detail="Row identifier is required for this correction.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT record FROM {} WHERE {} = %s").format(
                    sql.SQL(table_name),
                    sql.Identifier(key_column),
                ),
                (lookup_value,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Target record not found.")

            current_value = None
            record = row.get("record") or {}
            if isinstance(record, dict):
                current_value = record.get(field_name)

            cur.execute(
                """
                INSERT INTO qc.pending_change (
                    instrument_code,
                    submission_key,
                    case_id,
                    issue_id,
                    table_name,
                    row_identifier,
                    field_name,
                    current_value,
                    proposed_value,
                    change_reason,
                    requested_by_user_id,
                    requested_device_id
                )
                VALUES ('main', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING change_id::text AS change_id, change_status, requested_at
                """,
                (
                    submission_key,
                    case_id,
                    issue_id,
                    table_name,
                    row_identifier,
                    field_name,
                    None if current_value is None else str(current_value),
                    proposed_value,
                    reason,
                    user.id,
                    device_id,
                ),
            )
            created = cur.fetchone()

            if issue_id:
                cur.execute(
                    """
                    UPDATE qc.issue_queue
                    SET issue_status = 'in_review', updated_at = now()
                    WHERE issue_id = %s AND submission_key = %s
                    """,
                    (issue_id, submission_key),
                )
            _apply_main_pending_change_review(
                cur,
                user,
                created["change_id"],
                "approved",
                AUTO_APPROVED_CORRECTION_NOTE,
                device_id,
            )
            created["change_status"] = "approved"
            created["review_note"] = AUTO_APPROVED_CORRECTION_NOTE
        conn.commit()

    return created


def _apply_main_pending_change_review(
    cur: Any,
    user: AuthUser,
    change_id: str,
    decision: str,
    note: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    cur.execute("SELECT * FROM qc.pending_change WHERE change_id = %s", (change_id,))
    change = cur.fetchone()
    if not change:
        raise HTTPException(status_code=404, detail="Pending change not found.")
    if change["change_status"] != "pending":
        raise HTTPException(status_code=400, detail="Pending change has already been reviewed.")

    if decision == "approved":
        table_name = change["table_name"]
        key_column = EDITABLE_TABLES[table_name]
        lookup_value = change["row_identifier"] if key_column != "submission_key" else change["submission_key"]

        cur.execute(
            sql.SQL("SELECT record FROM {} WHERE {} = %s").format(
                sql.SQL(table_name), sql.Identifier(key_column)
            ),
            (lookup_value,),
        )
        current_row = cur.fetchone()
        current_record = current_row["record"] if current_row else {}
        if not isinstance(current_record, dict):
            current_record = {}
        parsed_value = _normalize_json_value(change["proposed_value"] or "")
        current_record[change["field_name"]] = parsed_value

        assignments = [sql.SQL("record = %s::jsonb"), sql.SQL("updated_at = now()")]
        params: list[Any] = [_serialize_record(current_record)]
        if change["field_name"] in STRUCTURED_FIELDS.get(table_name, set()):
            assignments.insert(1, sql.SQL("{} = %s").format(sql.Identifier(change["field_name"])))
            params.insert(1, _coerce_structured_value(change["field_name"], change["proposed_value"] or ""))
        params.append(lookup_value)

        cur.execute(
            sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                sql.SQL(table_name),
                sql.SQL(", ").join(assignments),
                sql.Identifier(key_column),
            ),
            params,
        )

        cur.execute(
            """
            INSERT INTO qc.data_change_log (
                instrument_code,
                submission_key,
                case_id,
                table_name,
                row_identifier,
                field_name,
                old_value,
                new_value,
                changed_by_user_id,
                change_reason,
                issue_id,
                changed_by_device_id
            )
            VALUES ('main', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                change["submission_key"],
                change["case_id"],
                change["table_name"],
                change["row_identifier"],
                change["field_name"],
                change["current_value"],
                change["proposed_value"],
                user.id,
                change["change_reason"],
                change["issue_id"],
                device_id,
            ),
        )

        if change.get("issue_id"):
            cur.execute(
                """
                UPDATE qc.issue_queue
                SET issue_status = 'resolved',
                    updated_at = now(),
                    resolved_at = now(),
                    resolution_note = %s
                WHERE issue_id = %s
                """,
                (note or "Correction approved and applied.", change["issue_id"]),
            )
    elif change.get("issue_id"):
        cur.execute(
            """
            UPDATE qc.issue_queue
            SET issue_status = 'pending_review', updated_at = now(), resolution_note = %s
            WHERE issue_id = %s
            """,
            (note or "Correction request was rejected.", change["issue_id"]),
        )

    cur.execute(
        """
        UPDATE qc.pending_change
        SET change_status = %s,
            reviewed_by_user_id = %s,
            reviewed_device_id = %s,
            reviewed_at = now(),
            review_note = %s
        WHERE change_id = %s
        """,
        (decision, user.id, device_id, note or "", change_id),
    )

    return {"changeId": change_id, "decision": decision}


def review_main_pending_change(
    settings: Settings,
    user: AuthUser,
    change_id: str,
    decision: str,
    note: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    if user.role not in MAIN_REVIEW_DECISION_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to review corrections.")
    if decision not in MAIN_FINAL_STATUSES:
        raise HTTPException(status_code=400, detail="Decision must be approved or rejected.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            result = _apply_main_pending_change_review(cur, user, change_id, decision, note, device_id)
        conn.commit()

    return result


def apply_main_analysis_correction(
    settings: Settings,
    user: AuthUser,
    submission_key: str,
    field_name: str,
    old_value: str,
    new_value: str,
    question_label: str,
    corrected_by_username: str,
) -> dict[str, Any]:
    """Directly apply a field correction to main_case/main_case_section for a submission."""
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to apply corrections.")
    table_name = "clean.main_case_section"

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT case_id, record FROM clean.main_case WHERE submission_key = %s",
                (submission_key,),
            )
            case_row = cur.fetchone()
            if not case_row:
                raise HTTPException(status_code=404, detail="Main Survey case not found.")

            updated = 0
            case_record = case_row.get("record") or {}
            if isinstance(case_record, dict) and _answer_values_match(case_record.get(field_name), old_value):
                parsed_value = _normalize_json_value(new_value)
                current_str = None if case_record.get(field_name) is None else str(case_record.get(field_name))
                case_record[field_name] = parsed_value

                assignments = [sql.SQL("record = %s::jsonb"), sql.SQL("updated_at = now()")]
                params: list[Any] = [_serialize_record(case_record)]
                if field_name in STRUCTURED_FIELDS.get("clean.main_case", set()):
                    assignments.insert(1, sql.SQL("{} = %s").format(sql.Identifier(field_name)))
                    params.insert(1, _coerce_structured_value(field_name, new_value))
                params.append(submission_key)

                cur.execute(
                    sql.SQL("UPDATE clean.main_case SET {} WHERE submission_key = %s").format(
                        sql.SQL(", ").join(assignments),
                    ),
                    params,
                )

                cur.execute(
                    """
                    INSERT INTO qc.data_change_log (
                        instrument_code,
                        submission_key,
                        table_name,
                        row_identifier,
                        field_name,
                        old_value,
                        new_value,
                        changed_by_user_id,
                        change_reason
                    )
                    VALUES ('main', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        submission_key,
                        "clean.main_case",
                        submission_key,
                        field_name,
                        current_str,
                        new_value,
                        user.id,
                        f"Analysis correction for question: {question_label}",
                    ),
                )
                updated += 1

            cur.execute(
                """
                SELECT section_row_id, record
                FROM clean.main_case_section
                WHERE case_id = %s
                """,
                (case_row["case_id"],),
            )
            rows = cur.fetchall()

            for row in rows:
                record = row.get("record") or {}
                if not isinstance(record, dict):
                    continue
                current_raw = record.get(field_name)
                if not _answer_values_match(current_raw, old_value):
                    continue

                current_str = None if current_raw is None else str(current_raw)
                parsed_value = _normalize_json_value(new_value)
                record[field_name] = parsed_value

                assignments = [sql.SQL("record = %s::jsonb"), sql.SQL("updated_at = now()")]
                params: list[Any] = [_serialize_record(record)]
                if field_name in STRUCTURED_FIELDS.get(table_name, set()):
                    assignments.insert(1, sql.SQL("{} = %s").format(sql.Identifier(field_name)))
                    params.insert(1, _coerce_structured_value(field_name, new_value))
                params.append(row["section_row_id"])

                cur.execute(
                    sql.SQL("UPDATE {} SET {} WHERE section_row_id = %s").format(
                        sql.SQL(table_name),
                        sql.SQL(", ").join(assignments),
                    ),
                    params,
                )

                cur.execute(
                    """
                    INSERT INTO qc.data_change_log (
                        instrument_code,
                        submission_key,
                        table_name,
                        row_identifier,
                        field_name,
                        old_value,
                        new_value,
                        changed_by_user_id,
                        change_reason
                    )
                    VALUES ('main', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        submission_key,
                        table_name,
                        str(row["section_row_id"]),
                        field_name,
                        current_str,
                        new_value,
                        user.id,
                        f"Analysis correction for question: {question_label}",
                    ),
                )
                updated += 1

            if updated == 0:
                raise HTTPException(
                    status_code=404,
                    detail=f"No matching value found for {field_name} on this submission.",
                )

        conn.commit()

    return {"updated_rows": updated, "message": f"Correction applied to {updated} row(s)."}


# ---------------------------------------------------------------------------
# QC rule engine
# ---------------------------------------------------------------------------


def _normalized_phone_key(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-11:] if len(digits) >= 7 else ""


def _start_time_flag(dt: datetime | None) -> bool:
    return dt is not None and (dt.hour >= 19 or dt.hour < 7)


def _main_clean_matrix_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text[:-2] if text.endswith(".0") else text


def _main_case_selected_panel_codes(case_id: str, panels_by_case_id: dict[str, list[str]]) -> list[str]:
    return [code for code in panels_by_case_id.get(case_id, []) if code]


def _build_enumerator_matrix_anomalies(
    cases: list[dict[str, Any]],
    cached: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str, datetime | None, datetime | None]],
    panels_by_case_id: dict[str, list[str]],
    settings: Settings,
) -> dict[str, str]:
    by_interviewer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        submission_key = str(case.get("submission_key") or "").strip()
        if not submission_key:
            continue
        record, _sections, interviewer_id, _start_dt, _end_dt = cached.get(submission_key, ({}, [], "", None, None))
        interviewer = str(interviewer_id or "").strip()
        if not interviewer:
            continue
        by_interviewer[interviewer].append(
            {
                "submission_key": submission_key,
                "case_id": str(case.get("case_id") or "").strip(),
                "record": record if isinstance(record, dict) else {},
            }
        )

    gender_labels = _choice_label_map_from_xlsform(str(settings.root_dir), "Gender")
    age_labels = _choice_label_map_from_xlsform(str(settings.root_dir), "Age_cal")
    sector_labels = {**MAIN_SECTOR_LABELS, **{f"{code}.0": label for code, label in MAIN_SECTOR_LABELS.items()}}

    findings_by_submission: dict[str, str] = {}
    for interviewer, interviewer_cases in by_interviewer.items():
        total_cases = len(interviewer_cases)
        if total_cases < MAIN_ENUMERATOR_MATRIX_MIN_CASES:
            continue

        buckets: dict[str, Counter[str]] = {
            "Gender": Counter(),
            "Age_cal": Counter(),
            "Sector": Counter(),
            "selected_panel": Counter(),
        }
        for item in interviewer_cases:
            record = item["record"]
            gender = _main_clean_matrix_value(record.get("Gender"))
            age = _main_clean_matrix_value(record.get("Age_cal"))
            sector = _main_clean_matrix_value(record.get("Sector"))
            if gender:
                buckets["Gender"][gender] += 1
            if age:
                buckets["Age_cal"][age] += 1
            if sector:
                buckets["Sector"][sector] += 1
            for panel_code in _main_case_selected_panel_codes(item["case_id"], panels_by_case_id):
                buckets["selected_panel"][panel_code] += 1

        messages: list[str] = []
        for variable, counter in buckets.items():
            if not counter:
                continue
            top_value, top_count = counter.most_common(1)[0]
            denominator = total_cases
            share = top_count / denominator if denominator else 0
            threshold = MAIN_ENUMERATOR_MATRIX_THRESHOLDS[variable]
            if share < threshold:
                continue
            if variable == "Gender":
                label = gender_labels.get(top_value) or gender_labels.get(f"{top_value}.0") or top_value
                variable_label = "Gender"
            elif variable == "Age_cal":
                label = age_labels.get(top_value) or age_labels.get(f"{top_value}.0") or top_value
                variable_label = "Age band"
            elif variable == "Sector":
                label = sector_labels.get(top_value) or sector_labels.get(f"{top_value}.0") or top_value
                variable_label = "Sector"
            else:
                label = BHT_PANEL_LABEL_BY_CODE.get(top_value, top_value)
                variable_label = "Selected panel"
            messages.append(
                f"{variable_label} '{label}' appears in {top_count}/{total_cases} cases ({share * 100:.1f}%) for interviewer {interviewer}."
            )

        if not messages:
            continue
        message = "Enumerator matrix anomaly: " + " ".join(messages[:4])
        for item in interviewer_cases:
            findings_by_submission[item["submission_key"]] = message

    return findings_by_submission


def _main_find_value(case_record: dict[str, Any], sections: list[dict[str, Any]], field_name: str) -> tuple[Any, str | None]:
    if field_name in case_record:
        return case_record.get(field_name), None
    for section in sections:
        sec_record = section.get("record") or {}
        if isinstance(sec_record, dict) and field_name in sec_record:
                return sec_record.get(field_name), str(section.get("section_row_id") or "") or None
    return None, None


def _main_loi_minutes(record: dict[str, Any], start_dt: datetime | None, end_dt: datetime | None) -> float | None:
    duration = _safe_float(record.get("duration"))
    if duration is not None and duration >= 0:
        return duration / 60 if duration > MAIN_INTERVIEW_MAX_MINUTES else duration
    if start_dt and end_dt and end_dt >= start_dt:
        return (end_dt - start_dt).total_seconds() / 60
    return None


def run_main_qc(
    settings: Settings,
    submission_key: str | None = None,
    submission_keys: list[str] | None = None,
    user: AuthUser | None = None,
    device_id: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
    only_pending: bool = True,
    batch_limit: int | None = 500,
) -> dict[str, Any]:
    def report(percent: int, message: str) -> None:
        if progress_callback is None:
            return
        progress_callback(max(1, min(99, int(percent))), message)

    report(1, "Preparing Main QC rules.")
    bootstrap_main_rule_definitions(settings)
    report(5, "Loading Main Survey cases that need QC.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            selected_submission_keys = [str(key or "").strip() for key in (submission_keys or []) if str(key or "").strip()]
            if submission_key and not selected_submission_keys:
                selected_submission_keys = [submission_key]
            if selected_submission_keys:
                cur.execute("DELETE FROM qc.issue_queue WHERE instrument_code = 'main' AND submission_key = ANY(%s)", (selected_submission_keys,))
                cur.execute("DELETE FROM qc.rule_result WHERE instrument_code = 'main' AND submission_key = ANY(%s)", (selected_submission_keys,))
                cur.execute(
                    """
                    SELECT submission_key, case_id, ea_id, interviewer_id, approval_stage, submitted_at, record
                    FROM clean.main_case
                    WHERE submission_key = ANY(%s)
                    ORDER BY submitted_at DESC NULLS LAST
                    """,
                    (selected_submission_keys,),
                )
            else:
                where_sql = """
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM clean.deleted_main_cases dmc
                        WHERE dmc.submission_key = clean.main_case.submission_key
                    )
                """
                if only_pending:
                    where_sql += " AND LOWER(COALESCE(approval_stage, '')) NOT IN ('approved', 'rejected')"
                limit_sql = ""
                if batch_limit is not None and batch_limit > 0:
                    limit_sql = f"LIMIT {int(batch_limit)}"
                cur.execute(
                    f"""
                    SELECT submission_key, case_id, ea_id, interviewer_id, approval_stage, submitted_at, record
                    FROM clean.main_case
                    {where_sql}
                    ORDER BY submitted_at DESC NULLS LAST
                    {limit_sql}
                    """
                )
                cases = cur.fetchall()
                submission_keys = [str(case.get("submission_key") or "").strip() for case in cases if str(case.get("submission_key") or "").strip()]
                if submission_keys:
                    cur.execute("DELETE FROM qc.issue_queue WHERE instrument_code = 'main' AND submission_key = ANY(%s)", (submission_keys,))
                    cur.execute("DELETE FROM qc.rule_result WHERE instrument_code = 'main' AND submission_key = ANY(%s)", (submission_keys,))
                total_cases = max(len(cases), 1)
                report(10, f"Loaded {len(cases):,} case(s) for this QC batch. Loading section records.")

            if selected_submission_keys:
                cases = cur.fetchall()
                total_cases = max(len(cases), 1)
            report(10, f"Loaded {len(cases):,} case(s). Loading section records.")
            created = 0
            case_ids = [str(case.get("case_id") or "").strip() for case in cases if str(case.get("case_id") or "").strip()]
            sections_by_case_id: dict[str, list[dict[str, Any]]] = {cid: [] for cid in case_ids}
            panels_by_case_id: dict[str, list[str]] = {cid: [] for cid in case_ids}
            if case_ids:
                cur.execute(
                    """
                    SELECT case_id::text AS case_id, section_row_id::text AS section_row_id, section_name, record
                    FROM clean.main_case_section
                    WHERE case_id = ANY(%s)
                    """,
                    (case_ids,),
                )
                for row in cur.fetchall():
                    cid = str(row.get("case_id") or "").strip()
                    if cid:
                        sections_by_case_id.setdefault(cid, []).append(dict(row))
                cur.execute(
                    """
                    SELECT case_id::text AS case_id, panel_code
                    FROM clean.main_case_panel
                    WHERE case_id = ANY(%s)
                      AND COALESCE(is_selected, TRUE)
                    """,
                    (case_ids,),
                )
                for row in cur.fetchall():
                    cid = str(row.get("case_id") or "").strip()
                    panel_code = str(row.get("panel_code") or "").strip()
                    if cid and panel_code:
                        panels_by_case_id.setdefault(cid, []).append(panel_code)
            report(18, "Building QC indexes.")

            durations: list[float] = []
            interviewer_timeline: dict[str, list[tuple[datetime, datetime, str]]] = {}
            phone_submissions: dict[str, dict[str, set[str]]] = {}
            global_phone_submissions: dict[str, set[str]] = {}
            gps_by_interviewer: dict[str, dict[tuple[float, float], set[str]]] = {}
            cached: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str, datetime | None, datetime | None]] = {}

            for index, case in enumerate(cases, start=1):
                sub_key = str(case["submission_key"])
                cid = str(case.get("case_id") or "").strip()
                record = case.get("record") or {}
                sections = sections_by_case_id.get(cid, [])
                interviewer_id = normalize_main_interviewer_id(case.get("interviewer_id") or record.get("interviewer_id") or record.get("username"))
                start_dt = _parse_datetime(record.get("starttime"))
                end_dt = _parse_datetime(record.get("endtime"))
                cached[sub_key] = (record, sections, interviewer_id, start_dt, end_dt)
                duration_minutes = _main_loi_minutes(record, start_dt, end_dt)
                if duration_minutes is not None:
                    durations.append(duration_minutes)
                    if interviewer_id and start_dt and end_dt and end_dt >= start_dt:
                        interviewer_timeline.setdefault(interviewer_id, []).append((start_dt, end_dt, sub_key))
                phone_values = {phone for phone in (_normalized_phone_key(p) for p in _extract_phone_candidates(record, sections)) if phone}
                for phone in phone_values:
                    global_phone_submissions.setdefault(phone, set()).add(sub_key)
                if interviewer_id:
                    phone_index = phone_submissions.setdefault(interviewer_id, {})
                    for phone in phone_values:
                        if phone:
                            phone_index.setdefault(phone, set()).add(sub_key)
                lat, lon = _extract_gps(record)
                if interviewer_id and lat is not None and lon is not None:
                    gps_by_interviewer.setdefault(interviewer_id, {}).setdefault((lat, lon), set()).add(sub_key)
                if index == total_cases or index % 250 == 0:
                    report(18 + round((index / total_cases) * 17), f"Indexed {index:,} of {len(cases):,} case(s).")

            loi_median = median(durations) if durations else None
            for interviewer_id in interviewer_timeline:
                interviewer_timeline[interviewer_id].sort(key=lambda item: item[0])
            interviewer_timeline_index: dict[str, tuple[list[tuple[datetime, datetime, str]], int]] = {}
            for timeline in interviewer_timeline.values():
                for idx, (_, _, timeline_submission_key) in enumerate(timeline):
                    interviewer_timeline_index[timeline_submission_key] = (timeline, idx)
            enumerator_matrix_findings = _build_enumerator_matrix_anomalies(cases, cached, panels_by_case_id, settings)
            report(36, "Scanning cases for QC issues.")

            numeric_specs = [
                ("LC5c", "MAIN_LC5C", lambda v: v > 5000 or v < 100 or int(v) % 10 != 0, "above 5000, below 100, or not ending with 0"),
                ("C3a_female", "MAIN_C3A_FEMALE", lambda v: v > 5, "greater than 5"),
                ("C3a_male", "MAIN_C3A_MALE", lambda v: v > 5, "greater than 5"),
                ("C5", "MAIN_C5", lambda v: v > 10, "greater than 10"),
                ("HHTotal", "MAIN_HHTOTAL", lambda v: v > 15, "greater than 15"),
            ]

            for index, case in enumerate(cases, start=1):
                sub_key = str(case["submission_key"])
                record, sections, interviewer_id, start_dt, end_dt = cached[sub_key]

                duration_minutes = _main_loi_minutes(record, start_dt, end_dt)
                if duration_minutes is not None and loi_median:
                    if duration_minutes < (0.5 * loi_median):
                        created += _create_issue(cur, sub_key, "MAIN_LOW_LOI", "high", f"Main LOI is {duration_minutes:.1f} minutes, below 50% of median LOI ({loi_median:.1f}).", None, "record", "clean.main_case")
                    if duration_minutes > (1.5 * loi_median):
                        created += _create_issue(cur, sub_key, "MAIN_HIGH_LOI", "high", f"Main LOI is {duration_minutes:.1f} minutes, above 150% of median LOI ({loi_median:.1f}).", None, "record", "clean.main_case")

                if _start_time_flag(start_dt):
                    created += _create_issue(cur, sub_key, "MAIN_START_TIME", "high", f"Interview started at {start_dt.strftime('%H:%M')}, which falls within odd hours (7:00 PM to 6:59 AM).", None, "record", "clean.main_case")

                case_phone_values = {phone for phone in (_normalized_phone_key(p) for p in _extract_phone_candidates(record, sections)) if phone}
                duplicate_phones = sorted(phone for phone in case_phone_values if phone and len(phone_submissions.get(interviewer_id, {}).get(phone, set())) > 1)
                if duplicate_phones:
                    created += _create_issue(cur, sub_key, "MAIN_DUPLICATE_PHONE_NUMBER", "high", "Duplicate phone number(s) within interviewer: " + ", ".join(duplicate_phones[:5]), None, "record", "clean.main_case")
                global_duplicate_phones = sorted(phone for phone in case_phone_values if phone and len(global_phone_submissions.get(phone, set())) > 1)
                if global_duplicate_phones:
                    created += _create_issue(cur, sub_key, "MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL", "high", "Duplicate phone number(s) across active dataset: " + ", ".join(global_duplicate_phones[:5]), None, "record", "clean.main_case")

                lat, lon = _extract_gps(record)
                if interviewer_id and lat is not None and lon is not None and len(gps_by_interviewer.get(interviewer_id, {}).get((lat, lon), set())) > 1:
                    created += _create_issue(cur, sub_key, "MAIN_DUPLICATE_GPS", "high", f"GPS ({lat}, {lon}) appears in other main interviews by interviewer {interviewer_id}.", None, "record", "clean.main_case")

                if interviewer_id and start_dt and end_dt and end_dt >= start_dt:
                    timeline_entry = interviewer_timeline_index.get(sub_key)
                    if timeline_entry:
                        timeline, idx = timeline_entry
                        s, e, _ = timeline[idx]
                        if idx > 0:
                            prev_s, prev_e, prev_sk = timeline[idx - 1]
                            gap_minutes = (s - prev_e).total_seconds() / 60
                            if 0 <= gap_minutes < 5:
                                created += _create_issue(cur, sub_key, "MAIN_GAP_BETWEEN_2_INTERVIEWS", "high", f"Gap is {gap_minutes:.1f} minutes between this interview and {prev_sk} for interviewer {interviewer_id}.", None, "record", "clean.main_case")
                            overlap_minutes = (prev_e - s).total_seconds() / 60
                            if overlap_minutes > 1:
                                created += _create_issue(cur, sub_key, "MAIN_TIME_INTERWOVEN", "high", f"Interview overlaps with {prev_sk} for interviewer {interviewer_id} by {overlap_minutes:.1f} minutes.", None, "record", "clean.main_case")
                        if idx + 1 < len(timeline):
                            next_s, _, next_sk = timeline[idx + 1]
                            overlap_minutes = (end_dt - next_s).total_seconds() / 60
                            if overlap_minutes > 1:
                                created += _create_issue(cur, sub_key, "MAIN_TIME_INTERWOVEN", "high", f"Interview overlaps with {next_sk} for interviewer {interviewer_id} by {overlap_minutes:.1f} minutes.", None, "record", "clean.main_case")

                matrix_message = enumerator_matrix_findings.get(sub_key)
                if matrix_message:
                    created += _create_issue(cur, sub_key, "MAIN_ENUMERATOR_MATRIX_ANOMALY", "high", matrix_message, None, "record", "clean.main_case")

                for field_name, rule_code, checker, reason in numeric_specs:
                    raw_value, section_row_id = _main_find_value(record, sections, field_name)
                    value = _safe_float(raw_value)
                    if value is not None and checker(value):
                        created += _create_issue(cur, sub_key, rule_code, "medium", f"{field_name} is {value:g}, {reason}.", section_row_id, field_name, "clean.main_case_section" if section_row_id else "clean.main_case")
                if index == total_cases or index % 100 == 0:
                    report(36 + round((index / total_cases) * 52), f"Scanned {index:,} of {len(cases):,} case(s).")

            report(90, "Finalizing Main QC results.")
            report(97, "Saving Main QC results.")
            conn.commit()

    return {"createdIssueCount": created, "autoApprovedCount": 0}


def list_main_qc_pending_submission_keys(settings: Settings) -> list[str]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT submission_key
                FROM clean.main_case
                WHERE LOWER(COALESCE(approval_stage, '')) NOT IN ('approved', 'rejected')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = clean.main_case.submission_key
                  )
                ORDER BY submitted_at DESC NULLS LAST
                """
            )
            return [str(row.get("submission_key") or "").strip() for row in cur.fetchall() if str(row.get("submission_key") or "").strip()]


# ---------------------------------------------------------------------------
# Callback management (design doc Section 4.3 / qc_callback_outcome)
# ---------------------------------------------------------------------------

def list_callbacks(settings: Settings, user: AuthUser) -> list[dict[str, Any]]:
    params: list[Any] = []
    history_join_condition = "co.case_id = mc.case_id"
    visibility_clause = ""
    city_label_expr_all = _city_label_sql("mc_all.record", str(settings.root_dir))

    query = f"""
        WITH all_case_ordinals AS (
            SELECT
                ranked.case_id,
                ranked.region_label,
                ranked.region_respondent_ordinal
            FROM (
                SELECT
                    mc_all.case_id,
                    COALESCE(
                        NULLIF(TRIM({city_label_expr_all}), ''),
                        NULLIF(TRIM(mcd_all.state_name), ''),
                        NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                        NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                        'Region'
                    ) AS region_label,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            NULLIF(TRIM({city_label_expr_all}), ''),
                            NULLIF(TRIM(mcd_all.state_name), ''),
                            NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                            NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                            'Region'
                        )
                        ORDER BY mc_all.submitted_at ASC NULLS LAST, mc_all.created_at ASC NULLS LAST, mc_all.case_id ASC
                    )::int AS region_respondent_ordinal
                FROM clean.main_case mc_all
                LEFT JOIN mart.main_case_dim mcd_all ON mcd_all.case_id = mc_all.case_id
            ) ranked
        ),
        issue_counts AS (
            SELECT
                COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), '')) AS join_key,
                COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issue_count
            FROM qc.issue_queue iq
            WHERE iq.instrument_code = 'main'
            GROUP BY COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
        )
        SELECT
            mq.submission_key,
            mq.case_id,
            mq.ea_id,
            mq.interviewer_id,
            mq.supervisor_id,
            mq.approval_stage,
            mq.start_time,
            mq.submitted_at,
            COALESCE(NULLIF(TRIM(mq.ea_name), ''), mc.record->>'ea_name') AS ea_name,
            COALESCE(NULLIF(TRIM(mq.lga_name), ''), mc.record->>'lga_name') AS lga_name,
            COALESCE(NULLIF(TRIM(mq.state_name), ''), NULLIF(TRIM(mcd.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), '')) AS state_name,
            mq.region_label,
            COALESCE(mq.region_respondent_ordinal, 1)::int AS region_respondent_ordinal,
            COALESCE(NULLIF(TRIM(mq.selected_panel_labels), ''), sp.selected_panel_labels, 'Omnibus') AS selected_panel_labels,
            COALESCE(ic.open_issue_count, 0)::int AS open_issue_count,
            COALESCE(ic.open_issue_count, 0)::int AS qc_flag_count,
            COALESCE(
                json_agg(
                    json_build_object(
                        'callback_id',           co.callback_id,
                        'attempt_no',            co.attempt_no,
                        'outcome_code',          co.outcome_code,
                        'outcome_note',          co.outcome_note,
                        'sampled_flag',          co.sampled_flag,
                        'assigned_to_user_id',   co.assigned_to_user_id,
                        'assigned_to_username',  COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), ''), co.assigned_to_user_id::text),
                        'completed_at',          co.completed_at,
                        'created_at',            co.created_at
                    ) ORDER BY co.attempt_no ASC
                ) FILTER (WHERE co.callback_id IS NOT NULL),
                '[]'::json
            ) AS callback_history
        FROM mart.main_case_queue mq
        INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
        LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
        LEFT JOIN (
            SELECT
                case_id,
                STRING_AGG(
                    DISTINCT CASE panel_code
                        WHEN 'Panel_1' THEN 'Noodles'
                        WHEN 'Panel_2' THEN 'Toothpaste'
                        WHEN 'Panel_3' THEN 'Edible Oil'
                        WHEN 'Panel_4' THEN 'Bleach'
                        WHEN 'Panel_5' THEN 'Toilet Cleaner'
                        WHEN 'Panel_6' THEN 'Snacks'
                        WHEN 'Panel_7' THEN 'Breakfast Cereals'
                        WHEN 'Panel_8' THEN 'Condiment Mixes'
                        WHEN 'Panel_9' THEN 'Wet Hair'
                        WHEN 'Panel_10' THEN 'Dry Hair'
                        WHEN 'Panel_11' THEN 'Malt'
                        ELSE panel_code
                    END,
                    ', '
                ) AS selected_panel_labels
            FROM clean.main_case_panel
            WHERE COALESCE(is_selected, TRUE)
            GROUP BY case_id
        ) sp ON sp.case_id = mc.case_id
        LEFT JOIN issue_counts ic ON ic.join_key = COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), ''))
        LEFT JOIN qc.callback_outcome co ON {history_join_condition}
        LEFT JOIN app.user_account ua ON ua.user_id = co.assigned_to_user_id
        WHERE (
            mc.is_callback_required = true
            OR EXISTS (
                SELECT 1
                FROM qc.callback_outcome co_hist
                WHERE co_hist.case_id = mc.case_id
                  AND COALESCE(co_hist.outcome_code, 'pending') = 'pending'
            )
        )
        {visibility_clause}
        GROUP BY mq.submission_key, mq.case_id, mq.ea_id, mq.interviewer_id,
                 mq.supervisor_id, mq.approval_stage, mq.start_time, mq.submitted_at,
                 mq.ea_name, mq.lga_name, mq.state_name, mq.region_label, mq.region_respondent_ordinal,
                 mq.selected_panel_labels, mc.record, mcd.state_name, sp.selected_panel_labels, ic.open_issue_count
        ORDER BY mq.submitted_at DESC NULLS LAST
    """

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    return [dict(r) for r in rows]


def create_callback(
    settings: Settings,
    user: AuthUser,
    case_id: str,
    sampled_flag: bool = False,
) -> dict[str, Any]:
    """Assign/claim a case for callback.

    Priority logic:
    1. If a pending callback already exists for this user â†’ return it (idempotent).
    2. If a pending callback is unassigned â†’ claim it by assigning to this user.
    3. If a pending callback is assigned to a different user â†’ 409 Conflict.
    4. No pending callback exists â†’ create a new one assigned to this user.
    """
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT case_id, is_callback_required FROM clean.main_case WHERE case_id = %s",
                (case_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Case not found.")
            if not row["is_callback_required"]:
                raise HTTPException(status_code=400, detail="This case does not require a callback.")

            # Check for an existing pending attempt
            cur.execute(
                """
                SELECT callback_id::text, assigned_to_user_id::text, attempt_no, outcome_code, sampled_flag, created_at
                FROM qc.callback_outcome
                WHERE case_id = %s AND outcome_code = 'pending'
                ORDER BY attempt_no DESC LIMIT 1
                """,
                (case_id,),
            )
            existing = cur.fetchone()

            if existing:
                assigned_id = existing["assigned_to_user_id"]

                if assigned_id and assigned_id != str(user.id):
                    raise HTTPException(
                        status_code=409,
                        detail="This case is already claimed by another reviewer. It is locked in their QC pipeline.",
                    )

                if assigned_id == str(user.id):
                    # Already mine â€” idempotent
                    _sync_main_case_queue_rows(cur, [case_id])
                    conn.commit()
                    _clear_main_case_list_cache()
                    return {**dict(existing), "case_id": case_id}

                # Unassigned pending record â€” claim it
                cur.execute(
                    """
                    UPDATE qc.callback_outcome
                    SET assigned_to_user_id = %s::uuid, updated_at = now()
                    WHERE callback_id = %s
                    RETURNING callback_id::text, case_id, attempt_no, outcome_code, sampled_flag, created_at
                    """,
                    (user.id, existing["callback_id"]),
                )
                claimed = cur.fetchone()
                _sync_main_case_queue_rows(cur, [case_id])
                conn.commit()
                _clear_main_case_list_cache()
                return dict(claimed) if claimed else {}

            # No pending record â€” create new one and claim it
            cur.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no FROM qc.callback_outcome WHERE case_id = %s",
                (case_id,),
            )
            next_no = (cur.fetchone() or {}).get("next_no", 1)

            cur.execute(
                """
                INSERT INTO qc.callback_outcome
                    (case_id, sampled_flag, attempt_no, assigned_to_user_id)
                VALUES (%s, %s, %s, %s::uuid)
                RETURNING callback_id::text, case_id, attempt_no, outcome_code, sampled_flag, created_at
                """,
                (case_id, sampled_flag, next_no, user.id),
            )
            new_row = cur.fetchone()
            _sync_main_case_queue_rows(cur, [case_id])
        conn.commit()

    _clear_main_case_list_cache()
    return dict(new_row) if new_row else {}




def unassign_callback(settings: Settings, user: AuthUser, case_id: str) -> dict[str, Any]:
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission for this action.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            resolved_case_id = _resolve_main_case_id(cur, case_id)
            if not resolved_case_id:
                raise HTTPException(status_code=404, detail="Case not found.")

            cur.execute(
                """
                DELETE FROM qc.callback_outcome co
                USING clean.main_case mc
                WHERE co.case_id = %s
                  AND mc.case_id = co.case_id
                  AND COALESCE(co.outcome_code, 'pending') = 'pending'
                  AND lower(COALESCE(mc.approval_stage, '')) IN ('pending_review', 'in_review')
                RETURNING co.callback_id::text
                """,
                (resolved_case_id,),
            )
            rows = cur.fetchall()
            cur.execute(
                """
                UPDATE clean.main_case
                SET is_callback_required = false,
                    updated_at = NOW()
                WHERE case_id = %s
                  AND lower(COALESCE(approval_stage, '')) IN ('pending_review', 'in_review')
                """,
                (resolved_case_id,),
            )
            _sync_main_case_queue_rows(cur, [resolved_case_id])
            conn.commit()

    _clear_main_case_list_cache()
    return {"case_id": resolved_case_id, "unassigned": len(rows)}


def unassign_audio_review(settings: Settings, user: AuthUser, audio_id: str) -> dict[str, Any]:
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission for this action.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM clean.audio_listening al
                USING clean.main_case mc
                WHERE al.audio_id = %s::uuid
                  AND mc.case_id = al.case_id
                  AND lower(COALESCE(mc.approval_stage, '')) IN ('pending_review', 'in_review')
                RETURNING al.audio_id::text, al.case_id
                """,
                (audio_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Audio review assignment not found.")
            _sync_main_case_queue_rows(cur, [row["case_id"]])
            conn.commit()

    _clear_main_case_list_cache()
    return {"audio_id": row["audio_id"], "case_id": row["case_id"], "status": "pending", "unassigned": True}



def bulk_unassign_callbacks(settings: Settings, user: AuthUser, submission_keys: list[str]) -> dict[str, Any]:
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission for this action.")
    if not submission_keys:
        raise HTTPException(status_code=400, detail="No submission keys provided.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH selected_cases AS (
                    SELECT case_id, submission_key
                    FROM clean.main_case
                    WHERE submission_key = ANY(%s::text[])
                ), updated_cases AS (
                    UPDATE clean.main_case mc
                    SET is_callback_required = false,
                        updated_at = NOW()
                    FROM selected_cases sc
                    WHERE mc.case_id = sc.case_id
                      AND lower(COALESCE(mc.approval_stage, '')) IN ('pending_review', 'in_review')
                    RETURNING sc.submission_key
                ), updated AS (
                    DELETE FROM qc.callback_outcome co
                    USING selected_cases sc, clean.main_case mc
                    WHERE co.case_id = sc.case_id
                      AND mc.case_id = sc.case_id
                      AND COALESCE(co.outcome_code, 'pending') = 'pending'
                      AND lower(COALESCE(mc.approval_stage, '')) IN ('pending_review', 'in_review')
                    RETURNING sc.submission_key
                )
                SELECT
                    (SELECT COUNT(*)::int FROM updated) AS unassigned_count,
                    ARRAY(SELECT DISTINCT submission_key FROM updated) AS unassigned_submission_keys
                """,
                (submission_keys,),
            )
            row = cur.fetchone() or {}
            cur.execute("SELECT case_id FROM clean.main_case WHERE submission_key = ANY(%s::text[])", (submission_keys,))
            _sync_main_case_queue_rows(cur, [case_row["case_id"] for case_row in cur.fetchall()])
            conn.commit()

    _clear_main_case_list_cache()
    return {
        "unassigned": int(row.get("unassigned_count") or 0),
        "submissionKeys": list(row.get("unassigned_submission_keys") or []),
    }


def bulk_unassign_audio_reviews(settings: Settings, user: AuthUser, submission_keys: list[str]) -> dict[str, Any]:
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission for this action.")
    if not submission_keys:
        raise HTTPException(status_code=400, detail="No submission keys provided.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH selected_cases AS (
                    SELECT case_id, submission_key
                    FROM clean.main_case
                    WHERE submission_key = ANY(%s::text[])
                ), updated AS (
                    DELETE FROM clean.audio_listening al
                    USING selected_cases sc, clean.main_case mc
                    WHERE al.case_id = sc.case_id
                      AND mc.case_id = sc.case_id
                      AND COALESCE(al.status, 'pending') = 'pending'
                      AND lower(COALESCE(mc.approval_stage, '')) IN ('pending_review', 'in_review')
                    RETURNING sc.submission_key
                )
                SELECT
                    (SELECT COUNT(*)::int FROM updated) AS unassigned_count,
                    ARRAY(SELECT DISTINCT submission_key FROM updated) AS unassigned_submission_keys
                """,
                (submission_keys,),
            )
            row = cur.fetchone() or {}
            cur.execute("SELECT case_id FROM clean.main_case WHERE submission_key = ANY(%s::text[])", (submission_keys,))
            _sync_main_case_queue_rows(cur, [case_row["case_id"] for case_row in cur.fetchall()])
            conn.commit()

    _clear_main_case_list_cache()
    return {
        "unassigned": int(row.get("unassigned_count") or 0),
        "submissionKeys": list(row.get("unassigned_submission_keys") or []),
    }

def record_callback_outcome(
    settings: Settings,
    user: AuthUser,
    callback_id: str,
    outcome_code: str,
    outcome_note: str | None = None,
) -> dict[str, Any]:
    if outcome_code not in CALLBACK_OUTCOME_CODES:
        raise HTTPException(
            status_code=400,
            detail=f"outcome_code must be one of: {', '.join(CALLBACK_OUTCOME_CODES)}",
        )

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT callback_id, case_id FROM qc.callback_outcome WHERE callback_id = %s",
                (callback_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Callback record not found.")

            # Verify the current user is the assigned reviewer. Platform managers may override.
            cur.execute(
                "SELECT assigned_to_user_id::text FROM qc.callback_outcome WHERE callback_id = %s",
                (callback_id,),
            )
            lock_row = cur.fetchone()
            if lock_row:
                assigned_id = lock_row["assigned_to_user_id"]
                manager_roles = {"SUPERADMIN", "PDM-ADMIN"}
                user_role = str(user.role or "").strip().upper()
                if assigned_id and assigned_id != str(user.id) and user_role not in manager_roles:
                    raise HTTPException(
                        status_code=409,
                        detail="This callback is assigned to another reviewer. Only SUPERADMIN or PDM-ADMIN can override.",
                    )

            completed_at = "now()" if outcome_code != "pending" else "NULL"
            cur.execute(
                f"""
                UPDATE qc.callback_outcome
                SET outcome_code = %s,
                    outcome_note = %s,
                    completed_by_user_id = %s::uuid,
                    completed_at = {completed_at},
                    updated_at = now()
                WHERE callback_id = %s
                RETURNING callback_id::text, case_id, outcome_code, outcome_note, completed_at, updated_at
                """,
                (outcome_code, outcome_note, user.id, callback_id),
            )
            updated = cur.fetchone()
            if updated:
                _sync_main_case_queue_rows(cur, [updated["case_id"]])

        conn.commit()

    _clear_main_case_list_cache()
    return dict(updated) if updated else {}


# ---------------------------------------------------------------------------
# Callback detail
# ---------------------------------------------------------------------------

AUDIO_AUDIT_FIELDS = [
    "QF1_audio_audit",
    "BAA1_audio_audit",
    "MF1_audio_audit",
    "NB1_audio_audit",
    "M1a_audio_audit",
    "SA1_audio_audit",
    "Q1_audio_audit",
]


@lru_cache(maxsize=4)
def _audio_variable_label_map(root_dir: str) -> dict[str, str]:
    labels: dict[str, str] = {}

    def usable_audio_label(label: str, variable: str) -> str:
        cleaned = _clean_label_text(label).strip()
        if not cleaned:
            return ""
        if re.fullmatch(r"(?i)silent\s+recording\d*", cleaned):
            return ""
        if cleaned.lower() == variable.strip().lower():
            return ""
        return cleaned

    try:
        _, dictionary_by_section = _load_dictionary(root_dir)
        for rows in dictionary_by_section.values():
            for row in rows:
                variable = str(row.get("variable") or "").strip()
                if not variable:
                    continue
                label = usable_audio_label(str(row.get("label") or ""), variable)
                if label:
                    labels[variable] = label
                    labels[f"audio_audit_{variable}"] = label
    except Exception:
        logger.info("Main dictionary not available for audio labels; falling back to monthly XLSForm.")

    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    dictionary_files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if dictionary_files:
        try:
            survey_df = pd.read_excel(dictionary_files[0], sheet_name="survey").fillna("")
            if {"name", "label"}.issubset(set(survey_df.columns)):
                for row in survey_df.to_dict(orient="records"):
                    variable = str(row.get("name") or "").strip()
                    label = usable_audio_label(str(row.get("label") or ""), variable)
                    if variable and label:
                        labels[variable] = label
                        labels[f"audio_audit_{variable}"] = label
        except Exception:
            logger.warning("Unable to load XLSForm audio labels from %s.", dictionary_files[0], exc_info=True)
    return labels


def _clean_audio_filename(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/").split("/")[-1]
    if "File skipped from exports:" in raw:
        raw = re.sub(r"(?i)^File skipped from exports:\s*", "", raw).strip()
    return raw.split("/")[-1].split("\\")[-1] or None


def _media_proxy_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return f"/api/main-survey/media-proxy/{quote(raw, safe='')}"


def _panel_label_sql(alias: str = "panel_code") -> str:
    parts = []
    for code, label in BHT_PANEL_LABEL_BY_CODE.items():
        safe_label = label.replace("'", "''")
        parts.append(f"WHEN '{code}' THEN '{safe_label}'")
    cases = " ".join(parts)
    return f"CASE {alias} {cases} ELSE {alias} END"


@lru_cache(maxsize=4)
def _city_choice_label_map(root_dir: str) -> dict[str, str]:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    dictionary_files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not dictionary_files:
        return {}
    try:
        choices_df = pd.read_excel(dictionary_files[0], sheet_name="choices").fillna("")
    except Exception:
        return {}
    labels: dict[str, str] = {}
    if {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        for row in choices_df.to_dict(orient="records"):
            if str(row.get("list_name") or "").strip().lower() not in {"city", "city_1", "cities"}:
                continue
            raw_code = str(row.get("name") or "").strip()
            label = _clean_label_text(str(row.get("label") or "")).strip()
            if not raw_code or not label:
                continue
            labels[raw_code] = label
            if raw_code.endswith(".0"):
                labels[raw_code[:-2]] = label
    return labels


def _city_label_sql(record_alias: str, root_dir: str) -> str:
    raw_expr = f"{record_alias}->>'City_1'"
    parts = []
    for code, label in _city_choice_label_map(root_dir).items():
        safe_code = code.replace("'", "''")
        safe_label = label.replace("'", "''")
        parts.append(f"WHEN '{safe_code}' THEN '{safe_label}'")
    if not parts:
        return raw_expr
    return f"CASE NULLIF(TRIM({raw_expr}), '') {' '.join(parts)} ELSE {raw_expr} END"


def _sector_label_sql(record_alias: str) -> str:
    raw_expr = f"{record_alias}->>'Sector'"
    parts = []
    for code, label in MAIN_SECTOR_LABELS.items():
        safe_code = str(code).replace("'", "''")
        safe_label = str(label).replace("'", "''")
        parts.append(f"WHEN '{safe_code}' THEN '{safe_label}'")
        parts.append(f"WHEN '{safe_code}.0' THEN '{safe_label}'")
    return f"CASE NULLIF(TRIM({raw_expr}), '') {' '.join(parts)} ELSE {raw_expr} END"


@lru_cache(maxsize=8)
def _choice_label_map_from_xlsform(root_dir: str, variable_name: str) -> dict[str, str]:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    dictionary_files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not dictionary_files:
        return {}
    try:
        survey_df = pd.read_excel(dictionary_files[0], sheet_name="survey").fillna("")
        choices_df = pd.read_excel(dictionary_files[0], sheet_name="choices").fillna("")
    except Exception:
        return {}
    if not {"name", "type"}.issubset(set(str(col) for col in survey_df.columns)) or not {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        return {}
    list_name = ""
    for row in survey_df.to_dict(orient="records"):
        if str(row.get("name") or "").strip() != variable_name:
            continue
        qtype = str(row.get("type") or "").strip()
        list_name = qtype.split(None, 1)[1].strip() if " " in qtype else ""
        break
    if not list_name:
        return {}
    labels: dict[str, str] = {}
    for row in choices_df.to_dict(orient="records"):
        if str(row.get("list_name") or "").strip() != list_name:
            continue
        code = str(row.get("name") or "").strip()
        label = _clean_label_text(str(row.get("label") or "")).strip()
        if not code or not label:
            continue
        labels[code] = label
        if code.endswith(".0"):
            labels[code[:-2]] = label
        else:
            labels[f"{code}.0"] = label
    return labels


@lru_cache(maxsize=4)
def _bau_snapshot_dictionary(root_dir: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    dictionary_files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not dictionary_files:
        return {}, {}

    try:
        survey_df = pd.read_excel(dictionary_files[0], sheet_name="survey").fillna("")
        choices_df = pd.read_excel(dictionary_files[0], sheet_name="choices").fillna("")
    except Exception:
        logger.warning("Unable to load XLSForm labels for BAU snapshot from %s.", dictionary_files[0], exc_info=True)
        return {}, {}

    choices_by_list: dict[str, dict[str, str]] = defaultdict(dict)
    if {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        for choice in choices_df.to_dict(orient="records"):
            list_name = str(choice.get("list_name") or "").strip()
            code = str(choice.get("name") or "").strip()
            label = _clean_label_text(str(choice.get("label") or "")).strip()
            if not list_name or not code or not label:
                continue
            choices_by_list[list_name][code] = label
            if code.endswith(".0"):
                choices_by_list[list_name][code[:-2]] = label
            elif "." not in code and code.lstrip("-").isdigit():
                choices_by_list[list_name][f"{code}.0"] = label

    question_labels: dict[str, str] = {}
    answer_labels: dict[str, dict[str, str]] = {}
    if {"type", "name", "label"}.issubset(set(str(col) for col in survey_df.columns)):
        for row in survey_df.to_dict(orient="records"):
            qtype = str(row.get("type") or "").strip()
            variable = str(row.get("name") or "").strip()
            if not variable:
                continue
            label = _clean_label_text(str(row.get("label") or "")).strip()
            if label:
                question_labels[variable] = re.sub(rf"^{re.escape(variable)}\.?\s*", "", label, flags=re.IGNORECASE).strip() or label

            low_type = qtype.lower()
            if low_type.startswith("select_one ") or low_type.startswith("select_multiple "):
                list_name = qtype.split(None, 1)[1].strip() if " " in qtype else ""
                labels = choices_by_list.get(list_name, {})
                if labels:
                    answer_labels[variable] = labels

    return question_labels, answer_labels


def _bau_snapshot_answer_label(record: dict[str, Any], variable: str, answer_labels: dict[str, dict[str, str]]) -> str:
    record_key_by_lower = {str(key).lower(): key for key in record.keys()}
    canonical_variable = str(record_key_by_lower.get(variable.lower()) or variable)
    raw = str(record.get(canonical_variable) or "").strip()
    labels = answer_labels.get(variable, {})
    if not labels:
        return _clean_label_text(raw) or raw

    values: list[str] = []
    if raw:
        for token in [part for part in re.split(r"[\s,;|]+", raw) if part]:
            mapped = _apply_label(token, labels)
            cleaned = _clean_label_text(mapped) or token
            if cleaned and cleaned not in values:
                values.append(cleaned)

    for key, selected in record.items():
        match = re.match(rf"^{re.escape(canonical_variable)}_(.+)$", str(key), flags=re.IGNORECASE)
        if not match:
            continue
        selected_text = str(selected or "").strip().lower()
        if selected_text not in {"1", "1.0", "true", "yes"}:
            continue
        code = match.group(1)
        mapped = _apply_label(code, labels)
        cleaned = _clean_label_text(mapped) or code
        if cleaned and cleaned not in values:
            values.append(cleaned)

    other_key = str(record_key_by_lower.get(f"{variable}_OTH".lower()) or f"{canonical_variable}_OTH")
    other_value = str(record.get(other_key) or "").strip()
    if other_value:
        cleaned_other = _clean_label_text(other_value) or other_value
        if cleaned_other not in values:
            values.append(cleaned_other)

    return ", ".join(values)


def _selected_panel_bau_snapshot(root_dir: str, record: dict[str, Any], selected_panel_codes: list[str]) -> list[dict[str, Any]]:
    labels = _audio_variable_label_map(root_dir)
    question_labels, answer_labels = _bau_snapshot_dictionary(root_dir)
    panel_to_slug = {
        str(meta["panelCode"]): slug
        for slug, meta in BHT_CATEGORY_PANEL_MAP.items()
        if meta.get("panelCode")
    }
    rows: list[dict[str, Any]] = []
    for panel_code in selected_panel_codes:
        slug = panel_to_slug.get(panel_code)
        if not slug:
            continue
        prefix = BHT_CATEGORY_BAU5A_PREFIX.get(slug)
        if not prefix:
            continue
        panel_label = BHT_CATEGORY_PANEL_MAP[slug]["label"]
        variables = [f"{prefix}_BAU1a", f"{prefix}_BAU5a"]
        seen: set[str] = set()
        for variable in variables:
            if variable in seen:
                continue
            seen.add(variable)
            value = _bau_snapshot_answer_label(record, variable, answer_labels)
            if not value:
                continue
            rows.append(
                {
                    "panelCode": panel_code,
                    "panelLabel": panel_label,
                    "variableName": variable,
                    "variableLabel": question_labels.get(variable) or labels.get(variable) or variable,
                    "value": value,
                }
            )
    return rows


def _load_case_audio_files(cur: Any, settings: Settings, case_id: str) -> tuple[dict[str, str | None], list[dict[str, Any]]]:
    labels = _audio_variable_label_map(str(settings.root_dir))
    cur.execute(
        """
        SELECT variable_name, file_name, surveycto_path
        FROM clean.main_case_media
        WHERE case_id = %s
          AND media_type = 'audio'
          AND NULLIF(TRIM(COALESCE(file_name, surveycto_path, '')), '') IS NOT NULL
        ORDER BY
          CASE
            WHEN variable_name = 'audio_audit_Non_compete' THEN 0
            WHEN variable_name ILIKE 'audio_audit_%%_QC%%' THEN 1
            ELSE 2
          END,
          variable_name
        """,
        (case_id,),
    )
    items: list[dict[str, Any]] = []
    audio_files: dict[str, str | None] = {}
    for row in cur.fetchall():
        variable_name = str(row.get("variable_name") or "").strip()
        media_ref = str(row.get("surveycto_path") or row.get("file_name") or "").strip()
        file_name = str(row.get("file_name") or "").strip() or _clean_audio_filename(media_ref)
        if not variable_name or not media_ref:
            continue
        label = labels.get(variable_name) or labels.get(variable_name.replace("audio_audit_", "", 1)) or variable_name
        audio_files[variable_name] = media_ref
        items.append(
            {
                "variable_name": variable_name,
                "label": label,
                "file_name": file_name,
                "media_url": _media_proxy_url(media_ref),
            }
        )
    return audio_files, items


def _extract_audio_files_from_sections(section_rows: list[dict[str, Any]]) -> dict[str, str | None]:
    audio_files: dict[str, str | None] = {f: None for f in AUDIO_AUDIT_FIELDS}
    for sec in section_rows:
        rec = sec.get("record") or {}
        if not isinstance(rec, dict):
            continue
        sec_name = str(sec.get("section_name") or "").upper()
        if "QUALITY CONTROL" not in sec_name and "QUALITY_CONTROL" not in sec_name:
            continue
        for field in AUDIO_AUDIT_FIELDS:
            val = rec.get(field)
            if val not in {None, ""}:
                audio_files[field] = str(val)
    return audio_files


def _pick_primary_audio_url(audio_files: dict[str, str | None]) -> str | None:
    for field in AUDIO_AUDIT_FIELDS:
        raw = audio_files.get(field)
        if not raw:
            continue
        value = str(raw).strip()
        if not value:
            continue
        # Leave raw value intact; many deployments already store full URLs here.
        return value
    return None


def _meaningful_for_verification(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text) and text not in {"nan", "none", "nat", "0", "0.0", "false", "no"}


def _is_multiselect_child(variable_name: str, variable_set: set[str]) -> bool:
    match = re.match(r"^(.+)_\d+$", variable_name)
    if not match:
        return False
    return match.group(1) in variable_set


def _is_grid_verification_question(variable_name: str, question_label: str) -> bool:
    if re.match(r"^.+\.\d+$", variable_name):
        return True

    cleaned_label = _clean_label_text(question_label)
    return bool(re.match(r"^\d+\.\s+", cleaned_label))


def _has_complete_callback_question_text(variable_name: str, question_label: str) -> bool:
    cleaned_label = _clean_label_text(question_label)
    normalized_label = cleaned_label.strip().lower()
    normalized_variable = variable_name.strip().lower()

    if not normalized_label or normalized_label == normalized_variable:
        return False
    if "${" in cleaned_label or "}" in cleaned_label:
        return False
    if cleaned_label.rstrip().endswith(("...", "…")):
        return False
    if re.search(r"\b(which of|which one of|select all that|choose all that)\s*$", normalized_label):
        return False

    return len(re.findall(r"[A-Za-z0-9]+", cleaned_label)) >= 4


def _latest_monthly_xlsform_files(root_dir: str) -> list[Path]:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    return sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _selected_panel_prefixes(selected_panel_codes: list[str]) -> set[str]:
    panel_to_slug = {
        str(meta["panelCode"]): slug
        for slug, meta in BHT_CATEGORY_PANEL_MAP.items()
        if meta.get("panelCode")
    }
    prefixes: set[str] = set()
    for panel_code in selected_panel_codes:
        slug = panel_to_slug.get(str(panel_code))
        prefix = BHT_CATEGORY_BAU5A_PREFIX.get(str(slug or ""))
        if prefix:
            prefixes.add(prefix.upper())
    return prefixes


def _is_omnibus_verification_candidate(section_name: str, variable_name: str) -> bool:
    normalized_section = section_name.strip().lower()
    upper_variable = variable_name.strip().upper()
    return any(token in normalized_section for token in OMNIBUS_VERIFICATION_SECTION_TOKENS) or any(
        upper_variable.startswith(prefix) for prefix in OMNIBUS_VERIFICATION_PREFIXES
    )


def _is_selected_panel_verification_candidate(
    section_name: str,
    variable_name: str,
    selected_panel_codes: list[str],
    selected_prefixes: set[str],
) -> bool:
    upper_variable = variable_name.strip().upper()
    if any(upper_variable.startswith(f"{prefix}_") for prefix in selected_prefixes):
        return True

    normalized_section = section_name.strip().lower()
    for panel_code in selected_panel_codes:
        if normalized_section in PANEL_VERIFICATION_SECTION_LABELS.get(str(panel_code), set()):
            return True
    return False


def _is_allowed_callback_verification_candidate(
    section_name: str,
    variable_name: str,
    selected_panel_codes: list[str],
    selected_prefixes: set[str],
) -> bool:
    return _is_omnibus_verification_candidate(section_name, variable_name) or _is_selected_panel_verification_candidate(
        section_name,
        variable_name,
        selected_panel_codes,
        selected_prefixes,
    )



def _is_single_response_or_numeric_question(variable_name: str, dictionary_row: dict[str, Any], section_rows: list[dict[str, Any]]) -> bool:
    storage_type = str(dictionary_row.get("storageType") or "").strip().lower()
    value_labels = str(dictionary_row.get("valueLabels") or "").strip()
    question_type = str(dictionary_row.get("questionType") or "").strip().lower()
    question_label = str(dictionary_row.get("label") or "")

    if _is_grid_verification_question(variable_name, question_label):
        return False
    if not _has_complete_callback_question_text(variable_name, question_label):
        return False

    if question_type and not question_type.startswith("select_one"):
        return False
    if not question_type and (not value_labels or storage_type == "numeric"):
        return False
    for row in section_rows:
        candidate = str(row.get("variable") or "")
        if re.match(rf"^{re.escape(variable_name)}_\d+$", candidate):
            return False
    return True


def _load_monthly_xlsform_verification_dictionary(root_dir: str) -> dict[str, list[dict[str, str]]]:
    dictionary_files = _latest_monthly_xlsform_files(root_dir)
    if not dictionary_files:
        return {}

    survey_df = pd.read_excel(dictionary_files[0], sheet_name="survey").fillna("")
    choices_df = pd.read_excel(dictionary_files[0], sheet_name="choices").fillna("")
    required = {"type", "name", "label"}
    if not required.issubset(set(str(col) for col in survey_df.columns)):
        return {}

    choices_by_list: dict[str, dict[str, str]] = defaultdict(dict)
    if {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        for choice in choices_df.to_dict(orient="records"):
            list_name = str(choice.get("list_name") or "").strip()
            value = str(choice.get("name") or "").strip()
            label = _clean_label_text(str(choice.get("label") or "")).strip()
            if list_name and value and label:
                choices_by_list[list_name][value] = label

    by_section: dict[str, list[dict[str, str]]] = defaultdict(list)
    section_stack: list[str] = ["Omnibus"]
    for row in survey_df.to_dict(orient="records"):
        qtype = str(row.get("type") or "").strip()
        variable = str(row.get("name") or "").strip()
        label = _clean_label_text(str(row.get("label") or "")).strip()
        low_type = qtype.lower()

        if low_type.startswith("begin group") or low_type.startswith("begin repeat"):
            section_stack.append(label or variable or section_stack[-1])
            continue
        if low_type.startswith("end group") or low_type.startswith("end repeat"):
            if len(section_stack) > 1:
                section_stack.pop()
            continue
        if not low_type.startswith("select_one ") or not variable:
            continue

        list_name = qtype.split(None, 1)[1].strip() if " " in qtype else ""
        by_section[section_stack[-1]].append(
            {
                "variable": variable,
                "label": label or variable,
                "storageType": "select_one",
                "measure": "nominal",
                "valueLabels": "|".join(f"{code}={choice_label}" for code, choice_label in choices_by_list.get(list_name, {}).items()),
                "questionType": qtype,
            }
        )
    return dict(by_section)

def _multiselect_answer_label(
    record: dict[str, Any],
    parent_variable: str,
    section_rows: list[dict[str, str]],
) -> str:
    children = [
        row for row in section_rows
        if re.match(rf"^{re.escape(parent_variable)}_\d+$", str(row.get("variable") or ""))
    ]
    if not children:
        return ""
    selected_labels: list[str] = []
    for child in children:
        raw = str(record.get(child["variable"]) or "").strip().lower()
        if raw in {"1", "1.0", "true", "yes"}:
            label = _clean_label_text(child.get("label") or child["variable"]) or child["variable"]
            selected_labels.append(label)
    return " / ".join(selected_labels)


def _answer_label_for_variable(
    record: dict[str, Any],
    variable_name: str,
    section_rows: list[dict[str, str]],
    label_maps: dict[str, dict[str, str]],
) -> str:
    multi_answer = _multiselect_answer_label(record, variable_name, section_rows)
    if multi_answer:
        return multi_answer

    raw = str(record.get(variable_name) or "").strip()
    if not raw:
        return ""
    label_map = label_maps.get(variable_name)
    tokens = [token for token in re.split(r"[\s,;|]+", raw) if token]
    if label_map and len(tokens) > 1:
        mapped = [_apply_label(token, label_map) for token in tokens]
        cleaned = [_clean_label_text(item) for item in mapped if _clean_label_text(item)]
        return " / ".join(cleaned)
    mapped = _apply_label(raw, label_map) if label_map else raw
    return _clean_label_text(mapped) or raw


def _inline_value_label_map(dictionary_row: dict[str, Any]) -> dict[str, str]:
    raw = str(dictionary_row.get("valueLabels") or "").strip()
    labels: dict[str, str] = {}
    if not raw:
        return labels
    for part in raw.split("|"):
        if "=" not in part:
            continue
        code, label = part.split("=", 1)
        code = code.strip()
        label = _clean_label_text(label).strip()
        if code and label:
            labels[code] = label
    return labels


def get_or_create_verification_questions(
    settings: Settings,
    user: AuthUser,
    case_id: str,
    mode: str = "qc",
) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT position, section_name, variable_name, question_label,
                       respondent_answer_label, callback_answer, is_correct, verified_at
                FROM qc.callback_verification_question
                WHERE case_id = %s
                ORDER BY position
                """,
                (case_id,),
            )
            existing = [dict(row) for row in cur.fetchall()]
            if existing and any(row.get("is_correct") is not None for row in existing):
                return {"questions": existing, "mode": mode, "fallback": False}
            if existing:
                cur.execute("DELETE FROM qc.callback_verification_question WHERE case_id = %s AND is_correct IS NULL", (case_id,))
                conn.commit()

            cur.execute(
                """
                SELECT section_name, record
                FROM clean.main_case_section
                WHERE case_id = %s
                """,
                (case_id,),
            )
            case_sections = [dict(row) for row in cur.fetchall()]

            try:
                _, dictionary_by_section = _load_dictionary(str(settings.root_dir))
            except Exception:
                dictionary_by_section = _load_monthly_xlsform_verification_dictionary(str(settings.root_dir))
            label_maps = _load_all_value_label_maps(str(settings.root_dir))
            if not case_sections:
                cur.execute(
                    """
                    SELECT record
                    FROM clean.main_case
                    WHERE case_id = %s OR submission_key = %s
                    LIMIT 1
                    """,
                    (case_id, case_id),
                )
                case_row = cur.fetchone()
                case_record = dict(case_row["record"] or {}) if case_row else {}
                case_sections = [
                    {"section_name": section_name, "record": case_record}
                    for section_name in dictionary_by_section
                ]

            cur.execute(
                """
                SELECT panel_code
                FROM clean.main_case_panel
                WHERE case_id = %s
                  AND COALESCE(is_selected, TRUE)
                ORDER BY panel_code
                """,
                (case_id,),
            )
            selected_panel_codes = [str(row["panel_code"]) for row in cur.fetchall() if row.get("panel_code")]
            selected_prefixes = _selected_panel_prefixes(selected_panel_codes)
            merged_record: dict[str, Any] = {}
            for section in case_sections:
                record = section.get("record") or {}
                if isinstance(record, dict):
                    merged_record.update(record)

            section_pool: dict[str, list[dict[str, Any]]] = {}

            for section_name, section_rows in dictionary_by_section.items():
                section_rows = dictionary_by_section.get(section_name, [])
                if not section_rows:
                    continue
                variable_set = {str(row.get("variable") or "") for row in section_rows if row.get("variable")}
                pool_rows = section_pool.setdefault(section_name, [])
                seen_variables = {item["variable_name"] for item in pool_rows}

                for dictionary_row in section_rows:
                    variable = str(dictionary_row.get("variable") or "")
                    if not variable or variable in seen_variables:
                        continue
                    if _is_helper_variable(variable):
                        continue
                    if _is_multiselect_child(variable, variable_set):
                        continue
                    if not _is_allowed_callback_verification_candidate(section_name, variable, selected_panel_codes, selected_prefixes):
                        continue
                    if not _is_single_response_or_numeric_question(variable, dictionary_row, section_rows):
                        continue

                    if not _meaningful_for_verification(merged_record.get(variable)):
                        continue

                    local_label_maps = label_maps
                    inline_labels = _inline_value_label_map(dictionary_row)
                    if inline_labels and variable not in label_maps:
                        local_label_maps = {**label_maps, variable: inline_labels}
                    answer_label = _answer_label_for_variable(merged_record, variable, section_rows, local_label_maps)
                    if not _meaningful_for_verification(answer_label):
                        continue

                    pool_rows.append(
                        {
                            "section_name": section_name,
                            "variable_name": variable,
                            "question_label": _clean_label_text(str(dictionary_row.get("label") or variable)) or variable,
                            "respondent_answer_label": answer_label,
                        }
                    )
                    seen_variables.add(variable)

            section_names = [name for name, rows in section_pool.items() if rows]
            random.shuffle(section_names)
            for section_name in section_names:
                random.shuffle(section_pool[section_name])

            selected: list[dict[str, Any]] = []
            index_by_section = {name: 0 for name in section_names}
            limit = 4 if mode == "random" else 5
            while len(selected) < limit:
                progressed = False
                for section_name in section_names:
                    rows = section_pool.get(section_name, [])
                    idx = index_by_section.get(section_name, 0)
                    if idx >= len(rows):
                        continue
                    selected.append(rows[idx])
                    index_by_section[section_name] = idx + 1
                    progressed = True
                    if len(selected) >= limit:
                        break
                if not progressed:
                    break

            if not selected:
                dictionary_rows = [
                    (section_name, row, rows)
                    for section_name, rows in dictionary_by_section.items()
                    for row in rows
                    if _is_allowed_callback_verification_candidate(
                        section_name,
                        str(row.get("variable") or ""),
                        selected_panel_codes,
                        selected_prefixes,
                    )
                ]
                random.shuffle(dictionary_rows)
                for section_name, dictionary_row, section_rows_for_dict in dictionary_rows:
                    variable = str(dictionary_row.get("variable") or "")
                    if not variable or variable not in merged_record:
                        continue
                    if _is_helper_variable(variable):
                        continue
                    variable_set = {str(row.get("variable") or "") for row in section_rows_for_dict if row.get("variable")}
                    if _is_multiselect_child(variable, variable_set):
                        continue
                    if not _is_single_response_or_numeric_question(variable, dictionary_row, section_rows_for_dict):
                        continue
                    if not _meaningful_for_verification(merged_record.get(variable)):
                        continue
                    local_label_maps = label_maps
                    inline_labels = _inline_value_label_map(dictionary_row)
                    if inline_labels and variable not in label_maps:
                        local_label_maps = {**label_maps, variable: inline_labels}
                    answer_label = _answer_label_for_variable(merged_record, variable, section_rows_for_dict, local_label_maps)
                    if not _meaningful_for_verification(answer_label):
                        continue
                    selected.append(
                        {
                            "section_name": section_name,
                            "variable_name": variable,
                            "question_label": _clean_label_text(str(dictionary_row.get("label") or variable)) or variable,
                            "respondent_answer_label": answer_label,
                        }
                    )
                    if len(selected) >= limit:
                        break

            for position, item in enumerate(selected[:limit], start=1):
                cur.execute(
                    """
                    INSERT INTO qc.callback_verification_question (
                        case_id, position, section_name, variable_name, question_label, respondent_answer_label
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (case_id, position) DO NOTHING
                    """,
                    (
                        case_id,
                        position,
                        item["section_name"],
                        item["variable_name"],
                        item["question_label"],
                        item["respondent_answer_label"],
                    ),
                )
            conn.commit()

            cur.execute(
                """
                SELECT position, section_name, variable_name, question_label,
                       respondent_answer_label, callback_answer, is_correct, verified_at
                FROM qc.callback_verification_question
                WHERE case_id = %s
                ORDER BY position
                """,
                (case_id,),
            )
            return {"questions": [dict(row) for row in cur.fetchall()], "mode": mode, "fallback": False}


def save_verification_response(
    settings: Settings,
    user: AuthUser,
    case_id: str,
    position: int,
    callback_answer: str,
    is_correct: bool,
) -> dict[str, Any]:
    if position < 1 or position > 5:
        raise HTTPException(status_code=400, detail="Position must be between 1 and 5.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE qc.callback_verification_question
                SET callback_answer = %s,
                    is_correct = %s,
                    verified_at = now()
                WHERE case_id = %s AND position = %s
                RETURNING position, section_name, variable_name, question_label,
                          respondent_answer_label, callback_answer, is_correct, verified_at
                """,
                (callback_answer, is_correct, case_id, position),
            )
            updated = cur.fetchone()
            if not updated:
                raise HTTPException(status_code=404, detail="Verification question not found.")
            conn.commit()
            return dict(updated)




def get_callback_case_detail(
    settings: Settings,
    user: AuthUser,
    case_id: str,
) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            panel_label_expr = _panel_label_sql("mcp.panel_code")
            city_label_expr = _city_label_sql("mc_all.record", str(settings.root_dir))
            cur.execute(
                f"""
                WITH all_case_ordinals AS (
                    SELECT
                        ranked.case_id,
                        ranked.region_label,
                        ranked.region_respondent_ordinal
                    FROM (
                        SELECT
                            mc_all.case_id,
                            COALESCE(
                                NULLIF(TRIM({city_label_expr}), ''),
                                NULLIF(TRIM(mcd_all.state_name), ''),
                                NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                                NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                                'Region'
                            ) AS region_label,
                            ROW_NUMBER() OVER (
                                PARTITION BY COALESCE(
                                    NULLIF(TRIM({city_label_expr}), ''),
                                    NULLIF(TRIM(mcd_all.state_name), ''),
                                    NULLIF(TRIM(mc_all.record->>'state_name'), ''),
                                    NULLIF(TRIM(mc_all.record->>'lga_name'), ''),
                                    'Region'
                                )
                                ORDER BY mc_all.submitted_at ASC NULLS LAST, mc_all.created_at ASC NULLS LAST, mc_all.case_id ASC
                            )::int AS region_respondent_ordinal
                        FROM clean.main_case mc_all
                        LEFT JOIN mart.main_case_dim mcd_all ON mcd_all.case_id = mc_all.case_id
                    ) ranked
                ),
                selected_panels AS (
                    SELECT
                        mcp.case_id,
                        STRING_AGG(DISTINCT {panel_label_expr}, ', ') AS selected_panel_labels
                    FROM clean.main_case_panel mcp
                    WHERE COALESCE(mcp.is_selected, TRUE)
                    GROUP BY mcp.case_id
                )
                SELECT
                       mq.submission_key,
                       mq.case_id,
                       mq.ea_id,
                       COALESCE(NULLIF(TRIM(mq.ea_name), ''), mc.record->>'ea_name') AS ea_name,
                       COALESCE(NULLIF(TRIM(mc.record->>'Sector'), ''), NULLIF(TRIM(mq.lga_name), ''), NULLIF(TRIM(mc.record->>'lga_name'), '')) AS lga_name,
                       COALESCE(NULLIF(TRIM(mq.state_name), ''), NULLIF(TRIM(mcd.state_name), ''), NULLIF(TRIM(mc.record->>'state_name'), '')) AS state_name,
                       COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), '')) AS supacc_confirm,
                       COALESCE(NULLIF(TRIM(mc.record->>'Take_pictures'), ''), NULLIF(TRIM(mc.record->>'sup_photo'), '')) AS sup_photo,
                       NULLIF(TRIM(mc.record->>'Mobile'), '') AS phone_mobile,
                       mc.record,
                       mq.interviewer_id,
                       mq.supervisor_id,
                       mq.approval_stage,
                       mq.submitted_at,
                       mc.is_callback_required,
                       COALESCE(NULLIF(TRIM(mq.selected_panel_labels), ''), sp.selected_panel_labels, 'Omnibus') AS selected_panel_labels,
                       mq.region_label,
                       COALESCE(mq.region_respondent_ordinal, 1)::int AS region_respondent_ordinal
                FROM mart.main_case_queue mq
                INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
                LEFT JOIN mart.main_case_dim mcd ON mcd.case_id = mc.case_id
                LEFT JOIN selected_panels sp ON sp.case_id = mc.case_id
                WHERE mq.case_id = %s
                """,
                (case_id,),
            )
            case_row = cur.fetchone()
            if not case_row:
                raise HTTPException(status_code=404, detail="Case not found.")
            case_row["lga_name"] = _main_choice_label(settings, "Sector", case_row.get("lga_name")) or case_row.get("lga_name")

            # Load all section records for this case
            cur.execute(
                "SELECT section_name, record FROM clean.main_case_section WHERE case_id = %s",
                (case_id,),
            )
            section_rows = cur.fetchall()

            # Extract phone numbers from section records. Audio comes from
            # clean.main_case_media first because it contains the real
            # SurveyCTO attachment filenames for the current BHT form.
            phone_no: str | None = None
            device_phone_no: str | None = None
            respondent_record = case_row.get("record") if isinstance(case_row.get("record"), dict) else {}
            respondent_name = _first_nonblank_record_value(
                respondent_record,
                (
                    "Resp_Title",
                    "First_name",
                    "Surname",
                ),
            )
            respondent_title = _main_respondent_title_label(settings, _first_nonblank_record_value(respondent_record, ("Resp_Title",)))
            respondent_first_name = _first_nonblank_record_value(respondent_record, ("First_name",))
            respondent_surname = _first_nonblank_record_value(respondent_record, ("Surname",))
            respondent_name_parts = [respondent_title, respondent_first_name, respondent_surname]
            respondent_name = " ".join(part for part in respondent_name_parts if part).strip() or respondent_name
            respondent_address = _first_nonblank_record_value(
                respondent_record,
                (
                    "respondent_address",
                    "RespondentAddress",
                    "Respondent_Address",
                    "address",
                    "Address",
                    "HH_address",
                    "household_address",
                    "location_address",
                    "street_address",
                ),
            )

            for sec in section_rows:
                rec = sec["record"] or {}
                sec_name = (sec["section_name"] or "").upper()
                if isinstance(rec, dict):
                    respondent_name = respondent_name or _first_nonblank_record_value(
                        rec,
                        (
                            "Resp_Title",
                            "First_name",
                            "Surname",
                        ),
                    )
                    section_title = _main_respondent_title_label(settings, _first_nonblank_record_value(rec, ("Resp_Title",)))
                    section_first_name = _first_nonblank_record_value(rec, ("First_name",))
                    section_surname = _first_nonblank_record_value(rec, ("Surname",))
                    section_name = " ".join(part for part in [section_title, section_first_name, section_surname] if part).strip()
                    respondent_name = section_name or respondent_name
                    respondent_address = respondent_address or _first_nonblank_record_value(
                        rec,
                        (
                            "respondent_address",
                            "RespondentAddress",
                            "Respondent_Address",
                            "address",
                            "Address",
                            "HH_address",
                            "household_address",
                            "location_address",
                            "street_address",
                        ),
                    )

                if "DEMOGRAPHICS" in sec_name or sec_name.startswith("E."):
                    raw = rec.get("Mobile") or rec.get("phone.no") or rec.get("phoneno") or rec.get("phone_no")
                    if raw:
                        phone_no = str(raw)

                if "META" in sec_name:
                    raw = rec.get("devicephonenum")
                    if raw:
                        device_phone_no = str(raw)
            if not phone_no and case_row.get("phone_mobile"):
                phone_no = str(case_row.get("phone_mobile"))

            audio_files, audio_file_items = _load_case_audio_files(cur, settings, case_row["case_id"])
            if not audio_files:
                audio_files = _extract_audio_files_from_sections(section_rows)
                audio_file_items = [
                    {
                        "variable_name": variable,
                        "label": _audio_variable_label_map(str(settings.root_dir)).get(variable, variable),
                        "file_name": value,
                        "media_url": _media_proxy_url(value),
                    }
                    for variable, value in audio_files.items()
                    if value
                ]

            # Load callback history
            history_query = """
                SELECT co.callback_id::text, co.attempt_no, co.outcome_code, co.outcome_note,
                       co.sampled_flag, co.assigned_to_user_id::text,
                       COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), ''), co.assigned_to_user_id::text) AS assigned_to_username,
                       co.completed_at, co.created_at
                FROM qc.callback_outcome co
                LEFT JOIN app.user_account ua ON ua.user_id = co.assigned_to_user_id
                WHERE co.case_id = %s
            """
            history_params: list[Any] = [case_id]
            manager_roles = {"SUPERADMIN", "PDM-ADMIN"}
            if str(user.role or "").strip().upper() not in manager_roles:
                history_query += " AND co.assigned_to_user_id = %s::uuid"
                history_params.append(user.id)
            history_query += " ORDER BY co.attempt_no ASC"
            cur.execute(history_query, history_params)
            history = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT position, section_name, variable_name, question_label,
                       respondent_answer_label, callback_answer, is_correct, verified_at
                FROM qc.callback_verification_question
                WHERE case_id = %s
                ORDER BY position
                """,
                (case_id,),
            )
            verification_questions = [dict(r) for r in cur.fetchall()]

            accompaniment = _get_accompaniment_verification(
                cur,
                case_row["case_id"],
                case_row.get("submission_key"),
                "callback",
                case_row.get("supacc_confirm"),
                case_row.get("sup_photo"),
            )

    return {
        "submission_key": case_row["submission_key"],
        "case_id": case_row["case_id"],
        "ea_id": case_row["ea_id"],
        "ea_name": case_row["ea_name"],
        "lga_name": case_row["lga_name"],
        "state_name": case_row["state_name"],
        "interviewer_id": case_row["interviewer_id"],
        "supervisor_id": case_row["supervisor_id"],
        "approval_stage": case_row["approval_stage"],
        "submitted_at": str(case_row["submitted_at"]) if case_row["submitted_at"] else None,
        "is_callback_required": bool(case_row["is_callback_required"]),
        "respondent_name": respondent_name,
        "respondent_address": respondent_address,
        "phone_no": phone_no,
        "audio_files": audio_files,
        "audio_file_items": audio_file_items,
        "callback_history": history,
        "verification_questions": verification_questions,
        "accompaniment": accompaniment,
        "supacc_confirm": case_row.get("supacc_confirm"),
        "sup_photo": case_row.get("sup_photo"),
        "selected_panel_labels": case_row.get("selected_panel_labels"),
        "region_label": case_row.get("region_label"),
        "region_respondent_ordinal": int(case_row.get("region_respondent_ordinal") or 1),
        "case_label": f"{case_row.get('region_label') or 'Region'}_Resp._{int(case_row.get('region_respondent_ordinal') or 1)}",
    }


# ---------------------------------------------------------------------------
# Accompaniment verification
# ---------------------------------------------------------------------------

def _ensure_accompaniment_verification_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qc.accompaniment_verification (
            verification_id text PRIMARY KEY,
            instrument_code text NOT NULL DEFAULT 'main',
            review_context text NOT NULL,
            case_id text NOT NULL,
            submission_key text NULL,
            verification_status text NULL,
            verification_note text NULL,
            picture_url text NULL,
            accompanied_value text NULL,
            verified_by_user_id uuid NULL,
            verified_at timestamptz NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS accompaniment_verification_case_context_idx
        ON qc.accompaniment_verification (instrument_code, review_context, case_id)
    """)
    cur.execute("""
        ALTER TABLE qc.accompaniment_verification
        ALTER COLUMN submission_key DROP NOT NULL,
        ALTER COLUMN verification_status DROP NOT NULL,
        ALTER COLUMN verification_note DROP NOT NULL,
        ALTER COLUMN picture_url DROP NOT NULL,
        ALTER COLUMN accompanied_value DROP NOT NULL,
        ALTER COLUMN verified_by_user_id DROP NOT NULL,
        ALTER COLUMN verified_at DROP NOT NULL
    """)


def _get_accompaniment_verification(cur, case_id: str, submission_key: str | None, review_context: str, accompanied_value: str | None, picture_url: str | None) -> dict[str, Any]:
    _ensure_accompaniment_verification_table(cur)
    cur.execute(
        """
        SELECT
            verification_status,
            verification_note,
            picture_url,
            accompanied_value,
            verified_at,
            verified_by_user_id::text AS verified_by_user_id
        FROM qc.accompaniment_verification
        WHERE instrument_code = 'main' AND review_context = %s AND case_id = %s
        LIMIT 1
        """,
        (review_context, case_id),
    )
    row = cur.fetchone()
    payload = dict(row) if row else {}
    payload['accompanied_value'] = payload.get('accompanied_value') or accompanied_value
    payload['picture_url'] = payload.get('picture_url') or picture_url
    payload['submission_key'] = submission_key
    return payload


def save_accompaniment_verification(
    settings: Settings,
    user: AuthUser,
    review_context: str,
    case_id: str,
    submission_key: str | None,
    verification_status: str,
    verification_note: str | None = None,
) -> dict[str, Any]:
    allowed_statuses = {'verified', 'not_verified', 'needs_review'}
    if review_context not in {'callback', 'audio'}:
        raise HTTPException(status_code=400, detail='Unsupported accompaniment review context.')
    if verification_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail='Invalid accompaniment verification status.')

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT submission_key, COALESCE(NULLIF(TRIM(record->>'accomp'), ''), NULLIF(TRIM(record->>'supacc_confirm'), '')) AS accompanied_value, COALESCE(NULLIF(TRIM(record->>'Take_pictures'), ''), NULLIF(TRIM(record->>'sup_photo'), '')) AS picture_url FROM clean.main_case WHERE case_id = %s",
                (case_id,),
            )
            case_row = cur.fetchone()
            if not case_row:
                raise HTTPException(status_code=404, detail='Main Survey case not found.')
            actual_submission_key = submission_key or case_row.get('submission_key')
            accompanied_value = case_row.get('accompanied_value')
            picture_url = case_row.get('picture_url')
            _ensure_accompaniment_verification_table(cur)
            cur.execute(
                """
                INSERT INTO qc.accompaniment_verification (
                    verification_id, instrument_code, review_context, case_id, submission_key, verification_status, verification_note,
                    picture_url, accompanied_value, verified_by_user_id, verified_at, updated_at
                )
                VALUES (%s, 'main', %s, %s, %s, %s, %s, %s, %s, %s::uuid, now(), now())
                ON CONFLICT (instrument_code, review_context, case_id) DO UPDATE
                SET submission_key = EXCLUDED.submission_key,
                    verification_status = EXCLUDED.verification_status,
                    verification_note = EXCLUDED.verification_note,
                    picture_url = EXCLUDED.picture_url,
                    accompanied_value = EXCLUDED.accompanied_value,
                    verified_by_user_id = EXCLUDED.verified_by_user_id,
                    verified_at = now(),
                    updated_at = now()
                RETURNING verification_status, verification_note, picture_url, accompanied_value, verified_at, verified_by_user_id::text AS verified_by_user_id
                """,
                (str(uuid4()), review_context, case_id, actual_submission_key, verification_status, verification_note, picture_url, accompanied_value, user.id),
            )
            saved = dict(cur.fetchone() or {})
        conn.commit()
    saved['submission_key'] = actual_submission_key
    return saved


# ---------------------------------------------------------------------------
# Main accompaniment/photo check
# ---------------------------------------------------------------------------

_ENSURE_MAIN_ACCOMPANIMENT_PHOTO_CHECK_TABLE = """
CREATE TABLE IF NOT EXISTS qc.main_accompaniment_photo_check (
    check_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             text NOT NULL,
    submission_key      text,
    assigned_to_user_id uuid,
    assigned_to_role    text DEFAULT 'PDM-QC',
    status              text NOT NULL DEFAULT 'pending',
    reviewer_note       text,
    decision            text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    reviewed_at         timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id)
)
"""


def _ensure_main_accompaniment_photo_check_table(cur: Any) -> None:
    cur.execute(_ENSURE_MAIN_ACCOMPANIMENT_PHOTO_CHECK_TABLE)
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_main_accomp_photo_status
        ON qc.main_accompaniment_photo_check (status, created_at DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_main_accomp_photo_assigned
        ON qc.main_accompaniment_photo_check (assigned_to_user_id, status)
        """
    )


def _main_accompaniment_answer_label(settings: Settings, value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        labels = _main_accompaniment_choice_labels(str(settings.root_dir))
        mapped = _apply_label(raw, labels) if labels else raw
        return _clean_label_text(mapped) or raw
    except Exception:
        return raw


@lru_cache(maxsize=4)
def _main_accompaniment_choice_labels(root_dir: str) -> dict[str, str]:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    dictionary_files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not dictionary_files:
        return {}
    try:
        choices_df = pd.read_excel(dictionary_files[0], sheet_name="choices").fillna("")
    except Exception:
        return {}
    labels: dict[str, str] = {}
    if {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        for row in choices_df.to_dict(orient="records"):
            if str(row.get("list_name") or "").strip() != "accomp":
                continue
            code = str(row.get("name") or "").strip()
            label = _clean_label_text(str(row.get("label") or "")).strip()
            if not code or not label:
                continue
            labels[code] = label
            if code.endswith(".0"):
                labels[code[:-2]] = label
            else:
                labels[f"{code}.0"] = label
    return labels


def _main_photo_group_key(state_name: str | None, interviewer_id: str | None) -> str:
    payload = {
        "state": str(state_name or "Unknown").strip() or "Unknown",
        "interviewer": str(interviewer_id or "Unknown").strip() or "Unknown",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "grp:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_main_photo_group_key(group_key: str) -> dict[str, str]:
    raw_key = str(group_key or "").strip()
    if not raw_key.startswith("grp:"):
        return {"state": "", "interviewer": raw_key}
    token = raw_key[4:]
    try:
        padded = token + ("=" * (-len(token) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return {
            "state": str(payload.get("state") or "").strip(),
            "interviewer": str(payload.get("interviewer") or "").strip(),
        }
    except Exception:
        return {"state": "", "interviewer": raw_key}


def _main_accompaniment_is_positive_sql(expr: str) -> str:
    return f"LOWER(COALESCE(NULLIF(TRIM({expr}), ''), '')) IN ('1', '1.0', '2', '2.0', 'yes', 'true')"


def list_main_accompaniment_photo_checks(
    settings: Settings,
    user: AuthUser,
    show_history: bool = False,
    filter_status: str | None = None,
    filter_date_from: str | None = None,
    filter_date_to: str | None = None,
) -> list[dict[str, Any]]:
    city_label_expr = _city_label_sql("mc.record", str(settings.root_dir))
    city_label_expr_all = _city_label_sql("mc_all.record", str(settings.root_dir))
    state_expr = f"COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown')"
    interviewer_expr = _main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")
    accompanied_expr = "COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), ''))"
    where_parts = ["TRUE"]
    params: list[Any] = []
    if not show_history:
        where_parts.append("(grouped.check_status IS NULL OR grouped.check_status NOT IN ('approved', 'rejected'))")
    if filter_status:
        where_parts.append("grouped.check_status = %s")
        params.append(filter_status)
    if filter_date_from:
        where_parts.append("grouped.latest_start_at >= %s::date")
        params.append(filter_date_from)
    if filter_date_to:
        where_parts.append("grouped.latest_start_at < (%s::date + interval '1 day')")
        params.append(filter_date_to)
    where_sql = " AND ".join(where_parts)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_accompaniment_photo_check_table(cur)
            try:
                mart_where_parts = ["TRUE"]
                mart_params: list[Any] = []
                if not show_history:
                    mart_where_parts.append("(status IS NULL OR status NOT IN ('approved', 'rejected'))")
                if filter_status:
                    mart_where_parts.append("status = %s")
                    mart_params.append(filter_status)
                if filter_date_from:
                    mart_where_parts.append("latest_start_at >= %s::date")
                    mart_params.append(filter_date_from)
                if filter_date_to:
                    mart_where_parts.append("latest_start_at < (%s::date + interval '1 day')")
                    mart_params.append(filter_date_to)
                cur.execute(
                    f"""
                    SELECT
                        state_name,
                        interviewer_id,
                        interviewer_id AS ea_name,
                        state_name AS ea_id,
                        total_interviews,
                        accompanied_interviews,
                        pct_accompanied AS accompanied_pct,
                        photo_count,
                        photo_count AS sampled_from_remittance,
                        total_interviews AS total_rows,
                        accompanied_interviews AS building_only_count,
                        pct_accompanied AS building_only_pct,
                        pct_accompanied AS residential_pct,
                        0.0 AS remittance_pct,
                        NULL::text AS check_id,
                        status AS check_status,
                        assigned_to_user_id,
                        assigned_to_username,
                        COALESCE(latest_start_at, latest_submitted_at) AS submitted_at
                    FROM mart.accompaniment_interviewer
                    WHERE {" AND ".join(mart_where_parts)}
                    ORDER BY state_name, interviewer_id
                    """,
                    tuple(mart_params),
                )
                rows = cur.fetchall()
                payload = []
                for row in rows:
                    item = dict(row)
                    item["submission_key"] = _main_photo_group_key(item.get("state_name"), item.get("interviewer_id"))
                    item["case_id"] = item["submission_key"]
                    payload.append(item)
                return payload
            except Exception:
                logger.exception("Falling back to live accompaniment query after mart accompaniment read failed.")

            cur.execute(
                f"""
                WITH media AS (
                    SELECT case_id, COUNT(*)::int AS photo_count
                    FROM clean.main_case_media
                    WHERE variable_name = 'Take_pictures'
                      AND media_type = 'image'
                    GROUP BY case_id
                ),
                check_row AS (
                    SELECT DISTINCT ON (case_id)
                        check_id,
                        case_id,
                        submission_key,
                        assigned_to_user_id,
                        assigned_to_role,
                        status,
                        reviewer_note,
                        reviewed_at,
                        created_at
                    FROM qc.main_accompaniment_photo_check
                    ORDER BY case_id, created_at DESC
                ),
                base AS (
                    SELECT
                        mc.case_id,
                        mc.submission_key,
                        COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown') AS state_name,
                        {_main_interviewer_sql("mq.interviewer_id", "mq.username")} AS interviewer_id,
                        {accompanied_expr} AS accompanied_value,
                        COALESCE(media.photo_count, 0)::int AS photo_count,
                        mq.submitted_at,
                        {main_row_effective_datetime_sql("mq")} AS interview_start_at,
                        check_row.check_id,
                        check_row.status,
                        check_row.assigned_to_user_id,
                        check_row.created_at AS check_created_at
                    FROM mart.main_case_queue mq
                    INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
                    LEFT JOIN media ON media.case_id = mc.case_id
                    LEFT JOIN check_row ON check_row.case_id = mc.case_id
                ),
                grouped AS (
                    SELECT
                        state_name,
                        interviewer_id,
                        COUNT(*)::int AS total_interviews,
                        COUNT(*) FILTER (WHERE {_main_accompaniment_is_positive_sql("accompanied_value")})::int AS accompanied_interviews,
                        SUM(photo_count)::int AS photo_count,
                        MAX(submitted_at) AS latest_submitted_at,
                        MAX(interview_start_at) AS latest_start_at,
                        CASE
                            WHEN COUNT(*) FILTER (WHERE status = 'rejected') > 0 THEN 'rejected'
                            WHEN COUNT(*) FILTER (WHERE status = 'pending') > 0 THEN 'pending'
                            WHEN COUNT(check_id) = 0 THEN NULL
                            WHEN COUNT(*) FILTER (WHERE status = 'approved') = COUNT(check_id) THEN 'approved'
                            WHEN COUNT(*) FILTER (WHERE status = 'checked') = COUNT(check_id) THEN 'checked'
                            ELSE 'pending'
                        END AS check_status,
                        MAX(assigned_to_user_id::text) FILTER (WHERE assigned_to_user_id IS NOT NULL) AS assigned_to_user_id,
                        MAX(check_created_at) AS check_created_at
                    FROM base
                    GROUP BY state_name, interviewer_id
                )
                SELECT
                    grouped.state_name,
                    grouped.interviewer_id,
                    grouped.interviewer_id AS ea_name,
                    grouped.state_name AS ea_id,
                    grouped.total_interviews,
                    grouped.accompanied_interviews,
                    ROUND(grouped.accompanied_interviews::numeric / NULLIF(grouped.total_interviews, 0) * 100, 1) AS accompanied_pct,
                    grouped.photo_count,
                    grouped.photo_count AS sampled_from_remittance,
                    grouped.total_interviews AS total_rows,
                    grouped.accompanied_interviews AS building_only_count,
                    ROUND(grouped.accompanied_interviews::numeric / NULLIF(grouped.total_interviews, 0) * 100, 1) AS building_only_pct,
                    ROUND(grouped.accompanied_interviews::numeric / NULLIF(grouped.total_interviews, 0) * 100, 1) AS residential_pct,
                    0.0 AS remittance_pct,
                    NULL::text AS check_id,
                    grouped.check_status,
                    grouped.assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), ''), grouped.assigned_to_user_id::text) AS assigned_to_username,
                    COALESCE(grouped.latest_start_at, grouped.latest_submitted_at) AS submitted_at
                FROM grouped
                LEFT JOIN app.user_account ua ON ua.user_id::text = grouped.assigned_to_user_id
                WHERE {where_sql}
                ORDER BY grouped.state_name, grouped.interviewer_id
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        conn.commit()
    payload = []
    for row in rows:
        item = dict(row)
        item["submission_key"] = _main_photo_group_key(item.get("state_name"), item.get("interviewer_id"))
        item["case_id"] = item["submission_key"]
        payload.append(item)
    return payload


def assign_main_accompaniment_photo_checks(
    settings: Settings,
    user: AuthUser,
    case_ids: list[str],
    assigned_to_user_id: str,
) -> dict[str, Any]:
    if not assigned_to_user_id:
        raise HTTPException(status_code=400, detail="Select a PDM-QC user before assigning photo checks.")
    normalized_keys = [str(case_id or "").strip() for case_id in case_ids if str(case_id or "").strip()]
    if not normalized_keys:
        raise HTTPException(status_code=400, detail="No cases selected.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_accompaniment_photo_check_table(cur)
            cur.execute(
                """
                SELECT 1
                FROM app.user_account ua
                INNER JOIN app.user_role ur ON ur.user_id = ua.user_id
                WHERE ua.user_id = %s::uuid
                  AND ua.is_active = true
                  AND ur.role_code = 'PDM-QC'
                """,
                (assigned_to_user_id,),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=400, detail="Photo checks can only be assigned to active PDM-QC users.")

            created = 0
            updated = 0
            skipped = 0
            city_label_expr = _city_label_sql("record", str(settings.root_dir))
            for case_id in normalized_keys:
                group = _decode_main_photo_group_key(case_id)
                if case_id.startswith("grp:"):
                    cur.execute(
                        f"""
                        SELECT case_id, submission_key
                        FROM clean.main_case
                        WHERE COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown') = %s
                          AND {_main_interviewer_sql("interviewer_id", "record->>'username'")} = %s
                        """,
                        (group.get("state") or "Unknown", normalize_main_interviewer_id(group.get("interviewer") or "Unknown")),
                    )
                    case_rows = cur.fetchall()
                else:
                    cur.execute(
                        "SELECT case_id, submission_key FROM clean.main_case WHERE case_id = %s",
                        (case_id,),
                    )
                    one_row = cur.fetchone()
                    case_rows = [one_row] if one_row else []
                if not case_rows:
                    skipped += 1
                    continue
                for case_row in case_rows:
                    if not case_row:
                        continue
                    cur.execute(
                        """
                        INSERT INTO qc.main_accompaniment_photo_check (
                            case_id, submission_key, assigned_to_user_id, assigned_to_role, status, created_at, updated_at
                        )
                        VALUES (%s, %s, %s::uuid, 'PDM-QC', 'pending', now(), now())
                        ON CONFLICT (case_id) DO UPDATE SET
                            submission_key = EXCLUDED.submission_key,
                            assigned_to_user_id = EXCLUDED.assigned_to_user_id,
                            assigned_to_role = 'PDM-QC',
                            status = CASE
                                WHEN qc.main_accompaniment_photo_check.status IN ('approved', 'rejected', 'checked') THEN qc.main_accompaniment_photo_check.status
                                ELSE 'pending'
                            END,
                            updated_at = now()
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (case_row["case_id"], case_row.get("submission_key"), assigned_to_user_id),
                    )
                    row = cur.fetchone() or {}
                    if row.get("inserted"):
                        created += 1
                    else:
                        updated += 1
        conn.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def _get_main_accompaniment_photo_group_detail(settings: Settings, user: AuthUser, group_key: str) -> dict[str, Any]:
    group = _decode_main_photo_group_key(group_key)
    state_name = group.get("state") or "Unknown"
    interviewer_id = group.get("interviewer") or "Unknown"
    city_label_expr = _city_label_sql("mc.record", str(settings.root_dir))
    city_label_expr_all = _city_label_sql("mc_all.record", str(settings.root_dir))
    state_expr = f"COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown')"
    interviewer_expr = _main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")
    accompanied_expr = "COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), ''))"

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_accompaniment_photo_check_table(cur)
            cur.execute(
                f"""
                WITH group_cases AS (
                    SELECT
                        mq.case_id,
                        mq.submission_key,
                        mq.submitted_at,
                        mq.start_time AS interview_start_time,
                        {accompanied_expr} AS accompanied_value
                    FROM mart.main_case_queue mq
                    INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
                    WHERE COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown') = %s
                      AND {_main_interviewer_sql("mq.interviewer_id", "mq.username")} = %s
                ),
                check_summary AS (
                    SELECT
                        CASE
                            WHEN COUNT(*) FILTER (WHERE check_row.status = 'rejected') > 0 THEN 'rejected'
                            WHEN COUNT(*) FILTER (WHERE check_row.status = 'pending') > 0 THEN 'pending'
                            WHEN COUNT(check_row.check_id) = 0 THEN NULL
                            WHEN COUNT(*) FILTER (WHERE check_row.status = 'approved') = COUNT(check_row.check_id) THEN 'approved'
                            WHEN COUNT(*) FILTER (WHERE check_row.status = 'checked') = COUNT(check_row.check_id) THEN 'checked'
                            ELSE 'pending'
                        END AS status,
                        MAX(check_row.assigned_to_user_id::text) FILTER (WHERE check_row.assigned_to_user_id IS NOT NULL) AS assigned_to_user_id,
                        MAX(check_row.reviewer_note) FILTER (WHERE NULLIF(TRIM(check_row.reviewer_note), '') IS NOT NULL) AS reviewer_note,
                        MAX(check_row.reviewed_at) AS reviewed_at
                    FROM group_cases gc
                    LEFT JOIN LATERAL (
                        SELECT *
                        FROM qc.main_accompaniment_photo_check check_inner
                        WHERE check_inner.case_id = gc.case_id
                        ORDER BY check_inner.created_at DESC
                        LIMIT 1
                    ) check_row ON true
                )
                SELECT
                    COUNT(*)::int AS total_interviews,
                    COUNT(*) FILTER (WHERE {_main_accompaniment_is_positive_sql("accompanied_value")})::int AS accompanied_interviews,
                    MAX(submitted_at) AS latest_submitted_at,
                    check_summary.status,
                    check_summary.assigned_to_user_id,
                    check_summary.reviewer_note,
                    check_summary.reviewed_at
                FROM group_cases, check_summary
                GROUP BY check_summary.status, check_summary.assigned_to_user_id, check_summary.reviewer_note, check_summary.reviewed_at
                """,
                (state_name, interviewer_id),
            )
            summary = cur.fetchone()
            if not summary:
                raise HTTPException(status_code=404, detail="Interviewer photo group not found.")
            cur.execute(
                f"""
                SELECT
                    mq.case_id,
                    mq.submission_key,
                    mq.submitted_at,
                    CONCAT(COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Region'), '_Resp._', COALESCE(mq.region_respondent_ordinal, 1)::text) AS case_label,
                    mq.start_time AS interview_start_time,
                    {accompanied_expr} AS accompanied_value,
                    media.variable_name,
                    media.file_name,
                    media.surveycto_path,
                    media.created_at
                FROM mart.main_case_queue mq
                INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
                INNER JOIN clean.main_case_media media ON media.case_id = mq.case_id
                WHERE COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown') = %s
                  AND {_main_interviewer_sql("mq.interviewer_id", "mq.username")} = %s
                  AND media.variable_name = 'Take_pictures'
                  AND media.media_type = 'image'
                  AND NULLIF(TRIM(COALESCE(media.surveycto_path, media.file_name, '')), '') IS NOT NULL
                ORDER BY mq.submitted_at DESC NULLS LAST, media.created_at NULLS LAST, media.file_name
                """,
                (state_name, interviewer_id),
            )
            media_rows = [dict(row) for row in cur.fetchall()]
        conn.commit()

    total = int(summary.get("total_interviews") or 0)
    accompanied = int(summary.get("accompanied_interviews") or 0)
    pct = round(accompanied / total * 100, 1) if total else 0.0
    check_record = {
        "check_id": group_key,
        "submission_key": group_key,
        "ea_id": state_name,
        "ea_name": interviewer_id,
        "state_name": state_name,
        "building_only_pct": pct,
        "building_only_count": accompanied,
        "total_rows": total,
        "status": summary.get("status") or "pending",
        "assigned_to_user_id": summary.get("assigned_to_user_id"),
        "reviewer_note": summary.get("reviewer_note"),
        "reviewed_at": summary.get("reviewed_at"),
        "accompanied_value": f"{accompanied} of {total}",
        "submitted_at": summary.get("latest_submitted_at"),
    }
    photos = []
    for index, row in enumerate(media_rows, start=1):
        media_ref = str(row.get("surveycto_path") or row.get("file_name") or "").strip()
        photos.append(
            {
                "listing_row_id": f"{row.get('case_id')}-Take_pictures-{index}",
                "building_no": index,
                "photo_ref": media_ref,
                "photo_url": _media_proxy_url(media_ref),
                "gps_lat": None,
                "gps_long": None,
                "variable_name": row.get("variable_name"),
                "file_name": row.get("file_name"),
                "case_id": row.get("case_id"),
                "submission_key": row.get("submission_key"),
                "case_label": row.get("case_label"),
                "start_time": row.get("interview_start_time"),
                "submitted_at": row.get("submitted_at"),
                "accompanied_value": _main_accompaniment_answer_label(settings, row.get("accompanied_value")),
            }
        )
    ea_info = {
        "ea_id": state_name,
        "ea_name": interviewer_id,
        "state_name": state_name,
        "building_only_pct": pct,
        "accompanied_value": f"{accompanied} of {total}",
        "submitted_at": summary.get("latest_submitted_at"),
    }
    return {"check": check_record, "ea_info": ea_info, "photos": photos}


def get_main_accompaniment_photo_detail(settings: Settings, user: AuthUser, case_id: str) -> dict[str, Any]:
    if str(case_id or "").startswith("grp:"):
        return _get_main_accompaniment_photo_group_detail(settings, user, case_id)
    city_label_expr = _city_label_sql("mc.record", str(settings.root_dir))
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_accompaniment_photo_check_table(cur)
            cur.execute(
                f"""
                SELECT
                    mq.case_id,
                    mq.submission_key,
                    mc.ea_id,
                    CONCAT(COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Region'), '_Resp._', COALESCE(mq.region_respondent_ordinal, 1)::text) AS case_label,
                    COALESCE(NULLIF(TRIM(mq.region_label), ''), {city_label_expr}, 'Unknown') AS ea_name,
                    COALESCE(NULLIF(TRIM(mq.region_label), ''), {city_label_expr}, 'Unknown') AS state_name,
                    COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), '')) AS accompanied_value,
                    mq.start_time AS interview_start_time,
                    mq.submitted_at,
                    check_row.check_id::text AS check_id,
                    check_row.status,
                    check_row.assigned_to_user_id::text AS assigned_to_user_id,
                    check_row.reviewer_note,
                    check_row.reviewed_at
                FROM mart.main_case_queue mq
                INNER JOIN clean.main_case mc ON mc.case_id = mq.case_id
                LEFT JOIN LATERAL (
                    SELECT *
                    FROM qc.main_accompaniment_photo_check check_inner
                    WHERE check_inner.case_id = mq.case_id
                    ORDER BY check_inner.created_at DESC
                    LIMIT 1
                ) check_row ON true
                WHERE mq.case_id = %s
                """,
                (case_id,),
            )
            case_row = cur.fetchone()
            if not case_row:
                raise HTTPException(status_code=404, detail="Main Survey case not found.")
            cur.execute(
                """
                SELECT variable_name, file_name, surveycto_path, created_at
                FROM clean.main_case_media
                WHERE case_id = %s
                  AND variable_name = 'Take_pictures'
                  AND media_type = 'image'
                  AND NULLIF(TRIM(COALESCE(surveycto_path, file_name, '')), '') IS NOT NULL
                ORDER BY created_at NULLS LAST, file_name
                """,
                (case_id,),
            )
            media_rows = [dict(row) for row in cur.fetchall()]
        conn.commit()

    accompanied_label = _main_accompaniment_answer_label(settings, case_row.get("accompanied_value"))
    check_record = {
        "check_id": case_row.get("check_id"),
        "submission_key": case_row.get("case_id"),
        "ea_id": case_row.get("ea_id"),
        "ea_name": case_row.get("ea_name"),
        "state_name": case_row.get("state_name"),
        "building_only_pct": 100.0 if media_rows else 0.0,
        "building_only_count": len(media_rows),
        "total_rows": 1,
        "status": case_row.get("status") or "pending",
        "assigned_to_user_id": case_row.get("assigned_to_user_id"),
        "reviewer_note": case_row.get("reviewer_note"),
        "reviewed_at": case_row.get("reviewed_at"),
        "accompanied_value": accompanied_label,
        "submitted_at": case_row.get("submitted_at"),
    }
    photos = []
    for index, row in enumerate(media_rows, start=1):
        media_ref = str(row.get("surveycto_path") or row.get("file_name") or "").strip()
        photos.append(
            {
                "listing_row_id": f"{case_id}-Take_pictures-{index}",
                "building_no": index,
                "photo_ref": media_ref,
                "photo_url": _media_proxy_url(media_ref),
                "gps_lat": None,
                "gps_long": None,
                "variable_name": row.get("variable_name"),
                "file_name": row.get("file_name"),
                "case_id": case_id,
                "submission_key": case_row.get("submission_key"),
                "case_label": case_row.get("case_label"),
                "start_time": case_row.get("interview_start_time"),
                "submitted_at": case_row.get("submitted_at"),
                "accompanied_value": accompanied_label,
            }
        )
    ea_info = {
        "ea_id": case_row.get("ea_id"),
        "ea_name": case_row.get("ea_name"),
        "state_name": case_row.get("state_name"),
        "building_only_pct": check_record["building_only_pct"],
        "accompanied_value": accompanied_label,
        "submitted_at": case_row.get("submitted_at"),
    }
    return {"check": check_record, "ea_info": ea_info, "photos": photos}


def submit_main_accompaniment_photo_decision(
    settings: Settings,
    user: AuthUser,
    check_id: str,
    status: str,
    reviewer_note: str | None = None,
) -> dict[str, Any]:
    if status not in {"approved", "rejected", "checked"}:
        raise HTTPException(status_code=400, detail="status must be 'approved', 'rejected', or 'checked'.")
    if str(check_id or "").startswith("grp:"):
        group = _decode_main_photo_group_key(check_id)
        state_name = group.get("state") or "Unknown"
        interviewer_id = group.get("interviewer") or "Unknown"
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                _ensure_main_accompaniment_photo_check_table(cur)
                cur.execute(
                    """
                    SELECT mq.case_id, mq.submission_key
                    FROM mart.main_case_queue mq
                    WHERE COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown') = %s
                      AND {_main_interviewer_sql("mq.interviewer_id")} = %s
                    """,
                    (state_name, interviewer_id),
                )
                rows = cur.fetchall()
                if not rows:
                    raise HTTPException(status_code=404, detail="Photo check group not found.")
                for case_row in rows:
                    cur.execute(
                        """
                        INSERT INTO qc.main_accompaniment_photo_check (
                            case_id, submission_key, assigned_to_user_id, assigned_to_role,
                            status, decision, reviewer_note, reviewed_at, created_at, updated_at
                        )
                        VALUES (%s, %s, NULL, 'PDM-QC', %s, %s, %s, now(), now(), now())
                        ON CONFLICT (case_id) DO UPDATE
                        SET status = EXCLUDED.status,
                            decision = EXCLUDED.decision,
                            reviewer_note = EXCLUDED.reviewer_note,
                            reviewed_at = now(),
                            updated_at = now()
                        """,
                        (case_row["case_id"], case_row.get("submission_key"), status, status, reviewer_note),
                    )
                _sync_main_case_queue_rows(cur, [str(row["case_id"]) for row in rows])
            conn.commit()
        _clear_main_case_list_cache()
        return {"check_id": check_id, "case_id": check_id, "status": status, "updated": len(rows)}
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_main_accompaniment_photo_check_table(cur)
            cur.execute(
                """
                UPDATE qc.main_accompaniment_photo_check
                SET status = %s,
                    decision = %s,
                    reviewer_note = %s,
                    reviewed_at = now(),
                    updated_at = now()
                WHERE check_id = %s::uuid
                RETURNING check_id::text, case_id, status
                """,
                (status, status, reviewer_note, check_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Photo check not found.")
        conn.commit()
    _clear_main_case_list_cache()
    return dict(row)


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------

def bulk_update_main_case_status(
    settings: Settings,
    user: AuthUser,
    submission_keys: list[str],
    new_status: str,
    note: str | None = None,
) -> dict[str, Any]:
    _ensure_status_allowed(new_status)
    if user.role not in MAIN_REVIEW_DECISION_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to update case status.")
    if not submission_keys:
        raise HTTPException(status_code=400, detail="No submission keys provided.")
    if len(submission_keys) > 200:
        raise HTTPException(status_code=400, detail="Bulk limit is 200 cases per request.")

    normalized_keys = list(dict.fromkeys(str(key or "").strip() for key in submission_keys if str(key or "").strip()))
    if not normalized_keys:
        raise HTTPException(status_code=400, detail="No valid submission keys provided.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH selected_cases AS MATERIALIZED (
                    SELECT
                        mc.submission_key,
                        mc.case_id,
                        mc.approval_stage AS previous_status
                    FROM clean.main_case mc
                    WHERE mc.submission_key = ANY(%s)
                ),
                updated_cases AS (
                    UPDATE clean.main_case mc
                    SET approval_stage = %s,
                        is_callback_required = CASE
                            WHEN %s THEN false
                            ELSE mc.is_callback_required
                        END,
                        updated_at = now()
                    FROM selected_cases selected
                    WHERE mc.submission_key = selected.submission_key
                    RETURNING
                        mc.submission_key,
                        mc.case_id,
                        selected.previous_status
                ),
                inserted_history AS (
                    INSERT INTO qc.case_status_history (
                        instrument_code,
                        submission_key,
                        case_id,
                        previous_status,
                        new_status,
                        changed_by_user_id,
                        change_note,
                        device_id
                    )
                    SELECT
                        'main',
                        updated.submission_key,
                        COALESCE(updated.case_id, updated.submission_key),
                        updated.previous_status,
                        %s,
                        %s,
                        %s,
                        NULL
                    FROM updated_cases updated
                    RETURNING submission_key
                )
                SELECT updated.submission_key, COALESCE(updated.case_id, updated.submission_key) AS case_id
                FROM updated_cases updated
                INNER JOIN inserted_history history ON history.submission_key = updated.submission_key
                """,
                (
                    normalized_keys,
                    new_status,
                    new_status in MAIN_FINAL_STATUSES,
                    new_status,
                    user.id,
                    note or "",
                ),
            )
            updated_rows = cur.fetchall()
            updated_keys = [str(row["submission_key"]) for row in updated_rows]
            case_ids = [str(row["case_id"]) for row in updated_rows]
            _sync_main_case_queue_rows(cur, case_ids)
        conn.commit()

    updated_key_set = set(updated_keys)
    not_found = [key for key in normalized_keys if key not in updated_key_set]
    _clear_main_status_dependent_caches(settings)
    return {"updated": len(updated_keys), "notFound": not_found, "newStatus": new_status}


def bulk_push_to_callback(
    settings: Settings,
    user: AuthUser,
    submission_keys: list[str],
    assigned_to_role: str | None = None,
    assigned_to_user_id: str | None = None,
) -> dict[str, Any]:
    if not submission_keys:
        raise HTTPException(status_code=400, detail="No submission keys provided.")
    if len(submission_keys) > 200:
        raise HTTPException(status_code=400, detail="Bulk limit is 200 cases per request.")
    assigned_to_role = "PDM-QC"

    if not assigned_to_user_id:
        raise HTTPException(status_code=400, detail="Select an active PDM-QC user before pushing callback cases.")
    elif not _user_has_active_role(settings, assigned_to_user_id, "PDM-QC"):
        raise HTTPException(status_code=400, detail="Callback cases can only be assigned to active PDM-QC users.")

    created = 0
    already_flagged = 0
    not_found: list[str] = []

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            for sk in submission_keys:
                cur.execute(
                    "SELECT case_id, is_callback_required FROM clean.main_case WHERE submission_key = %s",
                    (sk,),
                )
                row = cur.fetchone()
                if not row:
                    not_found.append(sk)
                    continue

                case_id = row["case_id"]

                # Flag the case for callback
                cur.execute(
                    "UPDATE clean.main_case SET is_callback_required = true, updated_at = now() WHERE submission_key = %s",
                    (sk,),
                )

                if row["is_callback_required"]:
                    cur.execute(
                        """
                        SELECT callback_id, assigned_to_user_id
                        FROM qc.callback_outcome
                        WHERE case_id = %s AND outcome_code = 'pending'
                        ORDER BY attempt_no DESC, created_at DESC NULLS LAST
                        LIMIT 1
                        """,
                        (case_id,),
                    )
                    existing_pending = cur.fetchone()
                    if existing_pending and (user.role or "").strip().upper() in EDIT_ROLES:
                        cur.execute(
                            "UPDATE qc.callback_outcome SET assigned_to_user_id = %s WHERE callback_id = %s",
                            (assigned_to_user_id, existing_pending["callback_id"]),
                        )
                    _sync_main_case_queue_rows(cur, [case_id])
                    already_flagged += 1
                    continue

                # Create initial pending callback record with optional assignee
                cur.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no FROM qc.callback_outcome WHERE case_id = %s",
                    (case_id,),
                )
                next_no = (cur.fetchone() or {}).get("next_no", 1)
                cur.execute(
                    """
                    INSERT INTO qc.callback_outcome (case_id, sampled_flag, attempt_no, assigned_to_user_id)
                    VALUES (%s, false, %s, %s)
                    """,
                    (case_id, next_no, assigned_to_user_id),
                )
                created += 1
                _sync_main_case_queue_rows(cur, [case_id])

        conn.commit()

    _clear_main_case_list_cache()
    return {"created": created, "alreadyFlagged": already_flagged, "notFound": not_found, "assigned_to_role": assigned_to_role}


# ---------------------------------------------------------------------------
# Audio Listening
# ---------------------------------------------------------------------------

def list_audio_listening(settings: Settings, user: AuthUser) -> list[dict[str, Any]]:
    params: list[Any] = []
    visibility_clause = ""

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'clean'
                  AND table_name = 'audio_listening'
                  AND column_name = 'audio_url'
                LIMIT 1
                """
            )
            has_audio_url = bool(cur.fetchone())

            audio_url_expr = "al.audio_url" if has_audio_url else "NULL::text AS audio_url"
            query = f"""
                SELECT
                    al.audio_id,
                    al.case_id,
                    mq.submission_key AS submission_key,
                    CONCAT(COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Region'), '_Resp._', COALESCE(mq.region_respondent_ordinal, 1)::text) AS case_label,
                    {audio_url_expr},
                    al.status,
                    al.quality_rating,
                    al.reviewer_note,
                    al.assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), ''), al.assigned_to_user_id::text) AS assigned_to_username,
                    al.reviewed_at,
                    al.created_at,
                    mq.approval_stage,
                    mq.start_time,
                    COALESCE(NULLIF(TRIM(mq.ea_name), ''), mc.record->>'ea_name') AS ea_name,
                    COALESCE(NULLIF(TRIM(mq.lga_name), ''), mc.record->>'lga_name') AS lga_name,
                    COALESCE(NULLIF(TRIM(mq.state_name), ''), mc.record->>'state_name') AS state_name,
                    COALESCE(NULLIF(TRIM(mq.region_label), ''), NULLIF(TRIM(mc.record->>'state_name'), '')) AS region_label,
                    COALESCE(NULLIF(TRIM(mq.selected_panel_labels), ''), COALESCE(sp.selected_panel_labels, 'Omnibus')) AS selected_panel_labels,
                    COALESCE(mq.qc_flag_count, 0)::int AS qc_flag_count,
                    mq.interviewer_id
                FROM clean.audio_listening al
                INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
                INNER JOIN clean.main_case mc ON mc.case_id = al.case_id
                LEFT JOIN (
                    SELECT
                        case_id,
                        STRING_AGG(
                            DISTINCT CASE panel_code
                                WHEN 'Panel_1' THEN 'Noodles'
                                WHEN 'Panel_2' THEN 'Toothpaste'
                                WHEN 'Panel_3' THEN 'Edible Oil'
                                WHEN 'Panel_4' THEN 'Bleach'
                                WHEN 'Panel_5' THEN 'Toilet Cleaner'
                                WHEN 'Panel_6' THEN 'Snacks'
                                WHEN 'Panel_7' THEN 'Breakfast Cereals'
                                WHEN 'Panel_8' THEN 'Condiment Mixes'
                                WHEN 'Panel_9' THEN 'Wet Hair'
                                WHEN 'Panel_10' THEN 'Dry Hair'
                                WHEN 'Panel_11' THEN 'Malt'
                                ELSE panel_code
                            END,
                            ', '
                        ) AS selected_panel_labels
                    FROM clean.main_case_panel
                    WHERE COALESCE(is_selected, TRUE)
                    GROUP BY case_id
                ) sp ON sp.case_id = mc.case_id
                LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                WHERE 1=1
                {visibility_clause}
                ORDER BY al.created_at DESC NULLS LAST
            """
            cur.execute(query, params)
            rows = cur.fetchall()

    return [dict(r) for r in rows]


def _resolve_main_case_id(cur: Any, identifier: str) -> str | None:
    cur.execute(
        """
        SELECT case_id
        FROM clean.main_case
        WHERE case_id = %s OR submission_key = %s
        LIMIT 1
        """,
        (identifier, identifier),
    )
    row = cur.fetchone()
    return row["case_id"] if row else None


def _user_has_active_role(settings: Settings, user_id: str, role_code: str) -> bool:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM app.user_account ua
                JOIN app.user_role ur USING (user_id)
                WHERE ua.user_id = %s::uuid
                  AND ua.is_active = true
                  AND ur.role_code = %s
                LIMIT 1
                """,
                (user_id, role_code),
            )
            return bool(cur.fetchone())


def assign_audio_review(settings: Settings, user: AuthUser, case_id: str, assigned_to_role: str | None = None, assigned_to_user_id: str | None = None) -> dict[str, Any]:
    assigned_to_role = "PDM-QC"

    if not assigned_to_user_id:
        raise HTTPException(status_code=400, detail="Select an active PDM-QC user before pushing audio listening cases.")
    elif not _user_has_active_role(settings, assigned_to_user_id, "PDM-QC"):
        raise HTTPException(status_code=400, detail="Audio listening cases can only be assigned to active PDM-QC users.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            resolved_case_id = _resolve_main_case_id(cur, case_id)
            if not resolved_case_id:
                raise HTTPException(status_code=404, detail="Case not found.")

            cur.execute(
                """
                SELECT audio_id::text, assigned_to_user_id, assigned_to_role, status, created_at
                FROM clean.audio_listening
                WHERE case_id = %s AND status = 'pending'
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """,
                (resolved_case_id,),
            )
            existing = cur.fetchone()

            if existing:
                existing_assignee = existing["assigned_to_user_id"]
                if existing_assignee and existing_assignee != assigned_to_user_id and existing_assignee != str(user.id):
                    cur.execute(
                        """
                        UPDATE clean.audio_listening
                        SET assigned_to_user_id = %s,
                            assigned_to_role = %s,
                            status = 'pending',
                            reviewed_at = NULL
                        WHERE audio_id = %s::uuid
                        RETURNING audio_id::text
                        """,
                        (assigned_to_user_id, assigned_to_role, existing["audio_id"]),
                    )
                    reassigned = cur.fetchone()
                    _sync_main_case_queue_rows(cur, [resolved_case_id])
                    conn.commit()
                    _clear_main_case_list_cache()
                    return {
                        "audio_id": reassigned["audio_id"] if reassigned else existing["audio_id"],
                        "case_id": resolved_case_id,
                        "assigned_to_user_id": assigned_to_user_id,
                        "assigned_to_role": assigned_to_role,
                        "already_assigned": False,
                        "reassigned": True,
                    }

                if existing_assignee == assigned_to_user_id or (not assigned_to_user_id and existing_assignee == str(user.id)):
                    _sync_main_case_queue_rows(cur, [resolved_case_id])
                    conn.commit()
                    _clear_main_case_list_cache()
                    return {
                        "audio_id": existing["audio_id"],
                        "case_id": resolved_case_id,
                        "assigned_to_user_id": existing_assignee,
                        "assigned_to_role": existing["assigned_to_role"],
                        "already_assigned": True,
                    }

                if not existing_assignee:
                    cur.execute(
                        """
                        UPDATE clean.audio_listening
                        SET assigned_to_user_id = %s,
                            assigned_to_role = %s,
                            status = 'pending',
                            reviewed_at = NULL
                        WHERE audio_id = %s::uuid
                        RETURNING audio_id::text
                        """,
                        (assigned_to_user_id, assigned_to_role, existing["audio_id"]),
                    )
                    claimed = cur.fetchone()
                    _sync_main_case_queue_rows(cur, [resolved_case_id])
                    conn.commit()
                    _clear_main_case_list_cache()
                    return {
                        "audio_id": claimed["audio_id"] if claimed else None,
                        "case_id": resolved_case_id,
                        "assigned_to_user_id": assigned_to_user_id,
                        "assigned_to_role": assigned_to_role,
                        "already_assigned": False,
                    }

            cur.execute(
                """
                INSERT INTO clean.audio_listening (case_id, assigned_to_user_id, assigned_to_role, status, created_at)
                VALUES (%s, %s, %s, 'pending', NOW())
                RETURNING audio_id::text
                """,
                (resolved_case_id, assigned_to_user_id, assigned_to_role),
            )
            result = cur.fetchone()
            _sync_main_case_queue_rows(cur, [resolved_case_id])
            conn.commit()

    _clear_main_case_list_cache()
    return {
        "audio_id": result["audio_id"] if result else None,
        "case_id": resolved_case_id,
        "assigned_to_user_id": assigned_to_user_id,
        "assigned_to_role": assigned_to_role,
        "already_assigned": False,
    }


def submit_audio_review(settings: Settings, user: AuthUser, audio_id: str, quality_rating: str, reviewer_note: str | None) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT assigned_to_user_id, status, case_id
                FROM clean.audio_listening
                WHERE audio_id = %s::uuid
                """,
                (audio_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Audio review assignment not found.")

            assigned_to_user_id = row["assigned_to_user_id"]
            role_code = (user.role or "").strip().upper()
            if assigned_to_user_id and assigned_to_user_id != str(user.id) and role_code not in {"SUPERADMIN", "PDM-ADMIN"}:
                raise HTTPException(
                    status_code=409,
                    detail="This audio review is assigned to another reviewer. Only an admin can override.",
                )

            cur.execute(
                """
                UPDATE clean.audio_listening
                SET quality_rating = %s,
                    reviewer_note = %s,
                    status = 'reviewed',
                    reviewed_at = NOW()
                WHERE audio_id = %s::uuid
                RETURNING audio_id::text
                """,
                (quality_rating, reviewer_note, audio_id),
            )
            result = cur.fetchone()
            _sync_main_case_queue_rows(cur, [row["case_id"]])
            conn.commit()

    return {"audio_id": audio_id, "status": "reviewed", "quality_rating": quality_rating}


def get_audio_review_detail(settings: Settings, user: AuthUser, audio_id: str) -> dict[str, Any]:
    role_code = (user.role or "").strip().upper()
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'clean'
                  AND table_name = 'audio_listening'
                  AND column_name = 'audio_url'
                LIMIT 1
                """
            )
            has_audio_url = bool(cur.fetchone())

            audio_url_expr = "al.audio_url" if has_audio_url else "NULL::text AS audio_url"
            cur.execute(
                f"""
                SELECT
                    al.audio_id::text AS audio_id,
                    al.case_id,
                    mq.submission_key AS submission_key,
                    CONCAT(COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Region'), '_Resp._', COALESCE(mq.region_respondent_ordinal, 1)::text) AS case_label,
                    {audio_url_expr},
                    al.status,
                    al.quality_rating,
                    al.reviewer_note,
                    al.assigned_to_user_id,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), NULLIF(TRIM(ua.username), ''), al.assigned_to_user_id::text) AS assigned_to_username,
                    al.assigned_to_role,
                    al.reviewed_at,
                    al.created_at,
                    mq.ea_id,
                    mq.interviewer_id,
                    mq.supervisor_id,
                    mq.approval_stage,
                    mq.submitted_at,
                    COALESCE(NULLIF(TRIM(mq.ea_name), ''), mc.record->>'ea_name') AS ea_name,
                    COALESCE(NULLIF(TRIM(mq.lga_name), ''), mc.record->>'lga_name') AS lga_name,
                    COALESCE(NULLIF(TRIM(mq.state_name), ''), mc.record->>'state_name') AS state_name,
                    COALESCE(NULLIF(TRIM(mc.record->>'accomp'), ''), NULLIF(TRIM(mc.record->>'supacc_confirm'), '')) AS supacc_confirm,
                    COALESCE(NULLIF(TRIM(mc.record->>'Take_pictures'), ''), NULLIF(TRIM(mc.record->>'sup_photo'), '')) AS sup_photo
                FROM clean.audio_listening al
                INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
                INNER JOIN clean.main_case mc ON mc.case_id = al.case_id
                LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
                WHERE al.audio_id = %s::uuid
                """,
                (audio_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Audio review assignment not found.")

            submission_key = row.get("submission_key")
            issues: list[dict[str, Any]] = []
            if submission_key:
                cur.execute(
                    """
                    SELECT
                        iq.issue_id::text AS issue_id,
                        iq.issue_status,
                        iq.issue_summary,
                        iq.created_at,
                        rr.rule_code,
                        rr.severity
                    FROM qc.issue_queue iq
                    LEFT JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                    WHERE iq.instrument_code = 'main'
                      AND iq.submission_key = %s
                      AND iq.issue_status <> 'resolved'
                    ORDER BY iq.created_at DESC
                    LIMIT 25
                    """,
                    (submission_key,),
                )
                issues = [dict(r) for r in cur.fetchall()]

            # Fallback audio extraction from case sections when audio_url is absent on audio_listening.
            case_id = row.get("case_id")
            audio_files: dict[str, str | None] = {f: None for f in AUDIO_AUDIT_FIELDS}
            audio_file_items: list[dict[str, Any]] = []
            accompaniment = {}
            if case_id:
                audio_files, audio_file_items = _load_case_audio_files(cur, settings, case_id)
                cur.execute(
                    """
                    SELECT section_name, record
                    FROM clean.main_case_section
                    WHERE case_id = %s
                    """,
                    (case_id,),
                )
                section_rows = cur.fetchall()
                if not audio_files:
                    audio_files = _extract_audio_files_from_sections(section_rows)
                    audio_file_items = [
                        {
                            "variable_name": variable,
                            "label": _audio_variable_label_map(str(settings.root_dir)).get(variable, variable),
                            "file_name": value,
                            "media_url": _media_proxy_url(value),
                        }
                        for variable, value in audio_files.items()
                        if value
                    ]
                accompaniment = _get_accompaniment_verification(
                    cur,
                    case_id,
                    row.get('submission_key'),
                    'audio',
                    row.get('supacc_confirm'),
                    row.get('sup_photo'),
                )

    payload = dict(row)
    primary_audio_url = _pick_primary_audio_url(audio_files)
    # Prefer the callback-style attachment URLs extracted directly from the case
    # sections. These match the working playback flow used on the callback detail
    # page and avoid relying on older blobKey/view URLs stored on audio_listening.
    payload["audio_url"] = primary_audio_url or payload.get("audio_url")

    normalized_audio_files = dict(audio_files)
    if not any(normalized_audio_files.values()) and payload.get("audio_url"):
        # Keep a single fallback player visible in the audio detail page even when
        # section extraction yields no labeled files for legacy records.
        normalized_audio_files[AUDIO_AUDIT_FIELDS[0]] = payload["audio_url"]

    payload["audio_files"] = normalized_audio_files
    payload["audio_file_items"] = audio_file_items
    payload["issues"] = issues
    payload["accompaniment"] = accompaniment
    return payload


def get_audio_review_case_detail(settings: Settings, user: AuthUser, case_id: str) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            resolved_case_id = _resolve_main_case_id(cur, case_id) or case_id
            params: list[Any] = [resolved_case_id]
            visibility_clause = ""
            cur.execute(
                f"""
                SELECT al.audio_id::text AS audio_id
                FROM clean.audio_listening al
                INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
                WHERE al.case_id = %s
                  {visibility_clause}
                ORDER BY
                    CASE COALESCE(NULLIF(TRIM(al.status), ''), 'pending') WHEN 'pending' THEN 0 ELSE 1 END,
                    al.created_at DESC NULLS LAST
                LIMIT 1
                """,
                tuple(params),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No audio review assignment found for this case.")
            audio_id = row["audio_id"]

    return get_audio_review_detail(settings, user, audio_id)


# ---------------------------------------------------------------------------
# Enumerator Stats
# ---------------------------------------------------------------------------

def list_enumerator_stats(settings: Settings, user: AuthUser, group_by: str = "enumerator") -> list[dict[str, Any]]:
    rule_codes = [rule[0] for rule in RULE_DEFINITIONS]
    normalized_group = "city" if str(group_by or "").strip().lower() == "city" else "enumerator"
    if normalized_group == "city":
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'mart'
                          AND table_name = 'city_performance'
                    ) AS exists
                    """
                )
                if bool((cur.fetchone() or {}).get("exists")):
                    cur.execute("SELECT COUNT(*)::int AS c FROM mart.city_performance")
                    if int((cur.fetchone() or {}).get("c") or 0) > 0:
                        cur.execute(
                            """
                            SELECT
                                city_id AS enumerator_id,
                                city_name AS enumerator_name,
                                total_cases,
                                approved_count,
                                rejected_count,
                                pending_count,
                                consent_obtained,
                                consent_refused,
                                avg_duration_minutes::float AS avg_duration_minutes,
                                avg_sections_completed::float AS avg_sections_completed,
                                open_issues,
                                total_issues,
                                rule_counts
                            FROM mart.city_performance
                            ORDER BY total_cases DESC, city_id
                            """
                        )
                        rows = []
                        for raw in cur.fetchall():
                            row = dict(raw)
                            rule_counts = row.pop("rule_counts", {}) or {}
                            for rule_code in rule_codes:
                                row[rule_code.lower()] = int(rule_counts.get(rule_code.lower()) or rule_counts.get(rule_code) or 0)
                            rows.append(row)
                        return rows

        city_label_expr = _city_label_sql("mc.record", str(settings.root_dir))
        scope_clause, scope_params = main_case_scope_clause(settings, "mc")
        rule_selects = ",\n                    ".join(
            f"COALESCE(MAX(rf.flag_count) FILTER (WHERE rf.rule_code = '{rule_code}'), 0)::int AS {rule_code.lower()}"
            for rule_code in rule_codes
        )
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH case_base AS (
                        SELECT
                            mc.case_id,
                            mc.submission_key,
                            COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown') AS group_id,
                            COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown') AS group_name,
                            mc.approval_stage,
                            lower(trim(COALESCE(mc.record->>'consent_obtained', ''))) AS consent_value,
                            CASE
                                WHEN COALESCE(mc.record->>'duration', '') ~ '^[0-9]+(\\.[0-9]+)?$'
                                    THEN CASE
                                        WHEN (mc.record->>'duration')::numeric > {MAIN_INTERVIEW_MAX_MINUTES}
                                            THEN ((mc.record->>'duration')::numeric / 60.0)
                                        ELSE (mc.record->>'duration')::numeric
                                    END
                                ELSE NULL
                            END AS duration_minutes
                        FROM clean.main_case mc
                        WHERE NOT EXISTS (
                            SELECT 1 FROM clean.deleted_main_cases dmc
                            WHERE dmc.submission_key = mc.submission_key
                        )
                        {scope_clause}
                    ),
                    issue_counts AS (
                        SELECT
                            cb.group_id,
                            COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issues,
                            COUNT(*)::int AS total_issues
                        FROM qc.issue_queue iq
                        INNER JOIN case_base cb
                          ON COALESCE(NULLIF(TRIM(cb.submission_key), ''), NULLIF(TRIM(cb.case_id), '')) =
                             COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                        WHERE iq.instrument_code = 'main'
                        GROUP BY cb.group_id
                    ),
                    rule_flags AS (
                        SELECT
                            cb.group_id,
                            NULLIF(TRIM(rr.rule_code), '') AS rule_code,
                            COUNT(*)::int AS flag_count
                        FROM qc.issue_queue iq
                        INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                        INNER JOIN case_base cb
                          ON COALESCE(NULLIF(TRIM(cb.submission_key), ''), NULLIF(TRIM(cb.case_id), '')) =
                             COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                        WHERE iq.instrument_code = 'main'
                          AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                          AND NULLIF(TRIM(rr.rule_code), '') IS NOT NULL
                        GROUP BY cb.group_id, NULLIF(TRIM(rr.rule_code), '')
                    ),
                    case_stats AS (
                        SELECT
                            group_id AS enumerator_id,
                            group_name AS enumerator_name,
                            COUNT(*)::int AS total_cases,
                            COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'approved')::int AS approved_count,
                            COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'rejected')::int AS rejected_count,
                            COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) IN ('submitted', 'pending_review', 'in_review', 'corrected'))::int AS pending_count,
                            COUNT(*) FILTER (WHERE consent_value IN ('true', 'yes', '1'))::int AS consent_obtained,
                            COUNT(*) FILTER (WHERE consent_value NOT IN ('true', 'yes', '1'))::int AS consent_refused,
                            ROUND(COALESCE(AVG(duration_minutes), 0), 2)::float AS avg_duration_minutes
                        FROM case_base
                        GROUP BY group_id, group_name
                    )
                    SELECT
                        cs.*,
                        0.0::float AS avg_sections_completed,
                        COALESCE(ic.open_issues, 0)::int AS open_issues,
                        COALESCE(ic.total_issues, 0)::int AS total_issues,
                        {rule_selects}
                    FROM case_stats cs
                    LEFT JOIN issue_counts ic ON ic.group_id = cs.enumerator_id
                    LEFT JOIN rule_flags rf ON rf.group_id = cs.enumerator_id
                    GROUP BY
                        cs.enumerator_id, cs.enumerator_name, cs.total_cases, cs.approved_count,
                        cs.rejected_count, cs.pending_count, cs.consent_obtained, cs.consent_refused,
                        cs.avg_duration_minutes, ic.open_issues, ic.total_issues
                    ORDER BY cs.total_cases DESC, cs.enumerator_id
                    """,
                    tuple(scope_params),
                )
                return [dict(r) for r in cur.fetchall()]

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'mart'
                      AND table_name = 'enumerator_performance'
                ) AS exists
                """
            )
            if bool((cur.fetchone() or {}).get("exists")):
                cur.execute("SELECT COUNT(*)::int AS c FROM mart.enumerator_performance")
                if int((cur.fetchone() or {}).get("c") or 0) > 0:
                    cur.execute(
                        """
                        SELECT
                            enumerator_id,
                            enumerator_name,
                            total_cases,
                            approved_count,
                            rejected_count,
                            pending_count,
                            consent_obtained,
                            consent_refused,
                            avg_duration_minutes::float AS avg_duration_minutes,
                            avg_sections_completed::float AS avg_sections_completed,
                            open_issues,
                            total_issues,
                            rule_counts
                        FROM mart.enumerator_performance
                        ORDER BY total_cases DESC, enumerator_id
                        """
                    )
                    rows = []
                    for raw in cur.fetchall():
                        row = dict(raw)
                        rule_counts = row.pop("rule_counts", {}) or {}
                        for rule_code in rule_codes:
                            row[rule_code.lower()] = int(rule_counts.get(rule_code.lower()) or rule_counts.get(rule_code) or 0)
                        rows.append(row)
                    return rows

    rule_selects = ",\n                    ".join(
        f"COALESCE(MAX(rf.flag_count) FILTER (WHERE rf.rule_code = '{rule_code}'), 0)::int AS {rule_code.lower()}"
        for rule_code in rule_codes
    )
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH case_base AS (
                    SELECT
                        {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                        mc.approval_stage,
                        lower(trim(COALESCE(mc.record->>'consent_obtained', ''))) AS consent_value,
                        CASE
                            WHEN COALESCE(mc.record->>'duration', '') ~ '^[0-9]+(\\.[0-9]+)?$'
                                THEN CASE
                                    WHEN (mc.record->>'duration')::numeric > {MAIN_INTERVIEW_MAX_MINUTES}
                                        THEN ((mc.record->>'duration')::numeric / 60.0)
                                    ELSE (mc.record->>'duration')::numeric
                                END
                            ELSE NULL
                        END AS duration_minutes
                    FROM clean.main_case mc
                    WHERE {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} <> 'Unknown'
                      AND NOT EXISTS (
                          SELECT 1 FROM clean.deleted_main_cases dmc
                          WHERE dmc.submission_key = mc.submission_key
                      )
                ),
                issue_counts AS (
                    SELECT
                        {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                        COUNT(*) FILTER (WHERE COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved')::int AS open_issues,
                        COUNT(*)::int AS total_issues
                    FROM qc.issue_queue iq
                    INNER JOIN clean.main_case mc
                      ON COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), '')) =
                         COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    WHERE iq.instrument_code = 'main'
                      AND {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} <> 'Unknown'
                    GROUP BY {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")}
                ),
                rule_flags AS (
                    SELECT
                        {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} AS enumerator_id,
                        NULLIF(TRIM(rr.rule_code), '') AS rule_code,
                        COUNT(*)::int AS flag_count
                    FROM qc.issue_queue iq
                    INNER JOIN qc.rule_result rr ON rr.rule_result_id = iq.rule_result_id
                    INNER JOIN clean.main_case mc
                      ON COALESCE(NULLIF(TRIM(mc.submission_key), ''), NULLIF(TRIM(mc.case_id), '')) =
                         COALESCE(NULLIF(TRIM(iq.submission_key), ''), NULLIF(TRIM(iq.case_id), ''))
                    WHERE iq.instrument_code = 'main'
                      AND COALESCE(NULLIF(TRIM(iq.issue_status), ''), 'pending_review') <> 'resolved'
                      AND {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")} <> 'Unknown'
                      AND NULLIF(TRIM(rr.rule_code), '') IS NOT NULL
                    GROUP BY {_main_interviewer_sql("mc.interviewer_id", "mc.record->>'username'")},
                             NULLIF(TRIM(rr.rule_code), '')
                ),
                case_stats AS (
                    SELECT
                        enumerator_id,
                        enumerator_id AS enumerator_name,
                        COUNT(*)::int AS total_cases,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'approved')::int AS approved_count,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) = 'rejected')::int AS rejected_count,
                        COUNT(*) FILTER (WHERE lower(COALESCE(approval_stage, '')) IN ('submitted', 'pending_review', 'in_review', 'corrected'))::int AS pending_count,
                        COUNT(*) FILTER (WHERE consent_value IN ('true', 'yes', '1'))::int AS consent_obtained,
                        COUNT(*) FILTER (WHERE consent_value NOT IN ('true', 'yes', '1'))::int AS consent_refused,
                        ROUND(COALESCE(AVG(duration_minutes), 0), 2)::float AS avg_duration_minutes
                    FROM case_base
                    GROUP BY enumerator_id
                )
                SELECT
                    cs.*,
                    0.0::float AS avg_sections_completed,
                    COALESCE(ic.open_issues, 0)::int AS open_issues,
                    COALESCE(ic.total_issues, 0)::int AS total_issues,
                    {rule_selects}
                FROM case_stats cs
                LEFT JOIN issue_counts ic ON ic.enumerator_id = cs.enumerator_id
                LEFT JOIN rule_flags rf ON rf.enumerator_id = cs.enumerator_id
                GROUP BY
                    cs.enumerator_id, cs.enumerator_name, cs.total_cases, cs.approved_count,
                    cs.rejected_count, cs.pending_count, cs.consent_obtained, cs.consent_refused,
                    cs.avg_duration_minutes, ic.open_issues, ic.total_issues
                ORDER BY cs.total_cases DESC, cs.enumerator_id
                """
            )
            return [dict(r) for r in cur.fetchall()]


def _extract_date_key_from_candidates(*candidates: Any) -> str | None:
    for candidate in candidates:
        raw = str(candidate or '').strip()
        if not raw:
            continue
        if len(raw) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", raw[:10]):
            return raw[:10]
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            day, month, year = m.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
        m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", raw)
        if m:
            year, month, day = m.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def list_enumerator_productivity_by_date(settings: Settings, user: AuthUser, group_by: str = "enumerator") -> dict[str, Any]:
    normalized_group = "city" if str(group_by or "").strip().lower() == "city" else "enumerator"
    if normalized_group == "city":
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'mart'
                          AND table_name = 'city_productivity_by_date'
                    ) AS exists
                    """
                )
                if bool((cur.fetchone() or {}).get("exists")):
                    cur.execute("SELECT COUNT(*)::int AS c FROM mart.city_productivity_by_date")
                    if int((cur.fetchone() or {}).get("c") or 0) > 0:
                        cur.execute(
                            """
                            SELECT city_id AS enumerator_id, date_key::text AS date_key, case_count
                            FROM mart.city_productivity_by_date
                            ORDER BY date_key, city_id
                            """
                        )
                        rows = [dict(r) for r in cur.fetchall()]
                        dates = sorted({str(row.get("date_key") or "") for row in rows if row.get("date_key")})
                        pivot: dict[str, dict[str, int]] = {}
                        for row in rows:
                            enumerator_id = str(row.get("enumerator_id") or "Unknown")
                            date_key = str(row.get("date_key") or "").strip()
                            if not date_key:
                                continue
                            pivot.setdefault(enumerator_id, {})[date_key] = int(row.get("case_count") or 0)
                        return {
                            "dates": dates,
                            "items": [
                                {
                                    "enumerator_id": enumerator_id,
                                    "enumerator_name": enumerator_id,
                                    "counts": {date_key: int(counts.get(date_key, 0) or 0) for date_key in dates},
                                }
                                for enumerator_id, counts in sorted(pivot.items(), key=lambda item: item[0])
                            ],
                        }

        city_label_expr = _city_label_sql("mc.record", str(settings.root_dir))
        scope_clause, scope_params = main_case_scope_clause(settings, "mc")
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown') AS enumerator_id,
                        (mc.submitted_at AT TIME ZONE 'UTC')::date::text AS date_key,
                        COUNT(*)::int AS case_count
                    FROM clean.main_case mc
                    WHERE mc.submitted_at IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM clean.deleted_main_cases dmc
                          WHERE dmc.submission_key = mc.submission_key
                      )
                      {scope_clause}
                    GROUP BY COALESCE(NULLIF(TRIM({city_label_expr}), ''), 'Unknown'), (mc.submitted_at AT TIME ZONE 'UTC')::date
                    ORDER BY date_key, enumerator_id
                    """,
                    tuple(scope_params),
                )
                rows = [dict(r) for r in cur.fetchall()]

        dates: set[str] = set()
        pivot: dict[str, dict[str, int]] = {}
        for row in rows:
            enumerator_id = str(row.get("enumerator_id") or "Unknown")
            date_key = str(row.get("date_key") or "").strip()
            if not date_key:
                continue
            dates.add(date_key)
            row_counts = pivot.setdefault(enumerator_id, {})
            row_counts[date_key] = int(row_counts.get(date_key, 0) or 0) + int(row.get("case_count") or 0)

        sorted_dates = sorted(dates)
        return {
            "dates": sorted_dates,
            "items": [
                {
                    "enumerator_id": enumerator_id,
                    "enumerator_name": enumerator_id,
                    "counts": {date_key: int(counts.get(date_key, 0) or 0) for date_key in sorted_dates},
                }
                for enumerator_id, counts in sorted(pivot.items(), key=lambda item: item[0])
            ],
        }

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = 'mart'
                      AND table_name = 'enumerator_productivity_by_date'
                ) AS exists
                """
            )
            if bool((cur.fetchone() or {}).get("exists")):
                cur.execute("SELECT COUNT(*)::int AS c FROM mart.enumerator_productivity_by_date")
                if int((cur.fetchone() or {}).get("c") or 0) > 0:
                    cur.execute(
                        """
                        SELECT enumerator_id, date_key::text AS date_key, case_count
                        FROM mart.enumerator_productivity_by_date
                        ORDER BY date_key, enumerator_id
                        """
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                    dates = sorted({str(row.get("date_key") or "") for row in rows if row.get("date_key")})
                    pivot: dict[str, dict[str, int]] = {}
                    for row in rows:
                        enumerator_id = str(row.get("enumerator_id") or "Unknown")
                        date_key = str(row.get("date_key") or "").strip()
                        if not date_key:
                            continue
                        pivot.setdefault(enumerator_id, {})[date_key] = int(row.get("case_count") or 0)
                    return {
                        "dates": dates,
                        "items": [
                            {
                                "enumerator_id": enumerator_id,
                                "counts": {date_key: int(counts.get(date_key, 0) or 0) for date_key in dates},
                            }
                            for enumerator_id, counts in sorted(pivot.items(), key=lambda item: item[0])
                        ],
                    }

            cur.execute(
                """
                SELECT
                    {_main_interviewer_sql("interviewer_id", "record->>'username'")} AS enumerator_id,
                    (submitted_at AT TIME ZONE 'UTC')::date::text AS date_key,
                    COUNT(*)::int AS case_count
                FROM clean.main_case
                WHERE {_main_interviewer_sql("interviewer_id", "record->>'username'")} <> 'Unknown'
                  AND submitted_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM clean.deleted_main_cases dmc
                      WHERE dmc.submission_key = clean.main_case.submission_key
                  )
                GROUP BY {_main_interviewer_sql("interviewer_id", "record->>'username'")}, (submitted_at AT TIME ZONE 'UTC')::date
                ORDER BY date_key, enumerator_id
                """
            )
            rows = [dict(r) for r in cur.fetchall()]

    dates: set[str] = set()
    pivot: dict[str, dict[str, int]] = {}
    for row in rows:
        enumerator_id = str(row.get('enumerator_id') or 'Unknown')
        date_key = str(row.get('date_key') or '').strip()
        if not date_key:
            continue
        dates.add(date_key)
        row_counts = pivot.setdefault(enumerator_id, {})
        row_counts[date_key] = int(row_counts.get(date_key, 0) or 0) + int(row.get('case_count') or 0)

    sorted_dates = sorted(dates)
    items = [
        {
            'enumerator_id': enumerator_id,
            'counts': {date_key: int(counts.get(date_key, 0) or 0) for date_key in sorted_dates},
        }
        for enumerator_id, counts in sorted(pivot.items(), key=lambda item: item[0])
    ]
    return {'dates': sorted_dates, 'items': items}


def _normalize_main_qc_productivity_group(group_by: str | None) -> str:
    value = str(group_by or "qc_user").strip().lower().replace("-", "_")
    if value in {"qc", "user", "in_office_qc", "qc_user"}:
        return "qc_user"
    if value in {"interviewer", "city"}:
        return value
    raise HTTPException(status_code=400, detail="group_by must be one of: qc_user, interviewer, city")


def _fetch_main_qc_task_rows(settings: Settings, queue: str, group_by: str = "qc_user") -> list[dict[str, Any]]:
    normalized_queue = normalize_qc_productivity_queue(queue)
    normalized_group = _normalize_main_qc_productivity_group(group_by)
    if normalized_group == "interviewer":
        audio_group_expr = _main_interviewer_sql("mq.interviewer_id")
        audio_full_name_expr = "''"
        callback_group_expr = _main_interviewer_sql("mq.interviewer_id")
        callback_full_name_expr = "''"
    elif normalized_group == "city":
        audio_group_expr = "COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown')"
        audio_full_name_expr = "''"
        callback_group_expr = "COALESCE(NULLIF(TRIM(mq.region_label), ''), 'Unknown')"
        callback_full_name_expr = "''"
    else:
        audio_group_expr = "COALESCE(NULLIF(TRIM(ua.username), ''), NULLIF(TRIM(al.assigned_to_user_id), ''), 'Unknown')"
        audio_full_name_expr = "COALESCE(NULLIF(TRIM(ua.full_name), ''), '')"
        callback_group_expr = "COALESCE(NULLIF(TRIM(ua.username), ''), 'Unknown')"
        callback_full_name_expr = "COALESCE(NULLIF(TRIM(ua.full_name), ''), '')"

    task_queries: list[str] = []

    if normalized_queue in {"all", "audio"}:
        task_queries.append(
            f"""
            SELECT
                al.case_id,
                {audio_group_expr} AS username,
                {audio_full_name_expr} AS full_name,
                al.created_at AS assigned_at,
                COALESCE(
                    al.reviewed_at,
                    CASE
                        WHEN COALESCE(NULLIF(TRIM(al.status), ''), 'pending') <> 'pending' THEN al.created_at
                        ELSE NULL
                    END
                ) AS completed_at,
                mq.approval_stage
            FROM clean.audio_listening al
            INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
            LEFT JOIN app.user_account ua ON ua.user_id::text = al.assigned_to_user_id
            WHERE COALESCE(NULLIF(TRIM(al.assigned_to_user_id), ''), '') <> ''
            """
        )

    if normalized_queue in {"all", "callback"}:
        task_queries.append(
            f"""
            SELECT
                cb.case_id,
                {callback_group_expr} AS username,
                {callback_full_name_expr} AS full_name,
                cb.created_at AS assigned_at,
                cb.completed_at AS completed_at,
                mq.approval_stage
            FROM qc.callback_outcome cb
            INNER JOIN mart.main_case_queue mq ON mq.case_id = cb.case_id
            LEFT JOIN app.user_account ua ON ua.user_id = cb.assigned_to_user_id
            WHERE cb.assigned_to_user_id IS NOT NULL
            """
        )

    if not task_queries:
        return []

    query = "\nUNION ALL\n".join(task_queries)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return [dict(row) for row in cur.fetchall()]


def summarize_main_qc_status_task_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    canceled_statuses = {"rejected", "reviewed_rejected", "cancelled", "canceled"}
    approved_statuses = {"approved", "reviewed_approved"}

    for row in rows:
        username = str(row.get("username") or "").strip() or "Unknown"
        full_name = str(row.get("full_name") or "").strip()
        item = summary.setdefault(
            username,
            {
                "username": username,
                "full_name": full_name,
                "total_pushed": 0,
                "approved": 0,
                "pending": 0,
                "canceled": 0,
                "completed": 0,
                "_status_case_ids": set(),
            },
        )
        if full_name and not item["full_name"]:
            item["full_name"] = full_name
        status = str(row.get("approval_stage") or "").strip().lower()
        case_id = str(row.get("case_id") or "").strip()
        item["total_pushed"] += 1
        if case_id and case_id in item["_status_case_ids"]:
            continue
        if case_id:
            item["_status_case_ids"].add(case_id)
        if status in approved_statuses:
            item["approved"] += 1
            item["completed"] += 1
        elif status in canceled_statuses:
            item["canceled"] += 1
            item["completed"] += 1
        else:
            item["pending"] += 1

    values = []
    for item in summary.values():
        item.pop("_status_case_ids", None)
        values.append(item)

    return sorted(
        values,
        key=lambda item: (-int(item.get("total_pushed") or 0), str(item.get("username") or "").lower()),
    )


def get_main_qc_productivity_status_totals(settings: Settings, queue: str = "all") -> dict[str, int]:
    normalized_queue = normalize_qc_productivity_queue(queue)
    approved_statuses = ("approved", "reviewed_approved")
    canceled_statuses = ("rejected", "reviewed_rejected", "cancelled", "canceled")

    if normalized_queue == "audio":
        source_sql = """
            SELECT DISTINCT mq.case_id, mq.approval_stage
            FROM clean.audio_listening al
            INNER JOIN mart.main_case_queue mq ON mq.case_id = al.case_id
            WHERE COALESCE(NULLIF(TRIM(al.assigned_to_user_id), ''), '') <> ''
        """
        params: list[Any] = []
    elif normalized_queue == "callback":
        source_sql = """
            SELECT DISTINCT mq.case_id, mq.approval_stage
            FROM qc.callback_outcome cb
            INNER JOIN mart.main_case_queue mq ON mq.case_id = cb.case_id
            WHERE cb.assigned_to_user_id IS NOT NULL
        """
        params = []
    else:
        source_sql = """
            SELECT mq.case_id, mq.approval_stage
            FROM mart.main_case_queue mq
        """
        params = []

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH scoped AS (
                    {source_sql}
                )
                SELECT
                    COUNT(DISTINCT case_id)::int AS total_cases,
                    COUNT(DISTINCT case_id) FILTER (
                        WHERE LOWER(COALESCE(NULLIF(TRIM(approval_stage), ''), 'pending')) = ANY(%s)
                    )::int AS approved,
                    COUNT(DISTINCT case_id) FILTER (
                        WHERE LOWER(COALESCE(NULLIF(TRIM(approval_stage), ''), 'pending')) = ANY(%s)
                    )::int AS canceled
                FROM scoped
                """,
                [*params, list(approved_statuses), list(canceled_statuses)],
            )
            row = cur.fetchone() or {}
    total_cases = int(row.get("total_cases") or 0)
    approved = int(row.get("approved") or 0)
    canceled = int(row.get("canceled") or 0)
    return {
        "totalCases": total_cases,
        "approved": approved,
        "pending": max(total_cases - approved - canceled, 0),
        "canceled": canceled,
    }


def get_main_qc_productivity(settings: Settings, user: AuthUser, queue: str = "all", group_by: str = "qc_user") -> list[dict[str, Any]]:
    normalized_queue = normalize_qc_productivity_queue(queue)
    normalized_group = _normalize_main_qc_productivity_group(group_by)
    return summarize_main_qc_status_task_rows(_fetch_main_qc_task_rows(settings, normalized_queue, normalized_group))


def get_main_qc_productivity_by_date(settings: Settings, user: AuthUser, queue: str = "all", group_by: str = "qc_user") -> dict[str, Any]:
    normalized_queue = normalize_qc_productivity_queue(queue)
    normalized_group = _normalize_main_qc_productivity_group(group_by)
    return build_qc_productivity_by_date(_fetch_main_qc_task_rows(settings, normalized_queue, normalized_group))


# ---------------------------------------------------------------------------
# Case Deletion (admin only - soft delete)
# ---------------------------------------------------------------------------

def delete_main_case(settings: Settings, user: AuthUser, submission_key: str) -> dict[str, Any]:
    # Soft delete: insert into deleted_main_cases table instead of hard delete
    query = """
        INSERT INTO clean.deleted_main_cases (submission_key, case_id, deleted_by, deleted_at)
        SELECT submission_key, case_id, %s, now()
        FROM clean.main_case
        WHERE submission_key = %s
        ON CONFLICT (submission_key) DO NOTHING
        RETURNING id
    """

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (user.username, submission_key))
            result = cur.fetchone()
            deleted = 1 if result else 0
            conn.commit()

    _clear_main_case_list_cache()
    return {"deleted": deleted, "submission_key": submission_key}


def bulk_delete_main_cases(settings: Settings, user: AuthUser, submission_keys: list[str], reason: str) -> dict[str, Any]:
    if not submission_keys:
        raise HTTPException(status_code=400, detail="No submission keys provided.")
    if len(submission_keys) > 200:
        raise HTTPException(status_code=400, detail="Bulk limit is 200 cases per request.")
    reason_text = str(reason or "").strip()
    if len(reason_text) < 3:
        raise HTTPException(status_code=400, detail="Reason for deleting case(s) is required.")

    deleted = 0
    not_found: list[str] = []

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            for sk in submission_keys:
                cur.execute("""
                    INSERT INTO clean.deleted_main_cases (submission_key, case_id, deleted_by, deleted_at, reason)
                    SELECT submission_key, case_id, %s, now(), %s
                    FROM clean.main_case
                    WHERE submission_key = %s
                    ON CONFLICT (submission_key) DO NOTHING
                """, (user.username, reason_text, sk))
                if cur.rowcount > 0:
                    deleted += 1
                else:
                    # Check if it exists
                    cur.execute("SELECT 1 FROM clean.deleted_main_cases WHERE submission_key = %s", (sk,))
                    if not cur.fetchone():
                        not_found.append(sk)

        conn.commit()

    _clear_main_case_list_cache()
    return {"deleted": deleted, "notFound": not_found}
