from __future__ import annotations

from .sources.base import ResearchSource
from .sources.bbb import BbbBusinessProfileSource
from .sources.dol_enforcement import DolEnforcementSource
from .sources.mn_debarment import MinnesotaDebarredVendorsSource
from .sources.mn_pca import MinnesotaPcaEnforcementSource
from .sources.osha import OshaEstablishmentSource
from .sources.sam_uploaded import SamUploadedExclusionsSource
from .sources.violation_tracker import ViolationTrackerSource
from .sources.wcca import WccaOperatorAssistedSource
from .sources.wdfi import WdfiCorporateRecordsSource
from .sources.wisdot import WisdotContractorSource


SOURCE_ADAPTERS: dict[str, type[ResearchSource]] = {
    "bbb": BbbBusinessProfileSource,
    "dol_enforcement": DolEnforcementSource,
    "mn_debarment": MinnesotaDebarredVendorsSource,
    "mn_pca": MinnesotaPcaEnforcementSource,
    "osha": OshaEstablishmentSource,
    "sam": SamUploadedExclusionsSource,
    "violation_tracker": ViolationTrackerSource,
    "wcca": WccaOperatorAssistedSource,
    "wdfi": WdfiCorporateRecordsSource,
    "wisdot": WisdotContractorSource,
}


def implemented_source_keys() -> frozenset[str]:
    return frozenset(SOURCE_ADAPTERS)


def create_source(source_key: str) -> ResearchSource | None:
    adapter = SOURCE_ADAPTERS.get(source_key)
    return adapter() if adapter else None
