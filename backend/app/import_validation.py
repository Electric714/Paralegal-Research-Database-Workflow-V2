from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


BOOLEAN_FIELDS = {
    "wc",
    "osha",
    "state_federal_debarment",
    "mndol_ineligibility",
    "public_works_projects_budget_time_quality_complaint",
    "federal_court",
    "circuit_court",
    "ccap_show150",
    "environmental_violations",
    "prevailing_wage_violations",
    "dwd",
    "dwd_substance_abuse_plan",
    "better_business_bureau_complaints",
    "misc_violations",
    "tax_liability",
}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _valid_date(value: str) -> bool:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def validate_bidder_rows(columns: list[str], rows: list[dict[str, str]]) -> ValidationReport:
    report = ValidationReport()
    seen_ids: dict[str, int] = {}
    seen_names: dict[str, int] = {}

    for index, row in enumerate(rows, start=2):
        external_id = row.get("id", "").strip()
        contractor_name = row.get("contractor_name", "").strip()

        if not external_id:
            report.errors.append(f"Row {index}: id is required.")
        elif external_id in seen_ids:
            report.errors.append(
                f"Row {index}: duplicate id {external_id!r}; first seen on row {seen_ids[external_id]}."
            )
        else:
            seen_ids[external_id] = index

        if not contractor_name:
            report.errors.append(f"Row {index}: contractor_name is required.")
        else:
            normalized_name = re.sub(r"[^a-z0-9]+", "", contractor_name.casefold())
            if normalized_name in seen_names:
                report.warnings.append(
                    f"Row {index}: contractor name looks duplicated; first seen on row {seen_names[normalized_name]}."
                )
            else:
                seen_names[normalized_name] = index

        for state_field in ("state", "additional_address_state"):
            value = row.get(state_field, "").strip()
            if value and not re.fullmatch(r"[A-Za-z]{2}", value):
                report.errors.append(f"Row {index}: {state_field} must be a two-letter state code or blank.")

        for zip_field in ("zip", "additional_address_zip"):
            value = row.get(zip_field, "").strip()
            if value and not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", value):
                report.warnings.append(
                    f"Row {index}: {zip_field} value {value!r} is not a standard 5-digit or ZIP+4 value; it will be preserved as text."
                )

        wc_date = row.get("wc_date", "").strip()
        if wc_date and not _valid_date(wc_date):
            report.errors.append(f"Row {index}: wc_date {wc_date!r} is not a recognized date.")

        for field_name in BOOLEAN_FIELDS.intersection(columns):
            value = row.get(field_name, "").strip().upper()
            if value and value not in {"Y", "N"}:
                report.errors.append(
                    f"Row {index}: {field_name} must be Y, N, or blank; found {row.get(field_name)!r}."
                )

    return report
