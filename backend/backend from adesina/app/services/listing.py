from __future__ import annotations

import json
import logging
import math
import re
import time
import zipfile
from collections import Counter
from statistics import median
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import pandas as pd
import pyreadstat
import psycopg
from openpyxl import Workbook
from fastapi import HTTPException
from psycopg import sql

from backend.app.auth import AuthUser, EDIT_ROLES
from backend.app.database import db_connection
from backend.app.etl_bridge import describe_sync_failure, run_listing_sync_job
from backend.app.services.export_indicators import apply_multiselect_yes_no_indicators
from backend.app.services.qc_productivity import (
    build_qc_productivity_by_date,
    normalize_qc_productivity_queue,
    summarize_qc_task_rows,
)
from backend.app.settings import Settings
from survey_platform.config import load_listing_pipeline_config
from survey_platform.db import clear_manual_sync_override, request_manual_sync_override


logger = logging.getLogger(__name__)


LISTING_REVIEW_DECISION_ROLES = EDIT_ROLES
LISTING_FINAL_STATUSES = {"approved", "rejected"}
AUTO_APPROVED_CORRECTION_NOTE = "Correction submitted and automatically applied."


def _is_transient_db_lock_error(exc: BaseException) -> bool:
    """Return True for PostgreSQL errors that are safe to retry."""
    return isinstance(exc, psycopg.Error) and getattr(exc, "sqlstate", None) in {"40P01", "55P03", "57014"}


def _sleep_before_db_retry(attempt: int) -> None:
    # Small backoff keeps concurrent reviewers from immediately colliding again.
    time.sleep(min(0.25 * (2 ** attempt), 2.0))


def _sync_step_message(label: str, payload: dict[str, Any] | None) -> str:
    if not payload:
        return f"{label}: no status returned."

    status = str(payload.get("status") or "unknown")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    reason = payload.get("reason")

    if isinstance(result, dict) and result.get("message"):
        return f"{label}: {result['message']}"
    if isinstance(result, dict) and result.get("reason"):
        return f"{label}: {result['reason']}"
    if reason:
        return f"{label}: {reason}"
    if status == "success":
        return f"{label}: completed."
    if status == "warning":
        return f"{label}: completed with warnings."
    if status == "upstream_busy":
        return f"{label}: SurveyCTO is already serving another request."
    return f"{label}: {status}."


def _build_combined_sync_message(sync_result: dict[str, Any]) -> str:
    listing_status = str(sync_result.get("listing", {}).get("status") or "unknown")
    main_status = str(sync_result.get("main", {}).get("status") or "unknown")
    statuses = {listing_status, main_status}

    if "failed" in statuses:
        prefix = "Sync finished with failures."
    elif "warning" in statuses or "skipped" in statuses or "preempted" in statuses or "upstream_busy" in statuses:
        prefix = "Sync finished with warnings."
    else:
        prefix = "Listing + Main Survey sync completed."

    details = " ".join(
        [
            _sync_step_message("Listing", sync_result.get("listing")),
            _sync_step_message("Main Survey", sync_result.get("main")),
        ]
    )
    return f"{prefix} {details}".strip()


# Exact column order, SPSS 8-char names, and variable labels for HH listing long export.
# Sourced from HH_listing_Export_Template.sav.  Extra columns are excluded; missing
# columns are inserted as null so the structure always matches the template.
LISTING_LONG_TEMPLATE: list[tuple[str, str, str]] = [
    # (spss_name,  variable_name,                   label)
    ("COMPLETI", "CompletionDate",               "CompletionDate"),
    ("SUBMISSI", "SubmissionDate",               "SubmissionDate"),
    ("START",    "start",                        "start"),
    ("END",      "end",                          "end"),
    ("DEVICEID", "deviceid",                     "deviceid"),
    ("DEVICEPH", "devicephonenum",               "devicephonenum"),
    ("USERNAME", "username",                     "username"),
    ("DEVICE_I", "device_info",                  "device_info"),
    ("DURATION", "duration",                     "duration"),
    ("CASEID",   "caseid",                       "caseid"),
    ("ENUMERAT", "enumerator_id",                "Enumerator ID"),
    ("EA_ID",    "ea_id",                        "Enumeration Area (EA) ID"),
    ("EA_NAME",  "ea_name",                      "EA Name"),
    ("LGA_NAME", "lga_name",                     "LGA"),
    ("STATE_NA", "state_name",                   "State"),
    ("POLYGON",  "polygon_id",                   "Polygon id"),
    ("SAMPLE_T", "sample_type",                  "Sampling Source Type"),
    ("INTA",     "inta",                         "First Interviewer"),
    ("INTB",     "intb",                         "Second Interviewer"),
    ("LOC_CLAS", "loc_class",                    "Location Classification"),
    ("NBLD",     "NBLD",                         "Number of dwelling structures (auto)"),
    ("FORMDEF",  "formdef_version",              "formdef_version"),
    ("KEY",      "KEY",                          "KEY"),
    ("BLD_SERI", "bld_serial",                   "bld_serial"),
    ("BLD_LAST", "bld_last_another",             "bld_last_another"),
    ("STRUCTUR", "structure_no",                 "Structure or Building identifier"),
    ("ADDRESS",  "address_building",             "Address of Building"),
    ("USE_BUIL", "use_building",                 "Use of Building"),
    ("BUILDING", "building_photo",               "Capture building photo"),
    ("BLD_GPS",  "bld_gps",                      "Non Residential Building GPS"),
    ("HH_COUNT", "hh_count",                     "How many households are in this dwelling structure?"),
    ("IS_RESID", "is_residential",               "Is the building Residential or Dwelling Structure?"),
    ("BLD_ANOT", "bld_another",                  "Do you want to add another dwelling structure/building in this EA?"),
    ("V34_A",    "bld_another_here",             "bld_another_here"),
    ("IS_ELIGI", "is_eligible",                  "Is the building eligible for sampling?"),
    ("HH_INDEX", "hh_index",                     "hh_index"),
    ("HH_UID",   "hh_uid",                       "hh_uid"),
    ("HH_BLD_S", "hh_bld_serial",               "Building serial"),
    ("HH_HH_SE", "hh_hh_serial",                "Household serial"),
    ("HH_STRUC", "hh_structure_no",              "Structure ID"),
    ("HH_ADDRE", "hh_address_building",          "Building address"),
    ("HH_GPS",   "hh_gps",                       "Record GPS point"),
    ("HOUSEHOL", "household_no",                 "Household Serial Number"),
    ("HH_HEAD",  "hh_head_name",                 "Name of Head of Household"),
    ("NO_OF_MA", "no_of_male_less_15yrs",        "Number of Male in the household less than 15yrs"),
    ("NO_OF_FE", "no_of_female_less_15yrs",      "Number of Female in the household less than 15yrs"),
    ("TOTAL_LE", "total_less_15yrs",             "Total Household member less than 15 years"),
    ("V48_A",    "no_of_male_15_17yrs",          "Number of Male in the household 15-17 years"),
    ("V49_A",    "no_of_female_15_17yrs",        "Number of Female in the household 15-17 years"),
    ("TOTAL_15", "total_15_17yrs",               "Total Member 15-17years"),
    ("V51_A",    "no_of_male_18yrs_plus",        "Number of Male in the household 18 Years & Above"),
    ("V52_A",    "no_of_female_18yrs_plus",      "Number of Female in the household 18 Years & Above"),
    ("TOTAL_18", "total_18yrs_plus",             "Total Household member 18 years & Above"),
    ("TOTAL_MA", "total_male",                   "Total Male Household Members"),
    ("TOTAL_FE", "total_female",                 "Total Female Household Members"),
    ("TOTAL_HO", "total_household_size",         "Total Household Size"),
    ("HH_PHONE", "hh_phone_no_consent",          "To support the main data collection exercise, we would like to collect the phone number of the head of household so our team can contact you later"),
    ("V58_A",    "household_phone_no",           "Phone number of Head of HH or any Adult in HH"),
    ("V59_A",    "household_respondent",         "Who responded for this household?"),
    ("V60_A",    "household_respondent_oth",     "Other househod respondent specify"),
    ("HH_ENT",   "hh_ent",                       "Does any member of the household own an Enterprise?"),
    ("V62_A",    "building_no",                  "Building loop number"),
    ("V63_A",    "household_no_within_building", "Household loop number within building"),
    ("ROW_TYPE", "row_type",                     "Row type"),
    ("LISTING",  "listing_join_key",             "Listing join key"),
    ("GPS_SOUR", "gps_source",                   "GPS source"),
    ("GPS_LAT",  "gps_lat",                      "GPS latitude"),
    ("GPS_LONG", "gps_long",                     "GPS longitude"),
    ("SAMPLE_F", "sample_flag",                  "Selected household flag"),
    ("SAMPLE_S", "sample_status",                "Sample selection status"),
    ("SAMPLE_C", "sample_case_id",               "Sample case ID"),
    ("SAMPL0",   "sample_case_label",            "Sample case label"),
    ("STATUS",   "approval_status",              "Approval Status"),
]

SAMPLING_EA_COLUMN_ORDER: list[str] = [
    "CompletionDate", "SubmissionDate", "start", "end", "deviceid", "devicephonenum",
    "username", "device_info", "duration", "caseid", "enumerator_id", "ea_id", "ea_name",
    "lga_name", "state_name", "polygon_id", "sample_type", "inta", "intb", "loc_class",
    "NBLD", "formdef_version", "KEY", "overall_structures", "n_residential_structure",
    "n_non_residential_structure", "overall_hh_listed", "overall_eligible", "n_total_hh",
    "n_eligible", "eligible_list", "target_main", "target_reserve", "n_main", "n_reserve",
    "n_select_total", "interval", "max_start", "rand", "rand_start",
    "main_1", "main_2", "main_3", "main_4", "main_5", "main_6", "main_7", "main_8",
    "main_9", "main_10", "main_11", "main_12", "main_13", "main_14", "main_15",
    "main_bld_1", "main_hh_1", "main_bld_2", "main_hh_2", "main_bld_3", "main_hh_3",
    "main_bld_4", "main_hh_4", "main_bld_5", "main_hh_5", "main_bld_6", "main_hh_6",
    "main_bld_7", "main_hh_7", "main_bld_8", "main_hh_8", "main_bld_9", "main_hh_9",
    "main_bld_10", "main_hh_10", "main_bld_11", "main_hh_11", "main_bld_12", "main_hh_12",
    "main_bld_13", "main_hh_13", "main_bld_14", "main_hh_14", "main_bld_15", "main_hh_15",
    "remaining_list_space", "max_repl_offset", "repl_offset",
    "repl_1", "repl_2", "repl_3", "repl_4", "repl_5", "repl_6", "repl_7", "repl_8",
    "repl_9", "repl_10", "repl_11", "repl_12", "repl_13", "repl_14", "repl_15",
    "repl_bld_1", "repl_hh_1", "repl_bld_2", "repl_hh_2", "repl_bld_3", "repl_hh_3",
    "repl_bld_4", "repl_hh_4", "repl_bld_5", "repl_hh_5", "repl_bld_6", "repl_hh_6",
    "repl_bld_7", "repl_hh_7", "repl_bld_8", "repl_hh_8", "repl_bld_9", "repl_hh_9",
    "repl_bld_10", "repl_hh_10", "repl_bld_11", "repl_hh_11", "repl_bld_12", "repl_hh_12",
    "repl_bld_13", "repl_hh_13", "repl_bld_14", "repl_hh_14", "repl_bld_15", "repl_hh_15",
    "main_hh_selected", "repl_hh_selected",
]

SELECTED_LONG_COLUMN_ORDER: list[str] = [
    "CompletionDate", "SubmissionDate", "start", "end", "deviceid", "devicephonenum",
    "username", "device_info", "duration", "caseid", "enumerator_id", "ea_id", "ea_name",
    "lga_name", "state_name", "polygon_id", "sample_type", "inta", "intb", "loc_class",
    "NBLD", "formdef_version", "KEY", "slot", "slot_type", "sel_hh_index", "sel_bld_index",
    "sel_hhold_index", "sel_structure_no", "sel_hh_head_name", "sel_address_building",
    "sel_GPS", "sel_household_phone_no", "sel_total_15_17yrs", "sel_total_18yrs_plus",
    "sel_total_male", "sel_total_female", "sel_total_household_size", "sel_ea_id",
    "sel_ea_name", "sel_state_name", "sel_lga_name", "sel_sample_type", "sel_loc_class",
    "formids", "users", "case_id", "sel_building_serial", "sel_household_serial",
    "case_label", "selected_repeat_no", "selected_join_key",
]

LISTING_STATUSES = ["submitted", "pending_review", "in_review", "corrected", "approved", "rejected"]
EDITABLE_TABLES = {
    "clean.hh_sampling_ea": "submission_key",
    "clean.hh_listing_long": "listing_row_id",
    "clean.hh_selected_long": "selected_id",
}
STRUCTURED_FIELDS = {
    "clean.hh_sampling_ea": {"approval_status", "ea_id", "boundary_id", "interviewer_id", "supervisor_id"},
    "clean.hh_listing_long": {
        "approval_status",
        "ea_id",
        "boundary_id",
        "interviewer_id",
        "supervisor_id",
        "listing_join_key",
        "selected_join_key",
        "sample_case_id",
        "household_uid",
        "row_type",
        "sample_flag",
        "gps_lat",
        "gps_long",
        "gps_source",
    },
    "clean.hh_selected_long": {"selected_join_key", "sample_case_id", "sample_case_label", "slot_type"},
}
RULE_DEFINITIONS = [
    ("LISTING_LOW_LOI", "listing", "clean.hh_sampling_ea", "record", "high", "python", "Interview duration is below 50% of listing median LOI.", "flag_for_review"),
    ("LISTING_HIGH_LOI", "listing", "clean.hh_sampling_ea", "record", "high", "python", "Interview duration is above 150% of listing median LOI.", "flag_for_review"),
    ("LISTING_START_TIME", "listing", "clean.hh_sampling_ea", "record", "high", "python", "Interview occurred during odd hours (7:00 PM to 6:59 AM).", "flag_for_review"),
    ("LISTING_DUPLICATE_PHONE_NUMBER", "listing", "clean.hh_sampling_ea", "household_phone_no", "high", "python", "Respondent phone number is duplicated within the same interviewer.", "flag_for_review"),
    ("LISTING_DUPLICATE_GPS", "listing", "clean.hh_listing_long", "gps_lat", "high", "python", "Identical GPS coordinates appear in another listing interview by the same interviewer.", "flag_for_review"),
    ("LISTING_GAP_BETWEEN_2_INTERVIEWS", "listing", "clean.hh_sampling_ea", "record", "high", "python", "Gap between consecutive interviews by the same interviewer is below 5 minutes.", "flag_for_review"),
    ("LISTING_TIME_INTERWOVEN", "listing", "clean.hh_sampling_ea", "record", "high", "python", "Two interviews by the same interviewer overlap by more than 1 minute.", "flag_for_review"),
    ("LISTING_STRAIGHTLINING", "listing", "clean.hh_listing_long", "record", "high", "python", "Same response selected on 80% or more of eligible matrix items.", "flag_for_review"),
    ("LISTING_INSUFFICIENT_VALID_GPS", "listing", "clean.hh_listing_long", "gps_lat", "high", "python", "EA cannot be spatially auto-approved because 100% of listing points do not have valid non-zero GPS.", "flag_for_review"),
    ("LISTING_OUTSIDE_POLYGON", "listing", "clean.hh_listing_long", "gps_lat", "high", "python", "One or more listing GPS points fall outside the assigned EA polygon.", "flag_for_review"),
    ("LISTING_LOW_POLYGON_COVERAGE", "listing", "clean.hh_listing_long", "gps_lat", "high", "python", "Listing GPS points are too concentrated to satisfy polygon grid coverage or sparse-spread thresholds.", "flag_for_review"),
    ("LISTING_NO_OF_MALE_LESS_15YRS", "listing", "clean.hh_listing_long", "no_of_male_less_15yrs", "medium", "python", "no_of_male_less_15yrs is greater than 10.", "flag_for_review"),
    ("LISTING_NO_OF_FEMALE_LESS_15YRS", "listing", "clean.hh_listing_long", "no_of_female_less_15yrs", "medium", "python", "no_of_female_less_15yrs is greater than 10.", "flag_for_review"),
    ("LISTING_TOTAL_LESS_15YRS", "listing", "clean.hh_listing_long", "total_less_15yrs", "medium", "python", "total_less_15yrs is greater than 10.", "flag_for_review"),
    ("LISTING_NO_OF_MALE_15_17YRS", "listing", "clean.hh_listing_long", "no_of_male_15_17yrs", "medium", "python", "no_of_male_15_17yrs is greater than 10.", "flag_for_review"),
    ("LISTING_NO_OF_FEMALE_15_17YRS", "listing", "clean.hh_listing_long", "no_of_female_15_17yrs", "medium", "python", "no_of_female_15_17yrs is greater than 10.", "flag_for_review"),
    ("LISTING_TOTAL_15_17YRS", "listing", "clean.hh_listing_long", "total_15_17yrs", "medium", "python", "total_15_17yrs is greater than 10.", "flag_for_review"),
    ("LISTING_NO_OF_MALE_18YRS_PLUS", "listing", "clean.hh_listing_long", "no_of_male_18yrs_plus", "medium", "python", "no_of_male_18yrs_plus is greater than 10.", "flag_for_review"),
    ("LISTING_NO_OF_FEMALE_18YRS_PLUS", "listing", "clean.hh_listing_long", "no_of_female_18yrs_plus", "medium", "python", "no_of_female_18yrs_plus is greater than 10.", "flag_for_review"),
    ("LISTING_TOTAL_18YRS_PLUS", "listing", "clean.hh_listing_long", "total_18yrs_plus", "medium", "python", "total_18yrs_plus is greater than 10.", "flag_for_review"),
    ("LISTING_TOTAL_MALE", "listing", "clean.hh_listing_long", "total_male", "medium", "python", "total_male is greater than 10.", "flag_for_review"),
    ("LISTING_TOTAL_FEMALE", "listing", "clean.hh_listing_long", "total_female", "medium", "python", "total_female is greater than 10.", "flag_for_review"),
    ("LISTING_TOTAL_HOUSEHOLD_SIZE", "listing", "clean.hh_listing_long", "total_household_size", "medium", "python", "total_household_size is greater than 10.", "flag_for_review"),
]
EA_COVERAGE_RULE_CODES = (
    "LISTING_INSUFFICIENT_VALID_GPS",
    "LISTING_OUTSIDE_POLYGON",
    "LISTING_LOW_POLYGON_COVERAGE",
    "LISTING_DUPLICATE_GPS",
)
def _determine_coverage_approval_status(current_status: str | None, can_auto_approve: bool) -> str | None:
    normalized_status = str(current_status or "").strip().lower()
    if normalized_status == "rejected":
        return None
    target_status = "approved" if can_auto_approve else "pending_review"
    return target_status if target_status != normalized_status else None

NIGERIA_GPS_BOUNDS = {"lat_min": 4.0, "lat_max": 14.5, "lon_min": 2.5, "lon_max": 15.0}
# Thresholds referenced by QC rules
QC_THRESHOLDS = {
    "spatial_auto_approval_min_points": 10,
    "spatial_auto_approval_required_valid_gps_ratio": 1.0,
    "spatial_auto_approval_required_grid_coverage_ratio": 0.8,
}
LISTING_INTERVIEW_MIN_MINUTES = 5
LISTING_INTERVIEW_MAX_MINUTES = 120
LISTING_MIN_GAP_BETWEEN_INTERVIEWS_MINUTES = 3
LISTING_EXPORT_TEMPLATE_SAV = "HH_listing_Export_Template.sav"
LISTING_PHONE_FIELDS = (
    "household_phone_no",
    "devicephonenum",
    "phone.no",
    "phone_no",
    "phoneno",
    "respondent_phone",
)
LISTING_NUMERIC_FIELD_MINIMUMS: dict[str, int] = {
    "NBLD": 0,
    "bld_serial": 1,
    "bld_last_another": 0,
    "hh_count": 0,
    "hh_index": 1,
    "hh_bld_serial": 1,
    "hh_hh_serial": 1,
    "household_no": 1,
    "building_no": 1,
    "household_no_within_building": 1,
    "no_of_male_less_15yrs": 0,
    "no_of_female_less_15yrs": 0,
    "total_less_15yrs": 0,
    "no_of_male_15_17yrs": 0,
    "no_of_female_15_17yrs": 0,
    "total_15_17yrs": 0,
    "no_of_male_18yrs_plus": 0,
    "no_of_female_18yrs_plus": 0,
    "total_18yrs_plus": 0,
    "total_male": 0,
    "total_female": 0,
    "total_household_size": 1,
}
LISTING_NON_INTEGER_NUMERIC_FIELDS = {
    "gps_lat",
    "gps_long",
}
SCHEDULED_EXPORT_DATASETS = ("listing_long", "sampling_ea", "selected_long")
SCHEDULED_EXPORT_FORMATS = ("xlsx", "csv", "sav")
LISTING_EXPORT_DICTIONARY_FILE = "Listing export dictionary.xlsx"
LEGACY_DICTIONARY_ALLOWED_TYPES = {"string", "numeric", "date"}
LEGACY_VALUE_LABEL_PATTERN = re.compile(r"\{\s*([^,{}]+?)\s*,\s*([^{}]+?)\s*\}")


def _serialize_record(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=True)


def _normalize_legacy_choice_code(value: str) -> str:
    text = str(value).strip()
    if not text:
        return text
    try:
        numeric = float(text)
    except ValueError:
        return text
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _infer_listing_numeric_minimum(field_name: str) -> int:
    explicit = LISTING_NUMERIC_FIELD_MINIMUMS.get(field_name)
    if explicit is not None:
        return explicit

    normalized = field_name.lower()
    positive_markers = (
        "serial",
        "index",
        "repeat_no",
        "building_no",
        "household_no",
        "hh_bld",
        "hh_hh",
    )
    if any(marker in normalized for marker in positive_markers):
        return 1
    return 0


