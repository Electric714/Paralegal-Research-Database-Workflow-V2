from __future__ import annotations

import re
from typing import Any

from ..models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource

PUBLIC_WCCA_URL = "https://wcca.wicourts.gov/index.xsl"
ACQUISITION_METHOD = "operator_assisted_public_wcca"
CORE_CASE_FIELDS = ("case_number", "matched_party_name")
CASE_FIELDS = (
    "case_number",
    "county",
    "matched_party_name",
    "case_type",
    "case_status",
    "filing_date",
    "disposition",
    "case_url",
    "note",
)


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


def _normalize_cases(cases: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for raw in cases or []:
        row = {field: re.sub(r"\s+", " ", str(raw.get(field) or "").strip()) for field in CASE_FIELDS}
        if any(row.values()):
            result.append(row)
    return result


def _case_has_core_identifiers(case: dict[str, str]) -> bool:
    return all(case.get(field, "").strip() for field in CORE_CASE_FIELDS)


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
            "Complete any CAPTCHA manually. Record confirmed cases or explicitly mark the search incomplete. "
            "A public WCCA no-match is not proof that no circuit-court record exists."
        ),
    }


def _case_evidence(case_rows: list[dict[str, str]], *, identity_confirmed: bool) -> list[EvidenceRecord]:
    evidence: list[EvidenceRecord] = []
    for case in case_rows:
        if not case.get("case_number"):
            continue
        details = {
            "matched_party_name": case.get("matched_party_name", ""),
            "county": case.get("county", ""),
            "case_type": case.get("case_type", ""),
            "case_status": case.get("case_status", ""),
            "filing_date": case.get("filing_date", ""),
            "disposition": case.get("disposition", ""),
            "note": case.get("note", ""),
            "identity_confirmed": identity_confirmed,
            "comparison_only": True,
        }
        evidence.append(
            EvidenceRecord(
                field_name="wcca_case",
                observed_value=case["case_number"],
                source_record_id=case["case_number"],
                source_url=case.get("case_url") or PUBLIC_WCCA_URL,
                details=details,
            )
        )
    return evidence


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
    case_rows = _normalize_cases(cases)
    core_complete = bool(case_rows) and all(_case_has_core_identifiers(case) for case in case_rows)

    warnings: list[str] = []
    if missing:
        warnings.append("Required WCCA search name(s) were not confirmed as searched: " + "; ".join(missing))
    if operator_confirmed_complete and not all_names_searched:
        warnings.append("Operator marked the search complete, but one or more planned names were not checked; result forced to partial.")

    field_observation: str | None = None
    evidence: list[EvidenceRecord] = []

    if normalized_outcome == "findings":
        evidence.extend(_case_evidence(case_rows, identity_confirmed=identity_confirmed))
        if not case_rows:
            warnings.append("Findings outcome was selected without a case record; result requires manual review.")
            status = SourceResultStatus.MANUAL_REVIEW_REQUIRED
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.PARTIAL
        elif not core_complete:
            warnings.append(
                "A confirmed WCCA finding requires both a case number and matched party/business name for every recorded case."
            )
            status = SourceResultStatus.MANUAL_REVIEW_REQUIRED
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.PARTIAL
        elif not identity_confirmed:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        else:
            identity_status = IdentityStatus.CONFIRMED
            completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS if complete else SourceResultStatus.PARTIAL_RESULTS
            field_observation = "Y"
            evidence.insert(
                0,
                EvidenceRecord(
                    field_name="circuit_court",
                    observed_value="Y",
                    source_record_id=case_rows[0]["case_number"],
                    source_url=case_rows[0].get("case_url") or PUBLIC_WCCA_URL,
                    details={
                        "comparison_only": True,
                        "proposal_blocked_reason": (
                            "Legacy circuit_court semantics have not yet been confirmed with the firm."
                        ),
                        "case_count": len(case_rows),
                        "searched_names": searched_names,
                        "missing_planned_names": missing,
                        "positive_only_rule": True,
                    },
                ),
            )
    elif normalized_outcome == "no_match":
        identity_status = IdentityStatus.NOT_EVALUATED
        if complete:
            status = SourceResultStatus.SUCCESS_NO_MATCH
            completeness = CompletenessStatus.COMPLETE
            evidence.append(
                EvidenceRecord(
                    field_name="wcca_public_search",
                    observed_value="NO_CURRENTLY_DISPLAYED_MATCH",
                    source_url=PUBLIC_WCCA_URL,
                    details={
                        "searched_names": searched_names,
                        "operator_confirmed_complete": True,
                        "does_not_establish_circuit_court_n": True,
                        "reason": (
                            "WCCA is not a complete court record and online display/access limitations mean a public no-match "
                            "cannot safely be converted to circuit_court=N."
                        ),
                    },
                )
            )
        else:
            status = SourceResultStatus.PARTIAL_RESULTS
            completeness = CompletenessStatus.PARTIAL
            warnings.append("A WCCA no-match is complete only after every planned name is searched and completion is explicitly confirmed.")
        warnings.append(
            "No currently displayed WCCA match does not prove that the contractor has no historical, sealed, redacted, non-displayed, or otherwise unavailable circuit-court record."
        )
    elif normalized_outcome == "ambiguous":
        evidence.extend(_case_evidence(case_rows, identity_confirmed=False))
        status = SourceResultStatus.AMBIGUOUS_MATCH
        identity_status = IdentityStatus.REVIEW_REQUIRED
        completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
    elif normalized_outcome == "blocked":
        status = SourceResultStatus.BLOCKED
        identity_status = IdentityStatus.NOT_EVALUATED
        completeness = CompletenessStatus.PARTIAL
    elif normalized_outcome == "partial":
        evidence.extend(_case_evidence(case_rows, identity_confirmed=identity_confirmed))
        status = SourceResultStatus.PARTIAL_RESULTS
        identity_status = IdentityStatus.CONFIRMED if identity_confirmed and core_complete else IdentityStatus.NOT_EVALUATED
        completeness = CompletenessStatus.PARTIAL
        if identity_confirmed and case_rows and not core_complete:
            identity_status = IdentityStatus.REVIEW_REQUIRED
            warnings.append(
                "The partial search includes a claimed positive, but the case number and matched party/business name are required before identity can be treated as confirmed."
            )
        elif identity_confirmed and core_complete:
            field_observation = "Y"
            evidence.insert(
                0,
                EvidenceRecord(
                    field_name="circuit_court",
                    observed_value="Y",
                    source_record_id=case_rows[0]["case_number"],
                    source_url=case_rows[0].get("case_url") or PUBLIC_WCCA_URL,
                    details={
                        "comparison_only": True,
                        "case_count": len(case_rows),
                        "search_incomplete": True,
                        "positive_only_rule": True,
                    },
                ),
            )
    else:
        raise ValueError("WCCA outcome must be findings, no_match, ambiguous, partial, or blocked.")

    source_record_id = next((case.get("case_number") for case in case_rows if case.get("case_number")), None)
    payload = {
        "search_plan": plan,
        "searched_names": searched_names,
        "missing_planned_names": missing,
        "operator_confirmed_complete": operator_confirmed_complete,
        "identity_confirmed": identity_confirmed,
        "outcome": normalized_outcome,
        "cases": case_rows,
        "operator_note": operator_note or "",
        "field_observation": field_observation,
        "field_semantics": {
            "circuit_court": "positive_only_comparison_pending_firm_confirmation",
            "ccap_show150": "undefined_no_write",
            "public_no_match": "source_level_only_not_master_negative",
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
        adapter_version="0.2.0",
        parser_version="operator-v2",
    )


class WccaOperatorAssistedSource(ResearchSource):
    source_key = "wcca"
    display_name = "Wisconsin Circuit Court Access / CCAP"
    adapter_version = "0.2.0"
    parser_version = "operator-v2"

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "status": "operator_assisted",
            "implemented": True,
            "public_url": PUBLIC_WCCA_URL,
            "acquisition_method": ACQUISITION_METHOD,
            "negative_field_updates": False,
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
                adapter_version=self.adapter_version,
                parser_version=self.parser_version,
            )
        )
