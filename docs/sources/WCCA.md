# Wisconsin Circuit Court Access (WCCA / CCAP)

## Current acquisition decision

WCCA research uses the **public Wisconsin Circuit Court Access website through a local browser session**. The paid WCCA REST API is not used and is not the planned acquisition path for this project.

Primary implementation: `backend/app/research/sources/wcca_browser.py` → `WccaPublicBrowserSource`.

Manual fallback/result-normalization implementation: `backend/app/research/sources/wcca.py` and `/wcca-workbench.html`.

## Public-browser workflow

For each approved bidder, the adapter builds a bounded name list from `contractor_name` plus explicitly stored `related_companies` aliases. It opens the normal WCCA public search, accepts the site's ordinary acknowledgement, enters each name into the Business name field, submits the search, and reads the public case-results table.

The current live public layout was verified on 2026-09-26. The search field is `input[name="businessName"]`, the submit control is `button[name="search"]`, and results are returned in `table#caseSearchResults` with Case number, Filing date, County name, Case status, Name, Date of birth, and Caption columns. Case links use `a.case-link` and are retained only when they stay on the official `wcca.wicourts.gov` host.

The adapter attempts to display all result rows and otherwise follows the public results pagination. Case evidence is deduplicated by normalized case number plus matched party name.

## hCaptcha behavior

WCCA loads invisible hCaptcha on the public search page. The application must never solve, bypass, inject tokens into, reverse-engineer, or otherwise defeat hCaptcha.

Normal browser searches may proceed without an interactive challenge. When that happens, the adapter continues automatically. If WCCA presents an interactive challenge, the headed browser remains available for the operator to solve that challenge normally. After the public results page appears, the adapter resumes parsing automatically.

If the challenge is not completed before the configured human-action timeout, the source returns `BLOCKED`. A CAPTCHA/security challenge can never become a clean no-match.

The default persistent browser profile is `.runtime/browser-profiles/wcca`. Microsoft Edge is preferred on the supported Windows workstation, followed by Chrome and then Playwright Chromium when available.

## Identity rules

Automatic positive evidence requires an exact normalized match between the WCCA Name result and the approved bidder name or the specific approved alias that was searched. Case and punctuation differences are normalized, but the adapter does not automatically accept a merely similar company name.

Similar high-confidence names are retained as candidates and produce `AMBIGUOUS_MATCH` / `REVIEW_REQUIRED`. This keeps useful possible matches visible without assigning another company's court record to the bidder.

The adapter does not expand research beyond the approved bidder plus explicitly stored related-company aliases.

## Evidence and master-field safety

A confirmed displayed WCCA case may support comparison-only `circuit_court = Y` evidence plus one `wcca_case = <case number>` record per confirmed case.

A complete public search with no exact approved bidder/alias match stores only:

`wcca_public_search = NO_CURRENTLY_DISPLAYED_MATCH`

It never creates `circuit_court = N`. Public WCCA is not treated as a complete historical court record, so a current public no-match cannot erase or contradict an existing `circuit_court = Y` value.

Automatic master-field proposals remain disabled for WCCA until the firm's exact legacy `circuit_court` rule is documented. `ccap_show150` remains undefined/write-disabled until the firm defines it.

Blocked, timeout, browser-unavailable, layout-changed, partial, pagination-incomplete, and ambiguous results fail closed and cannot become false negatives.

If an earlier alias produces a confirmed positive and a later alias search becomes blocked, the confirmed evidence may be retained while the overall source result remains partial/blocked.

## Manual fallback

`/wcca-workbench.html` remains available for manual evidence entry, review, and recovery when needed. It is not the primary acquisition path.

The `/api/sources/wcca/status` contract reports the live public-browser mode, `rest_api_used = false`, `rest_api_planned = false`, and `captcha_bypass = false`.

## Acceptance

The browser adapter is implemented and registered on `main`, with deterministic regression coverage for clean no-match, confirmed positive, similar-name ambiguity, challenge timeout, layout change, unavailable browser, partial positive retention, source registry behavior, and the no-REST/no-CAPTCHA-bypass contract.

The remaining acceptance gate is a real application run on the supported Windows workstation against representative approved bidders. That test must exercise the local Edge/Chrome session and confirm the current WCCA site behavior end-to-end. Unit/CI success alone is not enough to label the source live verified.

Do not replace this acquisition path with the paid WCCA REST API unless project requirements are explicitly changed.
