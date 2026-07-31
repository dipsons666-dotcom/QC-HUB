from fastapi.testclient import TestClient

from app.main import app


def test_survey_platform_sync_persists_fetched_submissions(monkeypatch):
    def fake_fetch_submission_payloads():
        return [
            {
                "submission_id": "ext-1",
                "project_id": "proj-ext",
                "data": {
                    "case_id": "case-ext",
                    "respondent_name": "Bob",
                    "answers": {"age": 40},
                },
            }
        ]

    monkeypatch.setattr("app.main.fetch_submission_payloads", fake_fetch_submission_payloads)

    client = TestClient(app)
    response = client.post("/api/import/survey-platform/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "synced"
    assert body["source"] == "survey_platform"
    assert body["stored"] == 1