def _parse_legacy_value_labels(value: Any) -> list[tuple[str, str]]:
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return []

    parsed: list[tuple[str, str]] = []
    for code, label in LEGACY_VALUE_LABEL_PATTERN.findall(text):
        normalized_code = _normalize_legacy_choice_code(code)
        cleaned_label = str(label).strip().rstrip(".")
        if normalized_code and cleaned_label:
            parsed.append((normalized_code, cleaned_label))
    return parsed


def bootstrap_listing_export_dictionary(settings: Settings) -> dict[str, Any]:
    dictionary_path = settings.root_dir / LISTING_EXPORT_DICTIONARY_FILE
    if not dictionary_path.exists():
        return {"status": "missing", "path": str(dictionary_path)}

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM reference.xlsform_question
                WHERE instrument_code = 'listing'
                """
            )
            existing_count = int(cur.fetchone()["count"] or 0)

            if existing_count > 0:
                return {"status": "skipped", "reason": "listing dictionary already populated", "question_count": existing_count}

            dictionary_df = pd.read_excel(dictionary_path).fillna("")
            dictionary_df.columns = [str(column).strip() for column in dictionary_df.columns]
            required_columns = {"Name", "Type", "Label", "Values"}
            if not required_columns.issubset(set(dictionary_df.columns)):
                missing = ", ".join(sorted(required_columns.difference(set(dictionary_df.columns))))
                raise RuntimeError(f"Listing export dictionary is missing required columns: {missing}")

            questions: list[tuple[str, str, str, str | None, str | None]] = []
            choices: list[tuple[str, str, str, str, int]] = []
            seen_variables: set[str] = set()
            seen_choices: set[tuple[str, str]] = set()

            for row in dictionary_df.to_dict(orient="records"):
                variable_name = str(row.get("Name", "")).strip()
                question_type = str(row.get("Type", "")).strip().lower()
                question_label = str(row.get("Label", "")).strip()
                raw_values = row.get("Values", "")

                if not variable_name or question_type not in LEGACY_DICTIONARY_ALLOWED_TYPES:
                    continue
                if variable_name in seen_variables:
                    continue

                value_labels = _parse_legacy_value_labels(raw_values)
                choice_list_name = variable_name if value_labels else None
                questions.append(
                    (
                        "listing",
                        variable_name,
                        question_type,
                        question_label or variable_name,
                        choice_list_name,
                    )
                )
                seen_variables.add(variable_name)

                for sort_order, (choice_code, choice_label) in enumerate(value_labels, start=1):
                    choice_key = (variable_name, choice_code)
                    if choice_key in seen_choices:
                        continue
                    choices.append(("listing", variable_name, choice_code, choice_label, sort_order))
                    seen_choices.add(choice_key)

            if not questions:
                return {"status": "skipped", "reason": "no importable dictionary rows"}

            cur.executemany(
                """
                INSERT INTO reference.xlsform_question (
                    instrument_code,
                    variable_name,
                    question_type,
                    question_label,
                    choice_list_name
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (instrument_code, variable_name) DO UPDATE SET
                    question_type = EXCLUDED.question_type,
                    question_label = EXCLUDED.question_label,
                    choice_list_name = EXCLUDED.choice_list_name
                """,
                questions,
            )

            if choices:
                cur.executemany(
                    """
                    INSERT INTO reference.xlsform_choice (
                        instrument_code,
                        list_name,
                        choice_code,
                        choice_label,
                        sort_order
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_code, list_name, choice_code) DO UPDATE SET
                        choice_label = EXCLUDED.choice_label,
                        sort_order = EXCLUDED.sort_order
                    """,
                    choices,
                )
        conn.commit()

    return {
        "status": "imported",
        "path": str(dictionary_path),
        "question_count": len(questions),
        "choice_count": len(choices),
    }


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
    lowered = str(value).strip().lower()
    if field_name in {"sample_flag"}:
        return lowered in {"1", "true", "yes"}
    if field_name in {"gps_lat", "gps_long"}:
        try:
            return float(value)
        except ValueError:
            return None
    return value


def _safe_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_phone(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    digits = re.sub(r"\D+", "", str(value))
    if len(digits) < 7:
        return None
    return digits


def _extract_listing_phone_candidates(
    case_record: dict[str, Any],
    listing_rows: list[dict[str, Any]],
) -> list[str]:
    phones: list[str] = []
    for key in LISTING_PHONE_FIELDS:
        normalized = _normalize_phone(case_record.get(key))
        if normalized:
            phones.append(normalized)
    for row in listing_rows:
        rec = row.get("record") or {}
        if not isinstance(rec, dict):
            continue
        for key in LISTING_PHONE_FIELDS:
            normalized = _normalize_phone(rec.get(key))
            if normalized:
                phones.append(normalized)
    return phones


@lru_cache(maxsize=1)
def _listing_template_numeric_fields(template_path: str) -> set[str]:
    _, meta = pyreadstat.read_sav(template_path, row_limit=0)
    numeric_cols: set[str] = set()
    original_types = getattr(meta, "original_variable_types", {}) or {}
    for col_name in (meta.column_names or []):
        raw_type = str(original_types.get(col_name, "")).lower()
        if any(token in raw_type for token in ("f", "n", "num", "double", "float")):
            numeric_cols.add(str(col_name))
    return numeric_cols


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coord_key(lat: float, lon: float, precision: int = 6) -> tuple[float, float]:
    return (round(lat, precision), round(lon, precision))


def _iter_geometry_points(geometry: dict[str, Any] | None) -> list[tuple[float, float]]:
    if not geometry:
        return []
    coords = geometry.get("coordinates")
    if not coords:
        return []
    points: list[tuple[float, float]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, list):
            return
        if len(node) >= 2 and all(isinstance(item, (int, float)) for item in node[:2]):
            points.append((float(node[0]), float(node[1])))
            return
        for child in node:
            visit(child)

    visit(coords)
    return points


def _geometry_bounds(geometry: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    points = _iter_geometry_points(geometry)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_area(bounds: tuple[float, float, float, float] | None) -> float:
    if not bounds:
        return 0.0
    min_x, min_y, max_x, max_y = bounds
    return max(max_x - min_x, 0.0) * max(max_y - min_y, 0.0)


def _bounds_from_points(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))

def _point_on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    cross = (py - ay) * (bx - ax) - (px - ax) * (by - ay)
    if abs(cross) > 1e-9:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < 0:
        return False
    squared_len = (bx - ax) ** 2 + (by - ay) ** 2
    return dot <= squared_len


def _point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    if len(ring) < 3:
        return False
    x_value, y_value = point
    inside = False
    prev_x, prev_y = ring[-1]
    for curr_x, curr_y in ring:
        if _point_on_segment(x_value, y_value, prev_x, prev_y, curr_x, curr_y):
            return True
        intersects = ((curr_y > y_value) != (prev_y > y_value)) and (
            x_value < (prev_x - curr_x) * (y_value - curr_y) / ((prev_y - curr_y) or 1e-12) + curr_x
        )
        if intersects:
            inside = not inside
        prev_x, prev_y = curr_x, curr_y
    return inside


def _iter_polygon_rings(geometry: dict[str, Any] | None) -> list[list[list[tuple[float, float]]]]:
    if not isinstance(geometry, dict):
        return []
    geometry_type = str(geometry.get("type") or "").strip()
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []

    def normalize_ring(node: Any) -> list[tuple[float, float]]:
        ring: list[tuple[float, float]] = []
        if not isinstance(node, list):
            return ring
        for point in node:
            if isinstance(point, list) and len(point) >= 2 and all(isinstance(item, (int, float)) for item in point[:2]):
                ring.append((float(point[0]), float(point[1])))
        return ring

    if geometry_type == "Polygon":
        return [[normalize_ring(ring) for ring in coordinates if normalize_ring(ring)]]
    if geometry_type == "MultiPolygon":
        polygons: list[list[list[tuple[float, float]]]] = []
        for polygon in coordinates:
            if not isinstance(polygon, list):
                continue
            rings = [normalize_ring(ring) for ring in polygon if normalize_ring(ring)]
            if rings:
                polygons.append(rings)
        return polygons
    return []


def _point_in_geometry(point: tuple[float, float], geometry: dict[str, Any] | None) -> bool:
    for polygon in _iter_polygon_rings(geometry):
        exterior = polygon[0] if polygon else []
        holes = polygon[1:] if len(polygon) > 1 else []
        if exterior and _point_in_ring(point, exterior) and not any(_point_in_ring(point, hole) for hole in holes):
            return True
    return False


def _load_boundary_geometries(boundary_zip_path: str) -> dict[str, dict[str, Any]]:
    zip_path = Path(boundary_zip_path)
    if not zip_path.exists():
        return {}
    geometries: dict[str, dict[str, Any]] = {}
    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("output_geojson/") and name.endswith(".geojson")
        ]
        if not names:
            names = [name for name in archive.namelist() if name.endswith(".geojson")]
        for name in names:
            with archive.open(name) as handle:
                data = json.load(handle)
            for feature in data.get("features", []):
                props = feature.get("properties") or {}
                ea_id = str(props.get("sd_EA_ID") or "").strip()
                geometry = feature.get("geometry")
                if ea_id and isinstance(geometry, dict):
                    geometries[ea_id] = geometry
    return geometries


def _compute_polygon_grid_coverage(points: list[tuple[float, float]], geometry: dict[str, Any]) -> tuple[float, int, int]:
    bounds = _geometry_bounds(geometry)
    if not bounds or not points:
        return (0.0, 0, 0)
    min_x, min_y, max_x, max_y = bounds
    width = max(max_x - min_x, 0.0)
    height = max(max_y - min_y, 0.0)
    if width <= 0 or height <= 0:
        return (0.0, 0, 0)
    target_cell_count = max(1, int(math.ceil(len(points) / QC_THRESHOLDS["spatial_auto_approval_required_grid_coverage_ratio"])))
    cell_area = max(_bbox_area(bounds) / target_cell_count, 1e-12)
    cell_edge = math.sqrt(cell_area)
    cols = max(1, int(math.ceil(width / cell_edge)))
    rows = max(1, int(math.ceil(height / cell_edge)))
    cell_width = width / cols if cols else width
    cell_height = height / rows if rows else height
    usable_cells: set[tuple[int, int]] = set()
    for col in range(cols):
        for row in range(rows):
            center = (min_x + (col + 0.5) * cell_width, min_y + (row + 0.5) * cell_height)
            if _point_in_geometry(center, geometry):
                usable_cells.add((col, row))
    if not usable_cells:
        return (0.0, 0, 0)
    occupied_cells: set[tuple[int, int]] = set()
    for lon_value, lat_value in points:
        col = min(cols - 1, max(0, int((lon_value - min_x) / (cell_width or 1e-12))))
        row = min(rows - 1, max(0, int((lat_value - min_y) / (cell_height or 1e-12))))
        if (col, row) in usable_cells:
            occupied_cells.add((col, row))
    usable_count = len(usable_cells)
    occupied_count = len(occupied_cells)
    coverage_ratio = occupied_count / usable_count if usable_count else 0.0
    return (coverage_ratio, occupied_count, usable_count)


def _evaluate_sparse_spatial_evidence(
    points: list[tuple[float, float]],
    geometry: dict[str, Any] | None,
    settings: Settings,
) -> dict[str, Any]:
    geometry_bounds = _geometry_bounds(geometry)
    point_bounds = _bounds_from_points(points)
    geometry_bbox_area = _bbox_area(geometry_bounds)
    point_bbox_area = _bbox_area(point_bounds)
    bbox_coverage_ratio = (point_bbox_area / geometry_bbox_area) if geometry_bbox_area > 0 else 0.0
    quadrants_covered = _count_quadrants(points, geometry_bounds)
    unique_point_count = len(set(points))
    average_step_distance = _average_step_distance(points)
    min_point_count = max(int(settings.sparse_min_point_count), 1)
    min_unique_points = max(int(settings.sparse_min_unique_buildings), 1)
    min_bbox_ratio = max(float(settings.sparse_min_bbox_coverage_ratio), 0.0)
    min_quadrants = max(int(settings.sparse_min_quadrants), 1)
    static_max_step = max(float(settings.sparse_static_max_step_distance), 0.0)
    appears_static = (
        len(points) >= min_point_count
        and average_step_distance <= static_max_step
        and quadrants_covered < min_quadrants
    )
    spread_ok = (
        geometry is not None
        and len(points) >= min_point_count
        and unique_point_count >= min_unique_points
        and bbox_coverage_ratio >= min_bbox_ratio
        and quadrants_covered >= min_quadrants
        and not appears_static
    )
    return {
        "spread_ok": spread_ok,
        "bbox_coverage_ratio": bbox_coverage_ratio,
        "quadrants_covered": quadrants_covered,
        "unique_point_count": unique_point_count,
        "average_step_distance": average_step_distance,
        "appears_static": appears_static,
    }


def _average_step_distance(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    distances = [
        math.dist(points[index - 1], points[index])
        for index in range(1, len(points))
    ]
    return sum(distances) / len(distances)


def _count_quadrants(
    points: list[tuple[float, float]],
    bounds: tuple[float, float, float, float] | None,
) -> int:
    if not points or not bounds:
        return 0
    min_x, min_y, max_x, max_y = bounds
    mid_x = (min_x + max_x) / 2
    mid_y = (min_y + max_y) / 2
    quadrants = set()
    for x_value, y_value in points:
        quad_x = 0 if x_value < mid_x else 1
        quad_y = 0 if y_value < mid_y else 1
        quadrants.add((quad_x, quad_y))
    return len(quadrants)


@lru_cache(maxsize=1)
def _load_boundary_bounds(boundary_zip_path: str) -> dict[str, tuple[float, float, float, float]]:
    zip_path = Path(boundary_zip_path)
    if not zip_path.exists():
        return {}

    bounds_by_ea: dict[str, tuple[float, float, float, float]] = {}
    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("output_geojson/") and name.endswith(".geojson")
        ]
        if not names:
            names = [name for name in archive.namelist() if name.endswith(".geojson")]

        for name in names:
            with archive.open(name) as handle:
                data = json.load(handle)
            for feature in data.get("features", []):
                ea_id = str((feature.get("properties") or {}).get("sd_EA_ID") or "").strip()
                if not ea_id:
                    continue
                bounds = _geometry_bounds(feature.get("geometry"))
                if bounds:
                    bounds_by_ea[ea_id] = bounds
    return bounds_by_ea


@lru_cache(maxsize=1)
def _load_state_boundary_geojson(state_boundary_geojson_path: str) -> dict[str, Any]:
    geojson_path = Path(state_boundary_geojson_path)
    if not geojson_path.exists():
        return {"type": "FeatureCollection", "features": []}
    with geojson_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": []}
    return payload


def _normalize_boundary_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _normalize_boundary_text(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", " ", str(value or "").upper()).strip()
    return re.sub(r"\s+", " ", text)


def _feature_matches_boundary(
    feature: dict[str, Any],
    ea_id: str | None = None,
    boundary_id: str | None = None,
    ea_name: str | None = None,
    state_name: str | None = None,
    lga_name: str | None = None,
) -> bool:
    props = feature.get("properties") or {}
    candidates = {
        _normalize_boundary_key(props.get("sd_EA_ID")),
        _normalize_boundary_key(props.get("ea_id")),
        _normalize_boundary_key(props.get("EA_ID")),
        _normalize_boundary_key(props.get("boundary_id")),
        _normalize_boundary_key(props.get("sd_BOUNDARY_ID")),
    }
    candidates.discard("")
    targets = {_normalize_boundary_key(ea_id), _normalize_boundary_key(boundary_id)}
    targets.discard("")
    if candidates and targets and not candidates.isdisjoint(targets):
        return True

    name_target = _normalize_boundary_text(ea_name)
    if not name_target:
        return False

    name_candidates = {
        _normalize_boundary_text(props.get("sd_EA_NAME")),
        _normalize_boundary_text(props.get("name")),
        _normalize_boundary_text(props.get("ea_name")),
    }
    name_candidates.discard("")
    if name_target not in name_candidates:
        return False

    feature_state = _normalize_boundary_text(props.get("sd_STATE_NAME") or props.get("state") or props.get("state_name"))
    target_state = _normalize_boundary_text(state_name)
    if feature_state and target_state and feature_state != target_state:
        return False

    feature_lga = _normalize_boundary_text(props.get("sd_LGA_NAME") or props.get("lga_name"))
    target_lga = _normalize_boundary_text(lga_name)
    if feature_lga and target_lga and feature_lga != target_lga:
        return False

    return True


def _load_boundary_feature_from_zip(
    boundary_zip_path: str,
    ea_id: str | None = None,
    boundary_id: str | None = None,
    ea_name: str | None = None,
    state_name: str | None = None,
    lga_name: str | None = None,
) -> dict[str, Any] | None:
    zip_path = Path(boundary_zip_path)
    if not zip_path.exists():
        return None

    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("output_geojson/") and name.endswith(".geojson")
        ]
        if not names:
            names = [name for name in archive.namelist() if name.endswith(".geojson")]

        for name in names:
            with archive.open(name) as handle:
                data = json.load(handle)
            for feature in data.get("features", []):
                if _feature_matches_boundary(feature, ea_id, boundary_id, ea_name, state_name, lga_name):
                    geometry = feature.get("geometry")
                    if isinstance(geometry, dict):
                        properties = dict(feature.get("properties") or {})
                        if ea_id and not properties.get("sd_EA_ID"):
                            properties["sd_EA_ID"] = ea_id
                        if boundary_id and not properties.get("boundary_id"):
                            properties["boundary_id"] = boundary_id
                        return {
                            "type": "Feature",
                            "geometry": geometry,
                            "properties": properties,
                        }
    return None


def _has_postgis(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'reference'
              AND table_name = 'geo_boundaries_ea'
              AND column_name = 'geom'
            """
        )
        return cur.fetchone() is not None


def sync_boundaries_to_db(settings: Settings) -> dict[str, Any]:
    """Persist EA boundary polygons from the GeoJSON zip into reference.geo_boundaries_ea."""
    zip_path = Path(settings.boundary_zip_path)
    if not zip_path.exists():
        return {"loaded": 0, "error": "Boundary zip not found."}

    rows: list[tuple] = []
    seen: set[str] = set()

    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name for name in archive.namelist()
            if name.startswith("output_geojson/") and name.endswith(".geojson")
        ]
        if not names:
            names = [name for name in archive.namelist() if name.endswith(".geojson")]

        for name in names:
            with archive.open(name) as handle:
                data = json.load(handle)
            for feature in data.get("features", []):
                props = feature.get("properties") or {}
                ea_id = str(props.get("sd_EA_ID") or "").strip()
                if not ea_id or ea_id in seen:
                    continue
                seen.add(ea_id)
                bounds = _geometry_bounds(feature.get("geometry"))
                centroid_lat = centroid_long = None
                if bounds:
                    min_x, min_y, max_x, max_y = bounds
                    centroid_long = (min_x + max_x) / 2
                    centroid_lat = (min_y + max_y) / 2
                rows.append((
                    ea_id,
                    ea_id,  # boundary_id — use ea_id as stable key
                    str(props.get("sd_STATE_NAME") or ""),
                    str(props.get("sd_LGA_NAME") or ""),
                    str(props.get("sd_WARD_NAME") or props.get("sd_Ward_Name") or ""),
                    json.dumps(feature.get("geometry")),
                    centroid_lat,
                    centroid_long,
                    json.dumps(props),
                ))

    if not rows:
        return {"loaded": 0}

    with db_connection(settings) as conn:
        postgis = _has_postgis(conn)
        with conn.cursor() as cur:
            if postgis:
                for (ea_id, boundary_id, state_name, lga_name, ward_name, geom_json, centroid_lat, centroid_long, props_json) in rows:
                    cur.execute(
                        """
                        INSERT INTO reference.geo_boundaries_ea
                            (ea_id, boundary_id, state_name, lga_name, ward_name, geom, centroid, properties)
                        VALUES (%s, %s, %s, %s, %s, ST_GeomFromGeoJSON(%s), ST_Centroid(ST_GeomFromGeoJSON(%s)), %s::jsonb)
                        ON CONFLICT (ea_id) DO UPDATE SET
                            boundary_id    = EXCLUDED.boundary_id,
                            state_name     = EXCLUDED.state_name,
                            lga_name       = EXCLUDED.lga_name,
                            ward_name      = EXCLUDED.ward_name,
                            geom           = EXCLUDED.geom,
                            centroid       = EXCLUDED.centroid,
                            properties     = EXCLUDED.properties
                        """,
                        (ea_id, boundary_id, state_name, lga_name, ward_name, geom_json, geom_json, props_json),
                    )
            else:
                for (ea_id, boundary_id, state_name, lga_name, ward_name, geom_json, centroid_lat, centroid_long, props_json) in rows:
                    cur.execute(
                        """
                        INSERT INTO reference.geo_boundaries_ea
                            (ea_id, boundary_id, state_name, lga_name, ward_name,
                             geom_geojson, centroid_lat, centroid_long, properties)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                        ON CONFLICT (ea_id) DO UPDATE SET
                            boundary_id    = EXCLUDED.boundary_id,
                            state_name     = EXCLUDED.state_name,
                            lga_name       = EXCLUDED.lga_name,
                            ward_name      = EXCLUDED.ward_name,
                            geom_geojson   = EXCLUDED.geom_geojson,
                            centroid_lat   = EXCLUDED.centroid_lat,
                            centroid_long  = EXCLUDED.centroid_long,
                            properties     = EXCLUDED.properties
                        """,
                        (ea_id, boundary_id, state_name, lga_name, ward_name, geom_json, centroid_lat, centroid_long, props_json),
                    )
        conn.commit()

    return {"loaded": len(rows)}


def _ensure_status_allowed(new_status: str) -> None:
    if new_status not in LISTING_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unsupported status '{new_status}'.")


def _enforce_case_visibility(user: AuthUser, case_status: str) -> None:
    return None


def bootstrap_rule_definitions(settings: Settings) -> None:
    rule_codes = [rule[0] for rule in RULE_DEFINITIONS]
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE qc.rule_definition
                SET is_active = false
                WHERE instrument_code = 'listing'
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


