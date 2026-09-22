from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

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


WDFI_SEARCH_URL = "https://apps.dfi.wi.gov/apps/CorpSearch/Results.aspx"
WDFI_HOME_URL = "https://apps.dfi.wi.gov/apps/corpsearch/search.aspx"
MAX_SEARCH_TERMS = 6
MAX_DETAIL_CANDIDATES = 8
DETAIL_TIMEOUT_SECONDS = 20.0

ACTIVE_STATUSES = {
    "organized",
    "registered",
    "incorporated qualified registered",
    "restored to good standing",
    "active",
}


@dataclass
class HtmlCell:
    text_parts: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        text = "".join(self.text_parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return text.strip()


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[HtmlCell]] = []
        self._row: list[HtmlCell] | None = None
        self._cell: HtmlCell | None = None
        self._href: str | None = None
        self._link_text: list[str] = []
        self.h1_parts: list[str] = []
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = HtmlCell()
        elif tag == "br" and self._cell is not None:
            self._cell.text_parts.append("\n")
        elif tag == "a" and self._cell is not None:
            self._href = dict(attrs).get("href")
            self._link_text = []
        elif tag == "h1":
            self._in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._cell is not None and self._href:
            text = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            self._cell.links.append((self._href, text))
            self._href = None
            self._link_text = []
        elif tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text_parts.append(data)
            if self._href is not None:
                self._link_text.append(data)
        if self._in_h1:
            self.h1_parts.append(data)

    @property
    def h1(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.h1_parts)).strip()


@dataclass
class WdfiRecord:
    entity_id: str
    name: str
    entity_type: str
    registered_effective_date: str
    status: str
    status_date: str
    detail_url: str
    found_name: str = ""
    registered_office: str = ""
    principal_office: str = ""
    address_1: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""

    @property
    def record_id(self) -> str:
        return self.entity_id

    def public_details(self) -> dict[str, str]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "entity_type": self.entity_type,
            "registered_effective_date": self.registered_effective_date,
            "status": self.status,
            "status_date": self.status_date,
            "found_name": self.found_name,
            "registered_office": self.registered_office,
            "principal_office": self.principal_office,
            "address_1": self.address_1,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
        }


@dataclass
class WdfiCandidate:
    record: WdfiRecord
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    zip_score: float
    matched_search_name: str
    matched_record_name: str
    auto_confirmable: bool
    remembered_judgment: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_record_id": self.record.record_id,
            "record": self.record.public_details(),
            "score": self.score,
            "name_score": self.name_score,
            "address_score": self.address_score,
            "city_score": self.city_score,
            "state_score": self.state_score,
            "zip_score": self.zip_score,
            "matched_search_name": self.matched_search_name,
            "matched_record_name": self.matched_record_name,
            "auto_confirmable": self.auto_confirmable,
            "remembered_judgment": self.remembered_judgment,
        }


@dataclass
class ParsedSearchPage:
    records: list[WdfiRecord]
    declared_count: int | None
    complete: bool


class WdfiRequestError(RuntimeError):
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


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _company_aliases(contractor: ContractorContext) -> list[str]:
    names = [contractor.contractor_name]
    names.extend(
        part.strip()
        for part in re.split(r"[;|\n]+", contractor.related_companies or "")
        if part.strip()
    )
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized = normalize_company_name(name)
        key = normalized or normalize_text(name)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(name.strip())
    return unique[:MAX_SEARCH_TERMS]


def _query_text(name: str) -> str:
    normalized = normalize_company_name(name)
    return normalized or _clean_text(name)


def _entity_id(value: str) -> str:
    token = _clean_text(value).split(" ", 1)[0].upper()
    if re.fullmatch(r"[A-Z0-9]{6,8}", token) and re.search(r"[A-Z]", token) and re.search(r"\d", token):
        return token
    return ""


def _first_date(value: str) -> str:
    match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", value or "")
    return match.group(0) if match else ""


