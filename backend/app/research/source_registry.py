from __future__ import annotations

from .sources.base import ResearchSource
from .sources.bbb_resilient import ResilientBbbBusinessProfileSource
from .sources.dol_enforcement import DolEnforcementSource
from .sources.mn_debarment import MinnesotaDebarredVendorsSource
from .sources.mn_pca import MinnesotaPcaEnforcementSource
from .sources.osha_resilient import ResilientOshaEstablishmentSource
from .sources.responsible_mn import ResponsibleMinnesotaSource
from .sources.sam_uploaded import SamUploadedExclusionsSource
from .sources.violation_tracker import ViolationTrackerSource
from .sources.wcca import WccaOperatorAssistedSource
from .sources.wdfi import WdfiCorporateRecordsSource
from .sources.wisdot import WisdotContractorSource


SOURCE_ADAPTERS: dict[str, type[ResearchSource]] = {
    "bbb": ResilientBbbBusinessProfileSource,
    "dol_enforcement": DolEnforcementSource,
    "mn_debarment": MinnesotaDebarredVendorsSource,
    "mn_pca": MinnesotaPcaEnforcementSource,
    "osha": ResilientOshaEstablishmentSource,
    "responsible_mn": ResponsibleMinnesotaSource,
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
