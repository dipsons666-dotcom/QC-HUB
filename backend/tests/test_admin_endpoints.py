import json

from fastapi.testclient import TestClient

from app.database import get_session_local
from app.main import app
from app.models import IssueQueue, RawSurveyCTOSubmission


def test_can_create_and_list_staff_members():
    client = TestClient(app)

    response = client.post(
        "/api/admin/staff",
        json={"username": "Jane Reviewer", "email": "jane@example.com", "password": "safe-password", "role": "reviewer"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "Jane Reviewer"
    assert body["email"] == "jane@example.com"
    assert body["role"] == "reviewer"
    assert "staff_id" in body

    list_response = client.get("/api/admin/staff")
    assert list_response.status_code == 200
    staff_list = list_response.json()
    assert isinstance(staff_list, list)
    assert len(staff_list) == 1
    assert staff_list[0]["email"] == "jane@example.com"


def test_staff_can_login_by_username_or_email():
    client = TestClient(app)

    response = client.post(
        "/api/admin/staff",
        json={"username": "Jane Reviewer", "email": "jane@example.com", "password": "safe-password", "role": "reviewer"},
    )
    assert response.status_code == 201

    username_login = client.post(
        "/api/auth/login",
        json={"username": "Jane Reviewer", "password": "safe-password"},
    )
    assert username_login.status_code == 200
    assert username_login.json()["username"] == "Jane Reviewer"

    email_login = client.post(
        "/api/auth/login",
        json={"username": "jane@example.com", "password": "safe-password"},
    )
    assert email_login.status_code == 200
    assert email_login.json()["username"] == "Jane Reviewer"


def test_admin_dashboard_reflects_staff_counts():
    client = TestClient(app)

    client.post(
        "/api/admin/staff",
        json={"username": "Admin User", "email": "admin@example.com", "password": "safe-password", "role": "admin"},
    )

    response = client.get("/api/admin/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["staff_count"] == 1
    assert body["total_survey_count"] == 0
    assert body["good_survey_count"] == 0
    assert body["outlier_survey_count"] == 0
    assert body["raw_submission_count"] == 0
    assert body["issue_count"] == 0
    assert body["pending_review_count"] == 0
    assert body["high_severity_count"] == 0
    assert body["medium_severity_count"] == 0


def test_dashboard_survey_quality_totals_update_when_an_outlier_is_cleared():
    session = get_session_local()()
    try:
        for index in range(3):
            session.add(RawSurveyCTOSubmission(
                instrument_code="main",
                submission_key=f"survey-{index}",
                source_hash=f"hash-{index}",
                raw_payload={},
            ))
        active_issue = IssueQueue(
            instrument_code="main", submission_key="survey-0", severity="high",
            issue_status="pending_review", issue_summary="Active outlier",
        )
        session.add(active_issue)
        session.add(IssueQueue(
            instrument_code="main", submission_key="survey-1", severity="medium",
            issue_status="resolved", issue_summary="Cleared outlier",
        ))
        session.commit()

        client = TestClient(app)
        before_clear = client.get("/api/admin/dashboard").json()
        assert before_clear["total_survey_count"] == 3
        assert before_clear["outlier_survey_count"] == 1
        assert before_clear["good_survey_count"] == 2

        active_issue.issue_status = "resolved"
        session.commit()
    finally:
        session.close()

    after_clear = TestClient(app).get("/api/admin/dashboard").json()
    assert after_clear["total_survey_count"] == 3
    assert after_clear["outlier_survey_count"] == 0
    assert after_clear["good_survey_count"] == 3
