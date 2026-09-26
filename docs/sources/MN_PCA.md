# Minnesota PCA Enforcement Actions

## Purpose

This adapter answers one narrow bidder-database question: whether an approved bidder or explicitly stored related-company alias has a confirmed Minnesota Pollution Control Agency enforcement record relevant to the master field `environmental_violations`.

## Authoritative sources

The preferred complete acquisition target is the official MPCA **Enforcement actions with penalties** Tableau dataset.

When the Tableau host is unavailable or presents its Radware/hCaptcha challenge, the adapter falls back to official `pca.state.mn.us` enforcement-report pages published by MPCA itself. These pages contain the agency's recurring mid-year and end-of-year monetary-enforcement case tables.

The adapter does **not** use the MPCA WIMN REST API and does not use a CAPTCHA-solving service.

## Acquisition order

The source attempts acquisition in this order:

1. A browser-like direct HTTP session warms the official MPCA compliance page and Tableau view and requests the structured Tableau CSV.
2. If that fails, a local Chromium-family browser context opens the Tableau view and requests the CSV with normal browser state.
3. If the background browser is challenged, a persistent headed Edge/Chrome profile is attempted.
4. If the Tableau routes remain blocked, the adapter fetches MPCA's own enforcement-summary pages on `www.pca.state.mn.us`, parses their monetary-enforcement tables and lower-penalty case lists, validates the records, and combines them into an evidence dataset.
5. If the workstation IP is also challenged on an ordinary MPCA report page, the adapter may retrieve that same public MPCA page through the project's public-page reader transport. The canonical evidence URL remains the official `pca.state.mn.us` page; the reader is only a transport layer and is not an MPCA data API.
6. If a recent previously validated full Tableau extract is available, it may be merged with fresh report-page evidence to broaden historical coverage.

There is no CAPTCHA-solving or challenge-bypass code. The report-page path prevents the CAPTCHA-protected Tableau host from being a single point of failure while keeping evidence grounded in MPCA's own published pages.

## Tableau validation

A live Tableau extract is considered complete only when it has:

- a recognizable company/regulated-party column
- a recognizable violation column
- detail rows rather than a summary sheet
- a minimum production row-count sanity check
- deterministic record fingerprints and a SHA-256 dataset hash

HTML, CAPTCHA pages, HTTP failures, timeouts, schema changes, and suspiciously small extracts all fail closed.

Only a successfully validated **live full Tableau extract** can support a complete `SUCCESS_NO_MATCH` result.

## Official report-page fallback

The report fallback currently uses MPCA's published enforcement summaries beginning with the second half of 2023 and discovers newer matching report pages from MPCA's normal site search. Known pages include the second half of 2023, both halves of 2024, both halves of 2025, and the first half of 2026.

For each report page the parser:

- recognizes MPCA's enforcement case table headers
- ignores responsive Tablesaw accessibility labels so they cannot contaminate company names or other cell values
- extracts company/individual name, public date, violation location, violation description, net penalty, and case type when present
- also extracts lower-dollar cases that MPCA publishes as list items rather than table rows
- preserves the exact official MPCA report-page URL as evidence provenance
- deduplicates repeated case records
- validates that monetary-enforcement records were actually recovered before accepting a page

MPCA report headlines count all completed enforcement cases, including cases without monetary penalties. The monetary-enforcement table is therefore intentionally smaller than the headline count. The adapter does not incorrectly require the table to equal a fixed percentage of the headline total.

Because these published reports do not establish complete all-history coverage, the report-page dataset is explicitly **partial scope**. It can establish a positive finding or an ambiguous identity candidate, but a bidder absent from those pages receives `PARTIAL_RESULTS`, not `SUCCESS_NO_MATCH`.

This distinction is deliberate: a Tableau CAPTCHA must not prevent the remaining bidders from being researched, but incomplete historical coverage must never become a false clean negative.

## Cache safety

A previously validated full Tableau extract may be cached for short-term continuity. The cache is time-limited and must pass the same parser and row-count validation as the original live export.

Cached findings can be surfaced, but when live complete Tableau data cannot be refreshed, the overall dataset is partial for negative conclusions. A cache-only or report-plus-cache no-match is never a clean negative.

## Matching and field semantics

Research scope is limited to `contractor_name` plus explicitly stored `related_companies` aliases. Corporate punctuation/suffix normalization is allowed. Exact normalized identity can produce a confirmed finding. Very similar but non-exact names are review-required and cannot propose a master change.

Confirmed findings emit evidence for `environmental_violations = Y`. Dates, location, violation text, penalty, case type, source party text, official source URL, dataset hash, cache status, and scope status remain supporting evidence.

No acquisition path ever proposes `environmental_violations = N` merely because a name was absent. Only the research status can represent a complete no-match, and only when the live full Tableau dataset was validated.

## Reliability contract

The source is registered as `mn_pca` in the common V2 research pipeline. Failed, malformed, blocked, stale, partial-scope, cached-only no-match, or ambiguous results cannot become clean negatives.

The adapter's health metadata and result payload explicitly report `rest_api_used: false`. Regression tests also reject any request to `services.pca.state.mn.us`, preventing a future change from quietly reintroducing the WIMN REST pathway.
