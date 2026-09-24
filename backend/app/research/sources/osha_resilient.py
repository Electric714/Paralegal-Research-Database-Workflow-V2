from __future__ import annotations

from datetime import date
from typing import Callable

import httpx

from ..models import SourceResultStatus
from .osha import (
    REQUEST_TIMEOUT_SECONDS,
    SEARCH_PAGE_URL,
    SEARCH_URL,
    OshaEstablishmentSource,
    OshaFetchError,
    OshaSearchRow,
    parse_inspection_detail,
    parse_search_page,
)
from .public_browser import (
    BrowserBlockedError,
    BrowserFetchError,
    PublicBrowserSession,
    STANDARD_BROWSER_HEADERS,
)


OSHA_BROWSER_HOSTS = {
    "osha.gov",
    "www.osha.gov",
    "osha.prod.pace.dol.gov",
    "edit-ita.osha.gov",
}


class ResilientOshaEstablishmentSource(OshaEstablishmentSource):
    """OSHA adapter with a normal browser-compatible first attempt and safe fallback.

    The public OSHA IMIS search sometimes rejects bare HTTP clients with 403 even
    though the same public query works in a browser. This adapter first uses a
    warmed HTTP session with ordinary browser headers; only a 403 triggers a real
    local browser session. Explicit 429 rate limits are never bypassed.
    """

    adapter_version = "1.2.0"
    parser_version = "1.1.0"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        today: date | None = None,
        browser_session_factory: Callable[..., PublicBrowserSession] = PublicBrowserSession,
    ) -> None:
        self._owns_http_client = client is None
        if client is None:
            client = httpx.Client(
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers=dict(STANDARD_BROWSER_HEADERS),
            )
        super().__init__(client=client, today=today)
        self._browser_session_factory = browser_session_factory
        self._browser_session: PublicBrowserSession | None = None
        self._http_warmed = False

    def health_check(self) -> dict:
        result = super().health_check()
        result.update(
            {
                "acquisition_mode": "warmed_public_html_with_browser_fallback",
                "browser_fallback": "system_edge_or_chrome_on_http_403",
                "rate_limit_policy": "HTTP 429 is never bypassed",
            }
        )
        return result

    def _browser(self) -> PublicBrowserSession:
        if self._browser_session is None:
            self._browser_session = self._browser_session_factory(
                allowed_hosts=OSHA_BROWSER_HOSTS,
                warmup_url=SEARCH_PAGE_URL,
            )
        return self._browser_session

    def _close_browser(self) -> None:
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            finally:
                self._browser_session = None

    def _warm_http_session(self) -> None:
        if self._http_warmed or not self._owns_http_client:
            return
        self._http_warmed = True
        try:
            self.client.get(
                SEARCH_PAGE_URL,
                params={"lv": "true"},
                headers={"Referer": "https://www.osha.gov/data/"},
            )
        except httpx.HTTPError:
            # The real search request below decides whether browser fallback is needed.
            pass

    @staticmethod
    def _search_params(
        search_name: str,
        state: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, str]:
        return {
            "establishment": search_name,
            "state": state or "all",
            "officetype": "all",
            "office": "all",
            "p_case": "all",
            "p_violations_exist": "both",
            "startmonth": f"{start_date.month:02d}",
            "startday": f"{start_date.day:02d}",
            "startyear": str(start_date.year),
            "endmonth": f"{end_date.month:02d}",
            "endday": f"{end_date.day:02d}",
            "endyear": str(end_date.year),
            "p_show": "100",
        }

    def _search_request(
        self,
        search_name: str,
        state: str,
        start_date: date,
        end_date: date,
    ):
        self._warm_http_session()
        try:
            return super()._search_request(search_name, state, start_date, end_date)
        except OshaFetchError as exc:
            # 429 means the site explicitly told us to slow down; respect it.
            if exc.status != SourceResultStatus.BLOCKED or exc.http_status != 403:
                raise

            params = self._search_params(search_name, state, start_date, end_date)
            target_url = str(httpx.Request("GET", SEARCH_URL, params=params).url)
            try:
                fetched = self._browser().get_document(target_url)
            except BrowserBlockedError as browser_exc:
                raise OshaFetchError(
                    f"OSHA still presented an access challenge in a real browser session: {browser_exc}",
                    status=SourceResultStatus.BLOCKED,
                    http_status=403,
                ) from browser_exc
            except BrowserFetchError as browser_exc:
                raise OshaFetchError(
                    f"OSHA direct request was blocked and the browser fallback failed: {browser_exc}",
                    status=SourceResultStatus.SOURCE_UNAVAILABLE,
                ) from browser_exc

            parsed = parse_search_page(fetched.text)
            if not parsed.table_found and not parsed.complete:
                raise OshaFetchError(
                    "OSHA browser fallback loaded the public page, but the results layout was not recognized.",
                    status=SourceResultStatus.LAYOUT_CHANGED,
                    http_status=fetched.status_code,
                )
            return parsed, fetched.final_url, fetched.status_code

    def _detail_request(self, row: OshaSearchRow) -> dict[str, str]:
        try:
            response = self.client.get(row.detail_url, headers={"Referer": SEARCH_PAGE_URL})
        except httpx.HTTPError:
            response = None

        if response is not None and response.status_code == 200:
            return parse_inspection_detail(response.text)
        if response is not None and response.status_code == 429:
            return {}
        if response is not None and response.status_code not in {403}:
            return {}

        # Only a direct-client 403 gets the normal-browser retry.
        try:
            fetched = self._browser().get_document(row.detail_url)
        except BrowserFetchError:
            return {}
        return parse_inspection_detail(fetched.text)

    def search(self, contractor):
        self._http_warmed = False
        try:
            return super().search(contractor)
        finally:
            self._close_browser()
