from __future__ import annotations

import re
from typing import Any

import pandas as pd

from backend.app.database import db_connection
from backend.app.settings import Settings

_YES_TOKENS = {"yes", "y", "1", "1.0", "true", "t"}
_NO_TOKENS = {"no", "n", "0", "0.0", "false", "f"}


def _normalize_indicator_token(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().lower()


def _looks_like_indicator_series(series: pd.Series) -> bool:
    observed = {
        _normalize_indicator_token(value)
        for value in series.tolist()
        if _normalize_indicator_token(value)
    }
    return observed.issubset(_YES_TOKENS | _NO_TOKENS)


def _coerce_indicator_value(value: Any) -> str:
    token = _normalize_indicator_token(value)
    if not token or token in _NO_TOKENS:
        return "No"
    if token in _YES_TOKENS:
        return "Yes"
    return str(value).strip()


def _indicator_selected_tokens(choice_code: str) -> set[str]:
    normalized = _normalize_indicator_token(choice_code)
    if not normalized:
        return set(_YES_TOKENS)
    tokens = {normalized}
    try:
        number = float(normalized)
    except (TypeError, ValueError):
        number = None
    if number is not None:
        tokens.add(str(number))
        if number.is_integer():
            tokens.add(str(int(number)))
    return tokens | _YES_TOKENS


def _coerce_indicator_value_for_choice(value: Any, selected_tokens: set[str] | None) -> str:
    token = _normalize_indicator_token(value)
    if not token or token in _NO_TOKENS:
        return "No"
    if token in _YES_TOKENS:
        return "Yes"
    if selected_tokens and token in selected_tokens:
        return "Yes"
    return str(value).strip()


def _load_multiselect_column_patterns(
    settings: Settings,
    instrument_code: str,
) -> list[tuple[str, list[str]]]:
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.variable_name,
                    q.question_type,
                    q.choice_list_name,
                    c.choice_code
                FROM reference.xlsform_question q
                LEFT JOIN reference.xlsform_choice c
                    ON c.instrument_code = q.instrument_code
                   AND c.list_name = q.choice_list_name
                WHERE q.instrument_code = %s
                  AND LOWER(COALESCE(q.question_type, '')) LIKE 'select_multiple %%'
                ORDER BY q.variable_name, c.sort_order NULLS LAST, c.choice_code
                """,
                (instrument_code,),
            )
            rows = cur.fetchall()

    grouped: dict[str, list[str]] = {}
    for row in rows:
        variable_name = str(row.get("variable_name") or "").strip()
        if not variable_name:
            continue
        grouped.setdefault(variable_name, [])
        choice_code = str(row.get("choice_code") or "").strip()
        if choice_code and choice_code not in grouped[variable_name]:
            grouped[variable_name].append(choice_code)
    return list(grouped.items())


def apply_multiselect_yes_no_indicators(
    settings: Settings,
    instrument_code: str,
    df: pd.DataFrame,
) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    column_names = set(str(column) for column in out.columns)
    target_columns: dict[str, set[str] | None] = {}
    patterns = _load_multiselect_column_patterns(settings, instrument_code)

    for base_name, choice_codes in patterns:
        exact_hits: dict[str, set[str]] = {}
        for choice_code in choice_codes:
            selected_tokens = _indicator_selected_tokens(choice_code)
            for column_name in (
                f"{base_name}_{choice_code}",
                f"{base_name}_{choice_code.replace('.', '_')}",
                f"{base_name}/{choice_code}",
            ):
                if column_name in column_names:
                    exact_hits[column_name] = selected_tokens
        if exact_hits:
            target_columns.update(exact_hits)
            continue

        fallback_hits = [
            column_name
            for column_name in column_names
            if re.match(rf"^{re.escape(base_name)}(?:_|/).+$", column_name)
            and _looks_like_indicator_series(out[column_name])
        ]
        for column_name in fallback_hits:
            target_columns[column_name] = None

    if instrument_code == "main" and not target_columns:
        for column_name in (
            str(column_name)
            for column_name in out.columns
            if re.match(r"^.+_\d+$", str(column_name))
        ):
            target_columns[column_name] = None

    for column_name in sorted(target_columns):
        if column_name not in out.columns:
            continue
        series = out[column_name]
        selected_tokens = target_columns.get(column_name)
        if instrument_code != "main" and selected_tokens is None and not _looks_like_indicator_series(series):
            continue
        if instrument_code == "main" and selected_tokens is None and not (
            _looks_like_indicator_series(series) or not any(_normalize_indicator_token(value) for value in series.tolist())
        ):
            continue
        out[column_name] = series.map(lambda value: _coerce_indicator_value_for_choice(value, selected_tokens))

    return out
