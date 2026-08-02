import pandas as pd
import json
import pyreadstat
from pathlib import Path

from backend.app.services import export_indicators
from backend.app.services.listing import _prepare_listing_delivery_dataframe, _save_sav_from_template


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


def test_apply_multiselect_yes_no_indicators_maps_choice_code_tokens_to_yes(monkeypatch):
    monkeypatch.setattr(
        export_indicators,
        "_load_multiselect_column_patterns",
        lambda settings, instrument_code: [("D3", ["1", "17"])],
    )

    df = pd.DataFrame(
        [
            {"D3_1": "1", "D3_17": "17"},
            {"D3_1": "", "D3_17": None},
            {"D3_1": "Yes", "D3_17": "17.0"},
        ]
    )

    result = export_indicators.apply_multiselect_yes_no_indicators(None, "main", df)

    assert result["D3_1"].tolist() == ["Yes", "No", "Yes"]
    assert result["D3_17"].tolist() == ["Yes", "No", "Yes"]


def test_prepare_listing_delivery_dataframe_backfills_template_metadata():
    df = pd.DataFrame(
        [
            {
                "submission_key": "uuid:abc",
                "_sampling_completion_date": "2026-05-12T10:00:00Z",
                "_sampling_submission_date": "2026-05-12T09:55:00Z",
                "_sampling_boundary_id": "poly-1",
                "nbld": "12",
                "form_def_version": "20260501",
                "device_phone_number": "08030000000",
                "case_id": "case-1",
                "Duration": "270",
            }
        ]
    )

    result = _prepare_listing_delivery_dataframe(None, "listing_long", df)

    assert result.loc[0, "CompletionDate"] == "2026-05-12T10:00:00Z"
    assert result.loc[0, "SubmissionDate"] == "2026-05-12T09:55:00Z"
    assert result.loc[0, "polygon_id"] == "poly-1"
    assert result.loc[0, "NBLD"] == "12"
    assert result.loc[0, "formdef_version"] == "20260501"
    assert result.loc[0, "devicephonenum"] == "08030000000"
    assert result.loc[0, "caseid"] == "case-1"
    assert result.loc[0, "duration"] == "270"
    assert result.loc[0, "KEY"] == "uuid:abc"


def test_prepare_listing_delivery_dataframe_expands_record_json_metadata():
    df = pd.DataFrame(
        [
            {
                "submission_key": "uuid:abc",
                "building_no": 7,
                "record": json.dumps(
                    {
                        "completiondate": "4/29/2026 16:26",
                        "submissiondate": "4/29/2026 16:26",
                        "devicephonenum": 2348140000000,
                        "duration": 9066,
                        "caseid": 2530,
                        "polygon_id": 2530,
                        "nbld": 30,
                        "formdef_version": 2604231523,
                        "key": "uuid:abc",
                        "structure_no": 7,
                    }
                ),
            }
        ]
    )

    result = _prepare_listing_delivery_dataframe(None, "listing_long", df)

    assert result.loc[0, "CompletionDate"] == "4/29/2026 16:26"
    assert result.loc[0, "SubmissionDate"] == "4/29/2026 16:26"
    assert result.loc[0, "devicephonenum"] == 2348140000000
    assert result.loc[0, "duration"] == 9066
    assert result.loc[0, "caseid"] == 2530
    assert result.loc[0, "polygon_id"] == 2530
    assert result.loc[0, "NBLD"] == 30
    assert result.loc[0, "formdef_version"] == 2604231523
    assert result.loc[0, "KEY"] == "uuid:abc"
    assert result.loc[0, "structure_no"] == 7


def test_listing_template_sav_preserves_string_metadata(tmp_path):
    df = pd.DataFrame(
        [
            {
                "CompletionDate": "4/29/2026 16:26",
                "SubmissionDate": "4/29/2026 16:26",
                "formdef_version": 2604231523,
                "polygon_id": 2530.0,
                "duration": 9066,
                "caseid": 2530.0,
                "devicephonenum": 2348140000000.0,
            }
        ]
    )
    path = tmp_path / "listing.sav"

    _save_sav_from_template(df, path, Path("HH_listing_Export_Template.sav"))

    reread, meta = pyreadstat.read_sav(str(path), apply_value_formats=False)
    assert reread.loc[0, "formdef_version"] == "2604231523"
    assert reread.loc[0, "polygon_id"] == "2530"
    assert reread.loc[0, "duration"] == "9066"
    assert reread.loc[0, "caseid"] == "2530"
    assert reread.loc[0, "devicephonenum"] == "2348140000000"
    assert meta.original_variable_types["formdef_version"] == "A10"
    assert meta.original_variable_types["polygon_id"] == "A4"
    assert meta.original_variable_types["duration"] == "A4"
    assert meta.original_variable_types["caseid"] == "A5"
    assert meta.original_variable_types["devicephonenum"] == "A14"


def test_listing_template_sav_preserves_start_end_datetimes(tmp_path):
    df = pd.DataFrame(
        [
            {
                "CompletionDate": "4/29/2026 16:26",
                "SubmissionDate": "4/29/2026 16:26",
                "start": "Wed Apr 29 2026 12:18:24 GMT+0100 (West Africa Standard Time)",
                "end": "Wed Apr 29 2026 15:02:43 GMT+0100 (West Africa Standard Time)",
            }
        ]
    )
    path = tmp_path / "listing_dates.sav"

    _save_sav_from_template(df, path, Path("HH_listing_Export_Template.sav"))

    reread, _ = pyreadstat.read_sav(str(path), apply_value_formats=False)
    assert str(reread.loc[0, "start"]) == "2026-04-29 12:18:24"
    assert str(reread.loc[0, "end"]) == "2026-04-29 15:02:43"
    assert str(reread.loc[0, "CompletionDate"]) == "2026-04-29 16:26:00"
    assert str(reread.loc[0, "SubmissionDate"]) == "2026-04-29 16:26:00"
