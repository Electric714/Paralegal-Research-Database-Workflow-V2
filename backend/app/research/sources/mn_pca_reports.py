from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx


SEARCH_URL = "https://www.pca.state.mn.us/search?search=enforcement%20cases%20at%20MPCA"
JINA_READER_BASE = "https://r.jina.ai/"
KNOWN_REPORT_URLS = (
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-118-enforcement-cases-in-second-half-of-2023",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-100-enforcement-cases-in-first-half-of-2024",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-75-enforcement-cases-in-second-half-of-2024",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-68-enforcement-cases-in-first-half-of-2025",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-78-enforcement-cases-in-second-half-of-2025",
    "https://www.pca.state.mn.us/news-and-stories/mpca-completes-147-enforcement-cases-in-first-half-of-2026",
)

MIN_TOTAL_REPORT_RECORDS = 250
MIN_REPORT_ROWS_PER_PAGE = 1
READER_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


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
        self._ignored_depth = 0

    @staticmethod
    def _is_tablesaw_label(attrs: list[tuple[str, str | None]]) -> bool:
        attr_map = {key.casefold(): (value or "") for key, value in attrs}
        classes = attr_map.get("class", "").casefold().split()
        return "tablesaw-cell-label" in classes or attr_map.get("aria-hidden", "").casefold() == "true"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if self._is_tablesaw_label(attrs):
            self._ignored_depth = 1
            return

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
        if self._ignored_depth:
            self._ignored_depth -= 1
            return

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
        if self._ignored_depth:
            return
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


