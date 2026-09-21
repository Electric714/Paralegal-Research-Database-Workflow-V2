from __future__ import annotations

import re
from typing import Any

from ..models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource

PUBLIC_WCCA_URL = "https://wcca.wicourts.gov/index.xsl"
ACQUISITION_METHOD = "operator_assisted_public_wcca"


def _canonical(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _search_names(contractor: ContractorContext) -> list[str]:
    values = [contractor.contractor_name]
    if contractor.related_companies:
        # Commas are common inside legal names, so only explicit separators split aliases.
        values.extend(re.split(r"[;|\n]+", contractor.related_companies))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value or "").strip())
        key = _canonical(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def build_search_plan(contractor: ContractorContext) -> dict[str, Any]:
    locations: list[str] = []
    primary = ", ".join(part for part in [contractor.address_1, contractor.city, contractor.state, contractor.zip] if part)
    secondary = ", ".join(
        part
        for part in [
            contractor.additional_address,
            contractor.additional_address_city,
            contractor.additional_address_state,
            contractor.additional_address_zip,
        ]
        if part
    )
    if primary:
        locations.append(primary)
    if secondary and secondary not in locations:
        locations.append(secondary)
    return {
        "bidder_id": contractor.internal_id,
        "external_id": contractor.external_id,
        "contractor_name": contractor.contractor_name,
        "search_names": _search_names(contractor),
        "locations": locations,
        "public_url": PUBLIC_WCCA_URL,
        "instructions": (
            "Search each listed business/party name in the statewide WCCA public search. "
            "Complete any CAPTCHA manually. Record confirmed cases or explicitly mark the search incomplete."
        ),
    }


def build_operator_result(
    contractor: ContractorContext,
    *,
    searched_names: list[str],
    outcome: str,
    cases: list[dict[str, Any]] | None = None,
    operator_note: str | None = None,
    operator_confirmed_complete: bool = False,
    identity_confirmed: bool = False,
) -> SourceResult:
    plan = build_search_plan(contractor)
    expected = {_canonical(name) for name in plan["search_names"]}
    searched = {_canonical(name) for name in searched_names if _canonical(name)}
    missing = [name for name in plan["search_names"] if _canonical(name) not in searched]
    all_names_searched = bool(expected) and expected.issubset(searched)
    complete = operator_confirmed_complete and all_names_searched
    normalized_outcome = outcome.strip().lower()
    case_rows = [dict(item) for item in (cases or []) if any(str(value or "").strip() for value in item.values())]

    warnings: list[str] = []
    if missing:
        warnings.append("Required WCCA search name(s) were not confirmed as searched: " + "; ".join(missing))
    if operator_confirmed_complete and not all_names_searched:
        warnings.append("Operator marked the search complete, but one or more planned names were not checked; result forced to partial.")

    if normalized_outcome == "findings":
        if not case_rows:
            warnings.append("Findings outcome was selected without a case record; result requires manual review.")
            status = SourceResultStatus.MANUAL_REVIEW_REQUIRED
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.PARTIAL
        elif not identity_confirmed:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        elif complete:
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            identity_status = IdentityStatus.CONFIRMED
            completeness = CompletenessStatus.COMPLETE
        else:
            status = SourceResultStatus.PARTIAL_RESULTS
            identity_status = IdentityStatus.CONFIRMED
            completeness = CompletenessStatus.PARTIAL
        observed_value = "Y"
    elif normalized_outcome == "no_match":
        identity_status = IdentityStatus.NOT_EVALUATED
        if complete:
            status = SourceResultStatus.SUCCESS_NO_MATCH
            completeness = CompletenessStatus.COMPLETE
        else:
            status = SourceResultStatus.PARTIAL_RESULTS
            completeness = CompletenessStatus.PARTIAL
            warnings.append("A WCCA no-match is clean only after every planned name is searched and completion is explicitly confirmed.")
        observed_value = "N"
    elif normalized_outcome == "ambiguous":
        status = SourceResultStatus.AMBIGUOUS_MATCH
        identity_status = IdentityStatus.REVIEW_REQUIRED
        completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        observed_value = None
    elif normalized_outcome == "blocked":
        status = SourceResultStatus.BLOCKED
        identity_status = IdentityStatus.NOT_EVALUATED
        completeness = CompletenessStatus.PARTIAL
        observed_value = None
    elif normalized_outcome == "partial":
        status = SourceResultStatus.PARTIAL_RESULTS
        identity_status = IdentityStatus.CONFIRMED if identity_confirmed else IdentityStatus.NOT_EVALUATED
        completeness = CompletenessStatus.PARTIAL
        observed_value = "Y" if case_rows and identity_confirmed else None
    else:
        raise ValueError("WCCA outcome must be findings, no_match, ambiguous, partial, or blocked.")

    source_record_id = None
    for item in case_rows:
        case_number = str(item.get("case_number") or "").strip()
        if case_number:
            source_record_id = case_number
            break

    evidence: list[EvidenceRecord] = []
    if observed_value is not None:
        evidence.append(
            EvidenceRecord(
                field_name="circuit_court",
                observed_value=observed_value,
                source_record_id=source_record_id,
                source_url=PUBLIC_WCCA_URL,
                details={
                    "comparison_only": True,
                    "proposal_blocked_reason": "Legacy circuit_court semantics have not yet been confirmed with the firm.",
                    "case_count": len(case_rows),
                    "searched_names": searched_names,
                    "missing_planned_names": missing,
                },
            )
        )

    payload = {
        "search_plan": plan,
        "searched_names": searched_names,
        "missing_planned_names": missing,
        "operator_confirmed_complete": operator_confirmed_complete,
        "identity_confirmed": identity_confirmed,
        "outcome": normalized_outcome,
        "cases": case_rows,
        "operator_note": operator_note or "",
        "field_semantics": {
            "circuit_court": "comparison_only_pending_firm_confirmation",
            "ccap_show150": "undefined_no_write",
        },
    }

    return SourceResult(
        source_key="wcca",
        contractor_id=contractor.internal_id,
        status=status,
        identity_status=identity_status,
        completeness_status=completeness,
        identity_confidence=1.0 if identity_status == IdentityStatus.CONFIRMED else None,
        searched_name=contractor.contractor_name,
        searched_address=contractor.address_1 or None,
        evidence=evidence,
        warnings=warnings,
        normalized_payload=payload,
        source_record_id=source_record_id,
        source_url=PUBLIC_WCCA_URL,
        acquisition_method=ACQUISITION_METHOD,
        adapter_version="0.1.0",
        parser_version="operator-v1",
    )


class WccaOperatorAssistedSource(ResearchSource):
    source_key = "wcca"
    display_name = "Wisconsin Circuit Court Access / CCAP"
    adapter_version = "0.1.0"
    parser_version = "operator-v1"

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "status": "operator_assisted",
            "implemented": True,
            "public_url": PUBLIC_WCCA_URL,
            "acquisition_method": ACQUISITION_METHOD,
        }

    def search(self, contractor: ContractorContext) -> SourceResult:
        plan = build_search_plan(contractor)
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.UNKNOWN,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1 or None,
                warnings=[
                    "WCCA public-site interaction requires an operator. Use the guided WCCA workbench; do not bypass CAPTCHA or scraping controls."
                ],
                normalized_payload={"search_plan": plan},
                source_url=PUBLIC_WCCA_URL,
                acquisition_method=ACQUISITION_METHOD,
            )
        )
