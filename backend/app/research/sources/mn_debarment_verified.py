from __future__ import annotations

from ..matching import normalize_company_name
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext
from .mn_debarment import (
    MINNESOTA_DEBARMENT_URL,
    MinnesotaDebarredVendorsSource,
    MinnesotaVendorRecord,
    _action_status,
    _aliases,
)


class VerifiedMinnesotaDebarredVendorsSource(MinnesotaDebarredVendorsSource):
    """Minnesota master-list lookup that mirrors the manual verification workflow.

    The official OSP page is already a complete master list. Once ``prepare()`` has
    validated that the entire list was downloaded and parsed, bidder verification is
    therefore a direct name-list comparison:

    * exact normalized bidder/approved-alias name on the list -> retain the source record
    * no exact normalized approved name on the complete list -> verified clean no-match

    Addresses are retained as evidence but do not block an exact listed-name finding.
    Similar-but-different names do not create an ambiguous hit merely because they look
    alike (for example ``#1 Transportation LLC`` vs ``A1 Transportation LLC``).
    """

    adapter_version = "1.1.0"

    def search(self, contractor: ContractorContext) -> SourceResult:
        if self.prepare_failure is not None or not self.records:
            return self._failure_result(contractor)

        aliases = _aliases(contractor)
        normalized_aliases = {
            normalize_company_name(alias): alias
            for alias in aliases
            if normalize_company_name(alias)
        }
        artifacts = [self.artifact] if self.artifact else []

        matches: list[tuple[MinnesotaVendorRecord, str, str | None]] = []
        for record in self.records:
            normalized_record_name = normalize_company_name(record.match_name)
            matched_alias = normalized_aliases.get(normalized_record_name)
            if not matched_alias:
                continue
            judgment = self._remembered_judgment(contractor.internal_id, record.source_record_id)
            if judgment == "DIFFERENT_ENTITY":
                continue
            matches.append((record, matched_alias, judgment))

        if not matches:
            return SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.SUCCESS_NO_MATCH,
                identity_status=IdentityStatus.REJECTED,
                completeness_status=CompletenessStatus.COMPLETE,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                artifacts=artifacts,
                normalized_payload={
                    "verification_status": "VERIFIED_NOT_LISTED",
                    "verification_basis": "complete_official_master_list_exact_normalized_name_comparison",
                    "dataset_record_count": len(self.records),
                    "searched_names": aliases,
                    "candidate_count": 0,
                    "matched_names": [],
                    "top_candidates": [],
                },
                source_url=MINNESOTA_DEBARMENT_URL,
                http_status=self.http_status,
                acquisition_method="official_html_master_list_name_verification",
                adapter_version=self.adapter_version,
                parser_version=self.parser_version,
            )

        unresolved_individuals = [
            (record, matched_alias)
            for record, matched_alias, judgment in matches
            if record.entity_type == "individual" and judgment != "SAME_ENTITY"
        ]
        if unresolved_individuals:
            return SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.AMBIGUOUS_MATCH,
                identity_status=IdentityStatus.REVIEW_REQUIRED,
                completeness_status=CompletenessStatus.COMPLETE,
                identity_confidence=1.0,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=[
                    "An exact approved name appears on the Minnesota list as an individual-person record; identity review is required before treating it as the bidder company."
                ],
                artifacts=artifacts,
                normalized_payload={
                    "verification_status": "LISTED_NAME_REQUIRES_IDENTITY_REVIEW",
                    "verification_basis": "exact_normalized_name_comparison",
                    "dataset_record_count": len(self.records),
                    "searched_names": aliases,
                    "candidate_count": len(matches),
                    "matched_names": [record.raw_name for record, _, _ in matches],
                    "top_candidates": [
                        {
                            "source_record_id": record.source_record_id,
                            "score": 1.0,
                            "name_score": 1.0,
                            "address_score": 0.0,
                            "city_score": 0.0,
                            "state_score": 0.0,
                            "matched_search_name": matched_alias,
                            "matched_record_name": record.raw_name,
                            "exact_name": True,
                            "location_corroborated": False,
                            "auto_confirmable": False,
                            "record": record.as_dict(),
                        }
                        for record, matched_alias, _ in matches
                    ],
                },
                source_url=MINNESOTA_DEBARMENT_URL,
                http_status=self.http_status,
                acquisition_method="official_html_master_list_name_verification",
                adapter_version=self.adapter_version,
                parser_version=self.parser_version,
            )

        record_details: list[dict[str, object]] = []
        active_debarments: list[dict[str, object]] = []
        for record, matched_alias, _ in matches:
            action_status = _action_status(record, self.today)
            detail = {
                **record.as_dict(),
                "matched_search_name": matched_alias,
                "match_basis": "exact_normalized_name",
                "current_action_status": action_status,
            }
            record_details.append(detail)
            if action_status == "ACTIVE_DEBARMENT":
                active_debarments.append(detail)

        observed = "Y" if active_debarments else None
        evidence = [
            EvidenceRecord(
                field_name="state_federal_debarment",
                observed_value=observed,
                source_record_id=matches[0][0].source_record_id if len(matches) == 1 else None,
                source_url=MINNESOTA_DEBARMENT_URL,
                details={
                    "confirmed_records": record_details,
                    "active_debarment_count": len(active_debarments),
                    "match_basis": "exact normalized bidder/approved-alias name on complete Minnesota master list",
                    "semantics": (
                        "An exact approved name on the complete Minnesota master list is retained as a finding. "
                        "Y is proposed only for an explicitly dated Minnesota debarment that is active on the research date."
                    ),
                },
            )
        ]

        warnings: list[str] = []
        if any(item["current_action_status"] == "SOURCE_LABEL_CONFLICT" for item in record_details):
            warnings.append(
                "Minnesota source text says debarred but supplies suspension dates rather than an explicit debarment date; retained as evidence only."
            )

        return SourceResult(
            source_key=self.source_key,
            contractor_id=contractor.internal_id,
            status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
            identity_status=IdentityStatus.CONFIRMED,
            completeness_status=CompletenessStatus.COMPLETE,
            identity_confidence=1.0,
            searched_name=contractor.contractor_name,
            searched_address=contractor.address_1,
            evidence=evidence,
            warnings=warnings,
            artifacts=artifacts,
            normalized_payload={
                "verification_status": "LISTED_ON_SOURCE",
                "verification_basis": "exact_normalized_name_comparison",
                "dataset_record_count": len(self.records),
                "searched_names": aliases,
                "candidate_count": len(matches),
                "matched_names": [record.raw_name for record, _, _ in matches],
                "confirmed_records": record_details,
                "active_debarment_count": len(active_debarments),
                "top_candidates": [],
            },
            source_record_id=matches[0][0].source_record_id if len(matches) == 1 else None,
            source_url=MINNESOTA_DEBARMENT_URL,
            http_status=self.http_status,
            acquisition_method="official_html_master_list_name_verification",
            adapter_version=self.adapter_version,
            parser_version=self.parser_version,
        )
