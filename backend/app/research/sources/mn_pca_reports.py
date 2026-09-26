from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx


SEARCH_URL = "https://www.pca.state.mn.us/search?search=enforcement%20cases%20at%20MPCA"
KNOWN_REPORT_URLS = (
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-118-enforcement-cases-in-second-half-of-2023",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-100-enforcement-cases-in-first-half-of-2024",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-75-enforcement-cases-in-second-half-of-2024",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-68-enforcement-cases-in-first-half-of-2025",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-78-enforcement-cases-in-second-half-of-2025",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-147-enforcement-cases-in-first-half-of-2026",
)

MIN_TOTAL_REPORT_RECORDS = 250
MIN_PAGE_COVERAGE_RATIO = 0.70


class MpcaReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class MpcaReportDataset:
    csv_bytes: bytes
    sha256: str
    record_count: int
    page_count: int
    source_urls: tuple[str, ...]
    warnings: tuple[str, ...]
    scope_start: str
    scope_end: str


class _ReportHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.list_items: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []

        self._table_depth = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_li = 0
        self._li_parts: list[str] = []
        self._current_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        if lower == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = []
        elif lower == "tr" and self._table_depth and self._current_table is not None:
            self._current_row = []
        elif lower in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
        elif lower == "li":
            self._in_li += 1
            if self._in_li == 1:
                self._li_parts = []
        elif lower == "a":
            self._current_href = None
            self._anchor_parts = []
            for key, value in attrs:
                if key.casefold() == "href" and value:
                    self._current_href = value.strip()
                    break

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_row.append(" ".join(self._current_cell).strip())
            self._current_cell = None
        elif lower == "tr" and self._current_row is not None and self._current_table is not None:
            if any(cell.strip() for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif lower == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._current_table is not None:
                if self._current_table:
                    self.tables.append(self._current_table)
                self._current_table = None
        elif lower == "li" and self._in_li:
            self._in_li -= 1
            if self._in_li == 0:
                value = " ".join(self._li_parts).strip()
                if value:
                    self.list_items.append(value)
                self._li_parts = []
        elif lower == "a":
            if self._current_href:
                self.links.append((self._current_href, " ".join(self._anchor_parts).strip()))
            self._current_href = None
            self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        self.text_parts.append(value)
        if self._current_cell is not None:
            self._current_cell.append(value)
        if self._in_li:
            self._li_parts.append(value)
        if self._current_href is not None:
            self._anchor_parts.append(value)

    @property
    def visible_text(self) -> str:
        return " ".join(self.text_parts)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _fingerprint(row: dict[str, str]) -> str:
    payload = "|".join(
        _norm(row.get(key, ""))
        for key in (
            "Company or individual(s)",
            "Public date",
            "Violation location",
            "Violation description",
            "Net penalty",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discover_report_urls(html: str, *, base_url: str = SEARCH_URL) -> list[str]:
    parser = _ReportHtmlParser()
    parser.feed(html or "")
    urls: list[str] = []
    for href, label in parser.links:
        absolute = urljoin(base_url, href)
        if not absolute.startswith("https://www.pca.state.mn.us/news-and-stories/"):
            continue
        label_norm = _norm(label)
        href_norm = _norm(absolute)
        if "enforcement cases" not in label_norm and "enforcement cases" not in href_norm:
            continue
        if not re.search(r"\b(?:first|second) half of 20\d{2}\b", label_norm):
            continue
        if absolute not in urls:
            urls.append(absolute)
    return urls


def _expected_case_count(text: str) -> int | None:
    match = re.search(
        r"(?:completes?|completed|closed)\s+(\d{1,4})\s+enforcement\s+cases",
        text,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _table_records(parser: _ReportHtmlParser, source_url: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for table in parser.tables:
        header_index = None
        header_map: dict[str, int] = {}
        for index, row in enumerate(table[:5]):
            norms = [_norm(cell) for cell in row]
            if not any("company or individual" in item for item in norms):
                continue
            if not any("net penalty" in item for item in norms):
                continue
            if not any("violation" in item for item in norms):
                continue
            header_index = index
            for cell_index, value in enumerate(norms):
                if "public date" in value:
                    header_map["date"] = cell_index
                elif "company or individual" in value:
                    header_map["company"] = cell_index
                elif "violation location" in value:
                    header_map["location"] = cell_index
                elif value == "violation" or "violation description" in value or "violation s" in value:
                    header_map["violation"] = cell_index
                elif "net penalty" in value:
                    header_map["penalty"] = cell_index
                elif "case type" in value:
                    header_map["case_type"] = cell_index
            break

        if header_index is None or not {"company", "violation", "penalty"}.issubset(header_map):
            continue

        for row in table[header_index + 1 :]:
            def cell(name: str) -> str:
                idx = header_map.get(name)
                return row[idx].strip() if idx is not None and idx < len(row) else ""

            party = cell("company")
            violation = cell("violation")
            penalty = cell("penalty")
            if not party or not violation or not penalty:
                continue
            results.append(
                {
                    "Company or individual(s)": party,
                    "Public date": cell("date"),
                    "Violation location": cell("location"),
                    "Violation description": violation,
                    "Net penalty": penalty,
                    "Case type": cell("case_type"),
                    "Source URL": source_url,
                }
            )
    return results


def _list_records(parser: _ReportHtmlParser, source_url: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    pattern = re.compile(
        r"^(?P<party>.+),\s+for\s+(?P<violation>.+?)\s+violations?"
        r"(?:\s+in\s+(?P<location>.*?))?,\s*(?P<penalty>\$[\d,]+(?:\.\d{2})?)\.?$",
        re.IGNORECASE,
    )
    for item in parser.list_items:
        match = pattern.match(item.strip())
        if not match:
            continue
        results.append(
            {
                "Company or individual(s)": match.group("party").strip(),
                "Public date": "",
                "Violation location": (match.group("location") or "").strip(),
                "Violation description": match.group("violation").strip(),
                "Net penalty": match.group("penalty").strip(),
                "Case type": "",
                "Source URL": source_url,
            }
        )
    return results


def parse_report_page(html: str, *, source_url: str) -> tuple[list[dict[str, str]], int | None]:
    parser = _ReportHtmlParser()
    parser.feed(html or "")
    expected = _expected_case_count(parser.visible_text)
    records = _table_records(parser, source_url) + _list_records(parser, source_url)

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in records:
        fingerprint = _fingerprint(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(row)
    return deduped, expected


def _response_html(response: httpx.Response, *, label: str) -> str:
    if response.status_code >= 400:
        raise MpcaReportError(f"{label} returned HTTP {response.status_code}.")
    content_type = response.headers.get("content-type", "").casefold()
    text = response.text
    if "html" not in content_type and "<html" not in text[:4096].casefold() and "<!doctype html" not in text[:4096].casefold():
        raise MpcaReportError(f"{label} did not return HTML.")
    lowered = text[:262144].casefold()
    if any(marker in lowered for marker in ("hcaptcha", "g-recaptcha", "radware captcha", "validate.perfdrive.com")):
        raise MpcaReportError(f"{label} returned a CAPTCHA/security challenge.")
    return text


def fetch_official_report_dataset(
    client: httpx.Client,
    *,
    report_urls: tuple[str, ...] | None = None,
    discover: bool = True,
    min_total_records: int = MIN_TOTAL_REPORT_RECORDS,
) -> MpcaReportDataset:
    urls = list(report_urls or KNOWN_REPORT_URLS)
    warnings: list[str] = []

    if discover:
        try:
            search_response = client.get(SEARCH_URL)
            search_html = _response_html(search_response, label="MPCA site search")
            for url in discover_report_urls(search_html):
                if url not in urls:
                    urls.append(url)
        except (httpx.HTTPError, MpcaReportError) as exc:
            warnings.append(f"MPCA report discovery could not refresh: {exc}")

    rows: list[dict[str, str]] = []
    successful_urls: list[str] = []
    failed_urls: list[str] = []

    for url in urls:
        try:
            response = client.get(url)
            html = _response_html(response, label=f"MPCA enforcement report {url}")
            page_rows, expected = parse_report_page(html, source_url=url)
            if not page_rows:
                raise MpcaReportError("no enforcement rows were recognized")
            if expected:
                minimum = max(1, int(expected * MIN_PAGE_COVERAGE_RATIO))
                if len(page_rows) < minimum:
                    raise MpcaReportError(
                        f"parsed {len(page_rows)} rows but page reports {expected} cases; minimum accepted coverage is {minimum}"
                    )
            rows.extend(page_rows)
            successful_urls.append(url)
        except (httpx.HTTPError, MpcaReportError) as exc:
            failed_urls.append(url)
            warnings.append(f"MPCA report page unavailable or incomplete: {url}: {exc}")

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        fingerprint = _fingerprint(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(row)

    if len(deduped) < max(1, int(min_total_records)):
        raise MpcaReportError(
            f"Official MPCA report fallback produced only {len(deduped)} validated records; "
            f"minimum required is {min_total_records}."
        )

    buffer = io.StringIO(newline="")
    fieldnames = [
        "Company or individual(s)",
        "Public date",
        "Violation location",
        "Violation description",
        "Net penalty",
        "Case type",
        "Source URL",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(deduped)
    raw = buffer.getvalue().encode("utf-8")

    if failed_urls:
        warnings.append(
            "The official report-page dataset is intentionally partial because one or more report pages could not be validated."
        )
    warnings.append(
        "The MPCA report-page fallback covers published enforcement summaries beginning with the second half of 2023; "
        "it is evidence-capable but not an all-history clean-negative source."
    )

    return MpcaReportDataset(
        csv_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        record_count=len(deduped),
        page_count=len(successful_urls),
        source_urls=tuple(successful_urls),
        warnings=tuple(warnings),
        scope_start="2023-07-01",
        scope_end="present published summaries",
    )
