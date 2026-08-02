import pandas as pd
import pyreadstat

from backend.app.services.main_survey import (
    _coerce_template_dataframe_for_sav,
    _prepare_main_delivery_dataframe,
    _write_sav_with_fallback,
)


def test_main_survey_sav_coerces_template_string_columns(tmp_path):
    df = pd.DataFrame(
        {
            "caseid": [2530],
            "duration": [9066],
        }
    )

    out, _ = _coerce_template_dataframe_for_sav(
        df,
        ["caseid", "duration"],
        {"caseid": "A15", "duration": "F15.0"},
        {},
    )

    assert out.loc[0, "caseid"] == "2530"
    assert out.loc[0, "duration"] == 9066

    path = tmp_path / "main.sav"
    _write_sav_with_fallback(
        out,
        path,
        {
            "column_labels": ["Case ID", "Duration"],
            "variable_format": {"caseid": "A15", "duration": "F15.0"},
        },
    )

    reread, meta = pyreadstat.read_sav(str(path), apply_value_formats=False)
    assert reread.loc[0, "caseid"] == "2530"
    assert reread.loc[0, "duration"] == 9066
    assert meta.original_variable_types["caseid"] == "A15"


def test_main_survey_sav_drops_nested_series_cell_values(tmp_path):
    duration = pd.Series([1454, 1345], name="duration")
    df = pd.DataFrame(
        {
            "caseid": [duration],
            "duration": [1454],
        }
    )

    out, _ = _coerce_template_dataframe_for_sav(
        df,
        ["caseid", "duration"],
        {"caseid": "A15", "duration": "F15.0"},
        {},
    )

    path = tmp_path / "main_nested.sav"
    _write_sav_with_fallback(
        out,
        path,
        {
            "column_labels": ["Case ID", "Duration"],
            "variable_format": {"caseid": "A15", "duration": "F15.0"},
        },
    )

    reread, _ = pyreadstat.read_sav(str(path), apply_value_formats=False)
    assert reread.loc[0, "caseid"] == ""
    assert reread.loc[0, "duration"] == 1454


def test_main_delivery_dataframe_preserves_template_note_variables(monkeypatch, tmp_path):
    class Settings:
        root_dir = tmp_path

    monkeypatch.setattr(
        "backend.app.services.main_survey._main_export_template_meta",
        lambda root_dir: {"column_names": ["caseid", "HHIFO", "C_intro"]},
    )
    monkeypatch.setattr(
        "backend.app.services.main_survey._main_export_column_meta",
        lambda root_dir: (["caseid", "HHIFO", "C_intro"], {}, {}, {}, {}, {}),
    )

    df = pd.DataFrame([{"caseid": "2530", "HHIFO": "note value", "C_intro": "intro value"}])

    out = _prepare_main_delivery_dataframe(Settings(), df)

    assert out.loc[0, "HHIFO"] == "note value"
    assert out.loc[0, "C_intro"] == "intro value"
