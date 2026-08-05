from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.database import get_session_local
from app.main import app
from app.models import IssueQueue, RawSurveyCTOSubmission


def test_review_queue_pages_all_findings_and_uses_context_interviewer_name():
    """The count must describe the whole queue, not just the current page."""
    session = get_session_local()()
    now = datetime.now(timezone.utc)
    try:
        for index in range(3):
            submission_key = f"paged-submission-{index}"
            session.add(RawSurveyCTOSubmission(
                instrument_code="main",
                submission_key=submission_key,
                source_hash=f"source-{index}",
                raw_payload={
                    "answers": {
                        "Interviewer": "49.0",
                        "First_name": "Ada",
                        "Surname": "Okafor",
                    }
                },
            ))
            session.add(IssueQueue(
                instrument_code="main",
                submission_key=submission_key,
                issue_status="pending_review",
                severity="high",
                issue_summary=f"Finding {index}",
                created_at=now + timedelta(seconds=index),
                updated_at=now + timedelta(seconds=index),
            ))
        session.commit()
    finally:
        session.close()

    client = TestClient(app)
    first_page = client.get("/api/qc/review-queue?limit=2&offset=0")
    second_page = client.get("/api/qc/review-queue?limit=2&offset=2")

    assert first_page.status_code == 200
    assert first_page.json()["count"] == 3
    assert len(first_page.json()["issues"]) == 2
    assert len(second_page.json()["issues"]) == 1

    first_issue = first_page.json()["issues"][0]
    assert first_issue["interviewer"] == first_issue["context"]["Interviewer"]
    assert first_issue["interviewer"].startswith("Warri Interviewer 1")
