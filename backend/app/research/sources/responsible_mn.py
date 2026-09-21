from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from rapidfuzz import fuzz

from ..matching import normalize_company_name, normalize_text
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    RawArtifact,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


RESPONSIBLE_MN_URL = "https://responsiblemn.org/ineligible-contractors/"
TIMEOUT_SECONDS = 25.0
USER_AGENT = "ParalegalResearchDatabaseV2/1.0 (+targeted public Responsible Minnesota research)"
FUZZY_REVIEW_CUTOFF = 0.94

_REQUIRED_HEADERS = {
    "contractor",
    "statutory provision rendering non responsible",
    "relevant public documents links",
    "end date",
}
_INDIVIDUAL_SUFFIX_RE = re.compile(r",\s*(?:an\s+)?individual(?:ly)?\s*$", re.I)
_COMPANY_AND_INDIVIDUAL_RE = re.compile(
    r"^(?P<organization>.+)\s+and\s+(?P<person>[^,]+),\s*(?:an\s+)?individual(?:ly)?\s*$",
    re.I,
)


class ResponsibleMinnesotaError(RuntimeError):
    def __init__(self, message: str, *, status: SourceResultStatus) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ResponsibleMinnesotaRecord:
    source_record_id: str
    raw_name: str
    match_names: tuple[str, ...]
    statutory_provision: str
    document_title: str
    document_url: str
    end_date_raw: str
    end_date: date | None
    row_number: int

    def as_dict(self) -> dict:
        return {
            "source_record_id": self.source_record_id,
            "raw_name": self.raw_name,
            "match_names": list(self.match_names),
            "statutory_provision": self.statutory_provision,
            "document_title": self.document_title,
            "document_url": self.document_url,
            "end_date_raw": self.end_date_raw,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "row_number": self.row_number,
        }


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
        lower = tag.casefold()
        if lower == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
                self.tables.append(self._table)
        elif lower == "tr" and self._table is not None and self._depth == 1:
            self._row = []
        elif lower in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._cell_links = []
        elif lower == "a" and self._cell_parts is not None:
            href = dict(attrs).get("href")
            if href:
                self._cell_links.append(href)
        elif lower == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower in {"td", "th"} and self._row is not None and self._cell_parts is not None:
            self._row.append(_Cell(_collapse_space(" ".join(self._cell_parts)), list(self._cell_links)))
            self._cell_parts = None
            self._cell_links = []
        elif lower == "tr" and self._row is not None:
            if self._row and self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif lower == "table":
            if self._depth == 1:
                self._table = None
                self._row = None
                self._cell_parts = None
                self._cell_links = []
            self._depth = max(0, self._depth - 1)


def _collapse_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def _canonical_header(value: str) -> str:
    text = normalize_text(value)
    aliases = {
        "statutory provision rendering non responsible": "statutory provision rendering non responsible",
        "relevant public documents links": "relevant public documents links",
        "relevant public documents link": "relevant public documents links",
    }
    return aliases.get(text, text)


def _parse_end_date(value: str, contractor_name: str) -> date | None:
    raw = _collapse_space(value)
    if not raw:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ResponsibleMinnesotaError(
        f"Responsible Minnesota row for {contractor_name!r} has an invalid end date: {raw!r}.",
        status=SourceResultStatus.DATASET_MALFORMED,
    )


def _listed_identities(value: str) -> tuple[str, ...]:
    raw = _collapse_space(value)
    if not raw:
        return ()

    combined = _COMPANY_AND_INDIVIDUAL_RE.match(raw)
    if combined:
        values = [
            _collapse_space(combined.group("organization")),
            _collapse_space(combined.group("person")),
        ]
    else:
        values = [_INDIVIDUAL_SUFFIX_RE.sub("", raw).strip()]

    result: list[str] = []
    seen: set[str] = set()
    for candidate in values:
        key = normalize_company_name(candidate)
        if candidate and key and key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def _record_id(
    raw_name: str,
    statutory_provision: str,
    document_url: str,
    end_date_raw: str,
) -> str:
    fingerprint = "|".join(
        normalize_text(value)
        for value in (raw_name, statutory_provision, document_url, end_date_raw)
    )
    return "responsible-mn:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]


