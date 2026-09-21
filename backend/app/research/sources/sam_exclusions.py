from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
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


SAM_EXTRACT_API = "https://api.sam.gov/data-services/v1/extracts"
SAM_PUBLIC_SEARCH = "https://sam.gov/entity-information"
MAX_EXTRACT_BYTES = 150 * 1024 * 1024
MAX_UNCOMPRESSED_CSV_BYTES = 250 * 1024 * 1024
MAX_FRESH_AGE_DAYS = 2


class SamExtractError(ValueError):
    pass


class SamDownloadError(RuntimeError):
    def __init__(self, message: str, *, status: SourceResultStatus, http_status: int | None = None):
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
        return "sam-" + hashlib.sha256(base.encode("utf-8", errors="replace")).hexdigest()[:20]

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
                info for info in archive.infolist()
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


def parse_sam_exclusions(data: bytes, filename: str) -> tuple[str, list[SamExclusionRecord]]:
    csv_name, csv_bytes = extract_csv_bytes(data, filename)
    text = _decode_csv(csv_bytes)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise SamExtractError("SAM exclusions CSV has no header row.")
    # Preserve the exact DictReader keys for row lookup. Header normalization is
    # used only to map the official labels to semantic fields.
    mapped = _mapped_headers([str(value or "") for value in reader.fieldnames])

    records: list[SamExclusionRecord] = []
    for raw in reader:
        def value(field: str) -> str:
            original = mapped.get(field)
            return (raw.get(original, "") if original else "") or ""

        classification = value("classification").strip()
        # The bidder database contains contractor/business entities. Individuals and
        # vessels are intentionally excluded from this bidder-focused candidate pool.
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
    # Current SAM naming is SAM_Exclusions_Public_Extract_V2_YYDDD.ZIP.
    match = re.search(r"(?:_|\b)(\d{5})(?=\.(?:zip|csv)$|\b)", Path(filename).name, re.I)
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


def store_uploaded_extract(data: bytes, filename: str, *, cache_dir: Path | None = None) -> SamDataset:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name) or "sam_exclusions.zip"
    csv_name, records = parse_sam_exclusions(data, safe_name)
    target_dir = cache_dir or _cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name
    target.write_bytes(data)
    return SamDataset(
        records=tuple(records),
        path=target,
        sha256=_sha256(data),
        extract_date=parse_extract_date(safe_name) or parse_extract_date(csv_name),
        csv_name=csv_name,
        source="manual_upload",
    )


def _filename_from_headers(response: httpx.Response) -> str | None:
    raw = response.headers.get("content-disposition")
    if not raw:
        return None
    message = Message()
    message["content-disposition"] = raw
    return message.get_filename()


def _looks_like_extract(response: httpx.Response) -> bool:
    data = response.content
    content_type = response.headers.get("content-type", "").casefold()
    filename = (_filename_from_headers(response) or "").casefold()
    if data.startswith(b"PK\x03\x04"):
        return True
    if filename.endswith((".zip", ".csv")):
        return True
    if "zip" in content_type or "csv" in content_type or "octet-stream" in content_type:
        return bool(data)
    return False


