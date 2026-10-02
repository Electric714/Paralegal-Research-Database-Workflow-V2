# Minnesota PCA Enforcement Actions

## Purpose

This adapter answers one narrow bidder-database question: whether an approved bidder or explicitly stored related-company alias has a confirmed Minnesota Pollution Control Agency enforcement record relevant to the master field `environmental_violations`.

## Authoritative sources

The acquisition source is MPCA's own public enforcement publications on `www.pca.state.mn.us`: the semiannual enforcement-case news pages and the downloadable `gp2-YYYY.pdf` case summaries linked from the compliance and enforcement page.

The adapter does not call the Tableau host `data.pca.state.mn.us`. That host's detail exports sit behind a Radware challenge and are not used. It also does not use the MPCA WIMN REST API or any CAPTCHA-solving service.

## Acquisition order

1. Download the known official news pages, plus any newer matching pages discovered from MPCA site search, with an ordinary HTTP GET.
2. Download the official `gp2` PDFs linked from the compliance page and parse their case rows.
3. Cache the combined CSV with a manifest of source URL, retrieval time, SHA-256, parsed count, and stated count.
4. If the live documents fail, a recent cache can still surface findings. A cache-only result is never a clean no-match.

A document is count-validated only when the number of parsed rows equals the count that document states. If any document is missing, short, blocked, or mismatched, the dataset is partial. Partial data can support a positive finding or an identity review. It cannot become `SUCCESS_NO_MATCH` and it cannot propose `environmental_violations = N`.

## Official pages and PDFs

Known news pages start with the second half of 2023 and include both halves of 2024 and 2025 plus the first half of 2026. The compliance page also links `gp2-2024.pdf` and `gp2-2025.pdf`.

Headlines often count every closed case, including cases the monetary table does not list. When the parsed row count does not equal that stated count, the page is retained as evidence and marked incomplete.

## Validation

Each downloaded file is hashed. A page or PDF is count-validated only when parsed rows equal the count stated by that file. HTML, challenge pages, HTTP failures, timeouts, and schema changes fail closed. A suspiciously small extract also fails closed against the production row floor.

A complete `SUCCESS_NO_MATCH` is allowed only when every fetched document is count-validated. Otherwise the result stays partial.

## Parser

For each report page the parser:

- recognizes MPCA's enforcement case table headers
- ignores responsive Tablesaw accessibility labels so they cannot contaminate company names or other cell values
- extracts company/individual name, public date, violation location, violation description, net penalty, and case type when present
- also extracts lower-dollar cases that MPCA publishes as list items rather than table rows
- preserves the exact official MPCA URL as evidence provenance
- deduplicates repeated case records

The PDF parser reads the same fields from `gp2` case summaries and splits a trailing location off a legal-suffix company name when that suffix is present.

## Cache safety

The combined dataset is cached with its manifest. The cache is time-limited and must pass the same parser validation as a live extract. Cached findings can be surfaced, but a cache-only no-match is never a clean negative.

## Matching and field semantics

Research scope is limited to `contractor_name` plus explicitly stored `related_companies` aliases. Corporate punctuation/suffix normalization is allowed. Exact normalized identity can produce a confirmed finding. Very similar but non-exact names are review-required and cannot propose a master change.

Confirmed findings emit evidence for `environmental_violations = Y`. Dates, location, violation text, penalty, case type, source party text, official source URL, dataset hash, cache status, and scope status remain supporting evidence.

No acquisition path proposes `environmental_violations = N` merely because a name was absent. Only a count-validated live document set can support a complete no-match.

## Reliability contract

The source is registered as `mn_pca` in the common V2 research pipeline. Failed, malformed, blocked, stale, partial-scope, cached-only no-match, or ambiguous results cannot become clean negatives.

The adapter's health metadata and result payload explicitly report `rest_api_used: false`. Regression tests also reject any request to `services.pca.state.mn.us`, preventing a future change from quietly reintroducing the WIMN REST pathway.
