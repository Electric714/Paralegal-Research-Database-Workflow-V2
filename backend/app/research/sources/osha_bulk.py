from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from rapidfuzz import fuzz

from ... import database as db
from ..matching import normalize_company_name
from ..models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext
from .osha import DETAIL_URL, OshaSearchRow, ParsedSearchPage
from .osha_resilient import ResilientOshaEstablishmentSource


DOL_OSHA_DATASET_PAGE = "https://data.dol.gov/datasets/10334"
DOL_OSHA_BULK_URL = "https://data.dol.gov/data-catalog/OSHA/inspection/OSHA_inspection.zip"
INDEX_FILENAME = "osha_inspection.sqlite3"
METADATA_FILENAME = "osha_inspection.metadata.json"
ARCHIVE_FILENAME = "OSHA_inspection.zip"
MAX_INDEX_AGE_DAYS = 14
MAX_CANDIDATES_PER_QUERY = 4000
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class OshaBulkIndexError(RuntimeError):
    pass


def _cache_root() -> Path:
    return db.DATA_DIR / "source_cache" / "osha"


def _field(row: dict[str, Any], *names: str) -> str:
    lowered = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value is not None:
            return str(value).strip()
    return ""


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    return None


def _normalize_open_date(value: str) -> str:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else value.strip()


def _row_tuple(row: dict[str, Any]) -> tuple[str, ...] | None:
    activity = _field(row, "activity_nr", "activity_number", "activity")
    establishment = _field(row, "estab_name", "establishment_name", "establishment")
    normalized = normalize_company_name(establishment)
    if not activity or not normalized:
        return None
    return (
        activity,
        establishment,
        normalized,
        _field(row, "reporting_id", "rid"),
        _field(row, "site_address", "site_street", "address"),
        _field(row, "site_city", "city"),
        _field(row, "site_state", "state").upper(),
        _field(row, "site_zip", "zip"),
        _field(row, "mail_street", "mailing_street"),
        _field(row, "mail_city", "mailing_city"),
        _field(row, "mail_state", "mailing_state").upper(),
        _field(row, "mail_zip", "mailing_zip"),
        _field(row, "insp_type", "inspection_type", "type"),
        _field(row, "insp_scope", "scope"),
        _field(row, "sic_code", "sic"),
        _field(row, "naics_code", "naics"),
        _normalize_open_date(_field(row, "open_date", "date_opened")),
        _field(row, "nr_violations", "violations", "violation_count"),
    )