def _walk_json_strings(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_json_strings(item, (*path, str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_json_strings(item, (*path, str(index)))
    elif isinstance(value, str):
        yield path, value.strip()


def _json_download_reference(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None, None

    file_name: str | None = None
    download_url: str | None = None
    for path, value in _walk_json_strings(payload):
        if not value:
            continue
        key = "".join(path).casefold()
        lower = value.casefold()
        if value.startswith(("https://", "http://")) and any(token in key for token in ("url", "link", "download")):
            download_url = download_url or value
        if lower.endswith((".zip", ".csv")) and not value.startswith(("https://", "http://")):
            if any(token in key for token in ("file", "name", "extract")):
                file_name = file_name or Path(value).name
    return file_name, download_url


def _sam_url_with_key(url: str, api_key: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (host == "sam.gov" or host.endswith(".sam.gov")):
        raise SamDownloadError(
            "SAM.gov returned a download URL on an unexpected host; refusing to fetch it automatically.",
            status=SourceResultStatus.DATASET_MALFORMED,
        )
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("api_key", api_key)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _classify_http_error(response: httpx.Response) -> SamDownloadError:
    if response.status_code in {401, 403}:
        return SamDownloadError(
            "SAM.gov rejected the API key or the account is not authorized for public extracts.",
            status=SourceResultStatus.AUTH_REQUIRED,
            http_status=response.status_code,
        )
    return SamDownloadError(
        f"SAM exclusions extract returned HTTP {response.status_code}.",
        status=SourceResultStatus.HTTP_ERROR,
        http_status=response.status_code,
    )


def _resolve_extract_response(
    client: httpx.Client,
    response: httpx.Response,
    *,
    api_key: str,
) -> tuple[bytes, str]:
    if response.status_code != 200:
        raise _classify_http_error(response)

    if _looks_like_extract(response):
        filename = _filename_from_headers(response)
        if not filename:
            # Never invent today's date. The CSV name inside an official ZIP can
            # still establish freshness; otherwise the extract remains undated and
            # therefore partial.
            filename = "SAM_Exclusions_Public_Extract_V2_download.ZIP" if response.content.startswith(b"PK\x03\x04") else "SAM_Exclusions_Public_Extract_V2_download.CSV"
        return response.content, filename

    file_name, download_url = _json_download_reference(response)
    follow_up: httpx.Response | None = None
    if file_name:
        follow_up = client.get(
            SAM_EXTRACT_API,
            params={"api_key": api_key, "fileName": file_name},
            headers={"Accept": "application/zip, application/json"},
        )
    elif download_url:
        follow_up = client.get(
            _sam_url_with_key(download_url, api_key),
            headers={"Accept": "application/zip, application/json"},
        )

    if follow_up is None:
        raise SamDownloadError(
            "SAM.gov returned JSON, but no usable extract file reference was present.",
            status=SourceResultStatus.DATASET_MALFORMED,
            http_status=response.status_code,
        )
    if follow_up.status_code != 200:
        raise _classify_http_error(follow_up)
    if not _looks_like_extract(follow_up):
        raise SamDownloadError(
            "SAM.gov returned a file reference, but the follow-up response was not a ZIP or CSV extract.",
            status=SourceResultStatus.DATASET_MALFORMED,
            http_status=follow_up.status_code,
        )
    resolved_name = _filename_from_headers(follow_up) or file_name
    if not resolved_name:
        resolved_name = "SAM_Exclusions_Public_Extract_V2_download.ZIP" if follow_up.content.startswith(b"PK\x03\x04") else "SAM_Exclusions_Public_Extract_V2_download.CSV"
    return follow_up.content, resolved_name


def download_latest_extract(*, api_key: str, cache_dir: Path | None = None) -> SamDataset:
    api_key = api_key.strip()
    if not api_key:
        raise SamDownloadError(
            "SAM public extract download requires a SAM.gov API key.",
            status=SourceResultStatus.AUTH_REQUIRED,
        )
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=True) as client:
            response = client.get(
                SAM_EXTRACT_API,
                params={"api_key": api_key, "fileType": "EXCLUSION"},
                headers={"Accept": "application/zip, application/json"},
            )
            data, filename = _resolve_extract_response(client, response, api_key=api_key)
    except SamDownloadError:
        raise
    except httpx.TimeoutException as exc:
        raise SamDownloadError("SAM exclusions extract download timed out.", status=SourceResultStatus.TIMEOUT) from exc
    except httpx.HTTPError as exc:
        raise SamDownloadError("SAM exclusions extract download failed.", status=SourceResultStatus.SOURCE_UNAVAILABLE) from exc

    try:
        dataset = store_uploaded_extract(data, filename, cache_dir=cache_dir)
    except SamExtractError as exc:
        raise SamDownloadError(
            f"SAM.gov responded, but the exclusions extract could not be parsed: {exc}",
            status=SourceResultStatus.DATASET_MALFORMED,
            http_status=response.status_code,
        ) from exc
    return SamDataset(
        records=dataset.records,
        path=dataset.path,
        sha256=dataset.sha256,
        extract_date=dataset.extract_date,
        csv_name=dataset.csv_name,
        source="sam_api",
    )


def load_cached_extract(*, cache_dir: Path | None = None) -> SamDataset | None:
    directory = cache_dir or _cache_dir()
    if not directory.exists():
        return None
    candidates = sorted(
        [path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() in {".zip", ".csv"}],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
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
            continue
    return None


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
    source_key = "sam"
    display_name = "SAM.gov Federal Exclusions"
    adapter_version = "1.1.0"
    parser_version = "1.0.1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        today: date | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("SAM_API_KEY", "")
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
            "api_key_configured": bool(self.api_key.strip()),
            "cached_extract": cached.path.name if cached else None,
            "cached_extract_date": cached.extract_date.isoformat() if cached and cached.extract_date else None,
            "cached_record_count": len(cached.records) if cached else 0,
        }

    def prepare(self) -> None:
        self.prepare_failure = None
        self.prepare_warnings = []
        cached = load_cached_extract(cache_dir=self.cache_dir)
        self.dataset = cached

        cached_age: int | None = None
        if cached and cached.extract_date:
            reference = self.today or datetime.now(timezone.utc).date()
            cached_age = (reference - cached.extract_date).days

        should_download = not cached or cached_age is None or cached_age > MAX_FRESH_AGE_DAYS
        if should_download and self.api_key.strip():
            try:
                self.dataset = download_latest_extract(api_key=self.api_key, cache_dir=self.cache_dir)
            except SamDownloadError as exc:
                if cached:
                    self.dataset = cached
                    self.prepare_warnings.append(
                        f"Latest SAM extract could not be downloaded; using cached file {cached.path.name}. {exc}"
                    )
                else:
                    self.prepare_failure = exc
                    self.dataset = None
        elif not cached and not self.api_key.strip():
            self.prepare_failure = SamDownloadError(
                "No SAM exclusion extract is cached and SAM_API_KEY is not configured. Upload an official SAM Public Exclusions V2 extract or configure a SAM.gov API key.",
                status=SourceResultStatus.AUTH_REQUIRED,
            )

        if self.dataset:
            self._build_index(self.dataset.records)
            if not self.dataset.extract_date:
                self.prepare_warnings.append(
                    "The SAM extract date could not be determined from the file name; results are treated as partial."
                )
            elif not self.dataset_is_fresh:
                self.prepare_warnings.append(
                    f"SAM extract {self.dataset.path.name} is {self._dataset_age_days()} days old; results are treated as partial until a current extract is loaded."
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
        self._name_choices_by_state = {key: sorted(values) for key, values in states.items()}

    def _candidate_records(self, contractor: ContractorContext) -> list[SamExclusionRecord]:
        search_names = _related_names(contractor)
        record_ids: set[str] = set()
        result: list[SamExclusionRecord] = []

        def add_name(normalized_name: str) -> None:
            for record in self._name_to_records.get(normalized_name, []):
                if record.record_id not in record_ids:
                    record_ids.add(record.record_id)
                    result.append(record)

        for name in search_names:
            normalized = normalize_company_name(name)
            add_name(normalized)

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

        # State-filtered fuzzy search is the normal fast path. If it produced no
        # candidates, do a small global fallback in case the SAM address differs.
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

    def _score_record(self, contractor: ContractorContext, record: SamExclusionRecord) -> CandidateMatch:
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
        zip_match = bool(contractor.zip and record.zip_code) and re.sub(r"\D", "", contractor.zip)[:5] == re.sub(r"\D", "", record.zip_code)[:5]
        has_location_confirmation = (
            score.address_score >= 0.90
            or zip_match
            or (score.city_score == 1.0 and score.state_score == 1.0 and bool(contractor.city and contractor.state))
        )
        auto_confirmable = score.name_score >= 0.97 and has_location_confirmation
        judgment = _remembered_judgment(contractor.internal_id, record.record_id)
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
        status = failure.status if isinstance(failure, SamDownloadError) else SourceResultStatus.DATASET_MALFORMED
        http_status = failure.http_status if isinstance(failure, SamDownloadError) else None
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

        candidates = [self._score_record(contractor, record) for record in self._candidate_records(contractor)]
        candidates = [item for item in candidates if item.score >= 0.72 and item.remembered_judgment != "DIFFERENT_ENTITY"]
        candidates.sort(key=lambda item: item.score, reverse=True)

        fresh = self.dataset_is_fresh
        completeness = CompletenessStatus.COMPLETE if fresh else CompletenessStatus.PARTIAL
        artifact = RawArtifact(
            artifact_type="sam_exclusions_extract",
            relative_path=_artifact_relative(self.dataset.path),
            sha256=self.dataset.sha256,
            mime_type="application/zip" if self.dataset.path.suffix.casefold() == ".zip" else "text/csv",
            metadata={
                "filename": self.dataset.path.name,
                "csv_name": self.dataset.csv_name,
                "extract_date": self.dataset.extract_date.isoformat() if self.dataset.extract_date else None,
                "record_count": len(self.dataset.records),
                "source": self.dataset.source,
            },
        )
        common_payload = {
            "dataset": artifact.metadata,
            "candidate_count": len(candidates),
            "top_candidates": [candidate.as_dict() for candidate in candidates[:5]],
        }
        warnings = list(self.prepare_warnings)

        remembered_same = [item for item in candidates if item.remembered_judgment == "SAME_ENTITY"]
        strong = [item for item in candidates if item.auto_confirmable]
        confirmed_pool = remembered_same or strong

        if confirmed_pool:
            # Multiple active exclusion rows can legitimately belong to the same
            # contractor. Treat them as one identity only when their identity keys
            # agree; otherwise surface ambiguity rather than guessing.
            identity_keys = {item.record.identity_key for item in confirmed_pool}
            if len(identity_keys) > 1 and not remembered_same:
                return self.validate_result(
                    SourceResult(
                        source_key=self.source_key,
                        contractor_id=contractor.internal_id,
                        status=SourceResultStatus.AMBIGUOUS_MATCH if fresh else SourceResultStatus.PARTIAL_RESULTS,
                        identity_status=IdentityStatus.REVIEW_REQUIRED,
                        completeness_status=completeness,
                        identity_confidence=confirmed_pool[0].score,
                        searched_name=contractor.contractor_name,
                        searched_address=contractor.address_1,
                        warnings=warnings + ["Multiple strong SAM records appear to represent different identities."],
                        artifacts=[artifact],
                        normalized_payload=common_payload,
                        source_url=SAM_PUBLIC_SEARCH,
                        acquisition_method="sam_public_exclusions_v2_extract",
                    )
                )

            primary = confirmed_pool[0]
            matching_identity = [item for item in candidates if item.record.identity_key == primary.record.identity_key]
            match_details = [item.as_dict() for item in matching_identity]
            evidence = EvidenceRecord(
                field_name="state_federal_debarment",
                observed_value="Y",
                source_record_id=primary.record.record_id,
                source_url=SAM_PUBLIC_SEARCH,
                details={
                    "basis": "Active federal exclusion in SAM.gov Public Exclusions V2 extract",
                    "active_exclusion_count": len(matching_identity),
                    "matches": match_details,
                    "dataset_extract_date": self.dataset.extract_date.isoformat() if self.dataset.extract_date else None,
                },
            )
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.SUCCESS_WITH_FINDINGS if fresh else SourceResultStatus.PARTIAL_RESULTS,
                    identity_status=IdentityStatus.CONFIRMED,
                    completeness_status=completeness,
                    identity_confidence=primary.score,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    evidence=[evidence],
                    warnings=warnings,
                    artifacts=[artifact],
                    normalized_payload={**common_payload, "confirmed_matches": match_details},
                    source_record_id=primary.record.record_id,
                    source_url=SAM_PUBLIC_SEARCH,
                    acquisition_method="sam_public_exclusions_v2_extract",
                )
            )

        medium = [item for item in candidates if item.score >= 0.78 and item.name_score >= 0.75]
        if medium:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.AMBIGUOUS_MATCH if fresh else SourceResultStatus.PARTIAL_RESULTS,
                    identity_status=IdentityStatus.REVIEW_REQUIRED,
                    completeness_status=completeness,
                    identity_confidence=medium[0].score,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings + ["SAM returned a possible company match that is not strong enough to auto-confirm."],
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
                    warnings=warnings + ["No match was found, but the SAM extract is not current enough for a clean negative result."],
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
