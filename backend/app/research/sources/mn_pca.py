from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from rapidfuzz import fuzz

from ..matching import normalize_company_name, normalize_text
from ..models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource


LANDING_URL = "https://www.pca.state.mn.us/trending-topics/compliance-and-enforcement"
DATA_VIEW_URL = "https://data.pca.state.mn.us/views/Enforcementactionswithpenalties/Complianceandenforcementnumberofcasesperyear"
CSV_EXPORT_URL = DATA_VIEW_URL + ".csv?:showVizHome=no"
TIMEOUT_SECONDS = 30.0
USER_AGENT = "ParalegalResearchDatabaseV2/1.0 (+targeted official MPCA enforcement research)"
FUZZY_REVIEW_THRESHOLD = 94.0
TOKEN_SUBSET_REVIEW_THRESHOLD = 100.0
TOKEN_SUBSET_MIN_TOKENS = 2

NAME_HEADERS = ("company or individual(s)", "company or individual", "regulated party", "company", "facility name")
DATE_HEADERS = ("public date", "date", "closed date")
LOCATION_HEADERS = ("violation location", "location", "city")
VIOLATION_HEADERS = ("violation", "violation description", "violation information", "category")
PENALTY_HEADERS = ("net penalty", "penalty", "penalty amount")
CASE_HEADERS = ("case type", "enforcement action", "action type")


@dataclass(frozen=True)
class MpcaRecord:
    record_id: str
    party: str
    public_date: str
    location: str
    violation: str
    penalty: str
    case_type: str
    raw: dict[str, str]


class MpcaDatasetError(RuntimeError):
    pass


def _header(value: str) -> str:
    return normalize_text(value)


def _first(row: dict[str, str], names: tuple[str, ...]) -> str:
    normalized = {_header(k): (v or "").strip() for k, v in row.items() if k}
    for name in names:
        if _header(name) in normalized:
            return normalized[_header(name)]
    return ""


def parse_mpca_csv(content: bytes | str) -> tuple[list[MpcaRecord], str]:
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    text = raw_bytes.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise MpcaDatasetError("MPCA CSV has no header row.")

    headers = {_header(h) for h in reader.fieldnames if h}
    if not any(_header(h) in headers for h in NAME_HEADERS):
        raise MpcaDatasetError("MPCA CSV layout changed: regulated-party/company column not found.")
    if not any(_header(h) in headers for h in VIOLATION_HEADERS):
        raise MpcaDatasetError("MPCA CSV layout changed: violation column not found.")

    records: list[MpcaRecord] = []
    for row in reader:
        party = _first(row, NAME_HEADERS)
        violation = _first(row, VIOLATION_HEADERS)
        if not party or not violation:
            continue
        public_date = _first(row, DATE_HEADERS)
        location = _first(row, LOCATION_HEADERS)
        penalty = _first(row, PENALTY_HEADERS)
        case_type = _first(row, CASE_HEADERS)
        fingerprint = "|".join(normalize_text(x) for x in (party, public_date, location, violation, penalty, case_type))
        record_id = "mn-pca:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        records.append(MpcaRecord(record_id, party, public_date, location, violation, penalty, case_type, {str(k): str(v or "") for k, v in row.items() if k}))

    return records, hashlib.sha256(raw_bytes).hexdigest()


def _approved_names(contractor: ContractorContext) -> list[tuple[str, str]]:
    values = [(contractor.contractor_name, "master_name")]
    values.extend((x.strip(), "approved_alias") for x in re.split(r"[;|\n]+", contractor.related_companies or "") if x.strip())
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, basis in values:
        key = normalize_company_name(value)
        if key and key not in seen:
            seen.add(key)
            result.append((value, basis))
    return result


def _party_segments(value: str) -> list[str]:
    # MPCA rows can name multiple regulated parties. Split only on strong delimiters;
    # do not split ordinary "&" business names.
    parts = [x.strip() for x in re.split(r"[;\n]+|\s+/\s+", value) if x.strip()]
    return parts or [value.strip()]


def _should_review_name_variant(approved_name: str, candidate_name: str, wratio: float) -> bool:
    """Keep plausible entity-name variants visible instead of emitting a false clean no-match.

    WRatio catches typos and punctuation variation. token_set_ratio catches common legal-name
    expansions such as "Acme Construction" vs "Acme Construction Services" where all tokens
    of the shorter approved name are present in the longer source name. These are review-only;
    they never become automatic confirmed matches.
    """

    if wratio >= FUZZY_REVIEW_THRESHOLD:
        return True
    approved_tokens = approved_name.split()
    candidate_tokens = candidate_name.split()
    if min(len(approved_tokens), len(candidate_tokens)) < TOKEN_SUBSET_MIN_TOKENS:
        return False
    return fuzz.token_set_ratio(approved_name, candidate_name) >= TOKEN_SUBSET_REVIEW_THRESHOLD


