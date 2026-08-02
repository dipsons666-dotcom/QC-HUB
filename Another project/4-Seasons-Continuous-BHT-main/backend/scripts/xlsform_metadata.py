from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
DIRECT_ALIASES = {
    "region": "City_1",
    "d3": "D3",
    "d5": "D5",
    "marital_status": "B1",
}
MULTI_CODE_RE = re.compile(r"^A_(.+)_(\d+)$", re.IGNORECASE)
INDEXED_CODE_RE = re.compile(r"^I_(\d+)_A_([A-Z]+)_(.+)_(\d+)$", re.IGNORECASE)


def normalize_spaces(value: str) -> str:
    return SPACE_RE.sub(" ", str(value or "").replace("\xa0", " ")).strip()


def strip_html(value: str) -> str:
    return normalize_spaces(HTML_TAG_RE.sub(" ", str(value or "")))


def normalize_lookup_key(value: str) -> str:
    return normalize_spaces(value).lower()


def _value_candidates(value: Any) -> list[str]:
    text = normalize_spaces("" if value is None else str(value))
    if not text:
        return []
    candidates = [text]
    try:
        numeric = float(text)
    except ValueError:
        return list(dict.fromkeys(candidates))

    if numeric.is_integer():
        candidates.append(str(int(numeric)))
    candidates.append(str(numeric))
    candidates.append(f"{numeric:.1f}")
    return list(dict.fromkeys(candidates))


def _find_choice_label(list_entry: dict[str, str], raw_value: Any) -> str | None:
    if not list_entry:
        return None
    for candidate in _value_candidates(raw_value):
        label = list_entry.get(candidate)
        if label:
            return strip_html(label)
    return None


def _extract_label_parts(label: str) -> dict[str, str | None]:
    original = str(label or "")
    span_hits = [strip_html(hit) for hit in re.findall(r"<span[^>]*>(.*?)</span>", original, flags=re.IGNORECASE | re.DOTALL)]
    cleaned = strip_html(original)
    return {
        "cleaned": cleaned,
        "subject": span_hits[-1] if span_hits else None,
    }


def load_xlsform_metadata(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"questions": {}, "lists": {}, "lookup": {}}

    metadata_path = Path(path)
    if not metadata_path.exists():
        return {"questions": {}, "lists": {}, "lookup": {}}

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    questions = payload.get("questions", {})
    lookup = {normalize_lookup_key(name): name for name in questions}
    for name, entry in questions.items():
        for alias in entry.get("aliases", []):
            lookup.setdefault(normalize_lookup_key(alias), name)
    return {
        "questions": questions,
        "lists": payload.get("lists", {}),
        "lookup": lookup,
    }


def _lookup_question_entry(question_name: str, metadata: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    lookup = metadata.get("lookup", {})
    questions = metadata.get("questions", {})
    hit = lookup.get(normalize_lookup_key(question_name))
    if not hit:
        return None, None
    return hit, questions.get(hit)


SUFFIX_CODE_RE = re.compile(r"^(.+)_(\d+)$", re.IGNORECASE)


def get_xlsform_question_metadata(question_code: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    code = normalize_spaces(question_code)
    if not code:
        return None

    resolved_name = None
    entry = None
    choice_value = None
    code_kind = "direct"

    indexed_match = INDEXED_CODE_RE.match(code)
    if indexed_match:
        left_index, category_code, base_name, choice_value = indexed_match.groups()
        resolved_name, entry = _lookup_question_entry(f"{category_code}_{base_name}.{left_index}", metadata)
        code_kind = "indexed"
    else:
        multi_match = MULTI_CODE_RE.match(code)
        if multi_match:
            base_name, choice_value = multi_match.groups()
            resolved_name, entry = _lookup_question_entry(base_name, metadata)
            code_kind = "multi"
        else:
            direct_name = DIRECT_ALIASES.get(normalize_lookup_key(code), code)
            resolved_name, entry = _lookup_question_entry(direct_name, metadata)

    # Fallback: SurveyCTO wide-export checkbox columns arrive as BASENAME_N (e.g. BAU1B_1).
    # If direct lookup found nothing, strip the trailing _N and try the base name.
    if not entry:
        suffix_match = SUFFIX_CODE_RE.match(code)
        if suffix_match:
            base_name, choice_value = suffix_match.groups()
            resolved_name, entry = _lookup_question_entry(base_name, metadata)
            if entry:
                code_kind = "multi"

    if not entry:
        return None

    list_name = entry.get("list_name") or ""
    list_entry = metadata.get("lists", {}).get(list_name, {})
    choice_label = _find_choice_label(list_entry, choice_value)
    label_parts = _extract_label_parts(entry.get("label", ""))

    return {
        "question_name": resolved_name,
        "label": entry.get("label", ""),
        "list_name": list_name,
        "kind": entry.get("kind", ""),
        "code_kind": code_kind,
        "choice_value": choice_value,
        "choice_label": choice_label,
        "cleaned_label": label_parts["cleaned"] or normalize_spaces(code),
        "subject": label_parts["subject"],
        "__list_entry": list_entry,
    }


def build_question_text(question_code: str, entry: dict[str, Any] | None) -> str:
    if not entry:
        return normalize_spaces(question_code)

    base = normalize_spaces(entry.get("cleaned_label") or question_code)
    choice_label = normalize_spaces(entry.get("choice_label") or "")
    subject = normalize_spaces(entry.get("subject") or "")
    code_kind = entry.get("code_kind", "direct")

    if code_kind in ("multi", "indexed") and choice_label and subject:
        return normalize_spaces(f"({subject}) {base} ({choice_label})")

    if code_kind in ("multi", "indexed") and choice_label:
        return normalize_spaces(f"{base} ({choice_label})")

    return base


def build_answer_text(question_code: str, raw_value: Any, entry: dict[str, Any] | None, is_binary: bool) -> str:
    if entry:
        code_kind = entry.get("code_kind", "direct")
        if code_kind in ("multi", "indexed") and entry.get("choice_label"):
            return normalize_spaces(entry["choice_label"])

        list_name = entry.get("list_name") or ""
        list_entry = entry.get("__list_entry")
        if list_name:
            value_label = _find_choice_label(list_entry or {}, raw_value)
            if value_label:
                return value_label

        if is_binary and entry.get("choice_label"):
            return normalize_spaces(entry["choice_label"])

    return normalize_spaces("" if raw_value is None else str(raw_value))


def map_demographic_value(column_name: str, raw_value: Any, metadata: dict[str, Any]) -> str:
    entry = get_xlsform_question_metadata(column_name, metadata)
    if not entry:
        return normalize_spaces("" if raw_value is None else str(raw_value))

    list_name = entry.get("list_name") or ""
    list_entry = metadata.get("lists", {}).get(list_name, {})
    mapped = _find_choice_label(list_entry, raw_value)
    if mapped:
        return mapped
    return normalize_spaces("" if raw_value is None else str(raw_value))
