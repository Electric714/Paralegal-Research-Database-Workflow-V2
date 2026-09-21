from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceFieldMapping:
    source_key: str
    owned_fields: frozenset[str]
    notes: str = ""


# These mappings are intentionally conservative. A source may collect evidence that
# is useful to a paralegal without being allowed to propose a master-field change.
# Add ownership only after the firm's existing field semantics are confirmed.
SOURCE_FIELD_MAPPINGS: dict[str, SourceFieldMapping] = {
    "wdfi": SourceFieldMapping("wdfi", frozenset({"dfi"}), "Wisconsin DFI business-status field."),
    "osha": SourceFieldMapping(
        "osha",
        frozenset({"osha"}),
        "OSHA finding flag only; severe-violation and years semantics remain evidence-only pending firm confirmation.",
    ),
    "wcrb": SourceFieldMapping(
        "wcrb",
        frozenset({"wc"}),
        "Confirmed WCRB coverage may support the workers-compensation flag. wc_date ownership is intentionally withheld until the firm's field semantics are confirmed.",
    ),
    "wcca": SourceFieldMapping(
        "wcca",
        frozenset(),
        "WCCA evidence is comparison-only until the firm confirms the exact circuit_court and ccap_show150 legacy rules.",
    ),
    "pacer": SourceFieldMapping("pacer", frozenset({"federal_court"}), "Federal-court field."),
    "sam": SourceFieldMapping("sam", frozenset({"state_federal_debarment"}), "Federal exclusions/debarment evidence."),
    "bbb": SourceFieldMapping("bbb", frozenset({"better_business_bureau_complaints"}), "BBB complaints field."),
    "mn_debarment": SourceFieldMapping("mn_debarment", frozenset({"state_federal_debarment"}), "Minnesota debarment evidence."),
    "mn_pca": SourceFieldMapping("mn_pca", frozenset({"environmental_violations"}), "Minnesota environmental enforcement."),
    "violation_tracker": SourceFieldMapping(
        "violation_tracker",
        frozenset(),
        "Cross-agency evidence-only source. Findings may be stored and compared, but Violation Tracker intentionally owns no master fields and cannot directly create master-field proposals.",
    ),
    "wisdot": SourceFieldMapping(
        "wisdot",
        frozenset({"state_federal_debarment"}),
        "Confirmed current WisDOT debarred/suspended/ineligible records may propose Y only; no-match never emits N. Finals/project-status findings remain evidence-only.",
    ),
    "dol_enforcement": SourceFieldMapping(
        "dol_enforcement",
        frozenset(),
        "Potentially maps to multiple labor fields; ownership intentionally withheld pending firm definition.",
    ),
    "gsa_state_debarment": SourceFieldMapping(
        "gsa_state_debarment",
        frozenset(),
        "Directory of state sources, not itself treated as a bidder-field owner.",
    ),
    "responsible_mn": SourceFieldMapping(
        "responsible_mn",
        frozenset(),
        "Exact bidder-field ownership must be confirmed before automatic proposals.",
    ),
}


def owned_fields_for(source_key: str) -> frozenset[str]:
    mapping = SOURCE_FIELD_MAPPINGS.get(source_key)
    return mapping.owned_fields if mapping else frozenset()


def source_owns_field(source_key: str, field_name: str) -> bool:
    return field_name in owned_fields_for(source_key)
