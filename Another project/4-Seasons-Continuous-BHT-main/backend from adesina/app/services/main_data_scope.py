from __future__ import annotations

import re
from typing import Any

from backend.app.settings import Settings


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def main_data_start_date(settings: Settings) -> str | None:
    raw = str(settings.main_survey_data_start_date or "").strip()
    if not raw:
        return None
    if not _DATE_RE.match(raw):
        raise ValueError("MAIN_SURVEY_DATA_START_DATE must use YYYY-MM-DD format.")
    return raw


def main_data_form_id(settings: Settings) -> str | None:
    raw = str(settings.surveycto_main_form_id or "").strip()
    return raw or None


def main_case_effective_datetime_sql(alias: str) -> str:
    def value_expr(field: str) -> str:
        return f"NULLIF(TRIM({alias}.record->>'{field}'), '')"

    candidates = [value_expr("starttime")]
    parsed = []
    for expr in candidates:
        parsed.extend(
            [
                f"CASE WHEN {expr} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN {expr}::timestamptz ELSE NULL END",
                f"CASE WHEN {expr} ~ '^[A-Za-z]{{3}}\\s+\\d{{1,2}},\\s+\\d{{4}}\\s+\\d{{1,2}}:\\d{{2}}:\\d{{2}}\\s+(AM|PM)$' THEN to_timestamp({expr}, 'Mon DD, YYYY HH12:MI:SS AM') ELSE NULL END",
            ]
        )
    return "COALESCE(" + ", ".join(parsed) + ")"


def main_row_effective_datetime_sql(
    alias: str,
    *,
    start_column: str = "start_time",
    submitted_column: str = "submitted_at",
) -> str:
    prefix = f"{alias}." if alias else ""
    start_expr = f"NULLIF(TRIM({prefix}{start_column}), '')"
    return (
        "COALESCE("
        f"CASE WHEN {start_expr} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN {start_expr}::timestamptz ELSE NULL END, "
        f"CASE WHEN {start_expr} ~ '^[A-Za-z]{{3}}\\s+\\d{{1,2}},\\s+\\d{{4}}\\s+\\d{{1,2}}:\\d{{2}}:\\d{{2}}\\s+(AM|PM)$' THEN to_timestamp({start_expr}, 'Mon DD, YYYY HH12:MI:SS AM') ELSE NULL END"
        ")"
    )


def main_case_scope_clause(settings: Settings, alias: str, *, prefix: str = "AND") -> tuple[str, list[Any]]:
    start_date = main_data_start_date(settings)
    form_id = main_data_form_id(settings)
    conditions: list[str] = []
    params: list[Any] = []
    if form_id:
        conditions.append(f"{alias}.form_id = %s")
        params.append(form_id)
    if start_date:
        conditions.append(f"{main_case_effective_datetime_sql(alias)} >= %s::date")
        params.append(start_date)
    if not conditions:
        return "", []
    return f"{prefix} {' AND '.join(conditions)}", params


def main_row_scope_clause(
    settings: Settings,
    alias: str,
    *,
    prefix: str = "AND",
    start_column: str = "start_time",
    submitted_column: str = "submitted_at",
) -> tuple[str, list[Any]]:
    start_date = main_data_start_date(settings)
    if not start_date:
        return "", []
    return (
        f"{prefix} {main_row_effective_datetime_sql(alias, start_column=start_column, submitted_column=submitted_column)} >= %s::date",
        [start_date],
    )


def main_case_scope_condition(settings: Settings, alias: str) -> tuple[str, list[Any]]:
    start_date = main_data_start_date(settings)
    form_id = main_data_form_id(settings)
    conditions: list[str] = []
    params: list[Any] = []
    if form_id:
        conditions.append(f"{alias}.form_id = %s")
        params.append(form_id)
    if start_date:
        conditions.append(f"{main_case_effective_datetime_sql(alias)} >= %s::date")
        params.append(start_date)
    if not conditions:
        return "TRUE", []
    return " AND ".join(conditions), params
