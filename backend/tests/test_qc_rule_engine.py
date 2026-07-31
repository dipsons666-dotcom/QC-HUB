import json

from fastapi.testclient import TestClient

from app.main import app


def test_qc_rule_evaluation_creates_result_for_missing_name(tmp_path, monkeypatch):
    raw_path = tmp_path / "raw_imports.jsonl"
    queue_path = tmp_path / "import_queue.jsonl"
    processed_path = tmp_path / "processed_imports.jsonl"
    transformed_path = tmp_path / "transformed_cases.jsonl"
    rule_path = tmp_path / "rule_definitions.jsonl"
    qc_result_path = tmp_path / "qc_results.jsonl"

    monkeypatch.setenv("RAW_IMPORT_STORE_PATH", str(raw_path))
    monkeypatch.setenv("IMPORT_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("PROCESSED_IMPORT_STORE_PATH", str(processed_path))
    monkeypatch.setenv("TRANSFORMED_CASE_STORE_PATH", str(transformed_path))
    monkeypatch.setenv("RULE_DEFINITION_STORE_PATH", str(rule_path))
    monkeypatch.setenv("QC_RESULT_STORE_PATH", str(qc_result_path))

    client = TestClient(app)

    payload = {
        "source": "surveycto",
        "project_id": "proj-qc",
        "submission_id": "sub-qc-1",
        "data": {
            "case_id": "case-qc-1",
            "answers": {"age": 35},
            "gps": {"lat": 1.23, "lon": 4.56},
        },
    }

    import_response = client.post("/api/import/surveycto", json=payload)
    assert import_response.status_code == 202

    process_response = client.post("/api/import/process-next")
    assert process_response.status_code == 200

    transform_response = client.post("/api/import/transform-next")
    assert transform_response.status_code == 200

    rule_response = client.post(
        "/api/qc/rules",
        json={
            "name": "missing_respondent_name",
            "description": "Flag cases where respondent_name is missing",
            "field": "respondent_name",
            "severity": "high",
            "operator": "is_empty",
        },
    )
    assert rule_response.status_code == 200
    body = rule_response.json()
    assert body["name"] == "missing_respondent_name"

    evaluate_response = client.post("/api/qc/evaluate-next")
    assert evaluate_response.status_code == 200
    evaluation = evaluate_response.json()
    assert evaluation["status"] == "flagged"
    assert evaluation["rule_name"] == "missing_respondent_name"
    assert evaluation["submission_id"] == "sub-qc-1"

    result_lines = qc_result_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(result_lines) == 1
    result = json.loads(result_lines[0])
    assert result["submission_id"] == "sub-qc-1"
    assert result["passed"] is False
