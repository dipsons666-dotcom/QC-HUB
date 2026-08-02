from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from backend.app.auth import APP_ROLES, SUPERADMIN_ROLE, normalize_role, hash_password, verify_password
from backend.app.database import db_connection
from backend.app.settings import Settings

VALID_ROLES = set(APP_ROLES)
USER_MANAGEMENT_ROLES = {SUPERADMIN_ROLE, "PDM-ADMIN"}
PDM_ADMIN_MANAGEABLE_ROLES = {"PDM-ADMIN", "PDM-QC"}


def _normalize_email(email: str | None) -> str | None:
    value = (email or "").strip()
    return value or None


def _assert_user_manager(actor_role: str | None) -> str:
    role = normalize_role(actor_role)
    if role not in USER_MANAGEMENT_ROLES:
        raise HTTPException(status_code=403, detail="You do not have permission to manage users.")
    return role


def _assert_role_can_be_managed(actor_role: str | None, target_role: str, action: str = "manage") -> str:
    actor = _assert_user_manager(actor_role)
    role = normalize_role(target_role)
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{role}'.")
    if actor != SUPERADMIN_ROLE and role not in PDM_ADMIN_MANAGEABLE_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"PDM-ADMIN users can only {action} PDM-ADMIN and PDM-QC users.",
        )
    return role