def parse_responsible_mn_html(content: bytes | str) -> tuple[list[ResponsibleMinnesotaRecord], str]:
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    try:
        html = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResponsibleMinnesotaError(
            "Responsible Minnesota page is not valid UTF-8.",
            status=SourceResultStatus.DATASET_MALFORMED,
        ) from exc

    parser = _TableParser()
    parser.feed(html)

    target_table: list[list[_Cell]] | None = None
    positions: dict[str, int] = {}
    for table in parser.tables:
        if not table:
            continue
        headers = [_canonical_header(cell.text) for cell in table[0]]
        if _REQUIRED_HEADERS.issubset(set(headers)):
            target_table = table
            positions = {header: index for index, header in enumerate(headers)}
            break

    if target_table is None:
        raise ResponsibleMinnesotaError(
            "Responsible Minnesota layout changed: ineligible-contractor table was not found.",
            status=SourceResultStatus.LAYOUT_CHANGED,
        )

    records: list[ResponsibleMinnesotaRecord] = []
    max_position = max(positions.values())
    for row_number, row in enumerate(target_table[1:], start=1):
        if len(row) <= max_position:
            continue

        contractor_cell = row[positions["contractor"]]
        statute_cell = row[positions["statutory provision rendering non responsible"]]
        document_cell = row[positions["relevant public documents links"]]
        end_date_cell = row[positions["end date"]]

        raw_name = _collapse_space(contractor_cell.text)
        if not raw_name:
            continue
        statutory_provision = _collapse_space(statute_cell.text)
        document_title = _collapse_space(document_cell.text)
        document_url = (
            urljoin(RESPONSIBLE_MN_URL, document_cell.links[0])
            if document_cell.links
            else ""
        )
        end_date_raw = _collapse_space(end_date_cell.text)
        end_date = _parse_end_date(end_date_raw, raw_name)
        match_names = _listed_identities(raw_name)
        if not match_names:
            raise ResponsibleMinnesotaError(
                f"Responsible Minnesota row {row_number} has no usable contractor identity.",
                status=SourceResultStatus.DATASET_MALFORMED,
            )

        records.append(
            ResponsibleMinnesotaRecord(
                source_record_id=_record_id(
                    raw_name,
                    statutory_provision,
                    document_url,
                    end_date_raw,
                ),
                raw_name=raw_name,
                match_names=match_names,
                statutory_provision=statutory_provision,
                document_title=document_title,
                document_url=document_url,
                end_date_raw=end_date_raw,
                end_date=end_date,
                row_number=row_number,
            )
        )

    if not records:
        raise ResponsibleMinnesotaError(
            "Responsible Minnesota table was recognized but contained no contractor records.",
            status=SourceResultStatus.DATASET_MALFORMED,
        )

    return records, hashlib.sha256(raw_bytes).hexdigest()


def _approved_names(contractor: ContractorContext) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = [(contractor.contractor_name, "master_name")]
    values.extend(
        (piece.strip(), "approved_alias")
        for piece in re.split(r"[;|\n]+", contractor.related_companies or "")
        if piece.strip()
    )

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, basis in values:
        key = normalize_company_name(value)
        if key and key not in seen:
            seen.add(key)
            result.append((_collapse_space(value), basis))
    return result


def _end_date_state(value: date | None, *, today: date | None = None) -> str:
    if value is None:
        return "NO_END_DATE"
    current = today or date.today()
    return "CURRENT" if value >= current else "EXPIRED"


