from app.services.table_engine import build_question_catalog, build_tables


def test_build_tables_calculates_n_and_percentages_with_region_cuts() -> None:
    metadata = {
        "questions": {
            "N_BAU1A": {"type": "select_one noodle_brand", "label": "Top-of-mind brand"},
            "City_1": {"type": "select_one city", "label": "Region"},
        },
        "lists": {
            "noodle_brand": {"1": "Indomie", "2": "Golden Penny"},
            "city": {"1": "Lagos", "2": "Abuja"},
        },
    }
    payloads = [
        {"answers": {"N_BAU1A": "1", "City_1": "1"}},
        {"answers": {"N_BAU1A": "1", "City_1": "2"}},
        {"answers": {"N_BAU1A": "2", "City_1": "1"}},
    ]

    tables = build_tables(payloads, metadata, category="noodles")

    table = tables[0]
    assert table["id"] == "N_BAU1A"
    assert table["base"] == 3
    assert table["rows"] == [
        {"label": "Indomie", "count": 2, "pct": 66.7},
        {"label": "Golden Penny", "count": 1, "pct": 33.3},
    ]
    lagos = next(group for group in table["cuts"][0]["groups"] if group["label"] == "Lagos")
    assert lagos == {"label": "Lagos", "base": 2, "counts": {"Indomie": 1, "Golden Penny": 1}}


def test_build_tables_uses_the_requested_filter_field() -> None:
    metadata = {
        "questions": {
            "N_BAU1A": {"type": "select_one noodle_brand", "label": "Top-of-mind brand"},
            "Gender": {"type": "select_one gender", "label": "Gender"},
        },
        "lists": {
            "noodle_brand": {"1": "Indomie", "2": "Golden Penny"},
            "gender": {"1": "Male", "2": "Female"},
        },
    }
    payloads = [
        {"answers": {"N_BAU1A": "1", "Gender": "1"}},
        {"answers": {"N_BAU1A": "2", "Gender": "2"}},
    ]

    table = build_tables(payloads, metadata, category="noodles", cut_fields=["Gender"])[0]

    assert table["cuts"][0]["field"] == "Gender"
    assert [group["label"] for group in table["cuts"][0]["groups"]] == ["Male", "Female"]


def test_build_question_catalog_uses_parent_labels_for_followup_fields() -> None:
    metadata = {
        "questions": {
            "ML_BAU4.98.1": {
                "type": "select_multiple ML_bau4",
                "label": "ML_BAU4.  And where have you seen or heard any advertisement/communication about <span style=\"color:blue\">Others</span>.",
            },
            "ML_BAU4.98.1_OTH": {"type": "text", "label": "Others"},
            "ML_BAU4.1": {"type": "select_one ML_bau4", "label": "ML_BAU4.  Which brand do you buy most often?"},
            "bau4_ml_1": {
                "type": "calculate",
                "label": "Maltina",
                "calculation": "concat(${ML_BAU4.1}, ' ', ${ML_BAU4.1})",
            },
            "note_DH_qbi": {"type": "note", "label": "DH_QBI. Now we would like your impressions."},
            "DH_QBI.1": {"type": "select_multiple DH_bau", "label": "DH_QBI.1. Which brand is best?"},
            "Gender": {"type": "select_one gender", "label": "Gender"},
            "Sector": {"type": "select_one sector", "label": "Sector"},
        },
        "lists": {},
    }

    catalog = build_question_catalog(metadata)
    labels = {name: label for name, label in catalog}

    assert "ML_BAU4.98.1_OTH" not in labels
    assert "bau4_ml_1" not in labels
    assert "note_DH_qbi" not in labels
    assert "Gender" not in labels
    assert "Sector" not in labels
    assert labels["DH_QBI.1"] == "Which brand is best?"


