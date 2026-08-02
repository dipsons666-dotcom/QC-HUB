import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models import ImportJob, RawSurveyCTOSubmission
from app.database import get_session_local


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


def test_queued_imports_endpoint_supports_pagination():
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        for index in range(3):
            raw_submission = RawSurveyCTOSubmission(
                instrument_code="instr-001",
                submission_key=f"sub-{index}",
                source_hash=f"hash-{index}",
                raw_payload={"value": index},
            )
            db.add(raw_submission)
            db.flush()
            db.add(
                ImportJob(
                    submission_key=raw_submission.submission_key,
                    instrument_code=raw_submission.instrument_code,
                    status="queued",
                )
            )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/api/import/queued?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    assert len(payload["items"]) == 2
    assert [item["submission_key"] for item in payload["items"]] == ["sub-0", "sub-1"]
