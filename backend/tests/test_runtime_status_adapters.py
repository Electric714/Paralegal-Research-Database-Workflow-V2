from __future__ import annotations

from app.research.models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from app.research.source_registry import SOURCE_ADAPTERS
from app.research.sources.base import ContractorContext
from app.research.sources.runtime_status_adapters import (
    OperationalSamUploadedExclusionsSource,
    OperationalWisdotContractorSource,
)
from app.research.sources.sam_uploaded import SamUploadedExclusionsSource
from app.research.sources.wisdot import WisdotContractorSource


def contractor() -> ContractorContext:
    return ContractorContext(
        internal_id=1,
        external_id="1",
        contractor_name="TEST CONTRACTOR LLC",
        address_1="1 Main St",
        city="Madison",
        state="WI",
        zip="53703",
    )


def partial_result(source_key: str, *, identity_status=IdentityStatus.NOT_EVALUATED, evidence=None):
    return SourceResult(
        source_key=source_key,
        contractor_id=1,
        status=SourceResultStatus.PARTIAL_RESULTS,
        identity_status=identity_status,
        completeness_status=CompletenessStatus.PARTIAL,
        searched_name="TEST CONTRACTOR LLC",
        evidence=list(evidence or []),
        warnings=["source completed with a completeness caveat"],
    )


def test_registry_uses_operational_status_adapters():
    assert SOURCE_ADAPTERS["sam"] is OperationalSamUploadedExclusionsSource
    assert SOURCE_ADAPTERS["wisdot"] is OperationalWisdotContractorSource


def test_sam_usable_stale_no_match_reports_success_without_becoming_clean_negative(monkeypatch):
    source = OperationalSamUploadedExclusionsSource()
    source.dataset = object()
    synthetic = partial_result("sam")
    monkeypatch.setattr(SamUploadedExclusionsSource, "search", lambda self, ctx: synthetic)

    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert result.normalized_payload["operational_status_normalization"]["clean_negative_allowed"] is False


def test_sam_usable_stale_finding_reports_finding(monkeypatch):
    source = OperationalSamUploadedExclusionsSource()
    source.dataset = object()
    synthetic = partial_result(
        "sam",
        identity_status=IdentityStatus.CONFIRMED,
        evidence=[
            EvidenceRecord(
                field_name="state_federal_debarment",
                observed_value="Y",
                source_record_id="sam-test",
            )
        ],
    )
    monkeypatch.setattr(SamUploadedExclusionsSource, "search", lambda self, ctx: synthetic)

    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.completeness_status == CompletenessStatus.PARTIAL


def test_sam_usable_stale_identity_review_reports_ambiguous(monkeypatch):
    source = OperationalSamUploadedExclusionsSource()
    source.dataset = object()
    synthetic = partial_result("sam", identity_status=IdentityStatus.REVIEW_REQUIRED)
    monkeypatch.setattr(SamUploadedExclusionsSource, "search", lambda self, ctx: synthetic)

    result = source.search(contractor())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.completeness_status == CompletenessStatus.PARTIAL


def test_wisdot_supplemental_failure_does_not_mask_completed_core_lookup(monkeypatch):
    source = OperationalWisdotContractorSource()
    source.datasets = {"debarment": object()}
    synthetic = partial_result("wisdot")
    monkeypatch.setattr(WisdotContractorSource, "search", lambda self, ctx: synthetic)

    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert "supplemental/live-refresh" in result.warnings[-1]


def test_wisdot_partial_remains_partial_when_core_debarment_dataset_is_missing(monkeypatch):
    source = OperationalWisdotContractorSource()
    source.datasets = {"finals_status": object()}
    synthetic = partial_result("wisdot")
    monkeypatch.setattr(WisdotContractorSource, "search", lambda self, ctx: synthetic)

    result = source.search(contractor())

    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
