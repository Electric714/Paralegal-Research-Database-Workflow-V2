# Violation Tracker Integration

**Status:** Production adapter implemented and registered  
**Acquisition:** Free public website only  
**Target source:** https://violationtracker.goodjobsfirst.org/  
**Reviewed:** 2026-09-21

## Role in V2

Violation Tracker is the name of Good Jobs First's cross-agency enforcement database; it is not an acronym.

V2 uses it as a **cross-agency evidence and discrepancy source**. It can surface workplace-safety, environmental, wage-and-hour, contracting, consumer-protection, discrimination, litigation, and other enforcement records that may overlap with the project's dedicated OSHA, environmental, labor, court, and debarment sources.

That overlap is intentional, but Violation Tracker is **evidence-only** in this project. `backend/app/research/field_mappings.py` gives `violation_tracker` an empty `owned_fields` set. Findings may be stored and compared, but they cannot directly create master-field proposals.

## Implemented free public-search method

The registered adapter is:

`backend/app/research/sources/violation_tracker.py`

The current public site supports an exact **Company or Current Parent** query with:

`GET /summary?company_op=%3D&company=<approved name>`

A live review on 2026-09-21 confirmed that this route applies the filter and returns the normal Violation Tracker result table. Pagination links currently continue on the site root while preserving the same `company_op`, `company`, and `page` parameters. The adapter therefore accepts both `/summary` and `/` result paths, but only on the expected HTTPS host and only when the exact company filter is preserved.

The earlier research note about relying on the Advanced Search POST form is superseded by the verified public summary-query route used by the production adapter.

No paid subscription, paid download, API key, subscriber export, or full-dataset access is required or used.

## Approved names and aliases

For each bidder already in the master database, the adapter searches:

1. `contractor_name`
2. every approved alias/DBA already stored in `related_companies`

Aliases are split only on semicolons, pipes, and newlines. Commas are deliberately not treated as alias separators because they commonly occur inside legal company names.

Each approved name is searched separately and all outcomes are aggregated into one `SourceResult`.

## Result semantics

The adapter follows the existing V2 source-result model.

### Match

`SUCCESS_WITH_FINDINGS` is returned when a result row's penalized-company name matches the approved searched name after conservative normalization. Legal suffix normalization is allowed; internal token boundaries are preserved.

Direct findings are retained as `EvidenceRecord` objects with `field_name="violation_tracker"`. Because Violation Tracker owns no master fields, those evidence records cannot create automatic master changes.

### No match

`SUCCESS_NO_MATCH` with `CompletenessStatus.COMPLETE` is returned only when **every approved-name search completes successfully** and no qualifying records remain.

An empty result is a valid successful lookup. It is not a failure merely because there is no new information.

### Ambiguous

The public exact query searches both Company and Current Parent. A row that matches only the current-parent field is therefore treated as an identity-review candidate, not as proof that the bidder itself committed the violation.

If only parent-level candidates exist, the adapter returns `AMBIGUOUS_MATCH`. If direct findings and parent-only candidates both exist, the direct findings remain confirmed evidence and the parent-only rows are retained separately for review with an explicit warning.

### Partial or failed

A blocked, changed, incomplete, or unverifiable query never becomes a clean negative. Depending on what completed successfully, the adapter returns an appropriate failure status or `PARTIAL_RESULTS`.

## Fail-closed validation

Before accepting a page, the adapter verifies that:

- the final URL uses HTTPS
- the hostname is `violationtracker.goodjobsfirst.org`
- the path is the expected `/summary` or `/` results path
- `company_op` remains exactly `=`
- `company` remains exactly the approved searched name
- the expected result table or explicit no-results marker is present
- the reported total result count is present for positive searches
- table rows do not exceed the reported total
- each returned row matches either the searched penalized company or the searched current parent
- pagination preserves the same exact query
- all reported records are accounted for by **unique source-record IDs**, not merely by raw row count

The last rule prevents a repeated or duplicated pagination page from falsely satisfying the reported result count.

If any of those invariants fail, the adapter rejects the response rather than treating it as a negative.

## Identity normalization

Identity matching is deliberately conservative. It uses the project's standard text normalization plus common corporate-suffix normalization.

