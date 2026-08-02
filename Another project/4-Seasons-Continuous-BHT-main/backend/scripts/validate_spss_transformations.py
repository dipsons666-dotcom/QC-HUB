#!/usr/bin/env python3
"""Synthetic check for stored Step 1 SPSS transformations."""
from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from surveycto_bht_sync import build_spss_case_tables


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bht-spss-") as temporary:
        root = Path(temporary)
        parquet_dir = root / "spss_case_values_parquet" / "category=Noodles" / "month=2026-06"
        parquet_dir.mkdir(parents=True)
        rows = [
            ("r1", "Region", "3", 3.0, "Abuja"),
            ("r2", "Region", "1", 1.0, "Lagos"),
            ("r3", "City_1", "3", 3.0, "Abuja"),
            ("r1", "D3", "High", None, "High"),
            ("r2", "D3", "Mid", None, "Mid"),
            ("r3", "D3_Q", "Low", None, "Low"),
            ("r1", "Age_cal", "18 - 25 years", None, "18 - 25 years"),
            ("r2", "Age_cal", "26 - 35 years", None, "26 - 35 years"),
            ("r3", "Age_cal", "18 - 25 years", None, "18 - 25 years"),
            ("r1", "SEC", "A", None, "A"),
            ("r2", "SEC", "B", None, "B"),
            ("r3", "SEC", "A", None, "A"),
            ("r1", "Gender", "1.0", 1.0, "Male"),
            ("r2", "Gender", "2", 2.0, "Female"),
            ("r3", "S3BI", "2", 2.0, "Female"),
            ("r1", "Week", "Week 1", None, "Week 1"),
            ("r2", "Week", "Week 2", None, "Week 2"),
            ("r3", "WEEK_TRACKER", "Legacy Week", None, "Legacy Week"),
            ("r1", "N_BAU4.1_1", "1", 1.0, "Yes"),
            ("r2", "N_BAU4.1.1_1", "1", 1.0, "Yes"),
        ]
        frame = pd.DataFrame(rows, columns=["respondent_id", "variable", "value_text", "value_num", "value_label"])
        frame.insert(0, "category", "Noodles")
        frame.insert(2, "file_month", "2026-06")
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), parquet_dir / "part.parquet")

        connection = duckdb.connect(":memory:")
        connection.execute(
            """
            CREATE TABLE respondent_dims(
              category VARCHAR, respondent_id VARCHAR, month VARCHAR,
              Region VARCHAR, D3 VARCHAR, D5 VARCHAR, B1 VARCHAR,
              Gender VARCHAR, Age VARCHAR, SEC VARCHAR, Week VARCHAR,
              SPSS_Region VARCHAR, SPSS_D3 VARCHAR, SPSS_Gender VARCHAR,
              SPSS_Age_cal VARCHAR, SPSS_SEC VARCHAR, SPSS_Week VARCHAR,
              City_1 VARCHAR, D3_Q VARCHAR, S3BI VARCHAR, WEEK_TRACKER VARCHAR
            )
            """
        )
        connection.execute(
            """
            INSERT INTO respondent_dims(category, respondent_id, month, Region, Gender, Age, SEC, Week, City_1, D3_Q, S3BI, WEEK_TRACKER)
            VALUES
              ('Noodles', 'r1', '2026-06', 'Abuja', 'Male', '18 - 25 years', 'A', 'Week 1', NULL, 'High', '2', 'Legacy Week'),
              ('Noodles', 'r2', '2026-06', 'Lagos', 'Female', '26 - 35 years', 'B', 'Week 2', NULL, 'Mid', '1', 'Legacy Week'),
              ('Noodles', 'r3', '2026-06', NULL, NULL, '18 - 25 years', 'A', NULL, '3', 'Low', '2', 'Legacy Week')
            """
        )
        connection.execute(
            """
            CREATE TABLE responses_fact(
              category VARCHAR, respondent_id VARCHAR, month VARCHAR,
              question VARCHAR, question_label VARCHAR, answer_label VARCHAR,
              answer_value VARCHAR, answer_value_num DOUBLE
            )
            """
        )

        counts = build_spss_case_tables(connection, root)
        region2 = connection.execute(
            "SELECT respondent_id, value_text, value_label FROM spss_case_values WHERE lower(variable)='region2' ORDER BY respondent_id"
        ).fetchall()
        dimensions = connection.execute(
            "SELECT respondent_id, REGION2, AGE2, SEC2, AGE2_New, Gender_New FROM respondent_dims ORDER BY respondent_id"
        ).fetchall()
        alias_values = connection.execute(
            """
            SELECT respondent_id,
                   MAX(CASE WHEN lower(variable)='region' THEN value_text END) AS region_value,
                   MAX(CASE WHEN lower(variable)='d3' THEN value_text END) AS income_value,
                   MAX(CASE WHEN lower(variable)='gender' THEN value_text END) AS gender_value,
                   MAX(CASE WHEN lower(variable)='week' THEN value_text END) AS week_value
            FROM spss_case_values
            GROUP BY respondent_id
            ORDER BY respondent_id
            """
        ).fetchall()
        bau4_count = connection.execute(
            "SELECT COUNT(*) FROM spss_case_values WHERE lower(variable)='bau4_n_1_1'"
        ).fetchone()[0]

        assert region2 == [("r1", "1", "Central"), ("r2", "2", "Lagos 1"), ("r3", "1", "Central")]
        assert dimensions == [
            ("r1", "1", "1", "1", "1", "1"),
            ("r2", "2", "2", "2", "2", "2"),
            ("r3", "1", "1", "1", "1", None),
        ]
        assert alias_values == [
            ("r1", "3", "High", "1.0", "Week 1"),
            ("r2", "1", "Mid", "2", "Week 2"),
            ("r3", "3", "Low", None, None),
        ]
        assert bau4_count == 2
        assert counts["spss_bau4_rules"] == 3467
        print({"status": "passed", **counts})


if __name__ == "__main__":
    main()
