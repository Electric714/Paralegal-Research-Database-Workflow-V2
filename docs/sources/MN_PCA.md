# Minnesota PCA Enforcement Actions

## Purpose

This adapter answers one narrow bidder-database question: whether an approved bidder or explicitly stored related-company alias has a confirmed Minnesota Pollution Control Agency enforcement record relevant to the master field `environmental_violations`.

## Authoritative sources

The preferred complete acquisition target is the official MPCA **Enforcement actions with penalties** Tableau dataset.

When the Tableau host is unavailable or presents its Radware/hCaptcha challenge, the adapter falls back to ordinary official `pca.state.mn.us` enforcement-report pages published by MPCA itself. These pages contain the agency's mid-year and end-of-year enforcement case tables and are retrieved as normal public HTML pages.

The adapter does **not** use the MPCA WIMN REST API and does not depend on a third-party data API or CAPTCHA-solving service.

## Acquisition order

The source attempts acquisition in this order:

1. A browser-like direct HTTP session warms the official MPCA compliance page and Tableau view and requests the structured Tableau CSV.
2. If that fails, a local Chromium-family browser context opens the Tableau view and requests the CSV with normal browser state.
3. If the background browser is challenged, a persistent headed Edge/Chrome profile is attempted.
4. If the Tableau routes remain blocked, the adapter fetches MPCA's own enforcement-summary pages on `www.pca.state.mn.us`, parses their case tables and lower-penalty case lists, validates page coverage, and combines the records into a local evidence dataset.
5. If a recent previously validated full Tableau extract is available, it may be merged with fresh report-page evidence to broaden historical coverage.

There is no CAPTCHA-solving or challenge-bypass code. The report-page path avoids making the CAPTCHA-protected Tableau host a single point of failure while staying on official MPCA public data.

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

- recognizes the enforcement case table headers
- extracts company/individual name, public date, violation location, violation description, net penalty, and case type when present
- also extracts lower-dollar cases that MPCA publishes as list items rather than table rows
- compares the number of parsed rows with the case count stated on the MPCA page and refuses suspiciously incomplete pages
- keeps the exact official page URL as evidence provenance
- deduplicates repeated case records

Because these published reports do not establish complete all-history coverage, the report-page dataset is explicitly **partial scope**. It can establish a positive finding or an ambiguous identity candidate, but a bidder absent from those pages receives `PARTIAL_RESULTS`, not `SUCCESS_NO_MATCH`.

This distinction is deliberate: a Tableau CAPTCHA must not prevent the other bidders from being researched, but incomplete historical coverage must never become a false clean negative.

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
