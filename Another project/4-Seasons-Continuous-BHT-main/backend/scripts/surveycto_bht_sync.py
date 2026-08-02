#!/usr/bin/env python3
"""Fetch BHT SurveyCTO data and build a staged DuckDB replacement.

The Node server owns promotion of the generated DuckDB file so it can close its
active connection before swapping files.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from xlsform_metadata import (
    build_answer_text,
    build_question_text,
    get_xlsform_question_metadata,
    load_xlsform_metadata,
    map_demographic_value,
)


class SyncFailure(Exception):
    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


PANEL_MAP = {
    "Panel_1": "Noodles",
    "Panel_2": "Toothpaste",
    "Panel_3": "Edible Oil",
    "Panel_4": "Bleach",
    "Panel_5": "Toilet Cleaner",
    "Panel_6": "Snacks Products",
    "Panel_7": "Breakfast Cereal",
    "Panel_8": "Condiment Mixes",
    "Panel_9": "Wet Hair Care",
    "Panel_10": "Dry Hair Care",
    "Panel_11": "Malt Beverage",
}
CATEGORY_TO_PANEL = {v: k for k, v in PANEL_MAP.items()}
DROP_PATTERNS = ["_gf_"]
RESPONDENT_DIM_FIELD_SOURCES = {
    "Region": ["Region", "City_1"],
    "D3": ["D3", "D3_Q"],
    "D5": ["D5", "D5_Q", "Working_Status", "Employment_Status"],
    "B1": ["B1", "Marital_Status"],
    "Gender": ["Gender"],
    "Age": ["Age", "AGE_GROUP", "AGE_TRACKER", "Age_cal"],
    "SEC": ["SEC"],
    "Week": ["Week"],
    # Preserve the unlabelled/raw values needed to reproduce SPSS recodes and
    # filters without changing the dashboard-facing demographic labels above.
    "SPSS_Region": ["Region", "City_1"],
    "SPSS_D3": ["D3", "D3_Q"],
    "SPSS_Gender": ["Gender"],
    "SPSS_Age_cal": ["Age_cal", "Age", "AGE_GROUP", "AGE_TRACKER"],
    "SPSS_SEC": ["SEC"],
    "SPSS_Week": ["Week"],
}
RESPONDENT_DIM_FIELDS = list(RESPONDENT_DIM_FIELD_SOURCES.keys())
SPSS_RAW_DIM_FIELDS = {field for field in RESPONDENT_DIM_FIELDS if field.startswith("SPSS_")}
SPSS_RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "spss_export_rules.json"
DEFAULT_ID_CANDIDATES = ["KEY"]
SPSS_CITY_CODE_LABELS = {
    "1": "Lagos",
    "2": "Ibadan",
    "3": "Abuja",
    "4": "Kano",
    "5": "Kaduna",
    "6": "PHC",
    "7": "Benin",
    "8": "Onitsha",
    "9": "Enugu",
    "10": "Owerri",
    "11": "Jos",
    "12": "Uyo",
    "13": "Ilorin",
    "14": "Sokoto",
    "15": "Warri",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_col(value: str) -> str:
    out = str(value).strip().lower()
    out = re.sub(r"[^a-z0-9]+", "_", out)
    return re.sub(r"_+", "_", out).strip("_")


def sanitize_category(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_ -]+", "", str(value)).strip() or "Unknown"
    return safe.replace(" ", "_")


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, pd.Timestamp)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return str(value)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=json_default), encoding="utf-8")
    tmp.replace(path)


def parse_datetime_series(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    if parsed.notna().any():
        return parsed
    # SurveyCTO exports often use strings like "Apr 9, 2026 12:22:15 AM".
    return pd.to_datetime(series, format="%b %d, %Y %I:%M:%S %p", errors="coerce", utc=True)


def datetime_to_epoch_seconds(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def should_drop_column(name: str) -> bool:
    lower = str(name).lower()
    return any(pattern.lower() in lower for pattern in DROP_PATTERNS)


def drop_unnecessary_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in df.columns if should_drop_column(c)], errors="ignore")


def resolve_default_datamap_path(backend_root: Path) -> Path:
    candidates = [
        backend_root / "data" / "Datamap.xlsx",
        backend_root.parent / "Datamap.xlsx",
        backend_root.parent.parent / "Datamap.xlsx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def load_datamap(datamap_path: Path | None) -> dict[str, Any]:
    if datamap_path is not None:
        datamap_path = Path(datamap_path)
    if not datamap_path or not datamap_path.exists():
        return {"new_to_old": {}, "duplicate_new_names": {}, "multiselect_parent_maps": {}}

    mapping_df = pd.read_excel(datamap_path)
    if mapping_df.shape[1] < 2:
        raise ValueError(f"Datamap workbook must have at least two columns: {datamap_path}")

    old_col, new_col = mapping_df.columns[:2]
    mapping_df = mapping_df[[old_col, new_col]].dropna(how="any").copy()
    mapping_df[old_col] = mapping_df[old_col].astype(str).str.strip()
    mapping_df[new_col] = mapping_df[new_col].astype(str).str.strip()
    mapping_df = mapping_df.loc[mapping_df[old_col].ne("") & mapping_df[new_col].ne("")]

    new_to_old: dict[str, str] = {}
    duplicate_new_names: dict[str, list[str]] = {}
    multiselect_parent_maps: dict[str, list[dict[str, str]]] = {}
    for new_name, group in mapping_df.groupby(new_col, sort=False):
        old_names = list(dict.fromkeys(group[old_col].astype(str).tolist()))
        if not old_names:
            continue
        new_to_old[str(new_name)] = old_names[0]
        if len(old_names) > 1:
            duplicate_new_names[str(new_name)] = old_names
        option_match = re.match(r"^(.+)_([0-9]+)$", str(new_name).strip(), flags=re.IGNORECASE)
        if option_match:
            parent_name, option_code = option_match.groups()
            multiselect_parent_maps.setdefault(parent_name, []).append(
                {"old_name": old_names[0], "option_code": option_code}
            )

    return {
        "new_to_old": new_to_old,
        "duplicate_new_names": duplicate_new_names,
        "multiselect_parent_maps": multiselect_parent_maps,
        "path": str(datamap_path),
    }


def expand_datamap_multiselect_parent_columns(
    df: pd.DataFrame,
    datamap: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, int]]:
    parent_maps: dict[str, list[dict[str, str]]] = datamap.get("multiselect_parent_maps") or {}
    if df.empty or not parent_maps:
        return df, {"expandedMultiselectParents": 0, "expandedMultiselectColumns": 0}

    columns_by_key = {norm_col(column): column for column in df.columns}
    expanded_parents = 0
    expanded_columns = 0
    new_columns: dict[str, pd.Series] = {}

    for parent_name, entries in parent_maps.items():
        source_column = columns_by_key.get(norm_col(parent_name))
        if not source_column:
            continue
        source = df[source_column]
        answered = source.notna() & source.astype("string").str.strip().fillna("").ne("")
        if not answered.any():
            continue

        token_sets = source.astype("string").str.findall(r"\d+").apply(
            lambda values: set(values) if isinstance(values, list) else set()
        )
        parent_created = False
        for entry in entries:
            target = entry.get("old_name")
            option_code = str(entry.get("option_code") or "").strip()
            if not target or not option_code:
                continue
            selected = token_sets.apply(lambda tokens: option_code in tokens)
            expanded = pd.Series(np.nan, index=df.index, dtype="float")
            expanded.loc[answered] = selected.loc[answered].astype(int)
            if target in df.columns:
                df[target] = df[target].combine_first(expanded)
            else:
                new_columns[target] = expanded
            expanded_columns += 1
            parent_created = True
        if parent_created:
            expanded_parents += 1

    if new_columns:
        df = pd.concat([df, pd.DataFrame(new_columns, index=df.index)], axis=1)

    return (
        df,
        {
            "expandedMultiselectParents": expanded_parents,
            "expandedMultiselectColumns": expanded_columns,
        },
    )


def apply_datamap_columns(df: pd.DataFrame, datamap: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    new_to_old: dict[str, str] = datamap.get("new_to_old") or {}
    if df.empty or not new_to_old:
        out = df
        out, parent_expansion_stats = expand_datamap_multiselect_parent_columns(out, datamap)
        return out, {
            "renamedColumns": 0,
            "coalescedColumns": 0,
            "duplicateNewNamesUsed": 0,
            "duplicateNewNamesTotal": 0,
            **parent_expansion_stats,
        }

    out = df
    lower_lookup = {str(name).lower(): str(name) for name in new_to_old}
    rename_pairs: list[tuple[str, str]] = []
    duplicate_new_names = datamap.get("duplicate_new_names") or {}

    for column in list(out.columns):
        column_text = str(column).strip()
        target = new_to_old.get(column_text)
        if not target:
            exact_new_name = lower_lookup.get(column_text.lower())
            target = new_to_old.get(exact_new_name) if exact_new_name else None
        if target and target != column:
            rename_pairs.append((column, target))

    renamed = 0
    coalesced = 0
    duplicate_used = 0
    for source, target in rename_pairs:
        if source not in out.columns:
            continue
        if str(source) in duplicate_new_names:
            duplicate_used += 1
        if target in out.columns:
            out[target] = out[target].combine_first(out[source])
            out = out.drop(columns=[source])
            coalesced += 1
        else:
            out = out.rename(columns={source: target})
            renamed += 1

    out, parent_expansion_stats = expand_datamap_multiselect_parent_columns(out, datamap)

    return out, {
        "renamedColumns": renamed,
        "coalescedColumns": coalesced,
        "duplicateNewNamesUsed": duplicate_used,
        "duplicateNewNamesTotal": len(duplicate_new_names),
        **parent_expansion_stats,
    }


def clean_minus_one(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return series.mask(series.isin([-1, -1.0]), np.nan)
    text = series.astype("string")
    return text.mask(text.isin(["-1", "-1.0"]), pd.NA)


def remove_empty_strings(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return series
    text = series.astype("string")
    return text.mask(text.str.strip().eq(""))


def resolve_column_name(columns: list[str], candidates: list[str]) -> str | None:
    column_map = {str(col).lower(): str(col) for col in columns}
    for candidate in candidates:
        hit = column_map.get(str(candidate).lower())
        if hit:
            return hit
    return None


def build_respondent_dim_projection(columns: list[str], table_alias: str = "") -> list[str]:
    prefix = f"{table_alias}." if table_alias else ""
    projection: list[str] = []
    for field, candidates in RESPONDENT_DIM_FIELD_SOURCES.items():
        source = resolve_column_name(columns, candidates)
        if source:
            projection.append(f'CAST({prefix}"{source}" AS VARCHAR) AS "{field}"')
        else:
            projection.append(f'CAST(NULL AS VARCHAR) AS "{field}"')
    return projection


def build_respondent_dim_frame(
    df: pd.DataFrame,
    id_column: str,
    metadata: dict[str, Any],
) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out[id_column] = df[id_column].astype("string")

    for field, candidates in RESPONDENT_DIM_FIELD_SOURCES.items():
        source = resolve_column_name(list(df.columns), candidates)
        if not source:
            out[field] = pd.Series(pd.NA, index=df.index, dtype="string")
            continue
        values = remove_empty_strings(clean_minus_one(df[source]))
        if field in SPSS_RAW_DIM_FIELDS:
            out[field] = values.astype("string")
        else:
            out[field] = values.map(
                lambda value, target_field=field: map_demographic_value(target_field, value, metadata) if pd.notna(value) else pd.NA
            ).astype("string")

    return out


def is_selected_panel(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").eq(1)


def is_binary_01(series: pd.Series) -> bool:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return False
    return set(pd.unique(values)).issubset({0, 1})


def map_metadata_variables_to_old_format(
    variables: set[str] | list[str],
    datamap: dict[str, Any] | None,
) -> set[str]:
    if not datamap:
        return {str(value).strip() for value in variables if str(value).strip()}

    new_to_old = datamap.get("new_to_old") or {}
    duplicate_new_names = datamap.get("duplicate_new_names") or {}
    parent_maps = datamap.get("multiselect_parent_maps") or {}
    canonical_by_lower = {
        str(name).strip().lower(): str(name).strip()
        for name in set(new_to_old) | set(duplicate_new_names) | set(parent_maps)
        if str(name).strip()
    }
    mapped: set[str] = set()
    for raw_value in variables:
        value = str(raw_value).strip()
        if not value:
            continue
        canonical = canonical_by_lower.get(value.lower(), value)
        targets = duplicate_new_names.get(canonical) or []
        if not targets and canonical in new_to_old:
            targets = [new_to_old[canonical]]
        parent_targets = [
            entry.get("old_name")
            for entry in parent_maps.get(canonical, [])
            if entry.get("old_name")
        ]
        resolved = [*targets, *parent_targets]
        if resolved:
            mapped.update(str(target).strip() for target in resolved if str(target).strip())
        else:
            mapped.add(value)
    return mapped


def load_meta_rules(metadata_path: Path, datamap: dict[str, Any] | None = None) -> dict[str, Any]:
    if not metadata_path.exists():
        raise FileNotFoundError(f"BHT metadata workbook not found: {metadata_path}")

    meta = pd.read_excel(metadata_path)
    meta.columns = [norm_col(c) for c in meta.columns]

    var_candidates = [c for c in meta.columns if c in {"variable", "var", "varname", "variable_name", "question", "qname"}]
    if not var_candidates:
        var_candidates = [c for c in meta.columns if "var" in c or "question" in c]
    if not var_candidates:
        raise KeyError(f"Could not detect variable column in {metadata_path.name}. Columns: {meta.columns.tolist()}")

    rule_candidates = [c for c in meta.columns if c in {"rule", "rules"}]
    if not rule_candidates:
        rule_candidates = [c for c in meta.columns if "rule" in c]
    if not rule_candidates:
        raise KeyError(f"Could not detect rule column in {metadata_path.name}. Columns: {meta.columns.tolist()}")

    var_col = var_candidates[0]
    rule_col = rule_candidates[0]
    meta[var_col] = meta[var_col].astype(str).str.strip()
    meta[rule_col] = meta[rule_col].astype(str).str.strip()
    meta["__rule_norm"] = meta[rule_col].str.strip().str.lower()

    remove_vars = map_metadata_variables_to_old_format(
        set(meta.loc[meta["__rule_norm"].eq("remove"), var_col]),
        datamap,
    )
    demo_vars = map_metadata_variables_to_old_format(
        set(meta.loc[meta["__rule_norm"].eq("demo"), var_col]),
        datamap,
    )
    general_vars = map_metadata_variables_to_old_format(
        set(meta.loc[meta["__rule_norm"].eq("general"), var_col]),
        datamap,
    )
    panel_vars = map_metadata_variables_to_old_format(
        set(meta.loc[meta["__rule_norm"].eq("panel"), var_col]),
        datamap,
    ) | set(PANEL_MAP.keys())
    identifier_vars = [value for value in meta.loc[meta["__rule_norm"].eq("identifier"), var_col].astype(str).tolist() if value]

    category_names = set(PANEL_MAP.values())
    category_q = {cat: set() for cat in category_names}
    for _, row in meta.iterrows():
        variable = row[var_col]
        if variable in remove_vars:
            continue
        rule_norm = str(row["__rule_norm"]).strip()
        for category in category_names:
            if rule_norm == category.lower():
                category_q[category].update(
                    map_metadata_variables_to_old_format({variable}, datamap)
                )
                break

    return {
        "remove_vars": remove_vars,
        "demo_vars": demo_vars,
        "general_vars": general_vars,
        "panel_vars": panel_vars,
        "category_names": category_names,
        "category_q": category_q,
        "identifier_vars": identifier_vars,
    }


def supplement_rules_from_dashboard_audit(
    rules: dict[str, Any],
    header_audit_path: Path,
    datamap: dict[str, Any],
) -> dict[str, Any]:
    audit = read_json(header_audit_path, {})
    audit_categories = audit.get("categories") if isinstance(audit, dict) else {}
    if not isinstance(audit_categories, dict) or not audit_categories:
        raise ValueError(
            f"Dashboard header audit metadata is missing or invalid: {header_audit_path}"
        )

    added_by_category: dict[str, int] = {}
    all_audited_variables: set[str] = set()
    for audit_key, audit_category in audit_categories.items():
        category = str(audit_category.get("category") or audit_key).replace("_", " ").strip()
        if category not in rules["category_q"]:
            continue
        audited_variables: set[str] = set()
        for row in audit_category.get("rows", []):
            if not isinstance(row, dict):
                continue
            source_table = str(row.get("sourceTable") or "").strip().lower()
            if source_table not in {"responses_fact", "question_catalog", "derived"}:
                continue
            audited_variables.update(
                map_metadata_variables_to_old_format(
                    row.get("sourceVariableNames") or [],
                    datamap,
                )
            )
        before = len(rules["category_q"][category])
        rules["category_q"][category].update(audited_variables)
        added_by_category[category] = len(rules["category_q"][category]) - before
        all_audited_variables.update(audited_variables)

    # The dashboard audit is the serving contract, so audited variables must not
    # be discarded by a stale "Remove" rule in the current-form workbook.
    overridden_remove_vars = rules["remove_vars"] & all_audited_variables
    rules["remove_vars"] -= all_audited_variables
    return {
        "path": str(header_audit_path),
        "auditedVariables": len(all_audited_variables),
        "addedVariablesByCategory": added_by_category,
        "removeRulesOverridden": len(overridden_remove_vars),
    }


def determine_respondent_id_column(raw_df: pd.DataFrame, rules: dict[str, Any], configured_column: str | None) -> str:
    candidates: list[str] = []
    configured = str(configured_column or "").strip()
    if configured:
        candidates.append(configured)
    candidates.extend(rules.get("identifier_vars", []))
    candidates.extend(DEFAULT_ID_CANDIDATES)

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in raw_df.columns:
            return normalized

    preview = ", ".join(list(map(str, raw_df.columns[:20])))
    raise ValueError(
        "Required respondent id column not found. "
        f"Tried: {', '.join(seen)}. "
        f"Available columns include: {preview}"
    )


def fetch_surveycto_submissions(config: dict[str, Any], since_iso: str | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    date_param = datetime_to_epoch_seconds(since_iso)
    url = (
        f"https://{config['surveycto_server']}.surveycto.com"
        f"/api/v2/forms/data/wide/json/{config['surveycto_form_id']}?date={date_param}"
    )
    info: dict[str, Any] = {
        "server": config["surveycto_server"],
        "formId": config["surveycto_form_id"],
        "dateParam": date_param,
        "sinceIso": since_iso,
    }
    response = requests.get(
        url,
        auth=(config["surveycto_username"], config["surveycto_password"]),
        timeout=config["request_timeout_seconds"],
        stream=True,
    )
    info["statusCode"] = response.status_code
    info["contentType"] = response.headers.get("content-type")
    info["contentLength"] = response.headers.get("content-length")
    if response.status_code == 417:
        info["rows"] = 0
        info["emptyReason"] = "surveycto_417_no_rows"
        return pd.DataFrame(), info
    if response.status_code != 200:
        raise RuntimeError(f"SurveyCTO request failed ({response.status_code}): {response.text[:500]}")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temp_file:
        temp_path = Path(temp_file.name)
        response.raw.decode_content = True
        shutil.copyfileobj(response.raw, temp_file)
    try:
        info["downloadedBytes"] = temp_path.stat().st_size
        df = drop_unnecessary_columns(pd.read_json(temp_path, encoding="utf-8-sig"))
        info["rows"] = int(len(df))
        info["columns"] = int(len(df.columns))
        return df, info
    finally:
        temp_path.unlink(missing_ok=True)


def load_raw_master(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def load_raw_master_keys(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(path, columns=["KEY"])
    except Exception:
        return pd.DataFrame()


def save_raw_master(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def normalize_raw_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df
    for col in ["CompletionDate", "SubmissionDate", "starttime", "endtime", "start", "end"]:
        if col in out.columns:
            parsed = parse_datetime_series(out[col])
            out[col] = parsed
    return out


def deduplicate_submissions(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if df.empty:
        return df, 0

    combined = drop_unnecessary_columns(df)
    combined = normalize_raw_dates(combined)
    before = len(combined)
    if "KEY" in combined.columns:
        sort_cols = [c for c in ["CompletionDate", "SubmissionDate"] if c in combined.columns]
        if sort_cols:
            combined = combined.sort_values(sort_cols)
        combined = combined.drop_duplicates(subset="KEY", keep="last")
    return combined.reset_index(drop=True), max(0, before - len(combined))


def merge_submissions(master: pd.DataFrame, new_df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    if new_df.empty:
        return master.copy(), 0

    combined = pd.concat([master, new_df], ignore_index=True, sort=False) if not master.empty else new_df.copy()
    return deduplicate_submissions(combined)


def dataframe_fingerprint(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    normalized = df.copy()
    normalized.columns = normalized.columns.map(str)
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    sort_columns = [column for column in ["KEY", "CompletionDate", "SubmissionDate"] if column in normalized.columns]
    if sort_columns:
        normalized = normalized.sort_values(sort_columns, na_position="last")
    normalized = normalized.reset_index(drop=True)
    for column in normalized.columns:
        if pd.api.types.is_datetime64_any_dtype(normalized[column]):
            normalized[column] = normalized[column].astype("string")
    return int(pd.util.hash_pandas_object(normalized.astype("string"), index=False).sum())


def reconcile_authoritative_snapshot(
    master: pd.DataFrame,
    snapshot: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, int | bool]]:
    reconciled, duplicates_dropped = deduplicate_submissions(snapshot)
    master_keys = set(master["KEY"].dropna().astype(str)) if "KEY" in master.columns else set()
    snapshot_keys = set(reconciled["KEY"].dropna().astype(str)) if "KEY" in reconciled.columns else set()
    changed = dataframe_fingerprint(master) != dataframe_fingerprint(reconciled)
    return reconciled, {
        "changed": changed,
        "duplicatesDropped": duplicates_dropped,
        "addedRows": len(snapshot_keys - master_keys),
        "deletedRows": len(master_keys - snapshot_keys),
    }


def month_key_from_completion(value: Any) -> str | None:
    if pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    return f"{ts.year:04d}-{ts.month:02d}"


def write_parquet(table: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(table, preserve_index=False), path, compression="snappy")


def build_parquet_staging(
    raw_df: pd.DataFrame,
    rules: dict[str, Any],
    staging_root: Path,
    xlsform_metadata: dict[str, Any] | None = None,
) -> dict[str, int]:
    long_root = staging_root / "responses_long_parquet"
    base_root = staging_root / "responses_base_parquet"
    spss_root = staging_root / "spss_case_values_parquet"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    long_root.mkdir(parents=True, exist_ok=True)
    base_root.mkdir(parents=True, exist_ok=True)
    spss_root.mkdir(parents=True, exist_ok=True)

    if raw_df.empty:
        raise ValueError("Raw master is empty; cannot build DuckDB.")
    if "CompletionDate" not in raw_df.columns:
        raise ValueError("CompletionDate is required to derive month partitions.")

    id_var = determine_respondent_id_column(raw_df, rules, rules.get("configured_id_var"))
    if id_var not in raw_df.columns:
        raise ValueError(f"Required respondent id column missing: {id_var}")

    df = normalize_raw_dates(raw_df)
    df["__month"] = df["CompletionDate"].map(month_key_from_completion)
    df = df.loc[df["__month"].notna()].copy()
    if df.empty:
        raise ValueError("No rows have a valid CompletionDate month.")
    current_month_floor = str(rules.get("current_month_floor") or "").strip()
    if current_month_floor:
        df = df.loc[df["__month"].astype(str) > current_month_floor].copy()
        if df.empty:
            raise ValueError(f"No SurveyCTO rows are newer than market DB month {current_month_floor}.")

    remove_vars: set[str] = rules["remove_vars"]
    demo_vars: set[str] = rules["demo_vars"]
    general_vars: set[str] = rules["general_vars"]
    panel_vars: set[str] = rules["panel_vars"]
    category_q: dict[str, set[str]] = rules["category_q"]
    category_names: set[str] = rules["category_names"]

    keep_vars = set(demo_vars) | set(general_vars) | set(panel_vars)
    for category in category_names:
        keep_vars |= category_q[category]
    keep_vars -= remove_vars
    # Dashboard dimensions must survive even when the current metadata workbook
    # excludes them from response facts (for example, SEC is marked Remove).
    for candidates in RESPONDENT_DIM_FIELD_SOURCES.values():
        source = resolve_column_name(list(df.columns), candidates)
        if source:
            keep_vars.add(source)
    keep_vars.add(id_var)
    keep_vars.add("__month")
    available = [c for c in df.columns if c in keep_vars]
    df = df[available].copy()
    if id_var not in df.columns:
        raise ValueError(f"Required respondent id column missing after filtering: {id_var}")
    df[id_var] = df[id_var].astype("string")

    for col in df.columns:
        if col in {id_var, "__month"}:
            continue
        df[col] = remove_empty_strings(clean_minus_one(df[col]))

    xlsform_metadata = xlsform_metadata or {"questions": {}, "lists": {}, "lookup": {}}

    long_rows = 0
    base_rows = 0
    spss_rows = 0

    for month, month_df in df.groupby("__month", dropna=True):
        month_value = str(month)
        demo_df = build_respondent_dim_frame(month_df, id_var, xlsform_metadata)

        for category in sorted(category_names):
            panel_var = CATEGORY_TO_PANEL.get(category)
            if not panel_var or panel_var not in month_df.columns:
                continue
            selected = is_selected_panel(month_df[panel_var])
            if not selected.any():
                continue

            allowed_q = set(general_vars) | set(category_q.get(category, set()))
            allowed_q -= set(demo_vars)
            allowed_q -= set(panel_vars)
            allowed_q -= remove_vars
            allowed_q = [q for q in allowed_q if q in month_df.columns]
            if not allowed_q:
                continue

            selected_df = month_df.loc[selected, [id_var] + allowed_q].copy()
            selected_demo = demo_df.loc[selected, :].copy()
            category_chunks: list[pd.DataFrame] = []
            spss_chunks: list[pd.DataFrame] = []
            base_records: list[dict[str, Any]] = []

            # Preserve respondent-level raw values (including binary zeroes) for
            # SPSS-compatible bases, filters, AUTORECODE, and stored Step 1 rules.
            for dim_field, candidates in RESPONDENT_DIM_FIELD_SOURCES.items():
                source = resolve_column_name(list(month_df.columns), candidates)
                if not source:
                    continue
                raw_dim = remove_empty_strings(clean_minus_one(month_df.loc[selected, source]))
                answered_dim = raw_dim.notna()
                if not answered_dim.any():
                    continue
                ids = month_df.loc[selected, id_var].loc[answered_dim].astype("string")
                dim_chunk = pd.DataFrame({
                    "category": category,
                    "respondent_id": ids.values,
                    "file_month": month_value,
                    "variable": dim_field,
                    "value_text": raw_dim.loc[answered_dim].astype("string").values,
                    "value_num": pd.to_numeric(raw_dim.loc[answered_dim], errors="coerce").values,
                    "value_label": selected_demo.loc[answered_dim, dim_field].astype("string").values,
                })
                spss_chunks.append(dim_chunk)
                if source.lower() != dim_field.lower():
                    alias_chunk = dim_chunk.copy()
                    alias_chunk["variable"] = source
                    spss_chunks.append(alias_chunk)

            for question in allowed_q:
                series = selected_df[question]
                metadata_entry = get_xlsform_question_metadata(question, xlsform_metadata)
                question_text = build_question_text(question, metadata_entry)
                if is_binary_01(series):
                    mask = pd.to_numeric(series, errors="coerce").eq(1)
                else:
                    mask = series.notna()
                answered = series.notna()
                if answered.any():
                    answered_ids = selected_df.loc[answered, id_var]
                    numeric = pd.to_numeric(series.loc[answered], errors="coerce")
                    if is_binary_01(series.loc[answered]):
                        n_ones: int | None = int(numeric.eq(1).sum())
                        n_zeros: int | None = int(numeric.eq(0).sum())
                    else:
                        n_ones = None
                        n_zeros = None
                    base_records.append(
                        {
                            "category": category,
                            "file_month": month_value,
                            "question": question,
                            "base_n": int(answered_ids.nunique()),
                            "n_ones": n_ones,
                            "n_zeros": n_zeros,
                        }
                    )
                    spss_chunk = pd.DataFrame({
                        "category": category,
                        "respondent_id": answered_ids.astype("string").values,
                        "file_month": month_value,
                        "variable": question,
                        "value_text": remove_empty_strings(series.loc[answered]).astype("string").values,
                        "value_num": numeric.values,
                        "value_label": [
                            build_answer_text(question, value, metadata_entry, is_binary_01(series))
                            for value in series.loc[answered].tolist()
                        ],
                    })
                    spss_chunks.append(spss_chunk)
                if not mask.any():
                    continue

                raw_answer = series.loc[mask]
                ids = selected_df.loc[mask, id_var].astype("string")
                chunk = selected_demo.loc[mask, :].copy()
                chunk["respondent_id"] = ids.values
                chunk["SbjNum"] = ids.values
                chunk["category"] = category
                chunk["file_month"] = month_value
                chunk["question"] = question
                chunk["question_label"] = question_text
                chunk["answer_value_num"] = pd.to_numeric(raw_answer, errors="coerce")
                chunk["answer_value"] = remove_empty_strings(raw_answer).astype("string").values
                chunk["answer_label"] = [
                    build_answer_text(question, value, metadata_entry, is_binary_01(series))
                    for value in chunk["answer_value"].tolist()
                ]
                chunk = chunk.loc[chunk["answer_value"].notna()].copy()
                if not chunk.empty:
                    category_chunks.append(chunk)

            safe_category = sanitize_category(category)
            if category_chunks:
                long_df = pd.concat(category_chunks, ignore_index=True)
                for col in ["file_month", "category", "question", "question_label", "answer_value", "answer_label"]:
                    if col in long_df.columns:
                        long_df[col] = long_df[col].astype("string")
                long_df["answer_value_num"] = pd.to_numeric(long_df["answer_value_num"], errors="coerce")
                out = (
                    long_root
                    / f"category={safe_category}"
                    / f"month={month_value}"
                    / f"part-{safe_category}-{month_value}-{uuid.uuid4().hex}.parquet"
                )
                write_parquet(long_df, out)
                long_rows += len(long_df)

            if base_records:
                base_df = pd.DataFrame(base_records)
                base_df["category"] = base_df["category"].astype("string")
                base_df["file_month"] = base_df["file_month"].astype("string")
                base_df["question"] = base_df["question"].astype("string")
                base_df["base_n"] = pd.to_numeric(base_df["base_n"], errors="coerce").astype("Int64")
                base_df["n_ones"] = pd.to_numeric(base_df["n_ones"], errors="coerce").astype("Int64")
                base_df["n_zeros"] = pd.to_numeric(base_df["n_zeros"], errors="coerce").astype("Int64")
                out = (
                    base_root
                    / f"category={safe_category}"
                    / f"month={month_value}"
                    / f"base-{safe_category}-{month_value}.parquet"
                )
                write_parquet(base_df, out)
                base_rows += len(base_df)

            if spss_chunks:
                spss_df = pd.concat(spss_chunks, ignore_index=True)
                # Match respondent_dims/responses_fact, whose category is supplied
                # by the hive partition folder (for example Breakfast_Cereal).
                spss_df["category"] = safe_category
                for col in ["category", "respondent_id", "file_month", "variable", "value_text", "value_label"]:
                    spss_df[col] = spss_df[col].astype("string")
                spss_df["value_num"] = pd.to_numeric(spss_df["value_num"], errors="coerce")
                spss_df = spss_df.drop_duplicates(
                    subset=["category", "respondent_id", "file_month", "variable"], keep="last"
                )
                out = (
                    spss_root
                    / f"category={safe_category}"
                    / f"month={month_value}"
                    / f"spss-{safe_category}-{month_value}-{uuid.uuid4().hex}.parquet"
                )
                write_parquet(spss_df, out)
                spss_rows += len(spss_df)

    if long_rows <= 0:
        raise ValueError("No long response rows were generated.")
    if base_rows <= 0:
        raise ValueError("No base rows were generated.")
    return {"long_rows": long_rows, "base_rows": base_rows, "spss_rows": spss_rows}


def quote_sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def slash_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def sql_in_list(values: list[str]) -> str:
    return ", ".join(quote_sql(value) for value in values) or "''"


def get_generated_months(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute("SELECT DISTINCT CAST(month AS VARCHAR) FROM respondent_dims ORDER BY 1").fetchall()
    return [str(row[0]) for row in rows if row and row[0] is not None]


def merge_seed_history(
    con: duckdb.DuckDBPyConnection,
    seed_db_path: Path,
    generated_months: list[str],
    replace_mode: str,
) -> dict[str, Any]:
    if not seed_db_path.exists() or not generated_months:
        return {"seedMerged": False, "replacementMonths": generated_months}

    replacement_months = generated_months
    if replace_mode == "latest":
        replacement_months = [max(generated_months)]

    month_sql = sql_in_list(replacement_months)

    # Copy the seed DB before attaching — DuckDB locks its file even with READ_ONLY,
    # which conflicts with the Node server's live connection. POSIX advisory locks
    # don't block file copies, so shutil.copy2 succeeds while Node is running.
    seed_copy_path = seed_db_path.parent / f"seed_copy_{uuid.uuid4().hex}.duckdb"
    try:
        shutil.copy2(seed_db_path, seed_copy_path)
    except OSError:
        return {"seedMerged": False, "replacementMonths": replacement_months}

    try:
        seed_path = slash_path(seed_copy_path)
        con.execute(f"ATTACH {quote_sql(seed_path)} AS seed_db (READ_ONLY)")
    except Exception:
        seed_copy_path.unlink(missing_ok=True)
        return {"seedMerged": False, "replacementMonths": replacement_months}

    try:
        seed_tables = {
            row[0]
            for row in con.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_catalog = 'seed_db'
                  AND table_schema = 'main'
                """
            ).fetchall()
        }
        if "respondent_dims" not in seed_tables or "responses_fact" not in seed_tables:
            return {"seedMerged": False, "replacementMonths": replacement_months}

        seed_dim_columns = [
            row[0] for row in con.execute("DESCRIBE SELECT * FROM seed_db.main.respondent_dims").fetchall()
        ]
        seed_dim_projection = ",\n              ".join(build_respondent_dim_projection(seed_dim_columns))

        con.execute(f"DELETE FROM respondent_dims WHERE CAST(month AS VARCHAR) NOT IN ({month_sql})")
        con.execute(f"DELETE FROM responses_fact WHERE CAST(month AS VARCHAR) NOT IN ({month_sql})")
        con.execute(
            f"""
            INSERT INTO respondent_dims
            SELECT
              CAST(category AS VARCHAR) AS category,
              CAST(respondent_id AS VARCHAR) AS respondent_id,
              CAST(month AS VARCHAR) AS month,
              {seed_dim_projection}
            FROM seed_db.main.respondent_dims
            WHERE CAST(month AS VARCHAR) NOT IN ({month_sql})
            """
        )
        con.execute(
            f"""
            INSERT INTO responses_fact
            SELECT
              CAST(category AS VARCHAR) AS category,
              CAST(respondent_id AS VARCHAR) AS respondent_id,
              CAST(month AS VARCHAR) AS month,
              CAST(question AS VARCHAR) AS question,
              CAST(question_label AS VARCHAR) AS question_label,
              CAST(answer_label AS VARCHAR) AS answer_label,
              CAST(answer_value AS VARCHAR) AS answer_value,
              CAST(answer_value_num AS DOUBLE) AS answer_value_num
            FROM seed_db.main.responses_fact
            WHERE CAST(month AS VARCHAR) NOT IN ({month_sql})
            """
        )
        # Preserve raw zeroes and prior Step 1 values once a seed database has
        # been built by this implementation. Older seed databases simply fall
        # back to responses_fact/respondent_dims reconstruction below.
        if "spss_case_values" in seed_tables:
            con.execute(
                f"""
                CREATE OR REPLACE TEMP TABLE seed_spss_case_values AS
                SELECT
                  CAST(category AS VARCHAR) AS category,
                  CAST(respondent_id AS VARCHAR) AS respondent_id,
                  CAST(month AS VARCHAR) AS month,
                  CAST(variable AS VARCHAR) AS variable,
                  CAST(value_text AS VARCHAR) AS value_text,
                  CAST(value_num AS DOUBLE) AS value_num,
                  CAST(value_label AS VARCHAR) AS value_label
                FROM seed_db.main.spss_case_values
                WHERE CAST(month AS VARCHAR) NOT IN ({month_sql})
                """
            )
        return {"seedMerged": True, "replacementMonths": replacement_months}
    finally:
        try:
            con.execute("DETACH seed_db")
        except Exception:
            pass
        seed_copy_path.unlink(missing_ok=True)



