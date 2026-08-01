from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


SURVEY_SYNC_ADVISORY_LOCK = 807001


@contextmanager
def survey_sync_lock(conn: Any) -> Iterator[bool]:
    lock_acquired = False
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (SURVEY_SYNC_ADVISORY_LOCK,))
        row = cur.fetchone() or {}
        lock_acquired = bool(row.get("locked"))

    try:
        yield lock_acquired
    finally:
        if lock_acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SURVEY_SYNC_ADVISORY_LOCK,))


@contextmanager
def survey_sync_lock_wait(conn: Any) -> Iterator[bool]:
    waited_for_lock = False
    lock_acquired = False

    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (SURVEY_SYNC_ADVISORY_LOCK,))
        row = cur.fetchone() or {}
        lock_acquired = bool(row.get("locked"))

        if not lock_acquired:
            waited_for_lock = True
            cur.execute("SELECT pg_advisory_lock(%s)", (SURVEY_SYNC_ADVISORY_LOCK,))
            lock_acquired = True

    try:
        yield waited_for_lock
    finally:
        if lock_acquired:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SURVEY_SYNC_ADVISORY_LOCK,))
