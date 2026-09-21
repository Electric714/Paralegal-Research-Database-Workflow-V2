from __future__ import annotations

import hashlib
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
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    RawArtifact,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


BBB_BASE = "https://www.bbb.org"
BBB_SITEMAP_INDEX = f"{BBB_BASE}/sitemap-business-profiles-index.xml"
BBB_FIELD = "better_business_bureau_complaints"
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_BOUNDARY_MARGIN = 1
MAX_ALIASES = 6
MAX_PROFILE_CANDIDATES = 8
ALLOWED_BBB_HOSTS = {"bbb.org", "www.bbb.org"}

# BBB's published business-profile sitemaps are geographically clustered rather
# than split cleanly by state. These ranges were mapped during live testing.
# A neighboring sitemap is included at each range boundary. Unsupported states
# fail closed as unknown/manual review instead of producing a negative result.
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
_PROFILE_RECORD_SUFFIX = re.compile(r"-(?P<record>\d{4,}-\d+)$")
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
_ZERO_COMPLAINTS_RE = re.compile(r"\bthis\s+business\s+has\s+0\s+complaints?\b", re.I)
_RATING_RE = re.compile(r"\bBBB\s+Rating\s*:?\s*([A-F](?:[+-])?)", re.I)
_CHALLENGE_RE = re.compile(
    r"(?:captcha|verify\s+(?:that\s+)?you\s+are\s+human|access\s+denied|cloudflare|"
    r"unusual\s+traffic|security\s+check)",
    re.I,
)
_SUMMARY_START_MARKERS = ("customer complaints summary", "complaints summary")
_SUMMARY_END_MARKERS = (
    "if you've experienced an issue",
    "if you’ve experienced an issue",
    "filter and sort by",
    "initial complaint",
)
_GENERIC_HEADINGS = {
    "business profile",
    "overview",
    "reviews",
    "complaints",
    "customer complaints summary",
    "bbb rating",
    "bbb accreditation rating",
    "about this business",
    "business details",
    "find a location",
}


