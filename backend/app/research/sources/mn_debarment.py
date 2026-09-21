from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from ... import database as db
from ..matching import normalize_address, normalize_company_name, normalize_text, score_candidate
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    RawArtifact,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


MINNESOTA_DEBARMENT_URL = "https://mn.gov/admin/osp/government/suspended-debarred/"
DEFAULT_TIMEOUT_SECONDS = 25.0
FUZZY_REVIEW_CUTOFF = 0.94
MAX_REVIEW_CANDIDATES = 8

_GENERIC_NAME_TOKENS = {
    "and", "company", "contracting", "construction", "corp", "corporation",
    "electric", "electrical", "enterprises", "group", "inc", "incorporated",
    "llc", "services", "service", "systems", "solutions", "the",
}
_RESULTS_RE = re.compile(r"Results\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)", re.I)
_INDIVIDUAL_SUFFIX_RE = re.compile(r",\s*an\s+individual\s*$", re.I)
_LOCATION_RE = re.compile(
    r"^\s*(?P<city>.*?),\s*(?P<state>[A-Z]{2})(?:\s+(?P<zip>\d{5}(?:-\d{4})?))?\s*$",
    re.I,
)


class MinnesotaDebarmentError(RuntimeError):
    def __init__(self, message: str, *, status: SourceResultStatus) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class MinnesotaVendorRecord:
    source_record_id: str
    raw_name: str
    match_name: str
    entity_type: str
    address_1: str
    city: str
    state: str
    zip_code: str
    owner_officer: str
    suspension_date: date | None
    suspension_end_date: date | None
    debarment_date: date | None
    debarment_end_date: date | None
    reinstatement_eligible_date: date | None
    cause: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_record_id": self.source_record_id,
            "name": self.raw_name,
            "match_name": self.match_name,
            "entity_type": self.entity_type,
            "address_1": self.address_1,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "owner_officer": self.owner_officer,
            "suspension_date": self.suspension_date.isoformat() if self.suspension_date else None,
            "suspension_end_date": self.suspension_end_date.isoformat() if self.suspension_end_date else None,
            "debarment_date": self.debarment_date.isoformat() if self.debarment_date else None,
            "debarment_end_date": self.debarment_end_date.isoformat() if self.debarment_end_date else None,
            "reinstatement_eligible_date": self.reinstatement_eligible_date.isoformat() if self.reinstatement_eligible_date else None,
            "cause": self.cause,
        }


@dataclass(frozen=True)
class MinnesotaCandidate:
    record: MinnesotaVendorRecord
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    matched_search_name: str
    exact_name: bool
    location_corroborated: bool
    remembered_judgment: str | None

    @property
    def auto_confirmable(self) -> bool:
        return (
            self.remembered_judgment == "SAME_ENTITY"
            or (
                self.remembered_judgment is None
                and self.exact_name
                and self.location_corroborated
                and self.record.entity_type != "individual"
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_record_id": self.record.source_record_id,
            "score": round(self.score, 4),
            "name_score": round(self.name_score, 4),
            "address_score": round(self.address_score, 4),
            "city_score": round(self.city_score, 4),
            "state_score": round(self.state_score, 4),
            "matched_search_name": self.matched_search_name,
            "matched_record_name": self.record.raw_name,
            "exact_name": self.exact_name,
            "location_corroborated": self.location_corroborated,
            "auto_confirmable": self.auto_confirmable,
            "remembered_judgment": self.remembered_judgment,
            "record": self.record.as_dict(),
        }


class _MinnesotaPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.names: dict[str, str] = {}
        self.detail_rows: dict[str, list[list[str]]] = {}
        self._anchor_id: str | None = None
        self._anchor_parts: list[str] = []
        self._detail_id: str | None = None
        self._detail_depth = 0
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        values = {key.casefold(): (value or "") for key, value in attrs}

        if lower == "a":
            anchor_id = values.get("id", "")
            match = re.fullmatch(r"(\d+)Anchor", anchor_id, re.I)
            if match:
                self._anchor_id = match.group(1)
                self._anchor_parts = []

        if lower == "div":
            if self._detail_id is not None:
                self._detail_depth += 1
            else:
                detail_id = values.get("id", "")
                if detail_id.isdigit():
                    self._detail_id = detail_id
                    self._detail_depth = 1
                    self.detail_rows.setdefault(detail_id, [])

        if self._detail_id is not None and lower == "tr":
            self._row = []
        elif self._detail_id is not None and lower == "td" and self._row is not None:
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._anchor_id is not None:
            self._anchor_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower == "a" and self._anchor_id is not None:
            name = " ".join("".join(self._anchor_parts).split())
            if name:
                self.names[self._anchor_id] = name
            self._anchor_id = None
            self._anchor_parts = []

        if lower == "td" and self._cell_parts is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell_parts).split()))
            self._cell_parts = None
        elif lower == "tr" and self._row is not None and self._detail_id is not None:
            self.detail_rows[self._detail_id].append(self._row)
            self._row = None

        if lower == "div" and self._detail_id is not None:
            self._detail_depth -= 1
            if self._detail_depth <= 0:
                self._detail_id = None
                self._detail_depth = 0


