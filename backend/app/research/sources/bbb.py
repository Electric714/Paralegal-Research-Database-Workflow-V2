from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from rapidfuzz import fuzz

from ... import database as db
from ..matching import normalize_company_name, normalize_text, score_candidate
from ..models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource


BBB_BASE = "https://www.bbb.org"
BBB_SITEMAP_INDEX = f"{BBB_BASE}/sitemap-business-profiles-index.xml"
BBB_FIELD = "better_business_bureau_complaints"
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_BOUNDARY_MARGIN = 1
MAX_ALIASES = 6
MAX_PROFILE_CANDIDATES = 8

# BBB's profile sitemaps are geographically clustered rather than split by state.
# These ranges were live-mapped against the published sitemap index during the
# earlier proof of concept. One neighboring sitemap is included at each boundary
# because state transitions can occur inside a sitemap file. Unsupported states
# fail closed as unknown instead of inventing a negative result.
BBB_STATE_SITEMAP_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "FL": ((185, 185), (188, 191), (269, 290), (321, 324), (358, 367)),
    "IL": ((291, 310), (357, 357)),
    "MN": ((332, 344),),
    "MO": ((346, 346), (356, 356), (368, 376)),
    "OH": ((136, 156), (192, 195)),
    "WI": ((325, 331),),
}

_SITEMAP_CHILD = re.compile(r"^/sitemap-business-profiles-(?P<number>\d+)\.xml$", re.I)
_PROFILE_PATH = re.compile(r"^/us/(?P<state>[a-z]{2})/(?P<city>[^/]+)/profile/[^/]+/[^/?#]+/?$", re.I)
_ADDRESS_ID_SUFFIX = re.compile(r"/addressId/\d+/?$", re.I)
_COMPLAINT_SUFFIX = re.compile(r"/complaints/?$", re.I)
_ADDRESS_RE = re.compile(
    r"(?P<street>\d{1,8}\s+[^,\n]{2,120}?)\s*,?\s+"
    r"(?P<city>[A-Za-z .'-]{2,80}),\s*(?P<state>[A-Z]{2})\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)\b"
)
_CITY_STATE_ZIP_RE = re.compile(
    r"(?P<city>[A-Za-z .'-]{2,80}),\s*(?P<state>[A-Z]{2})\s+"
    r"(?P<zip>\d{5}(?:-\d{4})?)\b"
)
_COMPLAINT_TOTAL_PATTERNS = (
    re.compile(r"\b([\d,]+)\s+total\s+complaints?\s+in\s+the\s+last\s+3\s+years?\b", re.I),
    re.compile(r"\b([\d,]+)\s+complaints?\s+in\s+the\s+last\s+3\s+years?\b", re.I),
)
_COMPLAINT_12M_PATTERNS = (
    re.compile(r"\b([\d,]+)\s+complaints?\s+closed\s+in\s+the\s+last\s+12\s+months?\b", re.I),
    re.compile(r"\b([\d,]+)\s+closed\s+complaints?\s+in\s+the\s+last\s+12\s+months?\b", re.I),
)
_RATING_RE = re.compile(r"\bBBB\s+Rating\s*:?\s*([A-F](?:\+|-)?)\b", re.I)
_CHALLENGE_RE = re.compile(
    r"(?:captcha|verify\s+(?:that\s+)?you\s+are\s+human|access\s+denied|cloudflare|"
    r"unusual\s+traffic|security\s+check)",
    re.I,
)