def load_spss_rules() -> dict[str, Any]:
    return read_json(SPSS_RULES_PATH, {"autorecode": [], "bau4": [], "region2": {}, "variableLabels": {}})


def spss_value_sort_key(item: tuple[str, float | None, str]) -> tuple[Any, ...]:
    value_text, value_num, _label = item
    if value_num is not None and not pd.isna(value_num):
        return (0, float(value_num), str(value_text))
    return (1, str(value_text).casefold(), str(value_text))


def build_spss_case_tables(con: duckdb.DuckDBPyConnection, staging_root: Path) -> dict[str, int]:
    """Create persistent SPSS-compatible long values and deterministic recodes."""
    rules = load_spss_rules()
    spss_glob = staging_root / "spss_case_values_parquet" / "category=*" / "month=*" / "*.parquet"
    spss_files = list((staging_root / "spss_case_values_parquet").glob("category=*/month=*/*.parquet"))
    if spss_files:
        con.execute(
            f"""
            CREATE OR REPLACE TABLE spss_case_values_raw AS
            SELECT
              CAST(category AS VARCHAR) AS category,
              CAST(respondent_id AS VARCHAR) AS respondent_id,
              CAST(file_month AS VARCHAR) AS month,
              CAST(variable AS VARCHAR) AS variable,
              CAST(value_text AS VARCHAR) AS value_text,
              CAST(value_num AS DOUBLE) AS value_num,
              CAST(value_label AS VARCHAR) AS value_label
            FROM read_parquet({quote_sql(slash_path(spss_glob))}, union_by_name = true)
            """
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE TABLE spss_case_values_raw (
              category VARCHAR, respondent_id VARCHAR, month VARCHAR, variable VARCHAR,
              value_text VARCHAR, value_num DOUBLE, value_label VARCHAR
            )
            """
        )

    seed_spss_exists = bool(
        con.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE lower(table_name) = 'seed_spss_case_values'
            """
        ).fetchone()[0]
    )
    seed_union = "UNION ALL SELECT * FROM seed_spss_case_values" if seed_spss_exists else ""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE spss_case_values AS
        SELECT * FROM spss_case_values_raw
        {seed_union}
        UNION ALL
        SELECT
          CAST(r.category AS VARCHAR), CAST(r.respondent_id AS VARCHAR), CAST(r.month AS VARCHAR),
          CAST(r.question AS VARCHAR), CAST(r.answer_value AS VARCHAR), CAST(r.answer_value_num AS DOUBLE),
          CAST(r.answer_label AS VARCHAR)
        FROM responses_fact r
        WHERE NOT EXISTS (
          SELECT 1 FROM spss_case_values_raw s
          WHERE CAST(s.category AS VARCHAR) = CAST(r.category AS VARCHAR)
            AND CAST(s.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
            AND CAST(s.month AS VARCHAR) = CAST(r.month AS VARCHAR)
            AND lower(CAST(s.variable AS VARCHAR)) = lower(CAST(r.question AS VARCHAR))
        )
        {"AND NOT EXISTS (SELECT 1 FROM seed_spss_case_values s WHERE CAST(s.category AS VARCHAR)=CAST(r.category AS VARCHAR) AND CAST(s.respondent_id AS VARCHAR)=CAST(r.respondent_id AS VARCHAR) AND CAST(s.month AS VARCHAR)=CAST(r.month AS VARCHAR) AND lower(CAST(s.variable AS VARCHAR))=lower(CAST(r.question AS VARCHAR)))" if seed_spss_exists else ""}
        """
    )

    # Recreate the explicit source renames documented at the top of the SPSS
    # syntax before falling back to labelled respondent dimensions. Historical
    # seed databases often still carry City_1/D3_Q/D5_Q/marital_status.
    alias_targets = {
        "Region": ["City_1"],
        "D3": ["D3_Q"],
        "D5": ["D5_Q", "Working_Status", "Employment_Status"],
        "B1": ["Marital_Status"],
    }
    for target, aliases in alias_targets.items():
        for alias in aliases:
            con.execute(
                """
                INSERT INTO spss_case_values
                SELECT v.category, v.respondent_id, v.month, ?, v.value_text, v.value_num, v.value_label
                FROM spss_case_values v
                WHERE lower(v.variable)=lower(?)
                  AND NOT EXISTS (
                    SELECT 1 FROM spss_case_values x
                    WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
                      AND lower(x.variable)=lower(?)
                  )
                """,
                [target, alias, target],
            )

    # Seed databases created before this feature have dimensions only as labels.
    # Add them as fallback values; newly synced rows retain both raw code and label.
    dim_columns = {row[0] for row in con.execute("DESCRIBE respondent_dims").fetchall()}
    for field in RESPONDENT_DIM_FIELDS:
        if field not in dim_columns:
            continue
        con.execute(
            f"""
            INSERT INTO spss_case_values
            SELECT
              CAST(d.category AS VARCHAR), CAST(d.respondent_id AS VARCHAR), CAST(d.month AS VARCHAR),
              {quote_sql(field)}, CAST(d.{chr(34) + field + chr(34)} AS VARCHAR),
              TRY_CAST(d.{chr(34) + field + chr(34)} AS DOUBLE), CAST(d.{chr(34) + field + chr(34)} AS VARCHAR)
            FROM respondent_dims d
            WHERE NULLIF(TRIM(CAST(d.{chr(34) + field + chr(34)} AS VARCHAR)), '') IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM spss_case_values s
                WHERE CAST(s.category AS VARCHAR) = CAST(d.category AS VARCHAR)
                  AND CAST(s.respondent_id AS VARCHAR) = CAST(d.respondent_id AS VARCHAR)
                  AND CAST(s.month AS VARCHAR) = CAST(d.month AS VARCHAR)
                  AND lower(CAST(s.variable AS VARCHAR)) = lower({quote_sql(field)})
              )
            """
        )

    variable_labels = rules.get("variableLabels") or {}
    region2 = rules.get("region2") or {}
    region_map = region2.get("map") or {}
    region_labels = region2.get("labels") or {}
    region_rows: list[tuple[str, int, str]] = []
    seen_region_sources: set[str] = set()
    for source, target in region_map.items():
        for source_value in (str(source), SPSS_CITY_CODE_LABELS.get(str(source))):
            if not source_value:
                continue
            key = source_value.strip().casefold()
            if not key or key in seen_region_sources:
                continue
            seen_region_sources.add(key)
            region_rows.append((source_value.strip(), int(target), str(region_labels.get(str(target), target))))
    con.execute("CREATE OR REPLACE TABLE spss_region2_map(source_value VARCHAR, target_code INTEGER, target_label VARCHAR)")
    if region_rows:
        con.executemany("INSERT INTO spss_region2_map VALUES (?, ?, ?)", region_rows)
        con.execute(
            """
            INSERT INTO spss_case_values
            SELECT v.category, v.respondent_id, v.month, 'REGION2',
                   CAST(m.target_code AS VARCHAR), CAST(m.target_code AS DOUBLE), m.target_label
            FROM spss_case_values v
            JOIN spss_region2_map m
              ON lower(TRIM(COALESCE(CAST(TRY_CAST(v.value_num AS BIGINT) AS VARCHAR), TRIM(v.value_text)))) = lower(TRIM(m.source_value))
            WHERE lower(v.variable) = 'region'
              AND NOT EXISTS (
                SELECT 1 FROM spss_case_values x
                WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
                  AND lower(x.variable)='region2'
              )
            """
        )

    con.execute("CREATE OR REPLACE TABLE spss_bau4_rules(target VARCHAR, source1 VARCHAR, source2 VARCHAR, value_label VARCHAR)")
    bau4_rows = [
        (str(rule.get("target")), str((rule.get("sources") or [""])[0]), str((rule.get("sources") or ["", ""])[1]),
         str(variable_labels.get(str(rule.get("target")), str(rule.get("target")))))
        for rule in (rules.get("bau4") or [])
        if rule.get("target") and len(rule.get("sources") or []) >= 2
    ]
    if bau4_rows:
        con.executemany("INSERT INTO spss_bau4_rules VALUES (?, ?, ?, ?)", bau4_rows)
        con.execute(
            """
            INSERT INTO spss_case_values
            SELECT DISTINCT v.category, v.respondent_id, v.month, r.target, '1', 1.0, r.value_label
            FROM spss_bau4_rules r
            JOIN spss_case_values v
              ON lower(v.variable) IN (lower(r.source1), lower(r.source2))
             AND COALESCE(v.value_num, TRY_CAST(v.value_text AS DOUBLE)) = 1
            WHERE NOT EXISTS (
              SELECT 1 FROM spss_case_values x
              WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
                AND lower(x.variable)=lower(r.target)
            )
            """
        )

    con.execute("CREATE OR REPLACE TABLE spss_autorecode_maps(source_variable VARCHAR, target_variable VARCHAR, source_value VARCHAR, source_label VARCHAR, target_code INTEGER)")
    for autorecode in rules.get("autorecode") or []:
        source = str(autorecode.get("source") or "").strip()
        target = str(autorecode.get("target") or "").strip()
        if not source or not target:
            continue
        rows = con.execute(
            """
            SELECT
                   CASE WHEN value_num IS NOT NULL THEN printf('%.15g', value_num) ELSE CAST(value_text AS VARCHAR) END,
                   MIN(value_num),
                   COALESCE(MAX(NULLIF(TRIM(value_label), '')), MAX(CAST(value_text AS VARCHAR)))
            FROM spss_case_values
            WHERE lower(variable)=lower(?) AND NULLIF(TRIM(value_text), '') IS NOT NULL
            GROUP BY CASE WHEN value_num IS NOT NULL THEN printf('%.15g', value_num) ELSE CAST(value_text AS VARCHAR) END
            """,
            [source],
        ).fetchall()
        ordered = sorted([(str(row[0]), row[1], str(row[2])) for row in rows], key=spss_value_sort_key)
        mappings = [(source, target, value, label, index + 1) for index, (value, _num, label) in enumerate(ordered)]
        if not mappings:
            continue
        con.executemany("INSERT INTO spss_autorecode_maps VALUES (?, ?, ?, ?, ?)", mappings)
        con.execute(
            """
            INSERT INTO spss_case_values
            SELECT v.category, v.respondent_id, v.month, m.target_variable,
                   CAST(m.target_code AS VARCHAR), CAST(m.target_code AS DOUBLE), m.source_label
            FROM spss_case_values v
            JOIN spss_autorecode_maps m
              ON lower(v.variable)=lower(m.source_variable)
             AND (CASE WHEN v.value_num IS NOT NULL THEN printf('%.15g', v.value_num) ELSE v.value_text END)=m.source_value
            WHERE lower(m.source_variable)=lower(?) AND lower(m.target_variable)=lower(?)
              AND NOT EXISTS (
                SELECT 1 FROM spss_case_values x
                WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
                  AND lower(x.variable)=lower(m.target_variable)
              )
            """,
            [source, target],
        )

    con.execute(
        """
        CREATE OR REPLACE TABLE spss_variable_dictionary AS
        SELECT category, variable, value_text, value_num,
               COALESCE(MAX(NULLIF(TRIM(value_label), '')), value_text) AS value_label
        FROM spss_case_values
        WHERE NULLIF(TRIM(variable), '') IS NOT NULL AND NULLIF(TRIM(value_text), '') IS NOT NULL
        GROUP BY category, variable, value_text, value_num
        """
    )

    # Persist common transformed dimensions as respondent columns as well as in
    # the long SPSS value table. Step 2 awareness totals are intentionally not stored.
    for target in ["REGION2", "AGE2", "SEC2", "AGE2_New", "Gender_New"]:
        con.execute(f'ALTER TABLE respondent_dims ADD COLUMN IF NOT EXISTS "{target}" VARCHAR')
        con.execute(
            f"""
            UPDATE respondent_dims AS d
            SET "{target}" = v.value_text
            FROM spss_case_values v
            WHERE CAST(v.category AS VARCHAR)=CAST(d.category AS VARCHAR)
              AND CAST(v.respondent_id AS VARCHAR)=CAST(d.respondent_id AS VARCHAR)
              AND CAST(v.month AS VARCHAR)=CAST(d.month AS VARCHAR)
              AND lower(v.variable)=lower({quote_sql(target)})
            """
        )

    con.execute("DROP TABLE IF EXISTS spss_case_values_raw")
    return {
        "spss_case_values": int(con.execute("SELECT COUNT(*) FROM spss_case_values").fetchone()[0]),
        "spss_autorecode_maps": int(con.execute("SELECT COUNT(*) FROM spss_autorecode_maps").fetchone()[0]),
        "spss_bau4_rules": int(con.execute("SELECT COUNT(*) FROM spss_bau4_rules").fetchone()[0]),
    }


def build_duckdb_from_staging(staging_root: Path, db_path: Path, seed_db_path: Path, replace_mode: str) -> dict[str, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ["", ".wal", "-wal", ".tmp"]:
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    long_glob = slash_path(staging_root / "responses_long_parquet" / "category=*" / "month=*" / "*.parquet")
    data_root = Path(os.environ.get("DATA_ROOT", "/var/data")).resolve()
    base_temp_directory = Path(os.environ.get("DUCKDB_TEMP_DIRECTORY", str(data_root / "duckdb_tmp"))).resolve()
    sync_temp_directory = base_temp_directory / f"sync-{uuid.uuid4().hex}"
    sync_temp_directory.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        threads = max(1, int(os.environ.get("DUCKDB_THREADS", "2")))
        memory_limit = os.environ.get("DUCKDB_MEMORY_LIMIT", "1536MB")
        max_temp_directory_size = os.environ.get("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "7GB")
        con.execute(f"PRAGMA threads={threads}")
        con.execute(f"PRAGMA memory_limit={quote_sql(memory_limit)}")
        con.execute(f"PRAGMA temp_directory={quote_sql(str(sync_temp_directory))}")
        con.execute(f"PRAGMA max_temp_directory_size={quote_sql(max_temp_directory_size)}")
        con.execute("SET preserve_insertion_order=false")
        long_columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM read_parquet({quote_sql(long_glob)}, hive_partitioning = true)").fetchall()]
        dim_projection = build_respondent_dim_projection(long_columns)

        con.execute(
            f"""
            CREATE TABLE respondent_dims AS
            SELECT DISTINCT
              CAST(category AS VARCHAR) AS category,
              CAST(respondent_id AS VARCHAR) AS respondent_id,
              CAST(month AS VARCHAR) AS month,
              {", ".join(dim_projection)}
            FROM read_parquet({quote_sql(long_glob)}, hive_partitioning = true)
            """
        )
        con.execute(
            f"""
            CREATE TABLE responses_fact AS
            SELECT
              CAST(category AS VARCHAR) AS category,
              CAST(respondent_id AS VARCHAR) AS respondent_id,
              CAST(month AS VARCHAR) AS month,
              CAST(question AS VARCHAR) AS question,
              CAST(question_label AS VARCHAR) AS question_label,
              COALESCE(NULLIF(TRIM(CAST(answer_label AS VARCHAR)), ''), '(No response)') AS answer_label,
              CAST(answer_value AS VARCHAR) AS answer_value,
              CAST(answer_value_num AS DOUBLE) AS answer_value_num
            FROM read_parquet({quote_sql(long_glob)}, hive_partitioning = true)
            WHERE category IS NOT NULL
              AND question IS NOT NULL
            """
        )
        generated_months = get_generated_months(con)
        seed_merge = merge_seed_history(con, seed_db_path, generated_months, replace_mode)
        spss_counts = build_spss_case_tables(con, staging_root)
        counts = {
            "respondent_dims": int(con.execute("SELECT COUNT(*) FROM respondent_dims").fetchone()[0]),
            "responses_fact": int(con.execute("SELECT COUNT(*) FROM responses_fact").fetchone()[0]),
            "generated_months": generated_months,
            **seed_merge,
            **spss_counts,
        }
        if counts["respondent_dims"] <= 0 or counts["responses_fact"] <= 0:
            raise ValueError(f"Generated DuckDB has invalid counts: {counts}")
        return counts
    finally:
        con.close()
        shutil.rmtree(sync_temp_directory, ignore_errors=True)


def validate_duckdb_file(db_path: Path) -> dict[str, Any]:
    con = None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        row = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM respondent_dims) AS respondent_dims,
              (SELECT COUNT(*) FROM responses_fact) AS responses_fact,
              (SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims) AS latest_month
            """
        ).fetchone()
        counts = {
            "respondent_dims": int(row[0] or 0),
            "responses_fact": int(row[1] or 0),
            "latest_month": str(row[2]) if row and row[2] is not None else None,
        }
        if counts["respondent_dims"] <= 0 or counts["responses_fact"] <= 0:
            raise ValueError(f"Generated DuckDB failed validation: {counts}")
        return counts
    finally:
        if con is not None:
            con.close()