def test_build_question_catalog_excludes_screener_and_metadata_questions() -> None:
    metadata = {
        "questions": {
            "PP_1": {"type": "select_one pp", "label": "PP_1 label"},
            "Screener": {"type": "text", "label": "Screener label"},
            "Intro1": {"type": "text", "label": "Intro 1 label"},
            "Consent1": {"type": "text", "label": "Consent 1 label"},
            "MAIN_INT": {"type": "text", "label": "Main intro label"},
            "Q1a": {"type": "text", "label": "Q1a label"},
            "N_BAU1A": {"type": "select_one noodle_brand", "label": "Top-of-mind brand"},
        },
        "lists": {},
    }

    catalog = build_question_catalog(metadata)
    names = {name for name, _ in catalog}

    assert "PP_1" not in names
    assert "Screener" not in names
    assert "Intro1" not in names
    assert "Consent1" not in names
    assert "MAIN_INT" not in names
    assert "Q1a" not in names
    assert "N_BAU1A" in names


def test_build_question_catalog_keeps_only_choice_questions() -> None:
    metadata = {
        "questions": {
            "N_BAU1A": {"type": "select_one brands", "label": "Brand"},
            "N_BAU1B": {"type": "select_multiple brands", "label": "Brands"},
            "N_BAU1A_OTH": {"type": "text", "label": "Other brand"},
            "score": {"type": "calculate", "label": "Score"},
            "Age": {"type": "integer", "label": "Age"},
        },
        "lists": {},
    }

    names = {name for name, _ in build_question_catalog(metadata)}

    assert names == {"N_BAU1A", "N_BAU1B"}


def test_build_question_catalog_omits_questions_absent_from_imported_data() -> None:
    metadata = {
        "questions": {
            "Active": {"type": "select_one choices", "label": "Active question"},
            "FutureWave": {"type": "select_one choices", "label": "Future-wave question"},
            "Gender": {"type": "select_one gender", "label": "Gender"},
        },
        "lists": {},
    }

    catalog = build_question_catalog(metadata, available_fields=["active", "GENDER"])

    assert catalog == [("Active", "Active question")]


def test_build_tables_matches_submission_fields_case_insensitively() -> None:
    metadata = {
        "questions": {
            "N_BAU1a": {"type": "select_one brands", "label": "Brand"},
            "Gender": {"type": "select_one gender", "label": "Gender"},
        },
        "lists": {
            "brands": {"1": "Indomie", "2": "Golden Penny"},
            "gender": {"1": "Male", "2": "Female"},
        },
    }
    payloads = [{"answers": {"N_BAU1A": "1", "gender": "2"}}]

    table = build_tables(payloads, metadata, registry=[("N_BAU1A", "Brand")], cut_fields=["Gender"])[0]

    assert table["base"] == 1
    assert table["rows"] == [
        {"label": "Indomie", "count": 1, "pct": 100.0},
        {"label": "Golden Penny", "count": 0, "pct": 0.0},
    ]
    assert table["cuts"][0]["groups"] == [
        {"label": "Male", "base": 0, "counts": {}},
        {"label": "Female", "base": 1, "counts": {"Indomie": 1}},
    ]


def test_build_tables_counts_each_question_option_for_each_filter_choice() -> None:
    metadata = {
        "questions": {
            "Brand": {"type": "select_one brands", "label": "Brand"},
            "Gender": {"type": "select_one gender", "label": "Gender"},
        },
        "lists": {
            "brands": {"1": "Indomie", "2": "Golden Penny"},
            "gender": {"1": "Male", "2": "Female"},
        },
    }
    payloads = [
        {"answers": {"Brand": "1", "Gender": "1"}},
        {"answers": {"Brand": "2", "Gender": "2"}},
        {"answers": {"Brand": "1", "Gender": "2"}},
    ]

    table = build_tables(payloads, metadata, registry=[("Brand", "Brand")], cut_fields=["Gender"])[0]

    assert table["rows"] == [
        {"label": "Indomie", "count": 2, "pct": 66.7},
        {"label": "Golden Penny", "count": 1, "pct": 33.3},
    ]
    assert table["cuts"][0]["groups"] == [
        {"label": "Male", "base": 1, "counts": {"Indomie": 1}},
        {"label": "Female", "base": 2, "counts": {"Golden Penny": 1, "Indomie": 1}},
    ]
