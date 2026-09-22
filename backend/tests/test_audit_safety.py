from __future__ import annotations

import httpx
import pytest

from app import database as db
from app.research import executor
from app.research.models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from app.research.service import list_tasks, persist_source_result
from app.research.sources.base import ContractorContext, ResearchSource
from app.research.sources.mn_pca import CSV_EXPORT_URL, MinnesotaPcaEnforcementSource
from app.sources import SOURCES


MPCA_CSV = """Company or individual(s),Public date,Violation location,Violation description,Net penalty,Case type
Acme Construction LLC,03/05/2026,Minneapolis,Construction stormwater,$9663,Administrative penalty order
Other Builder Inc,01/10/2025,St Paul,Hazardous waste,$1200,Administrative penalty order
"""


class FailingPrepareSource(ResearchSource):
    source_key = "osha"
    display_name = "Failing prepare fixture"

    def prepare(self) -> None:
        raise RuntimeError("prepare exploded")

    def search(self, contractor: ContractorContext) -> SourceResult:
        raise AssertionError("search must not run after prepare failure")


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def _mpca_source(body: str) -> MinnesotaPcaEnforcementSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CSV_EXPORT_URL
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/csv"},
            request=request,
        )

    return MinnesotaPcaEnforcementSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    )


def test_dol_implemented_source_is_exposed_as_ready():
    dol = next(source for source in SOURCES if source["key"] == "dol_enforcement")
    assert dol["status"] == "ready"
    assert "DOL_API_KEY" in dol["category"]


def test_mpca_name_expansion_is_review_required_not_clean_negative():
    body = MPCA_CSV.replace("Acme Construction LLC", "Acme Construction Services LLC")
    result = _mpca_source(body).search(
        ContractorContext(
            internal_id=7,
            external_id="B-7",
            contractor_name="Acme Construction, LLC",
            city="Minneapolis",
            state="MN",
        )
    )

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.is_clean_negative is False
    assert result.evidence[0].details["candidate_party"] == "Acme Construction Services LLC"
    assert result.evidence[0].details["master_field_proposal_allowed"] is False


def test_non_success_status_cannot_create_master_proposal(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "osha"],
        [{"id": "8", "contractor_name": "Contradictory Result LLC", "osha": "N"}],
        str(isolated_db / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    run = db.create_run(None, ["osha"], 1)
    task_id = list_tasks(run["id"])[0]["id"]

    result = SourceResult(
        source_key="osha",
        contractor_id=bidder_id,
        status=SourceResultStatus.PARTIAL_RESULTS,
        identity_status=IdentityStatus.CONFIRMED,
        completeness_status=CompletenessStatus.COMPLETE,
        searched_name="Contradictory Result LLC",
        evidence=[EvidenceRecord(field_name="osha", observed_value="Y")],
    )
    persisted = persist_source_result(task_id, result)

    assert persisted["proposal_ids"] == []
    assert db.get_bidder(bidder_id)["osha"] == "N"


def test_prepare_failure_is_persisted_without_aborting_run(isolated_db, monkeypatch):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "osha"],
        [{"id": "9", "contractor_name": "Prepare Failure LLC", "osha": "N"}],
        str(isolated_db / "master.csv"),
    )
    run = db.create_run(None, ["osha"], 1)

    monkeypatch.setattr(executor, "implemented_source_keys", lambda: frozenset({"osha"}))
    monkeypatch.setattr(executor, "create_source", lambda source_key: FailingPrepareSource())

    execution = executor.execute_research_run(run["id"])

    assert execution["run"]["status"] == "partial"
    assert execution["executed"] == 1
    assert execution["status_counts"] == {SourceResultStatus.PARSER_FAILURE.value: 1}
    task = list_tasks(run["id"])[0]
    assert task["status"] == SourceResultStatus.PARSER_FAILURE.value

    with db.connect() as conn:
        check = conn.execute(
            "SELECT * FROM source_checks WHERE research_task_id = ?",
            (task["id"],),
        ).fetchone()

    assert check["completeness_status"] == CompletenessStatus.UNKNOWN.value
    assert check["acquisition_method"] == "adapter_prepare_error"
