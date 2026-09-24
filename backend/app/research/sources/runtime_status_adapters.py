from __future__ import annotations

from datetime import date

from ..models import CompletenessStatus, IdentityStatus, SourceResultStatus
from .base import ContractorContext
from .sam_uploaded import SamUploadedExclusionsSource
from .wisdot import WisdotContractorSource


def _local_calendar_date() -> date:
    """Return the operator machine's calendar date for local desktop freshness checks."""
    return date.today()


class OperationalSamUploadedExclusionsSource(SamUploadedExclusionsSource):
    """Expose the SAM lookup outcome separately from dataset freshness.

    A usable uploaded extract can still be incomplete/stale evidence. Keep
    ``completeness_status=PARTIAL`` so a no-match is never a clean negative, but do
    not mislabel every successfully executed bidder lookup as ``PARTIAL_RESULTS``.

    The app is a local desktop workflow, so freshness follows the operator machine's
    calendar date. This avoids an uploaded extract becoming stale several hours early
    in U.S. time zones merely because UTC crossed midnight.
    """

    adapter_version = "1.6.1"

    def prepare(self) -> None:
        if self.today is None:
            self.today = _local_calendar_date()
        super().prepare()

    def search(self, contractor: ContractorContext):
        result = super().search(contractor)
        if (
            result.status == SourceResultStatus.PARTIAL_RESULTS
            and result.completeness_status == CompletenessStatus.PARTIAL
            and self.dataset is not None
        ):
            if result.identity_status == IdentityStatus.REVIEW_REQUIRED:
                normalized_status = SourceResultStatus.AMBIGUOUS_MATCH
            elif result.evidence:
                normalized_status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            else:
                normalized_status = SourceResultStatus.SUCCESS_NO_MATCH

            result.status = normalized_status
            result.normalized_payload = {
                **result.normalized_payload,
                "operational_status_normalization": {
                    "from": SourceResultStatus.PARTIAL_RESULTS.value,
                    "to": normalized_status.value,
                    "reason": (
                        "The uploaded SAM extract was usable and the bidder lookup completed, "
                        "but evidence freshness/completeness remains partial."
                    ),
                    "clean_negative_allowed": False,
                },
            }
        return result


class OperationalWisdotContractorSource(WisdotContractorSource):
    """Keep supplemental WisDOT dataset problems from masking completed research.

    The debarment dataset is the core dataset for the master debarment comparison.
    If it is available, a missing/stale supplemental contractor or finals dataset
    remains a completeness caveat instead of turning every bidder into
    ``PARTIAL_RESULTS``.
    """

    adapter_version = "1.2.0"

    def search(self, contractor: ContractorContext):
        result = super().search(contractor)
        if (
            result.status == SourceResultStatus.PARTIAL_RESULTS
            and result.completeness_status == CompletenessStatus.PARTIAL
            and "debarment" in self.datasets
        ):
            normalized_status = (
                SourceResultStatus.SUCCESS_WITH_FINDINGS
                if result.evidence
                else SourceResultStatus.SUCCESS_NO_MATCH
            )
            result.status = normalized_status
            result.normalized_payload = {
                **result.normalized_payload,
                "operational_status_normalization": {
                    "from": SourceResultStatus.PARTIAL_RESULTS.value,
                    "to": normalized_status.value,
                    "reason": (
                        "The WisDOT debarment dataset was available and the bidder lookup completed; "
                        "one or more supplemental/live-refresh datasets were incomplete."
                    ),
                    "clean_negative_allowed": False,
                },
            }
            warning = (
                "WisDOT debarment research completed, but one or more supplemental/live-refresh "
                "datasets were incomplete; completeness remains PARTIAL."
            )
            if warning not in result.warnings:
                result.warnings = [*result.warnings, warning]
        return result