def get_listing_overview(settings: Settings, user: AuthUser, state_name: str | list[str] | None = None) -> dict[str, Any]:
    state_names: list[str] = (
        [state_name] if isinstance(state_name, str)
        else (state_name or [])
    )

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            state_params: list[Any] = []
            state_clause = ""
            if state_names:
                placeholders = ", ".join(["%s"] * len(state_names))
                state_clause = f" AND LOWER(COALESCE(record->>'state_name', '')) IN ({placeholders})"
                state_params.extend(s.lower() for s in state_names)

            cur.execute(
                f"""
                SELECT
                    COUNT(*)::int AS total_cases,
                    COUNT(*) FILTER (WHERE approval_status = 'approved')::int AS approved_cases,
                    COUNT(*) FILTER (WHERE approval_status = 'pending_review')::int AS pending_review_cases,
                    COUNT(*) FILTER (WHERE approval_status = 'in_review')::int AS in_review_cases,
                    COUNT(*) FILTER (WHERE approval_status = 'corrected')::int AS corrected_cases,
                    COUNT(*) FILTER (WHERE approval_status = 'rejected')::int AS rejected_cases,
                    COUNT(*) FILTER (WHERE approval_status = 'submitted')::int AS submitted_cases
                FROM clean.hh_sampling_ea
                WHERE 1 = 1
                {state_clause}
                """,
                state_params,
            )
            status_counts = cur.fetchone() or {}

            listing_params: list[Any] = []
            listing_where = ["COALESCE(l.record->>'bld_last_another', '1') NOT IN ('0', '0.0')"]
            if state_names:
                placeholders = ", ".join(["%s"] * len(state_names))
                listing_where.append(f"LOWER(COALESCE(s.record->>'state_name', '')) IN ({placeholders})")
                listing_params.extend(s.lower() for s in state_names)
            listing_where_sql = f"WHERE {' AND '.join(listing_where)}" if listing_where else ""

            cur.execute(
                f"""
                SELECT
                    COUNT(DISTINCT CONCAT(l.submission_key, ':', l.building_no)) FILTER (WHERE l.building_no IS NOT NULL)::int AS buildings_listed,
                    COUNT(*) FILTER (WHERE l.row_type = 'household')::int AS household_rows,
                    COUNT(*) FILTER (WHERE l.row_type = 'building_only')::int AS building_only_rows,
                    COUNT(*) FILTER (WHERE l.sample_flag = true)::int AS sampled_households
                FROM clean.hh_listing_long l
                JOIN clean.hh_sampling_ea s
                    ON s.submission_key = l.submission_key
                {listing_where_sql}
                """,
                listing_params,
            )
            listing_counts = cur.fetchone() or {}

            issue_params: list[Any] = []
            issue_filters = ["i.instrument_code = 'listing'"]
            if state_names:
                placeholders = ", ".join(["%s"] * len(state_names))
                issue_filters.append(f"LOWER(COALESCE(s.record->>'state_name', '')) IN ({placeholders})")
                issue_params.extend(s.lower() for s in state_names)
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE i.issue_status <> 'resolved')::int AS open_issues,
                    COUNT(*) FILTER (WHERE i.issue_status = 'resolved')::int AS resolved_issues
                FROM qc.issue_queue i
                JOIN clean.hh_sampling_ea s
                    ON s.submission_key = i.submission_key
                WHERE {' AND '.join(issue_filters)}
                """,
                issue_params,
            )
            issue_counts = cur.fetchone() or {}

            change_params: list[Any] = []
            change_filters = ["pc.instrument_code = 'listing'"]
            if state_names:
                placeholders = ", ".join(["%s"] * len(state_names))
                change_filters.append(f"LOWER(COALESCE(s.record->>'state_name', '')) IN ({placeholders})")
                change_params.extend(s.lower() for s in state_names)
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE pc.change_status = 'pending')::int AS pending_changes,
                    COUNT(*) FILTER (WHERE pc.change_status = 'approved')::int AS approved_changes
                FROM qc.pending_change pc
                JOIN clean.hh_sampling_ea s
                    ON s.submission_key = pc.submission_key
                WHERE {' AND '.join(change_filters)}
                """,
                change_params,
            )
            change_counts = cur.fetchone() or {}

            cur.execute(
                """
                SELECT instrument_code, last_successful_completion_utc, last_run_started_at, last_run_finished_at, last_status, last_message
                FROM raw.sync_state
                WHERE instrument_code = 'listing'
                """
            )
            sync_state = cur.fetchone() or {}

    return {
        "statusCounts": status_counts,
        "listingCounts": listing_counts,
        "issueCounts": issue_counts,
        "changeCounts": change_counts,
        "syncState": sync_state,
        "stateEaSummary": get_state_ea_completion_summary(settings, user, state_names or None),
    }


TARGET_EA_OVERRIDES: dict[str, int] = {
    "ABIA": 50,
    "ADAMAWA": 50,
    "AKWA IBOM": 50,
    "ANAMBRA": 50,
    "BAUCHI": 50,
    "BAYELSA": 40,
    "BENUE": 50,
    "BORNO": 50,
    "CROSS RIVER": 50,
    "DELTA": 50,
    "EBONYI": 50,
    "EDO": 50,
    "EKITI": 40,
    "ENUGU": 50,
    "FCT": 50,
    "GOMBE": 40,
    "IMO": 50,
    "JIGAWA": 50,
    "KADUNA": 690,
    "KANO": 75,
    "KATSINA": 50,
    "KEBBI": 50,
    "KOGI": 50,
    "KWARA": 50,
    "LAGOS": 75,
    "NASARAWA": 50,
    "NIGER": 50,
    "OGUN": 75,
    "ONDO": 50,
    "OSUN": 50,
    "OYO": 50,
    "PLATEAU": 50,
    "RIVERS": 50,
    "SOKOTO": 50,
    "TARABA": 50,
    "YOBE": 50,
    "ZAMFARA": 50,
}

_STATE_TARGET_ALIASES: dict[str, str] = {
    "FEDERAL CAPITAL TERRITORY": "FCT",
    "F.C.T": "FCT",
    "F C T": "FCT",
    "ABUJA": "FCT",
}


def _target_state_key(state_name: Any) -> str:
    key = " ".join(str(state_name or "").strip().upper().split())
    return _STATE_TARGET_ALIASES.get(key, key)


def _target_eas_for_state(state_name: Any) -> int | None:
    return TARGET_EA_OVERRIDES.get(_target_state_key(state_name))


def get_state_ea_completion_summary(settings: Settings, user: AuthUser, state_name: str | list[str] | None = None) -> list[dict[str, Any]]:
    state_names_summary: list[str] = (
        [state_name] if isinstance(state_name, str)
        else (state_name or [])
    )

    boundary_where: list[str] = ["ea_id IS NOT NULL", "ea_id <> ''"]
    boundary_params: list[Any] = []
    if state_names_summary:
        placeholders = ", ".join(["%s"] * len(state_names_summary))
        boundary_where.append(f"LOWER(COALESCE(state_name, '')) IN ({placeholders})")
        boundary_params.extend(s.lower() for s in state_names_summary)

    state_summary: dict[str, dict[str, int | str]] = {}
    requested_target_states = {_target_state_key(s) for s in state_names_summary}

    # The EA Completion Rate target values are now controlled by the fixed
    # state-level Target Cases list provided for the Listing Overview page.
    # Pre-seed these rows so all target states appear even when there are no
    # uploaded/listed EAs yet.
    for target_state, target_cases in TARGET_EA_OVERRIDES.items():
        if requested_target_states and target_state not in requested_target_states:
            continue
        state_summary[target_state] = {
            "state": target_state,
            "targetEas": target_cases,
            "totalEas": 0,
            "completedEas": 0,
            "approvedEas": 0,
            "rejectedEas": 0,
        }

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # Target EAs from geographic reference
            cur.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(TRIM(state_name), ''), 'Unknown') AS state_name,
                    COUNT(DISTINCT ea_id)::int AS target_eas
                FROM reference.geo_boundaries_ea
                WHERE {' AND '.join(boundary_where)}
                GROUP BY COALESCE(NULLIF(TRIM(state_name), ''), 'Unknown')
                """,
                boundary_params,
            )
            for row in cur.fetchall():
                raw_state_name = str(row.get("state_name") or "").strip() or "Unknown"
                override_target = _target_eas_for_state(raw_state_name)
                sn = _target_state_key(raw_state_name) if override_target is not None else raw_state_name
                state_summary[sn] = {
                    "state": sn,
                    "targetEas": override_target if override_target is not None else int(row.get("target_eas") or 0),
                    "totalEas": 0,
                    "completedEas": 0,
                    "approvedEas": 0,
                    "rejectedEas": 0,
                }

            # EA counts by state — use the latest submission per EA so repeat submissions
            # do not inflate the state totals.
            ea_where: list[str] = ["1 = 1"]
            ea_params: list[Any] = []
            if state_names_summary:
                placeholders = ", ".join(["%s"] * len(state_names_summary))
                ea_where.append(
                    f"LOWER(COALESCE(NULLIF(TRIM(g.state_name), ''), NULLIF(TRIM(s.record->>'state_name'), ''), 'unknown')) IN ({placeholders})"
                )
                ea_params.extend(s.lower() for s in state_names_summary)

            ea_where_sql = " AND ".join(ea_where)

            cur.execute(
                f"""
                WITH latest_ea_rows AS (
                    SELECT
                        s.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.ea_id
                            ORDER BY COALESCE(s.completion_date, s.submission_date) DESC NULLS LAST,
                                     s.updated_at DESC NULLS LAST,
                                     s.submission_key DESC
                        ) AS rn
                    FROM clean.hh_sampling_ea s
                )
                SELECT
                    COALESCE(NULLIF(TRIM(g.state_name), ''), NULLIF(TRIM(s.record->>'state_name'), ''), 'Unknown') AS state_name,
                    COUNT(*)::int AS total_eas,
                    COUNT(*) FILTER (WHERE s.approval_status = 'approved')::int AS approved_eas,
                    COUNT(*) FILTER (WHERE s.approval_status = 'rejected')::int AS rejected_eas,
                    COUNT(*) FILTER (WHERE s.approval_status IN ('approved', 'submitted', 'pending_review', 'in_review', 'corrected'))::int AS completed_eas
                FROM latest_ea_rows s
                LEFT JOIN reference.geo_boundaries_ea g ON g.ea_id = REGEXP_REPLACE(s.ea_id::text, '\\.0+$', '')
                WHERE s.rn = 1 AND {ea_where_sql}
                GROUP BY COALESCE(NULLIF(TRIM(g.state_name), ''), NULLIF(TRIM(s.record->>'state_name'), ''), 'Unknown')
                ORDER BY state_name
                """,
                ea_params,
            )
            for row in cur.fetchall():
                raw_state_name = str(row.get("state_name") or "").strip() or "Unknown"
                override_target = _target_eas_for_state(raw_state_name)
                sn = _target_state_key(raw_state_name) if override_target is not None else raw_state_name
                entry = state_summary.setdefault(sn, {
                    "state": sn,
                    "targetEas": override_target or 0,
                    "totalEas": 0,
                    "completedEas": 0,
                    "approvedEas": 0,
                    "rejectedEas": 0,
                })
                if override_target is not None:
                    entry["targetEas"] = override_target
                entry["totalEas"] = int(row.get("total_eas") or 0)
                entry["completedEas"] = int(row.get("completed_eas") or 0)
                entry["approvedEas"] = int(row.get("approved_eas") or 0)
                entry["rejectedEas"] = int(row.get("rejected_eas") or 0)

    return sorted(state_summary.values(), key=lambda r: str(r["state"]).lower())


def list_listing_cases(
    settings: Settings,
    user: AuthUser,
    status_filters: list[str] | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where_parts = []

    if status_filters:
        where_parts.append("s.approval_status = ANY(%s::text[])")
        params.append(status_filters)

    if search:
        where_parts.append(
            """
            (
                s.submission_key ILIKE %s
                OR
                s.ea_id::text ILIKE %s
                OR s.record->>'ea_name' ILIKE %s
                OR s.record->>'lga_name' ILIKE %s
                OR s.record->>'caseid' ILIKE %s
                OR COALESCE(NULLIF(TRIM(s.record->>'state_name'), ''), '') ILIKE %s
                OR COALESCE(NULLIF(TRIM(s.approval_status), ''), '') ILIKE %s
                OR COALESCE(NULLIF(TRIM(s.record->>'sample_type'), ''), 'Listing submission') ILIKE %s
                OR COALESCE(TO_CHAR(COALESCE(s.completion_date, s.submission_date), 'YYYY-MM-DD HH24:MI:SS'), '') ILIKE %s
                OR CAST((
                    SELECT COUNT(*) FILTER (WHERE l.row_type = 'household')::int
                    FROM clean.hh_listing_long l
                    WHERE l.submission_key = s.submission_key
                ) AS text) ILIKE %s
                OR CAST((
                    SELECT COUNT(*) FILTER (WHERE l.row_type = 'building_only')::int
                    FROM clean.hh_listing_long l
                    WHERE l.submission_key = s.submission_key
                ) AS text) ILIKE %s
                OR CAST((
                    SELECT COUNT(*) FILTER (WHERE l.sample_flag = true)::int
                    FROM clean.hh_listing_long l
                    WHERE l.submission_key = s.submission_key
                ) AS text) ILIKE %s
                OR CAST((
                    SELECT COUNT(*) FILTER (WHERE iq.issue_status <> 'resolved')::int
                    FROM qc.issue_queue iq
                    WHERE iq.instrument_code = 'listing'
                      AND iq.submission_key = s.submission_key
                ) AS text) ILIKE %s
                OR EXISTS (
                    SELECT 1
                    FROM qc.case_status_history h
                    LEFT JOIN app.user_account u ON u.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'listing'
                      AND h.submission_key = s.submission_key
                      AND h.new_status = s.approval_status
                      AND (
                        CONCAT(COALESCE(NULLIF(TRIM(u.username), ''), ''), ': ', COALESCE(NULLIF(TRIM(u.full_name), ''), '')) ILIKE %s
                        OR COALESCE(NULLIF(TRIM(u.username), ''), '') ILIKE %s
                        OR COALESCE(NULLIF(TRIM(u.full_name), ''), '') ILIKE %s
                      )
                )
            )
            """
        )
        like = f"%{search}%"
        params.extend([like] * 16)

    if date_from:
        where_parts.append("COALESCE(s.completion_date, s.submission_date) >= %s::date")
        params.append(date_from)

    if date_to:
        where_parts.append("COALESCE(s.completion_date, s.submission_date) < (%s::date + interval '1 day')")
        params.append(date_to)

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    query = f"""
        SELECT
            s.submission_key,
            s.ea_id,
            s.boundary_id,
            s.interviewer_id,
            s.supervisor_id,
            s.approval_status,
            s.submission_date,
            s.completion_date,
            s.record->>'ea_name' AS ea_name,
            s.record->>'lga_name' AS lga_name,
            s.record->>'state_name' AS state_name,
            s.record->>'sample_type' AS sample_type,
            approver.approved_by,
            COUNT(l.listing_row_id) FILTER (WHERE l.row_type = 'household')::int AS household_count,
            COUNT(l.listing_row_id) FILTER (WHERE l.row_type = 'building_only')::int AS building_only_count,
            COUNT(l.listing_row_id) FILTER (WHERE l.sample_flag = true)::int AS sampled_household_count,
            COALESCE(i.open_issue_count, 0)::int AS open_issue_count,
            COALESCE(pc.pending_change_count, 0)::int AS pending_change_count
        FROM clean.hh_sampling_ea s
        LEFT JOIN clean.hh_listing_long l
            ON l.submission_key = s.submission_key
        LEFT JOIN (
            SELECT submission_key, COUNT(*) AS open_issue_count
            FROM qc.issue_queue
            WHERE instrument_code = 'listing' AND issue_status <> 'resolved'
            GROUP BY submission_key
        ) i ON i.submission_key = s.submission_key
        LEFT JOIN (
            SELECT submission_key, COUNT(*) AS pending_change_count
            FROM qc.pending_change
            WHERE instrument_code = 'listing' AND change_status = 'pending'
            GROUP BY submission_key
        ) pc ON pc.submission_key = s.submission_key
        LEFT JOIN (
            SELECT DISTINCT ON (h.submission_key, h.new_status)
                h.submission_key,
                h.new_status,
                CASE
                    WHEN NULLIF(TRIM(u.username), '') IS NOT NULL AND NULLIF(TRIM(u.full_name), '') IS NOT NULL THEN CONCAT(u.username, ': ', u.full_name)
                    WHEN NULLIF(TRIM(u.username), '') IS NOT NULL THEN u.username
                    WHEN NULLIF(TRIM(u.full_name), '') IS NOT NULL THEN u.full_name
                    ELSE NULL
                END AS approved_by
            FROM qc.case_status_history h
            LEFT JOIN app.user_account u
                ON u.user_id = h.changed_by_user_id
            WHERE h.instrument_code = 'listing'
            ORDER BY h.submission_key, h.new_status, h.changed_at DESC, h.status_history_id DESC
        ) approver ON approver.submission_key = s.submission_key AND approver.new_status = s.approval_status
        {where_sql}
        GROUP BY
            s.submission_key,
            s.ea_id,
            s.boundary_id,
            s.interviewer_id,
            s.supervisor_id,
            s.approval_status,
            s.submission_date,
            s.completion_date,
            s.record,
            approver.approved_by,
            i.open_issue_count,
            pc.pending_change_count
        ORDER BY s.completion_date DESC NULLS LAST
    """

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    return rows


def bulk_delete_listing_cases(settings: Settings, submission_keys: list[str]) -> int:
    """Delete listing cases by submission key. Returns count of deleted rows."""
    if not submission_keys:
        return 0
    placeholders = ", ".join(["%s"] * len(submission_keys))
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM clean.hh_sampling_ea WHERE submission_key IN ({placeholders})",
                submission_keys,
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def get_listing_case_detail(settings: Settings, user: AuthUser, submission_key: str) -> dict[str, Any]:
    with db_connection(settings) as conn:
        postgis = _has_postgis(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM clean.hh_sampling_ea WHERE submission_key = %s", (submission_key,))
            case_header = cur.fetchone()
            if not case_header:
                raise HTTPException(status_code=404, detail="Listing case not found.")

            _enforce_case_visibility(user, case_header["approval_status"])

            cur.execute(
                """
                SELECT
                    listing_row_id::text AS listing_row_id,
                    building_no,
                    household_no_within_building,
                    row_type,
                    sample_flag,
                    gps_lat,
                    gps_long,
                    gps_source,
                    record
                FROM clean.hh_listing_long
                WHERE submission_key = %s AND COALESCE(record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                ORDER BY building_no NULLS LAST, household_no_within_building NULLS FIRST
                """,
                (submission_key,),
            )
            listing_rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    selected_id::text AS selected_id,
                    selected_repeat_no,
                    selected_join_key,
                    sample_case_id,
                    sample_case_label,
                    slot_type,
                    record
                FROM clean.hh_selected_long
                WHERE submission_key = %s
                ORDER BY selected_repeat_no
                """,
                (submission_key,),
            )
            selected_rows = cur.fetchall()

            cur.execute(
                """
                SELECT
                    iq.issue_id::text AS issue_id,
                    iq.issue_status,
                    iq.issue_summary,
                    iq.resolution_note,
                    iq.created_at,
                    iq.resolved_at,
                    rr.rule_code,
                    rr.severity,
                    rr.table_name,
                    rr.row_identifier,
                    rr.field_name,
                    COALESCE(
                        NULLIF(rr.case_id, ''),
                        NULLIF(listing.sample_case_id::text, ''),
                        NULLIF(listing.listing_join_key::text, ''),
                        NULLIF(listing.household_uid::text, ''),
                        NULLIF(selected_case.sample_case_id::text, ''),
                        NULLIF(selected_case.selected_join_key::text, ''),
                        NULLIF(sampling.ea_id::text, ''),
                        iq.submission_key
                    ) AS case_id,
                    CASE
                        WHEN rr.table_name = 'clean.hh_listing_long' THEN COALESCE(
                            NULLIF(listing.sample_case_id::text, ''),
                            NULLIF(listing.listing_join_key::text, ''),
                            NULLIF(listing.household_uid::text, ''),
                            CONCAT('Building ', COALESCE(listing.building_no::text, '-'), ' / HH ', COALESCE(listing.household_no_within_building::text, '-')),
                            listing.listing_row_id::text
                        )
                        WHEN rr.table_name = 'clean.hh_selected_long' THEN COALESCE(
                            NULLIF(selected_case.sample_case_id::text, ''),
                            NULLIF(selected_case.selected_join_key::text, ''),
                            selected_case.selected_id::text
                        )
                        WHEN rr.table_name = 'clean.hh_sampling_ea' THEN COALESCE(NULLIF(sampling.ea_id::text, ''), sampling.submission_key)
                        ELSE iq.submission_key
                    END AS case_label,
                    CASE
                        WHEN rr.table_name = 'clean.hh_listing_long' AND rr.field_name IS NOT NULL THEN listing.record ->> rr.field_name
                        WHEN rr.table_name = 'clean.hh_selected_long' AND rr.field_name IS NOT NULL THEN selected_case.record ->> rr.field_name
                        WHEN rr.table_name = 'clean.hh_sampling_ea' AND rr.field_name IS NOT NULL THEN sampling.record ->> rr.field_name
                        ELSE NULL
                    END AS current_value,
                    q.question_label AS variable_label
                FROM qc.issue_queue iq
                LEFT JOIN qc.rule_result rr
                    ON rr.rule_result_id = iq.rule_result_id
                LEFT JOIN clean.hh_listing_long listing
                    ON rr.table_name = 'clean.hh_listing_long'
                   AND listing.listing_row_id::text = rr.row_identifier
                LEFT JOIN clean.hh_selected_long selected_case
                    ON rr.table_name = 'clean.hh_selected_long'
                   AND selected_case.selected_id::text = rr.row_identifier
                LEFT JOIN clean.hh_sampling_ea sampling
                    ON rr.table_name = 'clean.hh_sampling_ea'
                   AND sampling.submission_key = iq.submission_key
                LEFT JOIN reference.xlsform_question q
                    ON q.instrument_code = 'listing'
                   AND q.variable_name = rr.field_name
                WHERE iq.instrument_code = 'listing' AND iq.submission_key = %s
                ORDER BY created_at DESC
                """,
                (submission_key,),
            )
            issues = cur.fetchall()

            interviewer_id = str(case_header.get("interviewer_id") or "").strip()
            current_record = case_header.get("record") or {}
            phone_values = sorted({p for p in (_normalized_phone_key(v) for v in _extract_listing_phone_candidates(current_record, listing_rows)) if p})
            gps_pairs: list[tuple[float, float]] = []
            for row in listing_rows:
                lat = row.get("gps_lat")
                lon = row.get("gps_long")
                if lat is None or lon is None:
                    continue
                try:
                    gps_pairs.append((float(lat), float(lon)))
                except (TypeError, ValueError):
                    continue

            for issue in issues:
                rule_code = str(issue.get("rule_code") or "")
                if not issue.get("variable_label"):
                    if rule_code == "LISTING_DUPLICATE_PHONE_NUMBER":
                        issue["variable_label"] = "Respondent phone number"
                    elif rule_code == "LISTING_DUPLICATE_GPS":
                        issue["variable_label"] = "GPS coordinates"
                if rule_code == "LISTING_DUPLICATE_PHONE_NUMBER" and interviewer_id and phone_values:
                    cur.execute(
                        """
                        SELECT DISTINCT s.submission_key::text
                        FROM clean.hh_sampling_ea s
                        JOIN clean.hh_listing_long l ON l.submission_key = s.submission_key
                        WHERE s.interviewer_id::text = %s
                          AND COALESCE(NULLIF(TRIM(l.record->>'household_phone_no'), ''), NULLIF(TRIM(l.record->>'phone_no'), ''), NULLIF(TRIM(l.record->>'phoneno'), ''), NULLIF(TRIM(l.record->>'respondent_phone'), '')) = ANY(%s)
                        ORDER BY s.submission_key::text
                        """,
                        (interviewer_id, phone_values),
                    )
                    matches = [str(r[0]) for r in cur.fetchall() if str(r[0]) != submission_key]
                    issue["matching_case_keys"] = matches
                    if not issue.get("current_value") and phone_values:
                        issue["current_value"] = ", ".join(phone_values)
                elif rule_code == "LISTING_DUPLICATE_GPS" and interviewer_id and gps_pairs:
                    matches: set[str] = set()
                    for lat_value, lon_value in gps_pairs:
                        cur.execute(
                            """
                            SELECT DISTINCT s.submission_key::text
                            FROM clean.hh_sampling_ea s
                            JOIN clean.hh_listing_long l ON l.submission_key = s.submission_key
                            WHERE s.interviewer_id::text = %s
                              AND l.gps_lat = %s
                              AND l.gps_long = %s
                            ORDER BY s.submission_key::text
                            """,
                            (interviewer_id, lat_value, lon_value),
                        )
                        for r in cur.fetchall():
                            key = str(r[0])
                            if key != submission_key:
                                matches.add(key)
                    issue["matching_case_keys"] = sorted(matches)
                    if not issue.get("current_value") and gps_pairs:
                        issue["current_value"] = ", ".join(f"({lat}, {lon})" for lat, lon in gps_pairs[:5])

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
                WHERE pc.instrument_code = 'listing' AND pc.submission_key = %s
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
                WHERE h.instrument_code = 'listing' AND h.submission_key = %s
                ORDER BY h.changed_at DESC
                """,
                (submission_key,),
            )
            history = cur.fetchall()

            ea_id = _normalize_boundary_key(case_header.get("ea_id"))
            boundary_id = _normalize_boundary_key(case_header.get("boundary_id"))
            case_record = case_header.get("record") or {}
            ea_name = str(
                case_header.get("ea_name")
                or case_record.get("ea_name")
                or case_record.get("sd_EA_NAME")
                or case_record.get("name")
                or ""
            ).strip()
            state_name = str(
                case_header.get("state_name")
                or case_record.get("state_name")
                or case_record.get("sd_STATE_NAME")
                or case_record.get("state")
                or ""
            ).strip()
            lga_name = str(
                case_header.get("lga_name")
                or case_record.get("lga_name")
                or case_record.get("sd_LGA_NAME")
                or ""
            ).strip()
            ea_feature: dict[str, Any] | None = None

            if ea_id or boundary_id:
                if postgis:
                    cur.execute(
                        """
                        SELECT
                            ea_id,
                            boundary_id,
                            state_name,
                            lga_name,
                            ward_name,
                            properties,
                            ST_AsGeoJSON(geom)::jsonb AS geometry
                        FROM reference.geo_boundaries_ea
                        WHERE (%s <> '' AND ea_id = %s)
                           OR (%s <> '' AND boundary_id = %s)
                        ORDER BY CASE WHEN ea_id = %s THEN 0 WHEN boundary_id = %s THEN 1 ELSE 2 END
                        LIMIT 1
                        """,
                        (ea_id, ea_id, boundary_id, boundary_id, ea_id, boundary_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                            ea_id,
                            boundary_id,
                            state_name,
                            lga_name,
                            ward_name,
                            properties,
                            geom_geojson AS geometry
                        FROM reference.geo_boundaries_ea
                        WHERE (%s <> '' AND ea_id = %s)
                           OR (%s <> '' AND boundary_id = %s)
                        ORDER BY CASE WHEN ea_id = %s THEN 0 WHEN boundary_id = %s THEN 1 ELSE 2 END
                        LIMIT 1
                        """,
                        (ea_id, ea_id, boundary_id, boundary_id, ea_id, boundary_id),
                    )
                boundary_row = cur.fetchone()
                if boundary_row and isinstance(boundary_row.get("geometry"), dict):
                    boundary_properties = dict(boundary_row.get("properties") or {})
                    boundary_properties.setdefault("sd_EA_ID", boundary_row.get("ea_id"))
                    boundary_properties.setdefault("boundary_id", boundary_row.get("boundary_id"))
                    boundary_properties.setdefault("sd_STATE_NAME", boundary_row.get("state_name"))
                    boundary_properties.setdefault("sd_LGA_NAME", boundary_row.get("lga_name"))
                    boundary_properties.setdefault("sd_WARD_NAME", boundary_row.get("ward_name"))
                    ea_feature = {
                        "type": "Feature",
                        "geometry": boundary_row.get("geometry"),
                        "properties": boundary_properties,
                    }

            if ea_feature is None:
                ea_feature = _load_boundary_feature_from_zip(
                    str(settings.boundary_zip_path),
                    ea_id,
                    boundary_id,
                    ea_name,
                    state_name,
                    lga_name,
                )

    return {
        "case": case_header,
        "listingRows": listing_rows,
        "selectedRows": selected_rows,
        "issues": issues,
        "pendingChanges": pending_changes,
        "history": history,
        "eaFeature": ea_feature,
    }



