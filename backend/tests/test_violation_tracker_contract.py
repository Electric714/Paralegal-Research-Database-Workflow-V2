from __future__ import annotations

from app.research.field_mappings import owned_fields_for, source_owns_field
from app.research.source_registry import implemented_source_keys
from app.sources import SOURCES


POTENTIALLY_RELATED_MASTER_FIELDS = {
    "osha",
    "osha_severe_violations",
    "state_federal_debarment",
    "federal_court",
    "circuit_court",
    "environmental_violations",
    "prevailing_wage_violations",
    "misc_violations",
    "tax_liability",
}


def test_violation_tracker_stays_evidence_only_until_real_adapter_is_enabled():
    source = next(item for item in SOURCES if item["key"] == "violation_tracker")

    # The public-search design is documented, but there is not yet a production
    # adapter. The UI must not advertise the source as ready before one is
    # registered in the common research pipeline.
    assert source["status"] == "not_implemented"
    assert "violation_tracker" not in implemented_source_keys()
    assert "free public search only" in source["category"].casefold()

    # Violation Tracker is a cross-agency evidence/discrepancy source. It may
    # retain findings that relate to these fields, but it must not create direct
    # master-field proposals. Dedicated authoritative sources remain responsible
    # for any field-level proposal.
    assert owned_fields_for("violation_tracker") == frozenset()
    for field_name in POTENTIALLY_RELATED_MASTER_FIELDS:
        assert source_owns_field("violation_tracker", field_name) is False