def _parse_date(value: str, field_name: str, record_name: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError as exc:
        raise MinnesotaDebarmentError(
            f"Minnesota vendor record {record_name!r} has an invalid {field_name}: {value!r}.",
            status=SourceResultStatus.DATASET_MALFORMED,
        ) from exc


def _meaningful(value: str) -> bool:
    return bool(re.sub(r"[^A-Za-z0-9]", "", value or ""))


def _parse_record(record_id: str, name: str, rows: list[list[str]]) -> MinnesotaVendorRecord:
    labels: dict[str, str] = {}
    address_1 = ""
    city = ""
    state = ""
    zip_code = ""
    owner_officer = ""

    for row in rows:
        cells = [cell.strip() for cell in row]
        if not cells:
            continue

        joined = " ".join(cell for cell in cells if cell).strip()
        if joined.casefold().startswith("owner/officer:"):
            owner_officer = joined.split(":", 1)[1].strip() if ":" in joined else ""
            continue

        if len(cells) >= 2 and cells[0].endswith(":"):
            labels[normalize_text(cells[0].rstrip(":"))] = cells[1].strip()
            continue

        for value in cells:
            if not _meaningful(value):
                continue
            location = _LOCATION_RE.match(value)
            if location:
                city = (location.group("city") or "").strip()
                state = (location.group("state") or "").upper().strip()
                zip_code = (location.group("zip") or "").strip()
            elif not address_1:
                address_1 = value.strip()

    is_individual = bool(_INDIVIDUAL_SUFFIX_RE.search(name))
    match_name = _INDIVIDUAL_SUFFIX_RE.sub("", name).strip()

    return MinnesotaVendorRecord(
        source_record_id=f"mnosp:{record_id}",
        raw_name=name,
        match_name=match_name,
        entity_type="individual" if is_individual else "organization_or_unknown",
        address_1=address_1,
        city=city,
        state=state,
        zip_code=zip_code,
        owner_officer=owner_officer,
        suspension_date=_parse_date(labels.get("suspension date", ""), "suspension date", name),
        suspension_end_date=_parse_date(labels.get("suspension end date", ""), "suspension end date", name),
        debarment_date=_parse_date(labels.get("debarment date", ""), "debarment date", name),
        debarment_end_date=_parse_date(labels.get("debarment end date", ""), "debarment end date", name),
        reinstatement_eligible_date=_parse_date(
            labels.get("reinstatement eligible date", ""), "reinstatement eligible date", name
        ),
        cause=labels.get("cause of suspension or debarment", "").strip(),
    )


def parse_minnesota_debarment_page(html: str) -> list[MinnesotaVendorRecord]:
    count_match = _RESULTS_RE.search(html)
    if not count_match:
        raise MinnesotaDebarmentError(
            "Minnesota suspended/debarred vendor result count was not found; the page layout may have changed.",
            status=SourceResultStatus.LAYOUT_CHANGED,
        )

    first, last, total = (int(value) for value in count_match.groups())
    if total and (first != 1 or last != total):
        raise MinnesotaDebarmentError(
            f"Minnesota vendor page exposed only results {first}-{last} of {total}; complete pagination was not available.",
            status=SourceResultStatus.PAGINATION_INCOMPLETE,
        )

    parser = _MinnesotaPageParser()
    parser.feed(html)
    parser.close()

    records: list[MinnesotaVendorRecord] = []
    for record_id, name in parser.names.items():
        rows = parser.detail_rows.get(record_id)
        if rows is None:
            raise MinnesotaDebarmentError(
                f"Minnesota vendor record {record_id} has no detail block; the page layout may have changed.",
                status=SourceResultStatus.LAYOUT_CHANGED,
            )
        records.append(_parse_record(record_id, name, rows))

    if len(records) != total:
        raise MinnesotaDebarmentError(
            f"Minnesota vendor page reported {total} records but {len(records)} were parsed.",
            status=SourceResultStatus.LAYOUT_CHANGED,
        )
    return records


def _aliases(contractor: ContractorContext) -> list[str]:
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
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(name.strip())
    return unique[:8]


def _distinctive_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_company_name(value).split()
        if len(token) >= 4 and token not in _GENERIC_NAME_TOKENS
    }


