from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..matching import normalize_company_name, normalize_text, score_candidate
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


DOL_API_BASE = "https://apiprod.dol.gov/v4"
DOL_DATASETS_URL = f"{DOL_API_BASE}/datasets"
DOL_PORTAL_URL = "https://data.dol.gov/"
DOL_API_KEY_ENV = "DOL_API_KEY"
REQUEST_TIMEOUT_SECONDS = 30.0
CATALOG_PAGE_LIMIT = 20
DATA_PAGE_SIZE = 1000
DATA_PAGE_LIMIT = 50
MAX_QUERY_VARIANTS = 6
USER_AGENT = "Paralegal-Research-Database-Workflow-V2/1.0"

NAME_FIELD_CANDIDATES = (
    "trade_nm",
    "legal_name",
    "trade_name",
    "employer_name",
    "employer_legal_name",
    "establishment_name",
)
ADDRESS_FIELD_CANDIDATES = (
    "street_addr_1_txt",
    "street_address",
    "address",
    "employer_street_address",
)
CITY_FIELD_CANDIDATES = ("cty_nm", "city_nm", "city", "employer_city")
STATE_FIELD_CANDIDATES = ("st_cd", "state_cd", "state", "employer_state")
ZIP_FIELD_CANDIDATES = ("zip_cd", "zip", "zipcode", "zip_code", "employer_zip")
CASE_ID_FIELD_CANDIDATES = ("case_id", "case_identifier", "case_number", "case_no")


class DolFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: SourceResultStatus,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


@dataclass(frozen=True)
class DolDataset:
    agency: str
    endpoint: str
    name: str
    dataset_id: str

    @property
    def data_url(self) -> str:
        return f"{DOL_API_BASE}/get/{self.agency}/{self.endpoint}/json"

    @property
    def metadata_url(self) -> str:
        return f"{self.data_url}/metadata"


@dataclass(frozen=True)
class ResolvedSchema:
    fields: frozenset[str]
    name_fields: tuple[str, ...]
    address_field: str | None
    city_field: str | None
    state_field: str | None
    zip_field: str | None
    case_id_field: str | None


@dataclass(frozen=True)
class ScoredRow:
    row: dict[str, Any]
    candidate_name: str
    matched_identity: str
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    zip_match: bool
    exact_name: bool


def _aliases(contractor: ContractorContext) -> list[str]:
    raw = [contractor.contractor_name]
    raw.extend(
        part.strip()
        for part in re.split(r"[;|\n]+", contractor.related_companies or "")
        if part.strip()
    )
    variants: list[str] = []
    seen: set[str] = set()
    for name in raw:
        for value in (name.strip(), normalize_company_name(name)):
            key = normalize_text(value)
            if not key or key in seen:
                continue
            # Avoid broad API queries such as "construction" by itself.
            if len(key.split()) == 1 and len(key) < 8:
                continue
            seen.add(key)
            variants.append(value)
            if len(variants) >= MAX_QUERY_VARIANTS:
                return variants
    return variants


def _identity_names(contractor: ContractorContext) -> list[str]:
    names = [contractor.contractor_name]
    names.extend(
        part.strip()
        for part in re.split(r"[;|\n]+", contractor.related_companies or "")
        if part.strip()
    )
    return [name for name in names if normalize_text(name)]


def _dataset_score(item: dict[str, Any]) -> int:
    agency = str((item.get("agency") or {}).get("abbr") or item.get("agency_abbr") or "").upper()
    if agency != "WHD":
        return -1
    text = normalize_text(f"{item.get('name', '')} {item.get('description', '')}")
    score = 0
    if "compliance" in text:
        score += 6
    if "enforcement" in text:
        score += 5
    if "action" in text:
        score += 3
    if "wage" in text or "hour" in text:
        score += 2
    return score


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _extract_total(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    containers = [payload, payload.get("meta"), payload.get("metadata"), payload.get("pagination")]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("total_count", "total", "count", "record_count"):
            value = container.get(key)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _extract_metadata_fields(payload: Any) -> set[str]:
    fields: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_norm = normalize_text(str(key)).replace(" ", "_")
                if key_norm in {
                    "name",
                    "column_name",
                    "field_name",
                    "api_name",
                    "variable_name",
                } and isinstance(nested, str):
                    candidate = nested.strip()
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
                        fields.add(candidate)
                if key_norm in {"fields", "columns", "variables"} and isinstance(nested, dict):
                    for candidate in nested:
                        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(candidate)):
                            fields.add(str(candidate))
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    return fields


