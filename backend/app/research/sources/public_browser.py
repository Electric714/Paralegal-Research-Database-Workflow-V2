from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
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

    It does not synthesize challenge answers, solve CAPTCHAs, or retry explicit
    HTTP 429 rate limits. A caller can opt into a headed browser and an
    application-owned persistent profile when the public site behaves differently
    from a background/headless browser. Persistent profiles only preserve normal
    browser state (cookies/local storage) between runs.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str],
        warmup_url: str | None = None,
        timeout_ms: int = 45_000,
        headless: bool = True,
        profile_dir: str | None = None,
        blocked_retry_wait_ms: int = 0,
    ) -> None:
        self.allowed_hosts = {host.casefold().rstrip(".") for host in allowed_hosts}
        self.warmup_url = warmup_url
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.profile_dir = profile_dir
        self.blocked_retry_wait_ms = max(0, int(blocked_retry_wait_ms))
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
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - dependency failure is environment-specific
            raise BrowserUnavailableError(f"Playwright is unavailable: {exc}") from exc

        self._playwright = sync_playwright().start()
        launch_errors: list[str] = []

        if self.profile_dir:
            profile_path = Path(self.profile_dir).expanduser()
            profile_path.mkdir(parents=True, exist_ok=True)
            for channel in ("msedge", "chrome"):
                try:
                    self._context = self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_path),
                        channel=channel,
                        headless=self.headless,
                        locale="en-US",
                    )
                    break
                except Exception as exc:  # pragma: no cover - depends on local browser install
                    launch_errors.append(f"{channel}: {exc}")

            if self._context is None:
                try:
                    self._context = self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_path),
                        headless=self.headless,
                        locale="en-US",
                    )
                except Exception as exc:  # pragma: no cover - depends on local browser install
                    launch_errors.append(f"chromium: {exc}")
                    self.close()
                    raise BrowserUnavailableError(
                        "No usable Chromium-family browser was available for the public-site browser session. "
                        "Microsoft Edge is expected on the supported Windows workstation. "
                        + " | ".join(launch_errors)
                    ) from exc

            pages = list(self._context.pages)
            self._page = pages[0] if pages else self._context.new_page()
            return

        for channel in ("msedge", "chrome"):
            try:
                self._browser = self._playwright.chromium.launch(channel=channel, headless=self.headless)
                break
            except Exception as exc:  # pragma: no cover - depends on local browser install
                launch_errors.append(f"{channel}: {exc}")

        if self._browser is None:
            try:
                # This works when the Playwright-managed Chromium runtime has already
                # been installed (for example by a developer or a future launcher).
                self._browser = self._playwright.chromium.launch(headless=self.headless)
            except Exception as exc:  # pragma: no cover - depends on local browser install
                launch_errors.append(f"chromium: {exc}")
                self.close()
                raise BrowserUnavailableError(
                    "No usable Chromium-family browser was available for the public-site browser session. "
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

    def _body_text(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return ""

    def _raise_if_challenge_text(self, text: str) -> None:
        match = CHALLENGE_RE.search(text or "")
        if match:
            raise BrowserBlockedError(
                f"Public site presented an interactive security challenge: {match.group(0)!r}."
            )

    def _reload_after_block_wait(self) -> int | None:
        """Give normal site JavaScript/cookies one chance to settle, then reload.

        This deliberately does not click, answer, or solve any challenge. It merely
        allows a headed browser a short grace period before one ordinary reload.
        """
        if self._page is None or self.blocked_retry_wait_ms <= 0:
            return None
        try:
            self._page.wait_for_timeout(self.blocked_retry_wait_ms)
            response = self._page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._settle()
            return response.status if response is not None else 200
        except Exception:
            return None

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
        status = response.status if response is not None else 200
        if status == 429:
            raise BrowserBlockedError("Public-site browser warm-up returned HTTP 429.")
        if status in {401, 403}:
            retried = self._reload_after_block_wait()
            if retried is not None:
                status = retried
        if status in {401, 403, 429}:
            raise BrowserBlockedError(f"Public-site browser warm-up returned HTTP {status}.")
        body_text = self._body_text()
        if CHALLENGE_RE.search(body_text) and self.blocked_retry_wait_ms:
            retried = self._reload_after_block_wait()
            if retried is not None and retried < 400:
                body_text = self._body_text()
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
            raise BrowserFetchError(f"Browser navigation failed for {url}: {exc}") from exc
        self._settle()
        status = response.status if response is not None else 200
        if status == 429:
            raise BrowserBlockedError(f"Public-site browser request returned HTTP 429 for {url}.")
        if status in {401, 403}:
            retried = self._reload_after_block_wait()
            if retried is not None:
                status = retried
        if status in {401, 403, 429}:
            raise BrowserBlockedError(f"Public-site browser request returned HTTP {status} for {url}.")
        if status >= 400:
            raise BrowserFetchError(f"Public-site browser request returned HTTP {status} for {url}.")
        body_text = self._body_text()
        if CHALLENGE_RE.search(body_text) and self.blocked_retry_wait_ms:
            retried = self._reload_after_block_wait()
            if retried is not None and retried < 400:
                status = retried
                body_text = self._body_text()
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
            raise BrowserFetchError(f"Browser-context resource request failed for {url}: {exc}") from exc
        if response.status in {401, 403, 429}:
            raise BrowserBlockedError(
                f"Public-site browser-context request returned HTTP {response.status} for {url}."
            )
        if response.status >= 400:
            raise BrowserFetchError(
                f"Public-site browser-context request returned HTTP {response.status} for {url}."
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
        # A persistent context owns its browser process, so closing the context is
        # sufficient. A normal context/browser pair needs both calls.
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
