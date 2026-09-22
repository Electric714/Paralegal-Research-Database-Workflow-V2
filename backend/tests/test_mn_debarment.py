from __future__ import annotations

from datetime import date

import pytest

from app import database as db
from app.research.models import IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.mn_debarment import (
    MinnesotaDebarredVendorsSource,
    parse_minnesota_debarment_page,
)


FIXED_TODAY = date(2026, 9, 21)


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


def _record_html(
    record_id: str,
    name: str,
    *,
    address: str = "",
    location: str = "",
    owner: str = "",
    suspension_date: str = "",
    suspension_end: str = "",
    debarment_date: str = "",
    debarment_end: str = "",
    cause: str = "",
) -> str:
    rows = [
        f'<tr><td colspan="2">{address}</td></tr>',
        f'<tr><td colspan="2">{location}</td></tr>',
        f'<tr><td colspan="2" class="bottom-space">Owner/Officer: {owner}</td></tr>',
    ]
    for label, value in [
        ("Suspension Date:", suspension_date),
        ("Suspension End Date:", suspension_end),
        ("Debarment Date:", debarment_date),
        ("Debarment End Date:", debarment_end),
        ("Cause of Suspension or Debarment:", cause),
    ]:
        if value:
            rows.append(f'<tr><td class="col1">{label}</td><td>{value}</td></tr>')
    return (
        '<div class="results">'
        f'<div class="result-link"><a id="{record_id}Anchor">{name}</a></div>'
        f'<div id="{record_id}" style="display:none;"><table><tbody>{"".join(rows)}</tbody></table></div>'
        '</div>'
    )


def _page(*records: str, total: int | None = None, shown_end: int | None = None) -> str:
    count = len(records) if total is None else total
    end = len(records) if shown_end is None else shown_end
    return (
        '<html><body><h1>Suspended/Debarred Vendor Detailed Information</h1>'
        f'<div class="searchresult_number">Results 1 - {end} of {count}</div>'
        + "".join(records)
        + '</body></html>'
    )


def _contractor(
    name: str,
    *,
    address: str = "640 Railroad Drive Ste 600",
    city: str = "Norwood",
    state: str = "MN",
    zip_code: str = "55368",
) -> ContractorContext:
    return ContractorContext(
        internal_id=1,
        external_id="1",
        contractor_name=name,
        address_1=address,
        city=city,
        state=state,
        zip=zip_code,
    )


def _prepared_source(tmp_path, html: str) -> MinnesotaDebarredVendorsSource:
    source = MinnesotaDebarredVendorsSource(
        cache_dir=tmp_path / "mn-cache",
        today=FIXED_TODAY,
        html_override=html,
    )
    source.prepare()
    return source


def test_parser_extracts_structured_vendor_fields():
    records = parse_minnesota_debarment_page(
        _page(
            _record_html(
                "92",
                "3G Contracting Inc.",
                address="640 Railroad Drive Ste 600",
                location="Norwood, MN 55368",
                owner="Jennifer Rath",
                suspension_date="3/25/2025",
                suspension_end="9/25/2025",
                debarment_date="9/26/2025",
                debarment_end="9/26/2026",
                cause="Failure to provide certified payroll reports.",
            )
        )
    )

    assert len(records) == 1
    record = records[0]
    assert record.source_record_id == "mnosp:92"
    assert record.match_name == "3G Contracting Inc."
    assert record.address_1 == "640 Railroad Drive Ste 600"
    assert record.city == "Norwood"
    assert record.state == "MN"
    assert record.zip_code == "55368"
    assert record.owner_officer == "Jennifer Rath"
    assert record.debarment_date == date(2025, 9, 26)
    assert record.debarment_end_date == date(2026, 9, 26)


def test_active_explicit_debarment_proposes_y(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "92",
                "3G Contracting Inc.",
                address="640 Railroad Drive Ste 600",
                location="Norwood, MN 55368",
                owner="Jennifer Rath",
                debarment_date="9/26/2025",
                debarment_end="9/26/2026",
                cause="Failure to provide certified payroll reports.",
            )
        ),
    )

    result = source.search(_contractor("3G Contracting Inc"))

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.evidence[0].field_name == "state_federal_debarment"
    assert result.evidence[0].observed_value == "Y"
    assert result.normalized_payload["active_debarment_count"] == 1
    assert result.artifacts[0].sha256


