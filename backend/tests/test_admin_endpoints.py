import json

from fastapi.testclient import TestClient

from app.main import app


def test_can_create_and_list_staff_members():
    client = TestClient(app)

    response = client.post(
        "/api/admin/staff",
        json={"username": "Jane Reviewer", "email": "jane@example.com", "role": "reviewer"},
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


def test_admin_dashboard_reflects_staff_counts():
    client = TestClient(app)

    client.post(
        "/api/admin/staff",
        json={"username": "Admin User", "email": "admin@example.com", "role": "admin"},
    )

    response = client.get("/api/admin/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["staff_count"] == 1
    assert body["raw_submission_count"] == 0
    assert body["issue_count"] == 0
    assert body["pending_review_count"] == 0
    assert body["high_severity_count"] == 0
    assert body["medium_severity_count"] == 0
