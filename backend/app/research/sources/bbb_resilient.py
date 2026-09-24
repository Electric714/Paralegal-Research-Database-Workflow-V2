from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx

from ..models import CompletenessStatus, IdentityStatus, SourceResultStatus
from .base import ContractorContext
from .bbb import (
    BBB_BASE,
    BbbBusinessProfileSource,
    BbbRequestError,
    _aliases,
)
from .public_browser import (
    BrowserBlockedError,
    BrowserFetchError,
    BrowserUnavailableError,
    PublicBrowserSession,
    STANDARD_BROWSER_HEADERS,
)


BBB_BROWSER_HOSTS = {"bbb.org", "www.bbb.org"}
BBB_WARMUP_URL = f"{BBB_BASE}/"
BBB_SEARCH_URL = f"{BBB_BASE}/search"
MAX_SEARCH_ALIASES = 3
MAX_SEARCH_PAGES = 2
_SEARCH_PROFILE_PATH = re.compile(
    r"^/us/[a-z]{2}/[^/]+/profile/[^/]+/[^/?#]+(?:/addressId/\d+)?/?$",
    re.IGNORECASE,
)
_SEARCH_TOTAL_RE = re.compile(r"\bShowing:\s*([\d,]+)\s+results?\b", re.IGNORECASE)


class _SearchDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        for key, value in attrs:
            if key.casefold() == "href" and value:
                self.hrefs.append(value.strip())

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.text_parts.append(value)

    @property
    def visible_text(self) -> str:
        return " ".join(self.text_parts)


def _canonical_profile_url(url: str) -> str | None:
    absolute = urljoin(BBB_BASE, url)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").casefold().rstrip(".")
    if parts.scheme != "https" or host not in BBB_BROWSER_HOSTS or parts.query:
        return None
    if not _SEARCH_PROFILE_PATH.match(parts.path):
        return None
    path = re.sub(r"/addressId/\d+/?$", "", parts.path, flags=re.IGNORECASE).rstrip("/")
    return urlunsplit(("https", "www.bbb.org", path, "", ""))


def parse_search_profile_urls(html: str) -> tuple[list[str], int | None]:
    """Extract only BBB business-profile links from a rendered search page."""
    parser = _SearchDocument()
    parser.feed(html or "")
    urls: list[str] = []
    for href in parser.hrefs:
        profile = _canonical_profile_url(href)
        if profile and profile not in urls:
            urls.append(profile)
    total_match = _SEARCH_TOTAL_RE.search(parser.visible_text)
    total = int(total_match.group(1).replace(",", "")) if total_match else None
    return urls, total


def _search_location(contractor: ContractorContext) -> str:
    city = (contractor.city or "").strip()
    state = (contractor.state or "").strip().upper()
    zip_code = (contractor.zip or "").strip()
    if city and state:
        return f"{city}, {state}"
    if zip_code:
        return zip_code
    return state


def build_bbb_search_url(contractor: ContractorContext, name: str, *, page: int = 1) -> str:
    query: dict[str, str] = {
        "find_country": "USA",
        "find_loc": _search_location(contractor),
        "find_text": name.strip(),
    }
    if page > 1:
        query["page"] = str(page)
    return f"{BBB_SEARCH_URL}?{urlencode(query)}"


def _default_profile_dir() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data) / "ParalegalResearchDesk"
    else:
        base = Path.home() / ".paralegal-research-desk"
    return str(base / "browser" / "bbb")


