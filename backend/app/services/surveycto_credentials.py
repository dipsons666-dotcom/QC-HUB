from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

import requests
from fastapi import HTTPException

_SURVEYCTO_SESSION_TTL_SECONDS = 2 * 60 * 60


@dataclass
class SurveyCTOSession:
    token: str
    username: str
    password: str
    created_at: float
    expires_at: float


_SESSIONS: dict[str, SurveyCTOSession] = {}


def _prune_sessions() -> None:
    now = time.monotonic()
    expired = [token for token, session in _SESSIONS.items() if session.expires_at <= now]
    for token in expired:
        _SESSIONS.pop(token, None)


def create_surveycto_session(
    server: str,
    surveycto_username: str,
    surveycto_password: str,
    target_form_id: str,
) -> dict[str, str | int]:
    username = surveycto_username.strip()
    password = surveycto_password.strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="SurveyCTO username and password are required.")
    if not server:
        raise HTTPException(status_code=503, detail="SurveyCTO server is not configured.")
    if not target_form_id:
        raise HTTPException(status_code=503, detail="SurveyCTO form ID is not configured.")

    validation_url = f"https://{server}.surveycto.com/api/v2/forms/data/wide/json/{target_form_id}"
    try:
        response = requests.get(
            validation_url,
            auth=(username, password),
            params={"date": "20240101"},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not validate SurveyCTO credentials: {exc}") from exc

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="SurveyCTO rejected those credentials.")
    if response.status_code == 403:
        raise HTTPException(status_code=403, detail="SurveyCTO credentials do not have access to this form.")
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"SurveyCTO credential validation returned HTTP {response.status_code}.",
        )

    _prune_sessions()
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    _SESSIONS[token] = SurveyCTOSession(
        token=token,
        username=username,
        password=password,
        created_at=now,
        expires_at=now + _SURVEYCTO_SESSION_TTL_SECONDS,
    )
    return {"token": token, "expiresInSeconds": _SURVEYCTO_SESSION_TTL_SECONDS}


def resolve_surveycto_credentials(
    session_token: str | None = None,
    request_username: str | None = None,
    request_password: str | None = None,
) -> tuple[str, str]:
    if request_username and request_password:
        return request_username.strip(), request_password.strip()

    if session_token:
        _prune_sessions()
        session = _SESSIONS.get(session_token)
        if session:
            return session.username, session.password
        raise HTTPException(status_code=401, detail="Invalid or expired SurveyCTO session token.")

    env_username = os.getenv("SURVEYCTO_USERNAME", "").strip()
    env_password = os.getenv("SURVEYCTO_PASSWORD", "").strip()
    if env_username and env_password:
        return env_username, env_password

    raise HTTPException(status_code=503, detail="SurveyCTO credentials are required.")
