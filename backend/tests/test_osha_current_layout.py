from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.research.models import CompletenessStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.osha import OshaFetchError
from app.research.sources.osha_resilient import (
    SEARCH_RETRY_DELAYS_SECONDS,
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


def test_resilient_osha_retries_transient_504_then_recovers(monkeypatch):
    requests: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) < 3:
            return httpx.Response(504, text="Gateway Timeout", request=request)
        return httpx.Response(200, text=CURRENT_OSHA_NO_RESULTS, request=request)

    monkeypatch.setattr("app.research.sources.osha_resilient.sleep", sleeps.append)
    source = ResilientOshaEstablishmentSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        today=date(1972, 1, 1),
    )

    parsed, _url, status = source._search_request(
        "Acme Roofing",
        "WI",
        date(1972, 1, 1),
        date(1972, 1, 1),
    )

    assert status == 200
    assert parsed.complete is True
    assert parsed.total_results == 0
    assert len(requests) == 3
    assert sleeps == list(SEARCH_RETRY_DELAYS_SECONDS)


def test_resilient_osha_persistent_504_fails_closed_after_bounded_retries(monkeypatch):
    requests: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(504, text="Gateway Timeout", request=request)

    monkeypatch.setattr("app.research.sources.osha_resilient.sleep", sleeps.append)
    source = ResilientOshaEstablishmentSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        today=date(1972, 1, 1),
    )

    with pytest.raises(OshaFetchError) as exc_info:
        source._search_request(
            "Acme Roofing",
            "WI",
            date(1972, 1, 1),
            date(1972, 1, 1),
        )

    failure = exc_info.value
    assert failure.status == SourceResultStatus.SOURCE_UNAVAILABLE
    assert failure.http_status == 504
    assert "after 3 attempts" in str(failure)
    assert len(requests) == 3
    assert sleeps == list(SEARCH_RETRY_DELAYS_SECONDS)
