#!/usr/bin/env python3
"""Fetch SurveyCTO data and build a disk-backed DuckDB store for QC-HUB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import requests
from dotenv import load_dotenv

from app.main import get_survey_platform_config, _hash_payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp_path.replace(path)


def quote_sql(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SurveyCTO sync into DuckDB for QC-HUB")
    parser.add_argument("--status-file", type=str, help="Path to write sync result JSON")
    parser.add_argument("--duckdb-path", type=str, help="Path to write the generated DuckDB file")
    parser.add_argument("--batch-size", type=int, default=500, help="Number of rows to insert in each DuckDB batch")
    parser.add_argument("--temp-directory", type=str, help="DuckDB temporary directory")
    parser.add_argument("--memory-limit", type=str, help="DuckDB memory_limit PRAGMA value")
    parser.add_argument("--max-temp-directory-size", type=str, help="DuckDB max_temp_directory_size PRAGMA value")
    parser.add_argument("--threads", type=int, default=2, help="Number of DuckDB threads")
    return parser.parse_args()


def stream_submission_payloads_from_form(config: dict[str, str]) -> Any:
    if not config["server"] or not config["username"] or not config["password"] or not config["form_id"]:
        return

    surveycto_date = str(config["date"]).strip()
    if len(surveycto_date) != 8 or not surveycto_date.isdigit():
        surveycto_date = datetime.now(timezone.utc).strftime("%Y%m%d")

    url = f"https://{config['server']}.surveycto.com/api/v2/forms/data/wide/json/{config['form_id']}"
    response = requests.post(
        url,
        auth=(config["username"], config["password"]),
        params={"date": surveycto_date},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    for item in items:
        if isinstance(item, dict):
            yield item


def stream_surveycto_dataset_records(config: dict[str, str]) -> Any:
    if not config["server"] or not config["username"] or not config["password"] or not config["dataset_id"]:
        return

    url = f"https://{config['server']}.surveycto.com/api/v2/datasets/{config['dataset_id']}/records"
    next_cursor: str | None = None

    while True:
        params: dict[str, Any] = {"limit": 1000}
        if next_cursor:
            params["cursor"] = next_cursor

        response = requests.get(
            url,
            auth=(config["username"], config["password"]),
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload, dict):
            page_items = payload.get("data") or payload.get("items") or []
            next_cursor = payload.get("nextCursor")
        elif isinstance(payload, list):
            page_items = payload
            next_cursor = None
        else:
            page_items = []
            next_cursor = None

        for item in page_items:
            if isinstance(item, dict):
                yield item

        if not next_cursor or not page_items:
            break


def stream_submission_payloads(config: dict[str, str]) -> Any:
    if config.get("dataset_id"):
        yield from stream_surveycto_dataset_records(config)
    else:
        yield from stream_submission_payloads_from_form(config)


def make_duckdb_connection(
    db_path: Path,
    temp_directory: Path,
    memory_limit: str,
    max_temp_directory_size: str,
    threads: int,
) -> duckdb.DuckDBPyConnection:
    temp_directory.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(f"PRAGMA threads={threads}")
    con.execute(f"PRAGMA memory_limit={quote_sql(memory_limit)}")
    con.execute(f"PRAGMA temp_directory={quote_sql(str(temp_directory))}")
    con.execute(f"PRAGMA max_temp_directory_size={quote_sql(max_temp_directory_size)}")
    return con


def ensure_raw_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_surveycto_submission (
            submission_key VARCHAR,
            instrument_code VARCHAR,
            source_hash VARCHAR,
            fetched_at TIMESTAMP,
            raw_payload VARCHAR
        )
        """
    )


def insert_batch(con: duckdb.DuckDBPyConnection, rows: list[tuple[str, str, str, str, str]]) -> int:
    if not rows:
        return 0
    con.executemany(
        "INSERT INTO raw_surveycto_submission (submission_key, instrument_code, source_hash, fetched_at, raw_payload) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def main() -> int:
    args = parse_args()
    status_path = Path(args.status_file) if args.status_file else None

    try:
        load_dotenv(Path(".").resolve() / ".env", override=True)
        config = get_survey_platform_config()

        data_root = Path(os.getenv("DATA_ROOT", Path(__file__).resolve().parents[1])).resolve()
        duckdb_path = Path(args.duckdb_path or os.getenv("DUCKDB_PATH", str(data_root / "current.duckdb"))).resolve()
        temp_directory = Path(args.temp_directory or os.getenv("DUCKDB_TEMP_DIRECTORY", str(data_root / "duckdb_tmp"))).resolve()
        memory_limit = args.memory_limit or os.getenv("DUCKDB_MEMORY_LIMIT", "1536MB")
        max_temp_directory_size = args.max_temp_directory_size or os.getenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "7GB")
        threads = args.threads or max(1, int(os.getenv("DUCKDB_THREADS", "2")))
        batch_size = max(1, args.batch_size)

        temp_db_path = duckdb_path.with_suffix(duckdb_path.suffix + ".tmp") if duckdb_path.suffix else Path(str(duckdb_path) + ".tmp")
        if temp_db_path.exists():
            temp_db_path.unlink()

        sync_temp_directory = temp_directory / f"sync-{uuid.uuid4().hex}"
        sync_temp_directory.mkdir(parents=True, exist_ok=True)

        con = make_duckdb_connection(temp_db_path, sync_temp_directory, memory_limit, max_temp_directory_size, threads)
        ensure_raw_table(con)

        stored = 0
        updated = 0
        skipped = 0
        fetched = 0
        batch: list[tuple[str, str, str, str, str]] = []

        for item in stream_submission_payloads(config):
            fetched += 1
            submission_id = str(
                item.get("submission_id")
                or item.get("id")
                or item.get("KEY")
                or item.get("submissionkey")
                or ""
            )
            if not submission_id:
                skipped += 1
                continue

            payload_data = item.get("data") or item.get("payload") or item
            if not isinstance(payload_data, dict):
                payload_data = {"value": payload_data}

            instrument_code = str(item.get("instrument_code") or config.get("instrument_code") or "main")
            source_hash = _hash_payload(payload_data)
            fetched_at = datetime.now(timezone.utc).isoformat()
            raw_payload = json.dumps(payload_data, ensure_ascii=False, default=str)

            batch.append((submission_id, instrument_code, source_hash, fetched_at, raw_payload))
            if len(batch) >= batch_size:
                stored += insert_batch(con, batch)
                batch.clear()

        if batch:
            stored += insert_batch(con, batch)
            batch.clear()

        con.close()
        shutil.rmtree(sync_temp_directory, ignore_errors=True)

        duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        temp_db_path.replace(duckdb_path)

        result = {
            "ok": True,
            "stored": stored,
            "updated": updated,
            "skipped": skipped,
            "fetched": fetched,
            "duckdb_path": str(duckdb_path),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if status_path is not None:
            write_json(status_path, result)
        print(json.dumps(result, default=str))
        return 0
    except Exception as exc:
        error_result = {"ok": False, "error": str(exc), "timestamp": datetime.now(timezone.utc).isoformat()}
        if status_path is not None:
            try:
                write_json(status_path, error_result)
            except Exception:
                pass
        print(json.dumps(error_result, default=str))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