def _last_date(value: str) -> str:
    matches = re.findall(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", value or "")
    return matches[-1] if matches else ""


def parse_search_results(html: str, *, base_url: str = WDFI_SEARCH_URL) -> ParsedSearchPage:
    parser = TableParser()
    parser.feed(html)

    plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"\s+", " ", plain)
    count_match = re.search(r"\b([\d,]+)\s+records?\s+for\b", plain, flags=re.IGNORECASE)
    declared_count = int(count_match.group(1).replace(",", "")) if count_match else None

    records: list[WdfiRecord] = []
    for row in parser.rows:
        if len(row) < 4:
            continue
        entity_id = _entity_id(row[0].text)
        if not entity_id:
            continue

        detail_href = ""
        link_name = ""
        for href, text in row[1].links:
            if "details.aspx" in href.casefold():
                detail_href = href
                link_name = text
                break
        if not detail_href or not link_name:
            continue

        name_cell = row[1].text
        found_match = re.search(r"\bfound:\s*(.+?)(?:\n|$)", name_cell, flags=re.IGNORECASE)
        found_name = _clean_text(found_match.group(1)) if found_match else ""
        type_match = re.search(r"\b\d{2}\s*-\s*([^\n]+)", name_cell)
        entity_type = _clean_text(type_match.group(1)) if type_match else ""

        registered_effective_date = _first_date(row[2].text)
        status_date = _last_date(row[3].text)
        status = row[3].text
        if status_date:
            status = status.rsplit(status_date, 1)[0]
        status = re.sub(r"Request a Certificate of Status.*$", "", status, flags=re.IGNORECASE | re.DOTALL)
        status = _clean_text(status)

        records.append(
            WdfiRecord(
                entity_id=entity_id,
                name=_clean_text(link_name),
                entity_type=entity_type,
                registered_effective_date=registered_effective_date,
                status=status,
                status_date=status_date,
                detail_url=urljoin(base_url, detail_href),
                found_name=found_name,
            )
        )

    complete = True
    if declared_count is not None and declared_count != len(records):
        complete = False
    if declared_count is None and not records and "returned no records" not in plain.casefold():
        complete = False
    return ParsedSearchPage(records=records, declared_count=declared_count, complete=complete)


def _row_value(rows: list[list[HtmlCell]], label: str) -> str:
    wanted = normalize_text(label)
    for row in rows:
        if len(row) < 2:
            continue
        if normalize_text(row[0].text) == wanted:
            return row[1].text
    return ""


def _strip_action_text(value: str) -> str:
    value = re.sub(r"File a Registered Agent/Office Update Form.*$", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"Request a Certificate of Status.*$", "", value, flags=re.IGNORECASE | re.DOTALL)
    return value.strip()


def _parse_address(value: str) -> tuple[str, str, str, str]:
    cleaned = _strip_action_text(value)
    lines = [_clean_text(line) for line in cleaned.splitlines() if _clean_text(line)]
    if not lines:
        return "", "", "", ""

    location_index = -1
    location_match: re.Match[str] | None = None
    for index in range(len(lines) - 1, -1, -1):
        match = re.search(
            r"(?P<city>[A-Za-z0-9 .'\-&]+?)\s*,\s*(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)\b",
            lines[index],
        )
        if match:
            location_index = index
            location_match = match
            break
    if not location_match:
        return _clean_text(cleaned), "", "", ""

    city = _clean_text(location_match.group("city"))
    state = location_match.group("state").upper()
    zip_code = location_match.group("zip")
    before = lines[:location_index]
    address_lines = [
        line for line in before
        if re.search(r"\d", line) or re.search(r"\b(?:P\.?\s*O\.?\s*BOX|PO BOX)\b", line, flags=re.IGNORECASE)
    ]
    if not address_lines and before:
        address_lines = before[-1:]
    address = ", ".join(address_lines[-2:])
    return address, city, state, zip_code


