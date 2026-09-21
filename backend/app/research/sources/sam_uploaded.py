from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from ..models import SourceResultStatus
from .sam_exclusions import (
    MAX_FRESH_AGE_DAYS,
    SamDownloadError,
    SamExclusionsSource,
    load_cached_extract,
)


class SamUploadedExclusionsSource(SamExclusionsSource):
    """SAM exclusions adapter that only uses a locally uploaded official extract.

    This intentionally performs no SAM.gov API calls. The user supplies the official
    Public Exclusions V2 CSV/ZIP through the application, and research runs compare
    that local dataset against the approved bidder database.
    """

    adapter_version = "1.2.0"

    def __init__(self, *, cache_dir: Path | None = None, today: date | None = None) -> None:
        # Force the parent adapter's API key to empty so accidental network refreshes
        # cannot occur even if SAM_API_KEY exists in the environment.
        super().__init__(api_key="", cache_dir=cache_dir, today=today)

    def health_check(self) -> dict:
        cached = load_cached_extract(cache_dir=self.cache_dir)
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "manual_upload",
            "cached_extract": cached.path.name if cached else None,
            "cached_extract_date": cached.extract_date.isoformat() if cached and cached.extract_date else None,
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
            return

        reference = self.today or datetime.now(timezone.utc).date()
        age_days = (reference - self.dataset.extract_date).days
        if age_days < 0:
            self.prepare_warnings.append(
                f"The uploaded SAM extract is dated in the future ({self.dataset.extract_date.isoformat()}); no-match results are treated as partial."
            )
        elif age_days > MAX_FRESH_AGE_DAYS:
            self.prepare_warnings.append(
                f"The uploaded SAM extract {self.dataset.path.name} is {age_days} days old; no-match results are treated as partial until a current extract is uploaded."
            )
