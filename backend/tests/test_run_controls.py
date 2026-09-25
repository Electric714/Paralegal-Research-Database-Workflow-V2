from __future__ import annotations

import pytest

from app import database as db
from app.research import executor, run_control
from app.research.models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from app.research.run_control import prepare_resume, request_pause, request_stop
from app.research.service import list_tasks


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    data_dir = backend_root / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "BASE_DIR", backend_root)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()

    rows = [
        {"id": "1", "contractor_name": "First Contractor LLC", "address_1": "1 Main St", "city": "Madison", "state": "WI", "zip": "53703"},
        {"id": "2", "contractor_name": "Second Contractor LLC", "address_1": "2 Main St", "city": "Madison", "state": "WI", "zip": "53703"},
    ]
    db.replace_master_database(
        "master.csv",
        list(rows[0].keys()),
        rows,
        str(tmp_path / "master.csv"),
    )
    return tmp_path


def _success(contractor) -> SourceResult:
    return SourceResult(
        source_key="fake",
        contractor_id=contractor.internal_id,
        status=SourceResultStatus.SUCCESS_NO_MATCH,
        identity_status=IdentityStatus.NOT_EVALUATED,
        completeness_status=CompletenessStatus.COMPLETE,
        searched_name=contractor.contractor_name,
        searched_address=contractor.address_1,
        acquisition_method="test",
    )


def test_pause_stops_before_next_task_and_resume_finishes(isolated_db, monkeypatch):
    run = db.create_run(None, ["fake"], 2)
    run_id = int(run["id"])
    state = {"calls": 0, "pause_once": True}

    class Adapter:
        def prepare(self):
            return None

        def search(self, contractor):
            state["calls"] += 1
            if state["pause_once"] and state["calls"] == 1:
                request_pause(run_id, actor="test")
            return _success(contractor)

    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"fake"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: Adapter())

    first = executor.execute_research_run(run_id)
    assert first["run"]["status"] == "paused"
    assert first["executed"] == 1
    statuses = [task["status"] for task in list_tasks(run_id)]
    assert statuses.count(SourceResultStatus.SUCCESS_NO_MATCH.value) == 1
    assert statuses.count(SourceResultStatus.NOT_CHECKED.value) == 1

    state["pause_once"] = False
    prepare_resume(run_id, actor="test")
    second = executor.execute_research_run(run_id)
    assert second["run"]["status"] == "completed"
    assert second["executed"] == 1
    assert all(task["status"] == SourceResultStatus.SUCCESS_NO_MATCH.value for task in list_tasks(run_id))


def test_stop_cancels_run_and_leaves_remaining_tasks_unchecked(isolated_db, monkeypatch):
    run = db.create_run(None, ["fake"], 2)
    run_id = int(run["id"])
    state = {"calls": 0}

    class Adapter:
        def prepare(self):
            return None

        def search(self, contractor):
            state["calls"] += 1
            if state["calls"] == 1:
                request_stop(run_id, actor="test")
            return _success(contractor)

    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"fake"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: Adapter())

    result = executor.execute_research_run(run_id)
    assert result["run"]["status"] == "cancelled"
    assert result["executed"] == 1
    assert "cancelled" not in result
    statuses = [task["status"] for task in list_tasks(run_id)]
    assert statuses.count(SourceResultStatus.SUCCESS_NO_MATCH.value) == 1
    assert statuses.count(SourceResultStatus.NOT_CHECKED.value) == 1

    # A stopped run is terminal; a stray execute call must not restart it.
    again = executor.execute_research_run(run_id)
    assert again["run"]["status"] == "cancelled"
    assert again["executed"] == 0
    assert "cancelled" not in again
    assert state["calls"] == 1


def test_pause_stop_resume_is_the_only_run_control_contract():
    # The superseded implementation exposed request_cancel() and a separate
    # execution["cancelled"] boolean. The current design uses explicit
    # pause/stop/resume commands and run.status as the single source of truth.
    assert hasattr(run_control, "request_pause")
    assert hasattr(run_control, "request_stop")
    assert hasattr(run_control, "prepare_resume")
    assert not hasattr(run_control, "request_cancel")


def test_run_control_routes_are_registered():
    from app.main_with_wcca import app

    paths = {getattr(route, "path", "") for route in app.router.routes}
    assert "/api/runs/{run_id}/pause" in paths
    assert "/api/runs/{run_id}/stop" in paths
    assert "/api/runs/{run_id}/resume" in paths
    assert "/api/runs/{run_id}/diagnostics/export" in paths


@pytest.mark.parametrize('status', ['SESSION_EXPIRED', 'PAGINATION_INCOMPLETE'])
def test_diagnostics_exports_all_blocked_and_partial_tasks(isolated_db, status):
    from fastapi.testclient import TestClient
    from app.main_with_wcca import app
    from app.research.service import create_tasks_for_run

    run_id = db.create_run(None, ['fake'], 2)['id']
    create_tasks_for_run(run_id, db.active_bidder_ids(), ['fake'])
    with db.connect() as conn:
        conn.execute('UPDATE research_tasks SET status=? WHERE research_run_id=?', (status, run_id))
    with TestClient(app) as client:
        response = client.get(f'/api/runs/{run_id}/diagnostics/export')
    assert response.status_code == 200
    payload = response.json()
    assert payload['summary']['attention_tasks'] == 2
    assert len(payload['problematic_tasks']) == 2
    assert {task['status'] for task in payload['problematic_tasks']} == {status}