def _normalized_phone_key(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-11:] if len(digits) >= 7 else ""


def _start_time_flag(dt: datetime | None) -> bool:
    return dt is not None and (dt.hour >= 19 or dt.hour < 7)


def _matrix_groups(record: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    for key, raw in (record or {}).items():
        key_text = str(key or "").strip()
        match = re.match(r"^(.+?)_(\d+)$", key_text)
        if not match:
            continue
        if _safe_text(raw := raw) in {"", "0", "0.0", "nan", "NaN"}:
            continue
        groups.setdefault(match.group(1), []).append((key_text, str(raw).strip()))
    return groups


def _is_straightlined_record(record: dict[str, Any]) -> bool:
    for _, values in _matrix_groups(record).items():
        eligible = [value for _, value in values if value not in {"", "96", "97", "98", "99", "95"}]
        if len(eligible) < 3:
            continue
        top = Counter(eligible).most_common(1)[0][1]
        if top / len(eligible) >= 0.8:
            return True
    return False


def _listing_numeric_issue_specs() -> list[tuple[str, str, int]]:
    return [
        ("no_of_male_less_15yrs", "LISTING_NO_OF_MALE_LESS_15YRS", 10),
        ("no_of_female_less_15yrs", "LISTING_NO_OF_FEMALE_LESS_15YRS", 10),
        ("total_less_15yrs", "LISTING_TOTAL_LESS_15YRS", 10),
        ("no_of_male_15_17yrs", "LISTING_NO_OF_MALE_15_17YRS", 10),
        ("no_of_female_15_17yrs", "LISTING_NO_OF_FEMALE_15_17YRS", 10),
        ("total_15_17yrs", "LISTING_TOTAL_15_17YRS", 10),
        ("no_of_male_18yrs_plus", "LISTING_NO_OF_MALE_18YRS_PLUS", 10),
        ("no_of_female_18yrs_plus", "LISTING_NO_OF_FEMALE_18YRS_PLUS", 10),
        ("total_18yrs_plus", "LISTING_TOTAL_18YRS_PLUS", 10),
        ("total_male", "LISTING_TOTAL_MALE", 10),
        ("total_female", "LISTING_TOTAL_FEMALE", 10),
        ("total_household_size", "LISTING_TOTAL_HOUSEHOLD_SIZE", 10),
    ]


def refresh_listing_qc(settings: Settings, submission_key: str | None = None) -> dict[str, Any]:
    bootstrap_rule_definitions(settings)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            if submission_key:
                cur.execute("DELETE FROM qc.issue_queue WHERE instrument_code = 'listing' AND submission_key = %s", (submission_key,))
                cur.execute("DELETE FROM qc.rule_result WHERE instrument_code = 'listing' AND submission_key = %s", (submission_key,))
                cur.execute(
                    """
                    SELECT submission_key, approval_status, ea_id, interviewer_id, submission_date, completion_date, record
                    FROM clean.hh_sampling_ea
                    WHERE submission_key = %s
                    """,
                    (submission_key,),
                )
            else:
                cur.execute("DELETE FROM qc.issue_queue WHERE instrument_code = 'listing'")
                cur.execute("DELETE FROM qc.rule_result WHERE instrument_code = 'listing'")
                cur.execute(
                    """
                    SELECT submission_key, approval_status, ea_id, interviewer_id, submission_date, completion_date, record
                    FROM clean.hh_sampling_ea
                    """
                )

            cases = cur.fetchall()
            created = 0

            durations: list[float] = []
            interviewer_timeline: dict[str, list[tuple[datetime, datetime, str]]] = {}
            phone_submissions: dict[str, dict[str, set[str]]] = {}
            gps_submission_map: dict[str, dict[tuple[float, float], set[str]]] = {}
            listing_rows_by_submission: dict[str, list[dict[str, Any]]] = {}
            case_rows_by_submission: dict[str, dict[str, Any]] = {}
            submissions_by_ea: dict[str, set[str]] = {}
            ea_spatial_rows: dict[str, list[dict[str, Any]]] = {}
            boundary_geometries_by_ea = _load_boundary_geometries(str(settings.boundary_zip_path))

            for case in cases:
                sub_key = str(case["submission_key"])
                record = case.get("record") or {}
                case_rows_by_submission[sub_key] = dict(case)
                ea_id = str(case.get("ea_id") or record.get("ea_id") or "").strip()
                if ea_id:
                    submissions_by_ea.setdefault(ea_id, set()).add(sub_key)
                interviewer_id = str(case.get("interviewer_id") or record.get("interviewer_id") or "").strip()
                start_dt = _parse_datetime(record.get("start"))
                end_dt = _parse_datetime(record.get("end"))
                if start_dt and end_dt and end_dt >= start_dt:
                    duration_minutes = (end_dt - start_dt).total_seconds() / 60
                    durations.append(duration_minutes)
                    if interviewer_id:
                        interviewer_timeline.setdefault(interviewer_id, []).append((start_dt, end_dt, sub_key))

                cur.execute(
                    """
                    SELECT listing_row_id::text AS listing_row_id, row_type, gps_lat, gps_long, record
                    FROM clean.hh_listing_long
                    WHERE submission_key = %s AND COALESCE(record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                    ORDER BY listing_row_id
                    """,
                    (sub_key,),
                )
                pre_rows = [dict(r) for r in cur.fetchall()]
                listing_rows_by_submission[sub_key] = pre_rows
                if ea_id:
                    spatial_bucket = ea_spatial_rows.setdefault(ea_id, [])
                    for row in pre_rows:
                        spatial_bucket.append({**row, "submission_key": sub_key})

                if interviewer_id:
                    phone_index = phone_submissions.setdefault(interviewer_id, {})
                    for phone in set(_normalized_phone_key(p) for p in _extract_listing_phone_candidates(record, pre_rows)):
                        if phone:
                            phone_index.setdefault(phone, set()).add(sub_key)
                    gps_index = gps_submission_map.setdefault(interviewer_id, {})
                    for row in pre_rows:
                        lat_value = _safe_float(row.get("gps_lat"))
                        lon_value = _safe_float(row.get("gps_long"))
                        if lat_value is None or lon_value is None:
                            continue
                        gps_index.setdefault((lat_value, lon_value), set()).add(sub_key)

            loi_median = median(durations) if durations else None
            for interviewer_id in interviewer_timeline:
                interviewer_timeline[interviewer_id].sort(key=lambda item: item[0])

            for case in cases:
                sub_key = str(case["submission_key"])
                record = case.get("record") or {}
                case_rows_by_submission[sub_key] = dict(case)
                ea_id = str(case.get("ea_id") or record.get("ea_id") or "").strip()
                if ea_id:
                    submissions_by_ea.setdefault(ea_id, set()).add(sub_key)
                interviewer_id = str(case.get("interviewer_id") or record.get("interviewer_id") or "").strip()
                start_dt = _parse_datetime(record.get("start"))
                end_dt = _parse_datetime(record.get("end"))
                listing_rows = listing_rows_by_submission.get(sub_key, [])

                if start_dt and end_dt and end_dt >= start_dt and loi_median:
                    duration_minutes = (end_dt - start_dt).total_seconds() / 60
                    if duration_minutes < (0.5 * loi_median):
                        created += _create_issue(cur, sub_key, "LISTING_LOW_LOI", "high", f"Listing LOI is {duration_minutes:.1f} minutes, below 50% of median LOI ({loi_median:.1f}).", None, "record", "clean.hh_sampling_ea")
                    if duration_minutes > (1.5 * loi_median):
                        created += _create_issue(cur, sub_key, "LISTING_HIGH_LOI", "high", f"Listing LOI is {duration_minutes:.1f} minutes, above 150% of median LOI ({loi_median:.1f}).", None, "record", "clean.hh_sampling_ea")

                if _start_time_flag(start_dt):
                    created += _create_issue(cur, sub_key, "LISTING_START_TIME", "high", f"Interview started at {start_dt.strftime('%H:%M')}, which falls within odd hours (7:00 PM to 6:59 AM).", None, "record", "clean.hh_sampling_ea")

                duplicate_phones = sorted(phone for phone in set(_normalized_phone_key(p) for p in _extract_listing_phone_candidates(record, listing_rows)) if phone and len(phone_submissions.get(interviewer_id, {}).get(phone, set())) > 1)
                if duplicate_phones:
                    created += _create_issue(cur, sub_key, "LISTING_DUPLICATE_PHONE_NUMBER", "high", "Duplicate phone number(s) within interviewer: " + ", ".join(duplicate_phones[:5]), None, "household_phone_no", "clean.hh_sampling_ea")

                if interviewer_id and start_dt and end_dt and end_dt >= start_dt:
                    timeline = interviewer_timeline.get(interviewer_id, [])
                    for idx, (s, e, sk) in enumerate(timeline):
                        if sk != sub_key:
                            continue
                        if idx > 0:
                            prev_s, prev_e, prev_sk = timeline[idx - 1]
                            gap_minutes = (s - prev_e).total_seconds() / 60
                            if 0 <= gap_minutes < 5:
                                created += _create_issue(cur, sub_key, "LISTING_GAP_BETWEEN_2_INTERVIEWS", "high", f"Gap is {gap_minutes:.1f} minutes between this interview and {prev_sk} for interviewer {interviewer_id}.", None, "record", "clean.hh_sampling_ea")
                            overlap_minutes = (prev_e - s).total_seconds() / 60
                            if overlap_minutes > 1:
                                created += _create_issue(cur, sub_key, "LISTING_TIME_INTERWOVEN", "high", f"Interview overlaps with {prev_sk} for interviewer {interviewer_id} by {overlap_minutes:.1f} minutes.", None, "record", "clean.hh_sampling_ea")
                        if idx + 1 < len(timeline):
                            next_s, _, next_sk = timeline[idx + 1]
                            overlap_minutes = (end_dt - next_s).total_seconds() / 60
                            if overlap_minutes > 1:
                                created += _create_issue(cur, sub_key, "LISTING_TIME_INTERWOVEN", "high", f"Interview overlaps with {next_sk} for interviewer {interviewer_id} by {overlap_minutes:.1f} minutes.", None, "record", "clean.hh_sampling_ea")
                        break

                straightline_flagged = False
                for row in listing_rows:
                    lat_value = _safe_float(row.get("gps_lat"))
                    lon_value = _safe_float(row.get("gps_long"))
                    if interviewer_id and lat_value is not None and lon_value is not None:
                        duplicates = gps_submission_map.get(interviewer_id, {}).get((lat_value, lon_value), set())
                        if len(duplicates) > 1:
                            created += _create_issue(cur, sub_key, "LISTING_DUPLICATE_GPS", "high", f"GPS ({lat_value}, {lon_value}) appears in other listing interviews by interviewer {interviewer_id}.", row.get("listing_row_id"), "gps_lat", "clean.hh_listing_long")
                            break

                for row in listing_rows:
                    row_record = row.get("record") or {}
                    if not straightline_flagged and isinstance(row_record, dict) and _is_straightlined_record(row_record):
                        created += _create_issue(cur, sub_key, "LISTING_STRAIGHTLINING", "high", "Detected same response on 80% or more of eligible matrix items.", row.get("listing_row_id"), "record", "clean.hh_listing_long")
                        straightline_flagged = True
                    for field_name, rule_code, threshold in _listing_numeric_issue_specs():
                        value = _safe_float(row_record.get(field_name))
                        if value is not None and value > threshold:
                            created += _create_issue(cur, sub_key, rule_code, "medium", f"{field_name} is {value:g}, above the threshold of {threshold}.", row.get("listing_row_id"), field_name, "clean.hh_listing_long")

            spatial_threshold = QC_THRESHOLDS["spatial_auto_approval_required_grid_coverage_ratio"]
            min_points = int(QC_THRESHOLDS["spatial_auto_approval_min_points"])
            for ea_id, ea_rows in ea_spatial_rows.items():
                submission_keys = sorted(submissions_by_ea.get(ea_id, set()))
                if not submission_keys:
                    continue
                geometry = boundary_geometries_by_ea.get(ea_id)
                total_points = 0
                valid_points: list[tuple[float, float]] = []
                invalid_rows_by_submission: dict[str, list[str]] = {}
                outside_rows_by_submission: dict[str, list[str]] = {}
                duplicate_point_counter: Counter[tuple[float, float]] = Counter()

                for row in ea_rows:
                    total_points += 1
                    lat_value = _safe_float(row.get("gps_lat"))
                    lon_value = _safe_float(row.get("gps_long"))
                    submission_key = str(row.get("submission_key") or "").strip()
                    row_identifier = str(row.get("listing_row_id") or "").strip() or "row"
                    if lat_value in {None, 0.0} or lon_value in {None, 0.0}:
                        invalid_rows_by_submission.setdefault(submission_key, []).append(row_identifier)
                        continue
                    point = (lon_value, lat_value)
                    valid_points.append(point)
                    duplicate_point_counter[point] += 1
                    if geometry and not _point_in_geometry(point, geometry):
                        outside_rows_by_submission.setdefault(submission_key, []).append(row_identifier)

                valid_ratio = (len(valid_points) / total_points) if total_points else 0.0
                duplicate_rate_flag = any(count > 1 for count in duplicate_point_counter.values())
                coverage_ratio = 0.0
                occupied_cells = 0
                usable_cells = 0
                sparse_evidence = {
                    "spread_ok": False,
                    "bbox_coverage_ratio": 0.0,
                    "quadrants_covered": 0,
                    "unique_point_count": 0,
                    "average_step_distance": 0.0,
                    "appears_static": False,
                }
                if geometry:
                    inside_points = [point for point in valid_points if _point_in_geometry(point, geometry)]
                else:
                    inside_points = valid_points
                if geometry and inside_points:
                    coverage_ratio, occupied_cells, usable_cells = _compute_polygon_grid_coverage(inside_points, geometry)
                    sparse_evidence = _evaluate_sparse_spatial_evidence(inside_points, geometry, settings)
                spread_override = bool(sparse_evidence["spread_ok"])

                coverage_ok = (
                    geometry is not None
                    and total_points >= min_points
                    and valid_ratio >= QC_THRESHOLDS["spatial_auto_approval_required_valid_gps_ratio"]
                    and not outside_rows_by_submission
                    and (coverage_ratio >= spatial_threshold or spread_override)
                    and not duplicate_rate_flag
                )

                if valid_ratio < QC_THRESHOLDS["spatial_auto_approval_required_valid_gps_ratio"]:
                    for submission_key in submission_keys:
                        bad_rows = invalid_rows_by_submission.get(submission_key, [])
                        message = (
                            f"EA {ea_id} cannot be spatially auto-approved because only {len(valid_points)}/{total_points} listing points have valid non-zero GPS."
                            if total_points
                            else f"EA {ea_id} cannot be spatially auto-approved because it has no listing GPS points."
                        )
                        created += _create_issue(cur, submission_key, "LISTING_INSUFFICIENT_VALID_GPS", "high", message, bad_rows[0] if bad_rows else None, "gps_lat", "clean.hh_listing_long")

                if geometry is None:
                    for submission_key in submission_keys:
                        created += _create_issue(cur, submission_key, "LISTING_LOW_POLYGON_COVERAGE", "high", f"EA {ea_id} cannot be spatially auto-approved because no polygon geometry was found for the EA boundary.", None, "gps_lat", "clean.hh_listing_long")
                else:
                    for submission_key, row_ids in outside_rows_by_submission.items():
                        created += _create_issue(cur, submission_key, "LISTING_OUTSIDE_POLYGON", "high", f"EA {ea_id} has listing GPS points outside the assigned polygon boundary.", row_ids[0] if row_ids else None, "gps_lat", "clean.hh_listing_long")
                    if total_points < min_points or not (coverage_ratio >= spatial_threshold or spread_override):
                        for submission_key in submission_keys:
                            created += _create_issue(
                                cur,
                                submission_key,
                                "LISTING_LOW_POLYGON_COVERAGE",
                                "high",
                                (
                                    f"EA {ea_id} covers {coverage_ratio * 100:.1f}% of polygon grid cells ({occupied_cells}/{usable_cells}) "
                                    f"with {len(valid_points)} valid listing points. Spatial spread inside the polygon spans "
                                    f"{sparse_evidence['bbox_coverage_ratio'] * 100:.1f}% of the EA bounding box across "
                                    f"{sparse_evidence['quadrants_covered']} quadrants using {sparse_evidence['unique_point_count']} distinct GPS locations. "
                                    f"Minimum points required is {min_points}; approval requires either {spatial_threshold * 100:.0f}% grid coverage "
                                    f"or sufficient spread under the SPARSE_* settings."
                                ),
                                None,
                                "gps_lat",
                                "clean.hh_listing_long",
                            )

            conn.commit()

    return {"createdIssueCount": created}


def _create_issue(
    cur,
    submission_key: str,
    rule_code: str,
    severity: str,
    message: str,
    row_identifier: str | None,
    field_name: str | None,
    table_name: str | None = None,
) -> int:
    table_name = table_name or ("clean.hh_listing_long" if field_name in {"gps_lat", "listing_join_key"} else "clean.hh_sampling_ea")
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
        VALUES (%s, 'listing', %s, %s, %s, %s, %s, 'open', %s)
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
        VALUES (%s, 'listing', %s, 'pending_review', %s)
        """,
        (rule_result_id, submission_key, message),
    )
    return 1


def update_case_status(
    settings: Settings,
    user: AuthUser,
    submission_key: str,
    new_status: str,
    note: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    _ensure_status_allowed(new_status)
    if user.role not in LISTING_REVIEW_DECISION_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to update case status.")

    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with db_connection(settings) as conn:
                with conn.cursor() as cur:
                    # Keep the transaction from waiting forever behind another
                    # reviewer/bootstrap process. Deadlocks and lock timeouts
                    # are retried below.
                    cur.execute("SET LOCAL lock_timeout = '5s'")
                    cur.execute("SELECT approval_status, ea_id FROM clean.hh_sampling_ea WHERE submission_key = %s", (submission_key,))
                    row = cur.fetchone()
                    if not row:
                        raise HTTPException(status_code=404, detail="Listing case not found.")
                    previous_status = row["approval_status"]
                    case_id = row.get("ea_id") or submission_key

                    cur.execute(
                        "UPDATE clean.hh_sampling_ea SET approval_status = %s, updated_at = now() WHERE submission_key = %s",
                        (new_status, submission_key),
                    )
                    cur.execute(
                        "UPDATE clean.hh_listing_long SET approval_status = %s, updated_at = now() WHERE submission_key = %s",
                        (new_status, submission_key),
                    )
                    # Sync dedicated listing_case_status table (design doc Section 3.1)
                    cur.execute(
                        """
                        INSERT INTO clean.listing_case_status
                            (submission_key, ea_id, boundary_id, current_status, last_updated_by_user_id, updated_at)
                        SELECT %s, ea_id, boundary_id, %s, %s::uuid, now()
                        FROM clean.hh_sampling_ea
                        WHERE submission_key = %s
                        LIMIT 1
                        ON CONFLICT (submission_key) DO UPDATE SET
                            current_status = EXCLUDED.current_status,
                            last_updated_by_user_id = EXCLUDED.last_updated_by_user_id,
                            updated_at = now()
                        """,
                        (submission_key, new_status, user.id, submission_key),
                    )
                    _insert_case_status_history(cur, submission_key, case_id, previous_status, new_status, user, note, device_id)
                conn.commit()

            return {"submissionKey": submission_key, "previousStatus": previous_status, "newStatus": new_status}
        except HTTPException:
            raise
        except Exception as exc:
            if not _is_transient_db_lock_error(exc) or attempt >= max_attempts - 1:
                raise
            logger.warning(
                "Retrying listing status update after transient database lock for submission %s (attempt %s/%s): %s",
                submission_key,
                attempt + 1,
                max_attempts,
                exc,
            )
            _sleep_before_db_retry(attempt)

    raise HTTPException(status_code=503, detail="Database is busy. Please try again.")




def apply_ea_coverage_approvals(settings: Settings, submission_key: str | None = None) -> dict[str, Any]:
    params: list[Any] = []
    submission_clause = ""
    if submission_key:
        submission_clause = "WHERE s.submission_key = %s"
        params.append(submission_key)

    updated_to_approved = 0
    updated_to_pending_review = 0
    unchanged = 0
    skipped_rejected = 0

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                WITH case_scope AS (
                    SELECT s.submission_key, s.ea_id, s.approval_status
                    FROM clean.hh_sampling_ea s
                    {submission_clause}
                ),
                ea_high_issue AS (
                    SELECT
                        s.ea_id,
                        bool_or(iq.issue_status <> 'resolved' AND COALESCE(rr.severity, 'medium') = 'high') AS has_open_high_issue
                    FROM clean.hh_sampling_ea s
                    JOIN qc.issue_queue iq
                        ON iq.submission_key = s.submission_key
                       AND iq.instrument_code = 'listing'
                    JOIN qc.rule_result rr
                        ON rr.rule_result_id = iq.rule_result_id
                    GROUP BY s.ea_id
                )
                SELECT
                    cs.submission_key,
                    cs.ea_id,
                    cs.approval_status,
                    COALESCE(ehi.has_open_high_issue, false) AS has_open_high_issue
                FROM case_scope cs
                LEFT JOIN ea_high_issue ehi
                    ON ehi.ea_id = cs.ea_id
                """,
                params,
            )
            cases = cur.fetchall()

            for case in cases:
                next_status = _determine_coverage_approval_status(
                    case["approval_status"],
                    not bool(case["has_open_high_issue"]),
                )
                if next_status is None:
                    if str(case["approval_status"] or "").strip().lower() == "rejected":
                        skipped_rejected += 1
                    else:
                        unchanged += 1
                    continue

                cur.execute(
                    "UPDATE clean.hh_sampling_ea SET approval_status = %s, updated_at = now() WHERE submission_key = %s",
                    (next_status, case["submission_key"]),
                )
                cur.execute(
                    "UPDATE clean.hh_listing_long SET approval_status = %s, updated_at = now() WHERE submission_key = %s",
                    (next_status, case["submission_key"]),
                )
                cur.execute(
                    """
                    INSERT INTO clean.listing_case_status
                        (submission_key, ea_id, boundary_id, current_status, updated_at)
                    SELECT %s, ea_id, boundary_id, %s, now()
                    FROM clean.hh_sampling_ea
                    WHERE submission_key = %s
                    LIMIT 1
                    ON CONFLICT (submission_key) DO UPDATE SET
                        current_status = EXCLUDED.current_status,
                        updated_at = now()
                    """,
                    (case["submission_key"], next_status, case["submission_key"]),
                )

                if next_status == "approved":
                    updated_to_approved += 1
                else:
                    updated_to_pending_review += 1

        conn.commit()

    return {
        "updatedToApproved": updated_to_approved,
        "updatedToPendingReview": updated_to_pending_review,
        "unchanged": unchanged,
        "skippedRejected": skipped_rejected,
    }