def _street_number(value: str) -> str:
    normalized = normalize_address(value)
    first = normalized.split(" ", 1)[0] if normalized else ""
    return first if any(character.isdigit() for character in first) else ""


def _zip5(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[:5] if len(digits) >= 5 else ""


def _location_corroborates(
    *,
    master_address: str,
    master_city: str,
    master_state: str,
    master_zip: str,
    record: MinnesotaVendorRecord,
    address_score: float,
) -> bool:
    if not master_address or not record.address_1:
        return False

    master_state_norm = normalize_text(master_state)
    record_state_norm = normalize_text(record.state)
    if master_state_norm and record_state_norm and master_state_norm != record_state_norm:
        return False

    master_number = _street_number(master_address)
    record_number = _street_number(record.address_1)
    number_compatible = not (master_number and record_number) or master_number == record_number
    if address_score >= 0.90 and number_compatible:
        return True

    city_match = bool(
        normalize_text(master_city)
        and normalize_text(master_city) == normalize_text(record.city)
    )
    zip_match = bool(_zip5(master_zip) and _zip5(master_zip) == _zip5(record.zip_code))
    return (
        zip_match
        and city_match
        and bool(master_number and record_number and master_number == record_number)
        and address_score >= 0.75
    )


def _is_active(start: date | None, end: date | None, today: date) -> bool:
    if start is None or start > today:
        return False
    return end is None or today <= end


def _action_status(record: MinnesotaVendorRecord, today: date) -> str:
    if _is_active(record.debarment_date, record.debarment_end_date, today):
        return "ACTIVE_DEBARMENT"
    if _is_active(record.suspension_date, record.suspension_end_date, today):
        if not record.debarment_date and record.cause.casefold().startswith("debarred"):
            return "SOURCE_LABEL_CONFLICT"
        return "ACTIVE_SUSPENSION"
    if record.debarment_date or record.suspension_date:
        return "HISTORICAL_ACTION"
    return "ACTION_STATUS_UNKNOWN"


class MinnesotaDebarredVendorsSource(ResearchSource):
    source_key = "mn_debarment"
    display_name = "Minnesota Suspended/Debarred Vendors"
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        cache_dir: Path | None = None,
        today: date | None = None,
        html_override: str | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir or (db.DATA_DIR / "source_cache" / "mn_debarment")
        self.today = today or date.today()
        self.html_override = html_override
        self.records: list[MinnesotaVendorRecord] = []
        self.prepare_failure: MinnesotaDebarmentError | None = None
        self.artifact: RawArtifact | None = None
        self.http_status: int | None = None

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "official_html_master_list",
            "source_url": MINNESOTA_DEBARMENT_URL,
            "record_count": len(self.records),
            "prepared": bool(self.records) and self.prepare_failure is None,
        }

    def _store_artifact(self, html: str) -> RawArtifact:
        payload = html.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"mn-debarred-{digest[:20]}.html"
        if not path.exists():
            path.write_bytes(payload)
        try:
            relative_path = str(path.relative_to(db.DATA_DIR))
        except ValueError:
            relative_path = str(path)
        return RawArtifact(
            artifact_type="html",
            relative_path=relative_path,
            sha256=digest,
            mime_type="text/html",
            metadata={"source_url": MINNESOTA_DEBARMENT_URL},
        )

    def prepare(self) -> None:
        self.records = []
        self.prepare_failure = None
        self.artifact = None
        self.http_status = None
        try:
            if self.html_override is not None:
                html = self.html_override
                self.http_status = 200
            else:
                owns_client = self.client is None
                client = self.client or httpx.Client(
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                    follow_redirects=True,
                    headers={"User-Agent": "ParalegalResearchDatabaseV2/1.0"},
                )
                try:
                    response = client.get(MINNESOTA_DEBARMENT_URL)
                finally:
                    if owns_client:
                        client.close()
                self.http_status = response.status_code
                if response.status_code in {401, 403, 429}:
                    raise MinnesotaDebarmentError(
                        f"Minnesota vendor page returned HTTP {response.status_code}.",
                        status=SourceResultStatus.BLOCKED,
                    )
                if response.status_code >= 400:
                    raise MinnesotaDebarmentError(
                        f"Minnesota vendor page returned HTTP {response.status_code}.",
                        status=SourceResultStatus.HTTP_ERROR,
                    )
                html = response.text

            self.records = parse_minnesota_debarment_page(html)
            self.artifact = self._store_artifact(html)
            if self.artifact:
                self.artifact.metadata["record_count"] = len(self.records)
        except httpx.TimeoutException as exc:
            self.prepare_failure = MinnesotaDebarmentError(
                f"Minnesota vendor page timed out: {exc}", status=SourceResultStatus.TIMEOUT
            )
        except httpx.HTTPError as exc:
            self.prepare_failure = MinnesotaDebarmentError(
                f"Minnesota vendor page could not be retrieved: {exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            )
        except MinnesotaDebarmentError as exc:
            self.prepare_failure = exc
        except Exception as exc:
            self.prepare_failure = MinnesotaDebarmentError(
                f"Minnesota vendor page could not be parsed: {type(exc).__name__}: {exc}",
                status=SourceResultStatus.PARSER_FAILURE,
            )

    def _remembered_judgment(self, bidder_id: int, source_record_id: str) -> str | None:
        try:
            with db.connect() as conn:
                row = conn.execute(
                    """
                    SELECT judgment FROM identity_judgments
                    WHERE bidder_id=? AND source_key='mn_debarment' AND source_record_id=?
                    """,
                    (bidder_id, source_record_id),
                ).fetchone()
        except Exception:
            return None
        return str(row["judgment"]) if row else None

    def _score_record(
        self,
        contractor: ContractorContext,
        record: MinnesotaVendorRecord,
        aliases: list[str],
    ) -> MinnesotaCandidate | None:
        best: MinnesotaCandidate | None = None
        address_options = [
            (contractor.address_1, contractor.city, contractor.state, contractor.zip),
            (
                contractor.additional_address,
                contractor.additional_address_city,
                contractor.additional_address_state,
                contractor.additional_address_zip,
            ),
        ]

        for alias in aliases:
            exact_name = normalize_company_name(alias) == normalize_company_name(record.match_name)
            name_tokens_overlap = bool(_distinctive_tokens(alias) & _distinctive_tokens(record.match_name))

            for master_address, master_city, master_state, master_zip in address_options:
                match = score_candidate(
                    master_name=alias,
                    candidate_name=record.match_name,
                    master_address=master_address,
                    candidate_address=record.address_1,
                    master_city=master_city,
                    candidate_city=record.city,
                    master_state=master_state,
                    candidate_state=record.state,
                )
                plausible = exact_name or (
                    match.name_score >= FUZZY_REVIEW_CUTOFF and name_tokens_overlap
                )
                if not plausible:
                    continue

                location_ok = _location_corroborates(
                    master_address=master_address,
                    master_city=master_city,
                    master_state=master_state,
                    master_zip=master_zip,
                    record=record,
                    address_score=match.address_score,
                )
                score = max(match.score, 0.80 if exact_name else 0.78)
                candidate = MinnesotaCandidate(
                    record=record,
                    score=score,
                    name_score=match.name_score,
                    address_score=match.address_score,
                    city_score=match.city_score,
                    state_score=match.state_score,
                    matched_search_name=alias,
                    exact_name=exact_name,
                    location_corroborated=location_ok,
                    remembered_judgment=self._remembered_judgment(
                        contractor.internal_id, record.source_record_id
                    ),
                )
                if best is None or (candidate.score, candidate.address_score) > (
                    best.score,
                    best.address_score,
                ):
                    best = candidate
        return best

    def _failure_result(self, contractor: ContractorContext) -> SourceResult:
        failure = self.prepare_failure or MinnesotaDebarmentError(
            "Minnesota vendor dataset is not prepared.", status=SourceResultStatus.SOURCE_UNAVAILABLE
        )
        return SourceResult(
            source_key=self.source_key,
            contractor_id=contractor.internal_id,
            status=failure.status,
            identity_status=IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.UNKNOWN,
            searched_name=contractor.contractor_name,
            searched_address=contractor.address_1,
            warnings=[str(failure)],
            source_url=MINNESOTA_DEBARMENT_URL,
            http_status=self.http_status,
            acquisition_method="official_html_master_list",
            adapter_version=self.adapter_version,
            parser_version=self.parser_version,
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        if self.prepare_failure is not None or not self.records:
            return self._failure_result(contractor)

        aliases = _aliases(contractor)
        candidates: list[MinnesotaCandidate] = []
        for record in self.records:
            candidate = self._score_record(contractor, record, aliases)
            if candidate is None or candidate.remembered_judgment == "DIFFERENT_ENTITY":
                continue
            candidates.append(candidate)

        candidates.sort(key=lambda item: (item.score, item.name_score, item.address_score), reverse=True)
        candidates = candidates[:MAX_REVIEW_CANDIDATES]
        artifacts = [self.artifact] if self.artifact else []

        if not candidates:
            return SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.SUCCESS_NO_MATCH,
                identity_status=IdentityStatus.REJECTED,
                completeness_status=CompletenessStatus.COMPLETE,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                artifacts=artifacts,
                normalized_payload={
                    "dataset_record_count": len(self.records),
                    "candidate_count": 0,
                    "top_candidates": [],
                },
                source_url=MINNESOTA_DEBARMENT_URL,
                http_status=self.http_status,
                acquisition_method="official_html_master_list",
                adapter_version=self.adapter_version,
                parser_version=self.parser_version,
            )

        confirmed = [candidate for candidate in candidates if candidate.auto_confirmable]
        unresolved = [candidate for candidate in candidates if not candidate.auto_confirmable]
        if not confirmed or unresolved:
            warnings = [
                "Possible Minnesota suspended/debarred vendor match requires identity review before any master-field change."
            ]
            if any(candidate.record.entity_type == "individual" for candidate in candidates):
                warnings.append(
                    "Individual-person records are never auto-confirmed as the bidder company."
                )
            return SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.AMBIGUOUS_MATCH,
                identity_status=IdentityStatus.REVIEW_REQUIRED,
                completeness_status=CompletenessStatus.COMPLETE,
                identity_confidence=max(candidate.score for candidate in candidates),
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=warnings,
                artifacts=artifacts,
                normalized_payload={
                    "dataset_record_count": len(self.records),
                    "candidate_count": len(candidates),
                    "top_candidates": [candidate.as_dict() for candidate in candidates],
                },
                source_url=MINNESOTA_DEBARMENT_URL,
                http_status=self.http_status,
                acquisition_method="official_html_master_list",
                adapter_version=self.adapter_version,
                parser_version=self.parser_version,
            )

        record_details = []
        active_debarments = []
        for candidate in confirmed:
            status = _action_status(candidate.record, self.today)
            detail = {**candidate.record.as_dict(), "current_action_status": status}
            record_details.append(detail)
            if status == "ACTIVE_DEBARMENT":
                active_debarments.append(detail)

        observed = "Y" if active_debarments else None
        evidence = [
            EvidenceRecord(
                field_name="state_federal_debarment",
                observed_value=observed,
                source_record_id=confirmed[0].record.source_record_id if len(confirmed) == 1 else None,
                source_url=MINNESOTA_DEBARMENT_URL,
                details={
                    "confirmed_records": record_details,
                    "active_debarment_count": len(active_debarments),
                    "semantics": (
                        "Y is proposed only for an explicitly dated Minnesota debarment that is active on the research date. "
                        "Suspensions, historical actions, and conflicting source labels remain evidence-only."
                    ),
                },
            )
        ]
        warnings: list[str] = []
        if any(item["current_action_status"] == "SOURCE_LABEL_CONFLICT" for item in record_details):
            warnings.append(
                "Minnesota source text says debarred but supplies suspension dates rather than an explicit debarment date; retained as evidence only."
            )

        return SourceResult(
            source_key=self.source_key,
            contractor_id=contractor.internal_id,
            status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
            identity_status=IdentityStatus.CONFIRMED,
            completeness_status=CompletenessStatus.COMPLETE,
            identity_confidence=max(candidate.score for candidate in confirmed),
            searched_name=contractor.contractor_name,
            searched_address=contractor.address_1,
            evidence=evidence,
            warnings=warnings,
            artifacts=artifacts,
            normalized_payload={
                "dataset_record_count": len(self.records),
                "candidate_count": len(candidates),
                "confirmed_records": record_details,
                "active_debarment_count": len(active_debarments),
                "top_candidates": [candidate.as_dict() for candidate in confirmed],
            },
            source_record_id=confirmed[0].record.source_record_id if len(confirmed) == 1 else None,
            source_url=MINNESOTA_DEBARMENT_URL,
            http_status=self.http_status,
            acquisition_method="official_html_master_list",
            adapter_version=self.adapter_version,
            parser_version=self.parser_version,
        )