class BbbRequestError(RuntimeError):
    def __init__(self, message: str, *, status: SourceResultStatus, http_status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class _HtmlDocument(HTMLParser):
    """Non-executing reader for visible text, headings, and JSON-LD."""

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
    legacy_source_record_id: str
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


def _host_allowed(url: str) -> bool:
    return (urlsplit(url).hostname or "").casefold() in ALLOWED_BBB_HOSTS


def _profile_base(url: str) -> str:
    parts = urlsplit(urljoin(BBB_BASE, url))
    path = _ADDRESS_ID_SUFFIX.sub("", parts.path)
    path = _COMPLAINT_SUFFIX.sub("", path)
    return urlunsplit(("https", "www.bbb.org", path.rstrip("/"), "", ""))


def _complaints_url(url: str) -> str:
    return _profile_base(url) + "/complaints"


def _profile_segment(url: str) -> str:
    return _profile_base(url).rstrip("/").split("/")[-1]


def _record_id(url: str) -> str:
    """Return BBB's stable numeric file identifier when the URL exposes it."""
    segment = _profile_segment(url)
    match = _PROFILE_RECORD_SUFFIX.search(segment)
    return match.group("record") if match else segment


def _legacy_record_id(url: str) -> str:
    return _profile_segment(url)


def _slug_name(url: str) -> str:
    """Discovery hint only; the URL slug is never accepted as identity evidence."""
    segment = _profile_segment(url)
    match = _PROFILE_RECORD_SUFFIX.search(segment)
    slug = segment[: match.start()] if match else segment
    return " ".join(part for part in slug.split("-") if part)


def _profile_city_hint(url: str) -> str:
    match = _PROFILE_PATH.match(urlsplit(_profile_base(url)).path)
    return match.group("city").replace("-", " ") if match else ""


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
    result: list[str] = []
    seen: set[str] = set()
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


def _select_live_heading(headings: list[str], url: str) -> str:
    slug = normalize_company_name(_slug_name(url))
    if not slug:
        return ""
    ranked: list[tuple[float, str]] = []
    for heading in headings:
        normalized = normalize_text(heading)
        if not normalized or normalized in _GENERIC_HEADINGS:
            continue
        if normalized.startswith("find bbb accredited") or normalized.startswith("additional "):
            continue
        candidate = normalize_company_name(heading)
        if not candidate:
            continue
        ranked.append((fuzz.WRatio(slug, candidate), heading.strip()))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked[0][0] >= 60 else ""


def parse_profile_html(html: str, url: str) -> BbbProfile | None:
    """Parse identity only from content returned by the live BBB profile page."""
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
            normalized_types = {normalize_text(str(value or "")) for value in types}
            if not any(value == "organization" or value.endswith("business") or value == "professionalservice" for value in normalized_types):
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
            candidate_location_count = sum(bool(candidate.get(key)) for key in ("address", "city", "state", "zip"))
            best_location_count = sum(bool(best.get(key)) for key in ("address", "city", "state", "zip"))
            if candidate["name"] and candidate_location_count >= best_location_count:
                best = candidate

    if not best.get("name"):
        best["name"] = _select_live_heading(parser.headings, url)
    if not best.get("name"):
        return None

    location = _parse_location(text[:12000])
    for key, value in location.items():
        if value and not best.get(key):
            best[key] = value
    if not any(best.get(key) for key in ("address", "city", "state", "zip")):
        return None

    profile_url = _profile_base(url)
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
        legacy_source_record_id=_legacy_record_id(profile_url),
        name=best.get("name", ""),
        address=best.get("address", ""),
        city=best.get("city", ""),
        state=best.get("state", ""),
        zip_code=best.get("zip", ""),
        telephone=best.get("telephone", ""),
        rating=rating.group(1).upper() if rating else "",
        accredited=accredited,
    )


def _complaint_summary_region(text: str) -> str:
    lower = text.casefold()
    starts = [lower.find(marker) for marker in _SUMMARY_START_MARKERS]
    starts = [index for index in starts if index >= 0]
    if starts:
        start = min(starts)
        end = min(
            [index for marker in _SUMMARY_END_MARKERS if (index := lower.find(marker, start + 1)) >= 0]
            or [min(len(text), start + 1500)]
        )
        return text[start:end]

    # BBB currently uses a compact zero-state on some complaint pages instead of
    # rendering the Customer Complaints Summary heading. Accept only the exact
    # zero-state phrase and only before any complaint-submission/narrative marker.
    end = min(
        [index for marker in _SUMMARY_END_MARKERS if (index := lower.find(marker)) >= 0]
        or [min(len(text), 1500)]
    )
    prefix = text[:end]
    zero = _ZERO_COMPLAINTS_RE.search(prefix)
    return zero.group(0) if zero else ""


def parse_complaint_summary(html: str) -> tuple[int | None, int | None]:
    """Parse only BBB's official summary/zero-state, never complaint prose."""
    parser = _HtmlDocument()
    parser.feed(html)
    region = _complaint_summary_region(parser.visible_text)
    if not region:
        return None, None
    if _ZERO_COMPLAINTS_RE.search(region):
        return 0, None

    total = None
    closed_12 = None
    for pattern in _COMPLAINT_TOTAL_PATTERNS:
        match = pattern.search(region)
        if match:
            total = int(match.group(1).replace(",", ""))
            break
    for pattern in _COMPLAINT_12M_PATTERNS:
        match = pattern.search(region)
        if match:
            closed_12 = int(match.group(1).replace(",", ""))
            break
    return total, closed_12


def _remembered_judgment(bidder_id: int, profile: BbbProfile) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT judgment
            FROM identity_judgments
            WHERE bidder_id=? AND source_key='bbb'
              AND source_record_id IN (?, ?)
            ORDER BY CASE WHEN source_record_id=? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (
                bidder_id,
                profile.source_record_id,
                profile.legacy_source_record_id,
                profile.source_record_id,
            ),
        ).fetchone()
    return str(row["judgment"]) if row else None


