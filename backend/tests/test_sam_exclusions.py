from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app import database as db
from app.research.executor import execute_research_run
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.service import list_tasks, review_change
from app.research.sources.base import ContractorContext
from app.research.sources.sam_exclusions import (
    SamExclusionsSource,
    SamExtractError,
    parse_sam_exclusions,
    store_uploaded_extract,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sam_exclusions_v2_sample.csv"
FIXED_TODAY = date(2026, 9, 20)
FRESH_FILENAME = "SAM_Exclusions_Public_Extract_V2_26263.CSV"
STALE_FILENAME = "SAM_Exclusions_Public_Extract_V2_26250.CSV"


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
    return tmp_path


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _cache_dataset(tmp_path: Path, filename: str = FRESH_FILENAME):
    cache_dir = tmp_path / "sam-cache"
    dataset = store_uploaded_extract(_fixture_bytes(), filename, cache_dir=cache_dir)
    return cache_dir, dataset


def _acme_context() -> ContractorContext:
    return ContractorContext(
        internal_id=1,
        external_id="1220",
        contractor_name="ACME Electric LLC",
        address_1="123 Main St",
        city="Madison",
        state="WI",
        zip="53703",
    )


def test_official_v2_fixture_parses_firms_only():
    csv_name, records = parse_sam_exclusions(_fixture_bytes(), FRESH_FILENAME)
    assert csv_name == FRESH_FILENAME
    assert len(records) == 3
    assert records[0].name == "ACME ELECTRIC, L.L.C."
    assert records[0].uei_sam == "ACMEUEI12345"
    assert records[0].sam_number == "100000001"
    assert all(record.classification == "Firm" for record in records)


def test_zip_extract_is_supported():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(FRESH_FILENAME, _fixture_bytes())
    csv_name, records = parse_sam_exclusions(
        buffer.getvalue(),
        "SAM_Exclusions_Public_Extract_V2_26263.ZIP",
    )
    assert csv_name == FRESH_FILENAME
    assert len(records) == 3


def test_malformed_extract_is_rejected():
    with pytest.raises(SamExtractError):
        parse_sam_exclusions(b"Name,State\nAcme,WI\n", "bad.csv")


def test_fresh_confirmed_match_produces_y_evidence(isolated_db, tmp_path):
    cache_dir, _dataset = _cache_dataset(tmp_path)
    source = SamExclusionsSource(api_key="", cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()
    result = source.search(_acme_context())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].field_name == "state_federal_debarment"
    assert result.evidence[0].observed_value == "Y"
    assert result.source_record_id == "100000001"
    assert result.artifacts[0].sha256


def test_fresh_no_match_is_clean_negative_but_does_not_emit_n(isolated_db, tmp_path):
    cache_dir, _dataset = _cache_dataset(tmp_path)
    source = SamExclusionsSource(api_key="", cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()
    result = source.search(
        ContractorContext(
            internal_id=99,
            external_id="99",
            contractor_name="No Such Contractor LLC",
            address_1="999 Nowhere Ave",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.is_clean_negative is True
    assert result.evidence == []


def test_possible_multiple_identity_match_requires_review(isolated_db, tmp_path):
    cache_dir, _dataset = _cache_dataset(tmp_path)
    source = SamExclusionsSource(api_key="", cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()
    result = source.search(
        ContractorContext(
            internal_id=7,
            external_id="7",
            contractor_name="Common Builders LLC",
            city="Madison",
            state="WI",
        )
    )

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []
    assert len(result.normalized_payload["top_candidates"]) >= 2


def test_stale_dataset_never_creates_clean_negative(isolated_db, tmp_path):
    cache_dir, _dataset = _cache_dataset(tmp_path, STALE_FILENAME)
    source = SamExclusionsSource(api_key="", cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()
    result = source.search(
        ContractorContext(
            internal_id=10,
            external_id="10",
            contractor_name="No Such Contractor LLC",
            city="Madison",
            state="WI",
        )
    )

    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False


def test_missing_uploaded_extract_is_source_unavailable(isolated_db, tmp_path):
    source = SamExclusionsSource(api_key="ignored", cache_dir=tmp_path / "empty-cache", today=FIXED_TODAY)
    source.prepare()
    result = source.search(_acme_context())

    assert result.status == SourceResultStatus.SOURCE_UNAVAILABLE
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert "Upload the official SAM Public Exclusions V2" in result.warnings[0]


def test_remembered_same_entity_judgment_is_reused(isolated_db, tmp_path):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "city", "state", "state_federal_debarment"],
        [{
            "id": "7",
            "contractor_name": "Common Builders LLC",
            "city": "Madison",
            "state": "WI",
            "state_federal_debarment": "N",
        }],
        str(tmp_path / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO identity_judgments(
                bidder_id, source_key, source_record_id, judgment, confidence,
                decided_by, decided_at, notes
            ) VALUES (?, 'sam', '100000002', 'SAME_ENTITY', 1.0, 'tester', ?, '')
            """,
            (bidder_id, datetime.now(timezone.utc).isoformat()),
        )

    cache_dir, _dataset = _cache_dataset(tmp_path)
    source = SamExclusionsSource(api_key="", cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()
    result = source.search(
        ContractorContext(
            internal_id=bidder_id,
            external_id="7",
            contractor_name="Common Builders LLC",
            city="Madison",
            state="WI",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.source_record_id == "100000002"


def test_executor_creates_proposal_and_is_idempotent(isolated_db):
    today = datetime.now(timezone.utc).date()
    official_name = "SAM_Exclusions_Public_Extract_V2_" + today.strftime("%y%j") + ".CSV"
    store_uploaded_extract(_fixture_bytes(), official_name, cache_dir=db.DATA_DIR / "source_cache" / "sam")

    db.replace_master_database(
        "master.csv",
        [
            "id", "contractor_name", "related_companies", "address_1", "city", "state", "zip",
            "state_federal_debarment",
        ],
        [{
            "id": "1220",
            "contractor_name": "ACME Electric LLC",
            "related_companies": "",
            "address_1": "123 Main St",
            "city": "Madison",
            "state": "WI",
            "zip": "53703",
            "state_federal_debarment": "N",
        }],
        str(isolated_db / "master.csv"),
    )

    run = db.create_run(None, ["sam"], 1)
    execution = execute_research_run(run["id"])
    assert execution["executed"] == 1
    assert execution["proposal_count"] == 1
    assert execution["run"]["status"] == "completed"

    tasks = list_tasks(run["id"])
    assert tasks[0]["status"] == SourceResultStatus.SUCCESS_WITH_FINDINGS.value
    proposals = db.list_review_proposals()
    assert len(proposals) == 1
    assert proposals[0]["field_name"] == "state_federal_debarment"
    assert proposals[0]["proposed_value"] == "Y"

    bidder_id = db.active_bidder_ids()[0]
    assert db.get_bidder(bidder_id)["state_federal_debarment"] == "N"
    review_change(proposals[0]["id"], decision="approved", actor="tester")
    assert db.get_bidder(bidder_id)["state_federal_debarment"] == "Y"

    second = execute_research_run(run["id"])
    assert second["executed"] == 0
    with db.connect() as conn:
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM evidence_snapshots WHERE research_run_id=?", (run["id"],)
        ).fetchone()[0]
    assert snapshot_count == 1