def list_users(settings: Settings, actor_role: str | None = None) -> list[dict[str, Any]]:
    actor = _assert_user_manager(actor_role)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            role_filter_sql = ""
            params: tuple[Any, ...] = ()
            if actor != SUPERADMIN_ROLE:
                role_filter_sql = "AND ur.role_code = ANY(%s)"
                params = (list(PDM_ADMIN_MANAGEABLE_ROLES),)
            cur.execute(
                f"""
                SELECT
                    ua.user_id::text AS user_id,
                    ua.username,
                    ua.full_name,
                    ua.email,
                    ua.is_active,
                    ua.created_at,
                    COALESCE(array_agg(ur.role_code ORDER BY ur.role_code) FILTER (WHERE ur.role_code IS NOT NULL), '{{}}') AS roles
                FROM app.user_account ua
                LEFT JOIN app.user_role ur USING (user_id)
                WHERE ua.username IS NOT NULL
                  {role_filter_sql}
                GROUP BY ua.user_id, ua.username, ua.full_name, ua.email, ua.is_active, ua.created_at
                ORDER BY ua.full_name
                """,
                params,
            )
            rows = cur.fetchall()

    return [
        {
            "user_id": row["user_id"],
            "username": row["username"],
            "full_name": row["full_name"],
            "email": row["email"],
            "is_active": row["is_active"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "roles": [normalize_role(role) for role in row["roles"]],
        }
        for row in rows
    ]


def list_users_by_role(settings: Settings, role: str) -> list[dict[str, Any]]:
    role = normalize_role(role)
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{role}'.")
    
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ua.user_id::text AS user_id,
                    ua.username,
                    ua.full_name,
                    ua.email,
                    ua.is_active
                FROM app.user_account ua
                LEFT JOIN app.user_role ur USING (user_id)
                WHERE ur.role_code = %s AND ua.is_active = true AND ua.username IS NOT NULL
                ORDER BY ua.full_name
                """,
                (role,)
            )
            rows = cur.fetchall()

    return [
        {
            "user_id": row["user_id"],
            "username": row["username"],
            "full_name": row["full_name"],
            "email": row["email"],
            "is_active": row["is_active"],
        }
        for row in rows
    ]


def create_user(
    settings: Settings,
    actor_role: str | None,
    username: str,
    full_name: str,
    email: str | None,
    role: str,
    plain_password: str,
) -> dict[str, Any]:
    role = _assert_role_can_be_managed(actor_role, role, "create")
    if len(plain_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if not username.strip():
        raise HTTPException(status_code=400, detail="Username is required.")

    password_hash = hash_password(plain_password)
    normalized_email = _normalize_email(email)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM app.user_account WHERE username = %s",
                (username.strip(),),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail=f"Username '{username}' is already taken.")
            if normalized_email:
                cur.execute(
                    "SELECT user_id FROM app.user_account WHERE lower(email) = lower(%s)",
                    (normalized_email,),
                )
                if cur.fetchone():
                    raise HTTPException(status_code=409, detail=f"Email '{normalized_email}' is already in use.")

            cur.execute(
                """
                INSERT INTO app.user_account (username, full_name, email, password_hash, is_active)
                VALUES (%s, %s, %s, %s, true)
                RETURNING user_id::text AS user_id
                """,
                (username.strip(), full_name.strip(), normalized_email, password_hash),
            )
            row = cur.fetchone()
            user_id = row["user_id"]

            cur.execute(
                """
                INSERT INTO app.user_role (user_id, role_code)
                VALUES (%s::uuid, %s)
                ON CONFLICT (user_id, role_code) DO NOTHING
                """,
                (user_id, role),
            )
        conn.commit()

    return {
        "user_id": user_id,
        "username": username.strip(),
        "role": role,
        "message": "User created.",
        "user": {
            "user_id": user_id,
            "username": username.strip(),
            "full_name": full_name.strip(),
            "email": normalized_email,
            "is_active": True,
            "role": role,
        },
    }


def update_user(
    settings: Settings,
    actor_role: str | None,
    user_id: str,
    full_name: str,
    email: str | None,
    is_active: bool,
    role: str,
) -> dict[str, Any]:
    role = _assert_role_can_be_managed(actor_role, role, "assign")
    normalized_email = _normalize_email(email)

    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ua.username,
                    ua.full_name,
                    ua.email,
                    ua.is_active,
                    ur.role_code
                FROM app.user_account ua
                LEFT JOIN app.user_role ur USING (user_id)
                WHERE ua.user_id = %s::uuid
                """,
                (user_id,),
            )
            role_rows = cur.fetchall()
            if not role_rows:
                raise HTTPException(status_code=404, detail="User not found.")
            existing_roles = {normalize_role(row["role_code"]) for row in role_rows if row.get("role_code")}
            for existing_role in existing_roles:
                _assert_role_can_be_managed(actor_role, existing_role, "edit")

            if (not is_active) and (SUPERADMIN_ROLE in existing_roles or role == SUPERADMIN_ROLE):
                raise HTTPException(
                    status_code=400,
                    detail="SUPERADMIN accounts cannot be disabled.",
                )

            if normalized_email:
                cur.execute(
                    """
                    SELECT 1
                    FROM app.user_account
                    WHERE user_id <> %s::uuid
                      AND lower(email) = lower(%s)
                    """,
                    (user_id, normalized_email),
                )
                if cur.fetchone():
                    raise HTTPException(status_code=409, detail=f"Email '{normalized_email}' is already in use.")

            cur.execute(
                """
                UPDATE app.user_account
                SET full_name = %s, email = %s, is_active = %s
                WHERE user_id = %s::uuid
                """,
                (full_name.strip(), normalized_email, is_active, user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="User not found.")

            # Replace role
            cur.execute("DELETE FROM app.user_role WHERE user_id = %s::uuid", (user_id,))
            cur.execute(
                """
                INSERT INTO app.user_role (user_id, role_code)
                VALUES (%s::uuid, %s)
                ON CONFLICT (user_id, role_code) DO NOTHING
                """,
                (user_id, role),
            )
        conn.commit()

    first_row = role_rows[0]
    before_user = {
        "user_id": user_id,
        "username": first_row.get("username"),
        "full_name": first_row.get("full_name"),
        "email": first_row.get("email"),
        "is_active": first_row.get("is_active"),
        "role": next(iter(existing_roles), None),
    }
    after_user = {
        "user_id": user_id,
        "username": first_row.get("username"),
        "full_name": full_name.strip(),
        "email": normalized_email,
        "is_active": is_active,
        "role": role,
    }
    return {"user_id": user_id, "message": "User updated.", "before": before_user, "after": after_user}


def delete_user(settings: Settings, actor_role: str | None, user_id: str) -> dict[str, Any]:
    _assert_user_manager(actor_role)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ua.user_id::text AS user_id,
                    ua.username,
                    ua.full_name,
                    ua.email,
                    ua.is_active,
                    ur.role_code AS role
                FROM app.user_account ua
                LEFT JOIN app.user_role ur USING (user_id)
                WHERE ua.user_id = %s::uuid
                LIMIT 1
                """,
                (user_id,),
            )
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="User not found.")
            existing_role = normalize_role(existing.get("role"))
            _assert_role_can_be_managed(actor_role, existing_role, "delete")
            if existing_role == SUPERADMIN_ROLE:
                raise HTTPException(status_code=400, detail="SUPERADMIN accounts cannot be deleted.")
            cur.execute("DELETE FROM app.user_account WHERE user_id = %s::uuid", (user_id,))
        conn.commit()

    deleted_user = dict(existing)
    deleted_user["role"] = existing_role
    return {"user_id": user_id, "message": "User deleted.", "deleted_user": deleted_user}


def reset_user_password(settings: Settings, actor_role: str | None, user_id: str, new_plain_password: str) -> dict[str, Any]:
    _assert_user_manager(actor_role)
    if len(new_plain_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    new_hash = hash_password(new_plain_password)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ua.user_id::text AS user_id,
                    ua.username,
                    ua.full_name,
                    ua.email,
                    ua.is_active,
                    ur.role_code AS role
                FROM app.user_account ua
                LEFT JOIN app.user_role ur USING (user_id)
                WHERE ua.user_id = %s::uuid
                LIMIT 1
                """,
                (user_id,),
            )
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="User not found.")
            _assert_role_can_be_managed(actor_role, existing.get("role"), "reset passwords for")
            cur.execute(
                "UPDATE app.user_account SET password_hash = %s WHERE user_id = %s::uuid",
                (new_hash, user_id),
            )
        conn.commit()

    user = dict(existing)
    user["role"] = normalize_role(user.get("role"))
    return {"message": "Password reset successfully.", "user": user}
