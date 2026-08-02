from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from fastapi import HTTPException


QC_PRODUCTIVITY_QUEUES = {"all", "audio", "callback"}


def normalize_qc_productivity_queue(queue: str | None) -> str:
    value = str(queue or "all").strip().lower() or "all"
    if value not in QC_PRODUCTIVITY_QUEUES:
        raise HTTPException(status_code=400, detail="queue must be one of: all, audio, callback")
    return value


def _extract_date_key(candidate: Any) -> str | None:
    if candidate is None:
        return None
    if isinstance(candidate, datetime):
        return candidate.date().isoformat()
    if isinstance(candidate, date):
        return candidate.isoformat()

    raw = str(candidate).strip()
    if not raw:
        return None
    if len(raw) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", raw[:10]):
        return raw[:10]
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    match = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", raw)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def summarize_qc_task_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}

    for row in rows:
        username = str(row.get("username") or "").strip() or "Unknown"
        full_name = str(row.get("full_name") or "").strip()
        item = summary.setdefault(
            username,
            {
                "username": username,
                "full_name": full_name,
                "total_pushed": 0,
                "completed": 0,
                "pending": 0,
            },
        )
        if full_name and not item["full_name"]:
            item["full_name"] = full_name
        item["total_pushed"] += 1
        if row.get("completed_at"):
            item["completed"] += 1
        else:
            item["pending"] += 1

    return sorted(
        summary.values(),
        key=lambda item: (-int(item.get("total_pushed") or 0), str(item.get("username") or "").lower()),
    )


def build_qc_productivity_by_date(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates: set[str] = set()
    pivot: dict[str, dict[str, Any]] = {}

    for row in rows:
        username = str(row.get("username") or "").strip() or "Unknown"
        full_name = str(row.get("full_name") or "").strip()
        date_key = _extract_date_key(row.get("assigned_at"))
        if not date_key:
            continue
        dates.add(date_key)
        item = pivot.setdefault(username, {"username": username, "full_name": full_name, "counts": {}})
        if full_name and not item["full_name"]:
            item["full_name"] = full_name
        item["counts"][date_key] = int(item["counts"].get(date_key, 0) or 0) + 1

    sorted_dates = sorted(dates)
    items = []
    for username, row in sorted(pivot.items(), key=lambda item: item[0].lower()):
        counts = row.get("counts") or {}
        items.append(
            {
                "username": username,
                "full_name": row.get("full_name") or "",
                "counts": {date_key: int(counts.get(date_key, 0) or 0) for date_key in sorted_dates},
            }
        )
    return {"dates": sorted_dates, "items": items}
