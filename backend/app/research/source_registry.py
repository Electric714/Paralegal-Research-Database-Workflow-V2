from __future__ import annotations

from .sources.base import ResearchSource
from .sources.bbb_resilient import ResilientBbbBusinessProfileSource
from .sources.dol_enforcement import DolEnforcementSource
from .sources.mn_debarment_verified import VerifiedMinnesotaDebarredVendorsSource
from .sources.mn_pca import MinnesotaPcaEnforcementSource
from .sources.osha_resilient import ResilientOshaEstablishmentSource
from .sources.responsible_mn import ResponsibleMinnesotaSource
from .sources.runtime_status_adapters import (
    OperationalSamUploadedExclusionsSource,
    OperationalWisdotContractorSource,
)
from .sources.violation_tracker import ViolationTrackerSource
from .sources.wcca import WccaOperatorAssistedSource
from .sources.wdfi import WdfiCorporateRecordsSource


SOURCE_ADAPTERS: dict[str, type[ResearchSource]] = {
    "bbb": ResilientBbbBusinessProfileSource,
    "dol_enforcement": DolEnforcementSource,
    "mn_debarment": VerifiedMinnesotaDebarredVendorsSource,
    "mn_pca": MinnesotaPcaEnforcementSource,
    "osha": ResilientOshaEstablishmentSource,
    "responsible_mn": ResponsibleMinnesotaSource,
    "sam": OperationalSamUploadedExclusionsSource,
    "violation_tracker": ViolationTrackerSource,
    "wcca": WccaOperatorAssistedSource,
    "wdfi": WdfiCorporateRecordsSource,
    "wisdot": OperationalWisdotContractorSource,
}


def implemented_source_keys() -> frozenset[str]:
    return frozenset(SOURCE_ADAPTERS)


def create_source(source_key: str) -> ResearchSource | None:
    adapter = SOURCE_ADAPTERS.get(source_key)
    return adapter() if adapter else None
