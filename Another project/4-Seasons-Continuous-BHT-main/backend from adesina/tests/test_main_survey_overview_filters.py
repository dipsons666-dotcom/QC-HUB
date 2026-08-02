from backend.app.auth import AuthUser
from backend.app.services.main_survey import _extract_record_answers, _overview_approval_clause


def _user(role: str) -> AuthUser:
    return AuthUser(
        id="u1",
        username="tester",
        role=role,
        full_name="Test User",
        email="tester@example.com",
    )


def test_overview_clause_excludes_deleted_for_admin():
    clause, params = _overview_approval_clause(_user("admin"))

    assert "deleted_main_cases" in clause
    assert "submission_key = m.submission_key" in clause
    assert params == []


def test_overview_clause_excludes_deleted_for_client():
    clause, _ = _overview_approval_clause(_user("client"))

    assert "deleted_main_cases" in clause
    assert "approval_stage = 'approved'" not in clause


def test_extract_record_answers_preserves_numeric_looking_free_text():
    assert _extract_record_answers("8000000000", split_multi_value=False, canonicalize_numeric=False) == ["8000000000"]


def test_extract_record_answers_canonicalizes_coded_numeric_answers():
    assert _extract_record_answers("1", split_multi_value=False, canonicalize_numeric=True) == ["1.0"]
