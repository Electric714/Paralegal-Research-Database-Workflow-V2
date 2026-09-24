from __future__ import annotations

from datetime import date

import httpx

from app.research.models import CompletenessStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.osha_resilient import (
    ResilientOshaEstablishmentSource,
    _explicit_no_results,
    _parse_live_search_page,
)


CURRENT_OSHA_NO_RESULTS = """
<html><body>
<h3>Establishment Search</h3>
<form id="estab_search" action="https://www.osha.gov/ords/imis/establishment.search">
<p class="text-center red"><strong><em>Your search did not return any results.</em></strong></p>
<p>Enter an Establishment name, select an OSHA Office, or enter a Site Zip Code.</p>
</form>
</body></html>
"""


def test_current_osha_zero_hit_page_is_not_misclassified_as_layout_changed():
    assert _explicit_no_results(CURRENT_OSHA_NO_RESULTS) is True
    parsed = _parse_live_search_page(CURRENT_OSHA_NO_RESULTS)
    assert parsed.table_found is False
    assert parsed.complete is True
    assert parsed.total_results == 0
    assert parsed.rows == ()


def test_resilient_osha_treats_current_zero_hit_page_as_clean_no_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=CURRENT_OSHA_NO_RESULTS, request=request)

    source = ResilientOshaEstablishmentSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        today=date(1972, 1, 1),
    )
    result = source.search(
        ContractorContext(
            internal_id=1,
            external_id="1",
            contractor_name='"C" SCHLICHT PLUMBING INC',
            address_1="2807 W Vliet St",
            city="Milwaukee",
            state="WI",
            zip="53208",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence == []
    assert result.is_clean_negative is True