def build_bulk_index(
    archive_path: Path,
    *,
    index_path: Path,
    metadata_path: Path,
    source_url: str = DOL_OSHA_BULK_URL,
    downloaded_at: datetime | None = None,
    archive_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a compact local search index from DOL's complete OSHA inspection ZIP."""
    archive_path = Path(archive_path)
    if not archive_path.exists() or not zipfile.is_zipfile(archive_path):
        raise OshaBulkIndexError(f"OSHA bulk archive is missing or invalid: {archive_path}")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    temp_index = index_path.with_suffix(index_path.suffix + ".tmp")
    if temp_index.exists():
        temp_index.unlink()

    conn = sqlite3.connect(temp_index)
    record_count = 0
    member_count = 0
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            CREATE TABLE inspections (
                activity_nr TEXT PRIMARY KEY,
                establishment_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                reporting_id TEXT,
                site_address TEXT,
                site_city TEXT,
                site_state TEXT,
                site_zip TEXT,
                mail_street TEXT,
                mail_city TEXT,
                mail_state TEXT,
                mail_zip TEXT,
                inspection_type TEXT,
                scope TEXT,
                sic TEXT,
                naics TEXT,
                open_date TEXT,
                violations TEXT
            );
            """
        )
        insert_sql = """
            INSERT OR REPLACE INTO inspections(
                activity_nr, establishment_name, normalized_name, reporting_id,
                site_address, site_city, site_state, site_zip,
                mail_street, mail_city, mail_state, mail_zip,
                inspection_type, scope, sic, naics, open_date, violations
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with zipfile.ZipFile(archive_path) as archive:
            csv_members = [name for name in archive.namelist() if name.casefold().endswith(".csv")]
            if not csv_members:
                raise OshaBulkIndexError("The OSHA bulk ZIP did not contain any CSV files.")
            for member in csv_members:
                member_count += 1
                batch: list[tuple[str, ...]] = []
                with archive.open(member) as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
                    reader = csv.DictReader(text)
                    if not reader.fieldnames:
                        raise OshaBulkIndexError(f"OSHA bulk CSV has no header: {member}")
                    header = {name.strip().casefold() for name in reader.fieldnames if name}
                    if not ({"activity_nr", "activity_number", "activity"} & header):
                        raise OshaBulkIndexError(f"OSHA bulk CSV lost its activity-number column: {member}")
                    if not ({"estab_name", "establishment_name", "establishment"} & header):
                        raise OshaBulkIndexError(f"OSHA bulk CSV lost its establishment-name column: {member}")
                    for row in reader:
                        value = _row_tuple(row)
                        if value is None:
                            continue
                        batch.append(value)
                        if len(batch) >= 5000:
                            conn.executemany(insert_sql, batch)
                            record_count += len(batch)
                            batch.clear()
                    if batch:
                        conn.executemany(insert_sql, batch)
                        record_count += len(batch)
                conn.commit()

        conn.executescript(
            """
            CREATE INDEX idx_osha_normalized_name ON inspections(normalized_name);
            CREATE INDEX idx_osha_site_state ON inspections(site_state);
            CREATE INDEX idx_osha_open_date ON inspections(open_date);
            CREATE VIRTUAL TABLE inspection_names USING fts5(
                normalized_name,
                content='inspections',
                content_rowid='rowid'
            );
            INSERT INTO inspection_names(rowid, normalized_name)
                SELECT rowid, normalized_name FROM inspections;
            INSERT INTO inspection_names(inspection_names) VALUES('optimize');
            """
        )
        conn.commit()
    except Exception:
        conn.close()
        if temp_index.exists():
            temp_index.unlink()
        raise
    else:
        conn.close()

    temp_index.replace(index_path)
    downloaded_at = downloaded_at or datetime.now(timezone.utc)
    metadata = {
        "schema_version": 1,
        "source": "U.S. Department of Labor Open Data Portal - OSHA Inspection",
        "dataset_page": DOL_OSHA_DATASET_PAGE,
        "source_url": source_url,
        "downloaded_at": downloaded_at.astimezone(timezone.utc).isoformat(),
        "record_count": record_count,
        "csv_member_count": member_count,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": archive_sha256,
        "index_file": index_path.name,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def download_official_bulk_archive(
    destination: Path,
    *,
    client: httpx.Client | None = None,
) -> tuple[str, dict[str, str]]:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if partial.exists():
        partial.unlink()

    owns_client = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0),
        headers={
            "User-Agent": "Paralegal-Research-Database-Workflow-V2/2.0",
            "Accept": "application/zip,application/octet-stream,*/*",
        },
    )
    digest = hashlib.sha256()
    response_headers: dict[str, str] = {}
    try:
        with client.stream("GET", DOL_OSHA_BULK_URL) as response:
            response.raise_for_status()
            response_headers = {key.casefold(): value for key, value in response.headers.items()}
            with partial.open("wb") as output:
                for chunk in response.iter_bytes(DOWNLOAD_CHUNK_SIZE):
                    if chunk:
                        output.write(chunk)
                        digest.update(chunk)
    finally:
        if owns_client:
            client.close()

    if not zipfile.is_zipfile(partial):
        partial.unlink(missing_ok=True)
        raise OshaBulkIndexError(
            "DOL's OSHA complete-dataset download did not return a valid ZIP archive."
        )
    partial.replace(destination)
    return digest.hexdigest(), response_headers


def refresh_official_bulk_index(
    *,
    cache_dir: Path | None = None,
    source_archive: Path | None = None,
    force_download: bool = False,
    keep_archive: bool = True,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    cache_dir = Path(cache_dir or _cache_root())
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path = cache_dir / INDEX_FILENAME
    metadata_path = cache_dir / METADATA_FILENAME

    archive_sha256: str | None = None
    response_headers: dict[str, str] = {}
    downloaded_at = datetime.now(timezone.utc)
    if source_archive is not None:
        archive_path = Path(source_archive)
    else:
        archive_path = cache_dir / ARCHIVE_FILENAME
        if force_download or not archive_path.exists():
            archive_sha256, response_headers = download_official_bulk_archive(
                archive_path, client=client
            )
        elif metadata_path.exists():
            try:
                prior = json.loads(metadata_path.read_text(encoding="utf-8"))
                archive_sha256 = prior.get("archive_sha256")
            except (OSError, ValueError, TypeError):
                pass

    metadata = build_bulk_index(
        archive_path,
        index_path=index_path,
        metadata_path=metadata_path,
        downloaded_at=downloaded_at,
        archive_sha256=archive_sha256,
    )
    if response_headers:
        metadata["http_etag"] = response_headers.get("etag")
        metadata["http_last_modified"] = response_headers.get("last-modified")
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if source_archive is None and not keep_archive:
        archive_path.unlink(missing_ok=True)
    return metadata


def _fts_query(normalized_name: str) -> list[str]:
    tokens = [token for token in normalized_name.split() if token]
    queries: list[str] = []
    if tokens:
        significant = [token for token in tokens if len(token) >= 3]
        primary = significant or tokens
        queries.append(" AND ".join(f'"{token}"' for token in primary))
        if len(primary) > 1:
            for token in sorted(primary, key=len, reverse=True)[:2]:
                queries.append(f'"{token}"')
    return list(dict.fromkeys(queries))


class OfficialBulkOshaEstablishmentSource(ResilientOshaEstablishmentSource):
    """OSHA adapter backed by DOL's complete downloadable inspection dataset.

    The public OSHA HTML search is intentionally not the primary path. The DOL bulk
    file is downloaded/indexed separately, then bidder research is local and fast.
    This avoids turning a transient ORDS outage into hours of retries while preserving
    complete-history negative checks when the local index is fresh.
    """

    adapter_version = "2.0.0"
    parser_version = "2.0.0"

    def __init__(self, *, cache_dir: Path | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cache_dir = Path(cache_dir or _cache_root())
        self.index_path = self.cache_dir / INDEX_FILENAME
        self.metadata_path = self.cache_dir / METADATA_FILENAME
        self.bulk_metadata: dict[str, Any] = {}
        self.bulk_ready = False
        self.bulk_stale = True
        self._candidate_cache: dict[tuple[str, str], tuple[list[OshaSearchRow], bool]] = {}
        self._detail_cache: dict[str, dict[str, str]] = {}

    def prepare(self) -> None:
        self.bulk_ready = False
        self.bulk_stale = True
        self.bulk_metadata = {}
        self._candidate_cache.clear()
        self._detail_cache.clear()

        if os.getenv("OSHA_BULK_AUTO_REFRESH", "").strip().casefold() in {"1", "true", "yes"}:
            refresh_official_bulk_index(cache_dir=self.cache_dir, keep_archive=True)

        if not self.index_path.exists() or not self.metadata_path.exists():
            return
        try:
            self.bulk_metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            downloaded = datetime.fromisoformat(str(self.bulk_metadata["downloaded_at"]).replace("Z", "+00:00"))
            age_days = max(0.0, (datetime.now(timezone.utc) - downloaded.astimezone(timezone.utc)).total_seconds() / 86400.0)
            self.bulk_stale = age_days > MAX_INDEX_AGE_DAYS
            with sqlite3.connect(self.index_path) as conn:
                conn.execute("SELECT activity_nr FROM inspections LIMIT 1").fetchone()
                conn.execute("SELECT rowid FROM inspection_names LIMIT 1").fetchone()
        except (OSError, ValueError, KeyError, sqlite3.Error):
            self.bulk_metadata = {}
            return
        self.bulk_ready = True

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "official_dol_bulk_index",
            "dataset_page": DOL_OSHA_DATASET_PAGE,
            "bulk_url": DOL_OSHA_BULK_URL,
            "index_path": str(self.index_path),
            "index_ready": self.bulk_ready,
            "index_stale": self.bulk_stale,
            "index_max_age_days": MAX_INDEX_AGE_DAYS,
            "downloaded_at": self.bulk_metadata.get("downloaded_at"),
            "record_count": self.bulk_metadata.get("record_count"),
            "refresh_command": "python scripts/refresh_osha_bulk.py",
        }

    def _query_candidates(self, search_name: str, state: str) -> tuple[list[OshaSearchRow], bool]:
        normalized = normalize_company_name(search_name)
        cache_key = (normalized, state.upper())
        cached = self._candidate_cache.get(cache_key)
        if cached is not None:
            return cached
        if not normalized:
            result = ([], True)
            self._candidate_cache[cache_key] = result
            return result

        collected: dict[str, tuple[Any, ...]] = {}
        complete = True
        with sqlite3.connect(self.index_path) as conn:
            conn.row_factory = sqlite3.Row
            for match_query in _fts_query(normalized):
                sql = """
                    SELECT i.*
                    FROM inspection_names
                    JOIN inspections AS i ON i.rowid = inspection_names.rowid
                    WHERE inspection_names MATCH ?
                """
                params: list[Any] = [match_query]
                if state and state.casefold() != "all":
                    sql += " AND i.site_state = ?"
                    params.append(state.upper())
                sql += " LIMIT ?"
                params.append(MAX_CANDIDATES_PER_QUERY + 1)
                rows = conn.execute(sql, params).fetchall()
                if len(rows) > MAX_CANDIDATES_PER_QUERY:
                    complete = False
                    rows = rows[:MAX_CANDIDATES_PER_QUERY]
                for row in rows:
                    collected[str(row["activity_nr"])] = tuple(row)
                if rows and len(match_query.split(" AND ")) > 1:
                    break

            rows_out: list[OshaSearchRow] = []
            for raw in collected.values():
                row = dict(zip([d[1] for d in conn.execute("PRAGMA table_info(inspections)").fetchall()], raw))
                candidate_name = str(row.get("establishment_name") or "")
                candidate_normalized = str(row.get("normalized_name") or "")
                name_score = fuzz.WRatio(normalized, candidate_normalized) / 100.0
                containment = normalized in candidate_normalized or candidate_normalized in normalized
                if not containment and name_score < 0.60:
                    continue
                activity = str(row.get("activity_nr") or "")
                detail_url = f"{DETAIL_URL}?id={activity}"
                rows_out.append(
                    OshaSearchRow(
                        activity_number=activity,
                        date_opened=str(row.get("open_date") or ""),
                        reporting_id=str(row.get("reporting_id") or ""),
                        state=str(row.get("site_state") or "").upper(),
                        inspection_type=str(row.get("inspection_type") or ""),
                        scope=str(row.get("scope") or ""),
                        sic=str(row.get("sic") or ""),
                        naics=str(row.get("naics") or ""),
                        violations=str(row.get("violations") or ""),
                        establishment_name=candidate_name,
                        detail_url=detail_url,
                    )
                )
                self._detail_cache[activity] = {
                    "case_status": "",
                    "site_address": " | ".join(
                        part for part in (
                            str(row.get("site_address") or ""),
                            str(row.get("site_city") or ""),
                            str(row.get("site_state") or ""),
                            str(row.get("site_zip") or ""),
                        ) if part
                    ),
                    "site_street": str(row.get("site_address") or ""),
                    "site_city": str(row.get("site_city") or ""),
                    "site_state": str(row.get("site_state") or ""),
                    "site_zip": str(row.get("site_zip") or ""),
                    "mailing_address": " | ".join(
                        part for part in (
                            str(row.get("mail_street") or ""),
                            str(row.get("mail_city") or ""),
                            str(row.get("mail_state") or ""),
                            str(row.get("mail_zip") or ""),
                        ) if part
                    ),
                    "mailing_street": str(row.get("mail_street") or ""),
                    "mailing_city": str(row.get("mail_city") or ""),
                    "mailing_state": str(row.get("mail_state") or ""),
                    "mailing_zip": str(row.get("mail_zip") or ""),
                }

        result = (rows_out, complete)
        self._candidate_cache[cache_key] = result
        return result

    def _search_request(self, search_name: str, state: str, start_date: date, end_date: date):
        if not self.bulk_ready:
            return super()._search_request(search_name, state, start_date, end_date)
        candidates, complete = self._query_candidates(search_name, state)
        rows = []
        for row in candidates:
            opened = _parse_date(row.date_opened)
            if opened is not None and not (start_date <= opened <= end_date):
                continue
            rows.append(row)
        parsed = ParsedSearchPage(
            rows=tuple(rows),
            table_found=True,
            total_results=len(rows),
            complete=complete,
        )
        return parsed, DOL_OSHA_BULK_URL, 200

    def _detail_request(self, row: OshaSearchRow) -> dict[str, str]:
        if self.bulk_ready:
            return dict(self._detail_cache.get(row.activity_number, {}))
        return super()._detail_request(row)

    def search(self, contractor: ContractorContext) -> SourceResult:
        if not self.bulk_ready:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.SOURCE_UNAVAILABLE,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=[
                        "The local official OSHA bulk index is not prepared. Run "
                        "`python scripts/refresh_osha_bulk.py` once, then rerun OSHA research. "
                        "The former live ORDS search is not used as the primary path because repeated live timeouts can consume the entire source budget."
                    ],
                    acquisition_method="official_dol_bulk_index",
                    source_url=DOL_OSHA_DATASET_PAGE,
                    normalized_payload={"index_ready": False},
                )
            )

        result = super().search(contractor)
        result.acquisition_method = "official_dol_bulk_index"
        result.http_status = None
        result.normalized_payload["bulk_snapshot"] = {
            "dataset_page": DOL_OSHA_DATASET_PAGE,
            "source_url": DOL_OSHA_BULK_URL,
            "downloaded_at": self.bulk_metadata.get("downloaded_at"),
            "record_count": self.bulk_metadata.get("record_count"),
            "stale": self.bulk_stale,
        }
        if self.bulk_stale:
            warning = (
                f"The local OSHA complete-dataset index is older than {MAX_INDEX_AGE_DAYS} days. "
                "Positive findings remain usable, but a no-match is not considered a current clean negative until the index is refreshed."
            )
            if warning not in result.warnings:
                result.warnings.append(warning)
            if result.status == SourceResultStatus.SUCCESS_NO_MATCH:
                result.status = SourceResultStatus.PARTIAL_RESULTS
                result.completeness_status = CompletenessStatus.PARTIAL
        return self.validate_result(result)
