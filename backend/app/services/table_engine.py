"""Reusable survey-table calculations used by both the API and Excel exports."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence


NOODLES_TABLES = [
    ("N_BAU1A", "Top-of-mind brand awareness"),
    ("N_BAU1B", "Other spontaneous brand awareness"),
    ("N_BAU1C", "Top-of-mind advertising awareness"),
    ("N_BAU1D", "Other advertising awareness"),
    ("N_BAU2", "Prompted brand awareness"),
    ("N_BAU5A", "Ever consumed"),
    ("N_BAU5C", "Consumed in the last 3 months"),
    ("N_BAU6A", "Consumed in the last month"),
    ("N_BAU6B", "Consumed in the last 7 days"),
    ("N_BAU6C", "Main brand"),
    ("N_BAU8", "Preferred brand"),
    ("N_BAU9", "Purchase frequency"),
    ("N_BAU10", "Pack size purchased most often"),
]

PREFERRED_FILTER_FIELDS = [
    "starttime", "endtime", "today", "deviceid", "devicephonenum", "username", "simid",
    "caseid", "device_info", "duration", "City_1", "Sector", "Interviewer", "CS", "Q1a",
    "Age", "Age_Range", "Marital_Status", "Gender", "Sec_quest",
    "d3_q", "SEC", "Week",
]


def load_metadata(metadata_path: Path) -> dict[str, Any]:
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def load_questionnaire_xlsform(workbook_path: Path) -> dict[str, Any]:
    """Read SurveyCTO's XLSForm directly into the decoder shape used by this service."""
    from openpyxl import load_workbook

    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    if "survey" not in workbook.sheetnames or "choices" not in workbook.sheetnames:
        raise ValueError("Questionnaire workbook must contain survey and choices sheets")
    questions: dict[str, dict[str, str]] = {}
    survey_rows = workbook["survey"].iter_rows(values_only=True)
    survey_headers = [str(value or "").strip() for value in next(survey_rows)]
    survey_columns = {name: index for index, name in enumerate(survey_headers)}
    for row in survey_rows:
        name = str(row[survey_columns["name"]] or "").strip() if "name" in survey_columns else ""
        question_type = str(row[survey_columns["type"]] or "").strip() if "type" in survey_columns else ""
        if name and question_type:
            questions[name] = {
                "name": name,
                "type": question_type,
                "label": str(row[survey_columns["label"]] or "").strip() if "label" in survey_columns else name,
                "relevance": str(row[survey_columns["relevance"]] or "").strip() if "relevance" in survey_columns else "",
            }

    lists: dict[str, dict[str, str]] = {}
    choice_rows = workbook["choices"].iter_rows(values_only=True)
    choice_headers = [str(value or "").strip() for value in next(choice_rows)]
    choice_columns = {name: index for index, name in enumerate(choice_headers)}
    for row in choice_rows:
        list_name = str(row[choice_columns["list_name"]] or "").strip() if "list_name" in choice_columns else ""
        name = str(row[choice_columns["name"]] or "").strip() if "name" in choice_columns else ""
        if list_name and name:
            lists.setdefault(list_name, {})[name] = str(row[choice_columns["label"]] or name).strip()
    return {"questions": questions, "lists": lists}


def load_template_registry(template_path: Path, metadata: dict[str, Any], category: str) -> list[tuple[str, str]]:
    """Extract the question sequence for a category from the report template's BackEnd sheet."""
    from openpyxl import load_workbook

    workbook = load_workbook(template_path, read_only=True, data_only=False)
    if "BackEnd" not in workbook.sheetnames:
        return table_registry(category)
    sheet = workbook["BackEnd"]
    category_column = None
    for row in sheet.iter_rows():
        for cell in row:
            if str(cell.value or "").strip().lower() == category.lower():
                category_column = cell.column
                break
        if category_column is not None:
            break
    if category_column is None:
        return table_registry(category)

    questions = metadata.get("questions", {})
    question_names_by_lower = {str(name).lower(): str(name) for name in questions}
    registry: list[tuple[str, str]] = []
    seen: set[str] = set()
    qbi_index = 0
    for row in sheet.iter_rows(min_col=category_column + 1, max_col=category_column + 1):
        text = str(row[0].value or "")
        match = re.match(r"\s*([A-Za-z][A-Za-z0-9_]*(?:\.\d+)?)", text)
        question_name = question_names_by_lower.get(match.group(1).lower(), "") if match else ""
        # The report's BackEnd tab has an older QFS spelling while the July
        # XLSForm and visible Noodles sheet use QFH.
        if not question_name and match and match.group(1).lower().startswith("n_qfs"):
            question_name = question_names_by_lower.get(("N_QFH" + match.group(1)[5:]).lower(), "")
        # The historic template uses a shared question stem for matrix / routed
        # items. Resolve those entries against the XLSForm rather than silently
        # dropping them from the report.
        if not question_name and match:
            stem = match.group(1)
            candidates = [
                name for name, question in questions.items()
                if str(name).lower().startswith(stem.lower() + ".") and str(question.get("type", "")).startswith("select_")
            ]
            if stem.lower() == "n_qbi":
                qbi_index += 1
                expected = f"N_QBI.{qbi_index}"
                question_name = question_names_by_lower.get(expected.lower(), "")
            elif stem.lower() == "n_bau4":
                lowered = text.lower()
                brand = re.search(r"about\s+(.+?)\s*$", text, flags=re.IGNORECASE)
                brand_name = brand.group(1).strip().lower() if brand else ""
                question_name = next((name for name in candidates if brand_name and brand_name in str(questions[name].get("label", "")).lower()), "")
        if question_name in questions and question_name not in seen:
            registry.append((question_name, text.strip()))
            seen.add(question_name)
    return registry or table_registry(category)


