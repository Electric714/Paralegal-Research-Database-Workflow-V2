from __future__ import annotations

from datetime import date

from app.research.models import IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.mn_debarment_verified import VerifiedMinnesotaDebarredVendorsSource


FIXED_TODAY = date(2026, 9, 25)


def _record(record_id: str, name: str, *, address: str = "", location: str = "", debarment_date: str = "", debarment_end: str = "") -> str:
    date_rows = ""
    if debarment_date:
        date_rows += f'<tr><td class="col1">Debarment Date:</td><td>{debarment_date}</td></tr>'
    if debarment_end:
        date_rows += f'<tr><td class="col1">Debarment End Date:</td><td>{debarment_end}</td></tr>'
    return (
        '<div class="results">'
        f'<div class="result-link"><a id="{record_id}Anchor">{name}</a></div>'
        f'<div id="{record_id}" style="display:none;"><table><tbody>'
        f'<tr><td colspan="2">{address}</td></tr>'
        f'<tr><td colspan="2">{location}</td></tr>'
        '<tr><td colspan="2" class="bottom-space">Owner/Officer: Test Owner</td></tr>'
        f'{date_rows}'
        '</tbody></table></div>'
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


def _contractor(name: str, *, address: str = "123 Master St", city: str = "Madison", state: str = "WI", zip_code: str = "53703") -> ContractorContext:
    return ContractorContext(
        internal_id=999999,
        external_id="999999",
        contractor_name=name,
        address_1=address,
        city=city,
        state=state,
        zip=zip_code,
    )


def _source(tmp_path, html: str) -> VerifiedMinnesotaDebarredVendorsSource:
    source = VerifiedMinnesotaDebarredVendorsSource(
        cache_dir=tmp_path / "mn-verified-cache",
        today=FIXED_TODAY,
        html_override=html,
    )
    source.prepare()
    return source


def test_similar_but_different_name_is_verified_not_listed(tmp_path):
    source = _source(
        tmp_path,
        _page(
            _record(
                "1",
                "A1 Transportation LLC",
                address="456 Other Rd",
                location="Minneapolis, MN 55401",
            )
        ),
    )

    result = source.search(_contractor("#1 TRANSPORTATION LLC"))

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.is_clean_negative
    assert result.normalized_payload["verification_status"] == "VERIFIED_NOT_LISTED"
    assert result.normalized_payload["matched_names"] == []


def test_exact_normalized_name_is_finding_even_when_address_differs(tmp_path):
    source = _source(
        tmp_path,
        _page(
            _record(
                "92",
                "3G Contracting Inc.",
                address="640 Railroad Drive Ste 600",
                location="Norwood, MN 55368",
                debarment_date="9/26/2025",
                debarment_end="9/26/2026",
            )
        ),
    )

    result = source.search(
        _contractor(
            "3G CONTRACTING INC",
            address="999 Completely Different Road",
            city="Somewhere Else",
            state="WI",
            zip_code="53703",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.normalized_payload["verification_status"] == "LISTED_ON_SOURCE"
    assert result.normalized_payload["matched_names"] == ["3G Contracting Inc."]
    assert result.evidence[0].observed_value == "Y"


def test_incomplete_master_list_can_never_be_verified_clean(tmp_path):
    source = _source(
        tmp_path,
        _page(
            _record("1", "First Vendor"),
            total=2,
            shown_end=1,
        ),
    )

    result = source.search(_contractor("Completely Unrelated LLC"))

    assert result.status == SourceResultStatus.PAGINATION_INCOMPLETE
    assert not result.is_clean_negative
