from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from ..models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from .base import ContractorContext, ResearchSource

PUBLIC_WCCA_URL = "https://wcca.wicourts.gov/index.xsl"
OFFICIAL_WCCA_HOSTS = frozenset({"wcca.wicourts.gov"})
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


def _identity_key(value: str) -> str:
    """Conservative punctuation-insensitive key used only for planned-name scope checks."""
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _case_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


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


def _official_case_url(value: str) -> tuple[str, str | None]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return "", None
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return "", "Invalid WCCA case URL was discarded."
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or host not in OFFICIAL_WCCA_HOSTS:
        return "", "Non-official WCCA case URL was discarded; only https://wcca.wicourts.gov links are retained."
    return cleaned, None


def _normalize_cases(cases: list[dict[str, Any]] | None) -> tuple[list[dict[str, str]], list[str], bool]:
    result: list[dict[str, str]] = []
    warnings: list[str] = []
    seen: dict[str, dict[str, str]] = {}
    integrity_ok = True

    for raw in cases or []:
        row = {field: re.sub(r"\s+", " ", str(raw.get(field) or "").strip()) for field in CASE_FIELDS}
        if not any(row.values()):
            continue

        case_url, url_warning = _official_case_url(row.get("case_url", ""))
        row["case_url"] = case_url
        if url_warning:
            warnings.append(url_warning)

        key = _case_key(row.get("case_number", ""))
        if key and key in seen:
            previous = seen[key]
            same_party = _identity_key(previous.get("matched_party_name", "")) == _identity_key(row.get("matched_party_name", ""))
            if same_party:
                warnings.append(f"Duplicate WCCA case {row['case_number']} was entered more than once; duplicate evidence was ignored.")
                continue
            integrity_ok = False
            warnings.append(
                f"Conflicting WCCA entries use the same case number {row['case_number']} for different party names; manual review is required."
            )
        elif key:
            seen[key] = row
        result.append(row)

    return result, warnings, integrity_ok


def _case_has_core_identifiers(case: dict[str, str]) -> bool:
    return all(case.get(field, "").strip() for field in CORE_CASE_FIELDS)


def _case_parties_within_search_scope(case_rows: list[dict[str, str]], planned_names: list[str]) -> bool:
    planned = {_identity_key(name) for name in planned_names if _identity_key(name)}
    if not case_rows or not planned:
        return False
    return all(_identity_key(case.get("matched_party_name", "")) in planned for case in case_rows)


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
        "scope": "approved_bidder_and_explicit_related_company_aliases_only",
        "instructions": (
            "Search each listed business/party name in the statewide WCCA public search. "
            "Complete any CAPTCHA manually. Record confirmed cases or explicitly mark the search incomplete. "
            "A public WCCA no-match is not proof that no circuit-court record exists."
        ),
    }


