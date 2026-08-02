from __future__ import annotations

import logging
import sys
from pathlib import Path

from requests import exceptions as req_exc


ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from survey_platform.config import load_listing_pipeline_config, load_main_survey_pipeline_config  # noqa: E402
from survey_platform.db import SyncPreemptedError, advisory_lock, mark_sync_finished  # noqa: E402
from survey_platform.etl.listing import rebuild_listing_outputs  # noqa: E402
from survey_platform.etl.main_survey import rebuild_main_survey_outputs, run_main_survey_sync  # noqa: E402


logger = logging.getLogger(__name__)
GLOBAL_SURVEYCTO_LOCK_ID = 20250409


def describe_sync_failure(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, req_exc.HTTPError):
        response = exc.response
        status_code = response.status_code if response is not None else None
        if status_code == 409:
            return 503, "SurveyCTO is busy with another export request. Try again in a few minutes."
        if status_code == 401:
            return 502, "SurveyCTO rejected the credentials used for the export request."
        if status_code == 403:
            return 502, "SurveyCTO denied access to the requested form export."
        if status_code == 404:
            return 502, "SurveyCTO could not find the requested form export endpoint."
        return 502, f"SurveyCTO export request failed with HTTP {status_code}: {exc}"

    if isinstance(exc, req_exc.ChunkedEncodingError):
        return 503, "SurveyCTO closed the export response before completion. Retry in a few minutes."

    if isinstance(exc, (req_exc.ReadTimeout, req_exc.ConnectTimeout)):
        return 504, "SurveyCTO did not respond before the export timeout expired."

    if isinstance(exc, req_exc.ConnectionError):
        return 503, "Could not establish a stable connection to SurveyCTO."

    return 500, f"{type(exc).__name__}: {exc}"


def run_listing_sync_job(source: str = "system", sync_request_token: str | None = None):
    logger.info("Legacy listing sync requested; running unified main SurveyCTO sync instead.")
    return run_main_survey_sync_job(source=source, sync_request_token=sync_request_token)


def rebuild_listing_job():
    config = load_listing_pipeline_config(ROOT)
    return rebuild_listing_outputs(config)


def run_main_survey_sync_job(
    source: str = "system",
    sync_request_token: str | None = None,
    surveycto_username: str | None = None,
    surveycto_password: str | None = None,
    force_full: bool = False,
):
    config = load_main_survey_pipeline_config(
        ROOT,
        sync_source=source,
        sync_request_token=sync_request_token,
        force_full=force_full,
    )
    if surveycto_username and surveycto_password:
        config.username = surveycto_username
        config.password = surveycto_password
    result = run_main_survey_sync(config)
    if result.get("status") == "success":
        try:
            from backend.app.settings import get_settings
            from backend.app.services.main_survey import clear_bht_analytics_caches, refresh_main_verbatim_answer_mart

            settings = get_settings()
            clear_bht_analytics_caches(settings, refresh_map_mart=True)
            new_submission_keys = [
                str(key or "").strip()
                for key in result.get("newSubmissionKeys", [])
                if str(key or "").strip()
            ]
            if new_submission_keys:
                result["verbatimMartResult"] = refresh_main_verbatim_answer_mart(settings, submission_keys=new_submission_keys)
        except Exception:
            logger.exception("Failed to refresh BHT analytics/verbatim marts after main sync")
    new_submission_keys = [
        str(key or "").strip()
        for key in result.get("newSubmissionKeys", [])
        if str(key or "").strip()
    ]
    if result.get("status") == "success" and new_submission_keys:
        try:
            from backend.app.settings import get_settings
            from backend.app.services.main_survey_cases import _clear_main_case_list_cache, refresh_main_operational_marts, run_main_qc

            settings = get_settings()
            qc_result = run_main_qc(
                settings,
                submission_keys=new_submission_keys,
                only_pending=False,
                batch_limit=None,
            )
            result["operationalMartResult"] = refresh_main_operational_marts(settings)
            _clear_main_case_list_cache()
            result["qcResult"] = qc_result
            result["message"] = f"{result.get('message', '').strip()} QC scanned {len(new_submission_keys)} new case(s).".strip()
        except Exception as exc:
            logger.exception("Main QC after successful sync failed")
            result["qcResult"] = {
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "newSubmissionCount": len(new_submission_keys),
            }
            result["message"] = f"{result.get('message', '').strip()} QC failed after sync: {type(exc).__name__}: {exc}".strip()
    return result


def rebuild_main_survey_job():
    config = load_main_survey_pipeline_config(ROOT)
    return rebuild_main_survey_outputs(config)


def run_all_sync_jobs(source: str = "system", sync_request_token: str | None = None):
    main_config = load_main_survey_pipeline_config(
        ROOT,
        sync_source=source,
        sync_request_token=sync_request_token,
    )

    with advisory_lock(main_config, GLOBAL_SURVEYCTO_LOCK_ID) as locked:
        if not locked:
            return {
                "main": {
                    "status": "busy",
                    "reason": "Another sync is already running.",
                },
            }

        try:
            main_result = run_main_survey_sync(main_config)
        except SyncPreemptedError as exc:
            main_result = {"status": "preempted", "reason": str(exc)}
            mark_sync_finished(main_config, "main", "preempted", str(exc))
            return {
                "main": main_result,
            }
        except Exception as exc:
            logger.exception("Main survey sync failed")
            main_result = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            mark_sync_finished(main_config, "main", "failed", main_result["reason"])

        if main_result.get("status") in {"busy", "upstream_busy", "retry_later"}:
            mark_sync_finished(
                main_config,
                "main",
                str(main_result.get("status")),
                main_result.get("reason"),
            )

        return {"main": main_result}
