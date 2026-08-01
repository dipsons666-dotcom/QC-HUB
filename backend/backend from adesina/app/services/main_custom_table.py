"""Custom crosstab tables — logic ported from Market Insights Hub (Express/DuckDB) to PostgreSQL."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from backend.app.auth import AuthUser
from backend.app.database import db_connection
from backend.app.services.main_data_scope import main_case_scope_clause
from backend.app.services.main_survey import BHT_CATEGORY_BAU5A_PREFIX, BHT_CATEGORY_PANEL_MAP, SECTION_BY_SLUG
from backend.app.settings import Settings

MAIN_SURVEY_DICTIONARY_FILE = "MAIN_data_dictionary.xlsx"
BHT_CUSTOM_TABLE_SECTION_PREFIXES = {
    "omnibus": ("OB_",),
    **{slug: (f"{prefix}_",) for slug, prefix in BHT_CATEGORY_BAU5A_PREFIX.items()},
}


def _latest_monthly_xlsform_path(root_dir: str) -> Path | None:
    xlsform_dir = Path(root_dir) / "data" / "monthly_xlsform_dictionary"
    if not xlsform_dir.exists():
        return None
    files = sorted(
        (path for path in xlsform_dir.glob("*.xlsx") if not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def _safe_str_ct(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _strip_markup(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&nbsp;", " ")
    )


def _clean_label_text(text: str) -> str:
    out = re.sub(r"\$\{[^}]+\}", "", text or "")
    out = re.sub(r"<[^>]+>", " ", out)
    out = re.sub(r"&#\d+;", "", out)
    out = _strip_markup(out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


@lru_cache(maxsize=1)
def _load_all_value_label_maps(root_dir: str) -> dict[str, dict[str, str]]:
    """Load {variable_name: {code: label}} for every row in the XLSForm dictionary."""
    result: dict[str, dict[str, str]] = {}
    path = Path(root_dir) / MAIN_SURVEY_DICTIONARY_FILE
    if path.exists():
        try:
            df = pd.read_excel(path).fillna("")
        except Exception:
            df = pd.DataFrame()
    else:
        df = pd.DataFrame()

    norm = {str(c).strip(): c for c in df.columns}
    var_col = norm.get("variable_name")
    vl_col = norm.get("value_labels")
    if not var_col or not vl_col:
        df = pd.DataFrame()

    for row in df.to_dict(orient="records"):
        var = _safe_str_ct(row.get(var_col))
        raw_labels = _safe_str_ct(row.get(vl_col))
        if not var or not raw_labels:
            continue
        label_map: dict[str, str] = {}
        for part in raw_labels.split("|"):
            cleaned = part.strip()
            if not cleaned or "=" not in cleaned:
                continue
            code, label = cleaned.split("=", 1)
            norm = code.strip()
            lbl = _clean_label_text(label.strip())
            label_map[norm] = lbl
            # Also store integer version for float keys like "1.0" → "1"
            if norm.endswith(".0"):
                label_map[norm[:-2]] = lbl
            # Also store float version for integer keys like "1" → "1.0"
            elif "." not in norm and norm.lstrip("-").isdigit():
                label_map[f"{norm}.0"] = lbl
        if label_map:
            result[var] = label_map

    xlsform_path = _latest_monthly_xlsform_path(root_dir)
    if not xlsform_path:
        return result
    try:
        survey_df = pd.read_excel(xlsform_path, sheet_name="survey").fillna("")
        choices_df = pd.read_excel(xlsform_path, sheet_name="choices").fillna("")
    except Exception:
        return result

    choices_by_list: dict[str, dict[str, str]] = {}
    if {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        for row in choices_df.to_dict(orient="records"):
            list_name = _safe_str_ct(row.get("list_name"))
            code = _safe_str_ct(row.get("name"))
            label = _clean_label_text(_safe_str_ct(row.get("label")))
            if not list_name or not code or not label:
                continue
            bucket = choices_by_list.setdefault(list_name, {})
            bucket[code] = label
            if code.endswith(".0"):
                bucket[code[:-2]] = label
            elif "." not in code and code.lstrip("-").isdigit():
                bucket[f"{code}.0"] = label

    if {"type", "name"}.issubset(set(str(col) for col in survey_df.columns)):
        for row in survey_df.to_dict(orient="records"):
            qtype = _safe_str_ct(row.get("type")).lower()
            variable = _safe_str_ct(row.get("name"))
            if not variable or not (qtype.startswith("select_one ") or qtype.startswith("select_multiple ")):
                continue
            list_name = _safe_str_ct(row.get("type")).split(None, 1)[1].strip() if " " in _safe_str_ct(row.get("type")) else ""
            labels = choices_by_list.get(list_name)
            if labels:
                result[variable] = labels
    return result


def _bht_table_section_title(slug: str) -> str:
    meta = BHT_CATEGORY_PANEL_MAP.get(slug)
    if meta:
        return str(meta.get("label") or slug)
    return "Omnibus" if slug == "omnibus" else slug.replace("-", " ").title()


def _is_custom_table_variable(variable: str, qtype: str) -> bool:
    name = variable.strip()
    lowered = name.lower()
    if not name:
        return False
    if lowered.endswith("_oth") or lowered.endswith(".oth"):
        return False
    if lowered.startswith("audio_audit") or lowered.startswith("take_pictures"):
        return False
    if any(token in lowered for token in ("gps", "latitude", "longitude", "altitude", "accuracy", "image", "photo", "picture")):
        return False
    type_lower = qtype.lower().strip()
    return type_lower.startswith("select_one ") or type_lower.startswith("select_multiple ")


def get_custom_table_section_questions(settings: Settings, user: AuthUser, slug: str) -> dict[str, Any]:
    section_slug = _safe_str_ct(slug) or "omnibus"
    prefixes = BHT_CUSTOM_TABLE_SECTION_PREFIXES.get(section_slug)
    if not prefixes:
        raise ValueError(f"Unsupported custom table section: {slug}")

    xlsform_path = _latest_monthly_xlsform_path(str(settings.root_dir))
    if not xlsform_path:
        raise ValueError("Monthly XLSForm dictionary is not available.")

    survey_df = pd.read_excel(xlsform_path, sheet_name="survey").fillna("")
    choices_df = pd.read_excel(xlsform_path, sheet_name="choices").fillna("")

    choices_by_list: dict[str, list[dict[str, str]]] = {}
    if {"list_name", "name", "label"}.issubset(set(str(col) for col in choices_df.columns)):
        for row in choices_df.to_dict(orient="records"):
            list_name = _safe_str_ct(row.get("list_name"))
            code = _safe_str_ct(row.get("name"))
            label = _clean_label_text(_safe_str_ct(row.get("label")))
            if not list_name or not code or not label:
                continue
            normalized_code = code[:-2] if code.endswith(".0") else code
            choices_by_list.setdefault(list_name, []).append({"code": normalized_code, "label": label})

    question_cards: list[dict[str, Any]] = []
    if {"type", "name", "label"}.issubset(set(str(col) for col in survey_df.columns)):
        for row in survey_df.to_dict(orient="records"):
            qtype = _safe_str_ct(row.get("type"))
            variable = _safe_str_ct(row.get("name"))
            if not _is_custom_table_variable(variable, qtype):
                continue
            if not any(variable.upper().startswith(prefix.upper()) for prefix in prefixes):
                continue
            label = _clean_label_text(_safe_str_ct(row.get("label"))) or variable
            label = re.sub(rf"^{re.escape(variable)}\.?\s*", "", label, flags=re.IGNORECASE).strip() or label
            list_name = qtype.split(None, 1)[1].strip() if " " in qtype else ""
            choices = choices_by_list.get(list_name, [])
            is_multi = qtype.lower().startswith("select_multiple ")
            table_rows = [
                {
                    "code": f"{variable}_{choice['code']}" if is_multi else choice["code"],
                    "label": choice["label"],
                    "count": 0,
                    "percent": 0,
                }
                for choice in choices
                if choice.get("code")
            ]
            question_cards.append(
                {
                    "variable": variable,
                    "label": label,
                    "storageType": "numeric",
                    "measure": "nominal",
                    "valueLabels": " | ".join(f"{choice['code']}={choice['label']}" for choice in choices),
                    "source": "monthly_xlsform",
                    "isMultiSelect": is_multi,
                    "responseCount": 0,
                    "distinctResponseCount": len(table_rows),
                    "note": "",
                    "tableRows": table_rows,
                    "chartData": [],
                }
            )

    return {
        "section": {"slug": section_slug, "title": _bht_table_section_title(section_slug)},
        "questionCards": question_cards,
        "workbookPath": str(xlsform_path),
    }


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _meaningful(value: Any) -> bool:
    t = _safe_str(value).lower()
    return bool(t) and t not in {"nan", "none", "", "nat"}


def _record_value(record: dict[str, Any], code: str) -> Any:
    """Read variable values with tolerant key matching (exact, case-insensitive)."""
    if code in record:
        return record.get(code)

    lowered = code.lower()
    for key, value in record.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def _apply_label(code: str, label_map: dict[str, str] | None) -> str:
    """Map a raw code to its value label, handling int/float variants."""
    if not label_map:
        return code
    if code in label_map:
        return label_map[code]
    # Stored as float "1.0" but dict has int key "1"
    try:
        as_int = str(int(float(code)))
        if as_int in label_map:
            return label_map[as_int]
        as_float = f"{as_int}.0"
        if as_float in label_map:
            return label_map[as_float]
    except (ValueError, OverflowError):
        pass
    return code


def _apply_label_with_token_support(raw_value: str, label_map: dict[str, str] | None) -> str:
    """
    Apply value labels to either a single code ("3") or a multi-code string
    ("1 2 3", "1,2,3"). Multi-code values are mapped token-by-token.
    """
    if not label_map:
        return raw_value

    normalized = _safe_str(raw_value)
    if not normalized:
        return normalized

    # Direct match first (important for non-multi values like "1.0")
    direct = _apply_label(normalized, label_map)
    if direct != normalized:
        return direct

    # Tokenized match for select_multiple-style values stored in one variable.
    if re.search(r"[\s,;|]+", normalized):
        tokens = [tok for tok in re.split(r"[\s,;|]+", normalized) if tok]
        if len(tokens) > 1:
            mapped = [_apply_label(tok, label_map) for tok in tokens]
            return " + ".join(mapped)

    return normalized


def _map_labeled_tokens(raw_value: str, label_map: dict[str, str] | None) -> list[str]:
    """
    Return token-level value labels for a raw value.
    - Single-coded values return one-item list.
    - Multi-coded strings like "1 2 3" return one label per token.
    """
    normalized = _safe_str(raw_value)
    if not normalized:
        return []
    if label_map and re.search(r"[\s,;|]+", normalized):
        tokens = [tok for tok in re.split(r"[\s,;|]+", normalized) if tok]
        if len(tokens) > 1:
            return [_apply_label(tok, label_map) for tok in tokens]
    return [_apply_label(normalized, label_map)]


def _format_significance_column_letter(index: int) -> str:
    value = int(index)
    if value < 0:
        return ""
    letter = ""
    while value >= 0:
        letter = chr(65 + (value % 26)) + letter
        value = value // 26 - 1
    return letter


def _z_score(count_a: float, base_a: float, count_b: float, base_b: float) -> float | None:
    if base_a <= 0 or base_b <= 0:
        return None
    pooled = (count_a + count_b) / (base_a + base_b)
    variance = pooled * (1 - pooled) * ((1 / base_a) + (1 / base_b))
    if variance <= 0:
        return None
    return ((count_a / base_a) - (count_b / base_b)) / math.sqrt(variance)


def _compute_chi_square_summary(matrix: list[list[float]]) -> dict[str, Any] | None:
    if len(matrix) < 2 or not matrix[0] or len(matrix[0]) < 2:
        return None
    row_sums = [sum(float(x or 0) for x in row) for row in matrix]
    col_count = len(matrix[0])
    col_sums = [sum(float(matrix[r][c] or 0) for r in range(len(matrix))) for c in range(col_count)]
    total = sum(row_sums)
    if total <= 0:
        return None
    statistic = 0.0
    for ri, row in enumerate(matrix):
        for ci in range(col_count):
            expected = (row_sums[ri] * col_sums[ci]) / total
            if expected > 0:
                statistic += ((float(row[ci] or 0) - expected) ** 2) / expected
    return {
        "statistic": round(statistic, 3),
        "degreesOfFreedom": max(1, (len(matrix) - 1) * (col_count - 1)),
    }


def _significance_letters(
    counts: list[list[float]], column_bases: list[float]
) -> tuple[list[str], list[list[str]], int]:
    column_letters = [_format_significance_column_letter(i) for i in range(len(column_bases))]
    sig: list[list[str]] = [["" for _ in column_bases] for _ in counts]
    comparable = 0
    for ri, row in enumerate(counts):
        row_sets = [set() for _ in column_bases]
        for li in range(len(column_bases)):
            for rii in range(li + 1, len(column_bases)):
                z = _z_score(row[li], column_bases[li], row[rii], column_bases[rii])
                if z is None:
                    continue
                comparable += 1
                if abs(z) < 1.959963984540054:
                    continue
                left_pct = row[li] / column_bases[li] if column_bases[li] else 0
                right_pct = row[rii] / column_bases[rii] if column_bases[rii] else 0
                if left_pct == right_pct:
                    continue
                if left_pct > right_pct:
                    row_sets[li].add(column_letters[rii])
                else:
                    row_sets[rii].add(column_letters[li])
        sig[ri] = ["".join(sorted(s)) for s in row_sets]
    return column_letters, sig, comparable


def _derive_grouped_value(
    record: dict[str, Any],
    question_codes: list[str],
    label_maps: dict[str, dict[str, str]] | None = None,
) -> str:
    parts: list[str] = []
    for code in question_codes:
        raw = _record_value(record, code)
        if _meaningful(raw):
            raw_str = _safe_str(raw)
            lmap = (label_maps or {}).get(code)
            parts.append(_apply_label_with_token_support(raw_str, lmap))
    if not parts:
        return ""
    if len(question_codes) > 1:
        return " / ".join(parts)
    return parts[0]


@dataclass
class QuestionSpecIn:
    id: str
    label: str
    sectionId: str = ""
    sectionTitle: str = ""
    questionCodes: list[str] = field(default_factory=list)
    codeLabels: dict[str, str] = field(default_factory=dict)


@dataclass
class CustomTableRequest:
    slug: str
    topQuestions: list[QuestionSpecIn] = field(default_factory=list)
    sideQuestions: list[QuestionSpecIn] = field(default_factory=list)
    displayMode: str = "row_pct"
    analysisOptions: list[str] = field(default_factory=list)
    formatOptions: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, list[str]] = field(default_factory=dict)
    months: list[str] = field(default_factory=list)


@dataclass
class QuestionData:
    value_map: dict[str, set[str]]  # case_id -> values
    value_order: list[str]
    is_multi_valued: bool
    base_respondents: set[str] = field(default_factory=set)


def _approval_filter(user: AuthUser) -> tuple[str, list[Any]]:
    return "TRUE", []


def _requested_question_codes(specs: list["QuestionSpecIn"]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for spec in specs:
        for code in spec.questionCodes:
            key = _safe_str(code)
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            ordered.append(key)
    return ordered


def _requested_section_names(specs: list["QuestionSpecIn"]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for spec in specs:
        slug = _safe_str(spec.sectionId)
        section_name = _safe_str(SECTION_BY_SLUG.get(slug, {}).get("section"))
        if not section_name or section_name in seen:
            continue
        seen.add(section_name)
        ordered.append(section_name)
    return ordered


def _extract_requested_record_values(
    record: dict[str, Any],
    requested_codes: list[str],
) -> dict[str, Any]:
    if not record or not requested_codes:
        return {}

    requested_by_lower = {code.lower(): code for code in requested_codes}
    extracted: dict[str, Any] = {}
    for key, value in record.items():
        if not isinstance(key, str):
            continue
        target = requested_by_lower.get(key.lower())
        if target is not None:
            extracted[target] = value
    return extracted


def _filter_sql(filters: dict[str, list[str]]) -> tuple[str, list[Any]]:
    parts: list[str] = []
    params: list[Any] = []
    states = filters.get("states") or []
    genders = filters.get("genders") or []
    age_groups = filters.get("ageGroups") or filters.get("age_groups") or []
    sec_classes = filters.get("secClasses") or filters.get("sec_classes") or []
    months = filters.get("months") or []
    approval_stages = filters.get("approval_stage") or filters.get("approvalStage") or []
    final_outcomes = filters.get("final_outcome_code") or filters.get("finalOutcomeCodes") or filters.get("final_outcome_codes") or []
    categories = filters.get("categories") or filters.get("category") or []

    if states:
        parts.append("d.state_name = ANY(%s)")
        params.append(states)
    if genders:
        parts.append("d.gender = ANY(%s)")
        params.append(genders)
    if age_groups:
        parts.append("d.age_group = ANY(%s)")
        params.append(age_groups)
    if sec_classes:
        parts.append("d.sec_class = ANY(%s)")
        params.append(sec_classes)
    if months:
        parts.append("d.interview_month = ANY(%s)")
        params.append(months)
    if approval_stages:
        parts.append("m.approval_stage = ANY(%s)")
        params.append(approval_stages)
    if final_outcomes:
        parts.append("TRIM(COALESCE(m.record->>'final_outcome_code', '')) = ANY(%s)")
        params.append(final_outcomes)
    category_slugs = [str(item).strip() for item in categories if str(item).strip() and str(item).strip() != "omnibus"]
    panel_codes = [
        str(BHT_CATEGORY_PANEL_MAP[slug]["panelCode"])
        for slug in category_slugs
        if slug in BHT_CATEGORY_PANEL_MAP and BHT_CATEGORY_PANEL_MAP[slug].get("panelCode")
    ]
    if panel_codes:
        parts.append(
            """
            EXISTS (
                SELECT 1
                FROM clean.main_case_panel ctf_panel
                WHERE ctf_panel.case_id = m.case_id
                  AND ctf_panel.panel_code = ANY(%s)
                  AND COALESCE(ctf_panel.is_selected, TRUE)
            )
            """
        )
        params.append(panel_codes)

    if not parts:
        return "TRUE", []
    return " AND ".join(parts), params


def _load_case_records(
    settings: Settings,
    user: AuthUser,
    filters: dict[str, list[str]],
    requested_codes: list[str],
    requested_section_names: list[str],
) -> list[dict[str, Any]]:
    appr, appr_p = _approval_filter(user)
    filt_sql, filt_p = _filter_sql(filters)
    date_scope_sql, date_scope_params = main_case_scope_clause(settings, "m")
    sql = f"""
        SELECT
            m.case_id,
            COALESCE(m.record, '{{}}'::jsonb) AS record
        FROM clean.main_case m
        LEFT JOIN mart.main_case_dim d ON d.case_id = m.case_id
        WHERE {appr} AND {filt_sql}
          AND NOT EXISTS (
              SELECT 1
              FROM clean.deleted_main_cases dmc
              WHERE dmc.submission_key = m.submission_key
          )
          {date_scope_sql}
    """
    params = list(appr_p) + list(filt_p) + list(date_scope_params)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            case_rows = cur.fetchall()

            case_records: dict[str, dict[str, Any]] = {}
            for row in case_rows:
                cid = str(row["case_id"])
                rec = row.get("record") or {}
                if isinstance(rec, str):
                    try:
                        rec = json.loads(rec)
                    except json.JSONDecodeError:
                        rec = {}
                case_records[cid] = _extract_requested_record_values(rec, requested_codes)

            if case_records and requested_section_names:
                cur.execute(
                    """
                    SELECT case_id, record
                    FROM clean.main_case_section
                    WHERE case_id = ANY(%s)
                      AND section_name = ANY(%s)
                    """,
                    (list(case_records.keys()), requested_section_names),
                )
                for row in cur.fetchall():
                    cid = str(row["case_id"])
                    rec = row.get("record") or {}
                    if isinstance(rec, str):
                        try:
                            rec = json.loads(rec)
                        except json.JSONDecodeError:
                            rec = {}
                    case_records.setdefault(cid, {}).update(_extract_requested_record_values(rec, requested_codes))

    out: list[dict[str, Any]] = []
    for cid, rec in case_records.items():
        out.append({"case_id": cid, "record": rec})
    return out


def _fetch_question_data(
    rows: list[dict[str, Any]],
    spec: QuestionSpecIn,
    label_maps: dict[str, dict[str, str]] | None = None,
) -> QuestionData:
    codes = [c for c in spec.questionCodes if c]
    if not codes:
        return QuestionData({}, [], False, set())
    if spec.codeLabels:
        return _fetch_multiselect_data(rows, codes, spec.codeLabels)

    value_map: dict[str, set[str]] = {}
    value_order: list[str] = []
    seen: set[str] = set()
    max_per = 0
    base_respondents: set[str] = set()

    for row in rows:
        cid = str(row["case_id"])
        record = row["record"]
        values: list[str] = []
        if len(codes) == 1:
            code = codes[0]
            raw = _safe_str(_record_value(record, code))
            if not _meaningful(raw):
                continue

            def _clean_val(v: str) -> str:
                try:
                    f = float(v)
                    return str(int(f)) if f == int(f) else v
                except (ValueError, TypeError):
                    return v

            lmap = (label_maps or {}).get(code)
            cleaned_raw = _clean_val(raw)
            token_values = [_clean_label_text(token) for token in _map_labeled_tokens(cleaned_raw, lmap)]
            values = [value for value in token_values if _meaningful(value)]
            if not values:
                continue
            base_respondents.add(cid)
        else:
            grouped_value = _derive_grouped_value(record, codes, label_maps)
            if not _meaningful(grouped_value):
                continue
            values = [grouped_value]
            base_respondents.add(cid)

        if cid not in value_map:
            value_map[cid] = set()
        for value in values:
            value_map[cid].add(value)
            if value not in seen:
                seen.add(value)
                value_order.append(value)
        max_per = max(max_per, len(value_map[cid]))

    return QuestionData(value_map, value_order, max_per > 1, base_respondents)


def _fetch_multiselect_data(
    rows: list[dict[str, Any]],
    codes: list[str],
    code_labels: dict[str, str],
) -> QuestionData:
    value_map: dict[str, set[str]] = {}
    value_order = [_clean_label_text(code_labels.get(code, code)) or code for code in codes]
    selected_tokens = {"1", "1.0", "true", "yes"}
    unselected_tokens = {"0", "0.0", "false", "no"}
    base_respondents: set[str] = set()

    for row in rows:
        cid = str(row["case_id"])
        record = row["record"]
        selected: set[str] = set()
        answered_any = False
        for code, label in zip(codes, value_order):
            raw = _safe_str(_record_value(record, code)).lower()
            if raw in selected_tokens:
                selected.add(label)
                answered_any = True
            elif raw in unselected_tokens:
                answered_any = True
        if answered_any:
            base_respondents.add(cid)
        if selected:
            value_map[cid] = selected

    max_per = max((len(vals) for vals in value_map.values()), default=0)
    return QuestionData(value_map, value_order, max_per > 1, base_respondents)


def _build_value_respondent_map(question_data: QuestionData) -> dict[str, set[str]]:
    counts: dict[str, set[str]] = {}
    for cid, vals in question_data.value_map.items():
        for v in vals:
            counts.setdefault(v, set()).add(cid)
    return counts


def _ordered_labels(order: list[str], count_map: dict[str, set[str]]) -> list[str]:
    out = [v for v in order if v in count_map]
    for v in count_map:
        if v not in out:
            out.append(v)
    return out


def _build_column_block(
    top_spec: QuestionSpecIn,
    top_data: QuestionData,
    side_data: QuestionData,
    row_labels: list[str],
    analysis_options: list[str],
) -> dict[str, Any]:
    # Bases for top-break columns must be calculated from respondents who answered
    # both the side question and the top-break question. This keeps the N row aligned
    # with the actual table population instead of counting unrelated top-break answers.
    pair_column_respondents: dict[str, set[str]] = {}
    matrix_map: dict[str, dict[str, float]] = {}
    pair_respondents: set[str] = set()

    for cid, top_vals in top_data.value_map.items():
        side_vals = side_data.value_map.get(cid)
        if not side_vals or not top_vals:
            continue
        pair_respondents.add(cid)
        for cl in top_vals:
            pair_column_respondents.setdefault(cl, set()).add(cid)
        for rl in side_vals:
            row_map = matrix_map.setdefault(rl, {})
            for cl in top_vals:
                row_map[cl] = row_map.get(cl, 0) + 1

    column_labels = _ordered_labels(top_data.value_order, pair_column_respondents)
    counts = [[float(matrix_map.get(rl, {}).get(cl, 0)) for cl in column_labels] for rl in row_labels]
    column_bases = [float(len(pair_column_respondents.get(cl, set()))) for cl in column_labels]
    notes: list[str] = []
    column_letter_labels: list[str] = []
    significance_letters = [["" for _ in column_labels] for _ in row_labels]

    chi_square = None
    if "chi_square" in analysis_options:
        if top_data.is_multi_valued or side_data.is_multi_valued:
            notes.append("Chi-square summary is unavailable for multi-response question combinations.")
        else:
            chi_square = _compute_chi_square_summary(counts)
            if not chi_square:
                notes.append("Chi-square summary requires at least two populated row and column categories.")

    if "significance" in analysis_options:
        if top_data.is_multi_valued:
            notes.append("Significance letters are unavailable when Top Break columns come from a multi-response question.")
        elif len(column_labels) < 2:
            notes.append("Significance letters require at least two populated Top Break columns.")
        else:
            column_letter_labels, significance_letters, comparable = _significance_letters(counts, column_bases)
            if comparable == 0:
                notes.append("Significance letters require non-zero bases in at least two Top Break columns.")

    return {
        "id": top_spec.id,
        "topQuestion": {
            "id": top_spec.id,
            "label": top_spec.label,
            "sectionId": top_spec.sectionId,
            "sectionTitle": top_spec.sectionTitle,
            "questionCodes": top_spec.questionCodes,
        },
        "columnLabels": column_labels,
        "columnLetterLabels": column_letter_labels,
        "columnBases": column_bases,
        "counts": counts,
        "significanceLetters": significance_letters,
        "pairRespondents": len(pair_respondents),
        "chiSquare": chi_square,
        "notes": notes,
    }


def _spec_key(spec: QuestionSpecIn) -> str:
    codes = sorted({c for c in spec.questionCodes if c})
    return f"{spec.sectionId}::{'|'.join(codes)}::{spec.label}"


def run_custom_table(settings: Settings, user: AuthUser, req: CustomTableRequest) -> dict[str, Any]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not configured.")

    filters = dict(req.filters or {})
    if req.months:
        filters.setdefault("months", list(req.months))

    top_specs = [t for t in req.topQuestions if t.questionCodes]
    side_specs = [s for s in req.sideQuestions if s.questionCodes]
    if not top_specs or not side_specs:
        raise ValueError("topQuestions and sideQuestions are required")

    requested_specs = top_specs + side_specs
    requested_codes = _requested_question_codes(requested_specs)
    requested_section_names = _requested_section_names(requested_specs)

    rows = _load_case_records(settings, user, filters, requested_codes, requested_section_names)
    total_respondents = len({r["case_id"] for r in rows})

    # Load value label maps from XLSForm dictionary (cached per root_dir)
    label_maps = _load_all_value_label_maps(str(settings.root_dir))

    cache: dict[str, QuestionData] = {}
    for spec in top_specs + side_specs:
        cache[_spec_key(spec)] = _fetch_question_data(rows, spec, label_maps)

    analysis = [x for x in req.analysisOptions if x in ("significance", "chi_square")]

    tables: list[dict[str, Any]] = []
    for side_spec in side_specs:
        side_data = cache[_spec_key(side_spec)]
        row_count_by_value = _build_value_respondent_map(side_data)
        row_labels = _ordered_labels(side_data.value_order, row_count_by_value)
        if side_data.is_multi_valued:
            common_row_base = float(len(side_data.base_respondents))
            row_bases = [common_row_base for _ in row_labels]
        else:
            row_bases = [float(len(row_count_by_value.get(rl, set()))) for rl in row_labels]

        top_blocks: list[dict[str, Any]] = []
        for top_spec in top_specs:
            top_data = cache[_spec_key(top_spec)]
            if not top_data.value_map:
                top_blocks.append(
                    {
                        "id": top_spec.id,
                        "topQuestion": {
                            "id": top_spec.id,
                            "label": top_spec.label,
                            "sectionId": top_spec.sectionId,
                            "sectionTitle": top_spec.sectionTitle,
                            "questionCodes": top_spec.questionCodes,
                        },
                        "columnLabels": [],
                        "columnLetterLabels": [],
                        "columnBases": [],
                        "counts": [[] for _ in row_labels],
                        "significanceLetters": [[""] for _ in row_labels],
                        "pairRespondents": 0,
                        "chiSquare": None,
                        "notes": ["No data found for this top break question."],
                    }
                )
                continue
            top_blocks.append(_build_column_block(top_spec, top_data, side_data, row_labels, analysis))

        if len(top_blocks) == 1 and top_blocks[0].get("columnBases"):
            # Keep TOTAL N aligned with the visible top-break N row.
            # For normal single-response top breaks, the column bases partition the
            # table population and should sum exactly to TOTAL N.
            table_total_respondents = int(sum(float(x or 0) for x in top_blocks[0]["columnBases"]))
        else:
            table_total_respondents = len(side_data.base_respondents) if side_data.is_multi_valued else total_respondents
        tables.append(
            {
                "id": side_spec.id,
                "sideQuestion": {
                    "id": side_spec.id,
                    "label": side_spec.label,
                    "sectionId": side_spec.sectionId,
                    "sectionTitle": side_spec.sectionTitle,
                    "questionCodes": side_spec.questionCodes,
                },
                "rowLabels": row_labels,
                "rowBases": row_bases,
                "rowCounts": [float(len(row_count_by_value.get(rl, set()))) for rl in row_labels],
                "totalRespondents": table_total_respondents,
                "topBlocks": top_blocks,
            }
        )

    return {
        "category": req.slug,
        "displayMode": req.displayMode or "row_pct",
        "analysisOptions": analysis,
        "formatOptions": req.formatOptions or {},
        "totalRespondents": total_respondents,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tables": tables,
    }
