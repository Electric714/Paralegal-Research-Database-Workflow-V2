from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from ..matching import normalize_company_name, normalize_text
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


BASE_URL = "https://violationtracker.goodjobsfirst.org/"
SEARCH_URL = "https://violationtracker.goodjobsfirst.org/summary"
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_PAGES_PER_NAME = 10
USER_AGENT = "ParalegalResearchDatabaseV2/1.0 (+targeted public Violation Tracker research)"


@dataclass(frozen=True)
class ViolationTrackerRow:
    company: str
    current_parent: str
    current_parent_industry: str
    primary_offense: str
    year: str
    agency: str
    penalty: str
    penalty_amount: int | None
    duplicate_penalty: bool
    detail_url: str
    parent_url: str


@dataclass(frozen=True)
class ParsedViolationTrackerPage:
    rows: tuple[ViolationTrackerRow, ...]
    table_found: bool
    result_count: int | None
    no_results: bool
    pagination_urls: tuple[str, ...]
    data_version: str | None

    @property
    def layout_recognized(self) -> bool:
        return self.no_results or self.table_found


@dataclass(frozen=True)
class NameSearchOutcome:
    query_name: str
    query_basis: str
    rows: tuple[ViolationTrackerRow, ...]
    attempts: tuple[dict, ...]
    complete: bool
    pages_fetched: int
    reported_count: int | None
    data_version: str | None
    error_status: SourceResultStatus | None = None
    error_http_status: int | None = None
    warnings: tuple[str, ...] = ()


class ViolationTrackerFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: SourceResultStatus,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


