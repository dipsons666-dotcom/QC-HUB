from dataclasses import replace

import pytest
from fastapi import HTTPException

from backend.app.routers.integration import require_integration_key
from backend.app.services.integration import (
    APPROVED_STAGES,
    decode_feed_cursor,
    encode_feed_cursor,
    normalize_approval_stage,
)
from backend.app.settings import get_settings


def test_integration_approved_stages_include_all_agreed_values():
    assert APPROVED_STAGES == {"approved", "reviewed_approved"}


def test_integration_approval_stage_normalization():
    assert normalize_approval_stage("  Reviewed_Approved ") == "reviewed_approved"
    assert normalize_approval_stage("AUTO_APPROVED") == "auto_approved"


def test_integration_cursor_round_trip():
    cursor = encode_feed_cursor("2026-07-21T12:34:56+00:00", "case-123")
    assert decode_feed_cursor(cursor) == ("2026-07-21T12:34:56+00:00", "case-123")


def test_integration_key_accepts_only_matching_bearer_token():
    settings = replace(get_settings(), qc_integration_api_key="shared-secret")
    assert require_integration_key("Bearer shared-secret", None, settings) is None
    with pytest.raises(HTTPException) as exc:
        require_integration_key("Bearer wrong", None, settings)
    assert exc.value.status_code == 401
