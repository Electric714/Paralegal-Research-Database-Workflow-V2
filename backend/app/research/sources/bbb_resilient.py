from __future__ import annotations

import hashlib
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    RawArtifact,
    SourceResultStatus,
)
from .base import ContractorContext
from .bbb import (
    BBB_BASE,
    BBB_FIELD,
    BbbBusinessProfileSource,
    BbbCandidate,
    BbbRequestError,
    _aliases,
)
from .bbb_reader import (
    BbbReaderClient,
    BbbReaderError,
    JINA_READER_BASE,
    canonical_profile_url,
    parse_complaint_summary as parse_reader_complaint_summary,
    parse_search_profiles as parse_reader_search_profiles,
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


def parse_search_profile_urls(html: str) -> tuple[list[str], int | None]:
    """Extract BBB business-profile links from a normal rendered HTML search page."""
    import re

    parser = _SearchDocument()
    parser.feed(html or "")
    urls: list[str] = []
    for href in parser.hrefs:
        profile = canonical_profile_url(href)
        if profile and profile not in urls:
            urls.append(profile)

    match = re.search(r"\bShowing:\s*([\d,]+)\s+results?\b", parser.visible_text, re.IGNORECASE)
    total = int(match.group(1).replace(",", "")) if match else None
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


def _page_artifact(kind: str, source_url: str, text: str, *, via: str, mime_type: str) -> RawArtifact:
    return RawArtifact(
        artifact_type=kind,
        sha256=hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest(),
        mime_type=mime_type,
        metadata={
            "url": source_url,
            "fetched_via": via,
            "content_retained": False,
        },
    )


class ResilientBbbBusinessProfileSource(BbbBusinessProfileSource):
    """BBB adapter with a public-page reader as the primary transport.

    BBB repeatedly blocks local direct HTTP and automated Edge sessions. Production
    now asks Jina Reader to render BBB's public search and complaint pages and keeps
    the canonical BBB URLs as evidence. Basic Reader use does not require a key;
    JINA_API_KEY is optional for higher limits. The previous headed Edge path stays
    available as a fallback rather than being our main acquisition strategy.
    """

    adapter_version = "1.4.0"
    parser_version = "1.2.0"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        state_ranges=None,
        boundary_margin: int = 1,
        browser_session_factory: Callable[..., PublicBrowserSession] = PublicBrowserSession,
        browser_first: bool | None = None,
        browser_profile_dir: str | None = None,
        reader_transport: httpx.BaseTransport | None = None,
        reader_first: bool | None = None,
    ) -> None:
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

        self._reader_first = (transport is None) if reader_first is None else bool(reader_first)
        self._reader = BbbReaderClient(transport=reader_transport)

    def health_check(self) -> dict:
        result = super().health_check()
        result.update(
            {
                "acquisition_mode": "bbb_public_pages_via_jina_reader_with_local_browser_fallback",
                "discovery_url": BBB_SEARCH_URL,
                "reader_service": JINA_READER_BASE,
                "reader_key": "optional JINA_API_KEY for higher rate limits",
                "browser_fallback": "headed_system_edge_or_chrome",
                "persistent_browser_profile": True,
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
                f"BBB blocked the local browser fallback at {url}: {browser_exc}",
                status=SourceResultStatus.BLOCKED,
                http_status=direct_exc.http_status if direct_exc else 403,
            ) from browser_exc
        except BrowserUnavailableError as browser_exc:
            raise BbbRequestError(
                f"BBB local browser fallback could not start Edge/Chrome: {browser_exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=direct_exc.http_status if direct_exc else None,
            ) from browser_exc
        except BrowserFetchError as browser_exc:
            raise BbbRequestError(
                f"BBB local browser fallback failed at {url}: {browser_exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=direct_exc.http_status if direct_exc else None,
            ) from browser_exc

    def _result(self, *args, **kwargs):
        result = super()._result(*args, **kwargs)
        result.acquisition_method = "bbb_public_pages_reader_then_browser"
        return result

    @staticmethod
    def _reader_failure(exc: BbbReaderError) -> BbbRequestError:
        return BbbRequestError(str(exc), status=exc.status, http_status=exc.http_status)

    def _complaint_result(
        self,
        contractor: ContractorContext,
        candidate: BbbCandidate,
        *,
        search_url: str,
        search_artifacts: list[RawArtifact],
        warnings: list[str],
    ):
        complaints_url = candidate.profile.profile_url.rstrip("/") + "/complaints"
        try:
            text = self._reader.fetch(complaints_url)
            via = "jina_reader"
            mime_type = "text/markdown"
            total, closed_12 = parse_reader_complaint_summary(text)
        except BbbReaderError as reader_exc:
            try:
                response = self._browser_document(complaints_url)
                text = response.text
                via = "local_browser"
                mime_type = "text/html"
                from .bbb import parse_complaint_summary

                total, closed_12 = parse_complaint_summary(text)
            except BbbRequestError as browser_exc:
                combined = BbbRequestError(
                    f"BBB complaint retrieval failed through Reader and local browser. "
                    f"Reader: {reader_exc}. Browser: {browser_exc}",
                    status=(
                        SourceResultStatus.BLOCKED
                        if browser_exc.status == SourceResultStatus.BLOCKED
                        else SourceResultStatus.SOURCE_UNAVAILABLE
                    ),
                    http_status=browser_exc.http_status or reader_exc.http_status,
                )
                return self._failure_result(
                    contractor,
                    combined,
                    url=complaints_url,
                    identity_status=IdentityStatus.CONFIRMED,
                    identity_confidence=candidate.score,
                    source_record_id=candidate.profile.source_record_id,
                    payload={"matched_profile": candidate.as_dict(), "discovery_url": search_url},
                )

        artifact = _page_artifact(
            "bbb_complaints_page_hash",
            complaints_url,
            text,
            via=via,
            mime_type=mime_type,
        )
        all_artifacts = search_artifacts + [artifact]

        if total is None:
            return self._result(
                contractor,
                status=SourceResultStatus.LAYOUT_CHANGED,
                identity_status=IdentityStatus.CONFIRMED,
                completeness_status=CompletenessStatus.UNKNOWN,
                identity_confidence=candidate.score,
                source_record_id=candidate.profile.source_record_id,
                source_url=complaints_url,
                warnings=warnings + ["BBB complaint summary could not be recognized on the verified profile."],
                artifacts=all_artifacts,
                payload={
                    "matched_profile": candidate.as_dict(),
                    "discovery_url": search_url,
                    "fetched_via": via,
                },
            )

        observed = "Y" if total > 0 else "N"
        evidence = EvidenceRecord(
            field_name=BBB_FIELD,
            observed_value=observed,
            source_record_id=candidate.profile.source_record_id,
            source_url=complaints_url,
            details={
                "basis": "BBB public complaint page for a verified business profile",
                "total_complaints_3y": total,
                "closed_complaints_12m": closed_12,
                "profile": candidate.profile.as_dict(),
                "discovery_url": search_url,
                "fetched_via": via,
            },
        )
        return self._result(
            contractor,
            status=(
                SourceResultStatus.SUCCESS_WITH_FINDINGS
                if total > 0
                else SourceResultStatus.SUCCESS_COMPLETE
            ),
            identity_status=IdentityStatus.CONFIRMED,
            completeness_status=CompletenessStatus.COMPLETE,
            identity_confidence=candidate.score,
            source_record_id=candidate.profile.source_record_id,
            source_url=complaints_url,
            warnings=warnings,
            evidence=[evidence],
            artifacts=all_artifacts,
            payload={
                "matched_profile": candidate.as_dict(),
                "complaints": {
                    "total_complaints_3y": total,
                    "closed_complaints_12m": closed_12,
                },
                "discovery_url": search_url,
                "fetched_via": via,
            },
        )

    def _search_with_reader(self, contractor: ContractorContext):
        aliases = _aliases(contractor)[:MAX_SEARCH_ALIASES]
        warnings: list[str] = []
        all_profiles = []
        seen: set[str] = set()
        artifacts: list[RawArtifact] = []
        last_search_url = BBB_SEARCH_URL

        for alias_index, alias in enumerate(aliases):
            alias_count = 0
            alias_total: int | None = None

            for page in range(1, MAX_SEARCH_PAGES + 1):
                search_url = build_bbb_search_url(contractor, alias, page=page)
                last_search_url = search_url
                try:
                    text = self._reader.fetch(search_url)
                except BbbReaderError as exc:
                    raise self._reader_failure(exc) from exc

                artifacts.append(
                    _page_artifact(
                        "bbb_search_page_hash",
                        search_url,
                        text,
                        via="jina_reader",
                        mime_type="text/markdown",
                    )
                )
                page_profiles, total = parse_reader_search_profiles(text)
                if alias_total is None:
                    alias_total = total
                alias_count += len(page_profiles)

                for profile in page_profiles:
                    if profile.profile_url not in seen:
                        seen.add(profile.profile_url)
                        all_profiles.append(profile)

                candidates = [self._score_profile(contractor, profile) for profile in all_profiles]
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.remembered_judgment != "DIFFERENT_ENTITY"
                ]
                candidates.sort(key=lambda item: item.score, reverse=True)

                strong = [
                    candidate
                    for candidate in candidates
                    if candidate.remembered_judgment == "SAME_ENTITY" or candidate.auto_confirmable
                ]
                if strong:
                    best = strong[0]
                    competing = [
                        candidate
                        for candidate in strong[1:]
                        if candidate.profile.source_record_id != best.profile.source_record_id
                        and candidate.score >= best.score - 0.03
                    ]
                    if competing and best.remembered_judgment != "SAME_ENTITY":
                        return self._result(
                            contractor,
                            status=SourceResultStatus.AMBIGUOUS_MATCH,
                            identity_status=IdentityStatus.REVIEW_REQUIRED,
                            completeness_status=CompletenessStatus.PARTIAL,
                            identity_confidence=best.score,
                            source_url=search_url,
                            warnings=warnings + ["Multiple strong BBB profiles require identity review."],
                            artifacts=artifacts,
                            payload={
                                "top_candidates": [candidate.as_dict() for candidate in candidates[:5]],
                                "discovery_url": search_url,
                                "fetched_via": "jina_reader",
                            },
                        )

                    return self._complaint_result(
                        contractor,
                        best,
                        search_url=search_url,
                        search_artifacts=artifacts,
                        warnings=warnings,
                    )

                complete_page_set = alias_total is not None and alias_count >= alias_total
                if complete_page_set or not page_profiles:
                    break
                if alias_index > 0:
                    break

            if alias_total == 0:
                warnings.append(
                    f"BBB search returned 0 directory results for {alias!r} near {_search_location(contractor)!r}."
                )

        candidates = [self._score_profile(contractor, profile) for profile in all_profiles]
        candidates = [
            candidate
            for candidate in candidates
            if candidate.remembered_judgment != "DIFFERENT_ENTITY"
        ]
        candidates.sort(key=lambda item: item.score, reverse=True)

        medium = [
            candidate
            for candidate in candidates
            if candidate.score >= 0.78 and candidate.name_score >= 0.75
        ]
        if medium:
            return self._result(
                contractor,
                status=SourceResultStatus.AMBIGUOUS_MATCH,
                identity_status=IdentityStatus.REVIEW_REQUIRED,
                completeness_status=CompletenessStatus.PARTIAL,
                identity_confidence=medium[0].score,
                source_url=last_search_url,
                warnings=warnings + ["BBB returned a plausible company profile that is not strong enough to auto-confirm."],
                artifacts=artifacts,
                payload={
                    "top_candidates": [candidate.as_dict() for candidate in candidates[:5]],
                    "discovery_url": last_search_url,
                    "fetched_via": "jina_reader",
                },
            )

        return self._result(
            contractor,
            status=SourceResultStatus.PARTIAL_RESULTS,
            identity_status=IdentityStatus.REJECTED if candidates else IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.PARTIAL,
            source_url=last_search_url,
            warnings=warnings + ["No verified BBB profile match was found; absence is not treated as a clean negative."],
            artifacts=artifacts,
            payload={
                "candidate_count": len(candidates),
                "top_candidates": [candidate.as_dict() for candidate in candidates[:5]],
                "discovery_url": last_search_url,
                "fetched_via": "jina_reader",
            },
        )

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        if self._browser_first and not url.lower().split("?", 1)[0].endswith(".xml"):
            return self._browser_document(url)

        try:
            return super()._get(client, url)
        except BbbRequestError as exc:
            if exc.status != SourceResultStatus.BLOCKED or exc.http_status == 429:
                raise
            return self._browser_document(url, direct_exc=exc)

    def _search_browser_compat(self, contractor: ContractorContext):
        """Previous headed-browser search flow retained as a fallback/test path."""
        state = (contractor.state or "").strip().upper()
        self.state_ranges.setdefault(state, ((1, 1),))
        search_url = BBB_SEARCH_URL

        with self._client() as client:
            profile_urls: list[str] = []
            warnings: list[str] = []
            search_complete = False

            for alias_index, alias in enumerate(_aliases(contractor)[:MAX_SEARCH_ALIASES]):
                alias_urls: list[str] = []
                alias_total: int | None = None

                for page in range(1, MAX_SEARCH_PAGES + 1):
                    search_url = build_bbb_search_url(contractor, alias, page=page)
                    response = self._get(client, search_url)
                    page_urls, total = parse_search_profile_urls(response.text)
                    if alias_total is None:
                        alias_total = total

                    for url in page_urls:
                        if url not in alias_urls:
                            alias_urls.append(url)
                        if url not in profile_urls:
                            profile_urls.append(url)

                    if self._candidate_urls(contractor, profile_urls):
                        search_complete = alias_total is not None and alias_total <= len(alias_urls)
                        self._state_profiles[state] = (profile_urls, search_complete, warnings)
                        return super().search(contractor)

                    if alias_total is not None and alias_total <= len(alias_urls):
                        break
                    if not page_urls or alias_index > 0:
                        break

            self._state_profiles[state] = (profile_urls, search_complete, warnings)
            return super().search(contractor)

    def search(self, contractor: ContractorContext):
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

        try:
            if self._reader_first:
                try:
                    return self._search_with_reader(contractor)
                except BbbRequestError as reader_exc:
                    try:
                        result = self._search_browser_compat(contractor)
                        result.warnings.insert(
                            0,
                            f"Jina Reader was unavailable; local browser fallback used: {reader_exc}",
                        )
                        return result
                    except BbbRequestError as browser_exc:
                        combined = BbbRequestError(
                            f"BBB failed through both Reader and local browser. "
                            f"Reader: {reader_exc}. Browser: {browser_exc}",
                            status=(
                                SourceResultStatus.BLOCKED
                                if browser_exc.status == SourceResultStatus.BLOCKED
                                else SourceResultStatus.SOURCE_UNAVAILABLE
                            ),
                            http_status=browser_exc.http_status or reader_exc.http_status,
                        )
                        return self._failure_result(contractor, combined, url=BBB_SEARCH_URL)

            return self._search_browser_compat(contractor)
        finally:
            self._close_browser()
