from __future__ import annotations

from .models import SourceResultStatus


# Operator retry is intentionally narrower than the set of all problem states.
# Retry transient/incomplete acquisition outcomes. Do not hammer sources when the
# result requires identity review, credentials, a parser/layout fix, or a human
# decision before another request would be meaningful.
RETRYABLE_STATUSES = frozenset(
    {
        SourceResultStatus.TIMEOUT.value,
        SourceResultStatus.HTTP_ERROR.value,
        SourceResultStatus.SOURCE_UNAVAILABLE.value,
        SourceResultStatus.SESSION_EXPIRED.value,
        SourceResultStatus.PARTIAL_RESULTS.value,
        SourceResultStatus.PAGINATION_INCOMPLETE.value,
    }
)


def is_retryable_status(status: str) -> bool:
    return str(status) in RETRYABLE_STATUSES