def parse_detail_page(html: str, record: WdfiRecord) -> WdfiRecord:
    parser = TableParser()
    parser.feed(html)

    name = parser.h1 or record.name
    entity_id = _clean_text(_row_value(parser.rows, "Entity ID")) or record.entity_id
    registered_date = _clean_text(_row_value(parser.rows, "Registered Effective Date")) or record.registered_effective_date
    status = _strip_action_text(_row_value(parser.rows, "Status")) or record.status
    status_date = _clean_text(_row_value(parser.rows, "Status Date")) or record.status_date
    entity_type = _clean_text(_row_value(parser.rows, "Entity Type")) or record.entity_type
    registered_office = _strip_action_text(_row_value(parser.rows, "Registered Agent Office"))
    principal_office = _strip_action_text(_row_value(parser.rows, "Principal Office"))

    preferred = principal_office or registered_office
    address_1, city, state, zip_code = _parse_address(preferred)

    record.name = _clean_text(name)
    record.entity_id = entity_id
    record.registered_effective_date = registered_date
    record.status = _clean_text(status)
    record.status_date = status_date
    record.entity_type = entity_type
    record.registered_office = registered_office
    record.principal_office = principal_office
    record.address_1 = address_1
    record.city = city
    record.state = state
    record.zip_code = zip_code
    return record


