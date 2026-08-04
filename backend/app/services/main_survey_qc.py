"""Batch QC checks defined in *QC FLAGS DEFINITIONS* for Main Survey data."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Iterable


def _answers(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("answers")
    return nested if isinstance(nested, dict) else payload


def _value(row: dict[str, Any], *names: str) -> Any:
    lookup = {str(key).lower(): value for key, value in _answers(row).items()}
    return next((lookup[name.lower()] for name in names if name.lower() in lookup), None)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    for pattern in ("%b %d, %Y %I:%M:%S %p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    return None


def _phone(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _gps(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split()[:2])
    if isinstance(value, dict):
        return f"{value.get('lat') or value.get('latitude')} {value.get('lon') or value.get('longitude')}"
    return ""


def evaluate_main_survey(rows: Iterable[tuple[str, dict[str, Any]]]) -> list[dict[str, str]]:
    """Return one reviewable flag per affected submission and documented rule."""
    cases = [(key, payload) for key, payload in rows if isinstance(payload, dict)]
    flags: list[dict[str, str]] = []

    def flag(key: str, code: str, message: str) -> None:
        flags.append({"submission_key": key, "code": code, "severity": "high", "message": message})

    durations = [_number(_value(payload, "duration")) for _, payload in cases]
    durations = [value for value in durations if value is not None]
    if durations:
        middle = median(durations)
        for key, payload in cases:
            duration = _number(_value(payload, "duration"))
            if duration is not None and duration < middle * .5:
                flag(key, "MAIN_LOW_LOI", f"Interview duration {duration:g}s is below 50% of the batch median ({middle:g}s).")
            if duration is not None and duration > middle * 1.5:
                flag(key, "MAIN_HIGH_LOI", f"Interview duration {duration:g}s is above 150% of the batch median ({middle:g}s).")

    by_phone: dict[str, list[tuple[str, str]]] = defaultdict(list)
    by_gps: dict[str, list[tuple[str, str]]] = defaultdict(list)
    by_interviewer: dict[str, list[tuple[str, datetime | None, datetime | None, dict[str, Any]]]] = defaultdict(list)
    for key, payload in cases:
        interviewer = str(_value(payload, "Interviewer", "username", "interviewer_id") or "Unknown")
        start, end = _time(_value(payload, "starttime", "start_time")), _time(_value(payload, "endtime", "end_time"))
        if start and (start.hour >= 19 or start.hour < 7):
            flag(key, "MAIN_START_TIME", f"Interview started at unusual working hour: {start:%H:%M}.")
        phone = _phone(_value(payload, "phone", "phone_number", "respondent_phone", "devicephonenum"))
        if phone: by_phone[phone].append((key, interviewer))
        coordinate = _gps(_value(payload, "gps", "geopoint"))
        if coordinate: by_gps[coordinate].append((key, interviewer))
        by_interviewer[interviewer].append((key, start, end, payload))

    for phone, entries in by_phone.items():
        if len(entries) > 1:
            for key, _ in entries: flag(key, "MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL", f"Phone number ending {phone[-4:]} occurs in {len(entries)} active interviews.")
        for interviewer in {entry[1] for entry in entries}:
            same = [entry for entry in entries if entry[1] == interviewer]
            if len(same) > 1:
                for key, _ in same: flag(key, "MAIN_DUPLICATE_PHONE_NUMBER", f"Phone number ending {phone[-4:]} is duplicated for interviewer {interviewer}.")
    for coordinate, entries in by_gps.items():
        for interviewer in {entry[1] for entry in entries}:
            same = [entry for entry in entries if entry[1] == interviewer]
            if len(same) > 1:
                for key, _ in same: flag(key, "MAIN_DUPLICATE_GPS", f"Exact GPS coordinates are repeated in {len(same)} interviews by {interviewer}.")

    for interviewer, entries in by_interviewer.items():
        dated = sorted((entry for entry in entries if entry[1] and entry[2]), key=lambda entry: entry[1])
        for previous, current in zip(dated, dated[1:]):
            _, _, prior_end, _ = previous; key, start, _, _ = current
            gap = (start - prior_end).total_seconds() if prior_end else None
            if gap is not None and 0 <= gap < 300: flag(key, "MAIN_GAP_BETWEEN_2_INTERVIEWST", f"Only {gap / 60:.1f} minutes elapsed after the prior interview by {interviewer}.")
            if gap is not None and gap < -60: flag(key, "MAIN_TIME_INTERWOVEN", f"Interview overlaps the prior interview by {abs(gap) / 60:.1f} minutes for {interviewer}.")
        if len(entries) >= 10:
            for field, threshold in (("Gender", .9), ("Age_Range", .8), ("Sector", .85), ("P1", .9)):
                values = [str(_value(payload, field) or "") for _, _, _, payload in entries]
                values = [value for value in values if value]
                if values and max(Counter(values).values()) / len(values) >= threshold:
                    for key, _, _, _ in entries: flag(key, "MAIN_ENUMERATOR_MATRIX_ANOMALY", f"{interviewer}'s {field} responses are at least {threshold:.0%} concentrated in one category.")
    return flags
