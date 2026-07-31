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
