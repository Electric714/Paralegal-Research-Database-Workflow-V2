from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


CHALLENGE_RE = re.compile(
    r"(?:captcha|verify\s+(?:that\s+)?you\s+are\s+human|access\s+denied|"
    r"unusual\s+traffic|security\s+check|checking\s+your\s+browser|"
    r"just\s+a\s+moment|enable\s+javascript\s+and\s+cookies)",
    re.IGNORECASE,
)

# Used only for direct HTTP first attempts. Browser fallbacks use the installed
# browser's own network stack and normal browser identity.
STANDARD_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class BrowserFetchError(RuntimeError):
    pass


class BrowserBlockedError(BrowserFetchError):
    pass


class BrowserUnavailableError(BrowserFetchError):
    pass


@dataclass(frozen=True)
class BrowserFetchResult:
    text: str
    final_url: str
    status_code: int
    content_type: str = ""


class PublicBrowserSession:
    """Small, conservative Playwright session for public-record GET fallbacks.

    It does not synthesize challenge answers, solve CAPTCHAs, mutate cookies, or
    retry explicit HTTP 429 rate limits. Its purpose is simply to use the same
    browser stack a person uses when a public site rejects a bare HTTP client.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        warmup_url: str | None = None,
        timeout_ms: int = 45_000,
    ) -> None:
        self.allowed_hosts = {host.casefold().rstrip(".") for host in allowed_hosts}
        self.warmup_url = warmup_url
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._warmed = False

    def _validate_url(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if parts.scheme != "https" or host not in self.allowed_hosts:
            raise BrowserFetchError(f"Browser fallback refused URL outside the allowed public host set: {url}")

    def _launch(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - dependency failure is environment-specific
            raise BrowserUnavailableError(f"Playwright is unavailable: {exc}") from exc

        self._playwright = sync_playwright().start()
        launch_errors: list[str] = []
        for channel in ("msedge", "chrome"):
            try:
                self._browser = self._playwright.chromium.launch(channel=channel, headless=True)
                break
            except Exception as exc:  # pragma: no cover - depends on local browser install
                launch_errors.append(f"{channel}: {exc}")

        if self._browser is None:
            try:
                # This works when the Playwright-managed Chromium runtime has already
                # been installed (for example by a developer or a future launcher).
                self._browser = self._playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - depends on local browser install
                launch_errors.append(f"chromium: {exc}")
                self.close()
                raise BrowserUnavailableError(
                    "No usable Chromium-family browser was available for the public-site fallback. "
                    "Microsoft Edge is expected on the supported Windows workstation. "
                    + " | ".join(launch_errors)
                ) from exc

        self._context = self._browser.new_context(locale="en-US")
        self._page = self._context.new_page()

    def _settle(self) -> None:
        if self._page is None:
            return
        try:
            self._page.wait_for_load_state("networkidle", timeout=2_500)
        except Exception:
            pass
        try:
            self._page.wait_for_timeout(250)
        except Exception:
            pass

    def _raise_if_challenge_text(self, text: str) -> None:
        match = CHALLENGE_RE.search(text or "")
        if match:
            raise BrowserBlockedError(
                f"Public site presented an interactive security challenge: {match.group(0)!r}."
            )

    def _warm(self) -> None:
        if self._warmed or not self.warmup_url:
            return
        self._validate_url(self.warmup_url)
        self._launch()
        assert self._page is not None
        try:
            response = self._page.goto(
                self.warmup_url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
        except Exception as exc:
            raise BrowserFetchError(f"Browser warm-up failed: {exc}") from exc
        self._settle()
        if response is not None and response.status in {401, 403, 429}:
            raise BrowserBlockedError(
                f"Public-site browser warm-up returned HTTP {response.status}."
            )
        try:
            body_text = self._page.locator("body").inner_text(timeout=5_000)
        except Exception:
            body_text = ""
        self._raise_if_challenge_text(body_text)
        self._warmed = True

    def get_document(self, url: str) -> BrowserFetchResult:
        self._validate_url(url)
        self._launch()
        self._warm()
        assert self._page is not None
        try:
            response = self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception as exc:
            raise BrowserFetchError(f"Browser navigation failed: {exc}") from exc
        self._settle()
        status = response.status if response is not None else 200
        if status in {401, 403, 429}:
            raise BrowserBlockedError(f"Public-site browser request returned HTTP {status}.")
        if status >= 400:
            raise BrowserFetchError(f"Public-site browser request returned HTTP {status}.")
        try:
            body_text = self._page.locator("body").inner_text(timeout=5_000)
        except Exception:
            body_text = ""
        self._raise_if_challenge_text(body_text)
        return BrowserFetchResult(
            text=self._page.content(),
            final_url=self._page.url,
            status_code=status,
            content_type="text/html",
        )

    def get_resource(self, url: str) -> BrowserFetchResult:
        """Fetch raw text (not browser-rendered HTML), sharing browser cookies."""
        self._validate_url(url)
        self._launch()
        self._warm()
        assert self._context is not None
        headers = {"Referer": self.warmup_url} if self.warmup_url else None
        try:
            response = self._context.request.get(url, headers=headers, timeout=self.timeout_ms)
        except Exception as exc:
            raise BrowserFetchError(f"Browser-context resource request failed: {exc}") from exc
        if response.status in {401, 403, 429}:
            raise BrowserBlockedError(
                f"Public-site browser-context request returned HTTP {response.status}."
            )
        if response.status >= 400:
            raise BrowserFetchError(
                f"Public-site browser-context request returned HTTP {response.status}."
            )
        text = response.text()
        self._raise_if_challenge_text(text)
        headers_map = response.headers
        return BrowserFetchResult(
            text=text,
            final_url=response.url,
            status_code=response.status,
            content_type=headers_map.get("content-type", ""),
        )

    def close(self) -> None:
        for item in (self._context, self._browser):
            if item is None:
                continue
            try:
                item.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._warmed = False

    def __enter__(self) -> "PublicBrowserSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
