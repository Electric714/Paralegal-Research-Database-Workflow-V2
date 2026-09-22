from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from rapidfuzz import fuzz, process

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


SAM_PUBLIC_SEARCH = "https://sam.gov/entity-information"
MAX_EXTRACT_BYTES = 150 * 1024 * 1024
MAX_UNCOMPRESSED_CSV_BYTES = 250 * 1024 * 1024
MAX_FRESH_AGE_DAYS = 2


class SamExtractError(ValueError):
    pass


class SamDownloadError(RuntimeError):
    """Compatibility error type for source preparation failures.

    SAM research is upload-only in V2. The name is retained because other modules
    already import it, but the runtime adapter never performs SAM API downloads.
    """

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


@dataclass(frozen=True)
class SamExclusionRecord:
    classification: str
    name: str
    address_1: str
    city: str
    state: str
    zip_code: str
    uei_sam: str
    exclusion_program: str
    excluding_agency: str
    ct_code: str
    exclusion_type: str
    additional_comments: str
    active_date: str
    termination_date: str
    record_status: str
    cross_reference: str
    sam_number: str
    cage: str
    npi: str
    creation_date: str

    @property
    def record_id(self) -> str:
        if self.sam_number:
            return self.sam_number
        base = "|".join(
            [self.uei_sam, self.name, self.exclusion_type, self.active_date, self.excluding_agency]
        )
        return "sam-" + hashlib.sha256(
            base.encode("utf-8", errors="replace")
        ).hexdigest()[:20]

    @property
    def identity_key(self) -> tuple[str, str, str, str]:
        return (
            normalize_company_name(self.name),
            normalize_text(self.address_1),
            normalize_text(self.city),
            normalize_text(self.state),
        )

    def public_details(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "name": self.name,
            "address_1": self.address_1,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "uei_sam": self.uei_sam,
            "exclusion_program": self.exclusion_program,
            "excluding_agency": self.excluding_agency,
            "ct_code": self.ct_code,
            "exclusion_type": self.exclusion_type,
            "additional_comments": self.additional_comments,
            "active_date": self.active_date,
            "termination_date": self.termination_date,
            "record_status": self.record_status,
            "cross_reference": self.cross_reference,
            "sam_number": self.sam_number,
            "cage": self.cage,
            "npi": self.npi,
            "creation_date": self.creation_date,
        }


@dataclass(frozen=True)
class SamDataset:
    records: tuple[SamExclusionRecord, ...]
    path: Path
    sha256: str
    extract_date: date | None
    csv_name: str
    source: str

    @property
    def age_days(self) -> int | None:
        if not self.extract_date:
            return None
        return (datetime.now(timezone.utc).date() - self.extract_date).days


@dataclass(frozen=True)
class CandidateMatch:
    record: SamExclusionRecord
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    matched_search_name: str
    matched_record_name: str
    auto_confirmable: bool
    remembered_judgment: str | None = None

    def as_dict(self) -> dict:
        return {
            "source_record_id": self.record.record_id,
            "record": self.record.public_details(),
            "score": self.score,
            "name_score": self.name_score,
            "address_score": self.address_score,
            "city_score": self.city_score,
            "state_score": self.state_score,
            "matched_search_name": self.matched_search_name,
            "matched_record_name": self.matched_record_name,
            "auto_confirmable": self.auto_confirmable,
            "remembered_judgment": self.remembered_judgment,
        }


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


HEADER_MAP = {
    "classification": "classification",
    "name": "name",
    "address1": "address_1",
    "city": "city",
    "stateprovince": "state",
    "state": "state",
    "zipcode": "zip_code",
    "zippostalcode": "zip_code",
    "uniqueentityidentifiersam": "uei_sam",
    "uei": "uei_sam",
    "exclusionprogram": "exclusion_program",
    "excludingagency": "excluding_agency",
    "ctcode": "ct_code",
    "exclusiontype": "exclusion_type",
    "additionalcomments": "additional_comments",
    "activedate": "active_date",
    "terminationdate": "termination_date",
    "recordstatus": "record_status",
    "crossreference": "cross_reference",
    "samnumber": "sam_number",
    "cage": "cage",
    "cagecode": "cage",
    "npi": "npi",
    "creationdate": "creation_date",
}

