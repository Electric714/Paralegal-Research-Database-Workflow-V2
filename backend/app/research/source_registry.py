from __future__ import annotations

from .sources.base import ResearchSource
from .sources.osha import OshaEstablishmentSource
from .sources.sam_uploaded import SamUploadedExclusionsSource
from .sources.wcca import WccaOperatorAssistedSource
from .sources.wdfi import WdfiCorporateRecordsSource


SOURCE_ADAPTERS: dict[str, type[ResearchSource]] = {
    "osha": OshaEstablishmentSource,
    "sam": SamUploadedExclusionsSource,
    "wcca": WccaOperatorAssistedSource,
    "wdfi": WdfiCorporateRecordsSource,
}


def implemented_source_keys() -> frozenset[str]:
    return frozenset(SOURCE_ADAPTERS)


def create_source(source_key: str) -> ResearchSource | None:
    adapter = SOURCE_ADAPTERS.get(source_key)
    return adapter() if adapter else None