class MinnesotaPcaEnforcementSource(ResearchSource):
    source_key = "mn_pca"
    display_name = "Minnesota PCA Enforcement Actions"
    adapter_version = "1.0.1"
    parser_version = "1.0.0"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        self.records: list[MpcaRecord] | None = None
        self.dataset_sha256: str | None = None
        self.prepare_error: tuple[SourceResultStatus, str, int | None] | None = None

    def health_check(self) -> dict:
        return {"source_key": self.source_key, "implemented": True, "acquisition_mode": "official_tableau_csv_export", "url": CSV_EXPORT_URL}

    def prepare(self) -> None:
        self.prepare_error = None
        try:
            response = self.client.get(CSV_EXPORT_URL)
        except httpx.TimeoutException:
            self.prepare_error = (SourceResultStatus.TIMEOUT, "MPCA dataset export timed out.", None)
            return
        except httpx.HTTPError as exc:
            self.prepare_error = (SourceResultStatus.SOURCE_UNAVAILABLE, f"MPCA dataset export failed: {exc}", None)
            return

        if response.status_code in {401, 403}:
            self.prepare_error = (SourceResultStatus.BLOCKED, "MPCA structured export is not publicly accessible.", response.status_code)
            return
        if response.status_code >= 400:
            self.prepare_error = (SourceResultStatus.HTTP_ERROR, f"MPCA export returned HTTP {response.status_code}.", response.status_code)
            return

        content_type = response.headers.get("content-type", "").casefold()
        body = response.content
        if "html" in content_type or body.lstrip().lower().startswith(b"<!doctype html"):
            self.prepare_error = (SourceResultStatus.PARSER_FAILURE, "MPCA export returned HTML instead of structured CSV; no negative result is allowed.", response.status_code)
            return

        try:
            self.records, self.dataset_sha256 = parse_mpca_csv(body)
        except (UnicodeError, csv.Error, MpcaDatasetError) as exc:
            self.prepare_error = (SourceResultStatus.DATASET_MALFORMED, str(exc), response.status_code)

    def search(self, contractor: ContractorContext) -> SourceResult:
        if self.records is None and self.prepare_error is None:
            self.prepare()

        if self.prepare_error:
            status, warning, http_status = self.prepare_error
            return self.validate_result(SourceResult(
                source_key=self.source_key, contractor_id=contractor.internal_id, status=status,
                completeness_status=CompletenessStatus.UNKNOWN, searched_name=contractor.contractor_name,
                searched_address=contractor.address_1, warnings=[warning], source_url=DATA_VIEW_URL,
                http_status=http_status, acquisition_method="official_tableau_csv_export",
            ))

        approved = _approved_names(contractor)
        exact: list[tuple[MpcaRecord, str, str, str]] = []
        ambiguous: list[tuple[MpcaRecord, str, str, str, float]] = []

        for record in self.records or []:
            for segment in _party_segments(record.party):
                segment_norm = normalize_company_name(segment)
                if not segment_norm:
                    continue
                for name, basis in approved:
                    name_norm = normalize_company_name(name)
                    if segment_norm == name_norm:
                        exact.append((record, name, basis, segment))
                        break
                    score = fuzz.WRatio(name_norm, segment_norm)
                    if _should_review_name_variant(name_norm, segment_norm, score):
                        ambiguous.append((record, name, basis, segment, score / 100.0))

        # Deduplicate records that matched more than one representation.
        exact_by_id = {item[0].record_id: item for item in exact}
        ambiguous_by_id = {item[0].record_id: item for item in ambiguous if item[0].record_id not in exact_by_id}
        exact = list(exact_by_id.values())
        ambiguous = list(ambiguous_by_id.values())

        evidence: list[EvidenceRecord] = []
        for record, searched, basis, segment in exact:
            summary = " | ".join(x for x in (record.public_date, record.violation, record.penalty) if x)
            evidence.append(EvidenceRecord(
                field_name="environmental_violations", observed_value="Y",
                source_record_id=record.record_id, source_url=DATA_VIEW_URL,
                details={
                    "classification": "confirmed_mpca_enforcement",
                    "matched_party": segment, "source_party_text": record.party,
                    "matched_search_name": searched, "query_basis": basis,
                    "public_date": record.public_date, "location": record.location,
                    "violation": record.violation, "penalty": record.penalty,
                    "case_type": record.case_type, "summary": summary,
                    "dataset_sha256": self.dataset_sha256,
                    "master_field_proposal_allowed": True,
                },
            ))

        if exact:
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            identity = IdentityStatus.CONFIRMED
        elif ambiguous:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            identity = IdentityStatus.REVIEW_REQUIRED
            first = ambiguous[0]
            evidence.append(EvidenceRecord(
                field_name="environmental_violations", observed_value=None,
                source_record_id=first[0].record_id, source_url=DATA_VIEW_URL,
                details={
                    "classification": "possible_mpca_identity",
                    "candidate_count": len(ambiguous),
                    "matched_search_name": first[1], "candidate_party": first[3],
                    "name_similarity": first[4], "dataset_sha256": self.dataset_sha256,
                    "master_field_proposal_allowed": False,
                },
            ))
        else:
            status = SourceResultStatus.SUCCESS_NO_MATCH
            identity = IdentityStatus.NOT_EVALUATED

        warnings: list[str] = []
        if ambiguous and not exact:
            warnings.append("MPCA returned similar regulated-party names, but identity was not exact enough for an automatic environmental_violations proposal.")

        return self.validate_result(SourceResult(
            source_key=self.source_key, contractor_id=contractor.internal_id, status=status,
            identity_status=identity, completeness_status=CompletenessStatus.COMPLETE,
            identity_confidence=1.0 if exact else None, searched_name=contractor.contractor_name,
            searched_address=contractor.address_1, evidence=evidence, warnings=warnings,
            normalized_payload={
                "classification": "MATCH" if exact else ("AMBIGUOUS" if ambiguous else "NO_MATCH"),
                "approved_names_searched": [{"name": n, "basis": b} for n, b in approved],
                "confirmed_record_count": len(exact), "ambiguous_record_count": len(ambiguous),
                "dataset_record_count": len(self.records or []), "dataset_sha256": self.dataset_sha256,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "negative_semantics": "A clean no-match is research evidence only; it never proposes environmental_violations=N.",
            },
            source_record_id=exact[0][0].record_id if exact else None, source_url=DATA_VIEW_URL,
            acquisition_method="official_tableau_csv_export",
        ))
