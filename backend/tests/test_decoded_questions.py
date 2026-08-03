from pathlib import Path

from app.decoded_questions import decode_submission_to_question_rows


def test_decode_submission_uses_workbook_labels() -> None:
    workbook_path = Path(__file__).resolve().parents[1] / "BHT+4+SEASONS+JULY+2026.xlsx"

    rows = decode_submission_to_question_rows(
        {"City_1": 1, "N_QC1.1": "1 2"},
        workbook_path=workbook_path,
    )

    assert any(row["question"] == "REGION:" and row["response"] == "Lagos" for row in rows)
    assert any(row["category"] for row in rows)


def test_decode_submission_uses_metadata_json_without_workbook() -> None:
    metadata_path = Path(__file__).resolve().parents[1] / "data" / "xlsform_metadata.json"

    rows = decode_submission_to_question_rows(
        {"City_1": 1, "Sector": 2},
        metadata_path=metadata_path,
    )

    assert any(row["question"] == "REGION:" and row["response"] == "Lagos" for row in rows)
    assert any(row["question"] == "SECTOR:" for row in rows)
