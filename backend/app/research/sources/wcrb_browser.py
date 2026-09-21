from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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
_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HIDDEN_TYPE_RE = re.compile(
    r"\btype\s*=\s*(?:[\"']\s*hidden\s*[\"']|hidden)(?=\s|/?>)",
    re.IGNORECASE,
)
_VALUE_ATTR_RE = re.compile(
    r"\svalue\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE | re.DOTALL,
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


class WcrbEnvironmentError(WcrbProbeError):
    pass


class WcrbTimeoutError(WcrbProbeError):
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
    artifact_manifest: str
    diagnostics: str


def _is_coverage_lookup_path(path: str) -> bool:
    normalized = path or "/"
    return normalized == "/coverage-lookup" or normalized.startswith("/coverage-lookup/")


def browser_request_decision(
    method: str,
    url: str,
    *,
    resource_type: str = "document",
) -> BrowserRequestDecision:
    """Return the narrow browser-egress decision for the WCRB browser workflow.

    WCRB's ASP.NET Web Forms workflow legitimately uses POST. Same-origin form
    submissions under the Coverage Lookup path are explicitly allowed. Chromium
    creates the POST body and ASP.NET state naturally; this code never replays or
    synthesizes form state, NoBot values, or challenge responses.
    """

    method = method.upper().strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return BrowserRequestDecision(False, "malformed_url")

    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host:
        return BrowserRequestDecision(False, "https_required")

    if host in WCRB_HOSTS:
        if method in {"GET", "HEAD"}:
            return BrowserRequestDecision(True, "wcrb_read")
        if method == "POST" and _is_coverage_lookup_path(parts.path):
            return BrowserRequestDecision(True, "wcrb_coverage_lookup_post")
        return BrowserRequestDecision(False, "unexpected_wcrb_method_or_path")

    if method in {"GET", "HEAD"} and host in PUBLIC_STATIC_GET_HOSTS:
        return BrowserRequestDecision(True, "required_public_static_asset")

    if method == "POST":
        return BrowserRequestDecision(False, "external_post_blocked")
    if resource_type in {"websocket", "eventsource"}:
        return BrowserRequestDecision(False, "active_external_channel_blocked")
    return BrowserRequestDecision(False, "unneeded_external_request")


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:80] or "unnamed-bidder"


