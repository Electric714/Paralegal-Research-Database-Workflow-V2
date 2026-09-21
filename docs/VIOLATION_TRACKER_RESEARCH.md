# Violation Tracker Integration Research

**Branch:** `feature/violation-tracker-public-search`  
**Status:** Research/design complete; production adapter not yet implemented  
**Target source:** https://violationtracker.goodjobsfirst.org/  
**Research date:** 2026-09-21

## What the name means

**Violation Tracker is not an acronym.** It is the product/database name. It is produced by the Corporate Research Project of Good Jobs First.

The U.S. Violation Tracker combines enforcement and litigation information concerning corporate misconduct. Its current public site says it contains more than 700,000 civil and criminal cases from more than 450 agencies, with cases going back to 2000 and selected private litigation also included.

## Why this source is useful to our project

Violation Tracker is unusually valuable as a **cross-agency enforcement index**. Instead of answering only one narrow question, it can surface records involving workplace safety, environmental enforcement, wage and hour violations, government contracting, consumer protection, discrimination, False Claims Act matters, antitrust, financial regulation, bribery, and selected litigation.

That breadth also creates the main integration risk: Violation Tracker often republishes or standardizes information originating from sources that this project researches separately, including OSHA, EPA/ECHO, Department of Labor data, courts/PACER, state agencies, and other enforcement bodies. It also warns that duplicate or partially duplicate penalty entries can exist and that its own company-level totals are adjusted to reduce double counting.

For that reason, Violation Tracker should initially be treated as a **research evidence and discrepancy-detection source**, not as an automatic replacement for the authoritative source adapters already assigned to specific bidder fields.

## Current V2 repository fit

The current repository already has the correct safety posture for this source. `backend/app/research/field_mappings.py` defines `violation_tracker` with an empty `owned_fields` set and states that field ownership must be confirmed before automatic proposals are allowed.

That should remain unchanged for the first implementation.

The current bidder schema contains fields that can be compared against Violation Tracker evidence, including:

- `osha`
- `osha_severe_violations`
- `years`
- `state_federal_debarment`
- `federal_court`
- `circuit_court`
- `environmental_violations`
- `prevailing_wage_violations`
- `better_business_bureau_complaints`
- `misc_violations`
- `tax_liability`

However, comparison does **not** mean Violation Tracker should own those fields. Existing dedicated authoritative sources should continue to own their assigned fields.

## Data access research

### Public access

The site publicly supports basic search, summary pages, advanced search, and individual violation-record pages. Public search results expose useful columns including company, current parent, parent industry, primary offense type, year, agency, and penalty amount. Individual records can contain much richer data, including company name, current parent, penalty/date, offense group/type, agency, action type, civil/criminal status, case identifiers, facility address information, NAICS information, source links, and links back to originating agency records.

The site documentation describes five company-name matching modes in Advanced Search: starts with, exact/equal, contains any word, contains all words, and ends with.

The live Advanced Search form currently submits to `/search.php` using POST. This is important: implementation should inspect and submit the site's real form parameters rather than guessing GET parameters. A research probe that guessed an unsupported GET operator produced an unfiltered result set, demonstrating that a parser must fail closed if the site ignores or changes search parameters.

### Downloads and subscription

Searching and displaying results are free. Spreadsheet downloads are subscriber-only. Current published tiers allow up to 1,000, 5,000, or 10,000 downloaded records per search depending on plan.

The site also states that people needing a full dataset with corporate identifiers for academic purposes can contact Good Jobs First.

No documented public API was found in the official user guide, quick-start material, subscription page, or site search reviewed for this research. The documented access methods are the site search/summary interfaces and subscriber downloads.

### Terms and automation constraint

Good Jobs First's Terms of Service grant access for internal use subject to the Terms and posted data limits. Their acceptable-use section specifically prohibits automated activity that intentionally imposes unreasonable burdens on the service or circumvents technological blockers.

Therefore this project must **not** use aggressive crawling, scrape the complete site, evade blocks, bypass authentication/subscriber controls, or attempt to retrieve subscriber-only downloads without authorization.

A production adapter should use a conservative request rate, caching, retries/backoff, an identifiable user agent, no authentication bypass, and no CAPTCHA/blocker circumvention. If the site blocks automated requests, the result must become `blocked`/`partial` rather than triggering bypass logic.

## Recommended integration strategy

### Recommended Phase 1: targeted public lookup adapter

For each contractor already present in the approved master database, perform only the minimum public search needed for that contractor. Search the approved contractor name and, when available, approved `related_companies` aliases. Do not discover and auto-add unrelated companies.

The adapter should parse the candidate result rows and then open individual Violation Tracker records only for candidate matches that need identity verification or additional evidence.

This keeps request volume proportional to the master database rather than the size of Violation Tracker and aligns with the project's rule that the master list controls research scope.

