from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from backend.app.database import db_connection
from backend.app.settings import Settings


APPROVED_STAGES = frozenset({"approved", "reviewed_approved"})


def normalize_approval_stage(value: Any) -> str:
    return str(value or "").strip().lower()


def encode_feed_cursor(updated_at: datetime | str, case_id: str) -> str:
    timestamp = updated_at.isoformat() if isinstance(updated_at, datetime) else str(updated_at)
    payload = json.dumps({"updatedAt": timestamp, "caseId": str(case_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_feed_cursor(cursor: str | None) -> tuple[str, str]:
    if not cursor:
        return "1970-01-01T00:00:00+00:00", ""
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return str(payload["updatedAt"]), str(payload.get("caseId") or "")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid integration cursor.") from exc


def _serialize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def get_bht_case_feed(settings: Settings, cursor: str | None = None, limit: int = 500) -> dict[str, Any]:
    _cursor_updated_at, cursor_case_id = decode_feed_cursor(cursor)
    page_size = max(1, min(int(limit or 500), 10000))

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    mc.submission_key,
                    mc.case_id,
                    mc.survey_month,
                    mc.current_status,
                    mc.approval_stage,
                    mc.reviewed_at,
                    mc.approved_at,
                    mc.updated_at AS feed_updated_at
                FROM clean.main_case mc
                WHERE NULLIF(TRIM(mc.submission_key), '') IS NOT NULL
                  AND mc.case_id > %s
                ORDER BY mc.case_id
                LIMIT %s
                """,
                (cursor_case_id, page_size + 1),
            )
            rows = list(cur.fetchall())
            has_more = len(rows) > page_size
            rows = rows[:page_size]

            approved_submission_keys = [
                str(row["submission_key"])
                for row in rows
                if normalize_approval_stage(row.get("approval_stage")) in APPROVED_STAGES
            ]
            overrides_by_key: dict[str, dict[str, Any]] = {key: {} for key in approved_submission_keys}
            if approved_submission_keys:
                cur.execute(
                    """
                    WITH correction_events AS (
                        SELECT
                            d.submission_key,
                            d.field_name AS variable_name,
                            d.new_value,
                            d.changed_at AS event_at,
                            2 AS source_priority
                        FROM qc.data_change_log d
                        WHERE d.instrument_code = 'main'
                          AND d.submission_key = ANY(%s)
                          AND NULLIF(TRIM(d.field_name), '') IS NOT NULL

                        UNION ALL

                        SELECT
                            e.submission_key,
                            e.variable_name,
                            e.new_value,
                            e.cleaned_at AS event_at,
                            1 AS source_priority
                        FROM clean.main_data_error_audit e
                        WHERE e.submission_key = ANY(%s)
                          AND NULLIF(TRIM(e.variable_name), '') IS NOT NULL
                          AND e.new_value IS NOT NULL
                    )
                    SELECT DISTINCT ON (submission_key, variable_name)
                        submission_key, variable_name, new_value
                    FROM correction_events
                    ORDER BY submission_key, variable_name, event_at DESC, source_priority DESC
                    """,
                    (approved_submission_keys, approved_submission_keys),
                )
                for correction in cur.fetchall():
                    key = str(correction.get("submission_key") or "")
                    variable = str(correction.get("variable_name") or "").strip()
                    if key in overrides_by_key and variable:
                        overrides_by_key[key][variable] = correction.get("new_value")

    items = []
    for row in rows:
        submission_key = str(row["submission_key"])
        approval_stage = normalize_approval_stage(row.get("approval_stage"))
        approved = approval_stage in APPROVED_STAGES
        items.append(
            {
                "submissionKey": submission_key,
                "caseId": str(row.get("case_id") or ""),
                "surveyMonth": row.get("survey_month"),
                "currentStatus": normalize_approval_stage(row.get("current_status")),
                "approvalStage": approval_stage,
                "isApproved": approved,
                "reviewedAt": _serialize_timestamp(row.get("reviewed_at")),
                "approvedAt": _serialize_timestamp(row.get("approved_at")),
                "updatedAt": _serialize_timestamp(row.get("feed_updated_at")),
                "overrides": overrides_by_key.get(submission_key, {}) if approved else {},
            }
        )

    next_cursor = cursor
    if rows:
        last = rows[-1]
        next_cursor = encode_feed_cursor(last["feed_updated_at"], str(last["case_id"]))

    return {
        "items": items,
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "approvedStages": sorted(APPROVED_STAGES),
    }
