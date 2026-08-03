from app.services.table_engine import build_tables


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
    assert [group["label"] for group in table["cuts"][0]["groups"]] == ["Female", "Male"]
