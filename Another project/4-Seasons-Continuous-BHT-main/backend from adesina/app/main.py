from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse

from backend.app.database import bootstrap_database, database_ready_for_startup, db_connection
from backend.app.routers.auth import router as auth_router
from backend.app.routers.listing import router as listing_router
from backend.app.routers.main_survey import router as main_survey_router
from backend.app.routers.main_survey_cases import router as main_survey_cases_router
from backend.app.routers.user_management import router as user_management_router
from backend.app.routers.integration import router as integration_router
from backend.app.activity_log import bootstrap_activity_log
from backend.app.services.listing import (
    bootstrap_listing_case_status_reconciliation,
    bootstrap_listing_export_dictionary,
    bootstrap_rule_definitions,
)
from backend.app.services.main_survey_cases import (
    bootstrap_main_case_status_reconciliation,
    bootstrap_main_rule_definitions,
)
from backend.app.settings import get_settings

logger = logging.getLogger(__name__)


async def _run_post_startup_bootstraps() -> None:
    settings = get_settings()
    try:
        await asyncio.to_thread(bootstrap_database, settings)
    except Exception:
        logger.exception("Database bootstrap failed; skipping dependent post-startup bootstraps.")
        return

    try:
        database_ready = await asyncio.to_thread(database_ready_for_startup, settings)
    except Exception:
        logger.exception("Database readiness check failed after bootstrap; skipping dependent bootstraps.")
        return

    if not database_ready:
        logger.error("Database bootstrap finished but required tables are still missing; skipping dependent bootstraps.")
        return

    bootstrap_steps = [
        ("listing export dictionary", bootstrap_listing_export_dictionary),
        ("listing rule definitions", bootstrap_rule_definitions),
        ("listing case reconciliation", bootstrap_listing_case_status_reconciliation),
        ("main rule definitions", bootstrap_main_rule_definitions),
        ("main case reconciliation", bootstrap_main_case_status_reconciliation),
        ("activity log", bootstrap_activity_log),
    ]

    for label, func in bootstrap_steps:
        try:
            await asyncio.to_thread(func, settings)
        except Exception:
            logger.exception("Post-startup bootstrap failed for %s.", label)

    await _refresh_runtime_marts(settings)


async def _refresh_runtime_marts(settings) -> None:
    mart_steps = []
    try:
        from backend.app.services.main_survey import prewarm_bht_overview_cache, refresh_bht_map_mart
        from backend.app.services.main_survey_cases import refresh_main_operational_marts

        mart_steps = [
            ("main operational marts", refresh_main_operational_marts),
            ("BHT overview/map mart", refresh_bht_map_mart),
            ("BHT overview cache", prewarm_bht_overview_cache),
        ]
    except Exception:
        logger.exception("Unable to import runtime mart refreshers.")
        return

    for label, func in mart_steps:
        try:
            await asyncio.to_thread(func, settings)
        except Exception:
            logger.exception("Post-startup mart refresh failed for %s.", label)


async def _run_bootstraps_if_needed() -> None:
    settings = get_settings()
    try:
        database_ready = await asyncio.to_thread(database_ready_for_startup, settings)
    except Exception:
        logger.exception("Database readiness check failed after startup; attempting bootstraps in background.")
        database_ready = False

    if database_ready:
        logger.info("Database already initialized; runtime mart refresh is owned by the ETL worker.")
        return

    logger.info("Database not ready; running startup bootstraps in the background.")
    await _run_post_startup_bootstraps()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_task = asyncio.create_task(_run_bootstraps_if_needed())

    try:
        yield
    finally:
        if bootstrap_task and not bootstrap_task.done():
            bootstrap_task.cancel()
            try:
                await bootstrap_task
            except asyncio.CancelledError:
                pass


