from __future__ import annotations

import pytest

from app import database as db
from app.research import executor
from app.research.models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from app.research.run_control import request_cancel
from app.research.service import list_tasks
from app.research.sources.base import ContractorContext, ResearchSource


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name"],
        [
            {"id": "1", "contractor_name": "Alpha LLC"},
            {"id": "2", "contractor_name": "Beta LLC"},
            {"id": "3", "contractor_name": "Gamma LLC"},
        ],
        str(tmp_path / "master.csv"),
    )
    return tmp_path


class BlockingSource(ResearchSource):
    source_key = "osha"
    display_name = "Blocking fixture"

    def __init__(self):
        self.calls = 0

    def search(self, contractor: ContractorContext) -> SourceResult:
        self.calls += 1
        return SourceResult(
            source_key=self.source_key,
            contractor_id=contractor.internal_id,
            status=SourceResultStatus.BLOCKED,
            identity_status=IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.UNKNOWN,
            searched_name=contractor.contractor_name,
            warnings=["fixture source blocked"],
            acquisition_method="test",
        )


class CancelAfterFirstSource(ResearchSource):
    source_key = "osha"
    display_name = "Cancellation fixture"

    def __init__(self, run_id: int):
        self.run_id = run_id
        self.calls = 0

    def search(self, contractor: ContractorContext) -> SourceResult:
        self.calls += 1
        request_cancel(self.run_id, actor="test")
        return SourceResult(
            source_key=self.source_key,
            contractor_id=contractor.internal_id,
            status=SourceResultStatus.SUCCESS_NO_MATCH,
            identity_status=IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.COMPLETE,
            searched_name=contractor.contractor_name,
            acquisition_method="test",
        )


def test_source_wide_block_stops_hammering_same_source(isolated_db, monkeypatch):
    run = db.create_run(None, ["osha"], 3)
    source = BlockingSource()
    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"osha"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: source)

    result = executor.execute_research_run(run["id"])

    assert source.calls == 1
    assert result["executed"] == 1
    assert result["short_circuited"] == 2
    assert result["status_counts"] == {SourceResultStatus.BLOCKED.value: 3}
    assert result["run"]["status"] == "partial"
    tasks = list_tasks(run["id"])
    assert [task["status"] for task in tasks] == [SourceResultStatus.BLOCKED.value] * 3
    assert tasks[0]["attempt_count"] == 1
    assert tasks[1]["attempt_count"] == 0
    assert tasks[2]["attempt_count"] == 0
    assert "Not attempted because osha was halted" in tasks[1]["last_error"]

    warnings = [
        item for item in db.list_diagnostics(100)
        if item["message"].startswith("osha produced BLOCKED")
    ]
    assert len(warnings) == 1
    assert warnings[0]["details"]["task_count"] == 3
    assert warnings[0]["details"]["short_circuited_tasks"] == 2


def test_stop_request_leaves_remaining_tasks_not_checked(isolated_db, monkeypatch):
    run = db.create_run(None, ["osha"], 3)
    source = CancelAfterFirstSource(run["id"])
    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"osha"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: source)

    result = executor.execute_research_run(run["id"])

    assert source.calls == 1
    assert result["cancelled"] is True
    assert result["run"]["status"] == "cancelled"
    tasks = list_tasks(run["id"])
    assert tasks[0]["status"] == SourceResultStatus.SUCCESS_NO_MATCH.value
    assert tasks[1]["status"] == SourceResultStatus.NOT_CHECKED.value
    assert tasks[2]["status"] == SourceResultStatus.NOT_CHECKED.value
    assert "2 task(s) were left not checked" in result["run"]["message"]