REQUIRED_SEMANTIC_FIELDS = {
    "classification",
    "name",
    "exclusion_type",
    "active_date",
    "sam_number",
}


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SamExtractError("SAM exclusion CSV could not be decoded.")


def _safe_zip_csv(data: bytes) -> tuple[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            candidates = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".csv")
            ]
            if not candidates:
                raise SamExtractError("SAM ZIP does not contain a CSV file.")
            candidates.sort(
                key=lambda info: (
                    "exclusion" not in info.filename.casefold(),
                    info.filename.casefold(),
                )
            )
            info = candidates[0]
            if info.file_size > MAX_UNCOMPRESSED_CSV_BYTES:
                raise SamExtractError("SAM exclusion CSV is unexpectedly large.")
            return Path(info.filename).name, archive.read(info)
    except zipfile.BadZipFile as exc:
        raise SamExtractError("SAM extract is not a valid ZIP file.") from exc


def extract_csv_bytes(data: bytes, filename: str) -> tuple[str, bytes]:
    if len(data) > MAX_EXTRACT_BYTES:
        raise SamExtractError("SAM extract exceeds the configured size limit.")
    lower = filename.casefold()
    if data.startswith(b"PK\x03\x04") or lower.endswith(".zip"):
        return _safe_zip_csv(data)
    if lower.endswith(".csv"):
        return Path(filename).name, data
    raise SamExtractError("SAM extract must be an official .ZIP or .CSV exclusions extract.")


