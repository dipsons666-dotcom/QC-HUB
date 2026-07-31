import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_process_queue_moves_submission_to_processing(tmp_path, monkeypatch):
    store_path = tmp_path / "raw_imports.jsonl"
    queue_path = tmp_path / "import_queue.jsonl"
    processed_path = tmp_path / "processed_imports.jsonl"

    monkeypatch.setenv("RAW_IMPORT_STORE_PATH", str(store_path))
    monkeypatch.setenv("IMPORT_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("PROCESSED_IMPORT_STORE_PATH", str(processed_path))

    client = TestClient(app)

    payload = {
        "source": "surveycto",
        "project_id": "proj-001",
        "submission_id": "sub-456",
        "data": {"case_id": "case-456"},
    }
    import_response = client.post("/api/import/surveycto", json=payload)
    assert import_response.status_code == 202

    process_response = client.post("/api/import/process-next")
    assert process_response.status_code == 200
    body = process_response.json()
    assert body["status"] == "processed"
    assert body["submission_id"] == "sub-456"

    queue_lines = queue_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(queue_lines) == 0

    processed_lines = processed_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(processed_lines) == 1
    processed = json.loads(processed_lines[0])
    assert processed["submission_id"] == "sub-456"
    assert processed["status"] == "processed"