class ResilientBbbBusinessProfileSource(BbbBusinessProfileSource):
    """BBB adapter that follows the public consumer search flow in a real browser.

    BBB's sitemap/profile endpoints frequently reject background HTTP and headless
    automation. The supported local-workstation path now starts from BBB's normal
    public search page, uses a headed Edge/Chrome session with an application-owned
    persistent browser profile, and verifies only profile URLs returned by that
    search. We never synthesize challenge answers or retry HTTP 429 rate limits.
    """

    adapter_version = "1.3.0"
    parser_version = "1.1.1"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        state_ranges=None,
        boundary_margin: int = 1,
        browser_session_factory: Callable[..., PublicBrowserSession] = PublicBrowserSession,
        browser_first: bool | None = None,
        browser_profile_dir: str | None = None,
    ) -> None:
        # Copy the mapping because search-based discovery is nationwide and may add
        # states dynamically; never mutate the base module's shared mapping.
        effective_ranges = dict(state_ranges) if state_ranges is not None else None
        super().__init__(
            transport=transport,
            state_ranges=effective_ranges,
            boundary_margin=boundary_margin,
        )
        if effective_ranges is None:
            self.state_ranges = dict(self.state_ranges)
        self._browser_session_factory = browser_session_factory
        self._browser_session: PublicBrowserSession | None = None
        self._browser_first = (transport is None) if browser_first is None else bool(browser_first)
        self._browser_profile_dir = browser_profile_dir or _default_profile_dir()

    def health_check(self) -> dict:
        result = super().health_check()
        result.update(
            {
                "acquisition_mode": "bbb_public_search_then_verified_profile",
                "discovery_url": BBB_SEARCH_URL,
                "browser_mode": "headed_system_edge_or_chrome",
                "persistent_browser_profile": True,
                "rate_limit_policy": "HTTP 429 is never bypassed",
                "sitemap_dependency": False,
            }
        )
        return result

    def prepare(self) -> None:
        self._close_browser()
        super().prepare()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            follow_redirects=True,
            timeout=25.0,
            headers=dict(STANDARD_BROWSER_HEADERS),
        )

    def _browser(self) -> PublicBrowserSession:
        if self._browser_session is None:
            self._browser_session = self._browser_session_factory(
                allowed_hosts=BBB_BROWSER_HOSTS,
                warmup_url=BBB_WARMUP_URL,
                headless=False,
                profile_dir=self._browser_profile_dir,
                blocked_retry_wait_ms=5_000,
            )
        return self._browser_session

    def _close_browser(self) -> None:
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            finally:
                self._browser_session = None

    @staticmethod
    def _response_from_browser(fetched) -> httpx.Response:
        request = httpx.Request("GET", fetched.final_url)
        return httpx.Response(
            fetched.status_code,
            text=fetched.text,
            headers={"content-type": fetched.content_type or "text/html"},
            request=request,
        )

    def _browser_document(self, url: str, *, direct_exc: BbbRequestError | None = None) -> httpx.Response:
        try:
            fetched = self._browser().get_document(url)
            return self._response_from_browser(fetched)
        except BrowserBlockedError as browser_exc:
            raise BbbRequestError(
                f"BBB blocked the normal public browser flow at {url}: {browser_exc}",
                status=SourceResultStatus.BLOCKED,
                http_status=direct_exc.http_status if direct_exc else 403,
            ) from browser_exc
        except BrowserUnavailableError as browser_exc:
            raise BbbRequestError(
                f"BBB requires the local browser flow, but Edge/Chrome could not be started: {browser_exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=direct_exc.http_status if direct_exc else None,
            ) from browser_exc
        except BrowserFetchError as browser_exc:
            raise BbbRequestError(
                f"BBB public browser flow failed at {url}: {browser_exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=direct_exc.http_status if direct_exc else None,
            ) from browser_exc

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        # Production BBB acquisition is browser-first. This avoids poisoning the
        # session with a bot-looking request before opening Edge. Unit tests can
        # explicitly exercise the legacy direct->browser fallback by setting
        # browser_first=False.
        if self._browser_first and not url.lower().split("?", 1)[0].endswith(".xml"):
            return self._browser_document(url)

        try:
            return super()._get(client, url)
        except BbbRequestError as exc:
            if exc.status != SourceResultStatus.BLOCKED or exc.http_status == 429:
                raise
            if url.lower().split("?", 1)[0].endswith(".xml"):
                # The resilient production path no longer uses BBB sitemaps. Keep
                # this compatibility path for old tests/fixtures only.
                try:
                    fetched = self._browser().get_resource(url)
                    return self._response_from_browser(fetched)
                except BrowserBlockedError as browser_exc:
                    raise BbbRequestError(
                        f"BBB blocked the browser-context resource request at {url}: {browser_exc}",
                        status=SourceResultStatus.BLOCKED,
                        http_status=exc.http_status,
                    ) from browser_exc
                except BrowserFetchError as browser_exc:
                    raise BbbRequestError(
                        f"BBB browser-context resource request failed at {url}: {browser_exc}",
                        status=SourceResultStatus.SOURCE_UNAVAILABLE,
                        http_status=exc.http_status,
                    ) from browser_exc
            return self._browser_document(url, direct_exc=exc)

    def _discover_search_profiles(
        self,
        contractor: ContractorContext,
        client: httpx.Client,
    ) -> tuple[list[str], bool, list[str], str]:
        aliases = _aliases(contractor)[:MAX_SEARCH_ALIASES]
        collected: list[str] = []
        warnings: list[str] = []
        last_url = BBB_SEARCH_URL

        for alias_index, alias in enumerate(aliases):
            alias_urls: list[str] = []
            alias_total: int | None = None
            for page in range(1, MAX_SEARCH_PAGES + 1):
                search_url = build_bbb_search_url(contractor, alias, page=page)
                last_url = search_url
                response = self._get(client, search_url)
                page_urls, total = parse_search_profile_urls(response.text)
                if alias_total is None:
                    alias_total = total
                for profile_url in page_urls:
                    if profile_url not in alias_urls:
                        alias_urls.append(profile_url)
                    if profile_url not in collected:
                        collected.append(profile_url)

                plausible = self._candidate_urls(contractor, collected)
                complete = alias_total is not None and alias_total <= len(alias_urls)
                if plausible:
                    return collected, complete, warnings, search_url

                # If BBB says every result fits on this page, another page cannot
                # add a candidate. Move to the next legitimate company alias.
                if complete or not page_urls:
                    break

                # Related-company aliases are a fallback. Keep them to one page so
                # a single bidder cannot fan out into a broad BBB crawl.
                if alias_index > 0:
                    break

            if alias_total == 0:
                warnings.append(f"BBB search returned 0 directory results for {alias!r} near {_search_location(contractor)!r}.")

        return collected, False, warnings, last_url

    def search(self, contractor: ContractorContext):
        # Base behavior for missing names remains authoritative and needs no browser.
        if not contractor.contractor_name.strip():
            return super().search(contractor)

        state = (contractor.state or "").strip().upper()
        if not state:
            return self._result(
                contractor,
                status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.UNKNOWN,
                source_url=BBB_SEARCH_URL,
                warnings=["BBB search requires at least the bidder state/location; no negative result was inferred."],
            )

        # The base class uses membership in state_ranges as a guard before discovery.
        # Search-page discovery itself is nationwide, so add the current state only
        # to this adapter instance and preload its contractor-specific search URLs.
        self.state_ranges.setdefault(state, ((1, 1),))

        search_url = BBB_SEARCH_URL
        try:
            with self._client() as client:
                profile_urls, search_complete, warnings, search_url = self._discover_search_profiles(contractor, client)
        except BbbRequestError as exc:
            self._close_browser()
            return self._failure_result(contractor, exc, url=search_url)

        self._state_profiles[state] = (profile_urls, search_complete, warnings)
        try:
            return super().search(contractor)
        finally:
            self._close_browser()
