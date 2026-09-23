from __future__ import annotations

import pytest

from app import database as db
from app.research import executor
from app.research.models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
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
            warnings=["fixture returned HTTP 403"],
            acquisition_method="test",
        )


def test_source_wide_block_only_hits_source_once(isolated_db, monkeypatch):
    run = db.create_run(None, ["osha"], 3)
    source = BlockingSource()
    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"osha"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: source)

    result = executor.execute_research_run(run["id"])

    assert source.calls == 1
    assert result["executed"] == 1
    assert result["short_circuited"] == 2
    assert result["run"]["status"] == "partial"

    tasks = list_tasks(run["id"])
    assert tasks[0]["status"] == SourceResultStatus.BLOCKED.value
    assert tasks[0]["attempt_count"] == 1
    assert tasks[1]["status"] == SourceResultStatus.NOT_CHECKED.value
    assert tasks[2]["status"] == SourceResultStatus.NOT_CHECKED.value
    assert tasks[1]["attempt_count"] == 0
    assert tasks[2]["attempt_count"] == 0
    assert "Not attempted because osha was halted" in tasks[1]["last_error"]

    warnings = [
        item for item in db.list_diagnostics(100)
        if item["source_key"] == "osha" and "halted after BLOCKED" in item["message"]
    ]
    assert len(warnings) == 1
    assert warnings[0]["details"]["observed_task_count"] == 1
    assert warnings[0]["details"]["short_circuited_tasks"] == 2


def test_partial_run_cannot_feedback_loop_through_execute(isolated_db, monkeypatch):
    run = db.create_run(None, ["osha"], 3)
    with db.connect() as conn:
        conn.execute(
            "UPDATE research_runs SET status='partial', message='Finished with an attention task.' WHERE id=?",
            (run["id"],),
        )

    source = BlockingSource()
    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"osha"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: source)

    first = executor.execute_research_run(run["id"])
    second = executor.execute_research_run(run["id"])

    assert source.calls == 0
    assert first["executed"] == 0
    assert second["executed"] == 0
    assert first["run"]["status"] == "partial"
    assert second["run"]["status"] == "partial"
    assert all(task["status"] == SourceResultStatus.NOT_CHECKED.value for task in list_tasks(run["id"]))
