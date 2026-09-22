# PACER federal-court research source

## What PACER stands for

PACER is the **Public Access to Court Electronic Records** service operated by the federal Judiciary. It provides electronic public access to federal court records.

For this project, the intended master field is `federal_court`.

## Recommended acquisition method

Use the official PACER APIs rather than scraping the PACER website.

PACER publishes:

- a **PACER Authentication API** for programmatic login and token issuance
- a **PACER Case Locator (PCL) API** for programmatic nationwide federal case and party searches

The PCL is the nationwide index for district, bankruptcy, and appellate cases and is updated daily. PACER states that the public PCL API searches the same data set with the same search functionality as the PCL application.

The initial implementation should use PCL party searches only. It should not scrape individual court CM/ECF websites and it should not automatically purchase docket reports or case documents merely to decide the `federal_court` flag.

## Why the PCL API is the best fit

The current bidder database uses `federal_court` as a Y/N-style field. The supplied example database contains both `Y` and `N` values.

The PCL party-search response can provide enough evidence to determine that an approved bidder or explicit related-company alias appears as a party in at least one federal case. Relevant response fields include party name, court ID, case number, case title, party role/type, jurisdiction/court type, filed/closed/dismissed dates, bankruptcy chapter/disposition when applicable, and a case link.

This means the first PACER implementation can answer the narrow comparison question without automatically retrieving chargeable docket reports or filings.

## Authentication and credential handling

PACER requires a valid PACER account. The Authentication API accepts PACER credentials and returns an authentication token used in PCL API requests. PACER documentation says the token should be reused while valid rather than requesting a new token for every search.

Recommended local-app workflow:

1. Add a `Connect PACER` action in the app.
2. Prompt for PACER username and password, plus optional client code and one-time passcode when required by the account.
3. Send the credentials directly to the PACER Authentication API.
4. Keep the returned authentication token only in backend process memory.
5. Do not write the PACER password, one-time passcode, or authentication token into SQLite, evidence, logs, or exported files.
6. When the token expires or PACER returns an authentication/session error, mark tasks `AUTH_REQUIRED` or `SESSION_EXPIRED` and require reauthentication.

For development, use PACER's QA environment and QA account. PACER states that QA searches are not billable.

## Search flow

For each bidder selected for PACER research:

1. Build the bounded search-name set from `contractor_name` plus explicitly stored `related_companies` aliases.
2. Search the PCL party endpoint for each permitted name.
3. Use the immediate paged search endpoint rather than a bulk-download job for ordinary bidder checks.
4. Parse the first result page and the PACER receipt.
5. Retrieve additional pages only when required to prove the search complete; pages are billed as they are retrieved.
6. Client-side filter and score returned party names against the exact bidder/alias being searched.
7. Treat exact normalized business-name matches as strong candidates.
8. Route fuzzy, partial, common-name, individual-person, or otherwise uncertain matches to human identity review rather than guessing.
9. Retain all confirmed case hits as evidence, but create at most one `federal_court` proposal for the bidder.

PACER's `/parties/find` search is page-based. The current API documentation states that an immediate page contains up to 54 matches and that each retrieved page is billable.

## `federal_court` comparison semantics

A confirmed PACER case hit should support:

- current `federal_court = N` or blank -> propose `Y`
- current `federal_court = Y` -> no master-field change; add/update research evidence only

A no-match should **not** automatically propose `N`, and an existing `Y` should never be changed to `N` from a later PACER search.

Reasons:

- federal-court involvement is historical; a previously verified case does not cease to have existed
- party-name searches can miss records because of spelling, legal-name changes, aliases, punctuation, entity naming, or source/index limitations
- the project's core rule forbids turning incomplete or uncertain research into a false negative

Therefore PACER should be treated as a positive-evidence source for `federal_court`. A complete no-match may be stored as a completed research result, but it should not erase or downgrade the master field.

## Identity matching

PCL search results do not provide the same address-based identity corroboration available from sources such as WDFI. PACER matching should therefore be intentionally conservative.

Recommended rules:

