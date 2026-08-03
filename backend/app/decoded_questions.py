from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MAX_ROWS = 250


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = text.replace("\xa0", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _load_metadata(metadata_path: str | Path | None) -> dict[str, Any]:
    if not metadata_path:
        metadata_path = Path(__file__).resolve().parents[1] / "data" / "xlsform_metadata.json"

    path = Path(metadata_path)
    if not path.exists():
        raise FileNotFoundError(f"Metadata file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError("Metadata payload must be a JSON object")
    return payload


def _read_settings(payload: dict[str, Any]) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    questions = payload.get("questions", {})
    lists = payload.get("lists", {})
    choice_map: dict[str, dict[str, str]] = {}
    for list_name, items in lists.items():
        if not isinstance(items, dict):
            continue
        normalized_items: dict[str, str] = {}
        for code, label in items.items():
            normalized_items[str(code)] = _normalize_text(label)
        choice_map[str(list_name)] = normalized_items

    survey_rows: list[dict[str, Any]] = []
    for name, meta in questions.items():
        if not isinstance(meta, dict):
            continue
        qtype = _normalize_text(meta.get("type", ""))
        label = _normalize_text(meta.get("label", ""))
        question_name = _normalize_text(meta.get("name", name))
        if not question_name or qtype in {"start", "end", "today", "deviceid", "phonenumber", "username", "simserial", "caseid"}:
            continue
        survey_rows.append({"type": qtype, "name": question_name, "label": label})
    return choice_map, survey_rows


def _decode_value(raw_value: Any, list_name: str | None, choices: dict[str, dict[str, str]], question_type: str) -> str:
    if raw_value is None:
        return ""

    def _resolve_choice_label(token: Any) -> str | None:
        if list_name is None:
            return None
        choice_map = choices.get(list_name or "", {})
        if not choice_map:
            return None

        variants: list[str] = []
        text = str(token).strip()
        if text:
            variants.append(text)

        try:
            numeric = float(text)
            variants.append(str(numeric))
            if numeric.is_integer():
                variants.append(str(int(numeric)))
        except ValueError:
            pass

        if "." in text:
            variants.append(text.split(".", 1)[0])
        else:
            variants.append(f"{text}.0")

        for variant in variants:
            label = choice_map.get(variant)
            if label:
                return label
        return None

    if question_type.startswith("select_multiple"):
        tokens = []
        if isinstance(raw_value, list):
            tokens = [str(v) for v in raw_value]
        else:
            tokens = [token for token in str(raw_value).split() if token]

        labels: list[str] = []
        for token in tokens:
            label = _resolve_choice_label(token)
            if label:
                labels.append(label)
            else:
                labels.append(str(token))
        return ", ".join(labels) if labels else str(raw_value)

    label = _resolve_choice_label(raw_value)
    if label:
        return label

    return str(raw_value)


def decode_submission_to_question_rows(
    payload: dict[str, Any],
    workbook_path: str | Path | None = None,
    metadata_path: str | Path | None = None,
    limit: int | None = _MAX_ROWS,
) -> list[dict[str, Any]]:
    metadata = _load_metadata(metadata_path or (Path(__file__).resolve().parents[1] / "data" / "xlsform_metadata.json"))
    choices, questions = _read_settings(metadata)

    rows: list[dict[str, Any]] = []
    current_category = "General"
    count = 0

    for question in questions:
        name = question["name"]
        if name not in payload:
            continue
        raw_value = payload[name]
        qtype = question["type"]
        list_name = None
        if qtype.startswith("select_one"):
            list_name = qtype.split("select_one", 1)[1].strip()
        elif qtype.startswith("select_multiple"):
            list_name = qtype.split("select_multiple", 1)[1].strip()

        label = _normalize_text(question.get("label") or name)
        if label.startswith("CATEGORY QUESTIONS"):
            current_category = "Category Questions"
        elif label.startswith("Panel") or label.startswith("Noodles"):
            current_category = "Panel"
        elif label.lower().startswith("section"):
            current_category = "Section"

        rows.append(
            {
                "category": current_category,
                "question": label or name,
                "response": _decode_value(raw_value, list_name, choices, qtype),
            }
        )
        count += 1
        if limit is not None and count >= limit:
            break

    return rows
