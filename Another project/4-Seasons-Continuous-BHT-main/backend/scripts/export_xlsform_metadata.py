#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from xlsform_metadata import normalize_spaces


def parse_type(value: str) -> tuple[str, str]:
    text = normalize_spaces(value)
    if not text:
        return "", ""
    parts = text.split(" ", 1)
    kind = parts[0]
    list_name = parts[1] if len(parts) > 1 else ""
    return kind, list_name


def build_aliases(name: str) -> list[str]:
    normalized = normalize_spaces(name)
    aliases = {
        normalized,
        normalized.upper(),
        normalized.replace(".", "_"),
        normalized.upper().replace(".", "_"),
    }
    return sorted(alias for alias in aliases if alias)


def row_value(row: pd.Series, name: str) -> str:
    return normalize_spaces(row.get(name)) if name in row.index else ""


def infer_group_path(rows: pd.DataFrame, row_index: int) -> str:
    stack: list[str] = []
    for _, prior in rows.iloc[: row_index + 1].iterrows():
        kind, _list_name = parse_type(prior.get("type"))
        name = normalize_spaces(prior.get("name"))
        if kind in {"begin_group", "begin_repeat"} and name:
            stack.append(name)
        elif kind in {"end_group", "end_repeat"} and stack:
            stack.pop()
    return "/".join(stack)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export XLSForm survey/choices metadata to JSON.")
    parser.add_argument("xlsx_path", help="Path to the XLSForm workbook")
    parser.add_argument("output_path", help="Where to write the metadata JSON")
    args = parser.parse_args()

    xlsx_path = Path(args.xlsx_path).resolve()
    output_path = Path(args.output_path).resolve()

    survey = pd.read_excel(xlsx_path, sheet_name="survey")
    choices = pd.read_excel(xlsx_path, sheet_name="choices")

    payload = {
        "source": str(xlsx_path),
        "questions": {},
        "lists": {},
    }

    for row_index, row in survey.iterrows():
        name = normalize_spaces(row.get("name"))
        if not name:
            continue
        kind, list_name = parse_type(row.get("type"))
        payload["questions"][name] = {
            "name": name,
            "label": normalize_spaces(row.get("label")),
            "type": normalize_spaces(row.get("type")),
            "kind": kind,
            "list_name": normalize_spaces(list_name),
            "relevance": row_value(row, "relevance"),
            "required": row_value(row, "required"),
            "constraint": row_value(row, "constraint"),
            "calculation": row_value(row, "calculation"),
            "appearance": row_value(row, "appearance"),
            "group_path": infer_group_path(survey, row_index),
            "is_group": kind in {"begin_group", "end_group"},
            "is_repeat": kind in {"begin_repeat", "end_repeat"},
            "is_derived": kind in {"calculate", "hidden"} or bool(row_value(row, "calculation")),
            "aliases": build_aliases(name),
        }

    for _, row in choices.iterrows():
        list_name = normalize_spaces(row.get("list_name"))
        choice_name = normalize_spaces(row.get("name"))
        if not list_name or not choice_name:
            continue
        payload["lists"].setdefault(list_name, {})
        payload["lists"][list_name][choice_name] = normalize_spaces(row.get("label"))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Wrote {len(payload['questions'])} questions and {len(payload['lists'])} lists to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