def prune_duckdb_backups(backup_dir: Path, retention: int) -> None:
    if retention <= 0 or not backup_dir.exists():
        return
    backups = sorted(
        [path for path in backup_dir.glob("current-*.duckdb") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for backup in backups[retention:]:
        backup.unlink(missing_ok=True)


def remove_duckdb_sidecars(db_path: Path) -> None:
    for suffix in [".wal", "-wal"]:
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def promote_duckdb_file(temp_db_path: Path, active_db_path: Path, data_root: Path, backup_retention: int) -> dict[str, Any]:
    if not temp_db_path.exists():
        raise ValueError(f"Sync produced no DuckDB file at {temp_db_path}")

    validation = validate_duckdb_file(temp_db_path)
    backup_dir = data_root / "backups"
    active_db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"current-{timestamp}.duckdb"
    moved_existing = False

    try:
        if active_db_path.exists():
            active_db_path.replace(backup_path)
            moved_existing = True
        temp_db_path.replace(active_db_path)
    except Exception:
        if moved_existing and not active_db_path.exists() and backup_path.exists():
            backup_path.replace(active_db_path)
        raise

    remove_duckdb_sidecars(temp_db_path)
    remove_duckdb_sidecars(active_db_path)
    prune_duckdb_backups(backup_dir, backup_retention)
    return {
        "activeDbPath": str(active_db_path),
        "backupPath": str(backup_path) if moved_existing else None,
        "validation": validation,
        "promotedBy": "surveycto_bht_sync.py",
    }


def latest_completion_iso(df: pd.DataFrame) -> str | None:
    if df.empty or "CompletionDate" not in df.columns:
        return None
    parsed = parse_datetime_series(df["CompletionDate"])
    if parsed.notna().any():
        return parsed.max().isoformat()
    return None


def latest_month(df: pd.DataFrame) -> str | None:
    if df.empty or "CompletionDate" not in df.columns:
        return None
    parsed = parse_datetime_series(df["CompletionDate"])
    months = [month_key_from_completion(value) for value in parsed.dropna()]
    return max(months) if months else None


def available_completion_months(df: pd.DataFrame) -> list[str]:
    if df.empty or "CompletionDate" not in df.columns:
        return []
    parsed = parse_datetime_series(df["CompletionDate"])
    return sorted({
        month
        for month in (month_key_from_completion(value) for value in parsed.dropna())
        if month
    })


def validate_old_format_question_codes(
    db_path: Path,
    datamap: dict[str, Any],
) -> dict[str, Any]:
    new_to_old: dict[str, str] = datamap.get("new_to_old") or {}
    expected_old_by_new = {
        str(new_name).strip().lower(): str(old_name).strip()
        for new_name, old_name in new_to_old.items()
        if str(new_name).strip()
        and str(old_name).strip()
        and str(new_name).strip().lower() != str(old_name).strip().lower()
    }
    if not expected_old_by_new:
        return {"checked": True, "mappedNewNames": 0, "leakedNewFormatQuestions": []}

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT DISTINCT CAST(question AS VARCHAR) AS question
            FROM responses_fact
            WHERE question IS NOT NULL
            """
        ).fetchall()
    finally:
        con.close()

    leaked = sorted({
        str(row[0]).strip()
        for row in rows
        if row
        and row[0] is not None
        and str(row[0]).strip().lower() in expected_old_by_new
    })
    if leaked:
        preview = ", ".join(leaked[:20])
        raise ValueError(
            "Generated DuckDB still contains mapped new-format question codes: "
            f"{preview}{' ...' if len(leaked) > 20 else ''}"
        )
    return {
        "checked": True,
        "mappedNewNames": len(expected_old_by_new),
        "leakedNewFormatQuestions": [],
    }


def inspect_generated_dashboard_coverage(
    db_path: Path,
    header_audit_path: Path,
    datamap: dict[str, Any],
) -> dict[str, Any]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        dim_columns = {
            str(row[0])
            for row in con.execute("DESCRIBE SELECT * FROM respondent_dims").fetchall()
        }
        latest_month_row = con.execute(
            "SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims"
        ).fetchone()
        latest_month_value = str(latest_month_row[0]) if latest_month_row and latest_month_row[0] else ""
        dimension_counts: dict[str, int] = {}
        for field in RESPONDENT_DIM_FIELDS:
            if field not in dim_columns:
                dimension_counts[field] = 0
                continue
            count = con.execute(
                f"""
                SELECT COUNT(*)
                FROM respondent_dims
                WHERE CAST(month AS VARCHAR) = ?
                  AND NULLIF(TRIM(CAST("{field}" AS VARCHAR)), '') IS NOT NULL
                """,
                [latest_month_value],
            ).fetchone()[0]
            dimension_counts[field] = int(count or 0)

        respondent_rows = con.execute(
            """
            SELECT CAST(category AS VARCHAR), COUNT(DISTINCT respondent_id)
            FROM respondent_dims
            WHERE CAST(month AS VARCHAR) = ?
            GROUP BY category
            """,
            [latest_month_value],
        ).fetchall()
        response_rows = con.execute(
            """
            SELECT
              CAST(category AS VARCHAR),
              CAST(question AS VARCHAR),
              COUNT(*)
            FROM responses_fact
            WHERE CAST(month AS VARCHAR) = ?
            GROUP BY category, question
            """,
            [latest_month_value],
        ).fetchall()
    finally:
        con.close()

    if latest_month_value and not any(dimension_counts.values()):
        raise ValueError(
            "Generated DuckDB has no populated dashboard demographics for "
            f"{latest_month_value}: {dimension_counts}"
        )

    audit = read_json(header_audit_path, {})
    audit_categories = audit.get("categories") if isinstance(audit, dict) else {}
    if not isinstance(audit_categories, dict) or not audit_categories:
        raise ValueError(
            f"Dashboard header audit metadata is missing or invalid: {header_audit_path}"
        )

    respondents_by_category = {
        norm_col(str(category)): {
            "category": str(category),
            "respondents": int(count or 0),
        }
        for category, count in respondent_rows
        if category is not None
    }
    questions_by_category: dict[str, dict[str, dict[str, Any]]] = {}
    for category, question, count in response_rows:
        if category is None or question is None:
            continue
        category_key = norm_col(str(category))
        question_key = str(question).strip().lower()
        questions_by_category.setdefault(category_key, {})[question_key] = {
            "question": str(question).strip(),
            "rows": int(count or 0),
        }

    category_reports: list[dict[str, Any]] = []
    empty_categories: list[str] = []
    zero_row_pages: list[str] = []
    for audit_key, audit_category in audit_categories.items():
        category_key = norm_col(str(audit_key).replace("_", " "))
        respondent_summary = respondents_by_category.get(category_key)
        if not respondent_summary or respondent_summary["respondents"] <= 0:
            continue

        page_variables: dict[str, set[str]] = {}
        for row in audit_category.get("rows", []):
            if not isinstance(row, dict):
                continue
            source_table = str(row.get("sourceTable") or "").strip().lower()
            if source_table not in {"responses_fact", "question_catalog", "derived"}:
                continue
            page = str(row.get("page") or "").strip()
            if not page:
                continue
            source_variables = row.get("sourceVariableNames") or []
            page_variables.setdefault(page, set()).update(
                map_metadata_variables_to_old_format(source_variables, datamap)
            )

        available_questions = questions_by_category.get(category_key, {})
        page_reports: list[dict[str, Any]] = []
        category_page_rows = 0
        for page, expected_variables in sorted(page_variables.items()):
            expected_by_key = {
                str(variable).strip().lower(): str(variable).strip()
                for variable in expected_variables
                if str(variable).strip()
            }
            matched = [
                available_questions[key]
                for key in expected_by_key
                if key in available_questions
            ]
            matched_rows = sum(item["rows"] for item in matched)
            category_page_rows += matched_rows
            if expected_by_key and matched_rows <= 0:
                zero_row_pages.append(f"{respondent_summary['category']}: {page}")
            page_reports.append(
                {
                    "page": page,
                    "expectedQuestions": len(expected_by_key),
                    "matchedQuestions": len(matched),
                    "responseRows": matched_rows,
                    "missingQuestions": sorted(
                        expected_by_key[key]
                        for key in expected_by_key
                        if key not in available_questions
                    )[:25],
                }
            )

        if page_variables and category_page_rows <= 0:
            empty_categories.append(respondent_summary["category"])
        category_reports.append(
            {
                "category": respondent_summary["category"],
                "respondents": respondent_summary["respondents"],
                "pageResponseRows": category_page_rows,
                "pages": page_reports,
            }
        )

    if latest_month_value and empty_categories:
        raise ValueError(
            "Generated DuckDB has respondents but no mapped dashboard page rows for "
            f"{latest_month_value}: {', '.join(sorted(empty_categories))}"
        )
    return {
        "latestMonth": latest_month_value or None,
        "populatedDimensionRows": dimension_counts,
        "categories": category_reports,
        "zeroRowPages": zero_row_pages,
    }


def latest_month_in_duckdb(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    con = None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        tables = {
            row[0]
            for row in con.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                """
            ).fetchall()
        }
        if "respondent_dims" not in tables:
            return None
        row = con.execute("SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims").fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception:
        return None
    finally:
        if con is not None:
            con.close()