def _plain_markdown(value: str) -> str:
    text = unescape(value or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+(?:\s+\"[^\"]*\")?\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("\\|", "|")
    return " ".join(text.split()).strip()


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


def discover_report_urls_markdown(markdown: str) -> list[str]:
    urls: list[str] = []
    pattern = re.compile(
        r"\[(?P<label>[^\]\n]+)\]\((?P<url>https://www\.pca\.state\.mn\.us/news-and-stories/[^)\s]+)\)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(markdown or ""):
        label_norm = _norm(match.group("label"))
        if "enforcement cases" not in label_norm:
            continue
        if not re.search(r"\b(?:first|second) half of 20\d{2}\b", label_norm):
            continue
        url = match.group("url").rstrip("/")
        if url not in urls:
            urls.append(url)
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
    return _bullet_records(parser.list_items, source_url)


def _bullet_records(items: list[str], source_url: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    pattern = re.compile(
        r"^(?P<party>.+),\s+for\s+(?P<violation>.+?)\s+violations?"
        r"(?:\s+in\s+(?P<location>.*?))?,\s*(?P<penalty>\$[\d,]+(?:\.\d{2})?)\.?$",
        re.IGNORECASE,
    )
    for item in items:
        match = pattern.match(_plain_markdown(item).strip())
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


def _dedupe(records: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in records:
        fingerprint = _fingerprint(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(row)
    return result


def parse_report_page(html: str, *, source_url: str) -> tuple[list[dict[str, str]], int | None]:
    parser = _ReportHtmlParser()
    parser.feed(html or "")
    expected = _expected_case_count(parser.visible_text)
    records = _table_records(parser, source_url) + _list_records(parser, source_url)
    return _dedupe(records), expected


def parse_report_markdown(markdown: str, *, source_url: str) -> tuple[list[dict[str, str]], int | None]:
    expected = _expected_case_count(markdown or "")
    lines = (markdown or "").splitlines()
    records: list[dict[str, str]] = []
    bullet_items: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith(("-", "*")):
            bullet_items.append(line.lstrip("-* ").strip())
        if not (line.startswith("|") and line.endswith("|")):
            index += 1
            continue

        headers = [_norm(_plain_markdown(cell)) for cell in line.strip("|").split("|")]
        if not any("company or individual" in cell for cell in headers) or "net penalty" not in headers:
            index += 1
            continue

        header_map: dict[str, int] = {}
        for cell_index, value in enumerate(headers):
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

        index += 1
        if index < len(lines) and re.match(r"^\|?\s*:?-{3,}", lines[index].strip()):
            index += 1

        while index < len(lines):
            row_line = lines[index].strip()
            if not (row_line.startswith("|") and row_line.endswith("|")):
                break
            cells = [_plain_markdown(cell.strip()) for cell in row_line.strip("|").split("|")]

            def cell(name: str) -> str:
                idx = header_map.get(name)
                return cells[idx].strip() if idx is not None and idx < len(cells) else ""

            party = cell("company")
            violation = cell("violation")
            penalty = cell("penalty")
            if party and violation and penalty:
                records.append(
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
            index += 1
        continue

    records.extend(_bullet_records(bullet_items, source_url))
    return _dedupe(records), expected


def _response_html(response: httpx.Response, *, label: str) -> str:
    if response.status_code >= 400:
        raise MpcaReportError(f"{label} returned HTTP {response.status_code}.")
    content_type = response.headers.get("content-type", "").casefold()
    text = response.text
    if "html" not in content_type and "<html" not in text[:4096].casefold() and "<!doctype html" not in text[:4096].casefold():
        raise MpcaReportError(f"{label} did not return HTML.")
    lowered = text[:262144].casefold()
    challenge_phrases = (
        "radware captcha page",
        "captcha.perfdrive.com",
        "please solve this captcha",
        "we apologize for the inconvenience",
        "widget containing checkbox for hcaptcha",
    )
    if any(marker in lowered for marker in challenge_phrases):
        raise MpcaReportError(f"{label} returned a CAPTCHA/security challenge.")
    return text


def _reader_headers() -> dict[str, str]:
    headers = {
        "Accept": "text/plain",
        "User-Agent": "ParalegalResearchDesk/2.0",
        "X-Engine": "browser",
        "X-Timeout": "30",
        "X-No-Cache": "true",
    }
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _reader_fetch(target_url: str, *, transport: httpx.BaseTransport | None = None) -> str:
    if not target_url.startswith("https://www.pca.state.mn.us/"):
        raise MpcaReportError(f"MPCA reader refused target outside the official MPCA host: {target_url}")

    reader_url = JINA_READER_BASE + target_url
    last_status: int | None = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(
                transport=transport,
                follow_redirects=True,
                timeout=60.0,
                headers=_reader_headers(),
            ) as reader:
                response = reader.get(reader_url)
            last_status = response.status_code
            if response.status_code == 200 and response.text.strip():
                return response.text
            if response.status_code == 401:
                raise MpcaReportError("Public-page reader rejected the optional JINA_API_KEY.")
            if response.status_code not in READER_RETRYABLE_STATUSES:
                raise MpcaReportError(f"Public-page reader returned HTTP {response.status_code}.")
        except MpcaReportError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            last_error = exc

        if attempt == 0:
            time.sleep(1.0)

    detail = f"HTTP {last_status}" if last_status is not None else repr(last_error)
    raise MpcaReportError(f"Public-page reader could not retrieve the official MPCA page ({detail}).")


def _validate_page_records(records: list[dict[str, str]], expected: int | None, *, label: str) -> None:
    if not records:
        raise MpcaReportError(f"{label}: no monetary enforcement rows were recognized.")

    minimum = min(MIN_REPORT_ROWS_PER_PAGE, expected) if expected else MIN_REPORT_ROWS_PER_PAGE
    if len(records) < max(1, minimum):
        raise MpcaReportError(
            f"{label}: only {len(records)} monetary enforcement rows were recognized; expected at least {minimum}."
        )
    if expected is not None and len(records) > expected:
        raise MpcaReportError(
            f"{label}: parsed {len(records)} monetary rows but the page reports only {expected} total enforcement cases."
        )


def fetch_official_report_dataset(
    client: httpx.Client,
    *,
    report_urls: tuple[str, ...] | None = None,
    discover: bool = True,
    min_total_records: int = MIN_TOTAL_REPORT_RECORDS,
    reader_transport: httpx.BaseTransport | None = None,
    allow_reader_fallback: bool = True,
) -> MpcaReportDataset:
    urls = list(report_urls or KNOWN_REPORT_URLS)
    warnings: list[str] = []

    if discover:
        discovered: list[str] = []
        try:
            search_response = client.get(SEARCH_URL)
            search_html = _response_html(search_response, label="MPCA site search")
            discovered = discover_report_urls(search_html)
        except (httpx.HTTPError, MpcaReportError) as direct_exc:
            if allow_reader_fallback:
                try:
                    search_markdown = _reader_fetch(SEARCH_URL, transport=reader_transport)
                    discovered = discover_report_urls_markdown(search_markdown)
                    warnings.append(
                        f"MPCA site-search discovery used the public-page reader because the workstation request failed: {direct_exc}"
                    )
                except MpcaReportError as reader_exc:
                    warnings.append(
                        f"MPCA report discovery could not refresh. Direct: {direct_exc}. Reader: {reader_exc}"
                    )
            else:
                warnings.append(f"MPCA report discovery could not refresh: {direct_exc}")
        for url in discovered:
            if url not in urls:
                urls.append(url)

    rows: list[dict[str, str]] = []
    successful_urls: list[str] = []
    failed_urls: list[str] = []

    for url in urls:
        direct_error: Exception | None = None
        page_rows: list[dict[str, str]] = []
        expected: int | None = None
        fetched_via = "official_mpca_html"

        try:
            response = client.get(url)
            html = _response_html(response, label=f"MPCA enforcement report {url}")
            page_rows, expected = parse_report_page(html, source_url=url)
            _validate_page_records(page_rows, expected, label=url)
        except (httpx.HTTPError, MpcaReportError) as exc:
            direct_error = exc
            if allow_reader_fallback:
                try:
                    markdown = _reader_fetch(url, transport=reader_transport)
                    page_rows, expected = parse_report_markdown(markdown, source_url=url)
                    _validate_page_records(page_rows, expected, label=url)
                    fetched_via = "public_page_reader"
                except MpcaReportError as reader_exc:
                    failed_urls.append(url)
                    warnings.append(
                        f"MPCA report page unavailable or incomplete: {url}. Direct: {direct_error}. Reader: {reader_exc}"
                    )
                    continue
            else:
                failed_urls.append(url)
                warnings.append(f"MPCA report page unavailable or incomplete: {url}: {direct_error}")
                continue

        rows.extend(page_rows)
        successful_urls.append(url)
        if fetched_via == "public_page_reader":
            warnings.append(
                f"Official MPCA report page was retrieved through the public-page reader because the workstation request was challenged: {url}"
            )

    deduped = _dedupe(rows)
    if len(deduped) < max(1, int(min_total_records)):
        raise MpcaReportError(
            f"Official MPCA report fallback produced only {len(deduped)} validated monetary-enforcement records; "
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
        "MPCA report headlines count all enforcement cases, while these tables enumerate monetary-penalty cases. "
        "The fallback validates the monetary rows independently and never treats the headline total as the expected table row count."
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