@dataclass
class _Cell:
    text: str
    links: list[str]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[_Cell]]] = []
        self._table: list[list[_Cell]] | None = None
        self._row: list[_Cell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_links: list[str] = []
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._table = []
                self.tables.append(self._table)
        elif tag == "tr" and self._table is not None and self._table_depth == 1:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._cell_links = []
        elif tag == "a" and self._cell_parts is not None:
            href = dict(attrs).get("href")
            if href:
                self._cell_links.append(href)
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._row is not None and self._cell_parts is not None:
            self._row.append(
                _Cell(text=_collapse_space(" ".join(self._cell_parts)), links=list(self._cell_links))
            )
            self._cell_parts = None
            self._cell_links = []
        elif tag == "tr" and self._row is not None:
            if self._row and self._table is not None:
                self._table.append(self._row)
            self._row = None
            self._cell_parts = None
            self._cell_links = []
        elif tag == "table":
            if self._table_depth == 1:
                self._table = None
                self._row = None
                self._cell_parts = None
                self._cell_links = []
            self._table_depth = max(0, self._table_depth - 1)


class _PageMetaParser(HTMLParser):
    _BREAK_TAGS = {
        "br", "p", "div", "tr", "td", "th", "li", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.data_version: str | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attrs_dict = dict(attrs)
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and attrs_dict.get("href"):
            self.links.append(attrs_dict["href"] or "")
        if attrs_dict.get("data-version") and not self.data_version:
            self.data_version = attrs_dict["data-version"]
        if tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if not self._ignored_depth and tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        text = "".join(self.parts).replace("\xa0", " ")
        return "\n".join(
            _collapse_space(line) for line in text.splitlines() if _collapse_space(line)
        )


def _collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _canonical_header(value: str) -> str:
    value = normalize_text(value)
    aliases = {
        "company": "company",
        "current parent": "current parent",
        "current parent industry": "current parent industry",
        "primary offense": "primary offense",
        "primary offense type": "primary offense",
        "year": "year",
        "agency": "agency",
        "penalty": "penalty",
        "penalty amount": "penalty",
    }
    return aliases.get(value, value)


def _parse_penalty_amount(value: str) -> int | None:
    matches = re.findall(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", value)
    if not matches:
        return None
    raw = matches[-1].replace(",", "")
    try:
        return int(float(raw))
    except ValueError:
        return None


def parse_results_page(html: str) -> ParsedViolationTrackerPage:
    table_parser = _TableParser()
    table_parser.feed(html)
    meta_parser = _PageMetaParser()
    meta_parser.feed(html)
    visible_text = meta_parser.text()

    result_count: int | None = None
    count_match = re.search(
        r"([\d,]+)\s+Violation\s+Tracker\s+results?\s+found",
        visible_text,
        re.IGNORECASE,
    )
    if count_match:
        result_count = int(count_match.group(1).replace(",", ""))

    no_results = bool(
        re.search(r"No\s+Violation\s+Tracker\s+results?\s+found", visible_text, re.IGNORECASE)
    )
    if no_results:
        result_count = 0

    rows: list[ViolationTrackerRow] = []
    table_found = False
    required_headers = {
        "company",
        "current parent",
        "current parent industry",
        "primary offense",
        "year",
        "agency",
        "penalty",
    }

    for table in table_parser.tables:
        if not table:
            continue
        headers = [_canonical_header(cell.text) for cell in table[0]]
        if not required_headers.issubset(set(headers)):
            continue
        table_found = True
        header_index = {header: pos for pos, header in enumerate(headers) if header}

        for data_row in table[1:]:
            if len(data_row) <= max(header_index.values()):
                continue

            def cell(name: str) -> _Cell:
                return data_row[header_index[name]]

            company = cell("company").text.strip()
            if not company:
                continue
            detail_href = next(
                (href for href in cell("company").links if "/violation-tracker/" in href),
                "",
            )
            if not detail_href:
                detail_href = next(
                    (href for href in cell("penalty").links if "/violation-tracker/" in href),
                    "",
                )
            parent_href = next(
                (href for href in cell("current parent").links if "/parent/" in href),
                "",
            )
            penalty_text = cell("penalty").text.strip()
            rows.append(
                ViolationTrackerRow(
                    company=company,
                    current_parent=cell("current parent").text.strip(),
                    current_parent_industry=cell("current parent industry").text.strip(),
                    primary_offense=cell("primary offense").text.strip(),
                    year=cell("year").text.strip(),
                    agency=cell("agency").text.strip(),
                    penalty=penalty_text,
                    penalty_amount=_parse_penalty_amount(penalty_text),
                    duplicate_penalty=("asterisk" in penalty_text.casefold() or "(*)" in penalty_text),
                    detail_url=urljoin(BASE_URL, detail_href) if detail_href else "",
                    parent_url=urljoin(BASE_URL, parent_href) if parent_href else "",
                )
            )
        break

    pagination_urls: list[str] = []
    seen_urls: set[str] = set()
    for href in meta_parser.links:
        absolute = urljoin(BASE_URL, href)
        query = parse_qs(urlparse(absolute).query)
        if "page" not in query:
            continue
        try:
            page = int(query["page"][0])
        except (ValueError, IndexError):
            continue
        if page <= 1 or absolute in seen_urls:
            continue
        seen_urls.add(absolute)
        pagination_urls.append(absolute)

    pagination_urls.sort(key=_page_number)
    return ParsedViolationTrackerPage(
        rows=tuple(rows),
        table_found=table_found,
        result_count=result_count,
        no_results=no_results,
        pagination_urls=tuple(pagination_urls),
        data_version=meta_parser.data_version,
    )


def _page_number(url: str) -> int:
    query = parse_qs(urlparse(url).query)
    try:
        return int(query.get("page", ["1"])[0])
    except (ValueError, IndexError):
        return 1


def _split_related_companies(value: str) -> list[str]:
    if not value.strip():
        return []
    return [piece.strip() for piece in re.split(r"[;\n|]+", value) if piece.strip()]


def _approved_names(contractor: ContractorContext) -> list[tuple[str, str]]:
    candidates = [
        (contractor.contractor_name, "master_name"),
        *((name, "approved_alias") for name in _split_related_companies(contractor.related_companies)),
    ]
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, basis in candidates:
        value = _collapse_space(value)
        key = normalize_text(value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        result.append((value, basis))
    return result


def _identity_forms(value: str) -> set[str]:
    normalized = normalize_text(value)
    company = normalize_company_name(value)
    forms = {normalized, company}
    if normalized:
        forms.add(normalized.replace(" ", ""))
    if company:
        forms.add(company.replace(" ", ""))
    return {item for item in forms if item}


def _same_identity(left: str, right: str) -> bool:
    return bool(_identity_forms(left) & _identity_forms(right))


def _query_is_preserved(url: str, query_name: str) -> bool:
    parsed = urlparse(url)
    if parsed.hostname != "violationtracker.goodjobsfirst.org":
        return False
    if parsed.path not in {"/", "/summary"}:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    return query.get("company_op") == ["="] and query.get("company") == [query_name]


def _looks_like_challenge(html: str) -> bool:
    lowered = html.casefold()
    strong_markers = (
        "<title>just a moment",
        "<title>attention required",
        "verify you are human",
        "checking your browser before accessing",
    )
    has_result_markers = (
        "violation tracker results found" in lowered
        or "no violation tracker results found" in lowered
    )
    return not has_result_markers and any(marker in lowered for marker in strong_markers)


def _record_id(row: ViolationTrackerRow) -> str:
    if row.detail_url:
        slug = urlparse(row.detail_url).path.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            return f"vt:{slug}"
    fingerprint = "|".join(
        [
            normalize_text(row.company),
            normalize_text(row.current_parent),
            normalize_text(row.primary_offense),
            normalize_text(row.agency),
            row.year,
            str(row.penalty_amount or ""),
        ]
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
    return f"vt:fingerprint:{digest}"


def _row_payload(row: ViolationTrackerRow) -> dict:
    return {
        "source_record_id": _record_id(row),
        "company": row.company,
        "current_parent": row.current_parent,
        "current_parent_industry": row.current_parent_industry,
        "primary_offense": row.primary_offense,
        "year": row.year,
        "agency": row.agency,
        "penalty": row.penalty,
        "penalty_amount": row.penalty_amount,
        "duplicate_penalty": row.duplicate_penalty,
        "detail_url": row.detail_url,
        "parent_url": row.parent_url,
    }


class ViolationTrackerSource(ResearchSource):
    source_key = "violation_tracker"
    display_name = "Violation Tracker"
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )

    def health_check(self) -> dict:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "targeted_public_html_query",
            "search_url": SEARCH_URL,
            "query_operator": "exact_company_or_current_parent",
            "approved_aliases_searched": True,
            "api_key_required": False,
            "master_field_ownership": False,
            "max_pages_per_name": MAX_PAGES_PER_NAME,
        }

    def _request(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ViolationTrackerFetchError(
                "Violation Tracker request timed out.", status=SourceResultStatus.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise ViolationTrackerFetchError(
                f"Violation Tracker request failed: {exc}", status=SourceResultStatus.HTTP_ERROR
            ) from exc

        if response.status_code in {401, 407}:
            raise ViolationTrackerFetchError(
                "Violation Tracker unexpectedly required authentication.",
                status=SourceResultStatus.AUTH_REQUIRED,
                http_status=response.status_code,
            )
        if response.status_code in {403, 429}:
            raise ViolationTrackerFetchError(
                f"Violation Tracker blocked or rate-limited the request (HTTP {response.status_code}).",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if response.status_code >= 500:
            raise ViolationTrackerFetchError(
                f"Violation Tracker returned HTTP {response.status_code}.",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise ViolationTrackerFetchError(
                f"Violation Tracker returned HTTP {response.status_code}.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )
        if _looks_like_challenge(response.text):
            raise ViolationTrackerFetchError(
                "Violation Tracker returned an interactive anti-bot/challenge page; no bypass was attempted.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        return response

    def _query_name(self, query_name: str, query_basis: str) -> NameSearchOutcome:
        queue: list[tuple[str, dict[str, str] | None]] = [
            (SEARCH_URL, {"company_op": "=", "company": query_name})
        ]
        queued_urls: set[str] = set()
        visited_urls: set[str] = set()
        rows: list[ViolationTrackerRow] = []
        attempts: list[dict] = []
        warnings: list[str] = []
        reported_count: int | None = None
        data_version: str | None = None
        raw_rows_seen = 0
        pages_fetched = 0

        while queue and pages_fetched < MAX_PAGES_PER_NAME:
            request_url, params = queue.pop(0)
            try:
                response = self._request(request_url, params=params)
            except ViolationTrackerFetchError as exc:
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=tuple(rows),
                    attempts=tuple(attempts),
                    complete=False,
                    pages_fetched=pages_fetched,
                    reported_count=reported_count,
                    data_version=data_version,
                    error_status=exc.status,
                    error_http_status=exc.http_status,
                    warnings=tuple([*warnings, str(exc)]),
                )

            final_url = str(response.url)
            if not _query_is_preserved(final_url, query_name):
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=tuple(rows),
                    attempts=tuple(attempts),
                    complete=False,
                    pages_fetched=pages_fetched,
                    reported_count=reported_count,
                    data_version=data_version,
                    error_status=SourceResultStatus.PARSER_FAILURE,
                    error_http_status=response.status_code,
                    warnings=tuple(
                        [
                            *warnings,
                            "Violation Tracker did not preserve the exact company filter in the final response URL; the result was rejected rather than treated as a negative.",
                        ]
                    ),
                )

            visited_urls.add(final_url)
            parsed = parse_results_page(response.text)
            pages_fetched += 1
            data_version = parsed.data_version or data_version
            attempts.append(
                {
                    "query": query_name,
                    "query_basis": query_basis,
                    "url": final_url,
                    "page": _page_number(final_url),
                    "http_status": response.status_code,
                    "rows_on_page": len(parsed.rows),
                    "reported_total": parsed.result_count,
                    "data_version": parsed.data_version,
                }
            )

            if not parsed.layout_recognized:
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=tuple(rows),
                    attempts=tuple(attempts),
                    complete=False,
                    pages_fetched=pages_fetched,
                    reported_count=reported_count,
                    data_version=data_version,
                    error_status=SourceResultStatus.LAYOUT_CHANGED,
                    error_http_status=response.status_code,
                    warnings=tuple(
                        [
                            *warnings,
                            "Violation Tracker returned HTML, but the expected result table/no-results marker was not recognized.",
                        ]
                    ),
                )

            if parsed.no_results:
                if pages_fetched != 1 or rows:
                    return NameSearchOutcome(
                        query_name=query_name,
                        query_basis=query_basis,
                        rows=tuple(rows),
                        attempts=tuple(attempts),
                        complete=False,
                        pages_fetched=pages_fetched,
                        reported_count=reported_count,
                        data_version=data_version,
                        error_status=SourceResultStatus.PARSER_FAILURE,
                        error_http_status=response.status_code,
                        warnings=tuple([*warnings, "Violation Tracker returned an inconsistent no-results page."]),
                    )
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=(),
                    attempts=tuple(attempts),
                    complete=True,
                    pages_fetched=pages_fetched,
                    reported_count=0,
                    data_version=data_version,
                    warnings=tuple(warnings),
                )

            if parsed.result_count is None:
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=tuple(rows),
                    attempts=tuple(attempts),
                    complete=False,
                    pages_fetched=pages_fetched,
                    reported_count=reported_count,
                    data_version=data_version,
                    error_status=SourceResultStatus.LAYOUT_CHANGED,
                    error_http_status=response.status_code,
                    warnings=tuple([*warnings, "Violation Tracker result count could not be verified."]),
                )

            if reported_count is None:
                reported_count = parsed.result_count
            elif reported_count != parsed.result_count:
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=tuple(rows),
                    attempts=tuple(attempts),
                    complete=False,
                    pages_fetched=pages_fetched,
                    reported_count=reported_count,
                    data_version=data_version,
                    error_status=SourceResultStatus.PARSER_FAILURE,
                    error_http_status=response.status_code,
                    warnings=tuple([*warnings, "Violation Tracker result count changed during pagination."]),
                )

            for row in parsed.rows:
                if not (
                    _same_identity(row.company, query_name)
                    or _same_identity(row.current_parent, query_name)
                ):
                    return NameSearchOutcome(
                        query_name=query_name,
                        query_basis=query_basis,
                        rows=tuple(rows),
                        attempts=tuple(attempts),
                        complete=False,
                        pages_fetched=pages_fetched,
                        reported_count=reported_count,
                        data_version=data_version,
                        error_status=SourceResultStatus.PARSER_FAILURE,
                        error_http_status=response.status_code,
                        warnings=tuple(
                            [
                                *warnings,
                                "Violation Tracker returned a row that matched neither the searched company nor its current-parent field; the filter may have been ignored, so the query was rejected.",
                            ]
                        ),
                    )

            rows.extend(parsed.rows)
            raw_rows_seen += len(parsed.rows)
            if raw_rows_seen >= reported_count:
                return NameSearchOutcome(
                    query_name=query_name,
                    query_basis=query_basis,
                    rows=tuple(rows),
                    attempts=tuple(attempts),
                    complete=True,
                    pages_fetched=pages_fetched,
                    reported_count=reported_count,
                    data_version=data_version,
                    warnings=tuple(warnings),
                )

            for page_url in parsed.pagination_urls:
                if not _query_is_preserved(page_url, query_name):
                    continue
                if page_url in visited_urls or page_url in queued_urls:
                    continue
                queued_urls.add(page_url)
                queue.append((page_url, None))
            queue.sort(key=lambda item: _page_number(item[0]))

            if not queue and raw_rows_seen < reported_count:
                warnings.append(
                    f"Violation Tracker reported {reported_count} results for {query_name!r}, but only {raw_rows_seen} rows could be reached from pagination links."
                )
                break

        if raw_rows_seen < (reported_count or 0):
            warnings.append(
                f"Violation Tracker pagination was not exhausted within the {MAX_PAGES_PER_NAME}-page safety cap for {query_name!r}."
            )
        return NameSearchOutcome(
            query_name=query_name,
            query_basis=query_basis,
            rows=tuple(rows),
            attempts=tuple(attempts),
            complete=False,
            pages_fetched=pages_fetched,
            reported_count=reported_count,
            data_version=data_version,
            error_status=SourceResultStatus.PAGINATION_INCOMPLETE,
            warnings=tuple(warnings),
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        approved_names = _approved_names(contractor)
        if not approved_names:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=["The bidder does not have a usable approved company name for Violation Tracker research."],
                    acquisition_method="targeted_public_html_query",
                    source_url=SEARCH_URL,
                )
            )

        outcomes = [self._query_name(name, basis) for name, basis in approved_names]
        warnings = [warning for outcome in outcomes for warning in outcome.warnings]
        attempts = [attempt for outcome in outcomes for attempt in outcome.attempts]
        last_http_status = next(
            (
                int(attempt["http_status"])
                for outcome in reversed(outcomes)
                for attempt in reversed(outcome.attempts)
                if attempt.get("http_status") is not None
            ),
            None,
        )

        candidates: dict[str, dict] = {}
        for outcome in outcomes:
            for row in outcome.rows:
                if _same_identity(row.company, outcome.query_name):
                    match_basis = "penalized_company"
                    rank = 2
                elif _same_identity(row.current_parent, outcome.query_name):
                    match_basis = "current_parent_only"
                    rank = 1
                else:
                    match_basis = "unverified"
                    rank = 0
                record_id = _record_id(row)
                candidate = {
                    **_row_payload(row),
                    "query_name": outcome.query_name,
                    "query_basis": outcome.query_basis,
                    "match_basis": match_basis,
                }
                existing = candidates.get(record_id)
                if existing is None or rank > int(existing["_rank"]):
                    candidates[record_id] = {**candidate, "_rank": rank}

        direct_records = [
            {key: value for key, value in item.items() if key != "_rank"}
            for item in candidates.values()
            if item["_rank"] == 2
        ]
        parent_only_records = [
            {key: value for key, value in item.items() if key != "_rank"}
            for item in candidates.values()
            if item["_rank"] == 1
        ]
        unverified_records = [
            {key: value for key, value in item.items() if key != "_rank"}
            for item in candidates.values()
            if item["_rank"] == 0
        ]

        all_complete = all(outcome.complete for outcome in outcomes)
        any_valid_page = any(outcome.pages_fetched > 0 for outcome in outcomes)
        failed_queries = [outcome for outcome in outcomes if not outcome.complete]

        if not all_complete:
            status = SourceResultStatus.PARTIAL_RESULTS if any_valid_page else (
                failed_queries[0].error_status or SourceResultStatus.SOURCE_UNAVAILABLE
            )
            completeness = CompletenessStatus.PARTIAL if any_valid_page else CompletenessStatus.UNKNOWN
        elif direct_records:
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            completeness = CompletenessStatus.COMPLETE
        elif parent_only_records or unverified_records:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            completeness = CompletenessStatus.COMPLETE
        else:
            status = SourceResultStatus.SUCCESS_NO_MATCH
            completeness = CompletenessStatus.COMPLETE

        if direct_records:
            identity_status = IdentityStatus.CONFIRMED
            identity_confidence = 1.0
        elif parent_only_records or unverified_records:
            identity_status = IdentityStatus.REVIEW_REQUIRED
            identity_confidence = None
        else:
            identity_status = IdentityStatus.NOT_EVALUATED
            identity_confidence = None

        evidence: list[EvidenceRecord] = []
        for record in direct_records:
            summary_parts = [record["primary_offense"], record["year"], record["penalty"]]
            evidence.append(
                EvidenceRecord(
                    field_name="violation_tracker",
                    observed_value=" | ".join(part for part in summary_parts if part),
                    source_record_id=record["source_record_id"],
                    source_url=record["detail_url"] or None,
                    details={
                        **record,
                        "classification": "direct_penalized_company_match",
                        "master_field_proposal_allowed": False,
                    },
                )
            )

        if not direct_records and parent_only_records:
            first = parent_only_records[0]
            evidence.append(
                EvidenceRecord(
                    field_name="violation_tracker",
                    observed_value=f"Parent-only candidate records: {len(parent_only_records)}",
                    source_record_id=first["source_record_id"],
                    source_url=first["detail_url"] or first["parent_url"] or None,
                    details={
                        "classification": "parent_only_identity_review",
                        "candidate_count": len(parent_only_records),
                        "searched_names": [name for name, _ in approved_names],
                        "master_field_proposal_allowed": False,
                    },
                )
            )

        first_record = (direct_records or parent_only_records or unverified_records or [None])[0]
        source_url = (
            first_record.get("detail_url")
            if first_record
            else (attempts[0]["url"] if attempts else SEARCH_URL)
        )
        source_record_id = first_record.get("source_record_id") if first_record else None

        payload = {
            "classification": (
                "MATCH" if direct_records and all_complete
                else "AMBIGUOUS" if not direct_records and (parent_only_records or unverified_records) and all_complete
                else "NO_MATCH" if all_complete
                else "PARTIAL_OR_FAILED"
            ),
            "approved_names_searched": [
                {"name": name, "basis": basis} for name, basis in approved_names
            ],
            "query_outcomes": [
                {
                    "query_name": outcome.query_name,
                    "query_basis": outcome.query_basis,
                    "complete": outcome.complete,
                    "pages_fetched": outcome.pages_fetched,
                    "reported_count": outcome.reported_count,
                    "data_version": outcome.data_version,
                    "error_status": outcome.error_status.value if outcome.error_status else None,
                    "error_http_status": outcome.error_http_status,
                    "attempts": list(outcome.attempts),
                }
                for outcome in outcomes
            ],
            "direct_records": direct_records,
            "parent_only_records": parent_only_records,
            "unverified_records": unverified_records,
            "direct_record_count": len(direct_records),
            "parent_only_record_count": len(parent_only_records),
            "note": (
                "Violation Tracker exact company searches also return records where the searched entity is the current parent. "
                "Only penalized-company identity matches are confirmed findings; parent-only records require review. "
                "Violation Tracker owns no master bidder fields, so this evidence cannot create an automatic master-field proposal."
            ),
        }

        if status == SourceResultStatus.AMBIGUOUS_MATCH:
            warnings.append(
                "Violation Tracker returned records only through a current-parent relationship; these are not treated as violations of the bidder without human identity review."
            )
        if status == SourceResultStatus.PARTIAL_RESULTS:
            warnings.append(
                "At least one approved-name lookup was incomplete, so this result must not be treated as a clean negative even if another name returned no results."
            )

        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=status,
                identity_status=identity_status,
                completeness_status=completeness,
                identity_confidence=identity_confidence,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=evidence,
                warnings=warnings,
                normalized_payload=payload,
                source_record_id=source_record_id,
                source_url=source_url,
                http_status=last_http_status,
                acquisition_method="targeted_public_html_query",
            )
        )