def get_config(args: argparse.Namespace) -> dict[str, Any]:
    script_root = Path(__file__).resolve().parent
    backend_root = script_root.parent
    data_root = Path(os.environ.get("DATA_ROOT", "/var/data")).resolve()
    metadata_path = Path(os.environ.get("BHT_METADATA_PATH", str(backend_root / "data" / "Meta Data Rule.xlsx"))).resolve()
    return {
        "data_root": data_root,
        "metadata_path": metadata_path,
        "current_metadata_path": Path(
            os.environ.get(
                "BHT_CURRENT_METADATA_PATH",
                str(metadata_path),
            )
        ).resolve(),
        "datamap_path": Path(
            args.datamap_xlsx
            or os.environ.get("BHT_DATAMAP_PATH", str(resolve_default_datamap_path(backend_root)))
        ).resolve(),
        "xlsform_metadata_json_path": Path(
            os.environ.get("BHT_XLSFORM_METADATA_JSON_PATH", str(backend_root / "data" / "xlsform_metadata.json"))
        ).resolve(),
        "header_audit_metadata_path": Path(
            os.environ.get(
                "BHT_HEADER_AUDIT_METADATA_PATH",
                str(backend_root / "data" / "market_insights_header_audit_metadata.json"),
            )
        ).resolve(),
        "current_db_path": Path(os.environ.get("DUCKDB_PATH", str(data_root / "current.duckdb"))).resolve(),
        "market_db_path": Path(os.environ.get("MARKET_DB_PATH", str(data_root / "market_insights_1.duckdb"))).resolve(),
        "surveycto_server": os.environ.get("SURVEYCTO_SERVER", "").strip(),
        "surveycto_form_id": os.environ.get("SURVEYCTO_FORM_ID", "").strip(),
        "surveycto_username": os.environ.get("SURVEYCTO_USERNAME", "").strip(),
        "surveycto_password": os.environ.get("SURVEYCTO_PASSWORD", "").strip(),
        "request_timeout_seconds": int(os.environ.get("SURVEYCTO_TIMEOUT_SECONDS", "180")),
        "fetch_mode": os.environ.get("SURVEYCTO_FETCH_MODE", "full").strip().lower(),
        "allow_empty_snapshot": parse_bool(os.environ.get("SURVEYCTO_ALLOW_EMPTY_SNAPSHOT"), False),
        "backup_retention": int(os.environ.get("DUCKDB_BACKUP_RETENTION", "3")),
        "replace_months_mode": os.environ.get("SYNC_REPLACE_MONTHS", "latest").strip().lower(),
        "respondent_id_column": os.environ.get("BHT_RESPONDENT_ID_COLUMN", "KEY").strip() or "KEY",
        "fixture_json": Path(args.fixture_json).resolve() if args.fixture_json else None,
        "force_rebuild": args.force_rebuild,
        "skip_fetch": args.skip_fetch,
    }


