from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


COVERAGE_LOOKUP_URL = "https://www.wcrb.org/coverage-lookup/"
WCRB_HOSTS = frozenset({"wcrb.org", "www.wcrb.org"})
# WCRB currently loads Bootstrap/Popper from jsDelivr. Third-party analytics are
# deliberately unnecessary for the research workflow and remain blocked.
PUBLIC_STATIC_GET_HOSTS = frozenset({"cdn.jsdelivr.net"})
CHALLENGE_TEXT = re.compile(
    r"captcha|verify\s+(?:that\s+)?you\s+are\s+human|access\s+denied|"
    r"unusual\s+traffic|automated\s+requests|security\s+check",
    re.IGNORECASE,
)
DEFAULT_OUTPUT_ROOT = (
    Path(__file__).resolve().parents[3] / "data" / "source_cache" / "wcrb" / "probe"
)


class WcrbProbeError(RuntimeError):
    pass


class WcrbBlockedError(WcrbProbeError):
    pass


class WcrbLayoutError(WcrbProbeError):
    pass


@dataclass(frozen=True)
class BrowserRequestDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ProbeCapture:
    output_dir: str
    searched_name: str
    final_url: str
    search_form_html: str
    result_html: str
    search_form_screenshot: str
    result_screenshot: str
    control_manifest: str
    request_log: str


def browser_request_decision(
    method: str,
    url: str,
    *,
    resource_type: str = "document",
) -> BrowserRequestDecision:
    """Return the narrow browser-egress decision for the WCRB spike.

    Important: WCRB's ASP.NET Web Forms workflow legitimately uses POST. Those
    same-origin form submissions are explicitly allowed. We do not replay or
    synthesize POST bodies; Chromium generates them through normal form actions.
    """

    method = method.upper().strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return BrowserRequestDecision(False, "malformed_url")

    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in {"http", "https"} or not host:
        return BrowserRequestDecision(False, "unsupported_scheme_or_host")

    if host in WCRB_HOSTS:
        if method in {"GET", "HEAD"}:
            return BrowserRequestDecision(True, "wcrb_read")
        if method == "POST" and parts.path.startswith("/coverage-lookup"):
            return BrowserRequestDecision(True, "wcrb_coverage_lookup_post")
        return BrowserRequestDecision(False, "unexpected_wcrb_method_or_path")

    if method in {"GET", "HEAD"} and host in PUBLIC_STATIC_GET_HOSTS:
        return BrowserRequestDecision(True, "required_public_static_asset")

    # Never allow a form POST, navigation, websocket, or other active request to
    # an unrelated host from this narrowly-scoped source collector.
    if method == "POST":
        return BrowserRequestDecision(False, "external_post_blocked")
    if resource_type in {"websocket", "eventsource"}:
        return BrowserRequestDecision(False, "active_external_channel_blocked")
    return BrowserRequestDecision(False, "unneeded_external_request")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:80] or "unnamed-bidder"


def _settle(page, *, timeout_ms: int = 8_000) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=2_500)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(350)


def _raise_if_blocked(page) -> None:
    text = page.locator("body").inner_text(timeout=5_000)
    match = CHALLENGE_TEXT.search(text)
    if match:
        raise WcrbBlockedError(
            f"WCRB presented an interactive anti-automation/security challenge: {match.group(0)!r}."
        )


def _control_manifest(page) -> list[dict[str, Any]]:
    # Do not capture control values. ASP.NET hidden fields contain session/state
    # values that are neither needed for parser work nor appropriate to persist.
    return page.locator("input, select, textarea, button").evaluate_all(
        """
        (nodes) => nodes.map((node) => ({
          tag: node.tagName.toLowerCase(),
          type: node.getAttribute('type') || '',
          id: node.id || '',
          name: node.getAttribute('name') || '',
          placeholder: node.getAttribute('placeholder') || '',
          ariaLabel: node.getAttribute('aria-label') || '',
          text: (node.innerText || '').trim().slice(0, 200),
          visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
        }))
        """
    )


def _find_employer_name_input(page):
    inputs = page.locator("input[type='text']")
    best = None
    best_score = -10_000
    for index in range(inputs.count()):
        item = inputs.nth(index)
        if not item.is_visible():
            continue
        attrs = " ".join(
            filter(
                None,
                [
                    item.get_attribute("id"),
                    item.get_attribute("name"),
                    item.get_attribute("placeholder"),
                    item.get_attribute("aria-label"),
                ],
            )
        ).casefold()
        score = 0
        if "employer" in attrs:
            score += 6
        if "name" in attrs:
            score += 5
        if "address" in attrs:
            score -= 6
        if "city" in attrs or "zip" in attrs or "date" in attrs:
            score -= 4
        if score > best_score:
            best = item
            best_score = score

    if best is None or best_score < 6:
        raise WcrbLayoutError(
            "Could not confidently identify the visible employer-name input. "
            "Inspect the saved control manifest before changing selectors."
        )
    return best


