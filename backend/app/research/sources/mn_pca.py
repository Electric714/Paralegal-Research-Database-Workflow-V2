from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from rapidfuzz import fuzz

from ..matching import normalize_company_name, normalize_text
from ..models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource
from .mn_pca_reports import (
    KNOWN_REPORT_URLS,
    MIN_TOTAL_REPORT_RECORDS,
    MpcaReportError,
    fetch_official_report_dataset,
)
from .public_browser import (
    BrowserBlockedError,
    BrowserFetchError,
    BrowserUnavailableError,
    PublicBrowserSession,
    STANDARD_BROWSER_HEADERS,
)


LANDING_URL = "https://www.pca.state.mn.us/trending-topics/compliance-and-enforcement"
DATA_VIEW_URL = "https://data.pca.state.mn.us/views/Enforcementactionswithpenalties/Complianceandenforcementnumberofcasesperyear"
CSV_EXPORT_URL = DATA_VIEW_URL + ".csv?:showVizHome=no"
TABLEAU_HOSTS = {"www.pca.state.mn.us", "data.pca.state.mn.us"}

TIMEOUT_SECONDS = 35.0
FUZZY_REVIEW_THRESHOLD = 94.0
TOKEN_SUBSET_REVIEW_THRESHOLD = 100.0
TOKEN_SUBSET_MIN_TOKENS = 2
MIN_PRODUCTION_RECORDS = 25
CACHE_MAX_AGE_SECONDS = 72 * 60 * 60

CHALLENGE_MARKERS = (
    b"radware captcha page",
    b"captcha.perfdrive.com",
    b"validate.perfdrive.com",
    b"hcaptcha",
    b"g-recaptcha",
    b"we apologize for the inconvenience",
)

NAME_HEADERS = (
    "company or individual(s)",
    "company or individual(s) (location)",
    "company or individual",
    "company or individuals",
    "company or individuals (location)",
    "regulated party",
    "regulated party name",
    "regulated party/company",
    "company",
    "facility name",
)
DATE_HEADERS = ("public date", "date", "closed date")
LOCATION_HEADERS = ("violation location", "location", "city")
VIOLATION_HEADERS = (
    "violation",
    "violation(s)",
    "violations",
    "violation description",
    "violation(s) description",
    "violation information",
    "category",
)
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


class MpcaAcquisitionError(RuntimeError):
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
        fingerprint = "|".join(
            normalize_text(x)
            for x in (party, public_date, location, violation, penalty, case_type)
        )
        record_id = "mn-pca:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        records.append(
            MpcaRecord(
                record_id=record_id,
                party=party,
                public_date=public_date,
                location=location,
                violation=violation,
                penalty=penalty,
                case_type=case_type,
                raw={str(k): str(v or "") for k, v in row.items() if k},
            )
        )

    return records, hashlib.sha256(raw_bytes).hexdigest()


def _approved_names(contractor: ContractorContext) -> list[tuple[str, str]]:
    values = [(contractor.contractor_name, "master_name")]
    values.extend(
        (x.strip(), "approved_alias")
        for x in re.split(r"[;|\n]+", contractor.related_companies or "")
        if x.strip()
    )
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, basis in values:
        key = normalize_company_name(value)
        if key and key not in seen:
            seen.add(key)
            result.append((value, basis))
    return result


def _party_segments(value: str) -> list[str]:
    parts = [x.strip() for x in re.split(r"[;\n]+|\s+/\s+", value) if x.strip()]
    return parts or [value.strip()]


def _should_review_name_variant(approved_name: str, candidate_name: str, wratio: float) -> bool:
    if wratio >= FUZZY_REVIEW_THRESHOLD:
        return True
    approved_tokens = approved_name.split()
    candidate_tokens = candidate_name.split()
    if min(len(approved_tokens), len(candidate_tokens)) < TOKEN_SUBSET_MIN_TOKENS:
        return False
    return fuzz.token_set_ratio(approved_name, candidate_name) >= TOKEN_SUBSET_REVIEW_THRESHOLD


def _default_app_data() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "ParalegalResearchDesk"
    return Path.home() / ".paralegal-research-desk"


def _default_profile_dir() -> str:
    return str(_default_app_data() / "browser" / "mn-pca")


def _default_cache_path() -> str:
    return str(_default_app_data() / "cache" / "mn-pca-enforcement.csv")


def _looks_like_challenge(content: bytes | str) -> bool:
    raw = content if isinstance(content, bytes) else content.encode("utf-8", errors="ignore")
    sample = raw[:262144].lower()
    return any(marker in sample for marker in CHALLENGE_MARKERS)


