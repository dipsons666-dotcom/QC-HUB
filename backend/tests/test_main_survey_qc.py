from app.services.main_survey_qc import evaluate_main_survey


def test_matrix_anomalies_are_reported_as_independent_findings():
    """A single case can fail several fields and each failure must survive."""
    rows = [
        (
            f"submission-{index}",
            {
                "answers": {
                    "Interviewer": "enumerator-1",
                    "Gender": "Female",
                    "Age_Range": "25-34",
                    "Sector": "Retail",
                    "P1": "Yes",
                }
            },
        )
        for index in range(10)
    ]

    flags = evaluate_main_survey(rows)
    first_case_flags = [flag for flag in flags if flag["submission_key"] == "submission-0"]

    assert len(first_case_flags) == 4
    assert {flag["finding_key"] for flag in first_case_flags} == {
        "gender", "age_range", "sector", "p1",
    }
    assert "recorded Noodles responses" in first_case_flags[3]["message"]
