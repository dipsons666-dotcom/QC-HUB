from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from backend.app.auth import AuthUser, require_roles
from backend.app.services.user_management import (
    create_user,
    delete_user,
    list_users,
    list_users_by_role,
    reset_user_password,
    update_user,
)
from backend.app.settings import Settings, get_settings
from backend.app.activity_log import log_activity


router = APIRouter(prefix="/api/admin", tags=["user-management"])

_user_managers_only = require_roles("SUPERADMIN", "PDM-ADMIN")


class CreateUserRequest(BaseModel):
    username: str
    full_name: str
    email: str | None = None
    role: str
    password: str


class UpdateUserRequest(BaseModel):
    full_name: str
    email: str | None = None
    is_active: bool
    role: str


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.get("/users")
def get_users(
    _user: AuthUser = Depends(_user_managers_only),
    settings: Settings = Depends(get_settings),
):
    return {"users": list_users(settings, _user.role)}


@router.get("/users/by-role/{role}")
def get_users_by_role(
    role: str,
    _user: AuthUser = Depends(require_roles("SUPERADMIN", "INICIO-ADMIN", "PDM-ADMIN", "PDM-QC")),
    settings: Settings = Depends(get_settings),
):
    return {"users": list_users_by_role(settings, role)}


@router.post("/users")
def add_user(
    payload: CreateUserRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    _user: AuthUser = Depends(_user_managers_only),
    settings: Settings = Depends(get_settings),
):
    result = create_user(
        settings,
        _user.role,
        payload.username,
        payload.full_name,
        payload.email,
        payload.role,
        payload.password,
    )
    log_activity(
        settings,
        action="user_created",
        module="admin",
        user=_user,
        description=f"Created user {payload.username}.",
        entity_type="user",
        entity_id=str(result.get("user_id") or ""),
        after_value=result.get("user"),
        metadata={"username": payload.username, "role": payload.role},
        request=request,
        device_id=x_device_id,
    )
    return result


@router.patch("/users/{user_id}")
def edit_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    _user: AuthUser = Depends(_user_managers_only),
    settings: Settings = Depends(get_settings),
):
    result = update_user(settings, _user.role, user_id, payload.full_name, payload.email, payload.is_active, payload.role)
    before_user = result.get("before") or {}
    after_user = result.get("after") or {}
    action = "user_reactivated" if before_user.get("is_active") is False and after_user.get("is_active") is True else "user_deactivated" if before_user.get("is_active") is True and after_user.get("is_active") is False else "user_edited"
    log_activity(
        settings,
        action=action,
        module="admin",
        user=_user,
        description=f"Updated user {after_user.get('username') or user_id}.",
        entity_type="user",
        entity_id=user_id,
        before_value=before_user,
        after_value=after_user,
        metadata={"role_changed": before_user.get("role") != after_user.get("role")},
        request=request,
        device_id=x_device_id,
    )
    if before_user.get("role") != after_user.get("role"):
        log_activity(
            settings,
            action="role_change",
            module="admin",
            user=_user,
            description=f"Changed role for user {after_user.get('username') or user_id}.",
            entity_type="user",
            entity_id=user_id,
            before_value={"role": before_user.get("role")},
            after_value={"role": after_user.get("role")},
            request=request,
            device_id=x_device_id,
        )
    return result


@router.delete("/users/{user_id}")
def remove_user(
    user_id: str,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    _user: AuthUser = Depends(_user_managers_only),
    settings: Settings = Depends(get_settings),
):
    result = delete_user(settings, _user.role, user_id)
    log_activity(
        settings,
        action="user_deleted",
        module="admin",
        user=_user,
        description=f"Deleted user {result.get('deleted_user', {}).get('username') or user_id}.",
        entity_type="user",
        entity_id=user_id,
        before_value=result.get("deleted_user"),
        request=request,
        device_id=x_device_id,
    )
    return result


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: str,
    payload: ResetPasswordRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    _user: AuthUser = Depends(_user_managers_only),
    settings: Settings = Depends(get_settings),
):
    result = reset_user_password(settings, _user.role, user_id, payload.new_password)
    log_activity(
        settings,
        action="reset_password_triggered",
        module="admin",
        user=_user,
        description=f"Triggered password reset for user {result.get('user', {}).get('username') or user_id}.",
        entity_type="user",
        entity_id=user_id,
        metadata={"target_username": result.get("user", {}).get("username")},
        request=request,
        device_id=x_device_id,
    )
    return result
