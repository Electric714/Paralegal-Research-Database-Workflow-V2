from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from pypdf import PdfReader

from ... import database as db
from ..matching import normalize_company_name, normalize_text, score_candidate
from ..models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    RawArtifact,
    SourceResult,
    SourceResultStatus,
)
from .base import ContractorContext, ResearchSource


WISDOT_HOME_URL = "https://wisconsindot.gov/Pages/doing-bus/contractors/hcci/cntrct-info.aspx"
WISDOT_DATASETS: dict[str, str] = {
    "debarment": "https://wisconsindot.gov/hccidocs/debar.pdf",
    "all_contractors": "https://wisconsindot.gov/hccidocs/contracting-info/allcont.pdf",
    "prequalified": "https://wisconsindot.gov/hccidocs/prequal.pdf",
    "finals_status": "https://wisconsindot.gov/hcciupload/finals-status-statewide-report.pdf",
}
MAX_PDF_BYTES = 100 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60.0
USER_AGENT = "ParalegalResearchDatabaseV2/1.0 (+public WisDOT contractor research)"


@dataclass(frozen=True)
class CachedPdf:
    key: str
    url: str
    path: Path
    sha256: str
    text: str
    retrieved_at: str
    from_cache: bool


@dataclass(frozen=True)
class DebarmentRecord:
    name: str
    address_1: str
    city: str
    state: str
    zip_code: str
    effective_date: str
    termination_date: str
    action: str
    restricted_area: str
    acting_agency: str
    cause_code: str

    @property
    def record_id(self) -> str:
        base = "|".join(
            [self.name, self.address_1, self.effective_date, self.termination_date, self.action, self.acting_agency]
        )
        return "wisdot-debar-" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "address_1": self.address_1,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "effective_date": self.effective_date,
            "termination_date": self.termination_date,
            "action": self.action,
            "restricted_area": self.restricted_area,
            "acting_agency": self.acting_agency,
            "cause_code": self.cause_code,
        }


@dataclass(frozen=True)
class VendorRecord:
    vendor_id: str
    name: str
    address_1: str
    city: str
    state: str
    zip_code: str
    dataset_key: str

    @property
    def record_id(self) -> str:
        return f"wisdot-vendor-{self.vendor_id}-{self.dataset_key}"

    def as_dict(self) -> dict[str, str]:
        return {
            "vendor_id": self.vendor_id,
            "name": self.name,
            "address_1": self.address_1,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "dataset_key": self.dataset_key,
        }


@dataclass(frozen=True)
class Candidate:
    record: DebarmentRecord | VendorRecord
    score: float
    name_score: float
    address_score: float
    city_score: float
    state_score: float
    matched_alias: str
    matched_location: str
    auto_confirmable: bool
    remembered_judgment: str | None = None

    def as_dict(self) -> dict:
        return {
            "record_id": self.record.record_id,
            "record": self.record.as_dict(),
            "score": self.score,
            "name_score": self.name_score,
            "address_score": self.address_score,
            "city_score": self.city_score,
            "state_score": self.state_score,
            "matched_alias": self.matched_alias,
            "matched_location": self.matched_location,
            "auto_confirmable": self.auto_confirmable,
            "remembered_judgment": self.remembered_judgment,
        }


