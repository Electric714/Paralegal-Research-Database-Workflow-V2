from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz, process

from ..matching import normalize_company_name, normalize_text
from ..models import SourceResultStatus
from .base import ContractorContext
from .sam_exclusions import (
    MAX_FRESH_AGE_DAYS,
    CandidateMatch,
    SamDownloadError,
    SamExclusionRecord,
    SamExclusionsSource,
    load_cached_extract,
)


FUZZY_REVIEW_CUTOFF = 94.0
GLOBAL_FUZZY_REVIEW_CUTOFF = 97.0


def _zip5(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[:5] if len(digits) >= 5 else ""


def _street_number(value: str) -> str:
    normalized = normalize_text(value)
    first = normalized.split(" ", 1)[0] if normalized else ""
    return first if any(char.isdigit() for char in first) else ""


def _location_corroborates(
    contractor: ContractorContext,
    record: SamExclusionRecord,
    match: CandidateMatch,
) -> bool:
    """Require meaningful address evidence, not merely a shared city/state/ZIP."""

    master_state = normalize_text(contractor.state)
    candidate_state = normalize_text(record.state)
    if master_state and candidate_state and master_state != candidate_state:
        return False

    master_city = normalize_text(contractor.city)
    candidate_city = normalize_text(record.city)
    city_compatible = not (master_city and candidate_city) or master_city == candidate_city

    master_zip = _zip5(contractor.zip)
    candidate_zip = _zip5(record.zip_code)
    zip_match = bool(master_zip and candidate_zip and master_zip == candidate_zip)

    master_number = _street_number(contractor.address_1)
    candidate_number = _street_number(record.address_1)
    street_number_match = bool(
        master_number and candidate_number and master_number == candidate_number
    )
    street_number_compatible = (
        not (master_number and candidate_number) or street_number_match
    )

    address_match = (
        bool(contractor.address_1 and record.address_1)
        and match.address_score >= 0.90
        and street_number_compatible
    )

    # ZIP alone is not enough: two unrelated companies can share a ZIP and city.
    # A ZIP-supported confirmation also needs the same street number plus a reasonably
    # similar normalized address.
    zip_supported_address_match = (
        zip_match
        and city_compatible
        and street_number_match
        and match.address_score >= 0.75
    )

    return address_match or zip_supported_address_match


class SamUploadedExclusionsSource(SamExclusionsSource):
    """SAM exclusions adapter for a locally uploaded official Public V2 extract."""

    adapter_version = "1.4.0"

    def __init__(self, *, cache_dir: Path | None = None, today: date | None = None) -> None:
        super().__init__(cache_dir=cache_dir, today=today)

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

    def _candidate_records(self, contractor: ContractorContext) -> list[SamExclusionRecord]:
        """Prefer exact approved names; fuzzy candidates must be typo-level similar."""

        names = [contractor.contractor_name]
        if contractor.related_companies.strip():
            names.extend(
                part.strip()
                for part in re.split(r"[;|\n]+", contractor.related_companies)
                if part.strip()
            )

        record_ids: set[str] = set()
        result: list[SamExclusionRecord] = []

        def add_name(normalized_name: str) -> None:
            for record in self._name_to_records.get(normalized_name, []):
                if record.record_id not in record_ids:
                    record_ids.add(record.record_id)
                    result.append(record)

        normalized_names = [normalize_company_name(name) for name in names]
        normalized_names = [name for name in normalized_names if name]
        for normalized in normalized_names:
            add_name(normalized)

        # Exact approved names/aliases are important enough to review even if SAM's
        # stored address has changed, so do not bury them under fuzzy alternatives.
        if result:
            return result

        state = normalize_text(contractor.state)
        state_choices = self._name_choices_by_state.get(state, []) if state else []
        choices = state_choices or self._name_choices_global
        cutoff = FUZZY_REVIEW_CUTOFF if state_choices else GLOBAL_FUZZY_REVIEW_CUTOFF

        for normalized in normalized_names:
            if not choices:
                break
            for choice, similarity, _index in process.extract(
                normalized,
                choices,
                scorer=fuzz.WRatio,
                limit=10,
                score_cutoff=cutoff,
            ):
                if similarity >= cutoff:
                    add_name(choice)

        return result

    def _score_record(
        self,
        contractor: ContractorContext,
        record: SamExclusionRecord,
    ) -> CandidateMatch:
        match = super()._score_record(contractor, record)
        exact_name = (
            normalize_company_name(match.matched_search_name)
            == normalize_company_name(match.matched_record_name)
        )
        location_corroborated = _location_corroborates(contractor, record, match)

        # Only an exact approved company name/alias plus meaningful address evidence
        # can auto-confirm an exclusion.
        auto_confirmable = exact_name and location_corroborated

        score = match.score
        if exact_name and not location_corroborated:
            # Exact-name records must survive the parent's candidate floor so a moved
            # company or conflicting SAM address becomes review-required rather than
            # a false SUCCESS_NO_MATCH.
            score = max(score, 0.80)
        elif not exact_name:
            if match.name_score >= 0.94 and location_corroborated:
                # Near-exact names with independent address evidence remain visible,
                # but only for human review.
                score = max(score, 0.80)
            else:
                # Generic-word lookalikes are noise and should not enter review.
                score = min(score, 0.71)

        return CandidateMatch(
            record=match.record,
            score=score,
            name_score=match.name_score,
            address_score=match.address_score,
            city_score=match.city_score,
            state_score=match.state_score,
            matched_search_name=match.matched_search_name,
            matched_record_name=match.matched_record_name,
            auto_confirmable=auto_confirmable,
            remembered_judgment=match.remembered_judgment,
        )