def request_ea_review(
    settings: Settings,
    user: AuthUser,
    ea_id: str,
    decision: str,
    reason: str,
    device_id: str | None = None,
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Decision must be approved or rejected.")

    audit_note = f"Map EA {normalized_decision} request submitted for all EA cases. Reason: {reason.strip()}"

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT submission_key, approval_status
                FROM clean.hh_sampling_ea
                WHERE ea_id = %s
                ORDER BY submission_date DESC NULLS LAST, submission_key DESC
                """,
                (ea_id,),
            )
            rows = cur.fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail="EA not found.")

            updated_submission_keys: list[str] = []
            for row in rows:
                submission_key = str(row["submission_key"])
                previous_status = row["approval_status"]

                cur.execute(
                    "UPDATE clean.hh_sampling_ea SET approval_status = %s, updated_at = now() WHERE submission_key = %s",
                    ("pending_review", submission_key),
                )
                cur.execute(
                    "UPDATE clean.hh_listing_long SET approval_status = %s, updated_at = now() WHERE submission_key = %s",
                    ("pending_review", submission_key),
                )
                _insert_case_status_history(cur, submission_key, ea_id, previous_status, "pending_review", user, audit_note, device_id)
                updated_submission_keys.append(submission_key)

        conn.commit()

    return {
        "eaId": ea_id,
        "decision": normalized_decision,
        "newStatus": "pending_review",
        "affectedSubmissions": updated_submission_keys,
        "affectedCount": len(updated_submission_keys),
    }


def _insert_case_status_history(
    cur: Any,
    submission_key: str,
    case_id: str | None,
    previous_status: str | None,
    new_status: str,
    user: AuthUser,
    note: str | None = None,
    device_id: str | None = None,
) -> None:
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
        VALUES ('listing', %s, %s, %s, %s, %s, %s, %s)
        """,
        (submission_key, case_id, previous_status, new_status, user.id, note or "", device_id),
    )


def bootstrap_listing_case_status_reconciliation(settings: Settings) -> None:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest_qc_decision AS (
                    SELECT DISTINCT ON (h.submission_key)
                        h.submission_key,
                        h.new_status
                    FROM qc.case_status_history h
                    JOIN app.user_role ur
                      ON ur.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'listing'
                      AND LOWER(COALESCE(h.new_status, '')) IN ('approved', 'rejected')
                      AND ur.role_code = ANY(%s)
                    ORDER BY h.submission_key, h.changed_at DESC, h.status_history_id DESC
                ),
                eligible_updates AS (
                    SELECT
                        s.submission_key,
                        s.ea_id,
                        s.boundary_id,
                        latest_qc_decision.new_status
                    FROM clean.hh_sampling_ea s
                    JOIN latest_qc_decision
                      ON latest_qc_decision.submission_key = s.submission_key
                    WHERE LOWER(COALESCE(s.approval_status, '')) IN ('submitted', 'pending_review', 'in_review', 'corrected')
                      AND LOWER(COALESCE(s.approval_status, '')) <> LOWER(COALESCE(latest_qc_decision.new_status, ''))
                )
                UPDATE clean.hh_sampling_ea s
                SET approval_status = eligible_updates.new_status,
                    updated_at = now()
                FROM eligible_updates
                WHERE s.submission_key = eligible_updates.submission_key
                """,
                (list(LISTING_REVIEW_DECISION_ROLES),),
            )
            cur.execute(
                """
                WITH latest_qc_decision AS (
                    SELECT DISTINCT ON (h.submission_key)
                        h.submission_key,
                        h.new_status
                    FROM qc.case_status_history h
                    JOIN app.user_role ur
                      ON ur.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'listing'
                      AND LOWER(COALESCE(h.new_status, '')) IN ('approved', 'rejected')
                      AND ur.role_code = ANY(%s)
                    ORDER BY h.submission_key, h.changed_at DESC, h.status_history_id DESC
                ),
                eligible_updates AS (
                    SELECT
                        s.submission_key,
                        latest_qc_decision.new_status
                    FROM clean.hh_sampling_ea s
                    JOIN latest_qc_decision
                      ON latest_qc_decision.submission_key = s.submission_key
                    WHERE LOWER(COALESCE(s.approval_status, '')) IN ('approved', 'rejected')
                )
                UPDATE clean.hh_listing_long l
                SET approval_status = eligible_updates.new_status,
                    updated_at = now()
                FROM eligible_updates
                WHERE l.submission_key = eligible_updates.submission_key
                  AND LOWER(COALESCE(l.approval_status, '')) <> LOWER(COALESCE(eligible_updates.new_status, ''))
                """,
                (list(LISTING_REVIEW_DECISION_ROLES),),
            )
            cur.execute(
                """
                WITH latest_qc_decision AS (
                    SELECT DISTINCT ON (h.submission_key)
                        h.submission_key,
                        h.new_status
                    FROM qc.case_status_history h
                    JOIN app.user_role ur
                      ON ur.user_id = h.changed_by_user_id
                    WHERE h.instrument_code = 'listing'
                      AND LOWER(COALESCE(h.new_status, '')) IN ('approved', 'rejected')
                      AND ur.role_code = ANY(%s)
                    ORDER BY h.submission_key, h.changed_at DESC, h.status_history_id DESC
                )
                INSERT INTO clean.listing_case_status (
                    submission_key,
                    ea_id,
                    boundary_id,
                    current_status,
                    updated_at
                )
                SELECT
                    s.submission_key,
                    s.ea_id,
                    s.boundary_id,
                    latest_qc_decision.new_status,
                    now()
                FROM clean.hh_sampling_ea s
                JOIN latest_qc_decision
                  ON latest_qc_decision.submission_key = s.submission_key
                ON CONFLICT (submission_key) DO UPDATE
                SET current_status = EXCLUDED.current_status,
                    updated_at = now()
                """,
                (list(LISTING_REVIEW_DECISION_ROLES),),
            )
        conn.commit()


def create_pending_change(
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
                VALUES ('listing', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    SET issue_status = 'in_review',
                        updated_at = now()
                    WHERE issue_id = %s AND submission_key = %s
                    """,
                    (issue_id, submission_key),
                )
            _apply_pending_change_review(
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


def _apply_pending_change_review(
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
            sql.SQL("SELECT record FROM {} WHERE {} = %s").format(sql.SQL(table_name), sql.Identifier(key_column)),
            (lookup_value,),
        )
        current_row = cur.fetchone()
        current_record = current_row["record"] if current_row else {}
        if not isinstance(current_record, dict):
            current_record = {}
        parsed_value = _normalize_json_value(change["proposed_value"] or "")
        current_record[change["field_name"]] = parsed_value

        assignments = [sql.SQL("record = %s::jsonb")]
        params: list[Any] = [_serialize_record(current_record)]
        if change["field_name"] in STRUCTURED_FIELDS.get(table_name, set()):
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(change["field_name"])))
            params.append(_coerce_structured_value(change["field_name"], change["proposed_value"] or ""))
        if table_name != "clean.hh_selected_long":
            assignments.append(sql.SQL("updated_at = now()"))

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
            VALUES ('listing', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                (note or "Correction approved and applied to the database.", change["issue_id"]),
            )
    elif change.get("issue_id"):
        cur.execute(
            """
            UPDATE qc.issue_queue
            SET issue_status = 'pending_review',
                updated_at = now(),
                resolution_note = %s
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


def review_pending_change(
    settings: Settings,
    user: AuthUser,
    change_id: str,
    decision: str,
    note: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    if user.role not in LISTING_REVIEW_DECISION_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to review corrections.")
    if decision not in LISTING_FINAL_STATUSES:
        raise HTTPException(status_code=400, detail="Decision must be approved or rejected.")

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            result = _apply_pending_change_review(cur, user, change_id, decision, note, device_id)
        conn.commit()

    return result


def apply_analysis_correction(
    settings: Settings,
    user: AuthUser,
    submission_key: str,
    field_name: str,
    old_value: str,
    new_value: str,
    question_label: str,
    corrected_by_username: str,
) -> dict[str, Any]:
    """Directly apply a field correction to hh_listing_long for a given submission_key.

    Updates all rows in clean.hh_listing_long where submission_key matches and the
    record field currently holds old_value. Logs the change to qc.data_change_log.
    """
    if user.role not in EDIT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to apply corrections.")

    table_name = "clean.hh_listing_long"

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            # Fetch all rows for this submission_key where the field matches old_value
            cur.execute(
                sql.SQL(
                    "SELECT listing_row_id, record FROM {} WHERE submission_key = %s"
                ).format(sql.SQL(table_name)),
                (submission_key,),
            )
            rows = cur.fetchall()

            updated = 0
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

                assignments = [sql.SQL("record = %s::jsonb")]
                params: list[Any] = [_serialize_record(record)]
                if field_name in STRUCTURED_FIELDS.get(table_name, set()):
                    assignments.append(sql.SQL("{} = %s").format(sql.Identifier(field_name)))
                    params.append(_coerce_structured_value(field_name, new_value))
                assignments.append(sql.SQL("updated_at = now()"))
                params.append(row["listing_row_id"])

                cur.execute(
                    sql.SQL("UPDATE {} SET {} WHERE listing_row_id = %s").format(
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
                    VALUES ('listing', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        submission_key,
                        table_name,
                        str(row["listing_row_id"]),
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


def manual_sync(
    settings: Settings,
    user: AuthUser,
    device_id: str | None = None,
    client_ip: str | None = None,
    forwarded_for: str | None = None,
) -> dict[str, Any]:
    request_started_at = time.monotonic()
    sync_request_token = str(uuid4())
    pipeline_config = load_listing_pipeline_config(
        settings.root_dir,
        sync_source="manual",
        sync_request_token=sync_request_token,
    )
    logger.info(
        "Manual survey sync requested by user=%s user_id=%s role=%s device_id=%s client_ip=%s forwarded_for=%s request_token=%s",
        user.username,
        user.id,
        user.role,
        device_id or "-",
        client_ip or "-",
        forwarded_for or "-",
        sync_request_token,
    )
    request_manual_sync_override(
        pipeline_config,
        sync_request_token,
        user.username,
        "Manual full sync takes priority over cron/background SurveyCTO work.",
    )

    try:
        listing_result = run_listing_sync_job(
            source="manual",
            sync_request_token=sync_request_token,
        )
    except Exception as exc:
        status_code, detail = describe_sync_failure(exc)
        raise HTTPException(status_code=status_code, detail=f"Listing sync failed: {detail}") from exc
    finally:
        clear_manual_sync_override(pipeline_config, sync_request_token)

    listing_status = str(listing_result.get("status") or "unknown") if isinstance(listing_result, dict) else "unknown"
    if listing_status in {"success", "warning"}:
        qc_result = refresh_listing_qc(settings)
        coverage_approval_result = apply_ea_coverage_approvals(settings)
        boundary_result = sync_boundaries_to_db(settings)
    else:
        qc_result = {"message": "QC refresh skipped because listing sync did not complete successfully."}
        coverage_approval_result = {
            "message": "EA coverage approval refresh skipped because listing sync did not complete successfully."
        }
        boundary_result = {"message": "Boundary refresh skipped because listing sync did not complete successfully."}

    message = _sync_step_message("Listing sync", listing_result if isinstance(listing_result, dict) else {"status": listing_status})
    total_seconds = time.monotonic() - request_started_at
    logger.info(
        "Manual listing sync finished for user=%s total_seconds=%.3f",
        user.username,
        total_seconds,
    )
    return {
        "message": message,
        "sync": listing_result,
        "qc": qc_result,
        "coverageApproval": coverage_approval_result,
        "boundaries": boundary_result,
    }


def list_exports(settings: Settings, user: AuthUser) -> list[dict[str, Any]]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    fc.file_id::text AS file_id,
                    fc.export_job_id::text AS export_job_id,
                    fc.export_profile,
                    fc.export_format,
                    fc.file_name,
                    fc.file_path,
                    fc.generated_at,
                    fc.row_count,
                    fc.byte_size
                FROM export.file_catalog fc
                JOIN export.export_job ej
                    ON ej.export_job_id = fc.export_job_id
                WHERE fc.instrument_code = 'listing'
                  AND ej.requested_by_user_id = %s
                  AND ej.job_status = 'completed'
                ORDER BY generated_at DESC
                LIMIT 100
                """,
                (user.id,),
            )
            return cur.fetchall()


def clear_exports(settings: Settings, user: AuthUser) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM export.export_job
                WHERE instrument_code = 'listing'
                  AND requested_by_user_id = %s
                """,
                (user.id,),
            )
            deleted = cur.rowcount
        conn.commit()
    return {"deleted": deleted}



def _normalize_export_statuses(user: AuthUser, statuses: list[str] | None) -> list[str]:
    if user.role == "client":
        return ["approved"]
    return statuses or ["approved"]


def _normalize_excel_cell_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is not None:
            value = value.tz_convert(None)
        return value.to_pydatetime()
    return value


def _write_dataframe_to_xlsx(path: Path, df: pd.DataFrame, sheet_name: str = "Sheet1") -> None:
    # openpyxl write_only mode is much faster and uses far less memory than pandas.to_excel
    # for large delivery exports.
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet(title=(sheet_name or "Sheet1")[:31])
    worksheet.append([str(column) for column in df.columns])
    for row in df.itertuples(index=False, name=None):
        worksheet.append([_normalize_excel_cell_value(value) for value in row])
    workbook.save(path)

def create_export(settings: Settings, user: AuthUser, dataset: str, export_format: str, statuses: list[str] | None = None) -> dict[str, Any]:
    statuses = _normalize_export_statuses(user, statuses)

    return _create_export_artifact(
        settings,
        dataset,
        export_format,
        statuses,
        requested_by_user_id=user.id,
        job_message_prefix="Generated",
    )


def queue_export(settings: Settings, user: AuthUser, dataset: str, export_format: str, statuses: list[str] | None = None) -> dict[str, Any]:
    dataset_map = {"listing_long", "sampling_ea", "selected_long"}
    if dataset not in dataset_map:
        raise HTTPException(status_code=400, detail="Unsupported dataset.")
    if export_format not in {"csv", "xlsx", "sav"}:
        raise HTTPException(status_code=400, detail="Unsupported export format.")

    statuses = _normalize_export_statuses(user, statuses)

    export_job_id = str(uuid4())
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO export.export_job (
                    export_job_id,
                    instrument_code,
                    export_profile,
                    export_format,
                    run_scope,
                    job_status,
                    requested_by_user_id,
                    started_at,
                    job_message
                )
                VALUES (%s, 'listing', %s, %s, %s, 'running', %s, now(), %s)
                """,
                (
                    export_job_id,
                    dataset,
                    export_format,
                    ",".join(statuses),
                    user.id,
                    "Export generation is running.",
                ),
            )
        conn.commit()

    return {
        "queued": True,
        "exportJobId": export_job_id,
        "fileId": export_job_id,
        "fileName": f"{dataset}_{export_format}_running",
        "message": "Export generation started. The download will appear when the file is ready.",
        "statuses": statuses,
    }