def _run_startup_bootstraps_sync(settings) -> None:
    bootstrap_database(settings)
    if not database_ready_for_startup(settings):
        raise RuntimeError("Database bootstrap finished but required tables are still missing.")

    bootstrap_steps = [
        ("listing export dictionary", bootstrap_listing_export_dictionary),
        ("listing rule definitions", bootstrap_rule_definitions),
        ("listing case reconciliation", bootstrap_listing_case_status_reconciliation),
        ("main rule definitions", bootstrap_main_rule_definitions),
        ("main case reconciliation", bootstrap_main_case_status_reconciliation),
        ("activity log", bootstrap_activity_log),
    ]

    for label, func in bootstrap_steps:
        func(settings)

    from backend.app.services.main_survey import prewarm_bht_overview_cache, refresh_bht_map_mart, refresh_main_verbatim_answer_mart
    from backend.app.services.main_survey_cases import refresh_main_operational_marts

    refresh_main_operational_marts(settings)
    refresh_bht_map_mart(settings)
    prewarm_bht_overview_cache(settings)
    refresh_main_verbatim_answer_mart(settings)


app = FastAPI(
    title="INICIO SurveyCTO ETL QC Platform",
    version="0.1.0",
    summary="Listing ETL, QC, dashboards, exports, and map APIs for INICIO.",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/sync/status")
def sync_status():
    """
    Tiny health endpoint exposing latest listing/main sync timestamps and status.
    """
    default_state = {
        "lastSuccessfulCompletionUtc": None,
        "lastRunStartedAt": None,
        "lastRunFinishedAt": None,
        "lastStatus": "unknown",
        "lastMessage": None,
    }
    status_payload = {
        "listing": dict(default_state),
        "main": dict(default_state),
    }

    try:
        with db_connection(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT instrument_code, last_successful_completion_utc, last_run_started_at,
                           last_run_finished_at, last_status, last_message
                    FROM raw.sync_state
                    WHERE instrument_code IN ('listing', 'main')
                    """
                )
                for row in cur.fetchall():
                    instrument = str(row.get("instrument_code") or "").strip().lower()
                    if instrument not in status_payload:
                        continue
                    status_payload[instrument] = {
                        "lastSuccessfulCompletionUtc": row.get("last_successful_completion_utc"),
                        "lastRunStartedAt": row.get("last_run_started_at"),
                        "lastRunFinishedAt": row.get("last_run_finished_at"),
                        "lastStatus": row.get("last_status") or "unknown",
                        "lastMessage": row.get("last_message"),
                    }
    except Exception as exc:
        return {
            "status": "degraded",
            "error": str(exc),
            **status_payload,
        }

    return {
        "status": "ok",
        **status_payload,
    }


app.include_router(auth_router)
app.include_router(listing_router)
app.include_router(main_survey_router)
app.include_router(main_survey_cases_router)
app.include_router(user_management_router)
app.include_router(integration_router)

PROJECT_ROOT = Path(__file__).parent.parent.parent
FRONTEND_BUILD_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_SOURCE_DIR = PROJECT_ROOT / "frontend"


def _resolve_spa_index_path() -> Path:
    dist_index = FRONTEND_BUILD_DIR / "index.html"
    if dist_index.exists():
        return dist_index
    source_index = FRONTEND_SOURCE_DIR / "index.html"
    if source_index.exists():
        return source_index
    raise HTTPException(status_code=404, detail="SPA index not found")


@app.get("/api/spa")
def spa_health():
    return {"spa": "enabled"}


if FRONTEND_BUILD_DIR.exists():
    @app.get("/", include_in_schema=False)
    async def serve_spa_root():
        return FileResponse(FRONTEND_BUILD_DIR / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        # Let API 404s remain API 404s (instead of returning index.html payloads).
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        asset_path = FRONTEND_BUILD_DIR / full_path
        if asset_path.exists() and asset_path.is_file():
            return FileResponse(asset_path)

        return FileResponse(FRONTEND_BUILD_DIR / "index.html")
