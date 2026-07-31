import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_transform_processed_import_writes_canonical_case(tmp_path, monkeypatch):
    store_path = tmp_path / "raw_imports.jsonl"
    queue_path = tmp_path / "import_queue.jsonl"
    processed_path = tmp_path / "processed_imports.jsonl"
    transformed_path = tmp_path / "transformed_cases.jsonl"

    monkeypatch.setenv("RAW_IMPORT_STORE_PATH", str(store_path))
    monkeypatch.setenv("IMPORT_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("PROCESSED_IMPORT_STORE_PATH", str(processed_path))
    monkeypatch.setenv("TRANSFORMED_CASE_STORE_PATH", str(transformed_path))

    client = TestClient(app)

    payload = {
        "source": "surveycto",
        "project_id": "proj-001",
        "submission_id": "sub-789",
        "data": {
            "case_id": "case-789",
            "respondent_name": "Grace",
            "answers": {"age": 28, "gender": "female"},
            "gps": {"lat": 6.5, "lon": 3.4},
        },
    }

    import_response = client.post("/api/import/surveycto", json=payload)
    assert import_response.status_code == 202

    process_response = client.post("/api/import/process-next")
    assert process_response.status_code == 200

    transform_response = client.post("/api/import/transform-next")
    assert transform_response.status_code == 200
    body = transform_response.json()
    assert body["status"] == "transformed"
    assert body["submission_id"] == "sub-789"

    transformed_lines = transformed_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(transformed_lines) == 1
    transformed = json.loads(transformed_lines[0])
    assert transformed["submission_id"] == "sub-789"
    assert transformed["case_id"] == "case-789"
    assert transformed["respondent_name"] == "Grace"
    assert transformed["age"] == 28
    assert transformed["gender"] == "female"
