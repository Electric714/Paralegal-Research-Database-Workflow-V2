# Violation Tracker source

## Purpose

The `violation_tracker` adapter researches the free public Violation Tracker database from Good Jobs First for bidders already present in the approved master database. It is a cross-check/enforcement-evidence source, not a contractor-discovery crawler, and it does not own or automatically update any master bidder field.

Public search entry point: `https://violationtracker.goodjobsfirst.org/summary`

The implementation uses the public HTML search interface only. It does not use a paid download, authentication, challenge bypass, CAPTCHA solving, or bulk site harvesting.

## Verified public search behavior

The current advanced-search form submits a GET request to `/summary`. For an exact company/current-parent lookup the adapter sends:

- `company_op = =`
- `company = <approved bidder name or approved related-company alias>`

A key behavior of Violation Tracker is that an exact company search can return both records where the searched name is the penalized company and records where it is only the current parent. The adapter therefore never treats every returned row as a violation of the bidder.

## Acquisition flow

For each selected bidder the adapter:

1. Searches the approved `contractor_name` exactly as stored.
2. Searches each explicitly stored `related_companies` alias separately.
3. Verifies that the final response URL still contains the exact company filter before parsing the page.
4. Requires either the known result-table structure or the explicit `No Violation Tracker results found` marker.
5. For every result row, verifies that the searched identity matches either the penalized `Company` field or the `Current Parent` field. If neither matches, the query is rejected instead of being interpreted as a negative.
6. Follows public pagination links while preserving the same exact company filter, with a bounded page safety cap.
7. Deduplicates records by the canonical Violation Tracker detail URL when available, with a deterministic fingerprint fallback.
8. Stores the result as research evidence. It never adds contractors discovered on Violation Tracker to the master database.

## Identity classification

A row is a confirmed Violation Tracker finding only when the penalized `Company` is equivalent to the searched approved bidder name or approved alias after conservative punctuation/legal-suffix normalization.

A row where only `Current Parent` matches is retained as a parent-only candidate and produces `AMBIGUOUS_MATCH` when there is no direct penalized-company match. Parent-only records require human review and are not represented as bidder violations.

No fuzzy match can create a confirmed finding in this adapter.

## Result semantics

`SUCCESS_WITH_FINDINGS` means every approved-name lookup completed and at least one direct penalized-company record was found.

`SUCCESS_NO_MATCH` means every approved-name lookup demonstrably completed, each query was preserved and validated, and no direct or parent-only candidate records were returned. A valid empty result is therefore a successful clean negative, not a failure.

`AMBIGUOUS_MATCH` means all searches completed but Violation Tracker returned only current-parent candidates requiring identity review.

`PARTIAL_RESULTS` means useful/valid evidence exists or another approved-name query completed, but at least one required alias/page lookup did not complete. It is never a clean negative.

Blocked, timeout, HTTP, parser/layout, and source-unavailable conditions remain explicit failure states when no usable query completed. A 403/429 or an interactive anti-bot challenge is reported as blocked; the adapter does not attempt to bypass it.

## Pagination and filter validation

The public results page reports a total result count and exposes numbered pagination links. The adapter compares the number of verified rows reached against that reported total. If all reported rows cannot be reached, it reports `PAGINATION_INCOMPLETE`/`PARTIAL_RESULTS` rather than silently declaring the search complete.

The adapter also fails closed if a response contains rows that match neither the searched penalized-company identity nor the searched current-parent identity. This specifically protects against a changed/ignored search parameter returning a broad unfiltered result set.

## Evidence retained

For direct records the evidence snapshot retains, when available:

- penalized company
- current parent
- current parent industry
- primary offense type
- year
- agency
- penalty text and parsed amount
- duplicate-penalty marker
- canonical Violation Tracker detail URL
- current-parent URL
- searched name and whether it came from the master name or an approved alias
- match basis (`penalized_company` or `current_parent_only`)
- per-query URLs, page counts, reported result count, HTTP status, and visible data version
- adapter/parser versions and warnings

Parent-only candidates remain in the normalized evidence payload even when they are not promoted to confirmed evidence records.

## Master-field ownership

`violation_tracker` intentionally owns no master fields. The database covers many different enforcement categories and often overlaps with authoritative sources such as OSHA, environmental agencies, labor agencies, and courts. A Violation Tracker hit can identify new information that merits review or follow-up against those authoritative sources, but it cannot directly generate a proposed change to fields such as `osha`, `environmental_violations`, `prevailing_wage_violations`, `federal_court`, or `misc_violations`.

That ownership rule should remain conservative until the firm's exact legacy field semantics are explicitly defined and approved.