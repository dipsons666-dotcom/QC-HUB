import os

from app.database import get_engine
from app.main import sync_survey_platform_submissions
from app.main import app

if __name__ == "__main__":
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post("/api/import/survey-platform/sync")
        print(response.json())
