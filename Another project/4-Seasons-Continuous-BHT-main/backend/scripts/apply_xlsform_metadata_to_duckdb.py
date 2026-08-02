#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

from xlsform_metadata import (
    build_answer_text,
    build_question_text,
    get_xlsform_question_metadata,
    load_xlsform_metadata,
    map_demographic_value,
)


DEMOGRAPHIC_COLUMNS = ["Region", "City_1", "D3", "d3_q", "Gender", "Age", "Age_cal", "SEC", "Week"]


def rebuild_bau_metric_facts(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("drop table if exists bau_metric_facts")
    con.execute(
        """
        create table bau_metric_facts as
        with src as (
          select
            cast(category as varchar) as category,
            cast(respondent_id as varchar) as respondent_id,
            cast(month as varchar) as month,
            cast(question as varchar) as question,
            cast(question_label as varchar) as question_label,
            cast(answer_label as varchar) as answer_label,
            cast(answer_value as varchar) as answer_value,
            cast(answer_value_num as double) as answer_value_num
          from responses_fact
        )
        select * from (
          select category, respondent_id, month, 'brand_tom'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU1A$')

          union all
          select category, respondent_id, month, 'brand_spont'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU1B_[0-9]+$')
            and (coalesce(answer_value_num, 0) = 1 or answer_value in ('1', '1.0') or lower(coalesce(answer_label, '')) = 'yes')

          union all
          select category, respondent_id, month, 'ad_tom'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU1C$')

          union all
          select category, respondent_id, month, 'ad_spont'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU1D_[0-9]+$')
            and (coalesce(answer_value_num, 0) = 1 or answer_value in ('1', '1.0') or lower(coalesce(answer_label, '')) = 'yes')

          union all
          select category, respondent_id, month, 'aided'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU2_[0-9]+$')
            and (coalesce(answer_value_num, 0) = 1 or answer_value in ('1', '1.0') or lower(coalesce(answer_label, '')) = 'yes')

          union all
          select category, respondent_id, month, 'aided_ad'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU3_[0-9]+$')
            and (coalesce(answer_value_num, 0) = 1 or answer_value in ('1', '1.0') or lower(coalesce(answer_label, '')) = 'yes')

          union all
          select category, respondent_id, month, 'ever_consumed'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU5A_[0-9]+$')

          union all
          select category, respondent_id, month, 'last_3_months'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU5C_[0-9]+$')

          union all
          select category, respondent_id, month, 'last_1_month'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU6A_[0-9]+$')

          union all
          select category, respondent_id, month, 'last_7_days'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU6B_[0-9]+$')

          union all
          select category, respondent_id, month, 'most_often_used'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU6C$')

          union all
          select category, respondent_id, month, 'prefrence'::text as metric,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as brand,
                 cast(null as varchar) as option
          from src
          where regexp_matches(question, '(?i)(^|_)BAU8$')

          union all
          select category, respondent_id, month, 'media_source'::text as metric,
                 nullif(trim(regexp_extract(question_label, '^\\(([^)]+)\\)', 1)), '') as brand,
                 nullif(trim(coalesce(nullif(answer_label, ''), nullif(answer_value, ''))), '') as option
          from src
          where regexp_matches(question, '(?i)BAU4_[0-9]+$|BAU4_[0-9]+_[0-9]+$')
            and (coalesce(answer_value_num, 0) = 1 or answer_value in ('1', '1.0') or lower(coalesce(answer_label, '')) = 'yes' or answer_label is not null)
        )
        where brand is not null and brand <> '' and brand <> '{0}' and lower(brand) not in ('none', 'none of these')
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply XLSForm labels to an existing DuckDB file.")
    parser.add_argument("duckdb_path", help="Path to the DuckDB file to update")
    parser.add_argument(
        "--metadata-json",
        default=str(Path(__file__).resolve().parent.parent / "data" / "xlsform_metadata.json"),
        help="Path to the exported XLSForm metadata JSON",
    )
    args = parser.parse_args()

    db_path = Path(args.duckdb_path).resolve()
    metadata = load_xlsform_metadata(args.metadata_json)

    con = duckdb.connect(str(db_path))
    try:
        if con.execute("select count(*) from information_schema.tables where table_name='responses_fact'").fetchone()[0]:
            rows = con.execute(
                """
                select distinct
                  cast(question as varchar) as question,
                  cast(answer_value as varchar) as answer_value
                from responses_fact
                """
            ).fetchall()
            payload = []
            for question, answer_value in rows:
                entry = get_xlsform_question_metadata(question, metadata)
                payload.append(
                    {
                        "question": question,
                        "answer_value": answer_value,
                        "question_label": build_question_text(question, entry),
                        "answer_label": build_answer_text(question, answer_value, entry, False),
                    }
                )
            if payload:
                con.register("label_updates", pd.DataFrame(payload))
                con.execute(
                    """
                    update responses_fact as r
                    set question_label = u.question_label,
                        answer_label = u.answer_label
                    from label_updates as u
                    where cast(r.question as varchar) = u.question
                      and cast(r.answer_value as varchar) = u.answer_value
                    """
                )
            rebuild_bau_metric_facts(con)

        if con.execute("select count(*) from information_schema.tables where table_name='respondent_dims'").fetchone()[0]:
            dim_columns = {row[0] for row in con.execute("describe select * from respondent_dims").fetchall()}
            for column in DEMOGRAPHIC_COLUMNS:
                if column not in dim_columns:
                    continue
                values = con.execute(
                    f'select distinct cast("{column}" as varchar) from respondent_dims where "{column}" is not null'
                ).fetchall()
                payload = []
                for (raw_value,) in values:
                    mapped = map_demographic_value(column, raw_value, metadata)
                    if mapped and mapped != raw_value:
                        payload.append({"raw_value": raw_value, "mapped_value": mapped})
                if not payload:
                    continue
                temp_name = f"dim_map_{column.lower().replace('.', '_')}"
                con.register(temp_name, pd.DataFrame(payload))
                con.execute(
                    f"""
                    update respondent_dims as d
                    set "{column}" = m.mapped_value
                    from {temp_name} as m
                    where cast(d."{column}" as varchar) = m.raw_value
                    """
                )

        print(f"Applied XLSForm metadata to {db_path}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
