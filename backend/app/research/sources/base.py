from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..models import SourceResult


@dataclass(frozen=True)
class ContractorContext:
    internal_id: int
    external_id: str
    contractor_name: str
    related_companies: str = ""
    address_1: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    additional_address: str = ""
    additional_address_city: str = ""
    additional_address_state: str = ""
    additional_address_zip: str = ""
    dfi: str = ""


class ResearchSource(ABC):
    """Outer contract every source adapter must follow.

    Source-specific code may fetch data however it needs to, but it must return a
    SourceResult and must never update approved master bidder data directly.
    """

    source_key: str
    display_name: str
    adapter_version = "0.1.0"
    parser_version = "0.1.0"

    def health_check(self) -> dict[str, Any]:
        return {"source_key": self.source_key, "status": "unknown"}

    def prepare(self) -> None:
        """Optional one-time setup for cached datasets, sessions, etc."""

    @abstractmethod
    def search(self, contractor: ContractorContext) -> SourceResult:
        """Perform one contractor lookup and return a fully classified result."""
        raise NotImplementedError

    def validate_result(self, result: SourceResult) -> SourceResult:
        if result.source_key != self.source_key:
            raise ValueError(
                f"Adapter {self.source_key!r} returned result for {result.source_key!r}."
            )
        if result.adapter_version != self.adapter_version:
            result.adapter_version = self.adapter_version
        if result.parser_version != self.parser_version:
            result.parser_version = self.parser_version
        return result