def test_exact_name_wrong_address_requires_identity_review(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "92",
                "3G Contracting Inc.",
                address="999 Different Road",
                location="Minneapolis, MN 55401",
                debarment_date="9/26/2025",
                debarment_end="9/26/2026",
            )
        ),
    )

    result = source.search(_contractor("3G Contracting Inc"))

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []
    assert result.normalized_payload["candidate_count"] == 1


def test_individual_record_is_never_auto_confirmed_as_company(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "111",
                "Example Contractor, an individual",
                address="640 Railroad Drive Ste 600",
                location="Norwood, MN 55368",
                debarment_date="9/1/2026",
                debarment_end="9/1/2027",
            )
        ),
    )

    result = source.search(_contractor("Example Contractor"))

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert "Individual-person records" in " ".join(result.warnings)


def test_unrelated_vendor_is_clean_no_match(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "90",
                "Stillwater Masonry Restoration Inc",
                address="401 North Main Street",
                location="Stillwater, MN 55082",
                debarment_date="3/4/2025",
                debarment_end="3/4/2028",
            )
        ),
    )

    result = source.search(
        _contractor(
            "ABEL ELECTRIC INC",
            address="3385 Belmar Rd",
            city="Green Bay",
            state="WI",
            zip_code="54313",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.is_clean_negative
    assert result.normalized_payload["candidate_count"] == 0


def test_active_suspension_is_evidence_only_not_debarment_y(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "121",
                "Susie's Contracting, Inc.",
                address="P.O. Box 487",
                location="Finland, MN 55603",
                suspension_date="2/4/2026",
                suspension_end="10/3/2026",
                cause="Suspended as an organization while debarment is considered.",
            )
        ),
    )

    result = source.search(
        _contractor(
            "Susie's Contracting Inc",
            address="P.O. Box 487",
            city="Finland",
            state="MN",
            zip_code="55603",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.evidence[0].observed_value is None
    assert result.normalized_payload["active_debarment_count"] == 0


def test_source_label_conflict_is_evidence_only(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "122",
                "Example Community Services",
                address="12 Oak Road",
                location="St Paul, MN 55101",
                suspension_date="4/3/2026",
                suspension_end="4/2/2029",
                cause="Debarred as a grantee. Debarred due to violation of contract provisions.",
            )
        ),
    )

    result = source.search(
        _contractor(
            "Example Community Services",
            address="12 Oak Road",
            city="St Paul",
            state="MN",
            zip_code="55101",
        )
    )

    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.evidence[0].observed_value is None
    assert result.normalized_payload["confirmed_records"][0]["current_action_status"] == "SOURCE_LABEL_CONFLICT"
    assert result.warnings


def test_expired_debarment_does_not_propose_current_y(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html(
                "80",
                "John Aish, Inc.",
                address="2649 Cottage Grove Place",
                location="Cottage Grove, MN 55129",
                debarment_date="8/26/2024",
                debarment_end="8/27/2025",
            )
        ),
    )

    result = source.search(
        _contractor(
            "John Aish Inc",
            address="2649 Cottage Grove Place",
            city="Cottage Grove",
            state="MN",
            zip_code="55129",
        )
    )

    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.evidence[0].observed_value is None
    assert result.normalized_payload["confirmed_records"][0]["current_action_status"] == "HISTORICAL_ACTION"


def test_incomplete_result_window_cannot_become_clean_negative(isolated_db, tmp_path):
    source = _prepared_source(
        tmp_path,
        _page(
            _record_html("1", "First Vendor"),
            total=2,
            shown_end=1,
        ),
    )

    result = source.search(_contractor("Completely Unrelated LLC"))

    assert result.status == SourceResultStatus.PAGINATION_INCOMPLETE
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert not result.is_clean_negative


def test_reported_count_mismatch_is_layout_failure(isolated_db, tmp_path):
    html = _page(_record_html("1", "First Vendor"), total=2, shown_end=2)
    source = _prepared_source(tmp_path, html)

    result = source.search(_contractor("Completely Unrelated LLC"))

    assert result.status == SourceResultStatus.LAYOUT_CHANGED
    assert not result.is_clean_negative
