import json

from fastapi.testclient import TestClient

from app.main import app


def test_multiple_rule_types_create_review_queue_entries(tmp_path, monkeypatch):
    raw_path = tmp_path / "raw_imports.jsonl"
    queue_path = tmp_path / "import_queue.jsonl"
    processed_path = tmp_path / "processed_imports.jsonl"
    transformed_path = tmp_path / "transformed_cases.jsonl"
    rule_path = tmp_path / "rule_definitions.jsonl"
    qc_result_path = tmp_path / "qc_results.jsonl"
    issue_queue_path = tmp_path / "issue_queue.jsonl"

    monkeypatch.setenv("RAW_IMPORT_STORE_PATH", str(raw_path))
    monkeypatch.setenv("IMPORT_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("PROCESSED_IMPORT_STORE_PATH", str(processed_path))
    monkeypatch.setenv("TRANSFORMED_CASE_STORE_PATH", str(transformed_path))
    monkeypatch.setenv("RULE_DEFINITION_STORE_PATH", str(rule_path))
    monkeypatch.setenv("QC_RESULT_STORE_PATH", str(qc_result_path))
    monkeypatch.setenv("ISSUE_QUEUE_STORE_PATH", str(issue_queue_path))

    client = TestClient(app)

    import_response = client.post(
        "/api/import/surveycto",
        json={
            "source": "surveycto",
            "project_id": "proj-qc-2",
            "submission_id": "sub-qc-2",
            "data": {
                "case_id": "case-qc-2",
                "answers": {"age": 12},
                "gps": {"lat": 1.23, "lon": 4.56},
            },
        },
    )
    assert import_response.status_code == 202

    process_response = client.post("/api/import/process-next")
    assert process_response.status_code == 200

    transform_response = client.post("/api/import/transform-next")
    assert transform_response.status_code == 200

    missing_name_rule = client.post(
        "/api/qc/rules",
        json={
            "name": "missing_respondent_name",
            "description": "Flag cases where respondent_name is missing",
            "field": "respondent_name",
            "severity": "high",
            "operator": "is_empty",
        },
    )
    assert missing_name_rule.status_code == 200

    low_age_rule = client.post(
        "/api/qc/rules",
        json={
            "name": "age_below_minimum",
            "description": "Flag age below minimum",
            "field": "age",
            "severity": "medium",
            "operator": "min_value",
            "threshold": 18,
        },
    )
    assert low_age_rule.status_code == 200

    evaluate_response = client.post("/api/qc/evaluate-next")
    assert evaluate_response.status_code == 200
    body = evaluate_response.json()
    assert body["status"] == "flagged"
    assert body["issue_count"] == 2
    assert body["passed"] is False

    review_queue_response = client.get("/api/qc/review-queue")
    assert review_queue_response.status_code == 200
    queue_body = review_queue_response.json()
    assert len(queue_body["issues"]) == 2
    assert queue_body["issues"][0]["status"] == "pending_review"
    assert {item["severity"] for item in queue_body["issues"]} == {"high", "medium"}