def _safe_request_url(url: str) -> str:
    """Remove query/fragment values before persisting browser request metadata."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return "[malformed-url]"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _redact_hidden_input_values(html: str) -> str:
    """Redact ASP.NET/session hidden-field values from stored HTML snapshots."""

    def redact_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        if not _HIDDEN_TYPE_RE.search(tag):
            return tag
        if _VALUE_ATTR_RE.search(tag):
            return _VALUE_ATTR_RE.sub(' value="[redacted]"', tag)
        return tag

    return _INPUT_TAG_RE.sub(redact_tag, html)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_sanitized_html(path: Path, html: str) -> None:
    path.write_text(_redact_hidden_input_values(html), encoding="utf-8")


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
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        text = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError as exc:
        raise WcrbTimeoutError("WCRB body content did not become readable in time.") from exc
    match = CHALLENGE_TEXT.search(text)
    if match:
        raise WcrbBlockedError(
            f"WCRB presented an interactive anti-automation/security challenge: {match.group(0)!r}."
        )


def _control_manifest(page) -> list[dict[str, Any]]:
    # Never capture values. ASP.NET hidden fields contain transient state that is
    # neither needed for parser work nor appropriate to persist.
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
            "Inspect the saved failure/control diagnostics before changing selectors."
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
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        response = page.goto(COVERAGE_LOOKUP_URL, wait_until="domcontentloaded", timeout=60_000)
    except PlaywrightTimeoutError as exc:
        raise WcrbTimeoutError("Timed out opening the WCRB Coverage Lookup.") from exc

    if response is not None and response.status >= 400:
        raise WcrbProbeError(f"WCRB Coverage Lookup returned HTTP {response.status}.")

    _settle(page)
    _raise_if_blocked(page)

    new_search = page.locator("#ctl00_body_btnOverviewNewSearch")
    if new_search.count() and new_search.first.is_visible():
        try:
            new_search.first.click(timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise WcrbTimeoutError("Timed out starting a new WCRB search.") from exc
        _settle(page)
        _raise_if_blocked(page)

    accept = page.locator("#ctl00_body_btnDisclaimerAccept")
    if accept.count() and accept.first.is_visible():
        try:
            accept.first.click(timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise WcrbTimeoutError("Timed out accepting the WCRB public disclaimer.") from exc
        _settle(page)
        _raise_if_blocked(page)

    search_button = page.locator("#ctl00_body_btnEmployerSearch")
    if not (search_button.count() and search_button.first.is_visible()):
        raise WcrbLayoutError(
            "WCRB did not expose the employer search form after the normal "
            "New Search / disclaimer workflow."
        )


def _safe_page_snapshot(page, output_dir: Path, *, stem: str) -> dict[str, str]:
    paths: dict[str, str] = {}
    try:
        html_path = output_dir / f"{stem}.html"
        _write_sanitized_html(html_path, page.content())
        paths["html"] = str(html_path)
    except Exception:
        pass
    try:
        screenshot_path = output_dir / f"{stem}.png"
        page.screenshot(path=str(screenshot_path), full_page=True, timeout=10_000)
        paths["screenshot"] = str(screenshot_path)
    except Exception:
        pass
    try:
        controls_path = output_dir / f"{stem}-controls.json"
        _write_json(controls_path, _control_manifest(page))
        paths["controls"] = str(controls_path)
    except Exception:
        pass
    return paths


def _write_request_log(
    output_dir: Path,
    request_log: list[dict[str, Any]],
    blocked_requests: list[dict[str, Any]],
) -> Path:
    path = output_dir / "requests.json"
    _write_json(path, {"requests": request_log, "blocked_requests": blocked_requests})
    return path


def _write_artifact_manifest(output_dir: Path, paths: list[Path]) -> Path:
    manifest: list[dict[str, str | int]] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        manifest.append(
            {
                "name": path.name,
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    manifest_path = output_dir / "artifacts.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def _normalize_playwright_error(exc: Exception) -> WcrbProbeError:
    name = type(exc).__name__
    text = str(exc)
    lowered = text.casefold()
    if "executable doesn't exist" in lowered or "browser executable" in lowered:
        return WcrbEnvironmentError(
            "Project-local Chromium is not installed. Run scripts/WCRB_PROBE.ps1 so the "
            "matching Playwright Chromium runtime is installed under .runtime."
        )
    if name == "TimeoutError":
        return WcrbTimeoutError(f"WCRB browser operation timed out: {text}")
    if type(exc).__module__.startswith("playwright"):
        return WcrbProbeError(f"WCRB browser operation failed: {text}")
    if isinstance(exc, WcrbProbeError):
        return exc
    return WcrbProbeError(f"Unexpected WCRB probe failure ({name}): {text}")


def run_wcrb_probe(
    contractor_name: str,
    *,
    headed: bool = False,
    output_root: Path | None = None,
) -> ProbeCapture:
    """Run one normal public WCRB employer-name lookup and save research artifacts.

    This remains an acquisition probe rather than a registered production
    ResearchSource. Chromium is allowed to perform WCRB's ordinary same-origin
    ASP.NET POST requests. No CAPTCHA/NoBot value is solved, generated, replayed,
    or bypassed by this code.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise WcrbEnvironmentError(
            "Playwright is not installed in the project Python environment. Run START_HERE.bat first."
        ) from exc

    contractor_name = contractor_name.strip()
    if not contractor_name:
        raise ValueError("contractor_name is required")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
    root = output_root or DEFAULT_OUTPUT_ROOT
    output_dir = root / f"{timestamp}-{_safe_slug(contractor_name)}"
    output_dir.mkdir(parents=True, exist_ok=False)

    request_log: list[dict[str, Any]] = []
    blocked_requests: list[dict[str, Any]] = []
    browser = None
    context = None
    page = None
    stage = "initializing"

    try:
        with sync_playwright() as playwright:
            stage = "launching_chromium"
            browser = playwright.chromium.launch(headless=not headed)
            context = browser.new_context(
                service_workers="block",
                viewport={"width": 1440, "height": 1100},
                accept_downloads=False,
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
                    "url": _safe_request_url(request.url),
                    "resource_type": request.resource_type,
                    "allowed": decision.allowed,
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

            stage = "opening_search_form"
            _open_search_form(page)

            search_form_html_path = output_dir / "search-form.html"
            _write_sanitized_html(search_form_html_path, page.content())
            search_form_screenshot = output_dir / "search-form.png"
            page.screenshot(path=str(search_form_screenshot), full_page=True, timeout=10_000)

            manifest_path = output_dir / "controls.json"
            _write_json(manifest_path, _control_manifest(page))

            stage = "submitting_employer_search"
            employer_input = _find_employer_name_input(page)
            employer_input.fill(contractor_name, timeout=10_000)
            _find_employer_search_button(page).click(timeout=15_000)
            _settle(page, timeout_ms=12_000)
            _raise_if_blocked(page)

            final_parts = urlsplit(page.url)
            final_host = (final_parts.hostname or "").lower().rstrip(".")
            if final_parts.scheme != "https" or final_host not in WCRB_HOSTS:
                raise WcrbProbeError(
                    f"WCRB search navigated outside the expected HTTPS host: {_safe_request_url(page.url)}"
                )

            stage = "capturing_result"
            result_html_path = output_dir / "result.html"
            _write_sanitized_html(result_html_path, page.content())
            result_screenshot = output_dir / "result.png"
            page.screenshot(path=str(result_screenshot), full_page=True, timeout=10_000)

            request_log_path = _write_request_log(output_dir, request_log, blocked_requests)
            diagnostics_path = output_dir / "diagnostics.json"
            _write_json(
                diagnostics_path,
                {
                    "status": "success",
                    "stage": stage,
                    "searched_name": contractor_name,
                    "final_url": _safe_request_url(page.url),
                    "blocked_request_count": len(blocked_requests),
                },
            )
            artifact_manifest_path = _write_artifact_manifest(
                output_dir,
                [
                    search_form_html_path,
                    search_form_screenshot,
                    manifest_path,
                    result_html_path,
                    result_screenshot,
                    request_log_path,
                    diagnostics_path,
                ],
            )

            capture = ProbeCapture(
                output_dir=str(output_dir),
                searched_name=contractor_name,
                final_url=_safe_request_url(page.url),
                search_form_html=str(search_form_html_path),
                result_html=str(result_html_path),
                search_form_screenshot=str(search_form_screenshot),
                result_screenshot=str(result_screenshot),
                control_manifest=str(manifest_path),
                request_log=str(request_log_path),
                artifact_manifest=str(artifact_manifest_path),
                diagnostics=str(diagnostics_path),
            )
            _write_json(output_dir / "capture.json", asdict(capture))
            return capture
    except Exception as exc:
        normalized = _normalize_playwright_error(exc)
        failure_paths: dict[str, str] = {}
        if page is not None:
            failure_paths = _safe_page_snapshot(page, output_dir, stem="failure")
        try:
            _write_request_log(output_dir, request_log, blocked_requests)
        except Exception:
            pass
        try:
            _write_json(
                output_dir / "diagnostics.json",
                {
                    "status": "failed",
                    "stage": stage,
                    "searched_name": contractor_name,
                    "final_url": _safe_request_url(page.url) if page is not None else None,
                    "error_type": type(normalized).__name__,
                    "error": str(normalized),
                    "blocked_request_count": len(blocked_requests),
                    "failure_artifacts": failure_paths,
                },
            )
        except Exception:
            pass
        try:
            _write_artifact_manifest(output_dir, list(output_dir.iterdir()))
        except Exception:
            pass
        if normalized is exc:
            raise
        raise normalized from exc
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


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
    except (WcrbProbeError, ValueError) as exc:
        print(f"WCRB probe failed safely: {exc}")
        return 2
    except Exception as exc:
        print(f"WCRB probe failed unexpectedly but safely: {type(exc).__name__}: {exc}")
        return 3

    print(json.dumps(asdict(capture), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
