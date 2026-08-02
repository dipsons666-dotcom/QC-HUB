import csv
import tempfile
from pathlib import Path

import duckdb

from app import main


def test_export_raw_data_table_from_duckdb_creates_csv(tmp_path: Path):
    duckdb_path = tmp_path / "qc_hub.duckdb"
    csv_path = tmp_path / "raw-data.csv"

    con = duckdb.connect(str(duckdb_path))
    con.execute(
        "CREATE TABLE raw_surveycto_submission (submission_key VARCHAR, instrument_code VARCHAR, source_hash VARCHAR, fetched_at TIMESTAMP, raw_payload VARCHAR)"
    )
    con.execute(
        "INSERT INTO raw_surveycto_submission VALUES (?, ?, ?, ?, ?)",
        ("sub-1", "main", "hash-1", "2026-08-02 12:00:00", '{"field":"value"}'),
    )
    con.close()

    main.DUCKDB_PATH = duckdb_path
    assert main._export_raw_data_table_from_duckdb(csv_path) is True
    assert csv_path.exists()

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = list(reader)

    assert rows[0] == ["submission_key", "instrument_code", "fetched_at", "source_hash", "raw_payload"]
    assert rows[1][0] == "sub-1"
    assert rows[1][1] == "main"
    assert rows[1][3] == "hash-1"
    assert rows[1][4] == '{"field":"value"}'
