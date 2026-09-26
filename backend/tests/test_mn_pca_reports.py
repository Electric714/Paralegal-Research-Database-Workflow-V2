from __future__ import annotations

import csv
import io

import httpx

from app.research.sources.mn_pca_reports import (
    JINA_READER_BASE,
    fetch_official_report_dataset,
    parse_report_markdown,
    parse_report_page,
)


REPORT_URL = "https://www.pca.state.mn.us/news-and-stories/mpca-completes-147-enforcement-cases-in-first-half-of-2026"

TABLESAW_HTML = """<!doctype html><html><head>
<script>ssConf('cu', 'validate.perfdrive.com, ssc');</script>
</head><body>
<h1>MPCA completes 147 enforcement cases in first half of 2026</h1>
<table class="tablesaw">
<thead><tr><th>Public date</th><th>Company or individual(s)</th><th>Violation location</th><th>Violation</th><th>Net penalty</th></tr></thead>
<tbody>
<tr>
<td><strong class="tablesaw-cell-label" aria-hidden="true">Public date</strong><span class="tablesaw-cell-content">3/5/2026</span></td>
<td><strong class="tablesaw-cell-label" aria-hidden="true">Company or individual(s)</strong><span class="tablesaw-cell-content">Acme Construction LLC</span></td>
<td><strong class="tablesaw-cell-label" aria-hidden="true">Violation location</strong><span class="tablesaw-cell-content">Minneapolis</span></td>
<td><strong class="tablesaw-cell-label" aria-hidden="true">Violation</strong><span class="tablesaw-cell-content">Construction stormwater</span></td>
<td><strong class="tablesaw-cell-label" aria-hidden="true">Net penalty</strong><span class="tablesaw-cell-content">$9,663</span></td>
</tr>
</tbody></table>
</body></html>"""

READER_MARKDOWN = """Title: MPCA completes 147 enforcement cases in first half of 2026

URL Source: https://www.pca.state.mn.us/news-and-stories/mpca-completes-147-enforcement-cases-in-first-half-of-2026

| Public date | Company or individual(s) | Violation location | Violation | Net penalty |
| --- | --- | --- | --- | --- |
| 3/5/2026 | [Acme Construction LLC](https://www.pca.state.mn.us/news-and-stories/acme-fined) | Minneapolis | Construction stormwater | $9,663 |
| 4/1/2026 | Other Builder Inc | St Paul | Hazardous waste | $1,200 |
"""

CAPTCHA_HTML = """<!doctype html><html><title>Radware Captcha Page</title>
<body>We apologize for the inconvenience. Please solve this CAPTCHA.</body></html>"""


def test_tablesaw_accessibility_labels_are_not_part_of_record_values():
    records, expected = parse_report_page(TABLESAW_HTML, source_url=REPORT_URL)

    assert expected == 147
    assert len(records) == 1
    assert records[0]["Public date"] == "3/5/2026"
    assert records[0]["Company or individual(s)"] == "Acme Construction LLC"
    assert records[0]["Violation location"] == "Minneapolis"
    assert records[0]["Violation description"] == "Construction stormwater"
    assert records[0]["Net penalty"] == "$9,663"


def test_normal_radware_script_reference_is_not_mistaken_for_captcha():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == REPORT_URL
        return httpx.Response(
            200,
            text=TABLESAW_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    dataset = fetch_official_report_dataset(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        report_urls=(REPORT_URL,),
        discover=False,
        min_total_records=1,
        allow_reader_fallback=False,
    )

    assert dataset.record_count == 1
    assert dataset.page_count == 1


def test_markdown_reader_parser_handles_official_table_and_links():
    records, expected = parse_report_markdown(READER_MARKDOWN, source_url=REPORT_URL)

    assert expected == 147
    assert len(records) == 2
    assert records[0]["Company or individual(s)"] == "Acme Construction LLC"
    assert records[0]["Net penalty"] == "$9,663"
    assert records[1]["Company or individual(s)"] == "Other Builder Inc"


def test_headline_total_is_not_used_as_expected_monetary_table_row_count():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == REPORT_URL
        return httpx.Response(
            200,
            text=TABLESAW_HTML,
            headers={"content-type": "text/html"},
            request=request,
        )

    dataset = fetch_official_report_dataset(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        report_urls=(REPORT_URL,),
        discover=False,
        min_total_records=1,
        allow_reader_fallback=False,
    )

    # The page says 147 total enforcement cases, but only monetary-penalty cases
    # belong in this adapter's evidence dataset. One validated monetary row in this
    # synthetic fixture must therefore remain valid rather than being rejected by a
    # bogus percentage-of-147 threshold.
    assert dataset.record_count == 1


def test_captcha_on_workstation_request_falls_back_to_public_page_reader():
    def direct_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == REPORT_URL
        return httpx.Response(
            200,
            text=CAPTCHA_HTML,
            headers={"content-type": "text/html"},
            request=request,
        )

    def reader_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JINA_READER_BASE + REPORT_URL
        return httpx.Response(200, text=READER_MARKDOWN, request=request)

    dataset = fetch_official_report_dataset(
        httpx.Client(transport=httpx.MockTransport(direct_handler), follow_redirects=True),
        report_urls=(REPORT_URL,),
        discover=False,
        min_total_records=1,
        reader_transport=httpx.MockTransport(reader_handler),
        allow_reader_fallback=True,
    )

    assert dataset.record_count == 2
    assert dataset.page_count == 1
    assert any("public-page reader" in warning for warning in dataset.warnings)

    rows = list(csv.DictReader(io.StringIO(dataset.csv_bytes.decode("utf-8"))))
    assert rows[0]["Company or individual(s)"] == "Acme Construction LLC"
    assert rows[0]["Source URL"] == REPORT_URL
