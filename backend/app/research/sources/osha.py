from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin

import httpx
from rapidfuzz import fuzz

from ..matching import normalize_address, normalize_company_name, normalize_text, score_candidate
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


SEARCH_URL = "https://www.osha.gov/ords/imis/establishment.search"
SEARCH_PAGE_URL = "https://www.osha.gov/ords/imis/establishment.html"
DETAIL_URL = "https://www.osha.gov/ords/imis/establishment.inspection_detail"
FIRST_OSHA_DATA_DATE = date(1972, 1, 1)
MAX_SEARCH_VARIANTS = 4
MAX_DETAIL_FETCHES = 8
REQUEST_TIMEOUT_SECONDS = 20.0
USER_AGENT = "ParalegalResearchDatabaseV2/1.0 (+public OSHA establishment research)"


@dataclass(frozen=True)
class OshaSearchRow:
    activity_number: str
    date_opened: str
    reporting_id: str
    state: str
    inspection_type: str
    scope: str
    sic: str
    naics: str
    violations: str
    establishment_name: str
    detail_url: str

    @property
    def year(self) -> str:
        match = re.search(r"\b(19|20)\d{2}\b", self.date_opened)
        return match.group(0) if match else ""


@dataclass(frozen=True)
class ParsedSearchPage:
    rows: tuple[OshaSearchRow, ...]
    table_found: bool
    total_results: int | None
    complete: bool


@dataclass(frozen=True)
class ScoredGroup:
    rows: tuple[OshaSearchRow, ...]
    score: float
    status: str
    matched_identity: str
    components: tuple[dict, ...]


class OshaFetchError(RuntimeError):
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
        self.rows: list[list[_Cell]] = []
        self._row: list[_Cell] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
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
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell_parts = None
            self._cell_links = []