def ensure_config_for_fetch(config: dict[str, Any]) -> None:
    missing = [
        key
        for key in ["surveycto_server", "surveycto_form_id", "surveycto_username", "surveycto_password"]
        if not config.get(key)
    ]
    if missing:
        raise ValueError(f"Missing SurveyCTO environment variables: {', '.join(missing)}")


def run_sync(config: dict[str, Any], promote: bool = False) -> dict[str, Any]:
    data_root = config["data_root"]
    raw_path = data_root / "raw" / "raw_master.parquet"
    state_path = data_root / "state" / "sync_state.json"
    result_path = data_root / "state" / "sync_result.json"
    staging_root = data_root / "staging"
    build_root = data_root / "build"
    build_root.mkdir(parents=True, exist_ok=True)

    state = read_json(state_path, {})
    last_success = state.get("last_completion_utc")
    datamap = load_datamap(config["datamap_path"])
    master_df = (
        load_raw_master_keys(raw_path)
        if config["fetch_mode"] == "full" and not config["skip_fetch"]
        else load_raw_master(raw_path)
    )
    empty_datamap_stats = {
        "renamedColumns": 0,
        "coalescedColumns": 0,
        "duplicateNewNamesUsed": 0,
        "duplicateNewNamesTotal": 0,
        "expandedMultiselectParents": 0,
        "expandedMultiselectColumns": 0,
    }
    master_datamap_stats = dict(empty_datamap_stats)
    if not master_df.empty and config["fetch_mode"] != "full":
        master_df, master_datamap_stats = apply_datamap_columns(master_df, datamap)
        master_df = normalize_raw_dates(drop_unnecessary_columns(master_df))

    surveycto_fetch: dict[str, Any] = {
        "mode": config["fetch_mode"],
        "skipped": bool(config["skip_fetch"]),
    }
    if config["fixture_json"]:
        new_df = drop_unnecessary_columns(pd.read_json(config["fixture_json"], encoding="utf-8-sig"))
        surveycto_fetch.update({
            "source": "fixture",
            "fixtureJson": str(config["fixture_json"]),
            "rows": int(len(new_df)),
            "columns": int(len(new_df.columns)),
        })
    elif config["skip_fetch"]:
        new_df = pd.DataFrame()
        surveycto_fetch.update({"source": "raw_master", "rows": 0, "columns": 0})
    else:
        ensure_config_for_fetch(config)
        if config["fetch_mode"] not in {"full", "incremental"}:
            raise ValueError("SURVEYCTO_FETCH_MODE must be either 'full' or 'incremental'.")
        since_iso = None if config["fetch_mode"] == "full" else last_success
        new_df, surveycto_fetch = fetch_surveycto_submissions(config, since_iso)
        surveycto_fetch["mode"] = config["fetch_mode"]
        surveycto_fetch["skipped"] = False

    if not new_df.empty:
        new_df, new_datamap_stats = apply_datamap_columns(new_df, datamap)
        new_df = normalize_raw_dates(new_df)
    else:
        new_datamap_stats = dict(empty_datamap_stats)

    fetched_rows = int(len(new_df))
    fetched_months = available_completion_months(new_df)
    fetched_latest_month = latest_month(new_df)
    surveycto_fetch.update({
        "rowsAfterDatamap": fetched_rows,
        "latestMonth": fetched_latest_month,
        "availableMonths": fetched_months,
    })
    reconciliation = {
        "mode": config["fetch_mode"],
        "changed": False,
        "duplicatesDropped": 0,
        "addedRows": 0,
        "deletedRows": 0,
    }
    if config["skip_fetch"]:
        combined = master_df.copy()
        has_changes = config["force_rebuild"]
    elif config["fetch_mode"] == "full":
        if new_df.empty and not master_df.empty and not config["allow_empty_snapshot"]:
            raise ValueError(
                "SurveyCTO full snapshot returned no rows; refusing to delete the local raw cache. "
                "Set SURVEYCTO_ALLOW_EMPTY_SNAPSHOT=true only when an empty form is expected."
            )
        combined, duplicates_dropped = deduplicate_submissions(new_df)
        master_keys = set(master_df["KEY"].dropna().astype(str)) if "KEY" in master_df.columns else set()
        snapshot_keys = set(combined["KEY"].dropna().astype(str)) if "KEY" in combined.columns else set()
        authoritative_stats = {
            "changed": True,
            "duplicatesDropped": duplicates_dropped,
            "addedRows": len(snapshot_keys - master_keys),
            "deletedRows": len(master_keys - snapshot_keys),
        }
        reconciliation.update(authoritative_stats)
        has_changes = True
    else:
        combined, duplicates_dropped = merge_submissions(master_df, new_df)
        reconciliation.update({
            "changed": fetched_rows > 0,
            "duplicatesDropped": duplicates_dropped,
            "addedRows": max(0, len(combined) - len(master_df)),
        })
        has_changes = fetched_rows > 0 or config["force_rebuild"]
    duplicates_dropped = int(reconciliation["duplicatesDropped"])
    if combined.empty:
        payload = {
            "ok": False,
            "changed": False,
            "completedAt": utc_now_iso(),
            "error": "No raw data available after SurveyCTO fetch/merge.",
            "fetchedRows": fetched_rows,
            "rawRows": 0,
            "latestMonth": fetched_latest_month,
            "fetchedMonths": fetched_months,
            "surveyctoFetch": surveycto_fetch,
            "reconciliation": reconciliation,
            "datamapPath": str(config["datamap_path"]),
            "datamapApplied": {
                "enabled": bool(datamap.get("new_to_old")),
                "mappedColumns": len(datamap.get("new_to_old") or {}),
                "duplicateNewNames": len(datamap.get("duplicate_new_names") or {}),
                "newData": new_datamap_stats,
                "rawMaster": master_datamap_stats,
            },
        }
        write_json(result_path, payload)
        raise SyncFailure(payload["error"], payload)

    existing_current = config["current_db_path"].exists()
    if not has_changes and existing_current:
        payload = {
            "ok": True,
            "changed": False,
            "message": "No SurveyCTO changes detected; active DuckDB left unchanged.",
            "fetchedRows": fetched_rows,
            "rawRows": int(len(combined)),
            "duplicatesDropped": duplicates_dropped,
            "reconciliation": reconciliation,
            "datamapPath": str(config["datamap_path"]),
            "datamapApplied": {
                "enabled": bool(datamap.get("new_to_old")),
                "mappedColumns": len(datamap.get("new_to_old") or {}),
                "duplicateNewNames": len(datamap.get("duplicate_new_names") or {}),
                "newData": new_datamap_stats,
                "rawMaster": master_datamap_stats,
            },
            "latestMonth": latest_month(combined),
            "fetchedMonths": fetched_months,
            "surveyctoFetch": surveycto_fetch,
            "completedAt": utc_now_iso(),
        }
        write_json(result_path, payload)
        print(json.dumps(payload, default=json_default))
        return payload

    save_raw_master(raw_path, combined)
    replace_mode = config["replace_months_mode"]
    if replace_mode not in {"latest", "generated"}:
        raise ValueError("SYNC_REPLACE_MONTHS must be either 'latest' or 'generated'.")

    rules = load_meta_rules(config["current_metadata_path"], datamap)
    dashboard_rule_supplement = supplement_rules_from_dashboard_audit(
        rules,
        config["header_audit_metadata_path"],
        datamap,
    )
    rules["configured_id_var"] = config["respondent_id_column"]
    market_latest_month = latest_month_in_duckdb(config["market_db_path"])
    rules["current_month_floor"] = market_latest_month
    surveycto_months = available_completion_months(combined)
    if market_latest_month and not any(month > market_latest_month for month in surveycto_months):
        available_text = ", ".join(surveycto_months) if surveycto_months else "(none)"
        raise ValueError(
            f"No SurveyCTO rows are newer than market DB month {market_latest_month}. "
            f"SurveyCTO CompletionDate months found: {available_text}."
        )
    xlsform_metadata = load_xlsform_metadata(config["xlsform_metadata_json_path"])
    parquet_counts = build_parquet_staging(combined, rules, staging_root, xlsform_metadata)
    temp_db_path = build_root / f"current-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}.duckdb"
    table_counts = build_duckdb_from_staging(staging_root, temp_db_path, config["current_db_path"], replace_mode)
    old_format_validation = validate_old_format_question_codes(temp_db_path, datamap)
    dashboard_coverage = inspect_generated_dashboard_coverage(
        temp_db_path,
        config["header_audit_metadata_path"],
        datamap,
    )
    promotion = (
        promote_duckdb_file(temp_db_path, config["current_db_path"], data_root, config["backup_retention"])
        if promote
        else None
    )

    latest_completion = latest_completion_iso(combined)
    state_update = {
        "last_success_at": utc_now_iso(),
        "latest_month": latest_month(combined),
        "raw_rows": int(len(combined)),
    }
    if latest_completion:
        state_update["last_completion_utc"] = latest_completion

    payload = {
        "ok": True,
        "changed": True,
        "tempDbPath": str(temp_db_path),
        "promotion": promotion,
        "fetchedRows": fetched_rows,
        "rawRows": int(len(combined)),
        "duplicatesDropped": duplicates_dropped,
        "reconciliation": reconciliation,
        "latestCompletionUtc": latest_completion,
        "latestMonth": state_update.get("latest_month"),
        "fetchedMonths": fetched_months,
        "surveyctoFetch": surveycto_fetch,
        "stateUpdate": state_update,
        "metadataPath": str(config["current_metadata_path"]),
        "marketDbPath": str(config["market_db_path"]),
        "marketLatestMonth": market_latest_month,
        "datamapPath": str(config["datamap_path"]),
        "datamapApplied": {
            "enabled": bool(datamap.get("new_to_old")),
            "mappedColumns": len(datamap.get("new_to_old") or {}),
            "duplicateNewNames": len(datamap.get("duplicate_new_names") or {}),
            "newData": new_datamap_stats,
            "rawMaster": master_datamap_stats,
        },
        "xlsformMetadataJsonPath": str(config["xlsform_metadata_json_path"]),
        "headerAuditMetadataPath": str(config["header_audit_metadata_path"]),
        "respondentIdColumn": config["respondent_id_column"],
        "replaceMonthsMode": replace_mode,
        "parquetCounts": parquet_counts,
        "tableCounts": table_counts,
        "oldFormatValidation": old_format_validation,
        "dashboardRuleSupplement": dashboard_rule_supplement,
        "dashboardCoverage": dashboard_coverage,
        "completedAt": utc_now_iso(),
    }
    if promotion:
        current_state = read_json(state_path, {})
        write_json(
            state_path,
            {
                **current_state,
                **state_update,
                "last_success_at": utc_now_iso(),
            },
        )
    write_json(result_path, payload)
    print(json.dumps(payload, default=json_default))
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch SurveyCTO BHT data and build a DuckDB replacement.")
    parser.add_argument("--fixture-json", help="Read SurveyCTO-style wide JSON from this file instead of the API.")
    parser.add_argument("--datamap-xlsx", help="Excel workbook mapping old variable names to new SurveyCTO names.")
    parser.add_argument("--skip-fetch", action="store_true", help="Rebuild from the existing raw master without fetching SurveyCTO.")
    parser.add_argument("--force-rebuild", action="store_true", help="Rebuild parquet and DuckDB even when no new rows are fetched.")
    parser.add_argument("--promote", action="store_true", help="Promote the generated DuckDB to DUCKDB_PATH before exiting.")
    return parser.parse_args()


def load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    args = parse_args()
    # Load .env from backend root (one level up from this script's directory).
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    try:
        config = get_config(args)
        run_sync(config, promote=args.promote)
        return 0
    except Exception as exc:
        data_root = Path(os.environ.get("DATA_ROOT", "/var/data")).resolve()
        payload = getattr(exc, "payload", None) or {
            "ok": False,
            "error": str(exc),
            "completedAt": utc_now_iso(),
        }
        try:
            write_json(data_root / "state" / "sync_result.json", payload)
        except Exception:
            pass
        print(json.dumps(payload), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