## Contractor identity matching

Violation Tracker's own parent-company system is useful but cannot be treated as proof that a violation belongs to the exact bidder in our master database. Violation Tracker primarily links records to the company's **current parent**, even when ownership differed when the penalty occurred. Historical-parent data is partly subscriber-only.

Recommended matching hierarchy:

1. **Strongest:** exact normalized penalized-company name plus matching facility location/address information when present.
2. **Strong:** exact normalized master contractor name matching the penalized company name.
3. **Strong but separately labeled:** an approved `related_companies` alias matching the penalized company.
4. **Corroborating:** facility city/state/ZIP or address agrees with the master record.
5. **Supporting only:** current-parent relationship agrees with known company information.
6. **Ambiguous:** only a parent name matches, a generic company name matches, or address/location information conflicts or is absent where needed to distinguish entities.

The adapter must preserve which name caused the match: master legal name, related-company alias, or parent-company relationship.

A parent-only match should never automatically become a positive exact-bidder finding.

## Required result states

For each contractor lookup, search the master legal name plus every approved alias/DBA in `related_companies` and aggregate the searches into one source result.

The user-facing logic should map to four simple states:

- **MATCH** — one or more records can be tied confidently to the contractor or an approved alias.
- **NO MATCH** — every approved-name search demonstrably ran correctly and produced no qualifying records. This is a successful lookup, not a failure.
- **AMBIGUOUS** — candidate records exist but identity cannot be established safely, including parent-only or conflicting-location cases.
- **FAILED/PARTIAL** — the website blocked the request, changed layout, ignored the submitted filter, pagination could not be completed, or one or more approved-name searches could not be proven complete.

This is critical: a legitimate empty search result must be classified as `SUCCESS_NO_MATCH` with `CompletenessStatus.COMPLETE`; it must never be treated as a source failure merely because there is no new information.

## Normalization rules

Use conservative normalization for candidate generation only: Unicode normalization, lowercase/casefold, punctuation and repeated-whitespace normalization, and optional comparison with common legal suffixes such as LLC, Inc., Corporation, Co., LLP, and LP removed.

Never let normalization silently collapse genuinely different businesses. If removing suffixes or punctuation creates multiple plausible candidates, classify the lookup as ambiguous and preserve all candidates for human review.

Do not perform fuzzy matching aggressively. If fuzzy matching is added later, use it only to generate review candidates, never to establish a clean positive automatically.

## Evidence fields to capture

For every matched or ambiguous Violation Tracker record, preserve as much of the following as the public page exposes:

- canonical Violation Tracker record URL
- company name as reported by the source
- current parent company
- parent-at-penalty-time when legitimately available
- penalty amount
- year and exact date when available
- offense group
- primary offense type
- secondary offense type when available
- violation description
- level of government
- action type
- agency
- court when present
- civil/criminal classification
- case ID and case name when present
- facility state/county/city/address/ZIP
- facility NAICS
- source-of-data URL
- archived-source URL when present
- direct OSHA/ECHO/PACER link when present
- duplicate-penalty marker when exposed by the site
- retrieval timestamp
- parser/adapter version
- identity-match method and confidence/explanation

The canonical Violation Tracker record URL should be the preferred source-record identifier. If a canonical record identifier cannot be obtained, use a deterministic fallback fingerprint from stable record properties rather than row position.

## Comparison behavior against the master database

The first implementation should compare Violation Tracker evidence to existing master fields but **must not automatically overwrite them**.

Recommended comparison examples:

- Workplace-safety record found while master `osha` is blank or negative -> discrepancy/research flag; verify through the dedicated OSHA source.
- Environmental offense found while `environmental_violations` is blank or negative -> discrepancy/research flag; verify through the applicable authoritative environmental source.
- Wage-and-hour offense found while `prevailing_wage_violations` is blank or negative -> related evidence, but do not equate all wage-and-hour cases with a prevailing-wage violation; send to review/authoritative verification.
- Federal private-litigation/court record found while `federal_court` is blank -> court-evidence lead; PACER remains the dedicated court source.
- A category that does not fit any dedicated master field -> candidate for `misc_violations`, but only after the firm confirms the intended business meaning of that field.

This preserves the project's core distinction between **evidence discovered** and **master truth approved by a human**.

## Proposed result model for the UI

A Violation Tracker source result should show a compact bidder-level summary such as:

`3 matched records | $128,500 penalties | latest: 2024 | workplace safety (2), environmental (1)`

Under that summary, show individual records and a comparison state for each relevant master category:

- `Already represented in master`
- `Potential new discrepancy`
- `Needs identity review`
- `Needs authoritative-source verification`

Do not reduce the source to a single Yes/No because that would discard most of the useful evidence and create confusion when several categories are involved.

## Duplicate handling

