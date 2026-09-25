from __future__ import annotations

import os
import re
import time
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..models import SourceResultStatus
from .bbb import (
    BBB_BASE,
    BbbProfile,
    _legacy_record_id,
    _parse_location,
    _profile_city_hint,
    _record_id,
)


JINA_READER_BASE = "https://r.jina.ai/"
BBB_HOSTS = {"bbb.org", "www.bbb.org"}
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_SEARCH_PROFILE_PATH = re.compile(
    r"^/us/(?P<state>[a-z]{2})/(?P<city>[^/]+)/profile/[^/]+/[^/?#]+(?:/addressId/\d+)?/?$",
    re.IGNORECASE,
)
_PROFILE_LINK_RE = re.compile(
    r"\[(?P<label>[^\]\n]+)\]\((?P<url>https://(?:www\.)?bbb\.org/us/[a-z]{2}/"
    r"[^/\s)]+/profile/[^/\s)]+/[^)\s#?]+(?:/addressId/\d+)?)\)",
    re.IGNORECASE,
)
_TOTAL_RE = re.compile(
    r"\bShowing:\s*(?:\*{1,2})?\s*([\d,]+)\s*(?:\*{1,2})?\s+results?\b",
    re.IGNORECASE,
)
_ZERO_RE = re.compile(r"\bthis\s+business\s+has\s+0\s+complaints?\b", re.IGNORECASE)
_TOTAL_3Y_RE = re.compile(
    r"\b([\d,]+)\s+(?:total\s+)?complaints?\s+in\s+the\s+last\s+3\s+years?\b",
    re.IGNORECASE,
)
_CLOSED_12M_RE = re.compile(
    r"\b([\d,]+)\s+complaints?\s+closed\s+in\s+the\s+last\s+12\s+months?\b",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]*)?\(?\d{3}\)?[\s.-]+\d{3}[\s.-]+\d{4}(?!\d)")


class BbbReaderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: SourceResultStatus = SourceResultStatus.SOURCE_UNAVAILABLE,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def canonical_profile_url(url: str) -> str | None:
    absolute = urljoin(BBB_BASE, url)
    parts = urlsplit(absolute)
    host = (parts.hostname or "").casefold().rstrip(".")
    if parts.scheme != "https" or host not in BBB_HOSTS or parts.query:
        return None
    if not _SEARCH_PROFILE_PATH.match(parts.path):
        return None
    path = re.sub(r"/addressId/\d+/?$", "", parts.path, flags=re.IGNORECASE).rstrip("/")
    return urlunsplit(("https", "www.bbb.org", path, "", ""))


def plain_markdown(text: str) -> str:
    value = unescape(text or "")
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    value = value.replace("_", "")
    value = re.sub(r"[#>*]+", " ", value)
    return " ".join(value.split())


def _clean_label(label: str) -> str:
    value = unescape(label or "")
    value = re.sub(r"(?i)^\s*advertisement:\s*", "", value)
    value = value.replace("_", "").replace("*", "").replace("`", "")
    return " ".join(value.split()).strip()


def parse_search_profiles(text: str) -> tuple[list[BbbProfile], int | None]:
    """Parse BBB search-result cards from Jina Reader markdown."""
    matches = list(_PROFILE_LINK_RE.finditer(text or ""))
    profiles: list[BbbProfile] = []
    seen: set[str] = set()

    for index, match in enumerate(matches):
        canonical = canonical_profile_url(match.group("url"))
        if not canonical or canonical in seen:
            continue
        name = _clean_label(match.group("label"))
        if not name:
            continue

        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text or "")
        block_end = min(next_start, match.end() + 1200)
        block = plain_markdown((text or "")[match.end():block_end])
        block = " ".join(_PHONE_RE.sub(" ", block).split())
        location = _parse_location(block)

        path_match = _SEARCH_PROFILE_PATH.match(urlsplit(canonical).path)
        url_state = path_match.group("state").upper() if path_match else ""
        city = location.get("city") or _profile_city_hint(canonical)
        state = (location.get("state") or url_state).upper()

        profiles.append(
            BbbProfile(
                profile_url=canonical,
                source_record_id=_record_id(canonical),
                legacy_source_record_id=_legacy_record_id(canonical),
                name=name,
                address=location.get("address", ""),
                city=city,
                state=state,
                zip_code=location.get("zip", ""),
            )
        )
        seen.add(canonical)

    total_match = _TOTAL_RE.search(text or "")
    total = int(total_match.group(1).replace(",", "")) if total_match else None
    return profiles, total


