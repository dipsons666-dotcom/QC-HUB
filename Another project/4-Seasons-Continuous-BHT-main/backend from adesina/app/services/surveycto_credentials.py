from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import requests
from fastapi import HTTPException

from backend.app.auth import AuthUser
from backend.app.settings import Settings


SURVEYCTO_SESSION_TTL_SECONDS = 2 * 60 * 60


@dataclass
class SurveyCtoCredentialSession:
    token: str
    user_id: str
    username: str
    password: str
    created_at: float
    expires_at: float


_SESSIONS: dict[str, SurveyCtoCredentialSession] = {}


def _prune_sessions() -> None:
    now = time.monotonic()
    expired = [token for token, session in _SESSIONS.items() if session.expires_at <= now]
    for token in expired:
        _SESSIONS.pop(token, None)


def create_surveycto_session(
    settings: Settings,
    user: AuthUser,
    surveycto_username: str,
    surveycto_password: str,
    form_id: str | None = None,
) -> dict[str, object]:
    username = surveycto_username.strip()
    password = surveycto_password.strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="SurveyCTO username and password are required.")

    target_form_id = form_id or settings.surveycto_main_form_id or settings.surveycto_listing_form_id
    if not target_form_id:
        raise HTTPException(status_code=503, detail="SurveyCTO form ID is not configured.")

    validation_url = (
        f"https://{settings.surveycto_server}.surveycto.com"
        f"/api/v2/forms/data/wide/json/{target_form_id}"
    )
    try:
        response = requests.get(validation_url, auth=(username, password), params={"date": "4102444800000"}, timeout=20)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not validate SurveyCTO credentials: {exc}") from exc

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="SurveyCTO rejected those credentials.")
    if response.status_code == 403:
        raise HTTPException(status_code=403, detail="SurveyCTO credentials do not have access to this form.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"SurveyCTO credential validation returned HTTP {response.status_code}.")

    _prune_sessions()
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    _SESSIONS[token] = SurveyCtoCredentialSession(
        token=token,
        user_id=user.id,
        username=username,
        password=password,
        created_at=now,
        expires_at=now + SURVEYCTO_SESSION_TTL_SECONDS,
    )
    return {"token": token, "expiresInSeconds": SURVEYCTO_SESSION_TTL_SECONDS}


def resolve_surveycto_credentials(
    settings: Settings,
    user: AuthUser,
    session_token: str | None = None,
    request_username: str | None = None,
    request_password: str | None = None,
) -> tuple[str, str]:
    if request_username and request_password:
        return request_username.strip(), request_password.strip()

    if session_token:
        _prune_sessions()
        session = _SESSIONS.get(session_token)
        if session and session.user_id == user.id:
            return session.username, session.password

    if settings.surveycto_username and settings.surveycto_password:
        return settings.surveycto_username, settings.surveycto_password

    raise HTTPException(status_code=503, detail="SurveyCTO credentials are required.")


def resolve_surveycto_credentials_for_media(
    settings: Settings,
    session_token: str | None = None,
) -> tuple[str, str]:
    if session_token:
        _prune_sessions()
        session = _SESSIONS.get(session_token)
        if session:
            return session.username, session.password

    if settings.surveycto_username and settings.surveycto_password:
        return settings.surveycto_username, settings.surveycto_password

    raise HTTPException(status_code=401, detail="Valid SurveyCTO credentials are required for media playback.")
