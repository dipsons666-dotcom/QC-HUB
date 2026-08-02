from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel

from backend.app.auth import AuthUser, authenticate, get_current_user, hash_password, issue_token, serialize_user, verify_password
from backend.app.database import db_connection
from backend.app.activity_log import log_activity
from backend.app.settings import Settings, get_settings


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    settings: Settings = Depends(get_settings),
):
    user = authenticate(settings, payload.username, payload.password)
    client_ip = request.client.host if request.client else None
    if not user:
        log_activity(
            settings,
            action="login_failure",
            module="auth",
            username=payload.username,
            success=False,
            status="failed",
            description="Login failed.",
            entity_type="session",
            metadata={"username": payload.username},
            request=request,
            device_id=x_device_id,
            client_ip=client_ip,
            error_message="Invalid username or password.",
        )
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    token = issue_token(user)
    log_activity(
        settings,
        action="login_success",
        module="auth",
        user=user,
        description="User logged in.",
        entity_type="session",
        after_value={"authenticated": True},
        metadata={"role": user.role},
        request=request,
        device_id=x_device_id,
        client_ip=client_ip,
    )
    return {
        "token": token,
        "user": serialize_user(user),
    }


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)):
    return {"user": serialize_user(user)}


@router.post("/logout")
def logout(
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    log_activity(
        settings,
        action="logout",
        module="auth",
        user=user,
        description="User logged out.",
        entity_type="session",
        after_value={"authenticated": False},
        request=request,
        device_id=x_device_id,
    )
    return {"message": "Logged out."}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    x_device_id: str | None = Header(default=None, alias="x-device-id"),
    user: AuthUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM app.user_account WHERE user_id = %s::uuid",
                (user.id,),
            )
            row = cur.fetchone()

    if not row or not row.get("password_hash"):
        raise HTTPException(status_code=400, detail="Account has no password set.")
    if not verify_password(payload.current_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")

    new_hash = hash_password(payload.new_password)
    with db_connection(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.user_account SET password_hash = %s WHERE user_id = %s::uuid",
                (new_hash, user.id),
            )
        conn.commit()

    log_activity(
        settings,
        action="password_change",
        module="auth",
        user=user,
        description="Password changed.",
        entity_type="user",
        entity_id=user.id,
        metadata={"username": user.username},
        request=request,
        device_id=x_device_id,
    )
    return {"message": "Password updated successfully."}