def _find_employer_search_button(page):
    exact = page.locator("#ctl00_body_btnEmployerSearch")
    if exact.count() and exact.first.is_visible():
        return exact.first

    candidates = page.locator("input[type='submit'], button")
    for index in range(candidates.count()):
        item = candidates.nth(index)
        if not item.is_visible():
            continue
        attrs = " ".join(
            filter(
                None,
                [
                    item.get_attribute("id"),
                    item.get_attribute("name"),
                    item.get_attribute("value"),
                    item.inner_text().strip() if item.evaluate("el => !!el.innerText") else "",
                ],
            )
        ).casefold()
        if "employer" in attrs and "search" in attrs:
            return item
    raise WcrbLayoutError("Could not identify the WCRB employer-name Search control.")


def _open_search_form(page) -> None:
    page.goto(COVERAGE_LOOKUP_URL, wait_until="domcontentloaded", timeout=60_000)
    _settle(page)
    _raise_if_blocked(page)

    new_search = page.locator("#ctl00_body_btnOverviewNewSearch")
    if new_search.count() and new_search.first.is_visible():
        new_search.first.click()
        _settle(page)
        _raise_if_blocked(page)

    accept = page.locator("#ctl00_body_btnDisclaimerAccept")
    if accept.count() and accept.first.is_visible():
        accept.first.click()
        _settle(page)
        _raise_if_blocked(page)

    search_button = page.locator("#ctl00_body_btnEmployerSearch")
    if not (search_button.count() and search_button.first.is_visible()):
        raise WcrbLayoutError(
            "WCRB did not expose the employer search form after the normal "
            "New Search / disclaimer workflow."
        )


def run_wcrb_probe(
    contractor_name: str,
    *,
    headed: bool = False,
    output_root: Path | None = None,
) -> ProbeCapture:
    """Run one normal public WCRB employer-name lookup and save research artifacts.

    This is intentionally a development spike, not a registered production
    ResearchSource. Chromium is allowed to perform WCRB's ordinary same-origin
    ASP.NET POST requests. No CAPTCHA/NoBot value is solved, generated, replayed,
    or bypassed by this code.
    """

    from playwright.sync_api import sync_playwright

    contractor_name = contractor_name.strip()
    if not contractor_name:
        raise ValueError("contractor_name is required")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = output_root or DEFAULT_OUTPUT_ROOT
    output_dir = root / f"{timestamp}-{_safe_slug(contractor_name)}"
    output_dir.mkdir(parents=True, exist_ok=False)

    request_log: list[dict[str, str]] = []
    blocked_requests: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(
            service_workers="block",
            viewport={"width": 1440, "height": 1100},
        )

        def route_request(route) -> None:
            request = route.request
            decision = browser_request_decision(
                request.method,
                request.url,
                resource_type=request.resource_type,
            )
            entry = {
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "allowed": "yes" if decision.allowed else "no",
                "reason": decision.reason,
            }
            request_log.append(entry)
            if decision.allowed:
                route.continue_()
            else:
                blocked_requests.append(entry)
                route.abort()

        context.route("**/*", route_request)
        page = context.new_page()
        try:
            _open_search_form(page)

            search_form_html_path = output_dir / "search-form.html"
            search_form_html_path.write_text(page.content(), encoding="utf-8")
            search_form_screenshot = output_dir / "search-form.png"
            page.screenshot(path=str(search_form_screenshot), full_page=True)

            manifest_path = output_dir / "controls.json"
            manifest_path.write_text(
                json.dumps(_control_manifest(page), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            employer_input = _find_employer_name_input(page)
            employer_input.fill(contractor_name)
            _find_employer_search_button(page).click()
            _settle(page, timeout_ms=12_000)
            _raise_if_blocked(page)

            final_parts = urlsplit(page.url)
            if (final_parts.hostname or "").lower().rstrip(".") not in WCRB_HOSTS:
                raise WcrbProbeError(f"WCRB search navigated outside the expected host: {page.url}")

            result_html_path = output_dir / "result.html"
            result_html_path.write_text(page.content(), encoding="utf-8")
            result_screenshot = output_dir / "result.png"
            page.screenshot(path=str(result_screenshot), full_page=True)

            request_log_path = output_dir / "requests.json"
            request_log_path.write_text(
                json.dumps(
                    {
                        "requests": request_log,
                        "blocked_requests": blocked_requests,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            capture = ProbeCapture(
                output_dir=str(output_dir),
                searched_name=contractor_name,
                final_url=page.url,
                search_form_html=str(search_form_html_path),
                result_html=str(result_html_path),
                search_form_screenshot=str(search_form_screenshot),
                result_screenshot=str(result_screenshot),
                control_manifest=str(manifest_path),
                request_log=str(request_log_path),
            )
            (output_dir / "capture.json").write_text(
                json.dumps(asdict(capture), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return capture
        finally:
            context.close()
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one research-only Chromium lookup against WCRB Coverage Lookup."
    )
    parser.add_argument("--name", required=True, help="Approved bidder/company name to search")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show Chromium while the normal WCRB workflow runs",
    )
    args = parser.parse_args()

    try:
        capture = run_wcrb_probe(args.name, headed=args.headed)
    except WcrbProbeError as exc:
        print(f"WCRB probe failed safely: {exc}")
        return 2

    print(json.dumps(asdict(capture), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
