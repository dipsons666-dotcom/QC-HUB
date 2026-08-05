"""Batch QC checks defined in *QC FLAGS DEFINITIONS* for Main Survey data."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Iterable


def _duration(seconds: float) -> str:
    """Present a duration the way a reviewer would say it out loud."""
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hr")
    if minutes or not parts:
        parts.append(f"{minutes} min")
    if seconds and not hours:
        parts.append(f"{seconds} sec")
    return " ".join(parts)


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

    def flag(key: str, code: str, summary: str, evidence: str, action: str, finding_key: str = "default") -> None:
        # Keep the evidence self-contained: it is also visible in exports and
        # audit history, where the detail-screen labels are not available.
        if code == "MAIN_ENUMERATOR_MATRIX_ANOMALY" and finding_key == "default":
            # One interviewer can breach the concentration threshold for
            # several fields. Make each breached field a distinct finding.
            displayed_field = evidence.split(" recorded ", 1)[-1].split(" responses", 1)[0].lower()
            finding_key = {"noodles": "p1"}.get(displayed_field, displayed_field.replace(" ", "_"))
        flags.append({
            "submission_key": key, "code": code, "severity": "high",
            "summary": summary, "message": evidence, "action": action,
            # A rule can legitimately find more than one independent problem
            # in a submission. This stable key keeps those findings separate
            # when they are stored and when QC is rerun.
            "finding_key": finding_key,
        })

    durations = [_number(_value(payload, "duration")) for _, payload in cases]
    durations = [value for value in durations if value is not None]
    if durations:
        middle = median(durations)
        for key, payload in cases:
            duration = _number(_value(payload, "duration"))
            if duration is not None and duration < middle * .5:
                ratio = duration / middle
                flag(key, "MAIN_LOW_LOI", "Interview completed much faster than usual",
                     f"This interview took {_duration(duration)}. A typical interview in this batch takes {_duration(middle)}. "
                     f"That is {ratio:.0%} of the typical time ({_duration(middle - duration)} faster), which is below the 50% review threshold.",
                     "Check the interview audio, timestamps, and key responses to confirm that the questionnaire was completed properly rather than rushed or skipped.")
            if duration is not None and duration > middle * 1.5:
                ratio = duration / middle
                flag(key, "MAIN_HIGH_LOI", "Interview took much longer than usual",
                     f"This interview took {_duration(duration)}. A typical interview in this batch takes {_duration(middle)}. "
                     f"It took {ratio:.1f} times the typical time ({_duration(duration - middle)} longer), exceeding the 150% review threshold.",
                     "Check the interview timeline and audio. Long duration can be legitimate—for example, pauses or a difficult respondent—but it should be explained.")

    by_phone: dict[str, list[tuple[str, str]]] = defaultdict(list)
    by_gps: dict[str, list[tuple[str, str]]] = defaultdict(list)
    by_interviewer: dict[str, list[tuple[str, datetime | None, datetime | None, dict[str, Any]]]] = defaultdict(list)
    for key, payload in cases:
        interviewer = str(_value(payload, "Interviewer", "username", "interviewer_id") or "Unknown")
        start, end = _time(_value(payload, "starttime", "start_time")), _time(_value(payload, "endtime", "end_time"))
        if start and (start.hour >= 19 or start.hour < 7):
            flag(key, "MAIN_START_TIME", "Interview started outside normal field hours",
                 f"The interview started at {start:%H:%M}, outside the normal review window of 07:00–18:59.",
                 "Confirm that the respondent was genuinely interviewed at this time and verify the timestamp against the audio or fieldwork schedule.")
        phone = _phone(_value(payload, "phone", "phone_number", "respondent_phone", "devicephonenum"))
        if phone: by_phone[phone].append((key, interviewer))
        coordinate = _gps(_value(payload, "gps", "geopoint"))
        if coordinate: by_gps[coordinate].append((key, interviewer))
        by_interviewer[interviewer].append((key, start, end, payload))

    for phone, entries in by_phone.items():
        if len(entries) > 1:
            for key, _ in entries: flag(key, "MAIN_DUPLICATE_PHONE_NUMBER_GLOBAL", "Respondent phone number appears in multiple interviews",
                                        f"The phone number ending {phone[-4:]} appears in {len(entries)} active interviews across the survey.",
                                        "Compare the respondent details and call records. Shared household phones can be legitimate, but separate respondents must be verified.")
        for interviewer in {entry[1] for entry in entries}:
            same = [entry for entry in entries if entry[1] == interviewer]
            if len(same) > 1:
                for key, _ in same: flag(key, "MAIN_DUPLICATE_PHONE_NUMBER", "Same interviewer reused a respondent phone number",
                                          f"Interviewer {interviewer} recorded the phone number ending {phone[-4:]} in {len(same)} interviews.",
                                          "Compare these cases for duplicate respondents or reused details; confirm any genuine shared-phone situation with the interviewer.")
    for coordinate, entries in by_gps.items():
        for interviewer in {entry[1] for entry in entries}:
            same = [entry for entry in entries if entry[1] == interviewer]
            if len(same) > 1:
                for key, _ in same: flag(key, "MAIN_DUPLICATE_GPS", "Interviews were recorded at the exact same location",
                                          f"Interviewer {interviewer} submitted {len(same)} interviews with the exact same GPS point ({coordinate}).",
                                          "Check whether the interviews occurred in one compound or venue. If not, compare the records and GPS capture settings for possible reuse.")

    for interviewer, entries in by_interviewer.items():
        dated = sorted((entry for entry in entries if entry[1] and entry[2]), key=lambda entry: entry[1])
        for previous, current in zip(dated, dated[1:]):
            _, _, prior_end, _ = previous; key, start, _, _ = current
            gap = (start - prior_end).total_seconds() if prior_end else None
            if gap is not None and 0 <= gap < 300:
                flag(key, "MAIN_GAP_BETWEEN_2_INTERVIEWST", "Very little time between two interviews",
                     f"Only {_duration(gap)} passed between the previous interview ending and this interview starting for interviewer {interviewer}. The review threshold is less than 5 minutes.",
                     "Review both interview timelines and audio. Confirm whether travel and consent could realistically have happened in this time.")
            if gap is not None and gap < -60:
                flag(key, "MAIN_TIME_INTERWOVEN", "Interview times overlap",
                     f"This interview began {_duration(abs(gap))} before interviewer {interviewer}'s previous interview had ended.",
                     "Check timestamps and audio for both interviews. One interviewer may not be able to conduct overlapping interviews without an explanation.")
        if len(entries) >= 10:
            for field, threshold in (("Gender", .9), ("Age_Range", .8), ("Sector", .85), ("P1", .9)):
                values = [str(_value(payload, field) or "") for _, _, _, payload in entries]
                values = [value for value in values if value]
                if values and max(Counter(values).values()) / len(values) >= threshold:
                    common, count = Counter(values).most_common(1)[0]
                    share = count / len(values)
                    field_label = {"P1": "Noodles"}.get(field, field.replace("_", " "))
                    for key, _, _, _ in entries: flag(key, "MAIN_ENUMERATOR_MATRIX_ANOMALY", "Interviewer responses are unusually concentrated",
                                                       f"For interviewer {interviewer}, {count} of {len(values)} recorded {field_label} responses ({share:.0%}) are in the same category ({common}). The review threshold is {threshold:.0%}.",
                                                       "Check the interviewer’s sample allocation and respondent selection. A concentration may be expected in a targeted area, but it can also signal repetitive selection.")
    return flags
