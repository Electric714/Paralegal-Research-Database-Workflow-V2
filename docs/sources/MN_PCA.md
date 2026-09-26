# Minnesota PCA Enforcement Actions

## Purpose

This adapter answers one narrow bidder-database question: whether an approved bidder or explicitly stored related-company alias has a confirmed Minnesota Pollution Control Agency enforcement record relevant to the master field `environmental_violations`.

## Authoritative source

The authoritative acquisition target is the official MPCA **Enforcement actions with penalties** Tableau dataset. The adapter does **not** use the MPCA WIMN REST API, does not depend on a third-party data API, and does not replace the enforcement dataset with annual-report scraping.

The desired artifact is one validated statewide enforcement-detail CSV. Bidder matching happens locally after that extract is acquired, so the adapter does not make one remote source query per bidder.

## Hardened Tableau acquisition

The adapter attempts the official Tableau extract in this order:

1. A browser-like direct HTTP session warms the official MPCA compliance page and Tableau view, retaining first-party cookies, and then requests the structured CSV export with the Tableau view as its referrer.
2. If direct retrieval is blocked or malformed, a local Chromium-family browser session opens the real Tableau view and requests the CSV through that browser context so normal browser cookies and storage are shared.
3. If the background browser is challenged, a persistent **headed** system Edge/Chrome profile is attempted. The application keeps ordinary browser state between runs so a normal workstation session can remain usable when the public site treats fresh automated sessions differently.
4. If every live path fails, the adapter may use a recent previously validated full Tableau extract from its local cache for continuity. Cached results are explicitly partial evidence only.

There is no CAPTCHA-solving or challenge-bypass code. A challenge that cannot be cleared by a normal local browser session remains a blocked live acquisition rather than being converted into a false result.

## Dataset validation

Before any dataset can be treated as complete, the adapter requires:

- a recognizable company/regulated-party column
- a recognizable violation column
- valid detail rows rather than a Tableau summary sheet
- a minimum production row-count sanity check, preventing an obviously truncated or summary export from becoming authoritative
- deterministic per-record fingerprints and a SHA-256 dataset hash

HTML, CAPTCHA pages, HTTP failures, timeouts, schema changes, and suspiciously small extracts all fail closed.

A successfully validated live extract is cached only after validation. Cache write failure does not downgrade a successful live acquisition.

## Cache safety

The cache exists only to preserve useful positive evidence during a temporary MPCA outage. It is time-limited and must pass the same parser and row-count validation as a live export.

A cached confirmed enforcement record may still be surfaced as a finding, but the result is marked `PARTIAL`. A no-match found only in cache is **never** `SUCCESS_NO_MATCH`; it is `PARTIAL_RESULTS` and cannot be treated as a clean negative.

## Matching and field semantics

Research scope is limited to `contractor_name` plus explicitly stored `related_companies` aliases. Corporate punctuation/suffix normalization is allowed. Exact normalized identity can produce a confirmed finding. Very similar but non-exact names are review-required and cannot propose a master change.

Confirmed findings emit evidence for `environmental_violations = Y`. Dates, location, violation text, penalty, case type, source party text, dataset hash, and whether the dataset came from cache remain supporting evidence.

A clean live no-match is research status only. It never proposes `environmental_violations = N`, and it never clears an existing positive value.

## Reliability contract

The source is registered as `mn_pca` in the common V2 research pipeline. Failed, malformed, blocked, stale, cached-only no-match, or ambiguous results cannot become clean negatives. The adapter's health metadata explicitly reports `rest_api_used: false` so a future change cannot quietly swap the acquisition architecture back to the REST API without being visible in tests and diagnostics.
