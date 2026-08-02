import pandas as pd

from backend.app.services import export_indicators


def test_apply_multiselect_yes_no_indicators_sets_blank_to_no_for_listing(monkeypatch):
    monkeypatch.setattr(
        export_indicators,
        "_load_multiselect_column_patterns",
        lambda settings, instrument_code: [("Q1", ["1", "2"])],
    )

    df = pd.DataFrame(
        [
            {"Q1_1": "Yes", "Q1_2": ""},
            {"Q1_1": None, "Q1_2": "Yes"},
            {"Q1_1": "", "Q1_2": ""},
        ]
    )

    result = export_indicators.apply_multiselect_yes_no_indicators(None, "listing", df)

    assert result["Q1_1"].tolist() == ["Yes", "No", "No"]
    assert result["Q1_2"].tolist() == ["No", "Yes", "No"]


def test_apply_multiselect_yes_no_indicators_uses_main_suffix_fallback_when_metadata_missing(monkeypatch):
    monkeypatch.setattr(
        export_indicators,
        "_load_multiselect_column_patterns",
        lambda settings, instrument_code: [],
    )

    df = pd.DataFrame(
        [
            {"D3_1": "Yes", "D3_2": "", "age_1": "23"},
            {"D3_1": "", "D3_2": None, "age_1": "24"},
        ]
    )

    result = export_indicators.apply_multiselect_yes_no_indicators(None, "main", df)

    assert result["D3_1"].tolist() == ["Yes", "No"]
    assert result["D3_2"].tolist() == ["No", "No"]
    assert result["age_1"].tolist() == ["23", "24"]
