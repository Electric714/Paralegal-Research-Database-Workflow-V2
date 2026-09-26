from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import ContractorContext
from .osha_bulk import (
    ARCHIVE_FILENAME,
    DOL_OSHA_BULK_URL,
    INDEX_FILENAME,
    MAX_INDEX_AGE_DAYS,
    METADATA_FILENAME,
    OfficialBulkOshaEstablishmentSource,
    OshaBulkIndexError,
    build_bulk_index,
)


OSHA_BULK_CACHE_ENV = "OSHA_BULK_CACHE_DIR"
OSHA_BULK_AUTO_REFRESH_ENV = "OSHA_BULK_AUTO_REFRESH"
MIN_OFFICIAL_RECORD_COUNT = 3_000_000
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "Paralegal-Research-Database-Workflow-V2/2.2"


def default_operational_cache_dir() -> Path:
    """Return the stable app-level OSHA cache, independent of an audit DB location."""
    configured = os.getenv(OSHA_BULK_CACHE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    backend_dir = Path(__file__).resolve().parents[3]
    return backend_dir / "data" / "source_cache" / "osha"


def _metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sqlite_index_ok(index_path: Path) -> bool:
    if not index_path.exists():
        return False
    try:
        with sqlite3.connect(index_path) as conn:
            conn.execute("SELECT activity_nr FROM inspections LIMIT 1").fetchone()
            conn.execute("SELECT rowid FROM inspection_names LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def inspect_operational_snapshot(cache_dir: Path | None = None) -> dict[str, Any]:
    cache_dir = Path(cache_dir or default_operational_cache_dir())
    index_path = cache_dir / INDEX_FILENAME
    metadata_path = cache_dir / METADATA_FILENAME
    archive_path = cache_dir / ARCHIVE_FILENAME
    metadata = _metadata(metadata_path)
    downloaded = _parse_utc(metadata.get("downloaded_at"))
    checked = _parse_utc(metadata.get("source_checked_at"))
    freshness = _parse_utc(metadata.get("freshness_as_of")) or checked or downloaded
    age_days: float | None = None
    if freshness is not None:
        age_days = max(
            0.0,
            (datetime.now(timezone.utc) - freshness).total_seconds() / 86400.0,
        )

    sqlite_ok = _sqlite_index_ok(index_path)
    ready = bool(sqlite_ok and metadata and downloaded is not None)
    stale = not ready or age_days is None or age_days > MAX_INDEX_AGE_DAYS
    return {
        "cache_dir": str(cache_dir),
        "index_path": str(index_path),
        "metadata_path": str(metadata_path),
        "archive_path": str(archive_path),
        "index_exists": index_path.exists(),
        "metadata_exists": metadata_path.exists(),
        "archive_exists": archive_path.exists(),
        "sqlite_ok": sqlite_ok,
        "ready": ready,
        "stale": stale,
        "age_days": round(age_days, 3) if age_days is not None else None,
        "downloaded_at": metadata.get("downloaded_at"),
        "source_checked_at": metadata.get("source_checked_at"),
        "freshness_as_of": metadata.get("freshness_as_of"),
        "index_built_at": metadata.get("index_built_at"),
        "record_count": metadata.get("record_count"),
        "archive_bytes": metadata.get("archive_bytes"),
        "archive_sha256": metadata.get("archive_sha256"),
        "http_etag": metadata.get("http_etag"),
        "http_last_modified": metadata.get("http_last_modified"),
        "source_url": metadata.get("source_url") or DOL_OSHA_BULK_URL,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _http_client(client: httpx.Client | None = None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return (
        httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30.0, read=180.0, write=30.0, pool=30.0),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/zip,application/octet-stream,*/*",
            },
        ),
        True,
    )


def probe_official_archive(
    *,
    client: httpx.Client | None = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
) -> dict[str, str]:
    """Read the remote object's identity without downloading the 1+ GiB archive."""
    client, owns_client = _http_client(client)
    try:
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                response = client.head(DOL_OSHA_BULK_URL)
                if response.status_code in TRANSIENT_HTTP_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"Transient DOL OSHA bulk HEAD HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                headers = {key.casefold(): value for key, value in response.headers.items()}
                if not headers.get("etag") and not headers.get("last-modified"):
                    raise OshaBulkIndexError(
                        "The OSHA bulk server did not provide ETag or Last-Modified identity metadata."
                    )
                return headers
            except (httpx.HTTPError, OshaBulkIndexError) as exc:
                last_error = exc
                if attempt >= max(1, attempts):
                    break
                time.sleep(min(8.0, 2.0 ** (attempt - 1)))
        raise OshaBulkIndexError(
            f"Unable to verify the official OSHA snapshot after {max(1, attempts)} attempt(s): {last_error}"
        ) from last_error
    finally:
        if owns_client:
            client.close()


def _remote_matches_cached(
    remote_headers: dict[str, str],
    prior_metadata: dict[str, Any],
    archive_path: Path,
) -> bool:
    if not archive_path.exists():
        return False

    remote_etag = str(remote_headers.get("etag") or "").strip()
    cached_etag = str(prior_metadata.get("http_etag") or "").strip()
    if remote_etag and cached_etag:
        return remote_etag == cached_etag

    remote_modified = str(remote_headers.get("last-modified") or "").strip()
    cached_modified = str(prior_metadata.get("http_last_modified") or "").strip()
    remote_length = str(remote_headers.get("content-length") or "").strip()
    cached_bytes = prior_metadata.get("archive_bytes")
    same_length = bool(
        remote_length.isdigit()
        and int(remote_length) == archive_path.stat().st_size
        and (cached_bytes in (None, "") or int(cached_bytes) == int(remote_length))
    )
    return bool(remote_modified and cached_modified and remote_modified == cached_modified and same_length)


def download_official_archive_resilient(
    destination: Path,
    *,
    client: httpx.Client | None = None,
    attempts: int = DOWNLOAD_ATTEMPTS,
) -> tuple[str, dict[str, str]]:
    """Download the official ZIP without ever replacing a known-good archive on failure."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")

    client, owns_client = _http_client(client)
    try:
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            partial.unlink(missing_ok=True)
            digest = hashlib.sha256()
            try:
                with client.stream("GET", DOL_OSHA_BULK_URL) as response:
                    if response.status_code in TRANSIENT_HTTP_STATUSES:
                        raise httpx.HTTPStatusError(
                            f"Transient DOL OSHA bulk HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    headers = {key.casefold(): value for key, value in response.headers.items()}
                    expected_length = response.headers.get("content-length")
                    written = 0
                    with partial.open("wb") as output:
                        for chunk in response.iter_bytes(DOWNLOAD_CHUNK_SIZE):
                            if not chunk:
                                continue
                            output.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                    if expected_length and expected_length.isdigit() and written != int(expected_length):
                        raise OshaBulkIndexError(
                            f"Incomplete OSHA bulk download: expected {expected_length} bytes, received {written}."
                        )
                import zipfile

                if not zipfile.is_zipfile(partial):
                    raise OshaBulkIndexError(
                        "DOL's OSHA complete-dataset download did not return a valid ZIP archive."
                    )
                partial.replace(destination)
                return digest.hexdigest(), headers
            except (httpx.HTTPError, OshaBulkIndexError, OSError) as exc:
                last_error = exc
                partial.unlink(missing_ok=True)
                if attempt >= max(1, attempts):
                    break
                time.sleep(min(8.0, 2.0 ** (attempt - 1)))
        raise OshaBulkIndexError(
            f"Unable to download the official OSHA complete dataset after {max(1, attempts)} attempt(s): {last_error}"
        ) from last_error
    finally:
        partial.unlink(missing_ok=True)
        if owns_client:
            client.close()


def _write_metadata_atomically(path: Path, metadata: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    temporary.replace(path)


def refresh_operational_osha_index(
    *,
    cache_dir: Path | None = None,
    source_archive: Path | None = None,
    force_download: bool = False,
    keep_archive: bool = True,
    client: httpx.Client | None = None,
    minimum_record_count: int | None = None,
) -> dict[str, Any]:
    """Safely refresh or verify the operational OSHA snapshot.

    For an official cached archive, a normal refresh first performs a lightweight HEAD
    probe and compares ETag (or Last-Modified + length). If the remote object is
    unchanged, its cached snapshot is re-verified as current without redownloading the
    1+ GiB ZIP. `downloaded_at` remains the acquisition time; `source_checked_at` and
    `freshness_as_of` record the later authoritative remote verification.

    A changed/missing snapshot is downloaded into staging, indexed, validated, and only
    then promoted. Any failed refresh leaves the previous known-good files untouched.
    """
    cache_dir = Path(cache_dir or default_operational_cache_dir())
    cache_dir.mkdir(parents=True, exist_ok=True)
    current_archive = cache_dir / ARCHIVE_FILENAME
    current_metadata = cache_dir / METADATA_FILENAME
    current_index = cache_dir / INDEX_FILENAME
    prior_metadata = _metadata(current_metadata)
    checked_at = datetime.now(timezone.utc)

    remote_headers: dict[str, str] = {}
    remote_unchanged = False
    if source_archive is None and current_archive.exists() and not force_download:
        remote_headers = probe_official_archive(client=client)
        remote_unchanged = _remote_matches_cached(remote_headers, prior_metadata, current_archive)
        if remote_unchanged and _sqlite_index_ok(current_index):
            verified = dict(prior_metadata)
            verified["source_checked_at"] = checked_at.isoformat()
            verified["freshness_as_of"] = checked_at.isoformat()
            verified["snapshot_freshness_basis"] = "remote_identity_verified"
            verified["http_etag"] = remote_headers.get("etag") or verified.get("http_etag")
            verified["http_last_modified"] = remote_headers.get("last-modified") or verified.get("http_last_modified")
            if str(remote_headers.get("content-length") or "").isdigit():
                verified["archive_bytes"] = int(remote_headers["content-length"])
            _write_metadata_atomically(current_metadata, verified)
            if not keep_archive:
                current_archive.unlink(missing_ok=True)
            return verified

    stage_dir = cache_dir / f".refresh-{uuid.uuid4().hex[:10]}"
    stage_dir.mkdir(parents=True, exist_ok=False)
    stage_archive = stage_dir / ARCHIVE_FILENAME
    stage_index = stage_dir / INDEX_FILENAME
    stage_metadata = stage_dir / METADATA_FILENAME

    downloaded_new_archive = False
    response_headers: dict[str, str] = {}
    archive_sha256: str | None = None
    snapshot_downloaded_at: datetime
    try:
        if source_archive is not None:
            archive_for_build = Path(source_archive).resolve()
            if not archive_for_build.exists():
                raise OshaBulkIndexError(f"OSHA source archive does not exist: {archive_for_build}")
            snapshot_downloaded_at = datetime.fromtimestamp(
                archive_for_build.stat().st_mtime, tz=timezone.utc
            )
            archive_sha256 = _sha256_file(archive_for_build)
            official_snapshot = False
        else:
            official_snapshot = True
            prior_downloaded_at = _parse_utc(prior_metadata.get("downloaded_at"))
            need_download = force_download or not current_archive.exists() or not remote_unchanged
            if need_download:
                archive_sha256, response_headers = download_official_archive_resilient(
                    stage_archive, client=client
                )
                archive_for_build = stage_archive
                snapshot_downloaded_at = datetime.now(timezone.utc)
                checked_at = snapshot_downloaded_at
                downloaded_new_archive = True
            else:
                # Remote identity was verified unchanged, but the local index was
                # missing/corrupt. Rebuild locally without pretending the ZIP was
                # redownloaded today.
                archive_for_build = current_archive
                snapshot_downloaded_at = prior_downloaded_at or datetime.fromtimestamp(
                    current_archive.stat().st_mtime, tz=timezone.utc
                )
                archive_sha256 = prior_metadata.get("archive_sha256") or _sha256_file(current_archive)
                response_headers = remote_headers

        metadata = build_bulk_index(
            archive_for_build,
            index_path=stage_index,
            metadata_path=stage_metadata,
            downloaded_at=snapshot_downloaded_at,
            archive_sha256=archive_sha256,
        )
        floor = (
            MIN_OFFICIAL_RECORD_COUNT
            if minimum_record_count is None and official_snapshot
            else int(minimum_record_count or 0)
        )
        if floor and int(metadata.get("record_count") or 0) < floor:
            raise OshaBulkIndexError(
                "Official OSHA complete dataset failed the record-count sanity check: "
                f"{metadata.get('record_count')} records, expected at least {floor}."
            )

        metadata["index_built_at"] = datetime.now(timezone.utc).isoformat()
        metadata["minimum_record_count_checked"] = floor
        if official_snapshot:
            metadata["source_checked_at"] = checked_at.isoformat()
            metadata["freshness_as_of"] = checked_at.isoformat()
            metadata["snapshot_freshness_basis"] = (
                "downloaded_current_remote_object" if downloaded_new_archive else "remote_identity_verified"
            )
        else:
            metadata["snapshot_freshness_basis"] = "operator_provided_archive_mtime"
        if response_headers:
            metadata["http_etag"] = response_headers.get("etag")
            metadata["http_last_modified"] = response_headers.get("last-modified")
            if str(response_headers.get("content-length") or "").isdigit():
                metadata["archive_bytes"] = int(response_headers["content-length"])
        stage_metadata.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        # Promote only after every validation above succeeded.
        stage_index.replace(current_index)
        stage_metadata.replace(current_metadata)
        if downloaded_new_archive:
            if keep_archive:
                stage_archive.replace(current_archive)
            else:
                current_archive.unlink(missing_ok=True)
        elif source_archive is None and not keep_archive:
            current_archive.unlink(missing_ok=True)
        return metadata
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


class OperationalOshaEstablishmentSource(OfficialBulkOshaEstablishmentSource):
    """Production OSHA adapter with stable cache location and safe refresh behavior."""

    adapter_version = "2.2.0"
    parser_version = "2.0.0"

    def __init__(self, *, cache_dir: Path | None = None, **kwargs: Any) -> None:
        super().__init__(cache_dir=Path(cache_dir or default_operational_cache_dir()), **kwargs)
        self.auto_refresh_error: str | None = None

    def prepare(self) -> None:
        self.auto_refresh_error = None
        auto_refresh = os.getenv(OSHA_BULK_AUTO_REFRESH_ENV, "").strip().casefold() in {
            "1",
            "true",
            "yes",
        }
        state = inspect_operational_snapshot(self.cache_dir)
        if auto_refresh and (not state["ready"] or state["stale"]):
            try:
                refresh_operational_osha_index(
                    cache_dir=self.cache_dir,
                    force_download=False,
                    keep_archive=True,
                )
            except Exception as exc:  # Keep a stale last-known-good index usable for positives.
                self.auto_refresh_error = f"Automatic OSHA snapshot refresh failed: {type(exc).__name__}: {exc}"

        # The parent has an older optional auto-refresh hook. Disable that hook while
        # loading the index so only the staged/validated refresh path above can run.
        previous = os.environ.pop(OSHA_BULK_AUTO_REFRESH_ENV, None)
        try:
            super().prepare()
        finally:
            if previous is not None:
                os.environ[OSHA_BULK_AUTO_REFRESH_ENV] = previous

        # The parent predates remote identity verification and calculates staleness
        # from downloaded_at only. Operational freshness instead follows the latest
        # successful authoritative remote verification when available.
        operational_state = inspect_operational_snapshot(self.cache_dir)
        self.bulk_stale = bool(operational_state["stale"])
        if operational_state["metadata_exists"]:
            self.bulk_metadata = _metadata(self.metadata_path)

    def health_check(self) -> dict[str, Any]:
        health = super().health_check()
        state = inspect_operational_snapshot(self.cache_dir)
        health.update(
            operational_cache_dir=str(self.cache_dir),
            auto_refresh_error=self.auto_refresh_error,
            source_checked_at=state.get("source_checked_at"),
            freshness_as_of=state.get("freshness_as_of"),
            refresh_command="python scripts/refresh_osha_bulk.py",
        )
        return health

    def search(self, contractor: ContractorContext):
        result = super().search(contractor)
        if self.auto_refresh_error and self.auto_refresh_error not in result.warnings:
            result.warnings.append(self.auto_refresh_error)
        result.normalized_payload.setdefault("bulk_snapshot", {})
        result.normalized_payload["bulk_snapshot"].update(
            {
                "operational_cache_dir": str(self.cache_dir),
                "archive_sha256": self.bulk_metadata.get("archive_sha256"),
                "index_built_at": self.bulk_metadata.get("index_built_at"),
                "source_checked_at": self.bulk_metadata.get("source_checked_at"),
                "freshness_as_of": self.bulk_metadata.get("freshness_as_of"),
                "http_etag": self.bulk_metadata.get("http_etag"),
            }
        )
        return self.validate_result(result)