def _html_like(content_type: str, content: bytes) -> bool:
    lowered = content.lstrip().lower()
    return (
        "html" in (content_type or "").casefold()
        or lowered.startswith(b"<!doctype html")
        or lowered.startswith(b"<html")
    )


def _merge_records(*groups: list[MpcaRecord]) -> list[MpcaRecord]:
    merged: dict[str, MpcaRecord] = {}
    for group in groups:
        for record in group:
            merged.setdefault(record.record_id, record)
    return list(merged.values())


class MinnesotaPcaEnforcementSource(ResearchSource):
    source_key = "mn_pca"
    display_name = "Minnesota PCA Enforcement Actions"
    adapter_version = "1.4.0"
    parser_version = "1.0.2"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        browser_session_factory: Callable[..., PublicBrowserSession] = PublicBrowserSession,
        browser_profile_dir: str | None = None,
        cache_path: str | None = None,
        cache_max_age_seconds: int = CACHE_MAX_AGE_SECONDS,
        min_expected_records: int = MIN_PRODUCTION_RECORDS,
        allow_browser_fallback: bool = True,
        allow_cached_fallback: bool = True,
        allow_report_fallback: bool = True,
        report_urls: tuple[str, ...] | None = None,
        discover_report_urls: bool = True,
        report_min_records: int = MIN_TOTAL_REPORT_RECORDS,
    ) -> None:
        self.client = client or httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=dict(STANDARD_BROWSER_HEADERS),
        )
        self._browser_session_factory = browser_session_factory
        self._browser_profile_dir = browser_profile_dir or _default_profile_dir()
        self._cache_path = Path(cache_path or _default_cache_path())
        self._cache_max_age_seconds = max(0, int(cache_max_age_seconds))
        self._min_expected_records = max(1, int(min_expected_records))
        self._allow_browser_fallback = bool(allow_browser_fallback)
        self._allow_cached_fallback = bool(allow_cached_fallback)
        self._allow_report_fallback = bool(allow_report_fallback)
        self._report_urls = report_urls
        self._discover_report_urls = bool(discover_report_urls)
        self._report_min_records = max(1, int(report_min_records))

        self.records: list[MpcaRecord] | None = None
        self.dataset_sha256: str | None = None
        self.prepare_error: tuple[SourceResultStatus, str, int | None] | None = None
        self.acquisition_method: str | None = None
        self.dataset_is_cached = False
        self.dataset_is_partial_scope = False
        self.cache_age_seconds: float | None = None
        self.acquisition_warnings: list[str] = []
        self.report_scope_start: str | None = None
        self.report_scope_end: str | None = None
        self.report_page_count = 0
        self.report_source_urls: tuple[str, ...] = ()

    def health_check(self) -> dict:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "official_tableau_then_official_mpca_report_pages",
            "url": CSV_EXPORT_URL,
            "browser_fallback": "system_edge_or_chrome_with_persistent_profile",
            "official_report_fallback": True,
            "validated_cache_fallback": True,
            "rest_api_used": False,
        }

    def _validate_dataset(
        self,
        content: bytes | str,
        *,
        origin: str,
    ) -> tuple[list[MpcaRecord], str, bytes]:
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        if _looks_like_challenge(raw):
            raise MpcaAcquisitionError(
                f"MPCA {origin} returned an interactive security challenge instead of the enforcement dataset.",
                status=SourceResultStatus.BLOCKED,
                http_status=200,
            )
        try:
            records, digest = parse_mpca_csv(raw)
        except (UnicodeError, csv.Error, MpcaDatasetError) as exc:
            raise MpcaAcquisitionError(
                f"MPCA {origin} did not contain a valid enforcement-detail CSV: {exc}",
                status=SourceResultStatus.DATASET_MALFORMED,
                http_status=200,
            ) from exc
        if len(records) < self._min_expected_records:
            raise MpcaAcquisitionError(
                f"MPCA {origin} returned only {len(records)} validated detail rows; "
                f"expected at least {self._min_expected_records}. Refusing to treat a summary or partial export as complete.",
                status=SourceResultStatus.DATASET_MALFORMED,
                http_status=200,
            )
        return records, digest, raw

    def _response_bytes(self, response: httpx.Response, *, label: str) -> bytes:
        if response.status_code in {401, 403, 429}:
            raise MpcaAcquisitionError(
                f"MPCA {label} was blocked with HTTP {response.status_code}.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise MpcaAcquisitionError(
                f"MPCA {label} returned HTTP {response.status_code}.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )
        body = response.content
        if _looks_like_challenge(body):
            raise MpcaAcquisitionError(
                f"MPCA {label} returned a CAPTCHA/security challenge.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if _html_like(response.headers.get("content-type", ""), body) and label == "CSV export":
            raise MpcaAcquisitionError(
                "MPCA CSV export returned HTML instead of structured CSV.",
                status=SourceResultStatus.PARSER_FAILURE,
                http_status=response.status_code,
            )
        return body

    def _direct_tableau_export(self) -> tuple[list[MpcaRecord], str, bytes]:
        headers = dict(STANDARD_BROWSER_HEADERS)
        try:
            landing = self.client.get(LANDING_URL, headers=headers)
            self._response_bytes(landing, label="landing page")
            view = self.client.get(DATA_VIEW_URL, headers={**headers, "Referer": LANDING_URL})
            self._response_bytes(view, label="Tableau view")
            export = self.client.get(
                CSV_EXPORT_URL,
                headers={
                    **headers,
                    "Accept": "text/csv,text/plain;q=0.9,*/*;q=0.8",
                    "Referer": DATA_VIEW_URL,
                },
            )
            body = self._response_bytes(export, label="CSV export")
        except httpx.TimeoutException as exc:
            raise MpcaAcquisitionError(
                "MPCA direct Tableau acquisition timed out.",
                status=SourceResultStatus.TIMEOUT,
            ) from exc
        except httpx.HTTPError as exc:
            raise MpcaAcquisitionError(
                f"MPCA direct Tableau acquisition failed: {exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            ) from exc
        return self._validate_dataset(body, origin="direct Tableau export")

    def _browser_tableau_export(
        self,
        *,
        headless: bool,
        profile_dir: str | None,
        method: str,
    ) -> tuple[list[MpcaRecord], str, bytes]:
        session: PublicBrowserSession | None = None
        try:
            session = self._browser_session_factory(
                allowed_hosts=TABLEAU_HOSTS,
                warmup_url=LANDING_URL,
                timeout_ms=60_000,
                headless=headless,
                profile_dir=profile_dir,
                blocked_retry_wait_ms=5_000 if not headless else 2_000,
            )
            session.get_document(DATA_VIEW_URL)
            fetched = session.get_resource(CSV_EXPORT_URL)
            raw = fetched.text.encode("utf-8-sig" if fetched.text.startswith("\ufeff") else "utf-8")
            return self._validate_dataset(raw, origin=method)
        except BrowserBlockedError as exc:
            raise MpcaAcquisitionError(
                f"MPCA {method} encountered an interactive security challenge: {exc}",
                status=SourceResultStatus.BLOCKED,
                http_status=403,
            ) from exc
        except BrowserUnavailableError as exc:
            raise MpcaAcquisitionError(
                f"MPCA {method} could not start the local Edge/Chrome browser: {exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            ) from exc
        except BrowserFetchError as exc:
            raise MpcaAcquisitionError(
                f"MPCA {method} browser acquisition failed: {exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            ) from exc
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _write_cache(self, raw: bytes) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            temp_path.write_bytes(raw)
            temp_path.replace(self._cache_path)
        except OSError:
            pass

    def _read_recent_cache(self) -> tuple[list[MpcaRecord], str, bytes, float] | None:
        try:
            stat = self._cache_path.stat()
            age = max(0.0, time.time() - stat.st_mtime)
            if age > self._cache_max_age_seconds:
                return None
            raw = self._cache_path.read_bytes()
            records, digest, validated_raw = self._validate_dataset(raw, origin="validated local cache")
            return records, digest, validated_raw, age
        except (OSError, MpcaAcquisitionError):
            return None

    def _official_report_fallback(self) -> tuple[list[MpcaRecord], str]:
        try:
            dataset = fetch_official_report_dataset(
                self.client,
                report_urls=self._report_urls,
                discover=self._discover_report_urls,
                min_total_records=self._report_min_records,
            )
            records, digest = parse_mpca_csv(dataset.csv_bytes)
        except (httpx.HTTPError, MpcaReportError, UnicodeError, csv.Error, MpcaDatasetError) as exc:
            raise MpcaAcquisitionError(
                f"Official MPCA report-page fallback failed: {exc}",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            ) from exc

        self.report_scope_start = dataset.scope_start
        self.report_scope_end = dataset.scope_end
        self.report_page_count = dataset.page_count
        self.report_source_urls = dataset.source_urls
        self.acquisition_warnings.extend(dataset.warnings)
        return records, digest

    @staticmethod
    def _pick_failure(errors: list[MpcaAcquisitionError]) -> MpcaAcquisitionError:
        if not errors:
            return MpcaAcquisitionError("MPCA acquisition did not run.")
        priority = {
            SourceResultStatus.BLOCKED: 6,
            SourceResultStatus.DATASET_MALFORMED: 5,
            SourceResultStatus.PARSER_FAILURE: 5,
            SourceResultStatus.TIMEOUT: 4,
            SourceResultStatus.HTTP_ERROR: 3,
            SourceResultStatus.SOURCE_UNAVAILABLE: 2,
        }
        return max(errors, key=lambda exc: priority.get(exc.status, 1))

    def prepare(self) -> None:
        if self.records is not None or self.prepare_error is not None:
            return

        errors: list[MpcaAcquisitionError] = []
        live_attempts: list[tuple[str, Callable[[], tuple[list[MpcaRecord], str, bytes]]]] = [
            ("official_tableau_direct_csv", self._direct_tableau_export),
        ]
        if self._allow_browser_fallback:
            live_attempts.extend(
                [
                    (
                        "official_tableau_headless_browser_csv",
                        lambda: self._browser_tableau_export(
                            headless=True,
                            profile_dir=None,
                            method="headless browser-context Tableau export",
                        ),
                    ),
                    (
                        "official_tableau_persistent_browser_csv",
                        lambda: self._browser_tableau_export(
                            headless=False,
                            profile_dir=self._browser_profile_dir,
                            method="persistent system-browser Tableau export",
                        ),
                    ),
                ]
            )

        for method, acquire in live_attempts:
            try:
                records, digest, raw = acquire()
                self.records = records
                self.dataset_sha256 = digest
                self.acquisition_method = method
                self.dataset_is_cached = False
                self.dataset_is_partial_scope = False
                self.cache_age_seconds = None
                if errors:
                    self.acquisition_warnings = [
                        f"Earlier MPCA acquisition attempt failed closed: {exc}" for exc in errors
                    ]
                self._write_cache(raw)
                return
            except MpcaAcquisitionError as exc:
                errors.append(exc)

        report_records: list[MpcaRecord] | None = None
        report_digest: str | None = None
        if self._allow_report_fallback:
            try:
                report_records, report_digest = self._official_report_fallback()
            except MpcaAcquisitionError as exc:
                errors.append(exc)

        cached = self._read_recent_cache() if self._allow_cached_fallback else None

        if report_records is not None:
            if cached is not None:
                cache_records, _cache_digest, _raw, age = cached
                self.records = _merge_records(report_records, cache_records)
                self.dataset_sha256 = hashlib.sha256(
                    "|".join(sorted(record.record_id for record in self.records)).encode("utf-8")
                ).hexdigest()
                self.acquisition_method = "official_mpca_reports_plus_recent_tableau_cache"
                self.dataset_is_cached = True
                self.cache_age_seconds = age
                self.acquisition_warnings.append(
                    "A recent validated Tableau cache was merged with fresh official MPCA report pages to broaden historical coverage. "
                    "Because the live complete Tableau extract could not be refreshed, no-match results remain partial."
                )
            else:
                self.records = report_records
                self.dataset_sha256 = report_digest
                self.acquisition_method = "official_mpca_enforcement_report_pages"
                self.dataset_is_cached = False
                self.cache_age_seconds = None
            self.dataset_is_partial_scope = True
            self.acquisition_warnings.extend(
                f"Tableau acquisition failed closed before report fallback: {exc}" for exc in errors
                if "report-page fallback" not in str(exc).casefold()
            )
            return

        if cached is not None:
            records, digest, _raw, age = cached
            self.records = records
            self.dataset_sha256 = digest
            self.acquisition_method = "validated_recent_tableau_cache"
            self.dataset_is_cached = True
            self.dataset_is_partial_scope = True
            self.cache_age_seconds = age
            self.acquisition_warnings = [
                "Live MPCA acquisition failed; using a recent previously validated full Tableau extract. "
                "Cached results are partial evidence only and cannot create a clean negative."
            ] + [f"Live acquisition failure: {exc}" for exc in errors]
            return

        failure = self._pick_failure(errors)
        details = " | ".join(str(exc) for exc in errors)
        message = str(failure)
        if details and details != message:
            message = f"{message} Attempts: {details}"
        self.prepare_error = (failure.status, message, failure.http_status)

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
                    normalized_payload={
                        "rest_api_used": False,
                        "negative_semantics": "Acquisition failure can never become a clean negative.",
                    },
                    source_url=DATA_VIEW_URL,
                    http_status=http_status,
                    acquisition_method="official_mpca_acquisition_failed",
                )
            )

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

        exact_by_id = {item[0].record_id: item for item in exact}
        ambiguous_by_id = {
            item[0].record_id: item
            for item in ambiguous
            if item[0].record_id not in exact_by_id
        }
        exact = list(exact_by_id.values())
        ambiguous = list(ambiguous_by_id.values())

        evidence: list[EvidenceRecord] = []
        for record, searched, basis, segment in exact:
            record_url = record.raw.get("Source URL") or DATA_VIEW_URL
            summary = " | ".join(
                x for x in (record.public_date, record.violation, record.penalty) if x
            )
            evidence.append(
                EvidenceRecord(
                    field_name="environmental_violations",
                    observed_value="Y",
                    source_record_id=record.record_id,
                    source_url=record_url,
                    details={
                        "classification": "confirmed_mpca_enforcement",
                        "matched_party": segment,
                        "source_party_text": record.party,
                        "matched_search_name": searched,
                        "query_basis": basis,
                        "public_date": record.public_date,
                        "location": record.location,
                        "violation": record.violation,
                        "penalty": record.penalty,
                        "case_type": record.case_type,
                        "summary": summary,
                        "dataset_sha256": self.dataset_sha256,
                        "dataset_cached": self.dataset_is_cached,
                        "dataset_partial_scope": self.dataset_is_partial_scope,
                        "master_field_proposal_allowed": True,
                    },
                )
            )

        partial_dataset = self.dataset_is_cached or self.dataset_is_partial_scope

        if exact:
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            identity = IdentityStatus.CONFIRMED
        elif ambiguous:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            identity = IdentityStatus.REVIEW_REQUIRED
            first = ambiguous[0]
            evidence.append(
                EvidenceRecord(
                    field_name="environmental_violations",
                    observed_value=None,
                    source_record_id=first[0].record_id,
                    source_url=first[0].raw.get("Source URL") or DATA_VIEW_URL,
                    details={
                        "classification": "possible_mpca_identity",
                        "candidate_count": len(ambiguous),
                        "matched_search_name": first[1],
                        "candidate_party": first[3],
                        "name_similarity": first[4],
                        "dataset_sha256": self.dataset_sha256,
                        "dataset_cached": self.dataset_is_cached,
                        "dataset_partial_scope": self.dataset_is_partial_scope,
                        "master_field_proposal_allowed": False,
                    },
                )
            )
        elif partial_dataset:
            status = SourceResultStatus.PARTIAL_RESULTS
            identity = IdentityStatus.NOT_EVALUATED
        else:
            status = SourceResultStatus.SUCCESS_NO_MATCH
            identity = IdentityStatus.NOT_EVALUATED

        completeness = CompletenessStatus.PARTIAL if partial_dataset else CompletenessStatus.COMPLETE
        warnings = list(self.acquisition_warnings)
        if ambiguous and not exact:
            warnings.append(
                "MPCA returned similar regulated-party names, but identity was not exact enough for an automatic environmental_violations proposal."
            )
        if partial_dataset and not exact:
            warnings.append(
                "No bidder match was found in the available MPCA evidence, but the live all-history Tableau extract was unavailable; this is not a clean negative."
            )

        source_url = self.report_source_urls[0] if self.report_source_urls else DATA_VIEW_URL
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=status,
                identity_status=identity,
                completeness_status=completeness,
                identity_confidence=1.0 if exact else None,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=evidence,
                warnings=warnings,
                normalized_payload={
                    "classification": (
                        "MATCH"
                        if exact
                        else "AMBIGUOUS"
                        if ambiguous
                        else "PARTIAL_NO_MATCH"
                        if partial_dataset
                        else "NO_MATCH"
                    ),
                    "approved_names_searched": [
                        {"name": name, "basis": basis} for name, basis in approved
                    ],
                    "confirmed_record_count": len(exact),
                    "ambiguous_record_count": len(ambiguous),
                    "dataset_record_count": len(self.records or []),
                    "dataset_sha256": self.dataset_sha256,
                    "dataset_cached": self.dataset_is_cached,
                    "dataset_partial_scope": self.dataset_is_partial_scope,
                    "cache_age_seconds": self.cache_age_seconds,
                    "report_page_count": self.report_page_count,
                    "report_scope_start": self.report_scope_start,
                    "report_scope_end": self.report_scope_end,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "rest_api_used": False,
                    "negative_semantics": (
                        "Only a live validated complete Tableau extract can support SUCCESS_NO_MATCH. "
                        "Official report pages and cached data can support findings and review candidates, but no-match results remain PARTIAL_RESULTS and never propose environmental_violations=N."
                    ),
                },
                source_record_id=exact[0][0].record_id if exact else None,
                source_url=source_url,
                acquisition_method=self.acquisition_method,
            )
        )