- exact normalized primary business name -> strong candidate
- exact normalized explicitly stored related-company alias -> strong candidate but retain the alias relationship in evidence
- punctuation/legal-suffix-only differences -> acceptable normalization
- fuzzy/substring-only match -> human review
- bidder names that appear to be individuals -> human review unless an existing SAME_ENTITY judgment already identifies the PACER party/case relationship
- multiple materially different returned party names -> human review

Remember SAME_ENTITY / DIFFERENT_ENTITY judgments using stable case/party identity data wherever practical.

## Cost controls

PACER is fee-sensitive and the app must make cost visible.

As of September 2026, PACER charges $0.10 per billable page for searches, including searches that return no matches. PACER has announced a temporary increase to $0.12 per page beginning January 1, 2027. Search-result charges are not subject to the per-document $3 cap.

The PCL API response includes receipt information such as `billablePages`, `searchFee`, transaction date, search description, and report ID. Preserve those values with the research snapshot.

Recommended controls:

- show an explicit PACER fee warning before a run
- optionally require operator confirmation before beginning a PACER batch
- show running/finished PACER search cost using returned receipts rather than a hard-coded estimate
- do not automatically fetch documents or docket reports
- do not automatically use PCL batch-download endpoints for ordinary bidder research
- reuse authentication tokens rather than reauthenticating for every bidder
- avoid unnecessary duplicate alias searches
- cache identical completed query evidence within a research run when safe
- keep concurrency low
- follow PACER guidance that large automated data pulls should occur between 6 p.m. and 6 a.m. Central Time

## Evidence to retain

For each confirmed or reviewable PACER hit, retain when returned:

- bidder ID and searched name/alias
- returned party name
- court ID
- case ID
- full case number
- case title
- party type and party role
- jurisdiction/court type
- date filed
- date closed/terminated
- date dismissed
- bankruptcy chapter/disposition where applicable
- PACER case link
- source/API environment
- pagination/completeness information
- PACER receipt/report ID
- billable pages and actual search fee
- retrieval time
- adapter/parser version
- identity score/status and human judgment when applicable

Do not retain PACER passwords, one-time codes, or authentication tokens in evidence.

## Failure and completeness semantics

Map failures into the existing V2 result model rather than collapsing them into `N`:

- missing PACER connection -> `AUTH_REQUIRED`
- expired token -> `SESSION_EXPIRED` / reauthentication required
- authentication failure -> `AUTH_REQUIRED`
- timeout/network error -> `TIMEOUT` or `HTTP_ERROR`
- incomplete pagination -> `PARTIAL_RESULTS` / `PAGINATION_INCOMPLETE`
- invalid/unexpected API response -> `PARSER_FAILURE` or equivalent visible failure
- uncertain identity -> `AMBIGUOUS_MATCH` / `REVIEW_REQUIRED`
- completed query with no candidate -> `SUCCESS_NO_MATCH`, with no `federal_court=N` proposal

## Implementation phases

### Phase 1 — safe PACER integration

- add PACER connection/authentication session support
- add a PCL API client
- implement bounded party-name searches
- parse receipt/page/case/party data
- integrate identity review
- emit positive `federal_court=Y` proposals only
- store cost/provenance evidence
- add offline fixtures for positive, no-match, ambiguity, auth failure, pagination, and malformed responses
- test against PACER QA before any production use

### Phase 2 — operator UX

- PACER connect/disconnect state in the source UI
- optional OTP/client-code fields
- fee warning before run
- actual PACER cost summary after run
- explicit links to PACER case records

### Phase 3 — optional deeper case review

Only if the firm's workflow actually needs it, add an explicit user-triggered action to open or retrieve docket/case detail. Do not make paid document retrieval part of the normal `federal_court` Y/N comparison run.

## Decision

**Recommended design: official PACER Authentication API + PACER Case Locator party-search API, with in-memory credentials/token handling, conservative identity review, positive-only `federal_court` proposals, and explicit cost accounting.**

Do not build this source as a browser scraper, and do not automatically retrieve paid case documents just to populate the master Y/N field.
