from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from backend.app.auth import AuthUser
from backend.app.database import db_connection
from backend.app.settings import Settings


def _json_default(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _jsonb_payload(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def infer_module_from_path(path: str | None) -> str:
    normalized = str(path or "").strip().lower()
    if normalized.startswith("/api/admin"):
        return "admin"
    if normalized.startswith("/api/auth"):
        return "auth"
    if normalized.startswith("/api/main-survey"):
        return "main"
    if normalized.startswith("/api/listing") or normalized.startswith("/api/dashboard") or normalized.startswith("/api/exports"):
        return "listing"
    return "app"


def resolve_client_ip(
    request: Request | None = None,
    forwarded_for: str | None = None,
    client_ip: str | None = None,
) -> str | None:
    if client_ip:
        return client_ip
    forwarded_header = forwarded_for or (request.headers.get("x-forwarded-for") if request else None)
    if forwarded_header:
        first_hop = str(forwarded_header).split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    if request and request.client:
        return request.client.host
    return None


def log_activity(
    settings: Settings,
    *,
    action: str,
    module: str,
    user: AuthUser | None = None,
    user_id: str | None = None,
    username: str | None = None,
    role: str | None = None,
    status: str | None = None,
    success: bool | None = None,
    description: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before_value: Any = None,
    after_value: Any = None,
    metadata: dict[str, Any] | None = None,
    error_message: str | None = None,
    request: Request | None = None,
    device_id: str | None = None,
    forwarded_for: str | None = None,
    client_ip: str | None = None,
) -> None:
    """Best-effort activity logging. Failures must never break the request flow."""
    resolved_user_id = user_id or (user.id if user else None)
    resolved_username = username or (user.username if user else None)
    resolved_role = role or (user.role if user else None)
    resolved_success = success if success is not None else (False if status == "failed" else True)
    resolved_status = status or ("success" if resolved_success else "failed")
    resolved_device_id = device_id or (request.headers.get("x-device-id") if request else None)
    resolved_client_ip = resolve_client_ip(request, forwarded_for, client_ip)

    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit.activity_log (
                        user_id,
                        username,
                        role,
                        action,
                        module,
                        status,
                        success,
                        description,
                        entity_type,
                        entity_id,
                        before_value,
                        after_value,
                        metadata,
                        error_message,
                        device_id,
                        client_ip
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s)
                    """,
                    (
                        resolved_user_id,
                        resolved_username,
                        resolved_role,
                        action,
                        module,
                        resolved_status,
                        resolved_success,
                        description,
                        entity_type,
                        entity_id,
                        _jsonb_payload(before_value),
                        _jsonb_payload(after_value),
                        _jsonb_payload(metadata or {}) or "{}",
                        error_message,
                        resolved_device_id,
                        resolved_client_ip,
                    ),
                )
            conn.commit()
    except Exception:
        return


def bootstrap_activity_log(settings: Settings) -> None:
    """Best-effort backfill when the table is empty so the activity log is not blank."""
    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM audit.activity_log")
                row = cur.fetchone() or {}
                if int(row.get("count") or 0) > 0:
                    return

                cur.execute(
                    """
                    INSERT INTO audit.activity_log (
                        occurred_at,
                        user_id,
                        username,
                        role,
                        action,
                        module,
                        status,
                        success,
                        description,
                        entity_type,
                        entity_id,
                        before_value,
                        after_value,
                        metadata
                    )
                    SELECT
                        h.changed_at,
                        h.changed_by_user_id,
                        ua.username,
                        ur.role_code,
                        'case_status_change',
                        CASE WHEN h.instrument_code = 'main' THEN 'main' ELSE 'listing' END,
                        'success',
                        true,
                        COALESCE(h.change_note, 'Case status updated.'),
                        'case',
                        COALESCE(h.submission_key, h.case_id),
                        jsonb_build_object('status', h.previous_status),
                        jsonb_build_object('status', h.new_status),
                        jsonb_build_object(
                            'instrument_code', h.instrument_code,
                            'previous_status', h.previous_status,
                            'new_status', h.new_status,
                            'case_id', h.case_id,
                            'submission_key', h.submission_key
                        )
                    FROM qc.case_status_history h
                    LEFT JOIN app.user_account ua ON ua.user_id = h.changed_by_user_id
                    LEFT JOIN app.user_role ur ON ur.user_id = h.changed_by_user_id
                    ORDER BY h.changed_at DESC
                    LIMIT 2000
                    """
                )

                cur.execute(
                    """
                    INSERT INTO audit.activity_log (
                        occurred_at,
                        user_id,
                        username,
                        role,
                        action,
                        module,
                        status,
                        success,
                        description,
                        entity_type,
                        entity_id,
                        after_value,
                        metadata
                    )
                    SELECT
                        fc.generated_at,
                        ej.requested_by_user_id,
                        ua.username,
                        ur.role_code,
                        'export_generation_success',
                        CASE WHEN fc.instrument_code = 'main' THEN 'main' ELSE 'listing' END,
                        'success',
                        true,
                        'Backfilled export generation.',
                        'export',
                        fc.file_id::text,
                        jsonb_build_object(
                            'file_id', fc.file_id::text,
                            'file_name', fc.file_name,
                            'format', fc.export_format,
                            'row_count', fc.row_count
                        ),
                        jsonb_build_object(
                            'instrument_code', fc.instrument_code,
                            'profile', fc.export_profile,
                            'format', fc.export_format,
                            'file_name', fc.file_name,
                            'row_count', fc.row_count
                        )
                    FROM export.file_catalog fc
                    LEFT JOIN export.export_job ej ON ej.export_job_id = fc.export_job_id
                    LEFT JOIN app.user_account ua ON ua.user_id = ej.requested_by_user_id
                    LEFT JOIN app.user_role ur ON ur.user_id = ej.requested_by_user_id
                    ORDER BY fc.generated_at DESC
                    LIMIT 500
                    """
                )
            conn.commit()
    except Exception:
        return