def _case_evidence(
    case_rows: list[dict[str, str]],
    *,
    identity_confirmed: bool,
    planned_names: list[str],
) -> list[EvidenceRecord]:
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
            "matched_party_within_planned_scope": _identity_key(case.get("matched_party_name", ""))
            in {_identity_key(name) for name in planned_names},
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
    unexpected = [name for name in searched_names if _canonical(name) and _canonical(name) not in expected]
    all_names_searched = bool(expected) and expected.issubset(searched)
    complete = operator_confirmed_complete and all_names_searched
    normalized_outcome = outcome.strip().lower()
    case_rows, case_warnings, case_integrity_ok = _normalize_cases(cases)
    core_complete = bool(case_rows) and all(_case_has_core_identifiers(case) for case in case_rows)
    parties_in_scope = _case_parties_within_search_scope(case_rows, plan["search_names"])

    warnings: list[str] = list(case_warnings)
    if missing:
        warnings.append("Required WCCA search name(s) were not confirmed as searched: " + "; ".join(missing))
    if unexpected:
        warnings.append(
            "Search name(s) outside the approved bidder/related-company scope were ignored for completeness: "
            + "; ".join(unexpected)
        )
    if operator_confirmed_complete and not all_names_searched:
        warnings.append("Operator marked the search complete, but one or more planned names were not checked; result forced to partial.")

    field_observation: str | None = None
    evidence: list[EvidenceRecord] = []

    if normalized_outcome == "findings":
        evidence.extend(
            _case_evidence(case_rows, identity_confirmed=identity_confirmed, planned_names=plan["search_names"])
        )
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
        elif not case_integrity_ok:
            status = SourceResultStatus.MANUAL_REVIEW_REQUIRED
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.PARTIAL
        elif not identity_confirmed:
            status = SourceResultStatus.AMBIGUOUS_MATCH
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        elif not parties_in_scope:
            warnings.append(
                "A recorded WCCA party name falls outside the approved bidder/related-company search scope; identity must be reviewed before positive circuit_court evidence is accepted."
            )
            status = SourceResultStatus.MANUAL_REVIEW_REQUIRED
            identity_status = IdentityStatus.REVIEW_REQUIRED
            completeness = CompletenessStatus.PARTIAL
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
        evidence.extend(_case_evidence(case_rows, identity_confirmed=False, planned_names=plan["search_names"]))
        status = SourceResultStatus.AMBIGUOUS_MATCH
        identity_status = IdentityStatus.REVIEW_REQUIRED
        completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
    elif normalized_outcome == "blocked":
        status = SourceResultStatus.BLOCKED
        identity_status = IdentityStatus.NOT_EVALUATED
        completeness = CompletenessStatus.PARTIAL
    elif normalized_outcome == "partial":
        evidence.extend(
            _case_evidence(case_rows, identity_confirmed=identity_confirmed, planned_names=plan["search_names"])
        )
        status = SourceResultStatus.PARTIAL_RESULTS
        identity_status = IdentityStatus.CONFIRMED if identity_confirmed and core_complete else IdentityStatus.NOT_EVALUATED
        completeness = CompletenessStatus.PARTIAL
        if identity_confirmed and case_rows and not core_complete:
            identity_status = IdentityStatus.REVIEW_REQUIRED
            warnings.append(
                "The partial search includes a claimed positive, but the case number and matched party/business name are required before identity can be treated as confirmed."
            )
        elif identity_confirmed and not case_integrity_ok:
            identity_status = IdentityStatus.REVIEW_REQUIRED
        elif identity_confirmed and core_complete and not parties_in_scope:
            identity_status = IdentityStatus.REVIEW_REQUIRED
            warnings.append(
                "The partial search includes a claimed positive outside the approved bidder/related-company search scope; positive circuit_court evidence was withheld."
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
        "unexpected_search_names": unexpected,
        "operator_confirmed_complete": operator_confirmed_complete,
        "identity_confirmed": identity_confirmed,
        "outcome": normalized_outcome,
        "cases": case_rows,
        "operator_note": operator_note or "",
        "field_observation": field_observation,
        "integrity": {
            "case_records_consistent": case_integrity_ok,
            "matched_parties_within_planned_scope": parties_in_scope if case_rows else None,
            "official_case_urls_only": True,
        },
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
        adapter_version="1.0.0",
        parser_version="operator-v3",
    )


class WccaCcapSource(ResearchSource):
    """Complete public-WCCA adapter contract.

    The transport is intentionally operator-assisted because the public WCCA site uses
    CAPTCHA/anti-scraping controls. The adapter becomes complete when the operator
    finishes the bounded workbench search; unattended automation requires the official
    CCAP subscription REST interface and is deliberately not emulated here.
    """

    source_key = "wcca"
    display_name = "Wisconsin Circuit Court Access / CCAP"
    adapter_version = "1.0.0"
    parser_version = "operator-v3"

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "status": "ready_operator_assisted",
            "implemented": True,
            "public_url": PUBLIC_WCCA_URL,
            "acquisition_method": ACQUISITION_METHOD,
            "completion_path": "wcca_workbench",
            "public_site_automation": False,
            "official_rest_subscription_supported": True,
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
                    "WCCA public-site interaction requires an operator. Complete the guided WCCA workbench; do not bypass CAPTCHA or scraping controls."
                ],
                normalized_payload={"search_plan": plan, "completion_path": "/wcca-workbench.html"},
                source_url=PUBLIC_WCCA_URL,
                acquisition_method=ACQUISITION_METHOD,
                adapter_version=self.adapter_version,
                parser_version=self.parser_version,
            )
        )


# Backward-compatible import for older tests/modules while the registry uses the new name.
WccaOperatorAssistedSource = WccaCcapSource
