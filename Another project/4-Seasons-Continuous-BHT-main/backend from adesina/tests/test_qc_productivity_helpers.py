from backend.app.main import app
from backend.app.services.qc_productivity import (
    build_qc_productivity_by_date,
    normalize_qc_productivity_queue,
    summarize_qc_task_rows,
)


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


def test_normalize_qc_productivity_queue_defaults_to_all():
    assert normalize_qc_productivity_queue(None) == "all"
    assert normalize_qc_productivity_queue("AUDIO") == "audio"


def test_summarize_qc_task_rows_counts_workload_events_not_unique_cases():
    rows = [
        {"username": "qc.alpha", "full_name": "QC Alpha", "assigned_at": "2026-04-20T08:00:00Z", "completed_at": "2026-04-20T09:00:00Z"},
        {"username": "qc.alpha", "full_name": "QC Alpha", "assigned_at": "2026-04-20T10:00:00Z", "completed_at": None},
        {"username": "qc.beta", "full_name": "QC Beta", "assigned_at": "2026-04-21T08:00:00Z", "completed_at": "2026-04-21T09:00:00Z"},
    ]

    assert summarize_qc_task_rows(rows) == [
        {"username": "qc.alpha", "full_name": "QC Alpha", "total_pushed": 2, "completed": 1, "pending": 1},
        {"username": "qc.beta", "full_name": "QC Beta", "total_pushed": 1, "completed": 1, "pending": 0},
    ]


def test_build_qc_productivity_by_date_groups_on_assigned_date():
    rows = [
        {"username": "qc.alpha", "full_name": "QC Alpha", "assigned_at": "2026-04-20T08:00:00Z", "completed_at": "2026-04-20T09:00:00Z"},
        {"username": "qc.alpha", "full_name": "QC Alpha", "assigned_at": "2026-04-20T10:00:00Z", "completed_at": None},
        {"username": "qc.alpha", "full_name": "QC Alpha", "assigned_at": "2026-04-21T08:00:00Z", "completed_at": None},
        {"username": "qc.beta", "full_name": "QC Beta", "assigned_at": "2026-04-21", "completed_at": None},
    ]

    assert build_qc_productivity_by_date(rows) == {
        "dates": ["2026-04-20", "2026-04-21"],
        "items": [
            {"username": "qc.alpha", "full_name": "QC Alpha", "counts": {"2026-04-20": 2, "2026-04-21": 1}},
            {"username": "qc.beta", "full_name": "QC Beta", "counts": {"2026-04-20": 0, "2026-04-21": 1}},
        ],
    }


def test_qc_productivity_routes_are_admin_only():
    assert _allowed_roles_for_route("/api/main-survey/qc-productivity") == ("admin",)
    assert _allowed_roles_for_route("/api/main-survey/qc-productivity-by-date") == ("admin",)
    assert _allowed_roles_for_route("/api/listing/qc-productivity") == ("admin",)
    assert _allowed_roles_for_route("/api/listing/qc-productivity-by-date") == ("admin",)
