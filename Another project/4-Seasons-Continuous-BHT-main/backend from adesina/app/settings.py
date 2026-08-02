from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def load_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_env(name: str, dotenv: dict[str, str], default: str | None = None) -> str | None:
    return os.environ.get(name, dotenv.get(name, default))


@dataclass(slots=True)
class Settings:
    root_dir: Path
    database_url: str
    boundary_zip_path: Path
    state_boundary_geojson_path: Path
    frontend_origin: str
    export_dir: Path
    auto_sync_enabled: bool
    sync_interval_seconds: int
    auto_export_enabled: bool
    export_regen_interval_seconds: int
    sparse_min_point_count: int
    sparse_min_unique_buildings: int
    sparse_min_bbox_coverage_ratio: float
    sparse_min_quadrants: int
    sparse_static_max_step_distance: float
    admin_seed_username: str
    admin_seed_password: str
    surveycto_server: str
    surveycto_listing_form_id: str
    surveycto_main_form_id: str
    surveycto_username: str | None
    surveycto_password: str | None
    main_survey_data_start_date: str | None
    qc_integration_api_key: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root_dir = Path(__file__).resolve().parents[2]
    dotenv = load_dotenv_file(root_dir / ".env")
    export_dir = root_dir / "output" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        root_dir=root_dir,
        database_url=read_env("DATABASE_URL", dotenv, "") or "",
        boundary_zip_path=root_dir / "output_geojson.zip",
        state_boundary_geojson_path=root_dir / "NGA_State_Boundaries.geojson",
        frontend_origin=read_env("FRONTEND_ORIGIN", dotenv, "http://localhost:5173") or "http://localhost:5173",
        export_dir=export_dir,
        auto_sync_enabled=(read_env("AUTO_SYNC_ENABLED", dotenv, "true") or "true").lower() == "true",
        sync_interval_seconds=int(read_env("SYNC_INTERVAL_SECONDS", dotenv, "3600") or "3600"),
        auto_export_enabled=(read_env("AUTO_EXPORT_ENABLED", dotenv, "true") or "true").lower() == "true",
        export_regen_interval_seconds=int(read_env("EXPORT_REGEN_INTERVAL_SECONDS", dotenv, "86400") or "86400"),
        sparse_min_point_count=int(read_env("SPARSE_MIN_POINT_COUNT", dotenv, "3") or "3"),
        sparse_min_unique_buildings=int(read_env("SPARSE_MIN_UNIQUE_BUILDINGS", dotenv, "2") or "2"),
        sparse_min_bbox_coverage_ratio=float(read_env("SPARSE_MIN_BBOX_COVERAGE_RATIO", dotenv, "0.05") or "0.05"),
        sparse_min_quadrants=int(read_env("SPARSE_MIN_QUADRANTS", dotenv, "2") or "2"),
        sparse_static_max_step_distance=float(read_env("SPARSE_STATIC_MAX_STEP_DISTANCE", dotenv, "0.00005") or "0.00005"),
        admin_seed_username=read_env("ADMIN_SEED_USERNAME", dotenv, "superadmin") or "superadmin",
        admin_seed_password=read_env("ADMIN_SEED_PASSWORD", dotenv, "inicio2026") or "inicio2026",
        surveycto_server=read_env("SURVEYCTO_SERVER", dotenv, "edvoimpacts") or "edvoimpacts",
        surveycto_listing_form_id=read_env("SURVEYCTO_LISTING_FORM_ID", dotenv, "hh_listing_sampling") or "hh_listing_sampling",
        surveycto_main_form_id=read_env("SURVEYCTO_MAIN_FORM_ID", dotenv, "") or "",
        surveycto_username=read_env("SURVEYCTO_USERNAME", dotenv),
        surveycto_password=read_env("SURVEYCTO_PASSWORD", dotenv),
        main_survey_data_start_date=read_env("MAIN_SURVEY_DATA_START_DATE", dotenv),
        qc_integration_api_key=read_env("QC_INTEGRATION_API_KEY", dotenv, "") or "",
    )