Violation Tracker explicitly documents duplicate and partially duplicate penalty records. In addition, the same underlying case may also be found independently by this project's OSHA, environmental, DOL, PACER, or other adapters.

Keep source records individually for provenance, but create a cross-source deduplication/reconciliation layer for display and summary purposes.

Preferred within-Violation-Tracker key: canonical individual-record URL.

Fallback fingerprint: normalized company + agency + case ID when present + date/year + primary offense type + penalty amount.

Never sum penalties blindly when the site marks a record as duplicative or when multiple source records appear to describe the same case.

## Completeness and failure classification

A clean negative is allowed only when the adapter can demonstrate that the intended contractor query completed successfully and all returned result pages relevant to that query were evaluated.

Return `partial`, `ambiguous`, `blocked`, or `failed` rather than `no_match` when any of the following occurs:

- the site ignores submitted filters
- the result set exceeds the adapter's safe traversal limit
- pagination cannot be completed
- a page structure/parser signature changes
- the source rate-limits or blocks requests
- a CAPTCHA/authentication wall appears
- multiple entities cannot be resolved confidently
- subscriber-only information would be required to resolve identity

This follows the existing V2 rule that partial or failed research cannot be converted into a false negative.

## Caching and refresh cadence

Violation Tracker currently publishes updates approximately monthly; its 2026 update log shows updates in March, April, May, June, July, and August. The site's visualization documentation also describes monthly updating.

Accordingly, there is no reason to hammer the source hourly. Cache successful contractor searches and source records. A conservative default would be to reuse cached results during the same research run and allow scheduled refresh no more frequently than the source's meaningful update cadence unless a user explicitly requests a fresh lookup.

The adapter should store the source's visible update date/version when available so future runs can avoid refetching unchanged data.

## Health check and parser hardening

The adapter health check should verify only lightweight public invariants, for example:

- homepage/search endpoint reachable
- expected search-form controls still present
- result table headers/signature still recognizable
- individual-record page still exposes expected label/value structure

Do not use a real contractor search as a high-frequency health check.

Parsing should use labeled fields and URLs instead of fragile CSS positions wherever possible. Unknown/new fields should be retained in raw evidence rather than discarded.

## Proposed source adapter structure

Implementation should add a dedicated adapter under `backend/app/research/sources/violation_tracker.py` using the existing `ResearchSource` contract.

Suggested internal separation:

- HTTP/session client with throttling, retry/backoff and cache hooks
- search-form/query builder
- search-results parser
- individual-record parser
- identity matcher
- duplicate/fingerprint helper
- evidence-to-comparison classifier

The adapter should return `SourceResult` evidence only. It should not update master bidder rows directly.

## Proposed tests before enabling the source

Use fixture HTML, not the live website, for normal automated tests. Cover:

- exact company with zero results
- exact company with one result
- company with several results
- alias/related-company match
- parent-only match classified as ambiguous/review
- same name but conflicting state/address
- missing facility address
- duplicate penalty marker
- multi-page results
- malformed/changed result page
- filter silently ignored and unfiltered results returned
- rate-limit/block response
- subscriber-only field absent
- individual record missing optional fields
- cross-source overlap with an OSHA/environmental/court result

A small manually invoked smoke test can validate the live public form, but it should not run automatically in CI.

## Implementation decision

**Recommended:** implement Violation Tracker as a low-volume, contractor-scoped evidence adapter with strong caching and fail-closed completeness rules. Keep `owned_fields` empty at first. Use its findings to reveal discrepancies and to point the user toward the dedicated authoritative source that should confirm a master-field change.

**Do not implement:** full-site crawling, blocker/CAPTCHA bypass, subscriber-download bypass, mass harvesting unrelated to the approved master database, or automatic overwrites of dedicated OSHA/environmental/wage/court fields.

## Open decision before field proposals are enabled

The only master field that may eventually make sense for direct Violation Tracker ownership is `misc_violations`, and even that should remain disabled until the firm defines exactly what `misc_violations` is intended to mean.

If `misc_violations` means “other verified regulatory/enforcement misconduct not represented by a dedicated field,” then a narrow offense-type mapping can later be designed. If it means something else, Violation Tracker may remain evidence-only permanently.

## Official sources reviewed

- Violation Tracker homepage: https://violationtracker.goodjobsfirst.org/
- User Guide: https://violationtracker.goodjobsfirst.org/pages/user-guide
- Quick Start: https://violationtracker.goodjobsfirst.org/pages/quick-start
- Data Sources: https://violationtracker.goodjobsfirst.org/pages/violation-tracker-data-sources
- Update Log: https://violationtracker.goodjobsfirst.org/pages/update-log
- Subscription Plans: https://violationtracker.goodjobsfirst.org/plans
- Good Jobs First Terms of Service: https://goodjobsfirst.org/terms-of-service/
