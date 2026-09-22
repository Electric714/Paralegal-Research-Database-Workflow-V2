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
    had_valid_page: bool
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
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
                self.tables.append(self._table)
        elif tag == "tr" and self._table is not None and self._depth == 1:
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
                _Cell(_collapse_space(" ".join(self._cell_parts)), list(self._cell_links))
            )
            self._cell_parts = None
            self._cell_links = []
        elif tag == "tr" and self._row is not None:
            if self._row and self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table":
            if self._depth == 1:
                self._table = None
                self._row = None
                self._cell_parts = None
                self._cell_links = []
            self._depth = max(0, self._depth - 1)


class _MetaParser(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.data_version: str | None = None
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attrs_dict = dict(attrs)
        if tag in {"script", "style"}:
            self._ignored += 1
            return
        if self._ignored:
            return
        if tag == "a" and attrs_dict.get("href"):
            self.links.append(attrs_dict["href"] or "")
        if attrs_dict.get("data-version") and not self.data_version:
            self.data_version = attrs_dict["data-version"]
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"} and self._ignored:
            self._ignored -= 1
            return
        if not self._ignored and tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
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
    return {
        "primary offense type": "primary offense",
        "primary offense": "primary offense",
        "penalty amount": "penalty",
        "penalty": "penalty",
    }.get(value, value)


def _parse_penalty_amount(value: str) -> int | None:
    matches = re.findall(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", value)
    if not matches:
        return None
    try:
        return int(float(matches[-1].replace(",", "")))
    except ValueError:
        return None


def _page_number(url: str) -> int:
    query = parse_qs(urlparse(url).query)
    try:
        return int(query.get("page", ["1"])[0])
    except (ValueError, IndexError):
        return 1


def parse_results_page(html: str) -> ParsedViolationTrackerPage:
    tables = _TableParser()
    tables.feed(html)
    meta = _MetaParser()
    meta.feed(html)
    visible = meta.text()

    no_results = bool(
        re.search(r"No\s+Violation\s+Tracker\s+results?\s+found", visible, re.IGNORECASE)
    )
    count_match = re.search(
        r"([\d,]+)\s+Violation\s+Tracker\s+results?\s+found", visible, re.IGNORECASE
    )
    result_count = int(count_match.group(1).replace(",", "")) if count_match else None
    if no_results:
        result_count = 0

    required = {
        "company", "current parent", "current parent industry", "primary offense",
        "year", "agency", "penalty",
    }
    rows: list[ViolationTrackerRow] = []
    table_found = False

    for table in tables.tables:
        if not table:
            continue
        headers = [_canonical_header(cell.text) for cell in table[0]]
        if not required.issubset(set(headers)):
            continue
        table_found = True
        positions = {header: index for index, header in enumerate(headers)}
        max_position = max(positions.values())

        for data_row in table[1:]:
            if len(data_row) <= max_position:
                continue

            def cell(name: str) -> _Cell:
                return data_row[positions[name]]

            company = cell("company").text.strip()
            if not company:
                continue
            detail_href = next(
                (href for href in cell("company").links if "/violation-tracker/" in href), ""
            ) or next(
                (href for href in cell("penalty").links if "/violation-tracker/" in href), ""
            )
            parent_href = next(
                (href for href in cell("current parent").links if "/parent/" in href), ""
            )
            penalty = cell("penalty").text.strip()
            rows.append(
                ViolationTrackerRow(
                    company=company,
                    current_parent=cell("current parent").text.strip(),
                    current_parent_industry=cell("current parent industry").text.strip(),
                    primary_offense=cell("primary offense").text.strip(),
                    year=cell("year").text.strip(),
                    agency=cell("agency").text.strip(),
                    penalty=penalty,
                    penalty_amount=_parse_penalty_amount(penalty),
                    duplicate_penalty=("asterisk" in penalty.casefold() or "(*)" in penalty),
                    detail_url=urljoin(BASE_URL, detail_href) if detail_href else "",
                    parent_url=urljoin(BASE_URL, parent_href) if parent_href else "",
                )
            )
        break

    pages: list[str] = []
    seen: set[str] = set()
    for href in meta.links:
        absolute = urljoin(BASE_URL, href)
        query = parse_qs(urlparse(absolute).query)
        if "page" not in query:
            continue
        try:
            page = int(query["page"][0])
        except (ValueError, IndexError):
            continue
        if page > 1 and absolute not in seen:
            seen.add(absolute)
            pages.append(absolute)
    pages.sort(key=_page_number)

    return ParsedViolationTrackerPage(
        rows=tuple(rows),
        table_found=table_found,
        result_count=result_count,
        no_results=no_results,
        pagination_urls=tuple(pages),
        data_version=meta.data_version,
    )


def _split_related_companies(value: str) -> list[str]:
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
        if value and key and key not in seen:
            seen.add(key)
            result.append((value, basis))
    return result


def _identity_forms(value: str) -> set[str]:
    # Preserve token boundaries. Removing every internal space can collapse
    # genuinely different legal names (for example, "AB" and "A B").
    return {
        form
        for form in {normalize_text(value), normalize_company_name(value)}
        if form
    }


def _same_identity(left: str, right: str) -> bool:
    return bool(_identity_forms(left) & _identity_forms(right))


def _query_is_preserved(url: str, query_name: str) -> bool:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "violationtracker.goodjobsfirst.org"
        and parsed.path in {"/", "/summary"}
        and query.get("company_op") == ["="]
        and query.get("company") == [query_name]
    )


def _looks_like_challenge(html: str) -> bool:
    lowered = html.casefold()
    result_marker = (
        "violation tracker results found" in lowered
        or "no violation tracker results found" in lowered
    )
    challenge_markers = (
        "<title>just a moment",
        "<title>attention required",
        "verify you are human",
        "checking your browser before accessing",
    )
    return not result_marker and any(marker in lowered for marker in challenge_markers)


def _record_id(row: ViolationTrackerRow) -> str:
    if row.detail_url:
        slug = urlparse(row.detail_url).path.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            return f"vt:{slug}"
    fingerprint = "|".join(
        [
            normalize_text(row.company), normalize_text(row.current_parent),
            normalize_text(row.primary_offense), normalize_text(row.agency),
            row.year, str(row.penalty_amount or ""),
        ]
    )
    return "vt:fingerprint:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]


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
    adapter_version = "1.0.1"
    parser_version = "1.0.1"

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

    def _failed_outcome(
        self,
        *,
        query_name: str,
        query_basis: str,
        rows: list[ViolationTrackerRow],
        attempts: list[dict],
        pages_fetched: int,
        had_valid_page: bool,
        reported_count: int | None,
        data_version: str | None,
        status: SourceResultStatus,
        warning: str,
        http_status: int | None = None,
    ) -> NameSearchOutcome:
        return NameSearchOutcome(
            query_name=query_name,
            query_basis=query_basis,
            rows=tuple(rows),
            attempts=tuple(attempts),
            complete=False,
            had_valid_page=had_valid_page,
            pages_fetched=pages_fetched,
            reported_count=reported_count,
            data_version=data_version,
            error_status=status,
            error_http_status=http_status,
            warnings=(warning,),
        )

    def _query_name(self, query_name: str, query_basis: str) -> NameSearchOutcome:
        queue: list[tuple[str, dict[str, str] | None]] = [
            (SEARCH_URL, {"company_op": "=", "company": query_name})
        ]
        queued: set[str] = set()
        visited: set[str] = set()
        rows: list[ViolationTrackerRow] = []
        seen_record_ids: set[str] = set()
        attempts: list[dict] = []
        reported_count: int | None = None
        data_version: str | None = None
        pages_fetched = 0
        had_valid_page = False

        while queue and pages_fetched < MAX_PAGES_PER_NAME:
            request_url, params = queue.pop(0)
            try:
                response = self._request(request_url, params=params)
            except ViolationTrackerFetchError as exc:
                return self._failed_outcome(
                    query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                    pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                    reported_count=reported_count, data_version=data_version,
                    status=exc.status, warning=str(exc), http_status=exc.http_status,
                )

            final_url = str(response.url)
            if not _query_is_preserved(final_url, query_name):
                return self._failed_outcome(
                    query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                    pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                    reported_count=reported_count, data_version=data_version,
                    status=SourceResultStatus.PARSER_FAILURE,
                    warning=(
                        "Violation Tracker did not preserve the exact company filter in the final response URL; "
                        "the response was rejected rather than treated as a negative."
                    ),
                    http_status=response.status_code,
                )

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
                return self._failed_outcome(
                    query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                    pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                    reported_count=reported_count, data_version=data_version,
                    status=SourceResultStatus.LAYOUT_CHANGED,
                    warning="Violation Tracker's expected result table/no-results marker was not recognized.",
                    http_status=response.status_code,
                )

            if parsed.no_results:
                if pages_fetched != 1 or rows:
                    return self._failed_outcome(
                        query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                        pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                        reported_count=reported_count, data_version=data_version,
                        status=SourceResultStatus.PARSER_FAILURE,
                        warning="Violation Tracker returned an inconsistent no-results page.",
                        http_status=response.status_code,
                    )
                return NameSearchOutcome(
                    query_name=query_name, query_basis=query_basis, rows=(),
                    attempts=tuple(attempts), complete=True, had_valid_page=True,
                    pages_fetched=pages_fetched, reported_count=0, data_version=data_version,
                )

            if parsed.result_count is None:
                return self._failed_outcome(
                    query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                    pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                    reported_count=reported_count, data_version=data_version,
                    status=SourceResultStatus.LAYOUT_CHANGED,
                    warning="Violation Tracker's result count could not be verified.",
                    http_status=response.status_code,
                )
            if len(parsed.rows) > parsed.result_count:
                return self._failed_outcome(
                    query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                    pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                    reported_count=reported_count, data_version=data_version,
                    status=SourceResultStatus.PARSER_FAILURE,
                    warning="Violation Tracker returned more table rows than its reported result count.",
                    http_status=response.status_code,
                )
            if reported_count is None:
                reported_count = parsed.result_count
            elif reported_count != parsed.result_count:
                return self._failed_outcome(
                    query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                    pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                    reported_count=reported_count, data_version=data_version,
                    status=SourceResultStatus.PARSER_FAILURE,
                    warning="Violation Tracker's result count changed during pagination.",
                    http_status=response.status_code,
                )

            for row in parsed.rows:
                if not (_same_identity(row.company, query_name) or _same_identity(row.current_parent, query_name)):
                    return self._failed_outcome(
                        query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
                        pages_fetched=pages_fetched, had_valid_page=had_valid_page,
                        reported_count=reported_count, data_version=data_version,
                        status=SourceResultStatus.PARSER_FAILURE,
                        warning=(
                            "Violation Tracker returned a row matching neither the searched company nor its "
                            "current-parent field; the filter may have been ignored, so the query was rejected."
                        ),
                        http_status=response.status_code,
                    )

            had_valid_page = True
            for row in parsed.rows:
                record_id = _record_id(row)
                if record_id in seen_record_ids:
                    continue
                seen_record_ids.add(record_id)
                rows.append(row)
            visited.add(final_url)
            if len(seen_record_ids) >= reported_count:
                return NameSearchOutcome(
                    query_name=query_name, query_basis=query_basis, rows=tuple(rows),
                    attempts=tuple(attempts), complete=True, had_valid_page=True,
                    pages_fetched=pages_fetched, reported_count=reported_count,
                    data_version=data_version,
                )

            for page_url in parsed.pagination_urls:
                if (
                    _query_is_preserved(page_url, query_name)
                    and page_url not in visited
                    and page_url not in queued
                ):
                    queued.add(page_url)
                    queue.append((page_url, None))
            queue.sort(key=lambda item: _page_number(item[0]))
            if not queue:
                break

        return self._failed_outcome(
            query_name=query_name, query_basis=query_basis, rows=rows, attempts=attempts,
            pages_fetched=pages_fetched, had_valid_page=had_valid_page,
            reported_count=reported_count, data_version=data_version,
            status=SourceResultStatus.PAGINATION_INCOMPLETE,
            warning=(
                f"Violation Tracker pagination for {query_name!r} was incomplete: "
                f"{len(seen_record_ids)} unique of {reported_count or 'unknown'} reported rows were verified "
                f"within the {MAX_PAGES_PER_NAME}-page safety cap."
            ),
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
                    match_basis, rank = "penalized_company", 2
                elif _same_identity(row.current_parent, outcome.query_name):
                    match_basis, rank = "current_parent_only", 1
                else:
                    match_basis, rank = "unverified", 0
                record_id = _record_id(row)
                item = {
                    **_row_payload(row),
                    "query_name": outcome.query_name,
                    "query_basis": outcome.query_basis,
                    "match_basis": match_basis,
                    "_rank": rank,
                }
                if record_id not in candidates or rank > candidates[record_id]["_rank"]:
                    candidates[record_id] = item

        def records_at(rank: int) -> list[dict]:
            return [
                {key: value for key, value in item.items() if key != "_rank"}
                for item in candidates.values()
                if item["_rank"] == rank
            ]

        direct_records = records_at(2)
        parent_only_records = records_at(1)
        unverified_records = records_at(0)
        all_complete = all(outcome.complete for outcome in outcomes)
        any_complete_query = any(outcome.complete for outcome in outcomes)
        any_partial_evidence = any(outcome.had_valid_page and outcome.rows for outcome in outcomes)
        failed = [outcome for outcome in outcomes if not outcome.complete]

        if not all_complete:
            if any_complete_query or any_partial_evidence:
                status = SourceResultStatus.PARTIAL_RESULTS
                completeness = CompletenessStatus.PARTIAL
            else:
                status = failed[0].error_status or SourceResultStatus.SOURCE_UNAVAILABLE
                completeness = CompletenessStatus.UNKNOWN
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
            summary = " | ".join(
                part for part in [record["primary_offense"], record["year"], record["penalty"]] if part
            )
            evidence.append(
                EvidenceRecord(
                    field_name="violation_tracker",
                    observed_value=summary or "Violation Tracker record found",
                    source_record_id=record["source_record_id"],
                    source_url=record["detail_url"] or None,
                    details={
                        **record,
                        "classification": "direct_penalized_company_match",
                        "master_field_proposal_allowed": False,
                    },
                )
            )
        if parent_only_records:
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
            (first_record.get("detail_url") or first_record.get("parent_url"))
            if first_record
            else (attempts[0]["url"] if attempts else SEARCH_URL)
        )
        source_record_id = first_record.get("source_record_id") if first_record else None

        if all_complete and direct_records:
            classification = "MATCH"
        elif all_complete and (parent_only_records or unverified_records):
            classification = "AMBIGUOUS"
        elif all_complete:
            classification = "NO_MATCH"
        else:
            classification = "PARTIAL_OR_FAILED"

        payload = {
            "classification": classification,
            "approved_names_searched": [
                {"name": name, "basis": basis} for name, basis in approved_names
            ],
            "query_outcomes": [
                {
                    "query_name": outcome.query_name,
                    "query_basis": outcome.query_basis,
                    "complete": outcome.complete,
                    "had_valid_page": outcome.had_valid_page,
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

        if parent_only_records:
            warnings.append(
                "Violation Tracker also returned records through a current-parent relationship; those records are retained as review candidates and are not treated as bidder violations."
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
