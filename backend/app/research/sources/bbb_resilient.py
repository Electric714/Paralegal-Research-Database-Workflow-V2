from __future__ import annotations

from typing import Callable

import httpx

from ..models import SourceResultStatus
from .bbb import (
    BBB_BASE,
    BBB_SITEMAP_INDEX,
    BbbBusinessProfileSource,
    BbbRequestError,
)
from .public_browser import (
    BrowserBlockedError,
    BrowserFetchError,
    PublicBrowserSession,
    STANDARD_BROWSER_HEADERS,
)


BBB_BROWSER_HOSTS = {"bbb.org", "www.bbb.org"}
BBB_WARMUP_URL = f"{BBB_BASE}/search/"


class ResilientBbbBusinessProfileSource(BbbBusinessProfileSource):
    """BBB adapter that falls back from bare HTTP to a normal local browser.

    BBB publicly exposes search/profile pages and business-profile sitemaps, but
    frequently challenges direct HTTP clients. We keep the existing sitemap-first
    discovery and strict identity matching, while replacing blocked direct GETs
    with a normal Edge/Chrome session. Interactive challenges are not solved or
    bypassed, and explicit HTTP 429 rate limits remain blocked.
    """

    adapter_version = "1.2.0"
    parser_version = "1.1.1"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        state_ranges=None,
        boundary_margin: int = 1,
        browser_session_factory: Callable[..., PublicBrowserSession] = PublicBrowserSession,
    ) -> None:
        super().__init__(
            transport=transport,
            state_ranges=state_ranges,
            boundary_margin=boundary_margin,
        )
        self._browser_session_factory = browser_session_factory
        self._browser_session: PublicBrowserSession | None = None

    def health_check(self) -> dict:
        result = super().health_check()
        result.update(
            {
                "acquisition_mode": "published_sitemaps_with_browser_fallback",
                "browser_fallback": "system_edge_or_chrome_on_http_403_or_html_challenge",
                "rate_limit_policy": "HTTP 429 is never bypassed",
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
            )
        return self._browser_session

    def _close_browser(self) -> None:
        if self._browser_session is not None:
            try:
                self._browser_session.close()
            finally:
                self._browser_session = None

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        try:
            return super()._get(client, url)
        except BbbRequestError as exc:
            # Respect an explicit rate-limit response rather than trying a second
            # network identity around it.
            if exc.status != SourceResultStatus.BLOCKED or exc.http_status == 429:
                raise

            try:
                if url.lower().split("?", 1)[0].endswith(".xml"):
                    fetched = self._browser().get_resource(url)
                else:
                    fetched = self._browser().get_document(url)
            except BrowserBlockedError as browser_exc:
                raise BbbRequestError(
                    f"BBB still presented an interactive access challenge in a real browser session: {browser_exc}",
                    status=SourceResultStatus.BLOCKED,
                    http_status=exc.http_status,
                ) from browser_exc
            except BrowserFetchError as browser_exc:
                raise BbbRequestError(
                    f"BBB direct request was blocked and the browser fallback failed: {browser_exc}",
                    status=SourceResultStatus.SOURCE_UNAVAILABLE,
                    http_status=exc.http_status,
                ) from browser_exc

            content_type = fetched.content_type or (
                "application/xml" if url.lower().split("?", 1)[0].endswith(".xml") else "text/html"
            )
            request = httpx.Request("GET", fetched.final_url)
            return httpx.Response(
                fetched.status_code,
                text=fetched.text,
                headers={"content-type": content_type},
                request=request,
            )

    def search(self, contractor):
        try:
            return super().search(contractor)
        finally:
            self._close_browser()
