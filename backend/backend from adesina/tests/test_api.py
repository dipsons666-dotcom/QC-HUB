import json
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.services.listing import (
    _determine_coverage_approval_status,
    _evaluate_sparse_spatial_evidence,
    _feature_matches_boundary,
    _load_boundary_feature_from_zip,
    _normalize_boundary_key,
)
from backend.app.services.main_survey_cases import (
    VERIFICATION_ELIGIBLE_SECTIONS,
    _has_complete_callback_question_text,
    _is_single_response_or_numeric_question,
)
from backend.app.settings import Settings, get_settings


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_login_and_listing_overview():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "inicio2026"})
        assert login.status_code == 200
        token = login.json()["token"]

        overview = client.get("/api/dashboard/overview", headers={"Authorization": f"Bearer {token}"})
        assert overview.status_code == 200
        assert "statusCounts" in overview.json()

        cases = client.get("/api/listing/cases", headers={"Authorization": f"Bearer {token}"})
        assert cases.status_code == 200
        assert "items" in cases.json()


def test_coverage_approval_status_logic():
    assert _determine_coverage_approval_status("submitted", True) == "approved"
    assert _determine_coverage_approval_status("approved", False) == "pending_review"
    assert _determine_coverage_approval_status("pending_review", False) is None
    assert _determine_coverage_approval_status("approved", True) is None
    assert _determine_coverage_approval_status("rejected", True) is None


def test_sparse_spatial_evidence_accepts_well_spread_points_even_when_grid_would_be_strict():
    base_settings = get_settings()
    settings = Settings(
        root_dir=base_settings.root_dir,
        database_url="",
        boundary_zip_path=base_settings.boundary_zip_path,
        state_boundary_geojson_path=base_settings.state_boundary_geojson_path,
        frontend_origin=base_settings.frontend_origin,
        export_dir=base_settings.export_dir,
        auto_sync_enabled=False,
        sync_interval_seconds=0,
        auto_export_enabled=False,
        export_regen_interval_seconds=0,
        sparse_min_point_count=3,
        sparse_min_unique_buildings=2,
        sparse_min_bbox_coverage_ratio=0.05,
        sparse_min_quadrants=2,
        sparse_static_max_step_distance=0.00005,
        admin_seed_username="admin",
        admin_seed_password="secret",
        surveycto_server="server",
        surveycto_listing_form_id="listing",
        surveycto_main_form_id="main",
        surveycto_username=None,
        surveycto_password=None,
    )
    geometry = {
        "type": "Polygon",
        "coordinates": [[
            [0.0, 0.0],
            [10.0, 0.0],
            [10.0, 20.0],
            [0.0, 20.0],
            [0.0, 0.0],
        ]],
    }
    points = [
        (2.0, 2.0),
        (2.0, 6.0),
        (2.0, 10.0),
        (2.0, 14.0),
        (2.0, 18.0),
        (8.0, 2.0),
        (8.0, 6.0),
        (8.0, 10.0),
        (8.0, 14.0),
        (8.0, 18.0),
    ]

    result = _evaluate_sparse_spatial_evidence(points, geometry, settings)

    assert result["spread_ok"] is True
    assert result["bbox_coverage_ratio"] > 0.4
    assert result["quadrants_covered"] == 4
    assert result["unique_point_count"] == len(points)


def test_feature_matches_boundary_falls_back_to_normalized_name_with_region_match():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[7.0, 5.0], [7.1, 5.0], [7.1, 5.1], [7.0, 5.1], [7.0, 5.0]]]},
        "properties": {
            "sd_EA_NAME": "NEW LIVE LIVING MINISTRIES NKWOHA EZIUKWU ABA",
            "sd_STATE_NAME": "ABIA",
            "sd_LGA_NAME": "ABA SOUTH",
        },
    }

    assert _feature_matches_boundary(
        feature,
        ea_id="wrong-id",
        ea_name="New Live Living Ministries, Nkwoha Eziukwu Aba",
        state_name="Abia",
        lga_name="Aba South",
    ) is True
    assert _feature_matches_boundary(
        feature,
        ea_name="New Live Living Ministries Nkwoha Eziukwu Aba",
        state_name="Abia",
        lga_name="Aba North",
    ) is False


