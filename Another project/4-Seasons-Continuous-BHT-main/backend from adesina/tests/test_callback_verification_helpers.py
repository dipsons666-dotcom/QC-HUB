from backend.app.services.main_survey_cases import (
    _answer_label_for_variable,
    _is_multiselect_child,
    _multiselect_answer_label,
)


def test_is_multiselect_child_detects_parent_suffix_pattern():
    assert _is_multiselect_child("F4a_1", {"F4a", "F4a_1"}) is True
    assert _is_multiselect_child("F4a", {"F4a", "F4a_1"}) is False


def test_multiselect_answer_label_uses_selected_child_labels():
    record = {"F4a_1": "1.0", "F4a_2": "0.0", "F4a_3": "1"}
    section_rows = [
        {"variable": "F4a", "label": "F4a parent"},
        {"variable": "F4a_1", "label": "Saving money"},
        {"variable": "F4a_2", "label": "Food"},
        {"variable": "F4a_3", "label": "Health"},
    ]

    assert _multiselect_answer_label(record, "F4a", section_rows) == "Saving money / Health"


def test_answer_label_for_variable_applies_code_labels_for_token_values():
    record = {"E1": "1 2"}
    section_rows = [{"variable": "E1", "label": "Language"}]
    label_maps = {"E1": {"1": "English", "2": "Hausa"}}

    assert _answer_label_for_variable(record, "E1", section_rows, label_maps) == "English / Hausa"