def _resolve_one(fields: set[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {field.casefold(): field for field in fields}
    for candidate in candidates:
        if candidate.casefold() in lowered:
            return lowered[candidate.casefold()]
    return None


def _resolve_schema(fields: set[str]) -> ResolvedSchema:
    lowered = {field.casefold(): field for field in fields}
    name_fields = tuple(
        lowered[candidate.casefold()]
        for candidate in NAME_FIELD_CANDIDATES
        if candidate.casefold() in lowered
    )
    return ResolvedSchema(
        fields=frozenset(fields),
        name_fields=name_fields,
        address_field=_resolve_one(fields, ADDRESS_FIELD_CANDIDATES),
        city_field=_resolve_one(fields, CITY_FIELD_CANDIDATES),
        state_field=_resolve_one(fields, STATE_FIELD_CANDIDATES),
        zip_field=_resolve_one(fields, ZIP_FIELD_CANDIDATES),
        case_id_field=_resolve_one(fields, CASE_ID_FIELD_CANDIDATES),
    )


def _safe_string(row: dict[str, Any], field: str | None) -> str:
    if not field:
        return ""
    value = row.get(field)
    return "" if value is None else str(value).strip()


def _row_name(row: dict[str, Any], schema: ResolvedSchema) -> str:
    for field in schema.name_fields:
        value = _safe_string(row, field)
        if value:
            return value
    return ""


def _record_id(row: dict[str, Any], schema: ResolvedSchema) -> str:
    value = _safe_string(row, schema.case_id_field)
    if value:
        return value
    serialized = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]


def _number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _violation_counts(row: dict[str, Any]) -> dict[str, float]:
    counts: dict[str, float] = {}
    for key, value in row.items():
        normalized = key.casefold()
        if "viol" not in normalized:
            continue
        if not ("cnt" in normalized or "count" in normalized):
            continue
        count = _number(value)
        if count > 0:
            counts[key] = count
    return counts


def _dbra_count(row: dict[str, Any]) -> float:
    return sum(
        count
        for field, count in _violation_counts(row).items()
        if "dbra" in field.casefold() or "davis" in field.casefold()
    )


def _filter_object(name: str, schema: ResolvedSchema, state: str) -> dict[str, Any]:
    name_conditions = [
        {"field": field, "operator": "like", "value": f"%{name}%"}
        for field in schema.name_fields
    ]
    name_filter: dict[str, Any]
    if len(name_conditions) == 1:
        name_filter = name_conditions[0]
    else:
        name_filter = {"or": name_conditions}

    if state and schema.state_field:
        return {
            "and": [
                name_filter,
                {"field": schema.state_field, "operator": "eq", "value": state},
            ]
        }
    return name_filter


def _score_row(contractor: ContractorContext, row: dict[str, Any], schema: ResolvedSchema) -> ScoredRow:
    candidate_name = _row_name(row, schema)
    candidate_address = _safe_string(row, schema.address_field)
    candidate_city = _safe_string(row, schema.city_field)
    candidate_state = _safe_string(row, schema.state_field)
    candidate_zip = _safe_string(row, schema.zip_field)

    best = None
    best_identity = contractor.contractor_name
    for identity in _identity_names(contractor):
        score = score_candidate(
            master_name=identity,
            candidate_name=candidate_name,
            master_address=contractor.address_1,
            candidate_address=candidate_address,
            master_city=contractor.city,
            candidate_city=candidate_city,
            master_state=contractor.state,
            candidate_state=candidate_state,
        )
        if best is None or score.score > best.score:
            best = score
            best_identity = identity

    if best is None:
        best = score_candidate(master_name=contractor.contractor_name, candidate_name=candidate_name)

    master_zip = re.sub(r"\D", "", contractor.zip or "")[:5]
    row_zip = re.sub(r"\D", "", candidate_zip)[:5]
    zip_match = bool(master_zip and row_zip and master_zip == row_zip)
    exact_name = normalize_company_name(best_identity) == normalize_company_name(candidate_name)

    return ScoredRow(
        row=row,
        candidate_name=candidate_name,
        matched_identity=best_identity,
        score=best.score,
        name_score=best.name_score,
        address_score=best.address_score,
        city_score=best.city_score,
        state_score=best.state_score,
        zip_match=zip_match,
        exact_name=exact_name,
    )


def _auto_confirm(contractor: ContractorContext, candidate: ScoredRow) -> bool:
    has_master_location = bool(
        contractor.address_1.strip()
        or contractor.city.strip()
        or contractor.state.strip()
        or contractor.zip.strip()
    )
    strong_location = (
        candidate.zip_match
        or candidate.address_score >= 0.82
        or (candidate.city_score == 1.0 and candidate.state_score == 1.0)
    )
    if candidate.exact_name and strong_location:
        return True
    if candidate.score >= 0.95 and candidate.name_score >= 0.92 and strong_location:
        return True
    if candidate.exact_name and not has_master_location:
        return True
    return False


class DolEnforcementSource(ResearchSource):
    source_key = "dol_enforcement"
    display_name = "U.S. Department of Labor Enforcement Data"
    adapter_version = "0.1.0"
    parser_version = "0.1.0"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        api_key: str | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv(DOL_API_KEY_ENV, "")).strip()
        self.client = client or httpx.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._dataset: DolDataset | None = None
        self._schema: ResolvedSchema | None = None

    def health_check(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "official_dol_v4_api",
            "portal_url": DOL_PORTAL_URL,
            "catalog_url": DOL_DATASETS_URL,
            "api_key_required_for_data": True,
            "api_key_configured": bool(self.api_key),
            "phase": "WHD_enforcement",
        }

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> tuple[Any, int]:
        request_params = dict(params or {})
        if authenticated:
            if not self.api_key:
                raise DolFetchError(
                    f"Set {DOL_API_KEY_ENV} to use DOL metadata/data endpoints.",
                    status=SourceResultStatus.AUTH_REQUIRED,
                )
            # DOL's v4 guide documents X-API-KEY as a request parameter. Never retain
            # the resulting request URL because it contains the secret.
            request_params["X-API-KEY"] = self.api_key
        try:
            response = self.client.get(url, params=request_params)
        except httpx.TimeoutException as exc:
            raise DolFetchError(
                "The DOL API request timed out.", status=SourceResultStatus.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise DolFetchError(
                "The DOL API request failed before a response was received.",
                status=SourceResultStatus.HTTP_ERROR,
            ) from exc

        if response.status_code in {401, 403}:
            raise DolFetchError(
                "DOL rejected the API credentials or authorization.",
                status=SourceResultStatus.AUTH_REQUIRED,
                http_status=response.status_code,
            )
        if response.status_code == 429:
            raise DolFetchError(
                "DOL rate-limited the API request.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if response.status_code >= 500:
            raise DolFetchError(
                "The DOL API is temporarily unavailable.",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise DolFetchError(
                f"The DOL API returned HTTP {response.status_code}.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )
        try:
            return response.json(), response.status_code
        except ValueError as exc:
            raise DolFetchError(
                "The DOL API returned a non-JSON response where JSON was expected.",
                status=SourceResultStatus.DATASET_MALFORMED,
                http_status=response.status_code,
            ) from exc

    def _discover_dataset(self) -> DolDataset:
        if self._dataset is not None:
            return self._dataset
        best_item: dict[str, Any] | None = None
        best_score = -1
        page = 1
        for _ in range(CATALOG_PAGE_LIMIT):
            payload, _ = self._get_json(DOL_DATASETS_URL, params={"page": page})
            if not isinstance(payload, dict) or not isinstance(payload.get("datasets"), list):
                raise DolFetchError(
                    "The DOL dataset catalog response did not match the documented structure.",
                    status=SourceResultStatus.LAYOUT_CHANGED,
                )
            for item in payload["datasets"]:
                if not isinstance(item, dict):
                    continue
                score = _dataset_score(item)
                if score > best_score:
                    best_score = score
                    best_item = item
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            next_page = meta.get("next_page")
            if not next_page:
                break
            page = int(next_page)
        else:
            raise DolFetchError(
                "DOL dataset-catalog pagination exceeded the safety limit.",
                status=SourceResultStatus.PAGINATION_INCOMPLETE,
            )

        if best_item is None or best_score <= 0:
            raise DolFetchError(
                "A WHD compliance/enforcement dataset could not be found in the DOL v4 catalog.",
                status=SourceResultStatus.SOURCE_UNAVAILABLE,
            )
        agency = str((best_item.get("agency") or {}).get("abbr") or "").strip()
        endpoint = str(best_item.get("api_url") or "").strip()
        if not agency or not endpoint:
            raise DolFetchError(
                "The selected DOL dataset is missing its agency or API endpoint metadata.",
                status=SourceResultStatus.DATASET_MALFORMED,
            )
        self._dataset = DolDataset(
            agency=agency,
            endpoint=endpoint,
            name=str(best_item.get("name") or "WHD enforcement data"),
            dataset_id=str(best_item.get("id") or ""),
        )
        return self._dataset

    def _load_schema(self, dataset: DolDataset) -> ResolvedSchema:
        if self._schema is not None:
            return self._schema
        payload, _ = self._get_json(dataset.metadata_url, authenticated=True)
        fields = _extract_metadata_fields(payload)
        schema = _resolve_schema(fields)
        if not schema.name_fields:
            raise DolFetchError(
                "DOL WHD metadata did not expose a recognized employer-name field.",
                status=SourceResultStatus.DATASET_MALFORMED,
            )
        self._schema = schema
        return schema

    def _query_variant(
        self,
        dataset: DolDataset,
        schema: ResolvedSchema,
        name: str,
        state: str,
    ) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]], int | None]:
        rows: list[dict[str, Any]] = []
        attempts: list[dict[str, Any]] = []
        offset = 0
        complete = True
        last_http_status: int | None = None
        for _ in range(DATA_PAGE_LIMIT):
            params = {
                "limit": DATA_PAGE_SIZE,
                "offset": offset,
                "filter_object": json.dumps(
                    _filter_object(name, schema, state),
                    separators=(",", ":"),
                ),
            }
            payload, status = self._get_json(dataset.data_url, params=params, authenticated=True)
            last_http_status = status
            page_rows = _extract_rows(payload)
            total = _extract_total(payload)
            attempts.append(
                {
                    "query_name": name,
                    "state": state if schema.state_field else "",
                    "offset": offset,
                    "limit": DATA_PAGE_SIZE,
                    "returned": len(page_rows),
                    "reported_total": total,
                }
            )
            rows.extend(page_rows)
            offset += len(page_rows)
            if total is not None:
                if offset >= total:
                    break
            elif len(page_rows) < DATA_PAGE_SIZE:
                break
            if not page_rows:
                break
        else:
            complete = False
        return rows, complete, attempts, last_http_status

    def _failure_result(self, contractor: ContractorContext, failure: DolFetchError) -> SourceResult:
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=failure.status,
                identity_status=IdentityStatus.NOT_EVALUATED,
                completeness_status=CompletenessStatus.UNKNOWN,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                warnings=[str(failure)],
                acquisition_method="official_dol_v4_api",
                source_url=DOL_PORTAL_URL,
                http_status=failure.http_status,
            )
        )

    def search(self, contractor: ContractorContext) -> SourceResult:
        if not self.api_key:
            return self._failure_result(
                contractor,
                DolFetchError(
                    f"Set {DOL_API_KEY_ENV} to run DOL enforcement research.",
                    status=SourceResultStatus.AUTH_REQUIRED,
                ),
            )
        variants = _aliases(contractor)
        if not variants:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.MANUAL_REVIEW_REQUIRED,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=["The bidder has no usable company name for DOL research."],
                    acquisition_method="official_dol_v4_api",
                    source_url=DOL_PORTAL_URL,
                )
            )

        try:
            dataset = self._discover_dataset()
            schema = self._load_schema(dataset)
            state = contractor.state.strip().upper() if re.fullmatch(r"[A-Za-z]{2}", contractor.state.strip()) else ""
            all_rows: list[dict[str, Any]] = []
            attempts: list[dict[str, Any]] = []
            complete = True
            last_http_status: int | None = None
            for variant in variants:
                rows, variant_complete, variant_attempts, status = self._query_variant(
                    dataset, schema, variant, state
                )
                all_rows.extend(rows)
                attempts.extend(variant_attempts)
                complete = complete and variant_complete
                last_http_status = status or last_http_status
        except DolFetchError as failure:
            return self._failure_result(contractor, failure)

        unique: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            unique[_record_id(row, schema)] = row
        rows = list(unique.values())
        scored = [_score_row(contractor, row, schema) for row in rows if _row_name(row, schema)]
        scored.sort(key=lambda item: item.score, reverse=True)
        plausible = [item for item in scored if item.name_score >= 0.75 and item.score >= 0.78]

        safe_dataset_url = dataset.data_url
        base_payload = {
            "dataset": {
                "id": dataset.dataset_id,
                "name": dataset.name,
                "agency": dataset.agency,
                "endpoint": dataset.endpoint,
            },
            "search_attempts": attempts,
            "candidate_count": len(rows),
            "schema": {
                "name_fields": list(schema.name_fields),
                "address_field": schema.address_field,
                "city_field": schema.city_field,
                "state_field": schema.state_field,
                "zip_field": schema.zip_field,
                "case_id_field": schema.case_id_field,
            },
            "note": (
                "DOL field ownership is intentionally disabled. DBRA findings are retained as candidate "
                "evidence for prevailing_wage_violations until the firm's field semantics are confirmed."
            ),
        }

        if not plausible:
            status = SourceResultStatus.SUCCESS_NO_MATCH if complete else SourceResultStatus.PARTIAL_RESULTS
            completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=status,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=completeness,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    acquisition_method="official_dol_v4_api",
                    source_url=safe_dataset_url,
                    http_status=last_http_status,
                    normalized_payload={**base_payload, "match_found": False},
                )
            )

        best = plausible[0]
        best_key = normalize_company_name(best.candidate_name)
        competing = [
            item
            for item in plausible[1:]
            if item.score >= 0.92 and normalize_company_name(item.candidate_name) != best_key
        ]
        confirmed = _auto_confirm(contractor, best) and not competing
        matched_rows = [
            item
            for item in plausible
            if normalize_company_name(item.candidate_name) == best_key
            or (item.score >= 0.92 and item.matched_identity == best.matched_identity)
        ]
        matched_case_rows = [item.row for item in matched_rows]
        violation_cases = [row for row in matched_case_rows if _violation_counts(row)]
        dbra_cases = [row for row in matched_case_rows if _dbra_count(row) > 0]

        match_payload = {
            **base_payload,
            "match_found": True,
            "matched_employer_name": best.candidate_name,
            "matched_identity_name": best.matched_identity,
            "identity_score": best.score,
            "name_score": best.name_score,
            "address_score": best.address_score,
            "city_score": best.city_score,
            "state_score": best.state_score,
            "zip_match": best.zip_match,
            "matched_case_count": len(matched_case_rows),
            "violation_case_count": len(violation_cases),
            "dbra_violation_case_count": len(dbra_cases),
            "cases": matched_case_rows,
            "competing_match_count": len(competing),
        }

        if not confirmed:
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=SourceResultStatus.AMBIGUOUS_MATCH,
                    identity_status=IdentityStatus.REVIEW_REQUIRED,
                    completeness_status=CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL,
                    identity_confidence=best.score,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=[
                        "DOL returned a plausible employer match, but identity was not strong enough for automatic confirmation."
                    ],
                    acquisition_method="official_dol_v4_api",
                    source_record_id=_record_id(best.row, schema),
                    source_url=safe_dataset_url,
                    http_status=last_http_status,
                    normalized_payload=match_payload,
                )
            )

        evidence: list[EvidenceRecord] = []
        if dbra_cases:
            first = dbra_cases[0]
            evidence.append(
                EvidenceRecord(
                    field_name="prevailing_wage_violations",
                    observed_value="Y",
                    source_record_id=_record_id(first, schema),
                    source_url=safe_dataset_url,
                    details={
                        "evidence_only_pending_field_ownership": True,
                        "law": "Davis-Bacon and Related Acts (DBRA)",
                        "dbra_violation_case_count": len(dbra_cases),
                        "dbra_violation_count": sum(_dbra_count(row) for row in dbra_cases),
                        "matched_employer_name": best.candidate_name,
                    },
                )
            )

        if not complete:
            result_status = SourceResultStatus.PARTIAL_RESULTS
            completeness_status = CompletenessStatus.PARTIAL
        elif violation_cases:
            result_status = SourceResultStatus.SUCCESS_WITH_FINDINGS
            completeness_status = CompletenessStatus.COMPLETE
        else:
            result_status = SourceResultStatus.SUCCESS_COMPLETE
            completeness_status = CompletenessStatus.COMPLETE

        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=result_status,
                identity_status=IdentityStatus.CONFIRMED,
                completeness_status=completeness_status,
                identity_confidence=best.score,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=evidence,
                warnings=(
                    ["DOL pagination hit the configured safety limit; evidence is partial."]
                    if not complete
                    else []
                ),
                acquisition_method="official_dol_v4_api",
                source_record_id=_record_id(best.row, schema),
                source_url=safe_dataset_url,
                http_status=last_http_status,
                normalized_payload=match_payload,
            )
        )
