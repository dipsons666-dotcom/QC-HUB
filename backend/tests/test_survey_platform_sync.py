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


def test_survey_platform_sync_accepts_surveycto_credentials_payload(monkeypatch):
    def fake_fetch_submission_payloads(config=None):
        assert config is not None
        assert config["server"] == "testserver"
        assert config["username"] == "testuser"
        assert config["password"] == "testpass"
        assert config["form_id"] == "test_form"
        return [
            {
                "submission_id": "sub-001",
                "instrument_code": "main",
                "data": {"respondent_name": "Ada", "answers": {"age": 32}},
            }
        ]

    monkeypatch.setattr("app.main.fetch_submission_payloads", fake_fetch_submission_payloads)

    client = TestClient(app)
    response = client.post(
        "/api/import/survey-platform/sync",
        json={
            "surveyctoServer": "testserver",
            "surveyctoUsername": "testuser",
            "surveyctoPassword": "testpass",
            "formId": "test_form",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "synced"
    assert body["source"] == "survey_platform"
    assert body["stored"] == 1


def test_create_surveycto_session_endpoint(monkeypatch):
    def fake_create_surveycto_session(server, surveycto_username, surveycto_password, target_form_id):
        assert server == "testserver"
        assert surveycto_username == "testuser"
        assert surveycto_password == "testpass"
        assert target_form_id == "test_form"
        return {"token": "abc123", "expiresInSeconds": 7200}

    monkeypatch.setattr("app.main.create_surveycto_session", fake_create_surveycto_session)

    client = TestClient(app)
    response = client.post(
        "/api/surveycto/session",
        json={
            "surveyctoServer": "testserver",
            "surveyctoUsername": "testuser",
            "surveyctoPassword": "testpass",
            "formId": "test_form",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"token": "abc123", "expiresInSeconds": 7200}
