from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from backend.app.services.integration import get_bht_case_feed
from backend.app.settings import Settings, get_settings


router = APIRouter(prefix="/api/integrations/bht", tags=["integrations"])


def require_integration_key(
    authorization: str | None = Header(default=None),
    x_integration_key: str | None = Header(default=None, alias="x-integration-key"),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = str(settings.qc_integration_api_key or "").strip()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Integration API is not configured.")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    supplied = bearer or str(x_integration_key or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid integration credential.")


@router.get("/case-feed", dependencies=[Depends(require_integration_key)])
def bht_case_feed(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10000),
    settings: Settings = Depends(get_settings),
):
    try:
        return get_bht_case_feed(settings, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