def run_queued_export(
    settings: Settings,
    export_job_id: str,
    requested_by_user_id: str,
    dataset: str,
    export_format: str,
    statuses: list[str],
) -> None:
    try:
        _create_export_artifact(
            settings,
            dataset,
            export_format,
            statuses,
            requested_by_user_id=requested_by_user_id,
            job_message_prefix="Generated",
            export_job_id=export_job_id,
        )
    except Exception as exc:
        logger.exception("Queued listing export failed for job %s.", export_job_id)
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE export.export_job
                    SET job_status = 'failed',
                        finished_at = now(),
                        job_message = %s
                    WHERE export_job_id = %s
                    """,
                    (str(exc)[:1000], export_job_id),
                )
            conn.commit()


def _create_export_artifact(
    settings: Settings,
    dataset: str,
    export_format: str,
    statuses: list[str],
    requested_by_user_id: str | None,
    job_message_prefix: str,
    export_job_id: str | None = None,
) -> dict[str, Any]:
    dataset_map = {
        "listing_long": "clean.hh_listing_long",
        "sampling_ea": "clean.hh_sampling_ea",
        "selected_long": "clean.hh_selected_long",
    }
    if dataset not in dataset_map:
        raise HTTPException(status_code=400, detail="Unsupported dataset.")
    if export_format not in {"csv", "xlsx", "sav"}:
        raise HTTPException(status_code=400, detail="Unsupported export format.")

    df = _load_export_dataframe(settings, dataset, statuses)
    value_labels, variable_labels = _load_xlsform_labels(settings, "listing")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_job_id = export_job_id or str(uuid4())
    stem = f"listing_{dataset}_{'_'.join(statuses)}_{timestamp}"

    if export_format == "csv":
        path = settings.export_dir / f"{stem}.csv"
        df_prepared = _prepare_listing_delivery_dataframe(settings, dataset, df)
        _apply_export_labels(df_prepared, value_labels).to_csv(path, index=False, encoding="utf-8-sig")
    elif export_format == "xlsx":
        path = settings.export_dir / f"{stem}.xlsx"
        _write_dataframe_to_xlsx(path, _prepare_excel_dataframe(_prepare_listing_delivery_dataframe(settings, dataset, df)))
    else:
        sav_path = settings.export_dir / f"{stem}.sav"
        if dataset == "listing_long":
            template_path = settings.root_dir / "HH_listing_Export_Template.sav"
            df_prepared = _prepare_listing_delivery_dataframe(settings, dataset, df)
            _save_sav_from_template(df_prepared, sav_path, template_path)
        else:
            df_prepared = _prepare_listing_delivery_dataframe(settings, dataset, df)
            _save_sav(df_prepared, sav_path, value_labels, variable_labels)
        path = settings.export_dir / f"{stem}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            zf.write(sav_path, sav_path.name)
        sav_path.unlink()

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE export.file_catalog
                SET is_active = false
                WHERE instrument_code = 'listing'
                  AND export_profile = %s
                  AND export_format = %s
                """,
                (dataset, export_format),
            )
            cur.execute(
                """
                INSERT INTO export.export_job (
                    export_job_id,
                    instrument_code,
                    export_profile,
                    export_format,
                    run_scope,
                    job_status,
                    requested_by_user_id,
                    started_at,
                    finished_at,
                    job_message
                )
                VALUES (%s, 'listing', %s, %s, %s, 'completed', %s, now(), now(), %s)
                ON CONFLICT (export_job_id) DO UPDATE SET
                    job_status = 'completed',
                    started_at = COALESCE(export_job.started_at, EXCLUDED.started_at),
                    finished_at = EXCLUDED.finished_at,
                    job_message = EXCLUDED.job_message
                """,
                (
                    export_job_id,
                    dataset,
                    export_format,
                    ",".join(statuses),
                    requested_by_user_id,
                    f"{job_message_prefix} {len(df)} rows.",
                ),
            )
            file_id = str(uuid4())
            cur.execute(
                """
                INSERT INTO export.file_catalog (
                    file_id,
                    export_job_id,
                    instrument_code,
                    export_profile,
                    export_format,
                    file_name,
                    file_path,
                    row_count,
                    byte_size,
                    generated_at,
                    is_active
                )
                VALUES (%s, %s, 'listing', %s, %s, %s, %s, %s, %s, now(), true)
                """,
                (
                    file_id,
                    export_job_id,
                    dataset,
                    export_format,
                    path.name,
                    str(path),
                    len(df),
                    path.stat().st_size,
                ),
            )
        conn.commit()

    return {"fileId": file_id, "exportJobId": export_job_id, "fileName": path.name, "path": str(path)}


def _latest_export_generated_at(settings: Settings, dataset: str, export_format: str, run_scope: str) -> datetime | None:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(fc.generated_at) AS generated_at
                FROM export.file_catalog fc
                JOIN export.export_job ej ON ej.export_job_id = fc.export_job_id
                WHERE fc.instrument_code = 'listing'
                  AND fc.export_profile = %s
                  AND fc.export_format = %s
                  AND ej.run_scope = %s
                """,
                (dataset, export_format, run_scope),
            )
            row = cur.fetchone()
    return row["generated_at"] if row and row.get("generated_at") else None


def regenerate_scheduled_exports(settings: Settings) -> dict[str, Any]:
    generated: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)
    statuses = ["approved"]
    run_scope = ",".join(statuses)

    for dataset in SCHEDULED_EXPORT_DATASETS:
        for export_format in SCHEDULED_EXPORT_FORMATS:
            latest_generated_at = _latest_export_generated_at(settings, dataset, export_format, run_scope)
            if latest_generated_at:
                age_seconds = (now - latest_generated_at).total_seconds()
                if age_seconds < settings.export_regen_interval_seconds:
                    skipped.append({"dataset": dataset, "format": export_format, "reason": "fresh"})
                    continue
            artifact = _create_export_artifact(
                settings,
                dataset,
                export_format,
                statuses,
                requested_by_user_id=None,
                job_message_prefix="Scheduled regeneration generated",
            )
            generated.append({"dataset": dataset, "format": export_format, "fileName": artifact["fileName"]})

    return {"generated": generated, "skipped": skipped}


def _save_sav(
    df: pd.DataFrame,
    path: Path,
    value_labels: dict[str, dict[str, str]] | None = None,
    variable_labels: dict[str, str] | None = None,
    spss_rename_map: dict[str, str] | None = None,
    ordered_column_labels: list[str] | None = None,
) -> None:
    """Write df as a SPSS .sav file.

    When spss_rename_map and ordered_column_labels are provided (listing_long template
    path), those are used verbatim.  Otherwise names are auto-generated and labels are
    derived from variable_labels.
    """
    out = df.copy()

    if spss_rename_map is not None:
        rename_map = spss_rename_map
    else:
        rename_map = {col: str(col).replace(" ", "_") for col in out.columns}

    # Build ordered column_labels list (one entry per column in out)
    col_labels_list: list[str] = []
    if ordered_column_labels is not None:
        col_labels_list = ordered_column_labels
    elif variable_labels:
        for original_col in df.columns:
            col_labels_list.append(variable_labels.get(original_col, original_col))

    # Normalize labeled columns against their actual dtypes before renaming for SPSS.
    spss_value_labels: dict[str, dict[Any, str]] = {}
    if value_labels:
        for original_col, labels in value_labels.items():
            if original_col not in out.columns:
                continue

            series = out[original_col]
            normalized_labels: dict[Any, str] = {}

            if pd.api.types.is_bool_dtype(series):
                out[original_col] = series.astype("int64")
                for raw_key, label in labels.items():
                    lowered = str(raw_key).strip().lower()
                    if lowered in {"1", "true", "yes"}:
                        normalized_labels[1] = label
                    elif lowered in {"0", "false", "no"}:
                        normalized_labels[0] = label
            elif pd.api.types.is_numeric_dtype(series):
                for raw_key, label in labels.items():
                    text = str(raw_key).strip()
                    try:
                        numeric = float(text)
                    except ValueError:
                        continue
                    normalized_key: int | float = int(numeric) if numeric.is_integer() else numeric
                    normalized_labels[normalized_key] = label
            else:
                for raw_key, label in labels.items():
                    normalized_labels[str(raw_key)] = label

            renamed = rename_map.get(original_col)
            if renamed and normalized_labels:
                spss_value_labels[renamed] = normalized_labels

    out = out.rename(columns=rename_map)

    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]):
            out[col] = out[col].astype(str).replace("nan", "")

    pyreadstat.write_sav(
        out,
        str(path),
        variable_value_labels=spss_value_labels if spss_value_labels else None,
        column_labels=col_labels_list if col_labels_list else None,
    )


def _prepare_excel_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        dtype = out[col].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_localize(None)
    return out


# Each entry: (canonical_export_name, [sources_in_priority_order])
# The first source found is renamed; remaining duplicates are dropped.
_SAMPLING_CANONICAL_SOURCES: list[tuple[str, list[str]]] = [
    ("CompletionDate", ["completion_date", "completiondate", "_sampling_completion_date", "end"]),
    ("SubmissionDate", ["submission_date", "submissiondate", "_sampling_submission_date", "start"]),
    ("KEY", ["key", "submission_key"]),
    ("NBLD", ["nbld", "overall_structures"]),
    ("sel_GPS", ["sel_gps"]),
]

_LISTING_CANONICAL_SOURCES: dict[str, list[str]] = {
    "CompletionDate": ["completion_date", "completiondate", "_sampling_completion_date", "end"],
    "SubmissionDate": ["submission_date", "submissiondate", "_sampling_submission_date", "start"],
    "devicephonenum": ["device_phone_num", "device_phone_number", "phone_no", "phoneno"],
    "duration": ["Duration", "interview_duration"],
    "caseid": ["case_id", "caseID"],
    "polygon_id": ["boundary_id", "_sampling_boundary_id", "boundary_code", "ea_boundary_id"],
    "NBLD": ["nbld", "overall_structures", "total_structures"],
    "formdef_version": ["form_def_version", "form_version", "__version__"],
    "KEY": ["key", "submission_key"],
}


def _is_blank_export_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return bool(pd.isna(value))
    except Exception:
        return False


def _first_nonblank_export_value(row: pd.Series, sources: list[str]) -> Any:
    for source in sources:
        if source not in row.index:
            continue
        value = row.get(source)
        if not _is_blank_export_value(value):
            return value
    return None


def _parse_export_record_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _merge_export_record_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Expand JSON record columns into ordinary dataframe columns as fallbacks."""
    if df.empty or not any(col in df.columns for col in ("_sampling_record", "record")):
        return df

    out = df.copy()
    needed_keys = {column_name for _spss_name, column_name, _label in LISTING_LONG_TEMPLATE}
    needed_keys.update(_LISTING_CANONICAL_SOURCES)
    for aliases in _LISTING_CANONICAL_SOURCES.values():
        needed_keys.update(aliases)
    for _target, aliases in _SAMPLING_CANONICAL_SOURCES:
        needed_keys.update(aliases)

    record_columns = [col for col in ("_sampling_record", "record") if col in out.columns]
    for record_column in record_columns:
        payloads = []
        for value in out[record_column]:
            payload = _parse_export_record_payload(value)
            payloads.append({key: payload.get(key) for key in needed_keys if key in payload})
        if not payloads:
            continue

        payload_df = pd.DataFrame(payloads, index=out.index)
        for key in payload_df.columns:
            if key not in out.columns:
                out[key] = payload_df[key]
                continue
            blank_mask = out[key].isna() | (out[key].astype("string").str.strip() == "")
            if blank_mask.any():
                current = out[key].astype(object)
                replacement = payload_df[key].astype(object)
                out[key] = current.where(~blank_mask, replacement)
    return out


def _apply_listing_canonical_backfills(df: pd.DataFrame) -> pd.DataFrame:
    """Populate delivery-template variables from known database/raw aliases."""
    if df.empty:
        return df

    out = _merge_export_record_columns(df)
    for target, aliases in _LISTING_CANONICAL_SOURCES.items():
        sources = [target, *aliases]
        if target not in out.columns:
            out[target] = None
        present_sources = [source for source in sources if source in out.columns]
        if not present_sources:
            continue
        candidates = out[present_sources].copy()
        candidates = candidates.mask(candidates.astype("string").apply(lambda col: col.str.strip() == ""))
        out[target] = candidates.bfill(axis=1).iloc[:, 0]
    return out


def _apply_sampling_renames(df: pd.DataFrame) -> pd.DataFrame:
    """Rename DB column variants to canonical export names, dropping duplicate sources."""
    result = _apply_listing_canonical_backfills(df)
    renames: dict[str, str] = {}
    drop_cols: list[str] = []
    for target, sources in _SAMPLING_CANONICAL_SOURCES:
        if target in result.columns:
            drop_cols.extend(s for s in sources if s in result.columns)
            continue
        chosen = False
        for src in sources:
            if src not in result.columns:
                continue
            if not chosen:
                renames[src] = target
                chosen = True
            else:
                drop_cols.append(src)
    result = result.drop(columns=[c for c in drop_cols if c in result.columns])
    return result.rename(columns=renames) if renames else result


def _reorder_columns(df: pd.DataFrame, order: list[str]) -> pd.DataFrame:
    """Return df with columns in `order` first, followed by any remaining columns not in `order`."""
    present = [c for c in order if c in df.columns]
    extra = [c for c in df.columns if c not in set(order)]
    cols: list[str] = present + extra
    result: pd.DataFrame = df[cols].copy()
    return result


def _prepare_listing_delivery_dataframe(settings: Settings, dataset: str, df: pd.DataFrame) -> pd.DataFrame:
    if dataset in ("sampling_ea", "selected_long"):
        working = _apply_sampling_renames(df)
        order = SAMPLING_EA_COLUMN_ORDER if dataset == "sampling_ea" else SELECTED_LONG_COLUMN_ORDER
        result = working.reindex(columns=order).copy()
        if "approval_status" in working.columns:
            result["approval_status"] = working["approval_status"]
        return result
    if dataset != "listing_long":
        return df.copy()

    # Rename any SPSS-named columns (e.g. V48_A) to their platform variable names
    # (e.g. no_of_male_15_17yrs) so the template reindex below can find them.
    spss_to_var = {
        spss: var
        for spss, var, _ in LISTING_LONG_TEMPLATE
        if spss != var and spss in df.columns and var not in df.columns
    }
    working = df.rename(columns=spss_to_var) if spss_to_var else df
    working = _apply_listing_canonical_backfills(working)

    ordered = working.reindex(columns=[column_name for _spss_name, column_name, _label in LISTING_LONG_TEMPLATE]).copy()

    if "approval_status" in working.columns and "status" not in ordered.columns:
        ordered["status"] = working["approval_status"]
    for col in working.columns:
        if col not in ordered.columns:
            ordered[col] = working[col]
    return ordered


def _load_xlsform_labels(
    settings: Settings, instrument_code: str
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Return (value_labels, variable_labels) from the XLSForm dictionary.

    value_labels  — {variable_name: {choice_code: choice_label}}
    variable_labels — {variable_name: question_label}

    Returns empty dicts when the XLSForm tables have not been populated yet.
    """
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT q.variable_name,
                       q.question_label,
                       q.choice_list_name,
                       c.choice_code,
                       c.choice_label
                FROM reference.xlsform_question q
                LEFT JOIN reference.xlsform_choice c
                    ON c.instrument_code = q.instrument_code
                   AND c.list_name = q.choice_list_name
                WHERE q.instrument_code = %s
                """,
                (instrument_code,),
            )
            rows = cur.fetchall()

    value_labels: dict[str, dict[str, str]] = {}
    variable_labels: dict[str, str] = {}
    for row in rows:
        var = row["variable_name"]
        if row.get("question_label"):
            variable_labels[var] = row["question_label"]
        if row.get("choice_list_name") and row.get("choice_code") and row.get("choice_label"):
            value_labels.setdefault(var, {})[str(row["choice_code"])] = row["choice_label"]
    return value_labels, variable_labels


def _apply_export_labels(df: pd.DataFrame, value_labels: dict[str, dict[str, str]]) -> pd.DataFrame:
    """Insert {col}_label columns immediately after each column that has a choice mapping."""
    if not value_labels:
        return df
    result = df.copy()
    for col in list(df.columns):
        mapping = value_labels.get(col)
        if not mapping:
            continue
        label_series = result[col].astype(str).map(lambda v, m=mapping: m.get(v, ""))
        pos = result.columns.get_loc(col) + 1
        result.insert(pos, f"{col}_label", label_series)
    return result


def _sanitize_template_labels_for_sav(df: pd.DataFrame, meta: Any, columns: list[str]) -> tuple[pd.DataFrame, dict[str, dict[Any, str]]]:
    out = df.copy()
    original_types = dict(getattr(meta, "original_variable_types", {}) or {})
    template_vvl = dict(meta.variable_value_labels or {})
    sanitized_labels: dict[str, dict[Any, str]] = {}

    for col in columns:
        if col not in out.columns:
            continue
        raw_type = str(original_types.get(col, '') or '').strip().upper()
        labels = template_vvl.get(col) or {}
        numeric = False
        is_datetime = bool(raw_type and ('DATE' in raw_type or 'TIME' in raw_type) and not raw_type.startswith('A'))
        is_string = bool(raw_type.startswith('A'))

        if raw_type and not is_string and not is_datetime:
            numeric = True
        else:
            numeric = bool(labels) and not is_string and all(
                isinstance(key, (int, float)) or str(key).strip().replace('.', '', 1).replace('-', '', 1).isdigit()
                for key in labels.keys()
            )

        series = out[col]
        if is_datetime:
            out[col] = _parse_listing_sav_datetime(series)
        elif is_string:
            def _stringify_sav_value(value: Any) -> str:
                if _is_blank_export_value(value):
                    return ''
                if isinstance(value, float) and value.is_integer():
                    return str(int(value))
                return str(value)

            out[col] = series.map(_stringify_sav_value)
        elif pd.api.types.is_bool_dtype(series):
            out[col] = series.astype('int64')
            numeric = True
        elif numeric:
            out[col] = pd.to_numeric(series, errors='coerce')
        elif pd.api.types.is_object_dtype(series):
            out[col] = series.astype(str).replace({'nan': '', 'None': '', 'NaT': ''})

        cleaned: dict[Any, str] = {}
        for raw_key, raw_label in labels.items():
            if numeric:
                try:
                    number = float(raw_key)
                except (TypeError, ValueError):
                    continue
                cleaned[int(number) if number.is_integer() else number] = str(raw_label or '')
            else:
                cleaned[str(raw_key)] = str(raw_label or '')
        if cleaned:
            sanitized_labels[col] = cleaned

    return out, sanitized_labels


def _parse_listing_sav_datetime(series: pd.Series) -> pd.Series:
    """Parse SurveyCTO date strings for SPSS DATETIME template columns.

    Pandas 2.x/3.x is strict about mixing timezone-aware and timezone-naive
    datetimes. Listing data can contain both browser-style strings, such as
    ``Wed Apr 08 2026 11:50:16 GMT+0000 (...)``, and ISO strings, such as
    ``2026-04-08 11:50:16+00:00``. Parse both paths as UTC, then drop the
    timezone before writing to SPSS so the column has one stable dtype.
    """
    text = series.astype("string")
    js_datetime = (
        text.str.replace(r"\s*\([^)]*\)\s*$", "", regex=True)
        .str.replace(r"\s+GMT[+-]\d{4}\s*$", "", regex=True)
    )

    parsed = pd.to_datetime(
        js_datetime,
        format="%a %b %d %Y %H:%M:%S",
        errors="coerce",
        utc=True,
    )
    fallback = pd.to_datetime(text, errors="coerce", utc=True, format="mixed")
    parsed = parsed.combine_first(fallback)

    # SPSS DATETIME values should be timezone-naive; keep the same clock instant
    # in UTC and avoid assigning tz-aware values into a tz-naive Series.
    try:
        parsed = parsed.dt.tz_convert(None)
    except (AttributeError, TypeError):
        parsed = pd.to_datetime(parsed, errors="coerce")
    return parsed.astype("datetime64[ns]")


def _save_sav_from_template(df: pd.DataFrame, path: Path, template_path: Path) -> None:
    """Write df as a SPSS .sav using the exact structure from the template file.

    Column order, variable names, variable labels, and value labels are taken
    verbatim from template_path.  approval_status is appended as the last column
    with variable label "Approval Status".  Template columns absent from df are
    filled with None; df columns not in the template are dropped.
    """
    _, meta = pyreadstat.read_sav(str(template_path), row_limit=0)
    template_cols: list[str] = list(meta.column_names)

    out = pd.DataFrame()
    for col in template_cols:
        out[col] = df[col] if col in df.columns else None

    # Append approval_status as the last column
    out["status"] = df["approval_status"] if "approval_status" in df.columns else None

    # Column labels: one per column in the same order
    raw_labels = list(meta.column_labels) if meta.column_labels else []
    # meta.column_labels may be shorter than template_cols if some are blank
    col_labels: list[str] = []
    for i, col in enumerate(template_cols):
        col_labels.append(raw_labels[i] if i < len(raw_labels) else col)
    col_labels.append("Approval Status")

    # Value labels and variable-view metadata from the template
    raw_vvl = dict(meta.variable_value_labels or {})
    var_measure = dict(meta.variable_measure or {})
    var_display_width = dict(meta.variable_display_width or {})
    var_format = dict(getattr(meta, "original_variable_types", {}) or {})
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

    out, vvl = _sanitize_template_labels_for_sav(out, meta, list(out.columns))

    # Normalise dtypes so pyreadstat can serialise cleanly
    for col in out.columns:
        series = out[col]
        if pd.api.types.is_bool_dtype(series):
            out[col] = series.astype("int64")
        elif pd.api.types.is_object_dtype(series):
            out[col] = series.astype(str).replace({"nan": "", "None": "", "NaT": ""})

    write_kwargs = {
        "column_labels": col_labels,
        "variable_value_labels": vvl if vvl else None,
        "variable_measure": {col: value for col, value in var_measure.items() if col in out.columns},
        "variable_display_width": {col: value for col, value in var_display_width.items() if col in out.columns},
        "variable_format": {col: value for col, value in var_format.items() if col in out.columns},
        "missing_ranges": {col: value for col, value in missing_ranges.items() if col in out.columns and value},
    }
    write_kwargs = {k: v for k, v in write_kwargs.items() if v}

    try:
        pyreadstat.write_sav(
            out,
            str(path),
            **write_kwargs,
        )
    except Exception:
        fallback_kwargs = dict(write_kwargs)
        fallback_kwargs.pop("missing_ranges", None)
        fallback_kwargs.pop("variable_format", None)
        pyreadstat.write_sav(
            out,
            str(path),
            **fallback_kwargs,
        )


