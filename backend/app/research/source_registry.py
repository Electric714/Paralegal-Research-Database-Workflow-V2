from __future__ import annotations

from .sources.base import ResearchSource
from .sources.bbb import BbbBusinessProfileSource
from .sources.osha import OshaEstablishmentSource
from .sources.sam_uploaded import SamUploadedExclusionsSource
from .sources.wdfi import WdfiCorporateRecordsSource
from .sources.wisdot import WisdotContractorSource


SOURCE_ADAPTERS: dict[str, type[ResearchSource]] = {
    "bbb": BbbBusinessProfileSource,
    "osha": OshaEstablishmentSource,
    "sam": SamUploadedExclusionsSource,
    "wdfi": WdfiCorporateRecordsSource,
    "wisdot": WisdotContractorSource,
}


def implemented_source_keys() -> frozenset[str]:
    return frozenset(SOURCE_ADAPTERS)


def create_source(source_key: str) -> ResearchSource | None:
    adapter = SOURCE_ADAPTERS.get(source_key)
    return adapter() if adapter else None
