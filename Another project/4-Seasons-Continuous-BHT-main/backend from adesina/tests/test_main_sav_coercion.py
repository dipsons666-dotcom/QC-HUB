import pandas as pd

from backend.app.services.main_survey import _coerce_template_dataframe_for_sav


def test_coerce_template_dataframe_for_sav_maps_yes_no_strings_into_numeric_template_codes():
    df = pd.DataFrame([{"D3_1": "Yes", "D3_17": "No", "D3": "1 17"}])

    out, labels = _coerce_template_dataframe_for_sav(
        df,
        ["D3_1", "D3_17", "D3"],
        {"D3_1": "F15.0", "D3_17": "F15.0", "D3": "A40"},
        {
            "D3_1": {0.0: "No", 1.0: "Yes"},
            "D3_17": {0.0: "No", 1.0: "Yes"},
        },
    )

    assert out.at[0, "D3_1"] == 1
    assert out.at[0, "D3_17"] == 0
    assert out.at[0, "D3"] == "1 17"
    assert labels["D3_1"][1] == "Yes"
    assert labels["D3_17"][0] == "No"
