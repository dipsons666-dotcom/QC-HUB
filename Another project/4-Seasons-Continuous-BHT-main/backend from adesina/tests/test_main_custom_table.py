from backend.app.services.main_custom_table import (
    QuestionSpecIn,
    _extract_requested_record_values,
    _fetch_question_data,
    _requested_question_codes,
    _requested_section_names,
)


def test_single_code_without_labels_keeps_raw_code_not_question_text():
    rows = [{"case_id": "1", "record": {"E1": "1"}}]
    spec = QuestionSpecIn(id="e1", label="E1. Which languages do you speak fluently?", questionCodes=["E1"])

    data = _fetch_question_data(rows, spec, label_maps={})

    assert data.value_map == {"1": {"1"}}
    assert data.value_order == ["1"]


def test_single_code_multi_select_tokens_use_token_labels():
    rows = [{"case_id": "1", "record": {"E1": "1 2 4"}}]
    spec = QuestionSpecIn(id="e1", label="E1. Which languages do you speak fluently?", questionCodes=["E1"])
    label_maps = {"E1": {"1": "English", "2": "Hausa", "4": "Yoruba"}}

    data = _fetch_question_data(rows, spec, label_maps=label_maps)

    assert data.value_map == {"1": {"English", "Hausa", "Yoruba"}}
    assert data.value_order == ["English", "Hausa", "Yoruba"]


def test_single_code_uses_case_insensitive_record_key_lookup():
    rows = [{"case_id": "1", "record": {"f3": "2"}}]
    spec = QuestionSpecIn(id="f3", label="F3", questionCodes=["F3"])
    label_maps = {"F3": {"2": "Buying/building a house"}}

    data = _fetch_question_data(rows, spec, label_maps=label_maps)

    assert data.value_map == {"1": {"Buying/building a house"}}
    assert data.value_order == ["Buying/building a house"]


def test_requested_question_codes_deduplicates_case_insensitively():
    specs = [
        QuestionSpecIn(id="a", label="A", questionCodes=["D1", "D2"]),
        QuestionSpecIn(id="b", label="B", questionCodes=["d2", "E1"]),
    ]

    assert _requested_question_codes(specs) == ["D1", "D2", "E1"]


def test_requested_section_names_maps_from_section_slug():
    specs = [
        QuestionSpecIn(id="a", label="A", sectionId="household-questions", questionCodes=["D1"]),
        QuestionSpecIn(id="b", label="B", sectionId="demographics", questionCodes=["E1"]),
        QuestionSpecIn(id="c", label="C", sectionId="household-questions", questionCodes=["D2"]),
    ]

    assert _requested_section_names(specs) == ["D. HOUSEHOLD QUESTIONS", "E. DEMOGRAPHICS"]


def test_extract_requested_record_values_matches_keys_case_insensitively():
    record = {"d1": "1", "E1": "2", "unused": "3"}

    assert _extract_requested_record_values(record, ["D1", "e1"]) == {"D1": "1", "e1": "2"}