def _load_export_dataframe(settings: Settings, dataset: str, statuses: list[str]) -> pd.DataFrame:
    table_name = {
        "listing_long": "clean.hh_listing_long",
        "sampling_ea": "clean.hh_sampling_ea",
        "selected_long": "clean.hh_selected_long",
    }[dataset]

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            if dataset == "selected_long":
                cur.execute(
                    """
                    SELECT
                        sl.*,
                        se.approval_status,
                        se.submission_date AS _sampling_submission_date,
                        se.completion_date AS _sampling_completion_date,
                        se.ea_id AS _sampling_ea_id,
                        se.boundary_id AS _sampling_boundary_id,
                        se.record AS _sampling_record
                    FROM clean.hh_selected_long sl
                    JOIN clean.hh_sampling_ea se ON se.submission_key = sl.submission_key
                    WHERE se.approval_status = ANY(%s)
                    ORDER BY
                        COALESCE(se.completion_date, se.submission_date) NULLS LAST,
                        COALESCE(NULLIF(TRIM(se.ea_id), ''), se.record->>'ea_id', sl.record->>'ea_id') NULLS LAST,
                        CASE
                            WHEN NULLIF(TRIM(sl.record->>'sel_structure_no'), '') ~ '^[0-9]+(\\.[0-9]+)?$'
                                THEN NULLIF(TRIM(sl.record->>'sel_structure_no'), '')::numeric
                            ELSE NULL
                        END NULLS LAST,
                        NULLIF(TRIM(sl.record->>'sel_structure_no'), '') NULLS LAST,
                        sl.selected_repeat_no NULLS LAST,
                        sl.submission_key
                    """,
                    (statuses,),
                )
            elif dataset == "listing_long":
                cur.execute(
                    """
                    SELECT
                        l.*,
                        se.submission_date AS _sampling_submission_date,
                        se.completion_date AS _sampling_completion_date,
                        se.ea_id AS _sampling_ea_id,
                        se.boundary_id AS _sampling_boundary_id,
                        se.record AS _sampling_record
                    FROM clean.hh_listing_long l
                    LEFT JOIN clean.hh_sampling_ea se ON se.submission_key = l.submission_key
                    WHERE l.approval_status = ANY(%s)
                      AND COALESCE(l.record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                    ORDER BY
                        COALESCE(se.completion_date, se.submission_date) NULLS LAST,
                        COALESCE(NULLIF(TRIM(l.ea_id), ''), NULLIF(TRIM(se.ea_id), ''), se.record->>'ea_id', l.record->>'ea_id') NULLS LAST,
                        CASE
                            WHEN NULLIF(TRIM(l.record->>'structure_no'), '') ~ '^[0-9]+(\\.[0-9]+)?$'
                                THEN NULLIF(TRIM(l.record->>'structure_no'), '')::numeric
                            ELSE NULL
                        END NULLS LAST,
                        NULLIF(TRIM(l.record->>'structure_no'), '') NULLS LAST,
                        l.building_no NULLS LAST,
                        l.household_no_within_building NULLS LAST,
                        l.submission_key,
                        l.listing_row_id
                    """,
                    (statuses,),
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM clean.hh_sampling_ea
                    WHERE approval_status = ANY(%s)
                    ORDER BY
                        COALESCE(completion_date, submission_date) NULLS LAST,
                        COALESCE(NULLIF(TRIM(ea_id), ''), record->>'ea_id') NULLS LAST,
                        submission_key
                    """,
                    (statuses,),
                )
            rows = cur.fetchall()

    flattened = []
    for row in rows:
        record = row.get("record") or {}
        sampling_record = row.get("_sampling_record") or {}
        merged = {k: v for k, v in row.items() if k not in {"record", "_sampling_record"}}
        for source_record in (sampling_record, record):
            if isinstance(source_record, str):
                try:
                    source_record = json.loads(source_record)
                except json.JSONDecodeError:
                    source_record = {}
            if isinstance(source_record, dict):
                for key, value in source_record.items():
                    if key not in merged or merged.get(key) in (None, ""):
                        merged[key] = value
        if dataset == "listing_long":
            for key, value in {
                "CompletionDate": merged.get("_sampling_completion_date"),
                "SubmissionDate": merged.get("_sampling_submission_date"),
                "polygon_id": merged.get("_sampling_boundary_id"),
                "KEY": merged.get("submission_key"),
            }.items():
                if key not in merged or merged.get(key) in (None, ""):
                    merged[key] = value
        flattened.append(merged)
    dataframe = pd.DataFrame(flattened)
    return apply_multiselect_yes_no_indicators(settings, "listing", dataframe)


def get_export_file(settings: Settings, user: AuthUser, file_id: str) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    fc.file_id::text AS file_id,
                    fc.file_name,
                    fc.file_path,
                    fc.export_format
                FROM export.file_catalog fc
                JOIN export.export_job ej
                    ON ej.export_job_id = fc.export_job_id
                WHERE fc.file_id = %s
                  AND ej.requested_by_user_id = %s
                """,
                (file_id, user.id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Export file not found.")
    return row


def get_interviewer_productivity(settings: Settings, user: AuthUser) -> list[dict[str, Any]]:
    """Return per-interviewer QC productivity stats for the listing survey."""
    rule_codes = [rule[0] for rule in RULE_DEFINITIONS]

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(s.interviewer_id::text, 'Unknown') AS interviewer_id,
                    s.submission_key,
                    s.approval_status,
                    COALESCE(hh.household_count, 0)::int AS household_count,
                    COALESCE(hh.building_count, 0)::int AS building_count,
                    COALESCE(hh.sampled_count, 0)::int AS sampled_count,
                    COALESCE(iq.open_issue_count, 0)::int AS open_issue_count,
                    COALESCE(iq.total_issue_count, 0)::int AS total_issue_count
                FROM clean.hh_sampling_ea s
                LEFT JOIN (
                    SELECT
                        submission_key,
                        COUNT(*) FILTER (WHERE row_type = 'household')::int AS household_count,
                        COUNT(*) FILTER (WHERE row_type = 'building_only')::int AS building_count,
                        COUNT(*) FILTER (WHERE sample_flag = true)::int AS sampled_count
                    FROM clean.hh_listing_long
                    WHERE COALESCE(record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                    GROUP BY submission_key
                ) hh ON hh.submission_key = s.submission_key
                LEFT JOIN (
                    SELECT
                        submission_key,
                        COUNT(*) FILTER (WHERE issue_status <> 'resolved')::int AS open_issue_count,
                        COUNT(*)::int AS total_issue_count
                    FROM qc.issue_queue
                    WHERE instrument_code = 'listing'
                    GROUP BY submission_key
                ) iq ON iq.submission_key = s.submission_key
                ORDER BY s.interviewer_id NULLS LAST, s.submission_key
                """
            )
            case_rows = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    submission_key,
                    rule_code,
                    COUNT(*)::int AS flag_count
                FROM qc.rule_result
                WHERE instrument_code = 'listing'
                  AND submission_key IS NOT NULL
                GROUP BY submission_key, rule_code
                """
            )
            rule_rows = [dict(r) for r in cur.fetchall()]

    counts_by_submission: dict[str, dict[str, int]] = {}
    for row in rule_rows:
        submission_key = str(row.get('submission_key') or '').strip()
        rule_code = str(row.get('rule_code') or '').strip()
        flag_count = int(row.get('flag_count') or 0)
        if not submission_key or not rule_code:
            continue
        counts_by_submission.setdefault(submission_key, {})[rule_code] = flag_count

    stats_by_interviewer: dict[str, dict[str, Any]] = {}
    for row in case_rows:
        interviewer_id = str(row.get('interviewer_id') or 'Unknown')
        submission_key = str(row.get('submission_key') or '').strip()
        stat = stats_by_interviewer.setdefault(
            interviewer_id,
            {
                'interviewer_id': interviewer_id,
                'total_submissions': 0,
                'approved_count': 0,
                'rejected_count': 0,
                'pending_count': 0,
                'total_households': 0,
                'total_buildings': 0,
                'total_sampled': 0,
                'open_issues': 0,
                'total_issues': 0,
                **{rule_code.lower(): 0 for rule_code in rule_codes},
            },
        )
        stat['total_submissions'] += 1
        approval_status = str(row.get('approval_status') or '').strip().lower()
        if approval_status == 'approved':
            stat['approved_count'] += 1
        elif approval_status == 'rejected':
            stat['rejected_count'] += 1
        elif approval_status in {'pending_review', 'in_review', 'corrected', 'submitted'}:
            stat['pending_count'] += 1
        stat['total_households'] += int(row.get('household_count') or 0)
        stat['total_buildings'] += int(row.get('building_count') or 0)
        stat['total_sampled'] += int(row.get('sampled_count') or 0)
        issue_count = int(row.get('open_issue_count') or 0)
        total_issue_count = int(row.get('total_issue_count') or 0)
        rule_total_for_submission = sum(int(v or 0) for v in counts_by_submission.get(submission_key, {}).values())
        stat['open_issues'] += issue_count if issue_count > 0 else rule_total_for_submission
        stat['total_issues'] += max(total_issue_count, rule_total_for_submission)

        for rule_code, flag_count in counts_by_submission.get(submission_key, {}).items():
            key = rule_code.lower()
            if key in stat:
                stat[key] += int(flag_count or 0)

    return sorted(
        stats_by_interviewer.values(),
        key=lambda item: (-int(item.get('total_submissions') or 0), str(item.get('interviewer_id') or '')),
    )


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


def get_interviewer_productivity_by_date(settings: Settings, user: AuthUser) -> dict[str, Any]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(interviewer_id::text), ''), 'Unknown') AS interviewer_id,
                    completion_date::text AS completion_date_raw,
                    submission_date::text AS submission_date_raw,
                    record->>'end' AS end_raw,
                    record->>'start' AS start_raw
                FROM clean.hh_sampling_ea
                WHERE COALESCE(NULLIF(TRIM(interviewer_id::text), ''), '') <> ''
                """
            )
            rows = [dict(r) for r in cur.fetchall()]

    dates: set[str] = set()
    pivot: dict[str, dict[str, int]] = {}
    for row in rows:
        interviewer_id = str(row.get('interviewer_id') or 'Unknown')
        date_key = _extract_date_key_from_candidates(
            row.get('completion_date_raw'),
            row.get('submission_date_raw'),
            row.get('end_raw'),
            row.get('start_raw'),
        )
        if not date_key:
            continue
        dates.add(date_key)
        row_counts = pivot.setdefault(interviewer_id, {})
        row_counts[date_key] = int(row_counts.get(date_key, 0) or 0) + 1

    sorted_dates = sorted(dates)
    items = [
        {
            'interviewer_id': interviewer_id,
            'counts': {date_key: int(counts.get(date_key, 0) or 0) for date_key in sorted_dates},
        }
        for interviewer_id, counts in sorted(pivot.items(), key=lambda item: item[0])
    ]
    return {'dates': sorted_dates, 'items': items}


def _fetch_listing_qc_task_rows(settings: Settings, queue: str) -> list[dict[str, Any]]:
    normalized_queue = normalize_qc_productivity_queue(queue)
    if normalized_queue in {"audio", "callback"}:
        return []

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(ua.username), ''), NULLIF(TRIM(pc.assigned_to_user_id::text), ''), 'Unknown') AS username,
                    COALESCE(NULLIF(TRIM(ua.full_name), ''), '') AS full_name,
                    pc.created_at AS assigned_at,
                    pc.reviewed_at AS completed_at
                FROM qc.listing_picture_check pc
                LEFT JOIN app.user_account ua ON ua.user_id = pc.assigned_to_user_id
                WHERE pc.assigned_to_user_id IS NOT NULL
                """
            )
            return [dict(row) for row in cur.fetchall()]


def get_listing_qc_productivity(settings: Settings, user: AuthUser, queue: str = "all") -> list[dict[str, Any]]:
    return summarize_qc_task_rows(_fetch_listing_qc_task_rows(settings, queue))


def get_listing_qc_productivity_by_date(settings: Settings, user: AuthUser, queue: str = "all") -> dict[str, Any]:
    return build_qc_productivity_by_date(_fetch_listing_qc_task_rows(settings, queue))


def get_state_boundaries(settings: Settings, state_name: str | None = None) -> dict[str, Any]:
    payload = _load_state_boundary_geojson(str(settings.state_boundary_geojson_path))
    features = payload.get("features", [])
    if not state_name:
        return {"type": "FeatureCollection", "features": features}

    normalized_state = state_name.strip().lower()
    filtered_features = []
    for feature in features:
        properties = feature.get("properties") or {}
        feature_state_name = str(
            properties.get("statename")
            or properties.get("state_name")
            or properties.get("sd_STATE_NAME")
            or ""
        ).strip()
        if feature_state_name.lower() != normalized_state:
            continue
        filtered_features.append(feature)

    return {"type": "FeatureCollection", "features": filtered_features}


def _coerce_bbox(
    north: float | None,
    south: float | None,
    east: float | None,
    west: float | None,
) -> tuple[float, float, float, float] | None:
    if north is None or south is None or east is None or west is None:
        return None
    safe_north = max(float(north), float(south))
    safe_south = min(float(north), float(south))
    safe_east = max(float(east), float(west))
    safe_west = min(float(east), float(west))
    if safe_north < -90 or safe_south > 90 or safe_east < -180 or safe_west > 180:
        return None
    return safe_north, safe_south, safe_east, safe_west


def _iter_geometry_positions(value: Any) -> Iterable[tuple[float, float]]:
    if not isinstance(value, list):
        return
    if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
        yield float(value[0]), float(value[1])
        return
    for item in value:
        yield from _iter_geometry_positions(item)


def _feature_intersects_bbox(feature: dict[str, Any], bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    north, south, east, west = bbox
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    seen_position = False
    feature_west = 180.0
    feature_east = -180.0
    feature_south = 90.0
    feature_north = -90.0
    for lon, lat in _iter_geometry_positions(coordinates):
        seen_position = True
        feature_west = min(feature_west, lon)
        feature_east = max(feature_east, lon)
        feature_south = min(feature_south, lat)
        feature_north = max(feature_north, lat)
    if not seen_position:
        return False
    return not (feature_east < west or feature_west > east or feature_north < south or feature_south > north)


def get_map_features(
    settings: Settings,
    user: AuthUser,
    state_name: str | None = None,
    *,
    offset: int = 0,
    limit: int = 750,
    north: float | None = None,
    south: float | None = None,
    east: float | None = None,
    west: float | None = None,
) -> dict[str, Any]:
    """Return listing map data for the visible map viewport.

    The map endpoint supports both pagination and a geographic bounding box. The
    frontend sends the Leaflet viewport bounds after every pan/zoom so the browser
    only receives polygons and GPS points that intersect the area currently on
    screen. This prevents the Listing Overview page from mounting every Nigerian
    EA polygon and every listing GPS point at once.
    """
    safe_offset = max(int(offset or 0), 0)
    safe_limit = min(max(int(limit or 750), 1), 2000)
    bbox = _coerce_bbox(north, south, east, west)

    stats = _get_map_stats(settings, user, state_name)
    gps_points = _get_gps_points(
        settings,
        user,
        state_name,
        offset=safe_offset,
        limit=safe_limit,
        bbox=bbox,
    )
    features: list[dict[str, Any]] = []
    seen_ea_ids: set[str] = set()
    matched_feature_count = 0

    with zipfile.ZipFile(settings.boundary_zip_path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("output_geojson/") and name.endswith(".geojson")
        ]
        if not names:
            names = [name for name in archive.namelist() if name.endswith(".geojson")]

        for name in names:
            with archive.open(name) as handle:
                data = json.load(handle)
            for feature in data.get("features", []):
                properties = feature.get("properties", {})
                ea_id = str(properties.get("sd_EA_ID") or "").strip()
                feature_state_name = str(properties.get("sd_STATE_NAME") or "")
                if state_name and feature_state_name.lower() != state_name.lower():
                    continue
                if not ea_id or ea_id in seen_ea_ids:
                    continue
                if not _feature_intersects_bbox(feature, bbox):
                    continue
                seen_ea_ids.add(ea_id)

                current_index = matched_feature_count
                matched_feature_count += 1
                if current_index < safe_offset or current_index >= safe_offset + safe_limit:
                    continue

                stat = stats.get(str(ea_id), {})
                feature["properties"] = {
                    **properties,
                    "caseCount": stat.get("case_count", 0),
                    "approvedCount": stat.get("approved_count", 0),
                    "openIssueCount": stat.get("open_issue_count", 0),
                    "latestStatus": stat.get("latest_status"),
                }
                features.append(feature)

    has_more_features = safe_offset + safe_limit < matched_feature_count
    has_more_gps_points = len(gps_points) == safe_limit

    return {
        "type": "FeatureCollection",
        "features": features,
        "gpsPoints": gps_points,
        "summary": {
            "eaCount": matched_feature_count,
            "gpsPointCount": safe_offset + len(gps_points),
            "approvedEaCount": sum(1 for feature in features if feature.get("properties", {}).get("latestStatus") == "approved"),
            "issueEaCount": sum(1 for feature in features if (feature.get("properties", {}).get("openIssueCount") or 0) > 0),
        },
        "pageInfo": {
            "offset": safe_offset,
            "limit": safe_limit,
            "returnedFeatures": len(features),
            "returnedGpsPoints": len(gps_points),
            "hasMore": has_more_features or has_more_gps_points,
            "hasMoreFeatures": has_more_features,
            "hasMoreGpsPoints": has_more_gps_points,
            "viewportFiltered": bbox is not None,
        },
    }

def _get_map_stats(settings: Settings, user: AuthUser, state_name: str | None = None) -> dict[str, dict[str, Any]]:
    params: list[Any] = []
    where_parts: list[str] = []
    if state_name:
        where_parts.append("LOWER(COALESCE(s.record->>'state_name', '')) = LOWER(%s)")
        params.append(state_name)
    status_filter = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    s.ea_id,
                    COUNT(*)::int AS case_count,
                    COUNT(*) FILTER (WHERE s.approval_status = 'approved')::int AS approved_count,
                    SUM(COALESCE(i.open_issue_count, 0))::int AS open_issue_count,
                    CASE
                        WHEN COUNT(*) FILTER (WHERE s.approval_status <> 'approved') = 0
                             AND COUNT(*) FILTER (WHERE s.approval_status = 'approved') > 0 THEN 'approved'
                        WHEN COUNT(*) FILTER (WHERE s.approval_status IN ('submitted', 'pending_review', 'in_review', 'corrected')) > 0 THEN 'in_progress'
                        WHEN COUNT(*) FILTER (WHERE s.approval_status = 'rejected') > 0 THEN 'rejected'
                        ELSE NULL
                    END AS latest_status
                FROM clean.hh_sampling_ea s
                LEFT JOIN (
                    SELECT submission_key, COUNT(*) AS open_issue_count
                    FROM qc.issue_queue
                    WHERE instrument_code = 'listing' AND issue_status <> 'resolved'
                    GROUP BY submission_key
                ) i ON i.submission_key = s.submission_key
                {status_filter}
                GROUP BY s.ea_id
                """,
                params,
            )
            rows = cur.fetchall()
    return {str(row["ea_id"]): row for row in rows if row.get("ea_id")}


def _get_gps_points(
    settings: Settings,
    user: AuthUser,
    state_name: str | None = None,
    *,
    offset: int | None = None,
    limit: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where_parts: list[str] = [
        "l.gps_lat IS NOT NULL",
        "l.gps_long IS NOT NULL",
        "COALESCE(l.record->>'bld_last_another', '1') NOT IN ('0', '0.0')",
    ]
    if state_name:
        where_parts.append("LOWER(COALESCE(s.record->>'state_name', '')) = LOWER(%s)")
        params.append(state_name)
    if bbox is not None:
        north, south, east, west = bbox
        where_parts.append("l.gps_lat BETWEEN %s AND %s")
        params.extend([south, north])
        where_parts.append("l.gps_long BETWEEN %s AND %s")
        params.extend([west, east])
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    l.listing_row_id::text AS point_id,
                    l.submission_key,
                    l.ea_id,
                    l.row_type,
                    l.sample_flag,
                    l.gps_lat,
                    l.gps_long,
                    s.approval_status,
                    COALESCE(s.record->>'ea_name', s.ea_id::text, 'EA') AS ea_name,
                    s.record->>'state_name' AS state_name
                FROM clean.hh_listing_long l
                JOIN clean.hh_sampling_ea s
                    ON s.submission_key = l.submission_key
                WHERE {' AND '.join(where_parts)}
                ORDER BY l.submission_key, l.listing_row_id
                {"LIMIT %s OFFSET %s" if limit is not None else ""}
                """,
                [*params, int(limit), int(offset or 0)] if limit is not None else params,
            )
            rows = cur.fetchall()
    return rows


# ─── Picture Check ────────────────────────────────────────────────────────────