def _question(metadata: dict[str, Any], name: str) -> dict[str, Any]:
    return metadata.get("questions", {}).get(name, {})


def _options(metadata: dict[str, Any], name: str) -> dict[str, str]:
    question = _question(metadata, name)
    question_type = str(question.get("type", ""))
    list_name = question_type.replace("select_one", "", 1).replace("select_multiple", "", 1).strip()
    raw_options = metadata.get("lists", {}).get(list_name, {})
    return {str(key): str(label) for key, label in raw_options.items()}


def _choice_label(options: dict[str, str], value: Any) -> str:
    token = str(value).strip()
    if not token:
        return ""
    return options.get(token) or options.get(f"{token}.0") or token


def _answer_tokens(value: Any, question_type: str) -> list[Any]:
    if value in (None, ""):
        return []
    if str(question_type).startswith("select_multiple"):
        return value if isinstance(value, list) else str(value).split()
    return [value]


def _age_band(value: Any) -> str:
    """Match the three age bands used in the supplied full-data template."""
    try:
        age = int(float(value))
    except (TypeError, ValueError):
        return ""
    if 18 <= age <= 25:
        return "18 - 25 years"
    if 26 <= age <= 35:
        return "26 - 35 years"
    if 36 <= age <= 45:
        return "36 - 45 years"
    return ""


def _answers(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    nested = payload.get("answers")
    return nested if isinstance(nested, dict) else payload


def _field_label(field_name: str, metadata: dict[str, Any]) -> str:
    question = _question(metadata, field_name)
    return str(question.get("label") or field_name) if question else field_name.replace("_", " ")


def available_filter_fields(payloads: Sequence[dict[str, Any]], metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Return configured standard filters that are present in the submissions.

    Raw SurveyCTO payloads also contain thousands of dynamically generated keys;
    exposing every one would make the analyst selector unusable. Add new standard
    filter variables to PREFERRED_FILTER_FIELDS when a questionnaire requires it.
    """
    fields_by_lower: dict[str, str] = {}
    for payload in payloads:
        for key, value in _answers(payload).items():
            if value not in (None, ""):
                fields_by_lower[str(key).lower()] = str(key)
    preferred_by_lower = {field.lower(): field for field in PREFERRED_FILTER_FIELDS}
    ordered = [
        fields_by_lower[field.lower()]
        for field in PREFERRED_FILTER_FIELDS
        if field.lower() in fields_by_lower
    ]
    return [{"field": field, "label": _field_label(field, metadata)} for field in ordered]


def _resolve_field_name(field_name: str, fields: Sequence[str]) -> str:
    return next((field for field in fields if field.lower() == field_name.lower()), field_name)


def table_registry(category: str) -> list[tuple[str, str]]:
    if category.lower() != "noodles":
        return []
    return NOODLES_TABLES


def build_tables(
    payloads: Sequence[dict[str, Any]],
    metadata: dict[str, Any],
    category: str = "noodles",
    registry: Sequence[tuple[str, str]] | None = None,
    cut_fields: Sequence[str] | None = None,
    max_cut_groups: int = 50,
) -> list[dict[str, Any]]:
    """Calculate count and percentage tables for every configured question."""
    answer_rows = [_answers(payload) for payload in payloads]
    tables: list[dict[str, Any]] = []
    for question_name, title in registry or table_registry(category):
        question_name = next(
            (name for name in metadata.get("questions", {}) if str(name).lower() == question_name.lower()),
            question_name,
        )
        question = _question(metadata, question_name)
        if not question:
            continue
        question_type = str(question.get("type", ""))
        options = _options(metadata, question_name)
        valid_rows = [row for row in answer_rows if row.get(question_name) not in (None, "")]
        total_base = len(valid_rows)
        counts: Counter[str] = Counter()
        for row in valid_rows:
            counts.update(_choice_label(options, token) for token in _answer_tokens(row[question_name], question_type))

        cuts = []
        available_fields = list({key for row in answer_rows for key in row})
        requested_cut_fields = cut_fields or [
            field for field in PREFERRED_FILTER_FIELDS
            if any(available.lower() == field.lower() for available in available_fields)
        ]
        for requested_cut_name in requested_cut_fields:
            cut_name = _resolve_field_name(requested_cut_name, available_fields)
            if cut_name not in available_fields:
                continue
            cut_label = _field_label(cut_name, metadata)
            cut_options = _options(metadata, cut_name)
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in valid_rows:
                if row.get(cut_name) in (None, ""):
                    continue
                cut_type = str(_question(metadata, cut_name).get("type", ""))
                for token in _answer_tokens(row[cut_name], cut_type):
                    label = _choice_label(cut_options, token)
                    if label:
                        groups.setdefault(label, []).append(row)
            group_items = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0].lower()))
            cuts.append(
                {
                    "field": cut_name,
                    "label": cut_label,
                    "groups": [
                        {
                            "label": group_label,
                            "base": len(group_rows),
                            "counts": dict(Counter(
                                _choice_label(options, token)
                                for row in group_rows
                                for token in _answer_tokens(row[question_name], question_type)
                            )),
                        }
                        for group_label, group_rows in group_items[:max_cut_groups]
                    ],
                    "total_groups": len(group_items),
                    "truncated": len(group_items) > max_cut_groups,
                }
            )
        rows = [
            {"label": label, "count": count, "pct": round((count / total_base) * 100, 1) if total_base else 0}
            for label, count in counts.most_common()
            if label
        ]
        tables.append({
            "id": question_name,
            "title": title,
            "question": str(question.get("label") or question_name),
            "base": total_base,
            "rows": rows,
            "cuts": cuts,
        })
    return tables