def _mapped_headers(fieldnames: Iterable[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for original in fieldnames:
        semantic = HEADER_MAP.get(_header_key(original))
        if semantic and semantic not in mapped:
            mapped[semantic] = original
    missing = sorted(REQUIRED_SEMANTIC_FIELDS - set(mapped))
    if missing:
        raise SamExtractError(
            "SAM exclusions layout is missing required field(s): " + ", ".join(missing)
        )
    return mapped


def parse_sam_exclusions(
    data: bytes,
    filename: str,
) -> tuple[str, list[SamExclusionRecord]]:
    csv_name, csv_bytes = extract_csv_bytes(data, filename)
    text = _decode_csv(csv_bytes)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise SamExtractError("SAM exclusions CSV has no header row.")

    mapped = _mapped_headers([str(value or "") for value in reader.fieldnames])
    records: list[SamExclusionRecord] = []

    for raw in reader:

        def value(field: str) -> str:
            original = mapped.get(field)
            return (raw.get(original, "") if original else "") or ""

        classification = value("classification").strip()
        if normalize_text(classification) != "firm":
            continue

        name = value("name").strip()
        if not name:
            continue

        records.append(
            SamExclusionRecord(
                classification=classification,
                name=name,
                address_1=value("address_1").strip(),
                city=value("city").strip(),
                state=value("state").strip(),
                zip_code=value("zip_code").strip(),
                uei_sam=value("uei_sam").strip(),
                exclusion_program=value("exclusion_program").strip(),
                excluding_agency=value("excluding_agency").strip(),
                ct_code=value("ct_code").strip(),
                exclusion_type=value("exclusion_type").strip(),
                additional_comments=value("additional_comments").strip(),
                active_date=value("active_date").strip(),
                termination_date=value("termination_date").strip(),
                record_status=value("record_status").strip(),
                cross_reference=value("cross_reference").strip(),
                sam_number=value("sam_number").strip(),
                cage=value("cage").strip(),
                npi=value("npi").strip(),
                creation_date=value("creation_date").strip(),
            )
        )

    if not records:
        raise SamExtractError("SAM exclusions extract contains no Firm records.")
    return csv_name, records


def parse_extract_date(filename: str) -> date | None:
    match = re.search(
        r"(?:_|\b)(\d{5})(?=\.(?:zip|csv)$|\b)",
        Path(filename).name,
        re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%y%j").date()
    except ValueError:
        return None


def _cache_dir() -> Path:
    return db.DATA_DIR / "source_cache" / "sam"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(db.BASE_DIR.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def store_uploaded_extract(
    data: bytes,
    filename: str,
    *,
    cache_dir: Path | None = None,
) -> SamDataset:
    """Store an uploaded extract without overwriting prior evidence bytes."""

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name) or "sam_exclusions.zip"
    csv_name, records = parse_sam_exclusions(data, safe_name)
    digest = _sha256(data)

    target_dir = cache_dir or _cache_dir()
    version_dir = target_dir / digest[:16]
    version_dir.mkdir(parents=True, exist_ok=True)
    target = version_dir / safe_name

    if not target.exists():
        target.write_bytes(data)
    else:
        existing = target.read_bytes()
        if _sha256(existing) != digest:
            raise SamExtractError(
                "SAM cache collision detected; refusing to overwrite existing evidence bytes."
            )

    return SamDataset(
        records=tuple(records),
        path=target,
        sha256=digest,
        extract_date=parse_extract_date(safe_name) or parse_extract_date(csv_name),
        csv_name=csv_name,
        source="manual_upload",
    )


def _load_dataset_from_path(path: Path) -> SamDataset | None:
    try:
        data = path.read_bytes()
        csv_name, records = parse_sam_exclusions(data, path.name)
        return SamDataset(
            records=tuple(records),
            path=path,
            sha256=_sha256(data),
            extract_date=parse_extract_date(path.name) or parse_extract_date(csv_name),
            csv_name=csv_name,
            source="cache",
        )
    except (OSError, SamExtractError):
        return None


def load_cached_extract(*, cache_dir: Path | None = None) -> SamDataset | None:
    """Load the newest valid official extract date, not merely the newest upload."""

    directory = cache_dir or _cache_dir()
    if not directory.exists():
        return None

    paths = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".zip", ".csv"}
    ]

    loaded: list[tuple[SamDataset, float]] = []
    for path in paths:
        dataset = _load_dataset_from_path(path)
        if dataset is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        loaded.append((dataset, mtime))

    if not loaded:
        return None

    dated = [item for item in loaded if item[0].extract_date is not None]
    pool = dated or loaded
    dataset, _mtime = max(
        pool,
        key=lambda item: (
            item[0].extract_date or date.min,
            item[1],
            item[0].sha256,
        ),
    )
    return dataset


def _related_names(contractor: ContractorContext) -> list[str]:
    names = [contractor.contractor_name]
    if contractor.related_companies.strip():
        names.extend(
            part.strip()
            for part in re.split(r"[;|\n]+", contractor.related_companies)
            if part.strip()
        )

    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        normalized = normalize_company_name(name)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(name)
    return result


def _record_names(record: SamExclusionRecord) -> list[str]:
    names = [record.name]
    if record.cross_reference:
        names.extend(
            part.strip()
            for part in re.split(r"[;|\n]+", record.cross_reference)
            if part.strip()
        )
    return names


def _remembered_judgment(bidder_id: int, record_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT judgment FROM identity_judgments
            WHERE bidder_id=? AND source_key='sam' AND source_record_id=?
            """,
            (bidder_id, record_id),
        ).fetchone()
        return str(row["judgment"]) if row else None


class SamExclusionsSource(ResearchSource):
    """Shared SAM exclusions parser/search base for the upload-only V2 adapter."""

    source_key = "sam"
    display_name = "SAM.gov Federal Exclusions"
    adapter_version = "1.2.0"
    parser_version = "1.1.0"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        today: date | None = None,
    ) -> None:
        _ = api_key
        self.cache_dir = cache_dir
        self.today = today
        self.dataset: SamDataset | None = None
        self.prepare_failure: SamDownloadError | SamExtractError | None = None
        self.prepare_warnings: list[str] = []
        self._name_choices_global: list[str] = []
        self._name_choices_by_state: dict[str, list[str]] = {}
        self._name_to_records: dict[str, list[SamExclusionRecord]] = {}

    def _dataset_age_days(self) -> int | None:
        if not self.dataset or not self.dataset.extract_date:
            return None
        reference = self.today or datetime.now(timezone.utc).date()
        return (reference - self.dataset.extract_date).days

    @property
    def dataset_is_fresh(self) -> bool:
        age = self._dataset_age_days()
        return age is not None and 0 <= age <= MAX_FRESH_AGE_DAYS

    def health_check(self) -> dict:
        cached = load_cached_extract(cache_dir=self.cache_dir)
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "manual_upload",
            "cached_extract": cached.path.name if cached else None,
            "cached_extract_date": (
                cached.extract_date.isoformat()
                if cached and cached.extract_date
                else None
            ),
            "cached_record_count": len(cached.records) if cached else 0,
        }

    def prepare(self) -> None:
        self.prepare_failure = None
        self.prepare_warnings = []
        self.dataset = load_cached_extract(cache_dir=self.cache_dir)

        if self.dataset is None:
            self.prepare_failure = SamDownloadError(
                "No SAM exclusions extract has been uploaded. Upload the official SAM Public Exclusions V2 CSV or ZIP before running SAM research.",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            )
            self._name_choices_global = []
            self._name_choices_by_state = {}
            self._name_to_records = {}
            return

        self._build_index(self.dataset.records)
        if not self.dataset.extract_date:
            self.prepare_warnings.append(
                "The SAM extract date could not be determined from the uploaded file name; no-match results are treated as partial."
            )
        elif not self.dataset_is_fresh:
            self.prepare_warnings.append(
                f"SAM extract {self.dataset.path.name} is {self._dataset_age_days()} days old; results are treated as partial until a current extract is uploaded."
            )

    def _build_index(self, records: Iterable[SamExclusionRecord]) -> None:
        name_to_records: dict[str, list[SamExclusionRecord]] = {}
        states: dict[str, set[str]] = {}
        global_names: set[str] = set()

        for record in records:
            state = normalize_text(record.state)
            for source_name in _record_names(record):
                normalized = normalize_company_name(source_name)
                if not normalized:
                    continue
                name_to_records.setdefault(normalized, []).append(record)
                global_names.add(normalized)
                if state:
                    states.setdefault(state, set()).add(normalized)

        self._name_to_records = name_to_records
        self._name_choices_global = sorted(global_names)
        self._name_choices_by_state = {
            key: sorted(values) for key, values in states.items()
        }

    def _candidate_records(
        self,
        contractor: ContractorContext,
    ) -> list[SamExclusionRecord]:
        search_names = _related_names(contractor)
        record_ids: set[str] = set()
        result: list[SamExclusionRecord] = []

        def add_name(normalized_name: str) -> None:
            for record in self._name_to_records.get(normalized_name, []):
                if record.record_id not in record_ids:
                    record_ids.add(record.record_id)
                    result.append(record)

        for name in search_names:
            add_name(normalize_company_name(name))

        state = normalize_text(contractor.state)
        choices = self._name_choices_by_state.get(state) or self._name_choices_global

        for search_name in search_names:
            normalized = normalize_company_name(search_name)
            if not normalized or not choices:
                continue
            for choice, similarity, _index in process.extract(
                normalized,
                choices,
                scorer=fuzz.WRatio,
                limit=20,
                score_cutoff=72,
            ):
                if similarity >= 72:
                    add_name(choice)

        if not result and state and self._name_choices_global:
            for search_name in search_names:
                normalized = normalize_company_name(search_name)
                for choice, similarity, _index in process.extract(
                    normalized,
                    self._name_choices_global,
                    scorer=fuzz.WRatio,
                    limit=10,
                    score_cutoff=80,
                ):
                    if similarity >= 80:
                        add_name(choice)

        return result

    def _score_record(
        self,
        contractor: ContractorContext,
        record: SamExclusionRecord,
    ) -> CandidateMatch:
        best = None

        for master_name in _related_names(contractor):
            for candidate_name in _record_names(record):
                score = score_candidate(
                    master_name=master_name,
                    candidate_name=candidate_name,
                    master_address=contractor.address_1,
                    candidate_address=record.address_1,
                    master_city=contractor.city,
                    candidate_city=record.city,
                    master_state=contractor.state,
                    candidate_state=record.state,
                )
                if best is None or score.score > best[0].score:
                    best = (score, master_name, candidate_name)

        assert best is not None
        score, matched_search_name, matched_record_name = best

        zip_match = (
            bool(contractor.zip and record.zip_code)
            and re.sub(r"\D", "", contractor.zip)[:5]
            == re.sub(r"\D", "", record.zip_code)[:5]
        )
        has_location_confirmation = (
            score.address_score >= 0.90
            or zip_match
            or (
                score.city_score == 1.0
                and score.state_score == 1.0
                and bool(contractor.city and contractor.state)
            )
        )
        auto_confirmable = score.name_score >= 0.97 and has_location_confirmation
        judgment = _remembered_judgment(
            contractor.internal_id,
            record.record_id,
        )

        return CandidateMatch(
            record=record,
            score=score.score,
            name_score=score.name_score,
            address_score=score.address_score,
            city_score=score.city_score,
            state_score=score.state_score,
            matched_search_name=matched_search_name,
            matched_record_name=matched_record_name,
            auto_confirmable=auto_confirmable,
            remembered_judgment=judgment,
        )

    def _failure_result(self, contractor: ContractorContext) -> SourceResult:
        failure = self.prepare_failure
        status = (
            failure.status
            if isinstance(failure, SamDownloadError)
            else SourceResultStatus.DATASET_MALFORMED
        )
        http_status = (
            failure.http_status if isinstance(failure, SamDownloadError) else None
        )

        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=status,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.UNKNOWN,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=[str(failure)] if failure else ["SAM source was not prepared."],
                source_url=SAM_PUBLIC_SEARCH,
                http_status=http_status,
                acquisition_method="sam_public_exclusions_v2_extract",
            )
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        if self.dataset is None and self.prepare_failure is None:
            self.prepare()
        if self.dataset is None:
            return self._failure_result(contractor)

        candidates = [
            self._score_record(contractor, record)
            for record in self._candidate_records(contractor)
        ]
        candidates = [
            item
            for item in candidates
            if item.score >= 0.72
            and item.remembered_judgment != "DIFFERENT_ENTITY"
        ]
        candidates.sort(key=lambda item: item.score, reverse=True)

        fresh = self.dataset_is_fresh
        completeness = (
            CompletenessStatus.COMPLETE
            if fresh
            else CompletenessStatus.PARTIAL
        )

        artifact = RawArtifact(
            artifact_type="sam_exclusions_extract",
            relative_path=_artifact_relative(self.dataset.path),
            sha256=self.dataset.sha256,
            mime_type=(
                "application/zip"
                if self.dataset.path.suffix.casefold() == ".zip"
                else "text/csv"
            ),
            metadata={
                "filename": self.dataset.path.name,
                "csv_name": self.dataset.csv_name,
                "extract_date": (
                    self.dataset.extract_date.isoformat()
                    if self.dataset.extract_date
                    else None
                ),
                "record_count": len(self.dataset.records),
                "source": self.dataset.source,
            },
        )

        common_payload = {
            "dataset": artifact.metadata,
            "candidate_count": len(candidates),
            "top_candidates": [
                candidate.as_dict() for candidate in candidates[:5]
            ],
        }
        warnings = list(self.prepare_warnings)

        remembered_same = [
            item
            for item in candidates
            if item.remembered_judgment == "SAME_ENTITY"
        ]
        strong = [item for item in candidates if item.auto_confirmable]
        confirmed_pool = remembered_same or strong

        if confirmed_pool:
            identity_keys = {
                item.record.identity_key for item in confirmed_pool
            }
            if len(identity_keys) > 1 and not remembered_same:
                return self.validate_result(
                    SourceResult(
                        source_key=self.source_key,
                        contractor_id=contractor.internal_id,
                        status=(
                            SourceResultStatus.AMBIGUOUS_MATCH
                            if fresh
                            else SourceResultStatus.PARTIAL_RESULTS
                        ),
                        identity_status=IdentityStatus.REVIEW_REQUIRED,
                        completeness_status=completeness,
                        identity_confidence=confirmed_pool[0].score,
                        searched_name=contractor.contractor_name,
                        searched_address=contractor.address_1,
                        warnings=warnings
                        + [
                            "Multiple strong SAM records appear to represent different identities."
                        ],
                        artifacts=[artifact],
                        normalized_payload=common_payload,
                        source_url=SAM_PUBLIC_SEARCH,
                        acquisition_method="sam_public_exclusions_v2_extract",
                    )
                )

            primary = confirmed_pool[0]
            matching_identity = [
                item
                for item in candidates
                if item.record.identity_key == primary.record.identity_key
            ]
            match_details = [
                item.as_dict() for item in matching_identity
            ]

            evidence = EvidenceRecord(
                field_name="state_federal_debarment",
                observed_value="Y",
                source_record_id=primary.record.record_id,
                source_url=SAM_PUBLIC_SEARCH,
                details={
                    "basis": "Active federal exclusion in SAM.gov Public Exclusions V2 extract",
                    "active_exclusion_count": len(matching_identity),
                    "matches": match_details,
                    "dataset_extract_date": (
                        self.dataset.extract_date.isoformat()
                        if self.dataset.extract_date
                        else None
                    ),
                },
            )

            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=(
                        SourceResultStatus.SUCCESS_WITH_FINDINGS
                        if fresh
                        else SourceResultStatus.PARTIAL_RESULTS
                    ),
                    identity_status=IdentityStatus.CONFIRMED,
                    completeness_status=completeness,
                    identity_confidence=primary.score,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    evidence=[evidence],
                    warnings=warnings,
                    artifacts=[artifact],
                    normalized_payload={
                        **common_payload,
                        "confirmed_matches": match_details,
                    },
                    source_record_id=primary.record.record_id,
                    source_url=SAM_PUBLIC_SEARCH,
                    acquisition_method="sam_public_exclusions_v2_extract",
                )
            )

        medium = [
            item
            for item in candidates
            if item.score >= 0.78 and item.name_score >= 0.75
        ]
        if medium:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=(
                        SourceResultStatus.AMBIGUOUS_MATCH
                        if fresh
                        else SourceResultStatus.PARTIAL_RESULTS
                    ),
                    identity_status=IdentityStatus.REVIEW_REQUIRED,
                    completeness_status=completeness,
                    identity_confidence=medium[0].score,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings
                    + [
                        "SAM returned a possible company match that is not strong enough to auto-confirm."
                    ],
                    artifacts=[artifact],
                    normalized_payload=common_payload,
                    source_url=SAM_PUBLIC_SEARCH,
                    acquisition_method="sam_public_exclusions_v2_extract",
                )
            )

        if not fresh:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.PARTIAL_RESULTS,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.PARTIAL,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings
                    + [
                        "No match was found, but the SAM extract is not current enough for a clean negative result."
                    ],
                    artifacts=[artifact],
                    normalized_payload=common_payload,
                    source_url=SAM_PUBLIC_SEARCH,
                    acquisition_method="sam_public_exclusions_v2_extract",
                )
            )

        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.SUCCESS_NO_MATCH,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.COMPLETE,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=warnings,
                artifacts=[artifact],
                normalized_payload=common_payload,
                source_url=SAM_PUBLIC_SEARCH,
                acquisition_method="sam_public_exclusions_v2_extract",
            )
        )
