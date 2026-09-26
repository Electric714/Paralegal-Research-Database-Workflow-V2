from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from rapidfuzz import fuzz

from ..matching import normalize_text
from ..models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource
from .wcca import PUBLIC_WCCA_URL, build_operator_result, build_search_plan

PUBLIC_WCCA_SEARCH_URL = "https://wcca.wicourts.gov/case.html"
PUBLIC_BROWSER_ACQUISITION = "public_wcca_browser_human_challenge"


class WccaBrowserError(RuntimeError):
    pass


class WccaBrowserUnavailable(WccaBrowserError):
    pass


class WccaHumanActionTimeout(WccaBrowserError):
    pass


class WccaLayoutChanged(WccaBrowserError):
    pass


@dataclass(frozen=True)
class WccaSearchHit:
    case_number: str
    filing_date: str
    county: str
    case_status: str
    matched_party_name: str
    caption: str
    case_url: str


@dataclass(frozen=True)
class WccaBusinessSearchResult:
    searched_name: str
    hits: tuple[WccaSearchHit, ...]
    completed: bool = True


def _runtime_profile_dir() -> Path:
    configured = os.getenv("WCCA_BROWSER_PROFILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / ".runtime" / "browser-profiles" / "wcca"


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _case_type_code(case_number: str) -> str:
    match = re.match(r"^\d{4}([A-Za-z]{2,3})", str(case_number or "").strip())
    return match.group(1).upper() if match else ""


def _name_similarity(left: str, right: str) -> float:
    return fuzz.WRatio(normalize_text(left), normalize_text(right)) / 100.0


def _is_exact_party_match(searched_name: str, observed_name: str) -> bool:
    left = normalize_text(searched_name)
    right = normalize_text(observed_name)
    return bool(left and right and left == right)


class WccaPublicBrowser:
    """Live browser workflow for the public WCCA site without the paid REST API.

    The browser uses the same public pages a human uses. It never clicks, answers,
    token-injects, or otherwise bypasses hCaptcha. If WCCA presents an interactive
    challenge, the browser is brought to the foreground and waits for the operator
    to complete it normally before continuing.
    """

    def __init__(
        self,
        *,
        profile_dir: Path | None = None,
        headless: bool | None = None,
        navigation_timeout_ms: int = 60_000,
        human_action_timeout_ms: int | None = None,
    ) -> None:
        self.profile_dir = profile_dir or _runtime_profile_dir()
        self.headless = _truthy_env("WCCA_HEADLESS") if headless is None else bool(headless)
        self.navigation_timeout_ms = int(navigation_timeout_ms)
        configured_wait = os.getenv("WCCA_HUMAN_TIMEOUT_SECONDS", "").strip()
        if human_action_timeout_ms is not None:
            self.human_action_timeout_ms = int(human_action_timeout_ms)
        elif configured_wait.isdigit():
            self.human_action_timeout_ms = max(30_000, int(configured_wait) * 1000)
        else:
            self.human_action_timeout_ms = 180_000
        self._playwright = None
        self._context = None
        self._page = None

    def _launch(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - environment-specific
            raise WccaBrowserUnavailable(f"Playwright is unavailable: {exc}") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        errors: list[str] = []
        channels = [os.getenv("WCCA_BROWSER_CHANNEL", "").strip(), "msedge", "chrome"]
        channels = [item for item in dict.fromkeys(channels) if item]
        launch_args = [] if self.headless else ["--start-maximized"]

        for channel in channels:
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel=channel,
                    headless=self.headless,
                    locale="en-US",
                    viewport=None if not self.headless else {"width": 1440, "height": 1000},
                    args=launch_args,
                )
                break
            except Exception as exc:  # pragma: no cover - local browser availability
                errors.append(f"{channel}: {exc}")

        if self._context is None:
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=self.headless,
                    locale="en-US",
                    viewport=None if not self.headless else {"width": 1440, "height": 1000},
                    args=launch_args,
                )
            except Exception as exc:  # pragma: no cover - local browser availability
                errors.append(f"chromium: {exc}")
                self.close()
                raise WccaBrowserUnavailable(
                    "No usable Chromium-family browser was available for WCCA. "
                    "Microsoft Edge is expected on the supported Windows workstation. "
                    + " | ".join(errors)
                ) from exc

        pages = list(self._context.pages)
        self._page = pages[0] if pages else self._context.new_page()
        self._page.set_default_timeout(self.navigation_timeout_ms)
        self._page.set_default_navigation_timeout(self.navigation_timeout_ms)

    def _settle(self, milliseconds: int = 300) -> None:
        if self._page is None:
            return
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        try:
            self._page.wait_for_timeout(milliseconds)
        except Exception:
            pass

    def _search_form_ready(self) -> bool:
        if self._page is None:
            return False
        try:
            return self._page.locator("input[name='businessName']").count() > 0
        except Exception:
            return False

    def _results_ready(self) -> bool:
        if self._page is None:
            return False
        try:
            if self._page.locator("table#caseSearchResults").count() > 0:
                return True
            headings = [text.strip().casefold() for text in self._page.locator("h2").all_inner_texts()]
            return "case search results" in headings
        except Exception:
            return False

    def _ensure_search_page(self) -> None:
        self._launch()
        assert self._page is not None

        if self._search_form_ready():
            return

        try:
            self._page.goto(PUBLIC_WCCA_URL, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
        except Exception as exc:
            raise WccaBrowserError(f"Could not open public WCCA: {exc}") from exc
        self._settle()

        # This is WCCA's normal public acknowledgement button, not a CAPTCHA.
        try:
            agree = self._page.get_by_role("button", name="I agree")
            if agree.count() and agree.first.is_visible():
                agree.first.click()
                self._settle(500)
        except Exception as exc:
            raise WccaLayoutChanged(f"WCCA acknowledgement gate changed: {exc}") from exc

        if self._search_form_ready():
            return

        try:
            self._page.goto(PUBLIC_WCCA_SEARCH_URL, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
        except Exception as exc:
            raise WccaBrowserError(f"Could not open WCCA case search: {exc}") from exc
        self._settle()
        if not self._search_form_ready():
            raise WccaLayoutChanged("WCCA case search loaded without the expected Business name field.")

    def _return_to_search(self) -> None:
        assert self._page is not None
        if self._search_form_ready():
            return
        try:
            link = self._page.get_by_role("link", name="Return to search")
            if link.count() and link.first.is_visible():
                link.first.click()
                self._settle(300)
        except Exception:
            pass
        if not self._search_form_ready():
            try:
                self._page.goto(PUBLIC_WCCA_SEARCH_URL, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
            except Exception as exc:
                raise WccaBrowserError(f"Could not return to WCCA search: {exc}") from exc
            self._settle()
        if not self._search_form_ready():
            raise WccaLayoutChanged("WCCA search form could not be restored after a result page.")

    def _wait_for_results_after_submit(self) -> None:
        assert self._page is not None
        for _ in range(20):
            if self._results_ready():
                return
            try:
                self._page.wait_for_timeout(250)
            except Exception:
                pass

        # If hCaptcha asks for human interaction, do not touch it. Bring the normal
        # browser forward and wait for the operator to complete the challenge.
        try:
            if not self.headless:
                self._page.bring_to_front()
        except Exception:
            pass

        waited = 0
        interval = 500
        while waited < self.human_action_timeout_ms:
            if self._results_ready():
                return
            try:
                self._page.wait_for_timeout(interval)
            except Exception:
                pass
            waited += interval

        raise WccaHumanActionTimeout(
            "WCCA did not reach the results page. An hCaptcha/security challenge or stalled public search may require operator action in the opened browser. "
            "No CAPTCHA was solved or bypassed by the application."
        )

    def _show_all_rows_if_possible(self) -> None:
        assert self._page is not None
        selectors = [
            "select[name='caseSearchResults_length']",
            "#caseSearchResults_wrapper select",
        ]
        for selector in selectors:
            try:
                select = self._page.locator(selector).first
                if not select.count():
                    continue
                labels = [text.strip() for text in select.locator("option").all_inner_texts()]
                if "All" in labels:
                    select.select_option(label="All")
                    self._page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    def _read_current_rows(self) -> list[WccaSearchHit]:
        assert self._page is not None
        table = self._page.locator("table#caseSearchResults")
        if not table.count():
            raise WccaLayoutChanged("WCCA results page no longer contains table#caseSearchResults.")

        hits: list[WccaSearchHit] = []
        rows = table.locator("tbody tr")
        for index in range(rows.count()):
            row = rows.nth(index)
            cells = row.locator("td")
            count = cells.count()
            if count == 0:
                continue
            if count == 1:
                text = cells.nth(0).inner_text().strip().casefold()
                if "no matching" in text or "no data" in text or "no records" in text:
                    continue
                raise WccaLayoutChanged(f"Unexpected WCCA single-cell result row: {text[:120]}")
            if count < 7:
                raise WccaLayoutChanged(f"WCCA result row has {count} cells; expected at least 7.")

            case_number = cells.nth(0).inner_text().strip()
            link = cells.nth(0).locator("a.case-link")
            href = link.first.get_attribute("href") if link.count() else None
            if not case_number:
                continue
            hits.append(
                WccaSearchHit(
                    case_number=case_number,
                    filing_date=cells.nth(1).inner_text().strip(),
                    county=cells.nth(2).inner_text().strip(),
                    case_status=cells.nth(3).inner_text().strip(),
                    matched_party_name=cells.nth(4).inner_text().strip(),
                    caption=cells.nth(6).inner_text().strip(),
                    case_url=urljoin("https://wcca.wicourts.gov/", href or ""),
                )
            )
        return hits

    def _read_all_rows(self) -> tuple[WccaSearchHit, ...]:
        assert self._page is not None
        self._show_all_rows_if_possible()
        collected: list[WccaSearchHit] = []
        seen: set[tuple[str, str]] = set()

        for _ in range(100):
            for hit in self._read_current_rows():
                key = (
                    re.sub(r"[^A-Za-z0-9]", "", hit.case_number).upper(),
                    normalize_text(hit.matched_party_name),
                )
                if key not in seen:
                    seen.add(key)
                    collected.append(hit)

            next_button = self._page.locator("#caseSearchResults_next")
            if not next_button.count():
                break
            classes = (next_button.first.get_attribute("class") or "").casefold()
            if "disabled" in classes or "unavailable" in classes:
                break
            try:
                next_button.first.click()
                self._page.wait_for_timeout(200)
            except Exception as exc:
                raise WccaLayoutChanged(f"WCCA result pagination failed: {exc}") from exc
        else:
            raise WccaLayoutChanged("WCCA result pagination exceeded the 100-page safety limit.")

        return tuple(collected)

    def search_business(self, business_name: str) -> WccaBusinessSearchResult:
        name = re.sub(r"\s+", " ", str(business_name or "").strip())
        if not name:
            raise ValueError("WCCA business search requires a non-empty name.")
        self._ensure_search_page()
        self._return_to_search()
        assert self._page is not None

        field = self._page.locator("input[name='businessName']")
        search_button = self._page.locator("button[name='search']")
        if not field.count() or not search_button.count():
            raise WccaLayoutChanged("WCCA search controls changed; Business name/Search controls were not found.")

        try:
            field.fill(name)
            search_button.first.click()
        except Exception as exc:
            raise WccaBrowserError(f"Could not submit WCCA business search for {name!r}: {exc}") from exc

        self._wait_for_results_after_submit()
        self._settle(200)
        hits = self._read_all_rows()
        return WccaBusinessSearchResult(searched_name=name, hits=hits, completed=True)

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._playwright = None


class WccaPublicBrowserSource(ResearchSource):
    source_key = "wcca"
    display_name = "Wisconsin Circuit Court Access / CCAP"
    adapter_version = "2.0.0"
    parser_version = "public-results-v1"

    def __init__(self, browser_factory: Callable[[], Any] | None = None) -> None:
        self._browser_factory = browser_factory or WccaPublicBrowser
        self._browser: Any | None = None

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "status": "ready_public_browser",
            "implemented": True,
            "public_url": PUBLIC_WCCA_URL,
            "acquisition_method": PUBLIC_BROWSER_ACQUISITION,
            "public_site_automation": True,
            "rest_api_used": False,
            "captcha_bypass": False,
            "human_challenge_handling": "operator_completes_challenge_in_opened_browser_only_when_present",
            "persistent_browser_profile": str(_runtime_profile_dir()),
            "negative_field_updates": False,
        }

    def prepare(self) -> None:
        # Lazy on purpose: browser failures become explicit source statuses instead of
        # opaque adapter-preparation/parser errors.
        return None

    def _ensure_browser(self) -> Any:
        if self._browser is None:
            self._browser = self._browser_factory()
        return self._browser

    def _decorate_result(
        self,
        result: SourceResult,
        *,
        searches: list[WccaBusinessSearchResult],
        ambiguous_hits: list[dict[str, Any]],
    ) -> SourceResult:
        result.adapter_version = self.adapter_version
        result.parser_version = self.parser_version
        result.acquisition_method = PUBLIC_BROWSER_ACQUISITION
        payload = dict(result.normalized_payload or {})
        payload["browser_automation"] = {
            "public_site": True,
            "rest_api_used": False,
            "captcha_bypass": False,
            "searched_aliases": [item.searched_name for item in searches],
            "queries_completed": len(searches),
            "raw_result_counts": {item.searched_name: len(item.hits) for item in searches},
            "ambiguous_candidates": ambiguous_hits,
        }
        result.normalized_payload = payload
        return self.validate_result(result)

    def _cases_from_hits(self, records: list[tuple[str, WccaSearchHit]]) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for searched_name, hit in records:
            key = (
                re.sub(r"[^A-Za-z0-9]", "", hit.case_number).upper(),
                normalize_text(hit.matched_party_name),
            )
            if key in seen:
                continue
            seen.add(key)
            cases.append(
                {
                    "case_number": hit.case_number,
                    "county": hit.county,
                    "matched_party_name": hit.matched_party_name,
                    "case_type": _case_type_code(hit.case_number),
                    "case_status": hit.case_status,
                    "filing_date": hit.filing_date,
                    "disposition": "",
                    "case_url": hit.case_url,
                    "note": f"Captured automatically from WCCA public search results for {searched_name!r}. Caption: {hit.caption}",
                }
            )
        return cases

    def _problem_result(
        self,
        contractor: ContractorContext,
        *,
        status: SourceResultStatus,
        warning: str,
        searches: list[WccaBusinessSearchResult],
        confirmed_records: list[tuple[str, WccaSearchHit]],
        ambiguous_hits: list[dict[str, Any]],
    ) -> SourceResult:
        searched_names = [item.searched_name for item in searches]
        if confirmed_records:
            base = build_operator_result(
                contractor,
                searched_names=searched_names,
                outcome="partial",
                cases=self._cases_from_hits(confirmed_records),
                operator_note=warning,
                operator_confirmed_complete=False,
                identity_confirmed=True,
            )
        else:
            base = build_operator_result(
                contractor,
                searched_names=searched_names,
                outcome="partial",
                operator_note=warning,
                operator_confirmed_complete=False,
                identity_confirmed=False,
            )
        base.status = status
        base.completeness_status = CompletenessStatus.PARTIAL if searches else CompletenessStatus.UNKNOWN
        if not confirmed_records:
            base.identity_status = IdentityStatus.NOT_EVALUATED
        base.warnings.append(warning)
        return self._decorate_result(base, searches=searches, ambiguous_hits=ambiguous_hits)

    def search(self, contractor: ContractorContext) -> SourceResult:
        plan = build_search_plan(contractor)
        searches: list[WccaBusinessSearchResult] = []
        confirmed_records: list[tuple[str, WccaSearchHit]] = []
        ambiguous_candidates: list[dict[str, Any]] = []

        try:
            browser = self._ensure_browser()
            for search_name in plan["search_names"]:
                query = browser.search_business(search_name)
                searches.append(query)
                for hit in query.hits:
                    if _is_exact_party_match(search_name, hit.matched_party_name):
                        confirmed_records.append((search_name, hit))
                        continue
                    similarity = _name_similarity(search_name, hit.matched_party_name)
                    if similarity >= 0.90:
                        ambiguous_candidates.append(
                            {
                                "searched_name": search_name,
                                "similarity": round(similarity, 4),
                                **asdict(hit),
                            }
                        )
        except WccaBrowserUnavailable as exc:
            return self._problem_result(
                contractor,
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                warning=str(exc),
                searches=searches,
                confirmed_records=confirmed_records,
                ambiguous_hits=ambiguous_candidates,
            )
        except WccaHumanActionTimeout as exc:
            return self._problem_result(
                contractor,
                status=SourceResultStatus.BLOCKED,
                warning=str(exc),
                searches=searches,
                confirmed_records=confirmed_records,
                ambiguous_hits=ambiguous_candidates,
            )
        except WccaLayoutChanged as exc:
            return self._problem_result(
                contractor,
                status=SourceResultStatus.LAYOUT_CHANGED,
                warning=str(exc),
                searches=searches,
                confirmed_records=confirmed_records,
                ambiguous_hits=ambiguous_candidates,
            )
        except WccaBrowserError as exc:
            return self._problem_result(
                contractor,
                status=SourceResultStatus.TIMEOUT,
                warning=str(exc),
                searches=searches,
                confirmed_records=confirmed_records,
                ambiguous_hits=ambiguous_candidates,
            )

        searched_names = [item.searched_name for item in searches]
        complete = len(searched_names) == len(plan["search_names"])
        cases = self._cases_from_hits(confirmed_records)

        if confirmed_records:
            result = build_operator_result(
                contractor,
                searched_names=searched_names,
                outcome="findings",
                cases=cases,
                operator_note="WCCA public browser search completed automatically after any ordinary human challenge handling.",
                operator_confirmed_complete=complete,
                identity_confirmed=True,
            )
            if ambiguous_candidates:
                result.status = SourceResultStatus.AMBIGUOUS_MATCH
                result.identity_status = IdentityStatus.REVIEW_REQUIRED
                result.warnings.append(
                    "Confirmed WCCA cases were found, but additional similar-name WCCA result(s) require human identity review."
                )
            return self._decorate_result(result, searches=searches, ambiguous_hits=ambiguous_candidates)

        if ambiguous_candidates:
            candidate_cases = [
                {
                    "case_number": item["case_number"],
                    "county": item["county"],
                    "matched_party_name": item["matched_party_name"],
                    "case_type": _case_type_code(item["case_number"]),
                    "case_status": item["case_status"],
                    "filing_date": item["filing_date"],
                    "disposition": "",
                    "case_url": item["case_url"],
                    "note": f"Possible WCCA match for {item['searched_name']!r}; name similarity {item['similarity']:.2f}. Caption: {item['caption']}",
                }
                for item in ambiguous_candidates
            ]
            result = build_operator_result(
                contractor,
                searched_names=searched_names,
                outcome="ambiguous",
                cases=candidate_cases,
                operator_note="WCCA returned similar-name candidate(s) but no exact approved bidder/alias party match.",
                operator_confirmed_complete=complete,
                identity_confirmed=False,
            )
            return self._decorate_result(result, searches=searches, ambiguous_hits=ambiguous_candidates)

        result = build_operator_result(
            contractor,
            searched_names=searched_names,
            outcome="no_match" if complete else "partial",
            operator_note="Public WCCA browser search completed with no exact approved bidder/alias party match.",
            operator_confirmed_complete=complete,
            identity_confirmed=False,
        )
        return self._decorate_result(result, searches=searches, ambiguous_hits=[])

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown timing varies
        self.close()
