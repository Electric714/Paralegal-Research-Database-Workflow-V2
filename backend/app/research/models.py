from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceResultStatus(StrEnum):
    SUCCESS_COMPLETE = "SUCCESS_COMPLETE"
    SUCCESS_NO_MATCH = "SUCCESS_NO_MATCH"
    SUCCESS_WITH_FINDINGS = "SUCCESS_WITH_FINDINGS"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    PARTIAL_RESULTS = "PARTIAL_RESULTS"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PARSER_FAILURE = "PARSER_FAILURE"
    LAYOUT_CHANGED = "LAYOUT_CHANGED"
    DATASET_MALFORMED = "DATASET_MALFORMED"
    PAGINATION_INCOMPLETE = "PAGINATION_INCOMPLETE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    NOT_CHECKED = "NOT_CHECKED"


class IdentityStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_EVALUATED = "NOT_EVALUATED"


class CompletenessStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceRecord(BaseModel):
    field_name: str
    observed_value: str | None = None
    source_record_id: str | None = None
    source_url: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RawArtifact(BaseModel):
    artifact_type: str
    relative_path: str | None = None
    sha256: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceResult(BaseModel):
    source_key: str
    contractor_id: int
    status: SourceResultStatus
    identity_status: IdentityStatus = IdentityStatus.NOT_EVALUATED
    completeness_status: CompletenessStatus = CompletenessStatus.UNKNOWN
    identity_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    searched_name: str
    searched_address: str | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[RawArtifact] = Field(default_factory=list)
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    source_record_id: str | None = None
    source_url: str | None = None
    http_status: int | None = None
    acquisition_method: str | None = None
    adapter_version: str = "0.1.0"
    parser_version: str = "0.1.0"

    @property
    def is_clean_negative(self) -> bool:
        return (
            self.status == SourceResultStatus.SUCCESS_NO_MATCH
            and self.completeness_status == CompletenessStatus.COMPLETE
        )