It does **not** remove every internal space. Collapsing `"AB Construction"` and `"A B Construction"` into the same spaceless token can merge genuinely different legal names, so token boundaries are preserved.

Fuzzy matching is not used to establish confirmed Violation Tracker identity.

## Pagination and record identity

The public search can return multiple pages. The adapter follows only same-host pagination URLs that preserve the exact approved-name filter and stops at the configured safety cap.

Completeness is measured with unique Violation Tracker record IDs. The preferred record ID comes from the canonical individual-record URL slug. When no canonical detail URL is available, a deterministic fingerprint is used as a fallback.

If the site reports more records than can be uniquely verified within the available pagination links or safety cap, the outcome becomes `PAGINATION_INCOMPLETE` and the bidder-level result remains partial rather than clean.

## Parent-only records

Violation Tracker's exact query can return subsidiaries because the searched company is their current parent. The adapter keeps this distinction explicit:

- `penalized_company` = direct approved-name finding
- `current_parent_only` = identity-review candidate

Parent-only rows are never silently promoted into direct bidder violations. They are stored separately in `normalized_payload["parent_only_records"]`, and a parent-only evidence summary is retained even when direct findings are present in the same search.

## Evidence retained

For result rows the adapter currently retains:

- stable source record ID
- company
- current parent
- current parent industry
- primary offense
- year
- agency
- displayed penalty text
- parsed penalty amount
- duplicate-penalty marker
- canonical detail URL when present
- current-parent URL when present
- approved query name and whether it came from the master name or an approved alias
- match basis
- per-query pagination/request diagnostics
- reported result count
- source data-version marker when exposed
- adapter/parser versions

The source remains evidence-only, so these records support review and discrepancy detection without silently changing the approved bidder database.

## Duplicate handling

Violation Tracker marks some penalty entries as duplicates or multi-agency announcements. The parser preserves that marker.

The adapter also deduplicates pagination by stable source record ID before deciding a result set is complete. It does not sum penalties into a master field.

Cross-source reconciliation with OSHA, environmental, labor, or court findings remains a separate concern; overlapping source records should retain their own provenance.

## Blocking and site changes

The adapter does not bypass CAPTCHA, authentication, subscriber controls, anti-bot challenges, or rate limits.

HTTP 403/429 responses and recognizable interactive challenge pages become `BLOCKED`. Server errors, timeouts, layout changes, result-count inconsistencies, ignored filters, and incomplete pagination remain explicit non-clean states.

## Request discipline

Research is contractor-scoped rather than site-wide. The adapter never crawls the full Violation Tracker database and never discovers unrelated contractors for insertion into the master database.

`MAX_PAGES_PER_NAME` provides a hard pagination safety cap. If a very large exact/current-parent query cannot be completely traversed within that cap, the result fails closed as incomplete.

## Tests

Offline tests cover:

- parsing the public result table
- clean no-match across the master name and approved aliases
- direct approved-alias match
- parent-only ambiguity
- unrelated rows / ignored-filter protection
- one successful name plus one failed alias -> partial
- rate limiting and anti-bot challenge handling
- pagination before declaring completeness
- evidence-only field ownership
- internal-whitespace identity collisions
- repeated pagination rows not faking completeness

Normal CI uses mocked HTML/HTTP responses and does not hit the live site.

## Current limitations and future improvements

The adapter currently works from the public result table rather than fetching every individual detail page. That keeps request volume low and is sufficient for the current evidence-only workflow, but individual detail-page enrichment could later add facility address, case identifiers, action type, detailed descriptions, and originating-source links when those fields are needed for human review.

Caching across separate research runs could also reduce repeated requests. Any future cache must preserve source version/time and must never turn stale or incomplete data into a clean negative.

## Official material reviewed

- Violation Tracker homepage: https://violationtracker.goodjobsfirst.org/
- User Guide: https://violationtracker.goodjobsfirst.org/pages/user-guide
- Quick Start: https://violationtracker.goodjobsfirst.org/pages/quick-start
- Data Sources: https://violationtracker.goodjobsfirst.org/pages/violation-tracker-data-sources
- Update Log: https://violationtracker.goodjobsfirst.org/pages/update-log
- Good Jobs First Terms of Service: https://goodjobsfirst.org/terms-of-service/