class BbbRequestError(RuntimeError):
    def __init__(self, message: str, *, status: SourceResultStatus, http_status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class _HtmlDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.headings: list[str] = []
        self.json_scripts: list[str] = []
        self._hidden_depth = 0
        self._heading_depth = 0
        self._heading_parts: list[str] = []
        self._json_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        values = {key.casefold(): (value or "") for key, value in attrs}
        if lower in {"style", "noscript"}:
            self._hidden_depth += 1
        elif lower == "script":
            self._json_script = "ld+json" in values.get("type", "").casefold()
            self._script_parts = []
            if not self._json_script:
                self._hidden_depth += 1
        if lower in {"h1", "h2", "h3"}:
            self._heading_depth += 1
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower == "script":
            if self._json_script:
                raw = "".join(self._script_parts).strip()
                if raw:
                    self.json_scripts.append(raw)
            elif self._hidden_depth:
                self._hidden_depth -= 1
            self._json_script = False
        elif lower in {"style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1
        if lower in {"h1", "h2", "h3"} and self._heading_depth:
            heading = " ".join(" ".join(self._heading_parts).split())
            if heading:
                self.headings.append(heading)
            self._heading_depth -= 1
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_script:
            self._script_parts.append(data)
            return
        if self._hidden_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.text_parts.append(value)
        if self._heading_depth:
            self._heading_parts.append(value)

    @property
    def visible_text(self) -> str:
        return " ".join(self.text_parts)


@dataclass(frozen=True)
class BbbProfile:
    profile_url: str
    source_record_id: str
    name: str
    address: str
    city: str
    state: str
    zip_code: str
    telephone: str = ""
    rating: str = ""
    accredited: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_record_id": self.source_record_id,
            "profile_url": self.profile_url,
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "zip": self.zip_code,
            "telephone": self.telephone,
            "bbb_rating": self.rating,
            "bbb_accredited": self.accredited,
        }


@dataclass(frozen=True)
class BbbCandidate:
    profile: BbbProfile
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    zip_match: bool
    matched_search_name: str
    remembered_judgment: str | None

    @property
    def auto_confirmable(self) -> bool:
        location_ok = (
            self.address_score >= 0.90
            or self.zip_match
            or (self.city_score == 1.0 and self.state_score == 1.0)
        )
        return self.name_score >= 0.97 and location_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.profile.as_dict(),
            "score": round(self.score, 4),
            "name_score": round(self.name_score, 4),
            "address_score": round(self.address_score, 4),
            "city_score": round(self.city_score, 4),
            "state_score": round(self.state_score, 4),
            "zip_match": self.zip_match,
            "matched_search_name": self.matched_search_name,
            "auto_confirmable": self.auto_confirmable,
            "remembered_judgment": self.remembered_judgment,
        }


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _profile_base(url: str) -> str:
    parts = urlsplit(urljoin(BBB_BASE, url))
    path = _ADDRESS_ID_SUFFIX.sub("", parts.path)
    path = _COMPLAINT_SUFFIX.sub("", path)
    return urlunsplit(("https", "www.bbb.org", path.rstrip("/"), "", ""))


def _complaints_url(url: str) -> str:
    return _profile_base(url) + "/complaints"


def _record_id(url: str) -> str:
    return _profile_base(url).rstrip("/").split("/")[-1]


def _slug_name(url: str) -> str:
    slug = _record_id(url)
    slug = re.sub(r"-\d{4,}(?:-\d+)?$", "", slug)
    return " ".join(part for part in slug.split("-") if part)