_ENSURE_PICTURE_CHECK_TABLE = """
CREATE TABLE IF NOT EXISTS qc.listing_picture_check (
    check_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_key      TEXT NOT NULL,
    ea_id               TEXT,
    ea_name             TEXT,
    state_name          TEXT,
    building_only_pct   NUMERIC,
    building_only_count INT,
    total_rows          INT,
    status              TEXT NOT NULL DEFAULT 'pending',
    assigned_to_user_id UUID,
    reviewer_note       TEXT,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _ensure_picture_check_table(cur: Any) -> None:
    cur.execute(_ENSURE_PICTURE_CHECK_TABLE)


def get_picture_check_flagged_eas(
    settings: Settings,
    user: AuthUser,
    show_history: bool = False,
    filter_status: str | None = None,
    filter_date_from: str | None = None,
    filter_date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return EAs where building_only rows are ≥ 40% of total listing rows.

    Admin / data_engineer / qc_reviewer / client: all flagged EAs (active only by default), with latest check status.
        show_history=True (admin only): return all, with optional status/date filters.
    """
    role_code = (user.role or "").strip().lower()
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_picture_check_table(cur)
            conn.commit()

            if role_code in {"admin", "data_engineer", "qc_reviewer", "client"}:
                # Build WHERE clause additions for history/filter mode
                history_filter = ""
                params: list[Any] = []

                if role_code == "admin" and show_history:
                    # Show all; optionally filter by status and/or date range on the check record
                    conditions = []
                    if filter_status:
                        conditions.append("pc.status = %s")
                        params.append(filter_status)
                    if filter_date_from:
                        conditions.append("pc.created_at >= %s::timestamptz")
                        params.append(filter_date_from)
                    if filter_date_to:
                        conditions.append("pc.created_at <= %s::timestamptz")
                        params.append(filter_date_to)
                    if conditions:
                        history_filter = "AND (" + " AND ".join(conditions) + ")"
                else:
                    # Active-only: no check yet, or check is still pending
                    history_filter = "AND (pc.status IS NULL OR pc.status NOT IN ('approved', 'rejected'))"

                cur.execute(
                    f"""
                    SELECT
                        flagged.submission_key,
                        flagged.ea_id,
                        flagged.ea_name,
                        flagged.state_name,
                        flagged.building_only_count,
                        flagged.household_count,
                        flagged.total_rows,
                        flagged.building_only_pct,
                        pc.check_id::text              AS check_id,
                        pc.status                      AS check_status,
                        pc.assigned_to_user_id::text   AS assigned_to_user_id,
                        ua.full_name                   AS assigned_to_username
                    FROM (
                        SELECT
                            s.ea_id::text                    AS ea_id,
                            MAX(l.submission_key)            AS submission_key,
                            MIN(s.record->>'ea_name')        AS ea_name,
                            MIN(s.record->>'state_name')     AS state_name,
                            COUNT(*) FILTER (WHERE l.row_type = 'building_only')::int  AS building_only_count,
                            COUNT(*) FILTER (WHERE l.row_type = 'household')::int      AS household_count,
                            COUNT(*)::int                                               AS total_rows,
                            ROUND(
                                COUNT(*) FILTER (WHERE l.row_type = 'building_only')::numeric
                                / NULLIF(COUNT(*), 0) * 100, 1
                            )                           AS building_only_pct
                        FROM clean.hh_listing_long l
                        LEFT JOIN clean.hh_sampling_ea s ON s.submission_key = l.submission_key
                        WHERE COALESCE(l.record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                          AND s.ea_id IS NOT NULL
                        GROUP BY s.ea_id
                        HAVING COUNT(*) FILTER (WHERE l.row_type = 'building_only')::numeric
                               / NULLIF(COUNT(*), 0) >= 0.4
                    ) flagged
                    LEFT JOIN LATERAL (
                        SELECT check_id, status, assigned_to_user_id, created_at
                        FROM qc.listing_picture_check
                        WHERE ea_id = flagged.ea_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) pc ON true
                    LEFT JOIN app.user_account ua ON ua.user_id = pc.assigned_to_user_id
                    WHERE 1=1 {history_filter}
                    ORDER BY flagged.building_only_pct DESC
                    """,
                    params,
                )
            else:
                # QC reviewer — only their own assigned pending checks (role-isolated)
                cur.execute(
                    """
                    SELECT
                        pc.submission_key,
                        pc.ea_id,
                        pc.ea_name,
                        pc.state_name,
                        pc.building_only_count,
                        (pc.total_rows - pc.building_only_count) AS household_count,
                        pc.total_rows,
                        pc.building_only_pct,
                        pc.check_id::text        AS check_id,
                        pc.status               AS check_status,
                        pc.assigned_to_user_id::text AS assigned_to_user_id,
                        ua.full_name            AS assigned_to_username
                    FROM qc.listing_picture_check pc
                    LEFT JOIN app.user_account ua ON ua.user_id = pc.assigned_to_user_id
                    WHERE pc.assigned_to_user_id = %s::uuid
                      AND pc.status = 'pending'
                    ORDER BY pc.created_at DESC
                    """,
                    (str(user.id),),
                )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def bulk_assign_picture_check(
    settings: Settings,
    user: AuthUser,
    submission_keys: list[str],
    assigned_to_user_id: str,
) -> dict[str, Any]:
    """Admin-only: create qc.listing_picture_check rows for given submissions."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admins may assign picture checks.")

    created = 0
    skipped = 0

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_picture_check_table(cur)

            for sk in submission_keys:
                # Fetch EA metadata + ratio from listing data
                cur.execute(
                    """
                    SELECT
                        s.ea_id::text               AS ea_id,
                        s.record->>'ea_name'        AS ea_name,
                        s.record->>'state_name'     AS state_name,
                        COUNT(*) FILTER (WHERE l.row_type = 'building_only')::int  AS building_only_count,
                        COUNT(*)::int                                               AS total_rows,
                        ROUND(
                            COUNT(*) FILTER (WHERE l.row_type = 'building_only')::numeric
                            / NULLIF(COUNT(*), 0) * 100, 1
                        )                           AS building_only_pct
                    FROM clean.hh_sampling_ea s
                    JOIN clean.hh_listing_long l ON l.submission_key = s.submission_key
                    WHERE s.submission_key = %s
                      AND COALESCE(l.record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                    GROUP BY s.ea_id, s.record->>'ea_name', s.record->>'state_name'
                    """,
                    (sk,),
                )
                meta = cur.fetchone()
                if not meta:
                    skipped += 1
                    continue

                # Skip if already assigned to this user
                cur.execute(
                    "SELECT 1 FROM qc.listing_picture_check WHERE submission_key = %s AND assigned_to_user_id = %s::uuid",
                    (sk, assigned_to_user_id),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

                cur.execute(
                    """
                    INSERT INTO qc.listing_picture_check
                        (submission_key, ea_id, ea_name, state_name, building_only_pct,
                         building_only_count, total_rows, assigned_to_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid)
                    """,
                    (
                        sk,
                        meta["ea_id"],
                        meta["ea_name"],
                        meta["state_name"],
                        meta["building_only_pct"],
                        meta["building_only_count"],
                        meta["total_rows"],
                        assigned_to_user_id,
                    ),
                )
                created += 1

        conn.commit()

    return {"created": created, "skipped": skipped}


def get_picture_check_detail(settings: Settings, user: AuthUser, submission_key: str) -> dict[str, Any]:
    """Return the check record + all building_only listing rows for a submission."""
    role_code = (user.role or "").strip().lower()

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_picture_check_table(cur)
            conn.commit()

            cur.execute(
                "SELECT * FROM qc.listing_picture_check WHERE submission_key = %s ORDER BY created_at DESC LIMIT 1",
                (submission_key,),
            )
            check_row = cur.fetchone()

            if check_row:
                # Reviewers can browse flagged photos; decisions remain restricted separately.
                if role_code not in {"admin", "data_engineer", "qc_reviewer", "client"}:
                    assigned_id = str(check_row.get("assigned_to_user_id") or "")
                    if assigned_id != str(user.id):
                        raise HTTPException(status_code=403, detail="Not assigned to you.")
                check_record = dict(check_row)
                check_record["check_id"] = str(check_record.get("check_id") or "")
                check_record["assigned_to_user_id"] = str(check_record.get("assigned_to_user_id") or "")
            else:
                check_record = None

            # Always fetch EA metadata directly from source data
            cur.execute(
                """
                SELECT
                    s.ea_id::text                AS ea_id,
                    s.record->>'ea_name'         AS ea_name,
                    s.record->>'state_name'      AS state_name,
                    ROUND(
                        COUNT(*) FILTER (WHERE l.row_type = 'building_only')::numeric
                        / NULLIF(COUNT(*), 0) * 100, 1
                    )                            AS building_only_pct
                FROM clean.hh_sampling_ea s
                JOIN clean.hh_listing_long l ON l.submission_key = s.submission_key
                WHERE s.submission_key = %s
                GROUP BY s.ea_id, s.record->>'ea_name', s.record->>'state_name'
                LIMIT 1
                """,
                (submission_key,),
            )
            ea_row = cur.fetchone()
            ea_info = dict(ea_row) if ea_row else None

            cur.execute(
                """
                SELECT
                    l.listing_row_id::text  AS listing_row_id,
                    l.building_no,
                    l.record->>'building_photo' AS photo_ref,
                    l.submission_key,
                    l.gps_lat,
                    l.gps_long
                FROM clean.hh_listing_long l
                WHERE l.submission_key = %s
                  AND l.row_type = 'building_only'
                  AND COALESCE(l.record->>'bld_last_another', '1') NOT IN ('0', '0.0')
                ORDER BY l.building_no NULLS LAST, l.listing_row_id
                """,
                (submission_key,),
            )
            raw_photos = [dict(r) for r in cur.fetchall()]

    # Build SurveyCTO v2 per-submission attachment URLs
    import re as _re
    def _extract_filename(photo_ref: str | None) -> str | None:
        if not photo_ref:
            return None
        text = photo_ref.strip()
        # Strip leading "File skipped from exports:" prefix (case-insensitive)
        text = _re.sub(r'(?i)^file skipped from exports:\s*', '', text)
        # Strip optional "media\\" or "media/" directory prefix
        text = _re.sub(r'(?i)^media[/\\]', '', text)
        # Take only the bare filename (last path component)
        text = text.split('/')[-1].split('\\')[-1].strip()
        # Must look like a filename with an extension
        if '.' in text and _re.search(r'\.[a-zA-Z0-9]+$', text):
            return text
        return None

    photos: list[dict] = []
    for row in raw_photos:
        photo_ref = row.get("photo_ref")
        row_submission_key = row.get("submission_key") or submission_key
        filename = _extract_filename(photo_ref)
        if filename:
            # Build v2 per-submission attachment URL
            photo_url = f"/api/listing/media/{filename}"
            row["photo_url"] = photo_url
        else:
            row["photo_url"] = None
        photos.append(row)

    return {"check": check_record, "ea_info": ea_info, "photos": photos}


def submit_picture_check_decision(
    settings: Settings,
    user: AuthUser,
    check_id: str,
    status: str,
    reviewer_note: str | None = None,
) -> dict[str, Any]:
    """Approve or reject a picture check. Only the assigned reviewer or admin may submit."""
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be 'approved' or 'rejected'.")

    role_code = (user.role or "").strip().lower()

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            _ensure_picture_check_table(cur)

            cur.execute(
                "SELECT check_id, assigned_to_user_id FROM qc.listing_picture_check WHERE check_id = %s::uuid",
                (check_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Picture check not found.")

            if role_code not in {"admin", "data_engineer", "client"}:
                assigned_id = str(row.get("assigned_to_user_id") or "")
                if assigned_id != str(user.id):
                    raise HTTPException(status_code=403, detail="Not assigned to you.")

            cur.execute(
                """
                UPDATE qc.listing_picture_check
                SET status = %s,
                    reviewer_note = %s,
                    reviewed_at = now()
                WHERE check_id = %s::uuid
                """,
                (status, reviewer_note, check_id),
            )
        conn.commit()

    return {"updated": True, "check_id": check_id, "status": status}


def get_main_survey_scaffold() -> dict[str, Any]:
    return {
        "status": "dictionary-backed",
        "message": "Main Survey pages are workbook-backed and the SurveyCTO loader now writes clean.main_case plus clean.main_case_section.",
        "targetModel": "case_table_plus_section_tables",
    }


# ─── Listing Analysis ────────────────────────────────────────────────────────

_LISTING_ANALYSIS_FIELDS: list[tuple[str, str, str]] = [
    ("no_of_male_less_15yrs",   "Number of Males Under 15 Years",            "numeric"),
    ("no_of_female_less_15yrs", "Number of Females Under 15 Years",          "numeric"),
    ("total_less_15yrs",        "Total Household Members Under 15 Years",    "numeric"),
    ("no_of_male_15_17yrs",     "Number of Males 15\u201317 Years",          "numeric"),
    ("no_of_female_15_17yrs",   "Number of Females 15\u201317 Years",        "numeric"),
    ("total_15_17yrs",          "Total Household Members 15\u201317 Years",  "numeric"),
    ("no_of_male_18yrs_plus",   "Number of Males 18 Years & Above",          "numeric"),
    ("no_of_female_18yrs_plus", "Number of Females 18 Years & Above",        "numeric"),
    ("total_18yrs_plus",        "Total Household Members 18 Years & Above",  "numeric"),
    ("total_male",              "Total Male Household Members",               "numeric"),
    ("total_female",            "Total Female Household Members",             "numeric"),
    ("total_household_size",    "Total Household Size",                      "numeric"),
    ("hh_ent",                  "Household Enterprise Ownership",             "categorical"),
]


def get_listing_analysis_filter_options(
    settings: Settings,
    state: str | None = None,
) -> dict[str, Any]:
    """Return available states and state-scoped EAs for the Listing Analysis filter bar.

    The first page load only needs the state list. Returning every EA in the country made
    the analysis page do unnecessary work before the user selected a state.
    """
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT
                    COALESCE(NULLIF(TRIM(record->>'state_name'), ''), 'Unknown') AS state_name
                FROM clean.hh_sampling_ea
                WHERE approval_status != 'rejected'
                ORDER BY COALESCE(NULLIF(TRIM(record->>'state_name'), ''), 'Unknown')
            """)
            states: list[str] = [str(r["state_name"]) for r in cur.fetchall()]

            eas: list[dict[str, str]] = []
            if state:
                cur.execute(
                    """
                    SELECT DISTINCT
                        l.ea_id,
                        COALESCE(NULLIF(TRIM(s.record->>'ea_name'), ''), l.ea_id) AS ea_name
                    FROM clean.hh_listing_long l
                    JOIN clean.hh_sampling_ea s ON s.submission_key = l.submission_key
                    WHERE l.row_type = 'household'
                      AND s.approval_status != 'rejected'
                      AND l.ea_id IS NOT NULL
                      AND LOWER(COALESCE(s.record->>'state_name', '')) = LOWER(%s)
                    ORDER BY COALESCE(NULLIF(TRIM(s.record->>'ea_name'), ''), l.ea_id)
                    """,
                    [state],
                )
                eas = [{"ea_id": str(r["ea_id"]), "ea_name": str(r["ea_name"])} for r in cur.fetchall()]

    return {"states": states, "eas": eas}


def get_listing_analysis(
    settings: Settings,
    user: AuthUser,
    state: list[str] | str | None = None,
    ea_id: list[str] | str | None = None,
) -> dict[str, Any]:
    """Aggregate distribution tables for each listing demographic field.

    This intentionally scans the filtered household rows once and aggregates in Python.
    The previous implementation queried the same joined tables repeatedly for every field
    and fetched full numeric columns several times, which made /listing/analysis hang or
    get cancelled on larger datasets.
    """
    states = [state] if isinstance(state, str) else (state or [])
    ea_ids = [ea_id] if isinstance(ea_id, str) else (ea_id or [])

    base_where = ["l.row_type = 'household'", "s.approval_status != 'rejected'"]
    base_params: list[Any] = []
    if states:
        placeholders = ", ".join(["%s"] * len(states))
        base_where.append(f"LOWER(COALESCE(s.record->>'state_name', '')) IN ({placeholders})")
        base_params.extend(s.lower() for s in states)
    if ea_ids:
        placeholders = ", ".join(["%s"] * len(ea_ids))
        base_where.append(f"l.ea_id IN ({placeholders})")
        base_params.extend(ea_ids)
    where_sql = " AND ".join(base_where)

    fields = [field_key for field_key, _field_label, _field_type in _LISTING_ANALYSIS_FIELDS]
    select_fields = ",\n                    ".join(
        f"l.record->>'{field_key}' AS {field_key}" for field_key in fields
    )

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    {select_fields}
                FROM clean.hh_listing_long l
                JOIN clean.hh_sampling_ea s ON s.submission_key = l.submission_key
                WHERE {where_sql}
                """,
                base_params,
            )
            household_rows = [dict(r) for r in cur.fetchall()]

    total_hh = len(household_rows)
    cards: list[dict[str, Any]] = []

    def _clean_numeric_label(val: str) -> str:
        try:
            f = float(val)
            return str(int(f)) if f == int(f) else str(f)
        except (TypeError, ValueError, OverflowError):
            return str(val)

    def _numeric_sort_key(label: str) -> tuple[int, float, str]:
        if label == "Unknown/Missing":
            return (1, 0.0, label)
        try:
            return (0, float(label), label)
        except (TypeError, ValueError, OverflowError):
            return (0, float("inf"), str(label))

    def _to_float(val: Any) -> float | None:
        if val is None:
            return None
        text = str(val).strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if math.isnan(parsed) or math.isinf(parsed):
            return None
        return parsed

    for field_key, field_label, field_type in _LISTING_ANALYSIS_FIELDS:
        stats: dict[str, Any] | None = None
        bucket_counts: Counter[str] = Counter()
        numeric_values: list[float] = []

        for row in household_rows:
            raw_val = row.get(field_key)
            text_val = str(raw_val).strip() if raw_val is not None else ""

            if field_type == "numeric":
                if text_val:
                    numeric_value = _to_float(text_val)
                    if numeric_value is not None:
                        numeric_values.append(numeric_value)
                        bucket_counts[_clean_numeric_label(text_val)] += 1
                    else:
                        bucket_counts[text_val] += 1
                else:
                    bucket_counts["Unknown/Missing"] += 1
            else:
                bucket_counts[text_val if text_val else "Not Provided"] += 1

        if field_type == "numeric" and numeric_values:
            mode_value = Counter(numeric_values).most_common(1)[0][0]
            stats = {
                "mean": round(sum(numeric_values) / len(numeric_values), 2),
                "median": round(float(median(numeric_values)), 2),
                "mode": round(float(mode_value), 2),
            }

        if field_type == "numeric":
            ordered_items = sorted(bucket_counts.items(), key=lambda item: _numeric_sort_key(item[0]))
        else:
            ordered_items = sorted(bucket_counts.items(), key=lambda item: (-item[1], item[0]))

        response_count = sum(bucket_counts.values())
        table_rows: list[dict[str, Any]] = []
        for bucket, count in ordered_items:
            label = bucket
            if field_key == "hh_ent":
                label = {
                    "1": "Yes",
                    "1.0": "Yes",
                    "2": "No",
                    "2.0": "No",
                    "3": "Don't know",
                    "3.0": "Don't know",
                }.get(bucket, bucket)
            table_rows.append(
                {
                    "code": bucket,
                    "label": label,
                    "count": int(count),
                    "percent": round(int(count) / response_count * 100, 1) if response_count else 0.0,
                }
            )

        yes_no_labels = {str(row.get("label") or "").strip().lower() for row in table_rows}
        if yes_no_labels and yes_no_labels.issubset({"yes", "no"}):
            existing = {str(row.get("label") or "").strip().lower(): row for row in table_rows}
            table_rows = []
            for label in ("Yes", "No"):
                existing_row = existing.get(label.lower())
                count = int(existing_row.get("count") or 0) if existing_row else 0
                table_rows.append(
                    {
                        "code": str(existing_row.get("code")) if existing_row else label,
                        "label": label,
                        "count": count,
                        "percent": round(count / response_count * 100, 1) if response_count else 0.0,
                    }
                )

        cards.append(
            {
                "variable": field_key,
                "label": field_label,
                "responseCount": response_count,
                "tableRows": table_rows,
            }
            | ({"stats": stats} if stats else {})
        )

    return {"totalHouseholds": total_hh, "cards": cards}

def get_listing_answer_breakdown(
    settings: Settings,
    user: AuthUser,
    variable: str,
    code: str,
    state: str | None = None,
    ea_id: str | None = None,
) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_]+", variable):
        raise HTTPException(status_code=400, detail="Invalid variable.")

    # Match exact code OR numeric-equivalent (e.g. "0" matches "0.0" stored in JSONB)
    where_parts = [
        "l.row_type = 'household'",
        f"(l.record->>'{variable}' = %s"
        f" OR (l.record->>'{variable}' ~ '^-?[0-9]+(\\.[0-9]+)?$'"
        f"     AND %s ~ '^-?[0-9]+(\\.[0-9]+)?$'"
        f"     AND CAST(l.record->>'{variable}' AS numeric) = CAST(%s AS numeric)))",
    ]
    params: list[Any] = [code, code, code]
    if state:
        where_parts.append("LOWER(COALESCE(s.record->>'state_name','')) = LOWER(%s)")
        params.append(state)
    if ea_id:
        where_parts.append("l.ea_id = %s")
        params.append(ea_id)
    where_sql = " AND ".join(where_parts)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (s.submission_key)
                    s.submission_key,
                    COALESCE(NULLIF(TRIM(g.state_name), ''), NULLIF(TRIM(s.record->>'state_name'), ''), 'Unknown') AS state_name,
                    COALESCE(NULLIF(TRIM(s.record->>'ea_name'),''), s.ea_id, 'Unknown') AS ea_name,
                    COALESCE(s.completion_date::text, s.submission_date::text) AS submitted_at,
                    s.interviewer_id
                FROM clean.hh_listing_long l
                JOIN clean.hh_sampling_ea s ON s.submission_key = l.submission_key
                LEFT JOIN reference.geo_boundaries_ea g ON g.ea_id = REGEXP_REPLACE(s.ea_id::text, '\\.0+$', '')
                WHERE {where_sql}
                ORDER BY s.submission_key
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]