class WisdotDatasetError(RuntimeError):
    def __init__(self, message: str, *, status: SourceResultStatus, http_status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def _cache_root() -> Path:
    return db.DATA_DIR / "source_cache" / "wisdot"


def _artifact_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(db.BASE_DIR.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            text = page.extract_text() or ""
        pages.append(text)
    return "\n\f\n".join(pages)


def _validate_layout(key: str, text: str) -> None:
    if key == "debarment":
        folded = text.casefold()
        missing: list[str] = []
        if "contractor" not in folded or not any(token in folded for token in ("debarred", "suspended", "ineligible")):
            missing.append("contractor/action title")
        if not re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text):
            missing.append("dated contractor row")
        if missing:
            raise WisdotDatasetError(
                f"WisDOT {key} PDF layout is missing expected marker(s): {', '.join(missing)}",
                status=SourceResultStatus.LAYOUT_CHANGED,
            )
        return

    required = {
        "all_contractors": ("All Contractors", "Vendor Name & Address"),
        "prequalified": ("Prequalified Contractors", "Vendor Name & Address"),
        "finals_status": ("Finals Status After Actual Completion", "Contract ID", "Contractor"),
    }[key]
    missing = [token for token in required if token.casefold() not in text.casefold()]
    if missing:
        raise WisdotDatasetError(
            f"WisDOT {key} PDF layout is missing expected marker(s): {', '.join(missing)}",
            status=SourceResultStatus.LAYOUT_CHANGED,
        )


def _read_latest_cached(key: str, cache_dir: Path, extractor: Callable[[bytes], str]) -> CachedPdf | None:
    meta_path = cache_dir / key / "latest.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sha = str(meta["sha256"])
        raw_path = cache_dir / key / f"{sha}.pdf"
        text_path = cache_dir / key / f"{sha}.txt"
        if not raw_path.exists():
            return None
        text = text_path.read_text(encoding="utf-8") if text_path.exists() else extractor(raw_path.read_bytes())
        if not text_path.exists():
            text_path.write_text(text, encoding="utf-8")
        _validate_layout(key, text)
        return CachedPdf(
            key=key,
            url=str(meta.get("url") or WISDOT_DATASETS[key]),
            path=raw_path,
            sha256=sha,
            text=text,
            retrieved_at=str(meta.get("retrieved_at") or ""),
            from_cache=True,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, WisdotDatasetError):
        return None


def _store_pdf(
    *,
    key: str,
    url: str,
    data: bytes,
    cache_dir: Path,
    extractor: Callable[[bytes], str],
) -> CachedPdf:
    sha = _sha256(data)
    directory = cache_dir / key
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = directory / f"{sha}.pdf"
    text_path = directory / f"{sha}.txt"
    if not raw_path.exists():
        raw_path.write_bytes(data)
    text = text_path.read_text(encoding="utf-8") if text_path.exists() else extractor(data)
    if not text_path.exists():
        text_path.write_text(text, encoding="utf-8")
    _validate_layout(key, text)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    (directory / "latest.json").write_text(
        json.dumps({"sha256": sha, "url": url, "retrieved_at": retrieved_at}, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return CachedPdf(key, url, raw_path, sha, text, retrieved_at, False)


def _location_parts(value: str) -> tuple[str, str, str]:
    city_pattern = r"[A-Za-z][A-Za-z.'-]*(?: [A-Za-z][A-Za-z.'-]*)*"
    matches = list(
        re.finditer(
            rf"(?P<city>{city_pattern}),?\s+(?P<state>[A-Z]{{2}})\s+(?P<zip>\d{{5}}(?:-\d{{4}})?)\b",
            value,
        )
    )
    if not matches:
        return "", "", ""
    match = matches[-1]
    return match.group("city").strip(" ,"), match.group("state"), match.group("zip")


_DEBARMENT_ROW_RE = re.compile(
    r"(?P<effective>\d{1,2}/\d{1,2}/\d{2,4})\s+"
    r"(?P<termination>\d{1,2}/\d{1,2}/\d{2,4}|Indefinite)\s+"
    r"(?P<action>Debarment|Suspended|Ineligible)\s+"
    r"(?P<area>\S+)"
    r"(?:\s+(?P<agency>WisDOT|WisDWD|FHWA|GSA|AF|[A-Z][A-Za-z0-9&./-]*))?"
    r"(?:\s+(?P<cause>\d[\d,\s]*))?\s*$",
    re.IGNORECASE,
)
_DEBARMENT_HEADER_WORDS = {
    "name",
    "of",
    "contractor",
    "address",
    "effective",
    "date",
    "termination",
    "action",
    "restricted",
    "area",
    "acting",
    "agency",
    "cause",
    "code",
}
_DEBARMENT_NOISE_EXACT = {
    "debarred contractors",
    "suspended contractors",
    "ineligible contractors",
    "to:",
    "date:",
    "subject:",
    "from:",
}
_DEBARMENT_NOISE_MARKERS = (
    "prepared and issued",
    "list of debarred",
    "wisdot cause codes",
    "wisdwd cause codes",
    "correspondence/memorandum",
    "revisions from list",
    "the following have been",
    "region project development chiefs",
    "chief proposal management engineer",
)
_DEBARMENT_ADDRESS_HINT_RE = re.compile(
    r"\b(?:road|rd\.?|street|st\.?|avenue|ave\.?|boulevard|blvd\.?|lane|ln\.?|"
    r"drive|dr\.?|trail|trl\.?|place|pl\.?|court|ct\.?|parkway|pkwy\.?|"
    r"highway|hwy\.?|route|rte\.?|suite|ste\.?|building|bldg\.?|factory|"
    r"district|base|plant|project|area|no\.)\b",
    re.IGNORECASE,
)
_DEBARMENT_MIXED_NAME_ADDRESS_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<address>(?:\d{1,6}[A-Za-z-]*|[NSEW]\d{2,6}[A-Za-z-]*)\s+.+)$",
    re.IGNORECASE,
)
_DEBARMENT_FOREIGN_LOCATION_RE = re.compile(
    r",\s*(?:china|chn|hkg|hong kong|usa|u\.s\.a\.|canada|mexico)\s*$",
    re.IGNORECASE,
)


def _is_debarment_noise(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        return True
    folded = normalized.casefold()
    if folded in _DEBARMENT_NOISE_EXACT:
        return True
    if any(marker in folded for marker in _DEBARMENT_NOISE_MARKERS):
        return True
    words = set(re.findall(r"[a-z]+", folded))
    return bool(words and words <= _DEBARMENT_HEADER_WORDS)


def _looks_like_debarment_location(value: str) -> bool:
    _city, state, zip_code = _location_parts(value)
    if state and zip_code:
        return True
    return bool(_DEBARMENT_FOREIGN_LOCATION_RE.search(value.strip()))


def _looks_like_debarment_address(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if re.match(r"^(?:\d{1,6}[A-Za-z-]*|[NSEW]\d{2,6}[A-Za-z-]*)\b", text, flags=re.IGNORECASE):
        return True
    return bool(_DEBARMENT_ADDRESS_HINT_RE.search(text) and re.search(r"\d", text))


def _debarment_identity(lines: list[str]) -> tuple[str, str, str, str, str]:
    segments: list[str] = []
    for line in lines:
        for part in re.split(r"\s{2,}", line.strip()):
            cleaned = re.sub(r"\s+", " ", part).strip()
            if cleaned and not _is_debarment_noise(cleaned):
                segments.append(cleaned)

    city = state = zip_code = ""
    for segment in segments:
        loc_city, loc_state, loc_zip = _location_parts(segment)
        if loc_state and loc_zip:
            city, state, zip_code = loc_city, loc_state, loc_zip

    name_parts: list[str] = []
    address_parts: list[str] = []
    for segment in segments:
        if _looks_like_debarment_location(segment):
            continue

        mixed = _DEBARMENT_MIXED_NAME_ADDRESS_RE.match(segment)
        if mixed:
            name_part = mixed.group("name").strip(" ,-")
            address_part = mixed.group("address").strip(" ,-")
            if name_part:
                name_parts.append(name_part)
            if address_part:
                address_parts.append(address_part)
            continue

        if _looks_like_debarment_address(segment):
            address_parts.append(segment)
            continue

        if address_parts and _DEBARMENT_ADDRESS_HINT_RE.search(segment):
            address_parts.append(segment)
            continue

        name_parts.append(segment)

    return (
        " ".join(name_parts).strip(" ,-"),
        " ".join(address_parts).strip(" ,-"),
        city,
        state,
        zip_code,
    )


def parse_debarment_text(text: str) -> list[DebarmentRecord]:
    lines = [re.sub(r"\s+$", "", line) for line in text.splitlines()]
    records: list[DebarmentRecord] = []
    block_start = 0
    index = 0

    while index < len(lines):
        if not re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", lines[index]):
            index += 1
            continue

        match: re.Match[str] | None = None
        candidate = ""
        width = 0
        for candidate_width in range(1, min(3, len(lines) - index) + 1):
            candidate = " ".join(
                item.strip() for item in lines[index : index + candidate_width] if item.strip()
            )
            match = _DEBARMENT_ROW_RE.search(candidate)
            if match:
                width = candidate_width
                break

        if match is None:
            index += 1
            continue

        prefix = candidate[: match.start()].strip()
        context = list(lines[block_start:index])
        if prefix:
            context.append(prefix)
        name, address, city, state, zip_code = _debarment_identity(context)

        if name:
            records.append(
                DebarmentRecord(
                    name=name,
                    address_1=address,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                    effective_date=match.group("effective"),
                    termination_date=match.group("termination"),
                    action=match.group("action").title(),
                    restricted_area=match.group("area"),
                    acting_agency=(match.group("agency") or "").strip(),
                    cause_code=(match.group("cause") or "").strip(),
                )
            )

        block_start = index + width
        index += width

    if not records:
        raise WisdotDatasetError(
            "WisDOT debarment PDF was recognized but no contractor rows could be parsed.",
            status=SourceResultStatus.DATASET_MALFORMED,
        )
    return records


def parse_vendor_text(text: str, *, dataset_key: str) -> list[VendorRecord]:
    lines = [line.rstrip() for line in text.splitlines()]
    start_re = re.compile(r"^\s*(?P<vendor>[A-Z][A-Z0-9]{2,7})\s{2,}(?P<rest>.+)$")
    phone_or_date = re.compile(r"\s{2,}(?:\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}|\d{1,2}/\d{1,2}/\d{2,4})")
    starts: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = start_re.match(line)
        if not match:
            continue
        name = phone_or_date.split(match.group("rest").strip(), maxsplit=1)[0].strip()
        if name and "Vendor Name & Address" not in name:
            starts.append((index, match.group("vendor"), name))

    records: list[VendorRecord] = []
    for pos, (start, vendor_id, name) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else min(len(lines), start + 12)
        address = ""
        city = state = zip_code = ""
        for line in lines[start + 1 : min(end, start + 10)]:
            cleaned = re.sub(r"^\s*(Mail|Ship)\s+", "", line).strip()
            if not cleaned or "@" in cleaned or "Rated" in cleaned or "NO FAX" in cleaned:
                continue
            loc_city, loc_state, loc_zip = _location_parts(cleaned)
            if loc_state and loc_zip:
                city, state, zip_code = loc_city, loc_state, loc_zip
                continue
            if not address and re.search(r"\d", cleaned):
                cleaned = phone_or_date.split(cleaned, maxsplit=1)[0].strip()
                if cleaned:
                    address = cleaned
        records.append(VendorRecord(vendor_id, name, address, city, state, zip_code, dataset_key))
    if not records:
        raise WisdotDatasetError(
            f"WisDOT {dataset_key} PDF was recognized but no vendor rows could be parsed.",
            status=SourceResultStatus.DATASET_MALFORMED,
        )
    return records


def _aliases(contractor: ContractorContext) -> list[str]:
    names = [contractor.contractor_name]
    names.extend(
        part.strip()
        for part in re.split(r"[;|\n]+", contractor.related_companies or "")
        if part.strip()
    )
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        normalized = normalize_company_name(name)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(name.strip())
    return result


def _zip5(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[:5] if len(digits) >= 5 else ""


def _master_locations(contractor: ContractorContext) -> list[tuple[str, str, str, str, str]]:
    locations = [
        ("primary", contractor.address_1, contractor.city, contractor.state, contractor.zip),
        (
            "additional",
            contractor.additional_address,
            contractor.additional_address_city,
            contractor.additional_address_state,
            contractor.additional_address_zip,
        ),
    ]
    return [location for location in locations if any(location[1:])]


def _remembered_judgment(bidder_id: int, record_id: str) -> str | None:
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT judgment FROM identity_judgments WHERE bidder_id=? AND source_key='wisdot' AND source_record_id=?",
                (bidder_id, record_id),
            ).fetchone()
        return str(row["judgment"]) if row else None
    except Exception:
        return None


def _candidate_for(contractor: ContractorContext, record: DebarmentRecord | VendorRecord) -> Candidate | None:
    best: Candidate | None = None
    locations = _master_locations(contractor) or [("none", "", "", "", "")]
    remembered = _remembered_judgment(contractor.internal_id, record.record_id)

    for alias in _aliases(contractor):
        for label, address, city, state, zip_code in locations:
            scored = score_candidate(
                master_name=alias,
                candidate_name=record.name,
                master_address=address,
                candidate_address=record.address_1,
                master_city=city,
                candidate_city=record.city,
                master_state=state,
                candidate_state=record.state,
            )
            if scored.name_score < 0.78:
                continue
            exact_name = normalize_company_name(alias) == normalize_company_name(record.name)
            zip_match = bool(_zip5(zip_code) and _zip5(zip_code) == _zip5(record.zip_code))
            city_state_match = bool(
                normalize_text(city)
                and normalize_text(city) == normalize_text(record.city)
                and normalize_text(state)
                and normalize_text(state) == normalize_text(record.state)
            )
            address_match = bool(address and record.address_1 and scored.address_score >= 0.82)
            auto = remembered == "SAME_ENTITY" or (exact_name and (address_match or zip_match or city_state_match))
            if remembered == "DIFFERENT_ENTITY":
                auto = False
            candidate = Candidate(
                record=record,
                score=scored.score,
                name_score=scored.name_score,
                address_score=scored.address_score,
                city_score=scored.city_score,
                state_score=scored.state_score,
                matched_alias=alias,
                matched_location=label,
                auto_confirmable=auto,
                remembered_judgment=remembered,
            )
            if best is None or candidate.score > best.score:
                best = candidate
    return best


def _finals_findings(text: str, contractor: ContractorContext) -> list[dict]:
    aliases = [(alias, normalize_company_name(alias)) for alias in _aliases(contractor)]
    aliases = [(alias, normalized) for alias, normalized in aliases if len(normalized) >= 5]
    lines = text.splitlines()
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines):
        normalized_line = normalize_company_name(line)
        matched_alias = next((alias for alias, normalized in aliases if normalized in normalized_line), "")
        if not matched_alias:
            continue
        context = " ".join(
            item.strip() for item in lines[max(0, index - 4) : min(len(lines), index + 12)] if item.strip()
        )
        contract = re.search(r"\b\d{11}\b", context)
        project = re.search(r"\b\d{4}-\d{2}-\d{2}\b", context)
        remarks = re.search(r"Remarks:\s*(.*?)(?:Code Description|$)", context, flags=re.IGNORECASE)
        codes = sorted(set(re.findall(r"\b(?:CNQI|PLFC|WCLC|SFST|DNRP|OTHR)\b", context)))
        key = (contract.group(0) if contract else context[:80], matched_alias)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {
                "matched_alias": matched_alias,
                "contract_id": contract.group(0) if contract else "",
                "project_id": project.group(0) if project else "",
                "remarks": remarks.group(1).strip() if remarks else "",
                "codes": codes,
                "context": context[:1200],
            }
        )
    return findings[:50]


class WisdotContractorSource(ResearchSource):
    source_key = "wisdot"
    display_name = "Wisconsin DOT Contractor Information"
    adapter_version = "1.1.0"
    parser_version = "1.2.0"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        cache_dir: Path | None = None,
        pdf_text_extractor: Callable[[bytes], str] | None = None,
    ) -> None:
        self.client = client or httpx.Client(
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        self.cache_dir = cache_dir or _cache_root()
        self.pdf_text_extractor = pdf_text_extractor or _extract_pdf_text
        self.datasets: dict[str, CachedPdf] = {}
        self.prepare_warnings: list[str] = []
        self.prepare_errors: dict[str, WisdotDatasetError] = {}
        self.debarment_records: list[DebarmentRecord] = []
        self.vendor_records: list[VendorRecord] = []
        self.prequalified_records: list[VendorRecord] = []
        self.finals_text = ""

    def health_check(self) -> dict:
        return {
            "source_key": self.source_key,
            "implemented": True,
            "acquisition_mode": "automatic_pdf_refresh",
            "datasets": WISDOT_DATASETS,
            "cache_dir": str(self.cache_dir),
        }

    def _download_dataset(self, key: str, url: str) -> CachedPdf:
        try:
            response = self.client.get(url)
        except httpx.TimeoutException as exc:
            raise WisdotDatasetError(f"WisDOT {key} download timed out.", status=SourceResultStatus.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise WisdotDatasetError(
                f"WisDOT {key} download failed: {exc}", status=SourceResultStatus.SOURCE_UNAVAILABLE
            ) from exc
        if response.status_code in {403, 429}:
            raise WisdotDatasetError(
                f"WisDOT {key} download was blocked with HTTP {response.status_code}.",
                status=SourceResultStatus.BLOCKED,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise WisdotDatasetError(
                f"WisDOT {key} download returned HTTP {response.status_code}.",
                status=SourceResultStatus.HTTP_ERROR,
                http_status=response.status_code,
            )
        data = response.content
        if len(data) > MAX_PDF_BYTES:
            raise WisdotDatasetError(
                f"WisDOT {key} PDF exceeds the configured size limit.", status=SourceResultStatus.DATASET_MALFORMED
            )
        if not data.startswith(b"%PDF"):
            raise WisdotDatasetError(
                f"WisDOT {key} response is not a PDF.", status=SourceResultStatus.DATASET_MALFORMED
            )
        return _store_pdf(
            key=key,
            url=url,
            data=data,
            cache_dir=self.cache_dir,
            extractor=self.pdf_text_extractor,
        )

    def _parse_dataset(self, key: str, dataset: CachedPdf) -> None:
        try:
            if key == "debarment":
                self.debarment_records = parse_debarment_text(dataset.text)
            elif key == "all_contractors":
                self.vendor_records = parse_vendor_text(dataset.text, dataset_key=key)
            elif key == "prequalified":
                self.prequalified_records = parse_vendor_text(dataset.text, dataset_key=key)
            elif key == "finals_status":
                self.finals_text = dataset.text
        except WisdotDatasetError as exc:
            self.prepare_errors[key] = exc

    def prepare(self) -> None:
        self.datasets = {}
        self.prepare_warnings = []
        self.prepare_errors = {}
        self.debarment_records = []
        self.vendor_records = []
        self.prequalified_records = []
        self.finals_text = ""
        for key, url in WISDOT_DATASETS.items():
            try:
                dataset = self._download_dataset(key, url)
            except WisdotDatasetError as exc:
                cached = _read_latest_cached(key, self.cache_dir, self.pdf_text_extractor)
                if cached is None:
                    self.prepare_errors[key] = exc
                    continue
                dataset = cached
                self.prepare_warnings.append(f"WisDOT {key} live refresh failed; using the last cached artifact.")
                self.prepare_errors[key] = exc
            self.datasets[key] = dataset
            self._parse_dataset(key, dataset)

    def _artifacts(self) -> list[RawArtifact]:
        return [
            RawArtifact(
                artifact_type=f"wisdot_{key}_pdf",
                relative_path=_artifact_relative(dataset.path),
                sha256=dataset.sha256,
                mime_type="application/pdf",
                metadata={
                    "source_url": dataset.url,
                    "retrieved_at": dataset.retrieved_at,
                    "from_cache": dataset.from_cache,
                },
            )
            for key, dataset in sorted(self.datasets.items())
        ]

    def search(self, contractor: ContractorContext) -> SourceResult:
        warnings = list(self.prepare_warnings)
        artifacts = self._artifacts()
        acquisition_method = "automatic_cached_pdf"
        if not self.datasets:
            statuses = {error.status for error in self.prepare_errors.values()}
            status = SourceResultStatus.SOURCE_UNAVAILABLE
            if SourceResultStatus.BLOCKED in statuses:
                status = SourceResultStatus.BLOCKED
            elif SourceResultStatus.TIMEOUT in statuses:
                status = SourceResultStatus.TIMEOUT
            elif SourceResultStatus.HTTP_ERROR in statuses:
                status = SourceResultStatus.HTTP_ERROR
            warnings.extend(str(error) for error in self.prepare_errors.values())
            return self.validate_result(
                SourceResult(
                    source_key=self.source_key,
                    contractor_id=contractor.internal_id,
                    status=status,
                    identity_status=IdentityStatus.NOT_EVALUATED,
                    completeness_status=CompletenessStatus.UNKNOWN,
                    searched_name=contractor.contractor_name,
                    searched_address=contractor.address_1,
                    warnings=warnings,
                    source_url=WISDOT_HOME_URL,
                    acquisition_method=acquisition_method,
                )
            )

        all_debar = [
            candidate
            for record in self.debarment_records
            if (candidate := _candidate_for(contractor, record)) is not None
        ]
        all_debar.sort(key=lambda item: item.score, reverse=True)
        debar_candidates = [item for item in all_debar if item.remembered_judgment != "DIFFERENT_ENTITY"]
        confirmed_debar = [item for item in debar_candidates if item.auto_confirmable]

        all_vendor = [
            candidate
            for record in self.vendor_records + self.prequalified_records
            if (candidate := _candidate_for(contractor, record)) is not None
        ]
        all_vendor.sort(key=lambda item: item.score, reverse=True)
        vendor_candidates = [item for item in all_vendor if item.remembered_judgment != "DIFFERENT_ENTITY"]
        confirmed_vendor = [item for item in vendor_candidates if item.auto_confirmable]
        finals = _finals_findings(self.finals_text, contractor) if self.finals_text else []

        complete = not self.prepare_errors and len(self.datasets) == len(WISDOT_DATASETS)
        completeness = CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL
        if self.prepare_errors:
            warnings.extend(f"{key}: {error}" for key, error in sorted(self.prepare_errors.items()))

        evidence: list[EvidenceRecord] = []
        identity_status = IdentityStatus.NOT_EVALUATED
        identity_confidence: float | None = None
        source_record_id: str | None = None

        if confirmed_debar:
            best = confirmed_debar[0]
            identity_status = IdentityStatus.CONFIRMED
            identity_confidence = best.score
            source_record_id = best.record.record_id
            evidence.append(
                EvidenceRecord(
                    field_name="state_federal_debarment",
                    observed_value="Y",
                    source_record_id=best.record.record_id,
                    source_url=WISDOT_DATASETS["debarment"],
                    details={
                        "match": best.as_dict(),
                        "all_debarment_candidates": [item.as_dict() for item in all_debar[:10]],
                    },
                )
            )
        elif debar_candidates:
            best = debar_candidates[0]
            identity_status = IdentityStatus.REVIEW_REQUIRED
            identity_confidence = best.score
            source_record_id = best.record.record_id

        if confirmed_vendor:
            best_vendor = confirmed_vendor[0]
            if identity_status == IdentityStatus.NOT_EVALUATED:
                identity_status = IdentityStatus.CONFIRMED
                identity_confidence = best_vendor.score
                source_record_id = best_vendor.record.record_id
            evidence.append(
                EvidenceRecord(
                    field_name="wisdot_vendor_record",
                    observed_value=best_vendor.record.vendor_id,
                    source_record_id=best_vendor.record.record_id,
                    source_url=WISDOT_DATASETS[best_vendor.record.dataset_key],
                    details={"match": best_vendor.as_dict(), "candidate_count": len(vendor_candidates)},
                )
            )
        elif vendor_candidates and identity_status == IdentityStatus.NOT_EVALUATED:
            identity_status = IdentityStatus.REVIEW_REQUIRED
            identity_confidence = vendor_candidates[0].score
            source_record_id = vendor_candidates[0].record.record_id

        if finals:
            evidence.append(
                EvidenceRecord(
                    field_name="wisdot_finals_status",
                    observed_value=f"{len(finals)} matching project record(s)",
                    source_url=WISDOT_DATASETS["finals_status"],
                    details={"findings": finals},
                )
            )
            if identity_status == IdentityStatus.NOT_EVALUATED:
                identity_status = IdentityStatus.REVIEW_REQUIRED

        ambiguous = bool(debar_candidates and not confirmed_debar)
        has_findings = bool(evidence or debar_candidates or vendor_candidates or finals)
        if ambiguous:
            status = SourceResultStatus.AMBIGUOUS_MATCH
        elif not complete:
            status = SourceResultStatus.PARTIAL_RESULTS
        elif has_findings:
            status = SourceResultStatus.SUCCESS_WITH_FINDINGS
        else:
            status = SourceResultStatus.SUCCESS_NO_MATCH

        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=status,
                identity_status=identity_status,
                completeness_status=completeness,
                identity_confidence=identity_confidence,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                evidence=evidence,
                warnings=warnings,
                artifacts=artifacts,
                normalized_payload={
                    "debarment_candidates": [item.as_dict() for item in all_debar[:20]],
                    "vendor_candidates": [item.as_dict() for item in all_vendor[:20]],
                    "finals_findings": finals,
                    "dataset_status": {
                        key: {
                            "sha256": dataset.sha256,
                            "retrieved_at": dataset.retrieved_at,
                            "from_cache": dataset.from_cache,
                        }
                        for key, dataset in sorted(self.datasets.items())
                    },
                    "missing_or_stale_datasets": sorted(self.prepare_errors),
                },
                source_record_id=source_record_id,
                source_url=WISDOT_HOME_URL,
                acquisition_method=acquisition_method,
            )
        )