def test_normalize_boundary_key_strips_trailing_decimal_zeroes():
    assert _normalize_boundary_key("6480005600.0") == "6480005600"
    assert _normalize_boundary_key("6480005600.000") == "6480005600"
    assert _normalize_boundary_key("6480005600") == "6480005600"


def test_load_boundary_feature_from_zip_can_match_by_name_when_ids_are_missing():
    feature = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[7.0, 5.0], [7.1, 5.0], [7.1, 5.1], [7.0, 5.1], [7.0, 5.0]]]},
        "properties": {
            "sd_EA_ID": "6480005600",
            "sd_EA_NAME": "NEW LIVE LIVING MINISTRIES NKWOHA EZIUKWU ABA",
            "sd_STATE_NAME": "ABIA",
            "sd_LGA_NAME": "ABA SOUTH",
        },
    }
    archive_path = Path("output") / f"boundary-name-match-{uuid4().hex}.zip"
    try:
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "output_geojson/Main/Abia_full.geojson",
                json.dumps({"type": "FeatureCollection", "features": [feature]}),
            )

        matched = _load_boundary_feature_from_zip(
            str(archive_path),
            ea_id="",
            boundary_id="",
            ea_name="New Live Living Ministries Nkwoha Eziukwu Aba",
            state_name="Abia",
            lga_name="Aba South",
        )
    finally:
        archive_path.unlink(missing_ok=True)

    assert matched is not None
    assert matched["properties"]["sd_EA_ID"] == "6480005600"


def _allowed_roles_for_route(path: str) -> tuple[str, ...]:
    for route in app.routes:
        if getattr(route, "path", None) != path:
            continue
        for dependency in route.dependant.dependencies:
            call = getattr(dependency, "call", None)
            if getattr(call, "__name__", "") != "dependency":
                continue
            closure = getattr(call, "__closure__", None) or ()
            if closure:
                roles = closure[0].cell_contents
                if isinstance(roles, tuple):
                    return roles
    raise AssertionError(f"Route not found or has no role dependency: {path}")


def test_qc_reviewer_can_bulk_push_callback_and_audio_routes():
    callback_roles = _allowed_roles_for_route("/api/main-survey/callbacks/bulk")
    audio_roles = _allowed_roles_for_route("/api/main-survey/audio-listening/bulk-assign")

    assert "qc_reviewer" in callback_roles
    assert "qc_reviewer" in audio_roles


def test_callback_random_question_pool_excludes_early_sections_and_grids():
    assert VERIFICATION_ELIGIBLE_SECTIONS[0] == "F. FINANCIAL CAPABILITY"
    assert "C. INTRODUCTION AND SCREENING QUESTIONS" not in VERIFICATION_ELIGIBLE_SECTIONS
    assert "D. HOUSEHOLD QUESTIONS" not in VERIFICATION_ELIGIBLE_SECTIONS
    assert "E. DEMOGRAPHICS" not in VERIFICATION_ELIGIBLE_SECTIONS

    section_rows = [
        {"variable": "Gen1.1", "label": "1. ...whether you should work to earn income?", "storageType": "numeric", "valueLabels": "1.0=Myself | 2.0=Spouse"},
        {"variable": "Gen2", "label": "Gen2. On a typical day, how many hours do you spend on paid work?", "storageType": "numeric", "valueLabels": "1.0=Less than 2 hours | 2.0=2 to 5 hours"},
    ]

    assert not _is_single_response_or_numeric_question("Gen1.1", section_rows[0], section_rows)
    assert _is_single_response_or_numeric_question("Gen2", section_rows[1], section_rows)
    assert not _has_complete_callback_question_text("SA1", "")
    assert not _has_complete_callback_question_text("LC3", "LC3. Thinking about the money you had to pay back in the past 12 months, have you…")
