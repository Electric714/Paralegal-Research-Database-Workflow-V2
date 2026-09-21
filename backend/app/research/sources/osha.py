from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin

import httpx

from ..matching import normalize_company_name, normalize_text, score_candidate
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
MAX_SEARCH_VARIANTS = 6
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
            text = _collapse_space(" ".join(self._cell_parts))
            self._row.append(_Cell(text=text, links=list(self._cell_links)))
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
        "type": "type",
        "inspection type": "type",
        "establishment": "establishment name",
    }
    return aliases.get(value, value)


def _parse_total_results(text: str) -> int | None:
    match = re.search(r"Results\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1).replace(",", ""))
    zero_match = re.search(r"Results\s+0\s*-\s*0\s+of\s+0", text, re.IGNORECASE)
    return 0 if zero_match else None


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
                # The first non-result table/heading after a results table ends the run.
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
        if table_found:
            break

    total_results = _parse_total_results(visible_text)
    no_result_marker = bool(
        re.search(
            r"\b(no\s+(?:matching\s+)?(?:records|results|establishments)|0\s+results)\b",
            visible_text,
            re.IGNORECASE,
        )
    )
    complete = False
    if table_found:
        if total_results is None:
            complete = True
        else:
            complete = total_results <= len(rows)
    elif no_result_marker or total_results == 0:
        complete = True

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


def parse_inspection_detail(html: str) -> dict[str, str]:
    parser = _VisibleTextParser()
    parser.feed(html)
    lines = parser.lines()
    text = "\n".join(lines)

    case_match = re.search(r"Case Status:\s*([A-Za-z /-]+)", text, re.IGNORECASE)
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

    return {
        "case_status": _collapse_space(case_match.group(1)) if case_match else "",
        "site_address": site_address,
        "mailing_address": mailing_address,
    }


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
        add(raw)
        stripped = normalize_company_name(raw)
        if len(stripped) >= 4:
            add(stripped)
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


class OshaEstablishmentSource(ResearchSource):
    source_key = "osha"
    display_name = "OSHA Establishment Search"
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

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
        }

    def _search_request(self, search_name: str, state: str) -> tuple[ParsedSearchPage, str, int]:
        params = {
            "establishment": search_name,
            "state": state or "all",
            "officetype": "all",
            "office": "all",
            "p_case": "all",
            "p_violations_exist": "both",
            "startmonth": "01",
            "startday": "01",
            "startyear": "1970",
            "endmonth": f"{self.today.month:02d}",
            "endday": f"{self.today.day:02d}",
            "endyear": str(self.today.year),
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

    def _best_group(
        self,
        contractor: ContractorContext,
        rows: list[OshaSearchRow],
    ) -> tuple[list[OshaSearchRow], float, str, str, list[dict]] | None:
        identities = _identity_names(contractor)
        groups: dict[str, list[OshaSearchRow]] = {}
        for row in rows:
            key = normalize_company_name(row.establishment_name)
            if not key:
                continue
            groups.setdefault(key, []).append(row)

        scored_groups: list[tuple[list[OshaSearchRow], float, str, str, list[dict]]] = []
        for group_rows in groups.values():
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

            scored_groups.append(
                (group_rows, best_score, best_status, matched_identity, components)
            )

        if not scored_groups:
            return None
        scored_groups.sort(key=lambda item: item[1], reverse=True)
        return scored_groups[0]

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

        state = contractor.state.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", state):
            state = "all"

        rows_by_activity: dict[str, OshaSearchRow] = {}
        search_attempts: list[dict] = []
        warnings: list[str] = []
        failed_attempts: list[OshaFetchError] = []
        any_incomplete_page = False
        last_http_status: int | None = None
        best: tuple[list[OshaSearchRow], float, str, str, list[dict]] | None = None

        for variant in variants:
            try:
                parsed, request_url, http_status = self._search_request(variant, state)
                last_http_status = http_status
                any_incomplete_page = any_incomplete_page or not parsed.complete
                search_attempts.append(
                    {
                        "query": variant,
                        "url": request_url,
                        "result_count_on_page": len(parsed.rows),
                        "reported_total": parsed.total_results,
                        "complete": parsed.complete,
                    }
                )
                for row in parsed.rows:
                    rows_by_activity[row.activity_number] = row
                best = self._best_group(contractor, list(rows_by_activity.values()))
                if best and best[2] == "HIGH":
                    break
            except OshaFetchError as exc:
                failed_attempts.append(exc)
                warnings.append(str(exc))

        if not search_attempts:
            failure = failed_attempts[0] if failed_attempts else OshaFetchError(
                "OSHA research could not be completed.",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            )
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=failure.status,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings or [str(failure)],
                    acquisition_method="public_html_query",
                    source_url=SEARCH_PAGE_URL,
                    http_status=failure.http_status,
                    normalized_payload={"search_attempts": search_attempts},
                )
            )

        all_rows = list(rows_by_activity.values())
        best = best or self._best_group(contractor, all_rows)

        if best is None or best[2] == "LOW":
            status = (
                SourceResultStatus.PARTIAL_RESULTS
                if failed_attempts or any_incomplete_page
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
                    source_url=search_attempts[0]["url"],
                    http_status=last_http_status,
                    normalized_payload={
                        "match_found": False,
                        "search_attempts": search_attempts,
                        "candidate_rows": [_row_to_payload(row) for row in all_rows],
                    },
                )
            )

        group_rows, score_value, match_status, matched_identity, score_components = best
        candidate_name = group_rows[0].establishment_name
        candidate_key = normalize_company_name(candidate_name)
        competing_groups = {
            normalize_company_name(row.establishment_name)
            for row in all_rows
            if normalize_company_name(row.establishment_name) != candidate_key
        }
        exact_identity = any(
            normalize_company_name(name) == candidate_key
            for name in _identity_names(contractor)
        )

        # Fuzzy matches stay in review. Exact normalized names (LLC/Inc punctuation ignored)
        # or a unique HIGH score can be confirmed without pretending that a merely similar
        # establishment is the same bidder.
        confirmed = exact_identity or (match_status == "HIGH" and not competing_groups)
        if match_status == "HIGH" and competing_groups:
            # Multiple names can simply be low-quality noise from a broad search. Only make
            # this ambiguous if another group itself scores HIGH against one of our identities.
            other_high = False
            for other_key in competing_groups:
                other_rows = [row for row in all_rows if normalize_company_name(row.establishment_name) == other_key]
                other_best = self._best_group(contractor, other_rows)
                if other_best and other_best[2] == "HIGH":
                    other_high = True
                    break
            if other_high:
                confirmed = False

        details: list[dict[str, str]] = []
        for row in group_rows[:MAX_DETAIL_FETCHES]:
            detail = self._detail_request(row)
            if detail:
                details.append({"activity_number": row.activity_number, **detail})

        years = sorted({row.year for row in group_rows if row.year}, reverse=True)
        violation_counts = []
        for row in group_rows:
            if row.violations.strip().isdigit():
                violation_counts.append(int(row.violations.strip()))

        payload = {
            "match_found": True,
            "matched_establishment_name": candidate_name,
            "matched_identity_name": matched_identity,
            "identity_score": round(score_value, 4),
            "score_components": score_components,
            "inspection_count": len(group_rows),
            "inspection_years": years,
            "total_listed_violations": sum(violation_counts),
            "inspections": [_row_to_payload(row) for row in group_rows],
            "inspection_details": details,
            "search_attempts": search_attempts,
            "note": (
                "OSHA severe-violation and years master-field semantics are not yet defined; "
                "those values are retained as research evidence only and are not proposed automatically."
            ),
        }

        primary = group_rows[0]
        if confirmed:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
                    identity_status=IdentityStatus.CONFIRMED,
                    completeness_status=CompletenessStatus.COMPLETE,
                    identity_confidence=min(1.0, max(0.0, score_value)),
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
                                "matched_identity_name": matched_identity,
                                "inspection_count": len(group_rows),
                                "inspection_years": years,
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
                identity_confidence=min(1.0, max(0.0, score_value)),
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=[
                    EvidenceRecord(
                        field_name="osha",
                        observed_value="Y",
                        source_record_id=primary.activity_number,
                        source_url=primary.detail_url,
                        details={"possible_match": candidate_name},
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