class ResponsibleMinnesotaSource(ResearchSource):
    source_key = "responsible_mn"
    display_name = "Responsible Minnesota"
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        self.records: list[ResponsibleMinnesotaRecord] | None = None
        self.page_sha256: str | None = None
        self.retrieved_at: str | None = None
        self.http_status: int | None = None
        self.prepare_error: tuple[SourceResultStatus, str, int | None] | None = None

    def health_check(self) -> dict:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "public_html_snapshot_table",
            "url": RESPONSIBLE_MN_URL,
            "api_key_required": False,
            "fetch_once_per_research_run": True,
            "master_field_candidate": "mndol_ineligibility",
            "master_field_ownership": False,
            "negative_semantics": "no-match is partial evidence, not proof of eligibility",
        }

    def prepare(self) -> None:
        self.prepare_error = None
        self.records = None
        self.page_sha256 = None
        self.retrieved_at = datetime.now(timezone.utc).isoformat()
        self.http_status = None

        try:
            response = self.client.get(RESPONSIBLE_MN_URL)
        except httpx.TimeoutException:
            self.prepare_error = (
                SourceResultStatus.TIMEOUT,
                "Responsible Minnesota request timed out.",
                None,
            )
            return
        except httpx.HTTPError as exc:
            self.prepare_error = (
                SourceResultStatus.SOURCE_UNAVAILABLE,
                f"Responsible Minnesota request failed: {exc}",
                None,
            )
            return

        self.http_status = response.status_code
        if response.status_code in {401, 403, 429}:
            self.prepare_error = (
                SourceResultStatus.BLOCKED,
                f"Responsible Minnesota returned HTTP {response.status_code}.",
                response.status_code,
            )
            return
        if response.status_code >= 400:
            self.prepare_error = (
                SourceResultStatus.HTTP_ERROR,
                f"Responsible Minnesota returned HTTP {response.status_code}.",
                response.status_code,
            )
            return

        content_type = response.headers.get("content-type", "").casefold()
        if content_type and "html" not in content_type and "text" not in content_type:
            self.prepare_error = (
                SourceResultStatus.PARSER_FAILURE,
                f"Responsible Minnesota returned unexpected content type {content_type!r}.",
                response.status_code,
            )
            return

        try:
            self.records, self.page_sha256 = parse_responsible_mn_html(response.content)
        except ResponsibleMinnesotaError as exc:
            self.prepare_error = (exc.status, str(exc), response.status_code)

    def search(self, contractor: ContractorContext) -> SourceResult:
        if self.records is None and self.prepare_error is None:
            self.prepare()

        if self.prepare_error:
            status, warning, http_status = self.prepare_error
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=status,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=[warning],
                    source_url=RESPONSIBLE_MN_URL,
                    http_status=http_status,
                    acquisition_method="public_html_snapshot_table",
                )
            )

        approved = _approved_names(contractor)
        exact: list[tuple[ResponsibleMinnesotaRecord, str, str, str]] = []
        ambiguous: list[tuple[ResponsibleMinnesotaRecord, str, str, str, float]] = []

        for record in self.records or []:
            for candidate_name in record.match_names:
                candidate_norm = normalize_company_name(candidate_name)
                if not candidate_norm:
                    continue
                for approved_name, basis in approved:
                    approved_norm = normalize_company_name(approved_name)
                    if not approved_norm:
                        continue
                    if candidate_norm == approved_norm:
                        exact.append((record, approved_name, basis, candidate_name))
                        break
                    score = fuzz.WRatio(approved_norm, candidate_norm) / 100.0
                    if score >= FUZZY_REVIEW_CUTOFF:
                        ambiguous.append(
                            (record, approved_name, basis, candidate_name, round(score, 4))
                        )

        exact_by_id = {item[0].source_record_id: item for item in exact}
        ambiguous_by_id = {
            item[0].source_record_id: item
            for item in ambiguous
            if item[0].source_record_id not in exact_by_id
        }
        exact = list(exact_by_id.values())
        ambiguous = list(ambiguous_by_id.values())

        evidence: list[EvidenceRecord] = []
        current_count = 0
        expired_count = 0
        for record, matched_search_name, basis, matched_source_name in exact:
            end_state = _end_date_state(record.end_date)
            is_current = end_state in {"CURRENT", "NO_END_DATE"}
            current_count += 1 if is_current else 0
            expired_count += 0 if is_current else 1
            evidence.append(
                EvidenceRecord(
                    field_name="mndol_ineligibility",
                    observed_value="Y" if is_current else None,
                    source_record_id=record.source_record_id,
                    source_url=RESPONSIBLE_MN_URL,
                    details={
                        "classification": (
                            "responsible_mn_current_listing"
                            if is_current
                            else "responsible_mn_expired_listing"
                        ),
                        "listed_name": record.raw_name,
                        "matched_source_name": matched_source_name,
                        "matched_search_name": matched_search_name,
                        "query_basis": basis,
                        "statutory_provision": record.statutory_provision,
                        "supporting_document_title": record.document_title,
                        "supporting_document_url": record.document_url,
                        "end_date": record.end_date.isoformat() if record.end_date else None,
                        "end_date_raw": record.end_date_raw,
                        "end_date_state": end_state,
                        "page_sha256": self.page_sha256,
                        "master_field_candidate": "mndol_ineligibility",
                        "master_field_proposal_allowed": False,
                        "source_is_not_authoritative_or_exhaustive": True,
                    },
                )
            )

        if exact:
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            identity_status = IdentityStatus.CONFIRMED
            completeness_status = CompletenessStatus.COMPLETE
            identity_confidence = 1.0
        elif ambiguous:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness_status = CompletenessStatus.COMPLETE
            identity_confidence = None
            first = ambiguous[0]
            evidence.append(
                EvidenceRecord(
                    field_name="mndol_ineligibility",
                    observed_value=None,
                    source_record_id=first[0].source_record_id,
                    source_url=RESPONSIBLE_MN_URL,
                    details={
                        "classification": "possible_responsible_mn_identity",
                        "candidate_count": len(ambiguous),
                        "listed_name": first[0].raw_name,
                        "candidate_name": first[3],
                        "matched_search_name": first[1],
                        "query_basis": first[2],
                        "name_similarity": first[4],
                        "statutory_provision": first[0].statutory_provision,
                        "supporting_document_url": first[0].document_url,
                        "page_sha256": self.page_sha256,
                        "master_field_proposal_allowed": False,
                    },
                )
            )
        else:
            status = SourceResultStatus.SUCCESS_NO_MATCH
            identity_status = IdentityStatus.NOT_EVALUATED
            completeness_status = CompletenessStatus.PARTIAL
            identity_confidence = None

        warnings: list[str] = []
        if ambiguous and not exact:
            warnings.append(
                "Responsible Minnesota contains a similar contractor name, but identity "
                "was not exact enough for automatic confirmation."
            )
        if expired_count:
            warnings.append(
                "At least one matched Responsible Minnesota row has a passed end date; "
                "it is retained as historical evidence and does not assert current ineligibility."
            )
        if not exact and not ambiguous:
            warnings.append(
                "No Responsible Minnesota listing was found. The site disclaims completeness "
                "and says related entities may be ineligible without being individually listed, "
                "so this no-match is not proof of eligibility."
            )

        artifact = RawArtifact(
            artifact_type="html_snapshot_hash",
            sha256=self.page_sha256,
            mime_type="text/html",
            metadata={
                "source_url": RESPONSIBLE_MN_URL,
                "retrieved_at": self.retrieved_at,
                "record_count": len(self.records or []),
            },
        )

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
                evidence=evidence,
                warnings=warnings,
                artifacts=[artifact],
                normalized_payload={
                    "classification": (
                        "MATCH"
                        if exact
                        else ("AMBIGUOUS" if ambiguous else "NO_MATCH")
                    ),
                    "approved_names_searched": [
                        {"name": name, "basis": basis} for name, basis in approved
                    ],
                    "confirmed_record_count": len(exact),
                    "current_confirmed_record_count": current_count,
                    "expired_confirmed_record_count": expired_count,
                    "ambiguous_record_count": len(ambiguous),
                    "source_record_count": len(self.records or []),
                    "page_sha256": self.page_sha256,
                    "retrieved_at": self.retrieved_at,
                    "field_semantics": (
                        "Responsible Minnesota is comparison-only evidence for the "
                        "mndol_ineligibility candidate field until the firm's legacy "
                        "field semantics are explicitly confirmed."
                    ),
                    "negative_semantics": (
                        "No-match means only that no exact/approved-alias listing was "
                        "found in this Responsible Minnesota snapshot. It never means "
                        "the contractor is eligible and never proposes N."
                    ),
                },
                source_record_id=exact[0][0].source_record_id if exact else None,
                source_url=RESPONSIBLE_MN_URL,
                http_status=self.http_status,
                acquisition_method="public_html_snapshot_table",
            )
        )