def _xml_locs(text: str) -> list[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("BBB sitemap XML could not be parsed") from exc
    result: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() != "loc":
            continue
        value = (element.text or "").strip()
        if value:
            result.append(value)
    return result


def _aliases(contractor: ContractorContext) -> list[str]:
    names = [contractor.contractor_name]
    names.extend(
        part.strip()
        for part in re.split(r"[;|\n]+", contractor.related_companies or "")
        if part.strip()
    )
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        key = normalize_company_name(name) or normalize_text(name)
        if key and key not in seen:
            seen.add(key)
            result.append(name.strip())
    return result[:MAX_ALIASES]


def _parse_location(text: str) -> dict[str, str]:
    match = _ADDRESS_RE.search(text or "")
    if match:
        return {
            "address": match.group("street").strip(),
            "city": match.group("city").strip(),
            "state": match.group("state").upper(),
            "zip": match.group("zip").strip(),
        }
    match = _CITY_STATE_ZIP_RE.search(text or "")
    if match:
        return {
            "address": "",
            "city": match.group("city").strip(),
            "state": match.group("state").upper(),
            "zip": match.group("zip").strip(),
        }
    return {"address": "", "city": "", "state": "", "zip": ""}


def parse_profile_html(html: str, url: str) -> BbbProfile | None:
    parser = _HtmlDocument()
    parser.feed(html)
    text = parser.visible_text
    best: dict[str, str] = {}

    for raw in parser.json_scripts:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _walk_json(payload):
            raw_type = item.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if not any(
                normalize_text(str(value or "")) in {
                    "organization", "localbusiness", "professionalservice", "homeandconstructionbusiness"
                }
                for value in types
            ):
                continue
            address = item.get("address") if isinstance(item.get("address"), dict) else {}
            candidate = {
                "name": str(item.get("name") or "").strip(),
                "address": str(address.get("streetAddress") or "").strip(),
                "city": str(address.get("addressLocality") or "").strip(),
                "state": str(address.get("addressRegion") or "").strip().upper(),
                "zip": str(address.get("postalCode") or "").strip(),
                "telephone": str(item.get("telephone") or "").strip(),
            }
            if candidate["name"] and sum(bool(candidate.get(k)) for k in ("address", "city", "state", "zip")) >= sum(
                bool(best.get(k)) for k in ("address", "city", "state", "zip")
            ):
                best = candidate

    if not best.get("name"):
        ignored = {"business profile", "overview", "complaints", "reviews"}
        for heading in parser.headings:
            if normalize_text(heading) not in ignored and "bbb rating" not in heading.casefold():
                best["name"] = heading
                break
    if not best.get("name"):
        best["name"] = _slug_name(url)

    if not all(best.get(key) for key in ("city", "state", "zip")):
        location = _parse_location(text[:10000])
        for key, value in location.items():
            if value and not best.get(key):
                best[key] = value

    profile_url = _profile_base(url)
    path_match = _PROFILE_PATH.match(urlsplit(profile_url).path)
    if path_match:
        best.setdefault("state", path_match.group("state").upper())
        best.setdefault("city", path_match.group("city").replace("-", " "))

    if not best.get("name"):
        return None

    lower = text.casefold()
    accredited: bool | None = None
    if "not a bbb accredited business" in lower or "is not bbb accredited" in lower:
        accredited = False
    elif "bbb accredited business" in lower or "is bbb accredited" in lower:
        accredited = True
    rating = _RATING_RE.search(text)

    return BbbProfile(
        profile_url=profile_url,
        source_record_id=_record_id(profile_url),
        name=best.get("name", ""),
        address=best.get("address", ""),
        city=best.get("city", ""),
        state=best.get("state", ""),
        zip_code=best.get("zip", ""),
        telephone=best.get("telephone", ""),
        rating=rating.group(1).upper() if rating else "",
        accredited=accredited,
    )


def parse_complaint_summary(html: str) -> tuple[int | None, int | None]:
    parser = _HtmlDocument()
    parser.feed(html)
    text = parser.visible_text
    total = None
    closed_12 = None
    for pattern in _COMPLAINT_TOTAL_PATTERNS:
        match = pattern.search(text)
        if match:
            total = int(match.group(1).replace(",", ""))
            break
    for pattern in _COMPLAINT_12M_PATTERNS:
        match = pattern.search(text)
        if match:
            closed_12 = int(match.group(1).replace(",", ""))
            break
    return total, closed_12


def _remembered_judgment(bidder_id: int, source_record_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT judgment FROM identity_judgments
            WHERE bidder_id=? AND source_key='bbb' AND source_record_id=?
            """,
            (bidder_id, source_record_id),
        ).fetchone()
        return str(row["judgment"]) if row else None


def _cached_profile_url(bidder_id: int) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT source_url FROM evidence_snapshots
            WHERE bidder_id=? AND source_key='bbb' AND identity_status='CONFIRMED'
              AND source_url IS NOT NULL AND source_url <> ''
            ORDER BY id DESC LIMIT 1
            """,
            (bidder_id,),
        ).fetchone()
    return _profile_base(str(row["source_url"])) if row else None


class BbbBusinessProfileSource(ResearchSource):
    source_key = "bbb"
    display_name = "Better Business Bureau"
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        state_ranges: dict[str, tuple[tuple[int, int], ...]] | None = None,
        boundary_margin: int = DEFAULT_BOUNDARY_MARGIN,
    ) -> None:
        self.transport = transport
        self.state_ranges = state_ranges or BBB_STATE_SITEMAP_RANGES
        self.boundary_margin = max(0, boundary_margin)
        self._index_by_number: dict[int, str] | None = None
        self._state_profiles: dict[str, tuple[list[str], bool, list[str]]] = {}

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "published_profile_sitemaps_then_verified_profile",
            "sitemap_index": BBB_SITEMAP_INDEX,
            "mapped_states": sorted(self.state_ranges),
        }

    def prepare(self) -> None:
        self._index_by_number = None
        self._state_profiles = {}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            follow_redirects=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ParalegalResearchDesk/2.0; targeted public-record research)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

    def _get(self, client: httpx.Client, url: str) -> httpx.Response:
        try:
            response = client.get(url)
        except httpx.TimeoutException as exc:
            raise BbbRequestError("BBB request timed out.", status=SourceResultStatus.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise BbbRequestError("BBB request failed.", status=SourceResultStatus.SOURCE_UNAVAILABLE) from exc
        if response.status_code in {401, 403}:
            raise BbbRequestError("BBB blocked the request.", status=SourceResultStatus.BLOCKED, http_status=response.status_code)
        if response.status_code == 429:
            raise BbbRequestError("BBB rate limited the request.", status=SourceResultStatus.BLOCKED, http_status=429)
        if response.status_code >= 500:
            raise BbbRequestError("BBB is temporarily unavailable.", status=SourceResultStatus.SOURCE_UNAVAILABLE, http_status=response.status_code)
        if response.status_code >= 400:
            raise BbbRequestError(f"BBB returned HTTP {response.status_code}.", status=SourceResultStatus.HTTP_ERROR, http_status=response.status_code)
        if _CHALLENGE_RE.search(response.text):
            raise BbbRequestError("BBB returned an access challenge instead of the requested document.", status=SourceResultStatus.BLOCKED, http_status=response.status_code)
        return response

    def _load_index(self, client: httpx.Client) -> dict[int, str]:
        if self._index_by_number is not None:
            return self._index_by_number
        response = self._get(client, BBB_SITEMAP_INDEX)
        index: dict[int, str] = {}
        try:
            locations = _xml_locs(response.text)
        except ValueError as exc:
            raise BbbRequestError(str(exc), status=SourceResultStatus.LAYOUT_CHANGED, http_status=response.status_code) from exc
        for value in locations:
            parts = urlsplit(value)
            if (parts.hostname or "").casefold() != "www.bbb.org" or parts.query:
                continue
            match = _SITEMAP_CHILD.match(parts.path)
            if match:
                index[int(match.group("number"))] = value
        if not index:
            raise BbbRequestError("BBB sitemap index contained no recognized business-profile sitemaps.", status=SourceResultStatus.LAYOUT_CHANGED, http_status=response.status_code)
        self._index_by_number = index
        return index

    def _selected_numbers(self, state: str) -> set[int]:
        selected: set[int] = set()
        for start, end in self.state_ranges.get(state.upper(), ()):
            selected.update(range(max(1, start - self.boundary_margin), end + self.boundary_margin + 1))
        return selected

    def _load_state_profiles(self, client: httpx.Client, state: str) -> tuple[list[str], bool, list[str]]:
        state = state.upper()
        if state in self._state_profiles:
            return self._state_profiles[state]
        selected = self._selected_numbers(state)
        if not selected:
            result = ([], False, [f"BBB sitemap mapping is not configured for state {state or 'UNKNOWN'}. "])
            self._state_profiles[state] = result
            return result

        index = self._load_index(client)
        complete = True
        warnings: list[str] = []
        urls: list[str] = []
        for number in sorted(selected):
            child = index.get(number)
            if not child:
                complete = False
                warnings.append(f"BBB sitemap {number} was not present in the current published index.")
                continue
            try:
                response = self._get(client, child)
                locations = _xml_locs(response.text)
            except BbbRequestError as exc:
                complete = False
                warnings.append(str(exc))
                continue
            except ValueError:
                complete = False
                warnings.append(f"BBB sitemap {number} could not be parsed.")
                continue
            for value in locations:
                parts = urlsplit(value)
                match = _PROFILE_PATH.match(parts.path) if (parts.hostname or "").casefold() == "www.bbb.org" else None
                if match and not parts.query and match.group("state").upper() == state:
                    urls.append(_profile_base(value))
        result = (list(dict.fromkeys(urls)), complete, warnings)
        self._state_profiles[state] = result
        return result

    def _candidate_urls(self, contractor: ContractorContext, profile_urls: list[str]) -> list[str]:
        aliases = _aliases(contractor)
        ranked: list[tuple[float, str]] = []
        for url in profile_urls:
            slug_name = normalize_company_name(_slug_name(url))
            if not slug_name:
                continue
            best = max(
                (fuzz.WRatio(normalize_company_name(alias), slug_name) for alias in aliases if normalize_company_name(alias)),
                default=0.0,
            )
            if best >= 72:
                ranked.append((best, url))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in ranked[:MAX_PROFILE_CANDIDATES]]

    def _score_profile(self, contractor: ContractorContext, profile: BbbProfile) -> BbbCandidate:
        best = None
        for alias in _aliases(contractor):
            score = score_candidate(
                master_name=alias,
                candidate_name=profile.name,
                master_address=contractor.address_1,
                candidate_address=profile.address,
                master_city=contractor.city,
                candidate_city=profile.city,
                master_state=contractor.state,
                candidate_state=profile.state,
            )
            if best is None or score.score > best[0].score:
                best = (score, alias)
        assert best is not None
        score, alias = best
        master_zip = re.sub(r"\D", "", contractor.zip)[:5]
        profile_zip = re.sub(r"\D", "", profile.zip_code)[:5]
        return BbbCandidate(
            profile=profile,
            score=score.score,
            name_score=score.name_score,
            address_score=score.address_score,
            city_score=score.city_score,
            state_score=score.state_score,
            zip_match=bool(master_zip and profile_zip and master_zip == profile_zip),
            matched_search_name=alias,
            remembered_judgment=_remembered_judgment(contractor.internal_id, profile.source_record_id),
        )

    def _failure_result(self, contractor: ContractorContext, exc: BbbRequestError, *, url: str = BBB_SITEMAP_INDEX) -> SourceResult:
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=exc.status,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.UNKNOWN,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=[str(exc)],
                source_url=url,
                http_status=exc.http_status,
                acquisition_method="bbb_published_sitemaps_and_profile",
            )
        )

    def _confirmed_result(
        self,
        contractor: ContractorContext,
        candidate: BbbCandidate,
        client: httpx.Client,
        *,
        discovery_complete: bool,
        warnings: list[str],
        cached_profile_used: bool,
    ) -> SourceResult:
        complaints_url = _complaints_url(candidate.profile.profile_url)
        try:
            response = self._get(client, complaints_url)
        except BbbRequestError as exc:
            return self._failure_result(contractor, exc, url=complaints_url)
        total, closed_12 = parse_complaint_summary(response.text)
        if total is None:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.LAYOUT_CHANGED,
                    identity_status=IdentityStatus.CONFIRMED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    identity_confidence=candidate.score,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings + ["BBB complaint summary wording/layout was not recognized."],
                    normalized_payload={
                        "matched_profile": candidate.as_dict(),
                        "cached_profile_used": cached_profile_used,
                    },
                    source_record_id=candidate.profile.source_record_id,
                    source_url=complaints_url,
                    http_status=response.status_code,
                    acquisition_method="bbb_published_sitemaps_and_profile",
                )
            )

        observed = "Y" if total > 0 else "N"
        complete = total > 0 or discovery_complete
        completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        status = (
            SourceResultStatus.SUCCESS_WITH_FINDINGS
            if total > 0 and complete
            else SourceResultStatus.SUCCESS_COMPLETE
            if total == 0 and complete
            else SourceResultStatus.PARTIAL_RESULTS
        )
        evidence = EvidenceRecord(
            field_name=BBB_FIELD,
            observed_value=observed,
            source_record_id=candidate.profile.source_record_id,
            source_url=complaints_url,
            details={
                "basis": "BBB rolling three-year customer complaint summary for a verified business profile",
                "total_complaints_3y": total,
                "closed_complaints_12m": closed_12,
                "profile": candidate.profile.as_dict(),
            },
        )
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=status,
                identity_status=IdentityStatus.CONFIRMED,
                completeness_status=completeness,
                identity_confidence=candidate.score,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=[evidence],
                warnings=warnings,
                normalized_payload={
                    "matched_profile": candidate.as_dict(),
                    "complaints": {
                        "total_complaints_3y": total,
                        "closed_complaints_12m": closed_12,
                    },
                    "cached_profile_used": cached_profile_used,
                    "discovery_complete": discovery_complete,
                },
                source_record_id=candidate.profile.source_record_id,
                source_url=complaints_url,
                http_status=response.status_code,
                acquisition_method="bbb_published_sitemaps_and_profile",
            )
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        if not contractor.contractor_name.strip():
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.NOT_CHECKED,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.NOT_APPLICABLE,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=["The bidder record has no contractor name."],
                    source_url=BBB_SITEMAP_INDEX,
                    acquisition_method="bbb_published_sitemaps_and_profile",
                )
            )

        state = (contractor.state or "").strip().upper()
        if state not in self.state_ranges:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=[f"BBB sitemap discovery is not yet mapped for state {state or 'UNKNOWN'}; no negative result was inferred."],
                    source_url=BBB_SITEMAP_INDEX,
                    acquisition_method="bbb_published_sitemaps_and_profile",
                )
            )

        with self._client() as client:
            cached_url = _cached_profile_url(contractor.internal_id)
            if cached_url:
                try:
                    response = self._get(client, cached_url)
                    profile = parse_profile_html(response.text, cached_url)
                    if profile:
                        cached_candidate = self._score_profile(contractor, profile)
                        if cached_candidate.remembered_judgment != "DIFFERENT_ENTITY" and (
                            cached_candidate.remembered_judgment == "SAME_ENTITY" or cached_candidate.auto_confirmable
                        ):
                            return self._confirmed_result(
                                contractor,
                                cached_candidate,
                                client,
                                discovery_complete=True,
                                warnings=[],
                                cached_profile_used=True,
                            )
                except BbbRequestError as exc:
                    if exc.status == SourceResultStatus.BLOCKED:
                        return self._failure_result(contractor, exc, url=cached_url)
                    # Stale/missing cached profiles fall back to current sitemap discovery.

            try:
                profile_urls, discovery_complete, warnings = self._load_state_profiles(client, state)
            except BbbRequestError as exc:
                return self._failure_result(contractor, exc)

            candidate_urls = self._candidate_urls(contractor, profile_urls)
            if not candidate_urls:
                return self.validate_result(
                    SourceResult(
                        source_key=self.source_key,
                        contractor_id=contractor.internal_id,
                        status=SourceResultStatus.SUCCESS_NO_MATCH,
                        identity_status=IdentityStatus.NOT_EVALUATED,
                        completeness_status=CompletenessStatus.PARTIAL,
                        searched_name=contractor.contractor_name,
                        searched_address=contractor.address_1,
                        warnings=warnings + [
                            "No plausible BBB profile URL was found in the mapped sitemap blocks. This is not treated as a clean negative."
                        ],
                        normalized_payload={
                            "candidate_count": 0,
                            "discovery_complete": discovery_complete,
                            "mapped_state": state,
                        },
                        source_url=BBB_SITEMAP_INDEX,
                        acquisition_method="bbb_published_sitemaps_and_profile",
                    )
                )

            candidates: list[BbbCandidate] = []
            fetch_incomplete = False
            for url in candidate_urls:
                try:
                    response = self._get(client, url)
                except BbbRequestError as exc:
                    fetch_incomplete = True
                    warnings.append(f"Could not verify BBB candidate {url}: {exc}")
                    if exc.status == SourceResultStatus.BLOCKED:
                        return self._failure_result(contractor, exc, url=url)
                    continue
                profile = parse_profile_html(response.text, url)
                if profile is None:
                    fetch_incomplete = True
                    warnings.append(f"BBB profile identity could not be parsed for {url}.")
                    continue
                candidate = self._score_profile(contractor, profile)
                if candidate.remembered_judgment != "DIFFERENT_ENTITY" and candidate.score >= 0.72:
                    candidates.append(candidate)

            candidates.sort(key=lambda item: item.score, reverse=True)
            payload = {
                "candidate_count": len(candidates),
                "top_candidates": [candidate.as_dict() for candidate in candidates[:5]],
                "discovery_complete": discovery_complete and not fetch_incomplete,
                "mapped_state": state,
            }

            remembered_same = [item for item in candidates if item.remembered_judgment == "SAME_ENTITY"]
            strong = [item for item in candidates if item.auto_confirmable]
            confirmed = remembered_same or strong
            if confirmed:
                best = confirmed[0]
                competing = [item for item in confirmed[1:] if item.profile.source_record_id != best.profile.source_record_id and item.score >= best.score - 0.03]
                if competing and not remembered_same:
                    return self.validate_result(
                        SourceResult(
                            source_key=self.source_key,
                            contractor_id=contractor.internal_id,
                            status=SourceResultStatus.AMBIGUOUS_MATCH,
                            identity_status=IdentityStatus.REVIEW_REQUIRED,
                            completeness_status=CompletenessStatus.PARTIAL,
                            identity_confidence=best.score,
                            searched_name=contractor.contractor_name,
                            searched_address=contractor.address_1,
                            warnings=warnings + ["Multiple strong BBB business profiles require identity review."],
                            normalized_payload=payload,
                            source_url=BBB_SITEMAP_INDEX,
                            acquisition_method="bbb_published_sitemaps_and_profile",
                        )
                    )
                return self._confirmed_result(
                    contractor,
                    best,
                    client,
                    discovery_complete=discovery_complete and not fetch_incomplete,
                    warnings=warnings,
                    cached_profile_used=False,
                )

            medium = [item for item in candidates if item.score >= 0.78 and item.name_score >= 0.75]
            if medium:
                return self.validate_result(
                    SourceResult(
                        source_key=self.source_key,
                        contractor_id=contractor.internal_id,
                        status=SourceResultStatus.AMBIGUOUS_MATCH,
                        identity_status=IdentityStatus.REVIEW_REQUIRED,
                        completeness_status=CompletenessStatus.PARTIAL,
                        identity_confidence=medium[0].score,
                        searched_name=contractor.contractor_name,
                        searched_address=contractor.address_1,
                        warnings=warnings + ["BBB returned a plausible company profile that is not strong enough to auto-confirm."],
                        normalized_payload=payload,
                        source_url=BBB_SITEMAP_INDEX,
                        acquisition_method="bbb_published_sitemaps_and_profile",
                    )
                )

            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.SUCCESS_NO_MATCH,
                    identity_status=IdentityStatus.REJECTED if candidates else IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.PARTIAL,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings + ["No verified BBB profile match was found; absence is not treated as a clean complaint negative."],
                    normalized_payload=payload,
                    source_url=BBB_SITEMAP_INDEX,
                    acquisition_method="bbb_published_sitemaps_and_profile",
                )
            )
