from fastapi.testclient import TestClient

from app.main import app


def test_raw_surveycto_submissions_are_exposed_for_viewing(monkeypatch):
    client = TestClient(app)

    monkeypatch.setattr(
        "app.main.fetch_submission_payloads",
        lambda: [
            {
                "submission_id": "sub-001",
                "instrument_code": "main",
                "data": {"respondent_name": "Ada", "answers": {"age": 32}},
            }
        ],
    )

    sync_response = client.post("/api/import/survey-platform/sync")
    assert sync_response.status_code == 200

    list_response = client.get("/api/import/survey-platform/raw")
    assert list_response.status_code == 200

    body = list_response.json()
    assert body["count"] == 1
    assert body["items"][0]["submission_key"] == "sub-001"
    assert body["items"][0]["instrument_code"] == "main"
    assert body["items"][0]["raw_payload"]["respondent_name"] == "Ada"


def test_raw_surveycto_submissions_are_available_as_table_data(monkeypatch):
    client = TestClient(app)

    monkeypatch.setattr(
        "app.main.fetch_submission_payloads",
        lambda: [
            {
                "submission_id": "sub-002",
                "instrument_code": "main",
                "data": {"respondent_name": "Grace", "answers": {"age": 29}},
            }
        ],
    )

    client.post("/api/import/survey-platform/sync")

    table_response = client.get("/api/admin/raw-data-table")
    assert table_response.status_code == 200

    body = table_response.json()
    assert body["count"] == 1
    assert body["columns"][0]["name"] == "submission_key"
    assert body["rows"][0]["submission_key"] == "sub-002"
    assert body["rows"][0]["respondent_name"] == "Grace"
    assert body["rows"][0]["answers.age"] == 29