def _cached_profile_url(bidder_id: int) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT source_url
            FROM evidence_snapshots
            WHERE bidder_id=? AND source_key='bbb' AND identity_status='CONFIRMED'
              AND source_url IS NOT NULL AND source_url <> ''
            ORDER BY id DESC LIMIT 1
            """,
            (bidder_id,),
        ).fetchone()
    if not row:
        return None
    source_url = str(row["source_url"])
    if not _host_allowed(source_url):
        return None
    return _profile_base(source_url)


def _hash_artifact(kind: str, url: str, text: str) -> RawArtifact:
    return RawArtifact(
        artifact_type=kind,
        sha256=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        mime_type="text/html",
        metadata={"url": url, "content_retained": False},
    )


class BbbBusinessProfileSource(ResearchSource):
    source_key = "bbb"
    display_name = "Better Business Bureau"
    adapter_version = "1.1.1"
    parser_version = "1.1.1"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        state_ranges: dict[str, tuple[tuple[int, int], ...]] | None = None,
        boundary_margin: int = DEFAULT_BOUNDARY_MARGIN,
    ) -> None:
        self.transport = transport
        self.state_ranges = BBB_STATE_SITEMAP_RANGES if state_ranges is None else state_ranges
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
        if not _host_allowed(url):
            raise BbbRequestError("Refusing to request a non-BBB URL.", status=SourceResultStatus.HTTP_ERROR)
        try:
            response = client.get(url)
        except httpx.TimeoutException as exc:
            raise BbbRequestError("BBB request timed out.", status=SourceResultStatus.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise BbbRequestError("BBB request failed.", status=SourceResultStatus.SOURCE_UNAVAILABLE) from exc

        if not _host_allowed(str(response.url)):
            raise BbbRequestError(
                "BBB request redirected outside the allowed BBB host boundary.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )
        if response.status_code in {401, 403, 429}:
            raise BbbRequestError("BBB blocked or rate-limited the request.", status=SourceResultStatus.BLOCKED, http_status=response.status_code)
        if response.status_code >= 500:
            raise BbbRequestError("BBB is temporarily unavailable.", status=SourceResultStatus.SOURCE_UNAVAILABLE, http_status=response.status_code)
        if response.status_code >= 400:
            raise BbbRequestError(f"BBB returned HTTP {response.status_code}.", status=SourceResultStatus.HTTP_ERROR, http_status=response.status_code)
        if _CHALLENGE_RE.search(response.text):
            raise BbbRequestError(
                "BBB returned an access challenge instead of the requested document.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        return response

    def _load_index(self, client: httpx.Client) -> dict[int, str]:
        if self._index_by_number is not None:
            return self._index_by_number
        response = self._get(client, BBB_SITEMAP_INDEX)
        try:
            locations = _xml_locs(response.text)
        except ValueError as exc:
            raise BbbRequestError(str(exc), status=SourceResultStatus.LAYOUT_CHANGED, http_status=response.status_code) from exc

        index: dict[int, str] = {}
        for value in locations:
            parts = urlsplit(value)
            if not _host_allowed(value) or parts.query:
                continue
            match = _SITEMAP_CHILD.match(parts.path)
            if match:
                index[int(match.group("number"))] = value
        if not index:
            raise BbbRequestError(
                "BBB sitemap index contained no recognized business-profile sitemaps.",
                status=SourceResultStatus.LAYOUT_CHANGED,
                http_status=response.status_code,
            )
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
            result = ([], False, [f"BBB sitemap mapping is not configured for state {state or 'UNKNOWN'}."])
            self._state_profiles[state] = result
            return result

        index = self._load_index(client)
        mapped_complete = True
        warnings: list[str] = []
        urls: list[str] = []
        for number in sorted(selected):
            child = index.get(number)
            if not child:
                mapped_complete = False
                warnings.append(f"BBB sitemap {number} was not present in the current published index.")
                continue
            try:
                response = self._get(client, child)
                locations = _xml_locs(response.text)
            except BbbRequestError as exc:
                mapped_complete = False
                warnings.append(str(exc))
                continue
            except ValueError:
                mapped_complete = False
                warnings.append(f"BBB sitemap {number} could not be parsed.")
                continue

            for value in locations:
                parts = urlsplit(value)
                match = _PROFILE_PATH.match(parts.path) if _host_allowed(value) else None
                if match and not parts.query and match.group("state").upper() == state:
                    urls.append(_profile_base(value))

        result = (list(dict.fromkeys(urls)), mapped_complete, warnings)
        self._state_profiles[state] = result
        return result

    def _candidate_urls(self, contractor: ContractorContext, profile_urls: list[str]) -> list[str]:
        aliases = _aliases(contractor)
        master_city = normalize_text(contractor.city)
        ranked: list[tuple[float, float, str]] = []
        for url in profile_urls:
            slug_name = normalize_company_name(_slug_name(url))
            if not slug_name:
                continue
            name_score = max(
                (fuzz.WRatio(normalize_company_name(alias), slug_name) for alias in aliases if normalize_company_name(alias)),
                default=0.0,
            )
            if name_score < 72:
                continue
            city_hint = normalize_text(_profile_city_hint(url))
            city_bonus = 15.0 if master_city and city_hint == master_city else 0.0
            ranked.append((name_score + city_bonus, name_score, url))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [url for _, _, url in ranked[:MAX_PROFILE_CANDIDATES]]

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
            remembered_judgment=_remembered_judgment(contractor.internal_id, profile),
        )

    def _result(
        self,
        contractor: ContractorContext,
        *,
        status: SourceResultStatus,
        identity_status: IdentityStatus,
        completeness_status: CompletenessStatus,
        source_url: str,
        warnings: list[str],
        identity_confidence: float | None = None,
        source_record_id: str | None = None,
        http_status: int | None = None,
        evidence: list[EvidenceRecord] | None = None,
        artifacts: list[RawArtifact] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SourceResult:
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=status,
                identity_status=identity_status,
                completeness_status=completeness_status,
                identity_confidence=identity_confidence,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=evidence or [],
                artifacts=artifacts or [],
                warnings=warnings,
                normalized_payload=payload or {},
                source_record_id=source_record_id,
                source_url=source_url,
                http_status=http_status,
                acquisition_method="bbb_published_sitemaps_and_profile",
            )
        )

    def _failure_result(
        self,
        contractor: ContractorContext,
        exc: BbbRequestError,
        *,
        url: str,
        identity_status: IdentityStatus = IdentityStatus.NOT_EVALUATED,
        identity_confidence: float | None = None,
        source_record_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SourceResult:
        return self._result(
            contractor,
            status=exc.status,
            identity_status=identity_status,
            completeness_status=CompletenessStatus.UNKNOWN,
            identity_confidence=identity_confidence,
            source_record_id=source_record_id,
            source_url=url,
            http_status=exc.http_status,
            warnings=[str(exc)],
            payload=payload,
        )

    def _confirmed_result(
        self,
        contractor: ContractorContext,
        candidate: BbbCandidate,
        client: httpx.Client,
        *,
        mapped_discovery_complete: bool,
        warnings: list[str],
        cached_profile_used: bool,
        profile_artifact: RawArtifact,
    ) -> SourceResult:
        complaints_url = _complaints_url(candidate.profile.profile_url)
        try:
            response = self._get(client, complaints_url)
        except BbbRequestError as exc:
            return self._failure_result(
                contractor,
                exc,
                url=complaints_url,
                identity_status=IdentityStatus.CONFIRMED,
                identity_confidence=candidate.score,
                source_record_id=candidate.profile.source_record_id,
                payload={"matched_profile": candidate.as_dict(), "cached_profile_used": cached_profile_used},
            )

        complaint_artifact = _hash_artifact("bbb_complaints_html_hash", complaints_url, response.text)
        total, closed_12 = parse_complaint_summary(response.text)
        if total is None:
            return self._result(
                contractor,
                status=SourceResultStatus.LAYOUT_CHANGED,
                identity_status=IdentityStatus.CONFIRMED,
                completeness_status=CompletenessStatus.UNKNOWN,
                identity_confidence=candidate.score,
                source_record_id=candidate.profile.source_record_id,
                source_url=complaints_url,
                http_status=response.status_code,
                warnings=warnings + ["BBB complaint-summary section or wording was not recognized."],
                artifacts=[profile_artifact, complaint_artifact],
                payload={"matched_profile": candidate.as_dict(), "cached_profile_used": cached_profile_used},
            )

        complete = total > 0 or mapped_discovery_complete
        completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        status = (
            SourceResultStatus.SUCCESS_WITH_FINDINGS
            if total > 0
            else SourceResultStatus.SUCCESS_COMPLETE
            if complete
            else SourceResultStatus.PARTIAL_RESULTS
        )
        observed = "Y" if total > 0 else "N"
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
                "mapped_discovery_complete": mapped_discovery_complete,
            },
        )
        return self._result(
            contractor,
            status=status,
            identity_status=IdentityStatus.CONFIRMED,
            completeness_status=completeness,
            identity_confidence=candidate.score,
            source_record_id=candidate.profile.source_record_id,
            source_url=complaints_url,
            http_status=response.status_code,
            warnings=warnings,
            evidence=[evidence],
            artifacts=[profile_artifact, complaint_artifact],
            payload={
                "matched_profile": candidate.as_dict(),
                "complaints": {"total_complaints_3y": total, "closed_complaints_12m": closed_12},
                "cached_profile_used": cached_profile_used,
                "mapped_discovery_complete": mapped_discovery_complete,
            },
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        if not contractor.contractor_name.strip():
            return self._result(
                contractor,
                status=SourceResultStatus.NOT_CHECKED,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.NOT_APPLICABLE,
                source_url=BBB_SITEMAP_INDEX,
                warnings=["The bidder record has no contractor name."],
            )

        state = (contractor.state or "").strip().upper()
        if state not in self.state_ranges:
            return self._result(
                contractor,
                status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.UNKNOWN,
                source_url=BBB_SITEMAP_INDEX,
                warnings=[f"BBB sitemap discovery is not configured for state {state or 'UNKNOWN'}; no negative result was inferred."],
            )

        with self._client() as client:
            cache_warning: list[str] = []
            cached_url = _cached_profile_url(contractor.internal_id)
            if cached_url:
                try:
                    response = self._get(client, cached_url)
                    profile = parse_profile_html(response.text, cached_url)
                    if profile:
                        candidate = self._score_profile(contractor, profile)
                        if candidate.remembered_judgment != "DIFFERENT_ENTITY" and (
                            candidate.remembered_judgment == "SAME_ENTITY" or candidate.auto_confirmable
                        ):
                            return self._confirmed_result(
                                contractor,
                                candidate,
                                client,
                                mapped_discovery_complete=False,
                                warnings=["Previously confirmed BBB profile was revalidated directly; mapped sitemap discovery was skipped for this run."],
                                cached_profile_used=True,
                                profile_artifact=_hash_artifact("bbb_profile_html_hash", cached_url, response.text),
                            )
                        cache_warning.append("Previously confirmed BBB profile no longer auto-confirms against the current bidder identity; current discovery was used instead.")
                    else:
                        cache_warning.append("Previously confirmed BBB profile no longer exposed parseable live identity; current discovery was used instead.")
                except BbbRequestError as exc:
                    if exc.status == SourceResultStatus.BLOCKED:
                        return self._failure_result(contractor, exc, url=cached_url)
                    cache_warning.append(f"Previously confirmed BBB profile could not be reused: {exc}")

            try:
                profile_urls, mapped_complete, warnings = self._load_state_profiles(client, state)
            except BbbRequestError as exc:
                return self._failure_result(contractor, exc, url=BBB_SITEMAP_INDEX)
            warnings = cache_warning + warnings

            candidate_urls = self._candidate_urls(contractor, profile_urls)
            if not candidate_urls:
                return self._result(
                    contractor,
                    status=SourceResultStatus.PARTIAL_RESULTS,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.PARTIAL,
                    source_url=BBB_SITEMAP_INDEX,
                    warnings=warnings + ["No plausible BBB profile URL was found in the configured sitemap blocks; this is not a clean no-match result."],
                    payload={
                        "candidate_count": 0,
                        "mapped_discovery_complete": mapped_complete,
                        "mapped_state": state,
                    },
                )

            candidates: list[tuple[BbbCandidate, RawArtifact]] = []
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
                    warnings.append(f"BBB profile identity could not be parsed from the live page for {url}.")
                    continue
                candidate = self._score_profile(contractor, profile)
                if candidate.remembered_judgment != "DIFFERENT_ENTITY" and candidate.score >= 0.72:
                    candidates.append((candidate, _hash_artifact("bbb_profile_html_hash", url, response.text)))

            candidates.sort(key=lambda item: item[0].score, reverse=True)
            payload = {
                "candidate_count": len(candidates),
                "top_candidates": [candidate.as_dict() for candidate, _ in candidates[:5]],
                "mapped_discovery_complete": mapped_complete and not fetch_incomplete,
                "mapped_state": state,
            }

            remembered_same = [item for item in candidates if item[0].remembered_judgment == "SAME_ENTITY"]
            strong = [item for item in candidates if item[0].auto_confirmable]
            confirmed = remembered_same or strong
            if confirmed:
                best, best_artifact = confirmed[0]
                competing = [
                    item
                    for item in confirmed[1:]
                    if item[0].profile.source_record_id != best.profile.source_record_id
                    and item[0].score >= best.score - 0.03
                ]
                if competing and not remembered_same:
                    return self._result(
                        contractor,
                        status=SourceResultStatus.AMBIGUOUS_MATCH,
                        identity_status=IdentityStatus.REVIEW_REQUIRED,
                        completeness_status=CompletenessStatus.PARTIAL,
                        identity_confidence=best.score,
                        source_url=BBB_SITEMAP_INDEX,
                        warnings=warnings + ["Multiple strong BBB business profiles require identity review."],
                        payload=payload,
                    )
                return self._confirmed_result(
                    contractor,
                    best,
                    client,
                    mapped_discovery_complete=mapped_complete and not fetch_incomplete,
                    warnings=warnings,
                    cached_profile_used=False,
                    profile_artifact=best_artifact,
                )

            medium = [item for item in candidates if item[0].score >= 0.78 and item[0].name_score >= 0.75]
            if medium:
                return self._result(
                    contractor,
                    status=SourceResultStatus.AMBIGUOUS_MATCH,
                    identity_status=IdentityStatus.REVIEW_REQUIRED,
                    completeness_status=CompletenessStatus.PARTIAL,
                    identity_confidence=medium[0][0].score,
                    source_url=BBB_SITEMAP_INDEX,
                    warnings=warnings + ["BBB returned a plausible company profile that is not strong enough to auto-confirm."],
                    payload=payload,
                )

            return self._result(
                contractor,
                status=SourceResultStatus.PARTIAL_RESULTS,
                identity_status=IdentityStatus.REJECTED if candidates else IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.PARTIAL,
                source_url=BBB_SITEMAP_INDEX,
                warnings=warnings + ["No verified BBB profile match was found; absence is not treated as a clean negative."],
                payload=payload,
            )