def parse_complaint_summary(text: str) -> tuple[int | None, int | None]:
    """Read BBB's published complaint summary from Reader markdown."""
    # Reader includes a long navigation menu and a title containing "Complaints".
    # Anchor to the actual section before applying the bounded summary window.
    heading = re.search(r"^#{1,6}\s+(?:Customer\s+)?Complaints\s*$", text or '', re.IGNORECASE | re.MULTILINE)
    if heading:
        text = text[heading.start():]
    plain = plain_markdown(text)
    marker = plain.casefold().find("complaints")
    region = plain[marker: marker + 3000] if marker >= 0 else plain[:3000]

    if _ZERO_RE.search(region):
        return 0, None

    total = None
    closed_12 = None
    total_match = _TOTAL_3Y_RE.search(region)
    if total_match:
        total = int(total_match.group(1).replace(",", ""))

    closed_match = _CLOSED_12M_RE.search(region)
    if closed_match:
        closed_12 = int(closed_match.group(1).replace(",", ""))

    return total, closed_12


class BbbReaderClient:
    """Fetch public BBB pages through Jina Reader instead of the workstation IP."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
        api_key: str | None = None,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.api_key = (api_key if api_key is not None else os.environ.get("JINA_API_KEY", "")).strip()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "text/plain",
            "User-Agent": "ParalegalResearchDesk/2.0",
            "X-Engine": "browser",
            "X-Timeout": "30",
            "X-No-Cache": "true",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _validate_target(target_url: str) -> None:
        parts = urlsplit(target_url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if parts.scheme != "https" or host not in BBB_HOSTS:
            raise BbbReaderError(
                f"Reader refused a non-BBB target URL: {target_url}",
                status=SourceResultStatus.HTTP_ERROR,
            )

    def fetch(self, target_url: str) -> str:
        self._validate_target(target_url)
        reader_url = JINA_READER_BASE + target_url
        last_status: int | None = None
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                with httpx.Client(
                    transport=self.transport,
                    follow_redirects=True,
                    timeout=self.timeout,
                    headers=self._headers(),
                ) as client:
                    response = client.get(reader_url)
                last_status = response.status_code

                if response.status_code == 200 and response.text.strip():
                    return response.text
                if response.status_code == 401:
                    raise BbbReaderError(
                        "Jina Reader rejected JINA_API_KEY; remove or correct that optional key.",
                        status=SourceResultStatus.AUTH_REQUIRED,
                        http_status=401,
                    )
                if response.status_code not in RETRYABLE_STATUSES:
                    raise BbbReaderError(
                        f"Jina Reader returned HTTP {response.status_code}.",
                        http_status=response.status_code,
                    )

                if attempt == 0:
                    retry_after = response.headers.get("retry-after", "")
                    try:
                        delay = min(max(float(retry_after), 1.0), 5.0)
                    except ValueError:
                        delay = 1.0
                    time.sleep(delay)
                    continue
            except BbbReaderError:
                raise
            except httpx.TimeoutException as exc:
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc

            if attempt == 0:
                time.sleep(1.0)

        detail = f"HTTP {last_status}" if last_status is not None else repr(last_error)
        raise BbbReaderError(f"Jina Reader could not retrieve BBB ({detail}).", http_status=last_status)
