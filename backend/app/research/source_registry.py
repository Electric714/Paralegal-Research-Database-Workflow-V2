from __future__ import annotations

from .sources.base import ResearchSource
from .sources.sam_uploaded import SamUploadedExclusionsSource
from .sources.wdfi import WdfiCorporateRecordsSource


SOURCE_ADAPTERS: dict[str, type[ResearchSource]] = {
    "sam": SamUploadedExclusionsSource,
    "wdfi": WdfiCorporateRecordsSource,
}


def implemented_source_keys() -> frozenset[str]:
    return frozenset(SOURCE_ADAPTERS)


def create_source(source_key: str) -> ResearchSource | None:
    adapter = SOURCE_ADAPTERS.get(source_key)
    return adapter() if adapter else None