class _VisibleTextParser(HTMLParser):
    _BREAK_TAGS = {
        "br", "p", "div", "tr", "td", "th", "li", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def lines(self) -> list[str]:
        text = "".join(self.parts).replace("\xa0", " ")
        return [_collapse_space(line) for line in text.splitlines() if _collapse_space(line)]

    def text(self) -> str:
        return "\n".join(self.lines())


def _collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _canonical_header(value: str) -> str:
    value = normalize_text(value)
    aliases = {
        "activity nr": "activity",
        "activity number": "activity",
        "date opened": "date opened",
        "rid": "rid",
        "report id": "rid",
        "st": "state",
        "type": "type",
        "inspection type": "type",
        "establishment": "establishment name",
    }
    return aliases.get(value, value)


def _parse_total_results(text: str) -> int | None:
    match = re.search(r"Results\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)", text, re.IGNORECASE)
    return int(match.group(1).replace(",", "")) if match else None


def parse_search_page(html: str) -> ParsedSearchPage:
    table_parser = _TableParser()
    table_parser.feed(html)
    text_parser = _VisibleTextParser()
    text_parser.feed(html)
    visible_text = text_parser.text()

    rows: list[OshaSearchRow] = []
    table_found = False

    for index, row in enumerate(table_parser.rows):
        headers = [_canonical_header(cell.text) for cell in row]
        if "activity" not in headers or "establishment name" not in headers:
            continue
        table_found = True
        header_index = {header: pos for pos, header in enumerate(headers) if header}

        for data_row in table_parser.rows[index + 1 :]:
            values = [cell.text for cell in data_row]
            if len(values) <= max(header_index.values(), default=-1):
                continue
            activity = values[header_index["activity"]].strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", activity):
                if rows:
                    break
                continue

            def value(name: str) -> str:
                position = header_index.get(name)
                return values[position].strip() if position is not None and position < len(values) else ""

            activity_cell = data_row[header_index["activity"]]
            detail_href = next(
                (href for href in activity_cell.links if "inspection_detail" in href),
                "",
            )
            detail_url = (
                urljoin(SEARCH_URL, detail_href)
                if detail_href
                else f"{DETAIL_URL}?id={activity}"
            )
            rows.append(
                OshaSearchRow(
                    activity_number=activity,
                    date_opened=value("date opened"),
                    reporting_id=value("rid"),
                    state=value("state").upper(),
                    inspection_type=value("type"),
                    scope=value("scope"),
                    sic=value("sic"),
                    naics=value("naics"),
                    violations=value("violations"),
                    establishment_name=value("establishment name"),
                    detail_url=detail_url,
                )
            )
        break

    total_results = _parse_total_results(visible_text)
    no_result_marker = bool(
        re.search(
            r"\b(no\s+(?:matching\s+)?(?:records|results|establishments)|0\s+results)\b",
            visible_text,
            re.IGNORECASE,
        )
    )
    if table_found:
        complete = total_results is None or total_results <= len(rows)
    else:
        complete = no_result_marker or total_results == 0

    return ParsedSearchPage(
        rows=tuple(rows),
        table_found=table_found,
        total_results=total_results,
        complete=complete,
    )


def _extract_block(lines: list[str], label: str, stop_labels: Iterable[str]) -> str:
    label_cf = label.casefold()
    stops = tuple(stop.casefold() for stop in stop_labels)
    for index, line in enumerate(lines):
        if not line.casefold().startswith(label_cf):
            continue
        collected: list[str] = []
        _, _, remainder = line.partition(":")
        if remainder.strip():
            collected.append(remainder.strip())
        for next_line in lines[index + 1 :]:
            next_cf = next_line.casefold()
            if any(next_cf.startswith(stop) for stop in stops):
                break
            collected.append(next_line)
        return _collapse_space(" | ".join(collected))
    return ""


def _parse_address_block(block: str) -> dict[str, str]:
    result = {"street": "", "city": "", "state": "", "zip": ""}
    if not block:
        return result
    match = re.search(
        r"(?P<prefix>.*?)(?:\||,)\s*(?P<city>[^,|]+),\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\s*$",
        block,
    )
    if not match:
        return result
    prefix = match.group("prefix").strip(" ,|")
    result["street"] = prefix.split("|")[-1].strip(" ,|")
    result["city"] = match.group("city").strip()
    result["state"] = match.group("state").upper()
    result["zip"] = match.group("zip")
    return result


def parse_inspection_detail(html: str) -> dict[str, str]:
    parser = _VisibleTextParser()
    parser.feed(html)
    lines = parser.lines()
    text = "\n".join(lines)

    case_match = re.search(r"Case Status:\s*([^\n]+)", text, re.IGNORECASE)
    site_address = _extract_block(
        lines,
        "Site Address:",
        ("Mailing Address:", "Union Status:", "SIC:", "NAICS:", "Inspection Type:"),
    )
    mailing_address = _extract_block(
        lines,
        "Mailing Address:",
        ("Union Status:", "SIC:", "NAICS:", "Inspection Type:", "Scope:"),
    )
    site = _parse_address_block(site_address)
    mailing = _parse_address_block(mailing_address)

    return {
        "case_status": _collapse_space(case_match.group(1)) if case_match else "",
        "site_address": site_address,
        "site_street": site["street"],
        "site_city": site["city"],
        "site_state": site["state"],
        "site_zip": site["zip"],
        "mailing_address": mailing_address,
        "mailing_street": mailing["street"],
        "mailing_city": mailing["city"],
        "mailing_state": mailing["state"],
        "mailing_zip": mailing["zip"],
    }


def _safe_year_replace(value: date, year: int) -> date:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def inspection_date_windows(reference: date) -> list[tuple[date, date]]:
    """Return contiguous <=10-year windows back to OSHA's 1972 data boundary."""
    if reference < FIRST_OSHA_DATA_DATE:
        return []
    windows: list[tuple[date, date]] = []
    end = reference
    while end >= FIRST_OSHA_DATA_DATE:
        if end.year - 10 <= FIRST_OSHA_DATA_DATE.year:
            start = FIRST_OSHA_DATA_DATE
        else:
            start = _safe_year_replace(end, end.year - 10) + timedelta(days=1)
        if start < FIRST_OSHA_DATA_DATE:
            start = FIRST_OSHA_DATA_DATE
        windows.append((start, end))
        if start == FIRST_OSHA_DATA_DATE:
            break
        end = start - timedelta(days=1)
    return windows


def _split_related_companies(value: str) -> list[str]:
    if not value.strip():
        return []
    return [piece.strip() for piece in re.split(r"[;\n|]+", value) if piece.strip()]


def _search_variants(contractor: ContractorContext) -> list[str]:
    raw_names = [contractor.contractor_name, *_split_related_companies(contractor.related_companies)]
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = _collapse_space(value)
        key = normalize_text(value)
        if not value or not key or key in seen:
            return
        seen.add(key)
        variants.append(value)

    for raw in raw_names:
        stripped = normalize_company_name(raw)
        if len(stripped) >= 4:
            add(stripped)
        add(raw)
        if len(variants) >= MAX_SEARCH_VARIANTS:
            break
    return variants[:MAX_SEARCH_VARIANTS]


def _identity_names(contractor: ContractorContext) -> list[str]:
    names = [contractor.contractor_name, *_split_related_companies(contractor.related_companies)]
    return [name for name in names if name.strip()]


def _row_to_payload(row: OshaSearchRow) -> dict[str, str]:
    return {
        "activity_number": row.activity_number,
        "date_opened": row.date_opened,
        "reporting_id": row.reporting_id,
        "state": row.state,
        "inspection_type": row.inspection_type,
        "scope": row.scope,
        "sic": row.sic,
        "naics": row.naics,
        "violations": row.violations,
        "establishment_name": row.establishment_name,
        "detail_url": row.detail_url,
    }


def _address_support(contractor: ContractorContext, details: list[dict[str, str]]) -> float:
    if not contractor.address_1.strip():
        return 0.0
    best = 0.0
    for detail in details:
        candidate_street = detail.get("mailing_street", "")
        if not candidate_street:
            continue
        street_score = fuzz.WRatio(
            normalize_address(contractor.address_1), normalize_address(candidate_street)
        ) / 100.0
        components = [(street_score, 0.75)]
        if contractor.city and detail.get("mailing_city"):
            components.append(
                (
                    1.0
                    if normalize_text(contractor.city) == normalize_text(detail["mailing_city"])
                    else 0.0,
                    0.15,
                )
            )
        if contractor.state and detail.get("mailing_state"):
            components.append(
                (
                    1.0
                    if contractor.state.strip().upper() == detail["mailing_state"].strip().upper()
                    else 0.0,
                    0.10,
                )
            )
        weight = sum(item[1] for item in components)
        score = sum(value * item_weight for value, item_weight in components) / weight
        best = max(best, score)
    return round(best, 4)


class OshaEstablishmentSource(ResearchSource):
    source_key = "osha"
    display_name = "OSHA Establishment Search"
    adapter_version = "1.1.0"
    parser_version = "1.1.0"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        today: date | None = None,
    ) -> None:
        self.today = today or date.today()
        self.client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )

    def health_check(self) -> dict:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "public_html_query",
            "search_url": SEARCH_URL,
            "api_key_required": False,
            "date_strategy": "contiguous_10_year_windows_back_to_1972",
        }

    def _search_request(
        self,
        search_name: str,
        state: str,
        start_date: date,
        end_date: date,
    ) -> tuple[ParsedSearchPage, str, int]:
        params = {
            "establishment": search_name,
            "state": state or "all",
            "officetype": "all",
            "office": "all",
            "p_case": "all",
            "p_violations_exist": "both",
            "startmonth": f"{start_date.month:02d}",
            "startday": f"{start_date.day:02d}",
            "startyear": str(start_date.year),
            "endmonth": f"{end_date.month:02d}",
            "endday": f"{end_date.day:02d}",
            "endyear": str(end_date.year),
            "p_show": "100",
        }
        try:
            response = self.client.get(SEARCH_URL, params=params)
        except httpx.TimeoutException as exc:
            raise OshaFetchError(
                f"OSHA search timed out for {search_name!r}.",
                status=SourceResultStatus.TIMEOUT,
            ) from exc
        except httpx.HTTPError as exc:
            raise OshaFetchError(
                f"OSHA search request failed for {search_name!r}: {exc}",
                status=SourceResultStatus.HTTP_ERROR,
            ) from exc

        if response.status_code in {401, 407}:
            raise OshaFetchError(
                "OSHA unexpectedly required authentication.",
                status=SourceResultStatus.AUTH_REQUIRED,
                http_status=response.status_code,
            )
        if response.status_code in {403, 429}:
            raise OshaFetchError(
                f"OSHA blocked or rate-limited the search request (HTTP {response.status_code}).",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if response.status_code >= 500:
            raise OshaFetchError(
                f"OSHA search service returned HTTP {response.status_code}.",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise OshaFetchError(
                f"OSHA search returned HTTP {response.status_code}.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )

        parsed = parse_search_page(response.text)
        if not parsed.table_found and not parsed.complete:
            raise OshaFetchError(
                "OSHA returned HTML, but the establishment-results layout could not be recognized.",
                status=SourceResultStatus.LAYOUT_CHANGED,
                http_status=response.status_code,
            )
        return parsed, str(response.url), response.status_code

    def _detail_request(self, row: OshaSearchRow) -> dict[str, str]:
        try:
            response = self.client.get(row.detail_url)
        except httpx.HTTPError:
            return {}
        if response.status_code != 200:
            return {}
        return parse_inspection_detail(response.text)

    def _scored_groups(
        self,
        contractor: ContractorContext,
        rows: list[OshaSearchRow],
    ) -> list[ScoredGroup]:
        identities = _identity_names(contractor)
        grouped: dict[str, list[OshaSearchRow]] = {}
        for row in rows:
            key = normalize_company_name(row.establishment_name)
            if key:
                grouped.setdefault(key, []).append(row)

        scored: list[ScoredGroup] = []
        for group_rows in grouped.values():
            candidate_name = group_rows[0].establishment_name
            candidate_state = group_rows[0].state
            best_score = -1.0
            best_status = "LOW"
            matched_identity = contractor.contractor_name
            components: list[dict] = []
            for identity_name in identities:
                score = score_candidate(
                    master_name=identity_name,
                    candidate_name=candidate_name,
                    master_state=contractor.state,
                    candidate_state=candidate_state,
                )
                components.append(
                    {
                        "identity_name": identity_name,
                        "score": score.score,
                        "name_score": score.name_score,
                        "state_score": score.state_score,
                        "status": score.status,
                    }
                )
                if score.score > best_score:
                    best_score = score.score
                    best_status = score.status
                    matched_identity = identity_name
            scored.append(
                ScoredGroup(
                    rows=tuple(group_rows),
                    score=best_score,
                    status=best_status,
                    matched_identity=matched_identity,
                    components=tuple(components),
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored

    def _failure_result(
        self,
        contractor: ContractorContext,
        failure: OshaFetchError,
        *,
        warnings: list[str],
        search_attempts: list[dict],
        rows: list[OshaSearchRow],
    ) -> SourceResult:
        had_success = bool(search_attempts)
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.PARTIAL_RESULTS if had_success else failure.status,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=(
                    CompletenessStatus.PARTIAL if had_success else CompletenessStatus.UNKNOWN
                ),
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=[*warnings, str(failure)],
                acquisition_method="public_html_query",
                source_url=search_attempts[0]["url"] if search_attempts else SEARCH_PAGE_URL,
                http_status=failure.http_status,
                normalized_payload={
                    "search_attempts": search_attempts,
                    "candidate_rows": [_row_to_payload(row) for row in rows],
                },
            )
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        variants = _search_variants(contractor)
        if not variants:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=["The bidder does not have a usable company name for OSHA research."],
                    acquisition_method="public_html_query",
                    source_url=SEARCH_PAGE_URL,
                )
            )

        bidder_state = contractor.state.strip().upper()
        valid_state = bidder_state if re.fullmatch(r"[A-Z]{2}", bidder_state) else ""
        scopes = [valid_state] if valid_state else ["all"]
        if valid_state:
            # OSHA's State field is the inspection location, not necessarily the employer's
            # home state. Search the bidder state first for precision, then nationwide if needed.
            scopes.append("all")
        windows = inspection_date_windows(self.today)

        rows_by_activity: dict[str, OshaSearchRow] = {}
        search_attempts: list[dict] = []
        warnings: list[str] = []
        any_incomplete_page = False
        last_http_status: int | None = None
        selected_scope = ""
        selected_variant = ""

        for scope in scopes:
            matched_in_scope = False
            for variant in variants:
                variant_has_high = False
                for start_date, end_date in windows:
                    try:
                        parsed, request_url, http_status = self._search_request(
                            variant, scope, start_date, end_date
                        )
                    except OshaFetchError as exc:
                        return self._failure_result(
                            contractor,
                            exc,
                            warnings=warnings,
                            search_attempts=search_attempts,
                            rows=list(rows_by_activity.values()),
                        )
                    last_http_status = http_status
                    any_incomplete_page = any_incomplete_page or not parsed.complete
                    search_attempts.append(
                        {
                            "query": variant,
                            "state_scope": scope,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                            "url": request_url,
                            "result_count_on_page": len(parsed.rows),
                            "reported_total": parsed.total_results,
                            "complete": parsed.complete,
                        }
                    )
                    for row in parsed.rows:
                        rows_by_activity[row.activity_number] = row
                    groups = self._scored_groups(contractor, list(rows_by_activity.values()))
                    if groups and groups[0].status == "HIGH":
                        variant_has_high = True
                    # Continue all historical windows for this same variant after finding a
                    # match so older inspections are retained in the evidence snapshot.
                if variant_has_high:
                    selected_scope = scope
                    selected_variant = variant
                    matched_in_scope = True
                    break
            if matched_in_scope:
                break

        all_rows = list(rows_by_activity.values())
        groups = self._scored_groups(contractor, all_rows)
        if not groups or groups[0].status == "LOW":
            status = (
                SourceResultStatus.PARTIAL_RESULTS
                if any_incomplete_page
                else SourceResultStatus.SUCCESS_NO_MATCH
            )
            completeness = (
                CompletenessStatus.PARTIAL
                if status == SourceResultStatus.PARTIAL_RESULTS
                else CompletenessStatus.COMPLETE
            )
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=status,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=completeness,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings,
                    acquisition_method="public_html_query",
                    source_url=search_attempts[0]["url"] if search_attempts else SEARCH_PAGE_URL,
                    http_status=last_http_status,
                    normalized_payload={
                        "match_found": False,
                        "search_attempts": search_attempts,
                        "candidate_rows": [_row_to_payload(row) for row in all_rows],
                    },
                )
            )

        best = groups[0]
        group_rows = list(best.rows)
        candidate_name = group_rows[0].establishment_name
        candidate_key = normalize_company_name(candidate_name)
        exact_identity = any(
            normalize_company_name(name) == candidate_key for name in _identity_names(contractor)
        )
        competing_high = any(
            group.status == "HIGH"
            and normalize_company_name(group.rows[0].establishment_name) != candidate_key
            for group in groups[1:]
        )
        has_bidder_state_inspection = bool(
            valid_state and any(row.state == valid_state for row in group_rows)
        )

        details: list[dict[str, str]] = []
        for row in group_rows[:MAX_DETAIL_FETCHES]:
            detail = self._detail_request(row)
            if detail:
                details.append({"activity_number": row.activity_number, **detail})
        address_score = _address_support(contractor, details)
        address_confirmed = address_score >= 0.82

        confirmed = False
        if not competing_high:
            if exact_identity and has_bidder_state_inspection:
                confirmed = True
            elif best.status == "HIGH" and has_bidder_state_inspection:
                confirmed = True
            elif address_confirmed and (exact_identity or best.status == "HIGH"):
                confirmed = True

        years = sorted({row.year for row in group_rows if row.year}, reverse=True)
        violation_counts = [
            int(row.violations.strip())
            for row in group_rows
            if row.violations.strip().isdigit()
        ]
        selected_attempts = [
            attempt
            for attempt in search_attempts
            if attempt["query"] == selected_variant and attempt["state_scope"] == selected_scope
        ]
        history_complete = bool(selected_attempts) and all(
            attempt["complete"] for attempt in selected_attempts
        )

        payload = {
            "match_found": True,
            "matched_establishment_name": candidate_name,
            "matched_identity_name": best.matched_identity,
            "identity_score": round(best.score, 4),
            "address_support_score": address_score,
            "has_bidder_state_inspection": has_bidder_state_inspection,
            "score_components": list(best.components),
            "inspection_count": len(group_rows),
            "inspection_years": years,
            "total_listed_violations": sum(violation_counts),
            "inspections": [_row_to_payload(row) for row in group_rows],
            "inspection_details": details,
            "search_attempts": search_attempts,
            "selected_query": selected_variant,
            "selected_state_scope": selected_scope,
            "history_complete_for_selected_query": history_complete,
            "note": (
                "OSHA severe-violation and years master-field semantics are not yet defined; "
                "those values are retained as research evidence only and are not proposed automatically. "
                "OSHA State identifies inspection location, so nationwide fallback matches require extra identity support."
            ),
        }

        primary = group_rows[0]
        if confirmed:
            if any_incomplete_page or not history_complete:
                warnings.append(
                    "A confirmed OSHA match was found, but one or more result windows may not contain every inspection row; the positive OSHA finding is still supported."
                )
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
                    identity_status=IdentityStatus.CONFIRMED,
                    # Completeness here means the positive Y conclusion is supported; the
                    # payload separately records whether the inspection history is exhaustive.
                    completeness_status=CompletenessStatus.COMPLETE,
                    identity_confidence=min(1.0, max(0.0, best.score, address_score)),
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    evidence=[
                        EvidenceRecord(
                            field_name="osha",
                            observed_value="Y",
                            source_record_id=primary.activity_number,
                            source_url=primary.detail_url,
                            details={
                                "matched_establishment_name": candidate_name,
                                "matched_identity_name": best.matched_identity,
                                "inspection_count": len(group_rows),
                                "inspection_years": years,
                                "address_support_score": address_score,
                            },
                        )
                    ],
                    warnings=warnings,
                    acquisition_method="public_html_query",
                    source_record_id=primary.activity_number,
                    source_url=primary.detail_url,
                    http_status=last_http_status,
                    normalized_payload=payload,
                )
            )

        warnings.append(
            "OSHA returned a plausible establishment match, but identity was not strong enough for an automatic proposal."
        )
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.AMBIGUOUS_MATCH,
                identity_status=IdentityStatus.REVIEW_REQUIRED,
                completeness_status=CompletenessStatus.COMPLETE,
                identity_confidence=min(1.0, max(0.0, best.score, address_score)),
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=[
                    EvidenceRecord(
                        field_name="osha",
                        observed_value="Y",
                        source_record_id=primary.activity_number,
                        source_url=primary.detail_url,
                        details={
                            "possible_match": candidate_name,
                            "address_support_score": address_score,
                        },
                    )
                ],
                warnings=warnings,
                acquisition_method="public_html_query",
                source_record_id=primary.activity_number,
                source_url=primary.detail_url,
                http_status=last_http_status,
                normalized_payload=payload,
            )
        )