def _date_key(value: str) -> str:
    match = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", value or "")
    if not match:
        return ""
    month, day, year = [int(item) for item in match.groups()]
    if year < 100:
        year += 2000 if year < 70 else 1900
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def _semantic_dfi_key(value: str) -> tuple[str, str]:
    text = normalize_text(value)
    if text in {"y", "yes"}:
        return "active", ""
    date_key = _date_key(value)
    text = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", text)
    text = re.sub(r"\bas of\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text, date_key


def _format_short_date(value: str) -> str:
    key = _date_key(value)
    if not key:
        return value
    parsed = datetime.strptime(key, "%Y-%m-%d")
    return f"{parsed.month}/{parsed.day}/{str(parsed.year)[2:]}"


def _mapped_dfi_value(record: WdfiRecord, current_value: str) -> str | None:
    status_key = normalize_text(record.status)
    if not status_key:
        return None
    if status_key in ACTIVE_STATUSES:
        observed = "Y"
    else:
        short_date = _format_short_date(record.status_date)
        status_display = record.status[:1].upper() + record.status[1:].lower() if record.status else ""
        observed = f"{status_display} as of {short_date}".strip()
    if current_value and _semantic_dfi_key(current_value) == _semantic_dfi_key(observed):
        return current_value
    return observed


def _location_corroborates(contractor: ContractorContext, candidate: WdfiRecord) -> tuple[bool, float]:
    locations = [
        (contractor.address_1, contractor.city, contractor.state, contractor.zip),
        (
            contractor.additional_address,
            contractor.additional_address_city,
            contractor.additional_address_state,
            contractor.additional_address_zip,
        ),
    ]
    corroborated = False
    best_score = 0.0
    candidate_zip = re.sub(r"\D", "", candidate.zip_code or "")[:5]
    for address, city, state, zip_code in locations:
        if not any([address, city, state, zip_code]):
            continue
        score = score_candidate(
            master_name=contractor.contractor_name,
            candidate_name=candidate.name,
            master_address=address,
            candidate_address=candidate.address_1,
            master_city=city,
            candidate_city=candidate.city,
            master_state=state,
            candidate_state=candidate.state,
        )
        local_zip = re.sub(r"\D", "", zip_code or "")[:5]
        zip_match = bool(local_zip and candidate_zip and local_zip == candidate_zip)
        best_score = max(best_score, score.score)
        state_match = bool(state and candidate.state and normalize_text(state) == normalize_text(candidate.state))
        city_match = bool(city and candidate.city and normalize_text(city) == normalize_text(candidate.city))
        address_match = bool(address and candidate.address_1 and score.address_score >= 0.82)
        if zip_match or (state_match and (city_match or address_match)):
            corroborated = True
    return corroborated, best_score


class WdfiCorporateRecordsSource(ResearchSource):
    source_key = "wdfi"
    display_name = "Wisconsin Department of Financial Institutions"
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=DETAIL_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": "ParalegalResearchDesk/2.0 (+local law-firm research workflow)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "public_html",
            "url": WDFI_HOME_URL,
        }

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise WdfiRequestError(
                "WDFI request timed out.",
                status=SourceResultStatus.TIMEOUT,
            ) from exc
        except httpx.HTTPError as exc:
            raise WdfiRequestError(
                f"WDFI request failed: {exc}",
                status=SourceResultStatus.HTTP_ERROR,
            ) from exc

        if response.status_code in {401, 403, 429}:
            raise WdfiRequestError(
                f"WDFI returned HTTP {response.status_code}; the lookup was not treated as complete.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise WdfiRequestError(
                f"WDFI returned HTTP {response.status_code}.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )
        return response

    def _search_name(self, name: str) -> tuple[ParsedSearchPage, str]:
        params = {
            "incDateEnd": "",
            "incDateStart": "",
            "includeActiveOrgs": "Include",
            "includeOldNames": "Include",
            "nameSet": "Entities",
            "orgTypes": "",
            "q": _query_text(name),
            "textSearchType": "ExactPhrase",
            "type": "Advanced",
        }
        response = self._get(WDFI_SEARCH_URL, params=params)
        parsed = parse_search_results(response.text, base_url=str(response.url))
        return parsed, str(response.url)

    def _load_detail(self, record: WdfiRecord) -> WdfiRecord:
        response = self._get(record.detail_url)
        return parse_detail_page(response.text, record)

    def _remembered_judgment(self, bidder_id: int, source_record_id: str) -> str | None:
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT judgment FROM identity_judgments
                WHERE bidder_id=? AND source_key='wdfi' AND source_record_id=?
                """,
                (bidder_id, source_record_id),
            ).fetchone()
        return str(row["judgment"]) if row else None

    def _candidate(
        self,
        contractor: ContractorContext,
        aliases: list[str],
        record: WdfiRecord,
    ) -> WdfiCandidate:
        best_alias = contractor.contractor_name
        best_record_name = record.name
        best_name_score = -1.0
        for alias in aliases:
            for record_name in [record.name, record.found_name]:
                if not record_name:
                    continue
                score = score_candidate(
                    master_name=alias,
                    candidate_name=record_name,
                    master_address=contractor.address_1,
                    candidate_address=record.address_1,
                    master_city=contractor.city,
                    candidate_city=record.city,
                    master_state=contractor.state,
                    candidate_state=record.state,
                )
                if score.name_score > best_name_score:
                    best_name_score = score.name_score
                    best_alias = alias
                    best_record_name = record_name

        scored = score_candidate(
            master_name=best_alias,
            candidate_name=best_record_name,
            master_address=contractor.address_1,
            candidate_address=record.address_1,
            master_city=contractor.city,
            candidate_city=record.city,
            master_state=contractor.state,
            candidate_state=record.state,
        )
        corroborated, location_score = _location_corroborates(contractor, record)
        primary_name_match = normalize_company_name(best_alias) == normalize_company_name(contractor.contractor_name)
        exact_name = normalize_company_name(best_alias) == normalize_company_name(best_record_name)
        has_master_location = any(
            [
                contractor.address_1,
                contractor.city,
                contractor.state,
                contractor.zip,
                contractor.additional_address,
                contractor.additional_address_city,
                contractor.additional_address_state,
                contractor.additional_address_zip,
            ]
        )
        auto_confirmable = primary_name_match and exact_name and (corroborated or not has_master_location)
        zip_score = 0.0
        master_zip = re.sub(r"\D", "", contractor.zip or "")[:5]
        candidate_zip = re.sub(r"\D", "", record.zip_code or "")[:5]
        if master_zip and candidate_zip and master_zip == candidate_zip:
            zip_score = 1.0

        return WdfiCandidate(
            record=record,
            score=round(max(scored.score, location_score), 4),
            name_score=scored.name_score,
            address_score=scored.address_score,
            city_score=scored.city_score,
            state_score=scored.state_score,
            zip_score=zip_score,
            matched_search_name=best_alias,
            matched_record_name=best_record_name,
            auto_confirmable=auto_confirmable,
            remembered_judgment=self._remembered_judgment(contractor.internal_id, record.record_id),
        )

    def _failure_result(
        self,
        contractor: ContractorContext,
        error: WdfiRequestError,
        *,
        warnings: list[str] | None = None,
    ) -> SourceResult:
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=error.status,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.PARTIAL,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                source_url=WDFI_HOME_URL,
                http_status=error.http_status,
                acquisition_method="public_html",
                warnings=[str(error), *(warnings or [])],
            )
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        aliases = _company_aliases(contractor)
        if not aliases:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    source_url=WDFI_HOME_URL,
                    acquisition_method="public_html",
                    warnings=["The bidder has no usable company name for a WDFI lookup."],
                )
            )

        records_by_id: dict[str, WdfiRecord] = {}
        search_log: list[dict[str, Any]] = []
        warnings: list[str] = []
        failures: list[WdfiRequestError] = []
        all_searches_complete = True

        for alias in aliases:
            try:
                page, search_url = self._search_name(alias)
            except WdfiRequestError as exc:
                failures.append(exc)
                all_searches_complete = False
                warnings.append(f"{alias}: {exc}")
                continue
            search_log.append(
                {
                    "searched_name": alias,
                    "query": _query_text(alias),
                    "url": search_url,
                    "declared_count": page.declared_count,
                    "parsed_count": len(page.records),
                    "complete": page.complete,
                }
            )
            if not page.complete:
                all_searches_complete = False
                warnings.append(
                    f"WDFI reported {page.declared_count if page.declared_count is not None else 'an unknown number of'} result(s) for {alias!r}, but the result table could not be proven complete."
                )
            for record in page.records:
                records_by_id.setdefault(record.entity_id, record)

        if not records_by_id:
            if failures and not search_log:
                return self._failure_result(contractor, failures[0], warnings=warnings[1:])
            status = SourceResultStatus.SUCCESS_NO_MATCH if all_searches_complete else SourceResultStatus.PARTIAL_RESULTS
            completeness = CompletenessStatus.COMPLETE if all_searches_complete else CompletenessStatus.PARTIAL
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=status,
                    identity_status=IdentityStatus.REJECTED if all_searches_complete else IdentityStatus.NOT_EVALUATED,
                    completeness_status=completeness,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    source_url=WDFI_HOME_URL,
                    acquisition_method="public_html",
                    warnings=warnings,
                    normalized_payload={"searches": search_log, "top_candidates": []},
                )
            )

        preliminary: list[tuple[float, WdfiRecord]] = []
        for record in records_by_id.values():
            best = 0.0
            for alias in aliases:
                for candidate_name in [record.name, record.found_name]:
                    if candidate_name:
                        best = max(best, score_candidate(master_name=alias, candidate_name=candidate_name).name_score)
            if best >= 0.72:
                preliminary.append((best, record))
        preliminary.sort(key=lambda item: item[0], reverse=True)

        if not preliminary:
            status = SourceResultStatus.SUCCESS_NO_MATCH if all_searches_complete else SourceResultStatus.PARTIAL_RESULTS
            completeness = CompletenessStatus.COMPLETE if all_searches_complete else CompletenessStatus.PARTIAL
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=status,
                    identity_status=IdentityStatus.REJECTED if all_searches_complete else IdentityStatus.NOT_EVALUATED,
                    completeness_status=completeness,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    source_url=WDFI_HOME_URL,
                    acquisition_method="public_html",
                    warnings=warnings,
                    normalized_payload={"searches": search_log, "top_candidates": []},
                )
            )

        detail_failures: list[str] = []
        detailed_records: list[WdfiRecord] = []
        for _, record in preliminary[:MAX_DETAIL_CANDIDATES]:
            try:
                detailed_records.append(self._load_detail(record))
            except WdfiRequestError as exc:
                detail_failures.append(f"{record.entity_id}: {exc}")
                detailed_records.append(record)

        candidates = [self._candidate(contractor, aliases, record) for record in detailed_records]
        candidates.sort(key=lambda item: (item.score, item.name_score), reverse=True)
        top_candidates = candidates[:MAX_DETAIL_CANDIDATES]
        warnings.extend(detail_failures)

        human_same = [candidate for candidate in top_candidates if candidate.remembered_judgment == "SAME_ENTITY"]
        unresolved = [candidate for candidate in top_candidates if candidate.remembered_judgment != "DIFFERENT_ENTITY"]
        auto = [candidate for candidate in unresolved if candidate.auto_confirmable and candidate.name_score >= 0.98]

        selected: WdfiCandidate | None = None
        if len(human_same) == 1:
            selected = human_same[0]
        elif not human_same and len(auto) == 1:
            selected = auto[0]

        if selected is None:
            if not unresolved:
                status = SourceResultStatus.SUCCESS_NO_MATCH if all_searches_complete else SourceResultStatus.PARTIAL_RESULTS
                completeness = CompletenessStatus.COMPLETE if all_searches_complete else CompletenessStatus.PARTIAL
                warnings.append("All plausible WDFI candidates were previously marked as different entities.")
                return self.validate_result(
                    SourceResult(
                        source_key=self.source_key,
                        contractor_id=contractor.internal_id,
                        status=status,
                        identity_status=IdentityStatus.REJECTED,
                        completeness_status=completeness,
                        searched_name=contractor.contractor_name,
                        searched_address=contractor.address_1,
                        source_url=WDFI_HOME_URL,
                        acquisition_method="public_html",
                        warnings=warnings,
                        normalized_payload={
                            "searches": search_log,
                            "top_candidates": [candidate.as_dict() for candidate in top_candidates],
                        },
                    )
                )

            if len(auto) > 1:
                warnings.append("More than one WDFI entity exactly matched and corroborated the bidder; human identity review is required.")
            elif detail_failures:
                warnings.append("A WDFI candidate was found, but one or more detail pages could not be fully retrieved; identity was not guessed.")
            else:
                warnings.append("Possible WDFI entities were found but could not be safely tied to the bidder automatically.")
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.AMBIGUOUS_MATCH,
                    identity_status=IdentityStatus.REVIEW_REQUIRED,
                    completeness_status=CompletenessStatus.PARTIAL if detail_failures or not all_searches_complete else CompletenessStatus.COMPLETE,
                    identity_confidence=top_candidates[0].score if top_candidates else None,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    source_url=WDFI_HOME_URL,
                    acquisition_method="public_html",
                    warnings=warnings,
                    normalized_payload={
                        "searches": search_log,
                        "top_candidates": [candidate.as_dict() for candidate in top_candidates],
                    },
                )
            )

        record = selected.record
        observed = _mapped_dfi_value(record, contractor.dfi)
        evidence: list[EvidenceRecord] = []
        if normalize_text(contractor.state) == "wi" and observed:
            evidence.append(
                EvidenceRecord(
                    field_name="dfi",
                    observed_value=observed,
                    source_record_id=record.record_id,
                    source_url=record.detail_url,
                    details={
                        "raw_status": record.status,
                        "status_date": record.status_date,
                        "entity_type": record.entity_type,
                        "registered_effective_date": record.registered_effective_date,
                        "entity_id": record.entity_id,
                        "principal_office": record.principal_office,
                        "registered_office": record.registered_office,
                    },
                )
            )
        elif normalize_text(contractor.state) != "wi":
            warnings.append(
                "WDFI registration evidence was retained, but no automatic dfi-field proposal was created because this bidder's primary state is not Wisconsin and the legacy dfi field may also contain home-state registry context."
            )
        elif not observed:
            warnings.append("WDFI returned an unrecognized blank status; no dfi-field proposal was created.")

        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
                identity_status=IdentityStatus.CONFIRMED,
                completeness_status=CompletenessStatus.COMPLETE,
                identity_confidence=selected.score,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                source_record_id=record.record_id,
                source_url=record.detail_url,
                acquisition_method="public_html",
                warnings=warnings,
                normalized_payload={
                    "searches": search_log,
                    "selected_record": record.public_details(),
                    "mapped_dfi_value": observed,
                    "top_candidates": [candidate.as_dict() for candidate in top_candidates],
                },
                evidence=evidence,
                artifacts=[
                    RawArtifact(
                        artifact_type="wdfi_entity_record",
                        mime_type="text/html",
                        metadata={
                            "entity_id": record.entity_id,
                            "detail_url": record.detail_url,
                            "acquisition_method": "public_html",
                        },
                    )
                ],
            )
        )
