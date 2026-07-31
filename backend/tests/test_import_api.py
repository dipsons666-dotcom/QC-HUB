import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_import_surveycto_accepts_payload_and_persists_raw_data(tmp_path, monkeypatch):
    store_path = tmp_path / "raw_imports.jsonl"
    queue_path = tmp_path / "import_queue.jsonl"
    monkeypatch.setenv("RAW_IMPORT_STORE_PATH", str(store_path))
    monkeypatch.setenv("IMPORT_QUEUE_PATH", str(queue_path))

    client = TestClient(app)
    payload = {
        "source": "surveycto",
        "project_id": "proj-001",
        "submission_id": "sub-123",
        "data": {
            "case_id": "case-001",
            "respondent_name": "Ada",
            "answers": {"age": 30}
        }
    }

    response = client.post("/api/import/surveycto", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["source"] == "surveycto"
    assert body["submission_id"] == "sub-123"

    stored_lines = store_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(stored_lines) == 1
    stored = json.loads(stored_lines[0])
    assert stored["submission_id"] == "sub-123"
    assert stored["data"]["case_id"] == "case-001"

    queue_lines = queue_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(queue_lines) == 1
    queued = json.loads(queue_lines[0])
    assert queued["submission_id"] == "sub-123"
    assert queued["status"] == "queued"
