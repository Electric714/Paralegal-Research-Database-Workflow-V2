# Violation Tracker Integration Research

**Status:** Research/design reviewed; production adapter not yet implemented  
**Target source:** https://violationtracker.goodjobsfirst.org/  
**Research date:** 2026-09-21  
**Implementation decision:** free public-search workflow only; evidence-only source

## What Violation Tracker is

Violation Tracker is not an acronym. It is the name of the enforcement database produced by the Corporate Research Project of Good Jobs First.

The public database aggregates corporate enforcement and litigation records from many federal, state, and local sources. That breadth makes it useful as a cross-agency research index, but it also means it often overlaps with sources this project researches separately, such as OSHA, environmental agencies, labor-enforcement sources, and court systems.

For V2, Violation Tracker should therefore be treated as a **cross-agency evidence and discrepancy-detection source**, not as an authoritative owner of any master bidder field.

## Repository contract

The repository intentionally defines `violation_tracker` with an empty `owned_fields` set in `backend/app/research/field_mappings.py`.

That is now a deliberate rule, not a placeholder:

- Violation Tracker findings may be stored as evidence.
- Findings may be compared with existing master values and surfaced as discrepancies or follow-up leads.
- Violation Tracker must not directly create master-field proposals.
- Dedicated authoritative sources remain responsible for any field-level proposal.
- A future decision to grant field ownership must be explicit, separately reviewed, and accompanied by test changes.

The production source must remain `not_implemented` and absent from `SOURCE_ADAPTERS` until a real adapter and fixture coverage exist.

## Free public-search acquisition method

The selected implementation path is the site's free public search interface. This project will not use a paid subscription, paid export, subscriber-only download, or paid dataset path.

The public site supports company searches and individual record pages. Research found that the Advanced Search form submits to `/search.php` using POST. The implementation must submit the site's actual public form parameters rather than guessing unsupported GET parameters.

A previous research probe demonstrated why this matters: an unsupported guessed query returned a large unfiltered result set. The adapter must therefore prove that the requested company filter was actually applied before it can classify a search as complete.

No documented public API was identified in the official material reviewed for this source. The free public website is therefore the planned acquisition surface.

### Automation constraints

Good Jobs First's published terms prohibit unreasonable automated load and circumvention of technological blockers. The adapter must use conservative, contractor-scoped requests and must not:

- crawl the entire site
- bypass CAPTCHA or anti-automation controls
- bypass authentication or subscriber controls
- retrieve subscriber-only downloads
- continue hammering the source after rate limiting or blocking

If the site blocks automation or the public data is insufficient to resolve identity, the result must remain blocked, partial, or ambiguous rather than attempting a bypass.

## Search scope and aliases

For each contractor already present in the approved master database, search only:

1. the approved master `contractor_name`
2. approved aliases/DBAs already stored in `related_companies`

Do not discover unrelated companies and do not auto-add companies to the master database.

Each approved name should be searched separately and then aggregated into one bidder-level source result. Alias parsing should follow the conservative conventions already used by the existing source adapters and must not split commas blindly inside legal company names.

## Required result states

The user-facing behavior maps cleanly onto the existing `SourceResult` model.

### MATCH

Use `SUCCESS_WITH_FINDINGS` only when one or more public records can be tied confidently to the contractor or an approved alias.

### NO MATCH

Use `SUCCESS_NO_MATCH` with `CompletenessStatus.COMPLETE` only when **every approved-name search demonstrably ran correctly** and produced no qualifying records.

An empty search result is a successful lookup. It is not a failure merely because there is no new information.

### AMBIGUOUS

Use `AMBIGUOUS_MATCH` when candidate records exist but identity cannot be established safely, including parent-only matches, generic names, conflicting locations, or multiple plausible entities.

### FAILED / PARTIAL

Use the appropriate blocked, HTTP, parser, pagination, timeout, or partial state when any approved-name search cannot be proven complete.

A partial or failed lookup must never collapse into a clean negative.

## Fail-closed completeness rules

A clean negative is allowed only when the adapter can prove all of the following:

- the intended public company filter was actually applied
- the response is a recognized search-results page
- all relevant returned pages were evaluated
- pagination, if present, completed successfully
- no approved alias search failed
- the site did not return a blocker, challenge, or authentication wall
- the parser recognized the expected result structure

Return a non-clean state when:

- the site silently ignores the submitted filter
- the result set exceeds the adapter's safe traversal limit
- pagination cannot be completed
- the layout changes
- the source rate-limits or blocks requests
- a CAPTCHA or authentication wall appears
- identity cannot be resolved from the free public information

## Contractor identity matching

Violation Tracker's current-parent information is useful supporting evidence but must not be treated as proof that the exact bidder is the penalized entity.

Recommended matching hierarchy:

1. exact normalized penalized-company name plus matching facility/address information when present
2. exact normalized master contractor name matching the penalized company
3. exact approved `related_companies` alias matching the penalized company
4. corroborating facility city/state/ZIP or address
5. current-parent relationship as supporting evidence only
6. ambiguous when only a parent matches, the name is generic, locations conflict, or multiple plausible entities remain

The adapter must preserve which approved name produced the match: master legal name or approved alias.

A parent-only match must never become an automatic exact-bidder finding.

## Normalization

Use conservative normalization for matching:

- Unicode/case normalization
- punctuation normalization
- repeated-whitespace normalization
- comparison with common legal suffixes removed when useful

Do not use aggressive fuzzy matching to confirm identity. Fuzzy similarity may be used only to surface review candidates.

If normalization creates multiple plausible entities, return ambiguity rather than selecting one automatically.

## Evidence to retain

For matched or ambiguous public records, preserve as much of the following as the public page exposes:

- canonical Violation Tracker record URL
- company name reported by the source
- current parent company
- penalty amount
- year and exact date when available
- offense group
- primary and secondary offense type when available
- violation description
- level of government
- action type
- agency
- court when present
- civil/criminal classification
- case ID and case name when present
- facility state/county/city/address/ZIP
- facility NAICS when present
- source-of-data URL
- archived-source URL when public
- public OSHA/ECHO/PACER-related links when present
- duplicate-penalty marker when exposed
- parser/adapter version
- match method and identity explanation

Prefer the canonical individual-record URL as `source_record_id`. If a stable record identifier cannot be obtained, use a deterministic fingerprint based on stable record properties rather than row position.

## Comparison against the master database

Violation Tracker should identify possible discrepancies without directly proposing master changes.

Examples:

- workplace-safety evidence while the master OSHA fields are blank or negative -> surface as a discrepancy/follow-up lead; confirm through the dedicated OSHA source
- environmental enforcement while `environmental_violations` is blank or negative -> surface as a discrepancy; confirm through the applicable environmental source
- wage-and-hour evidence while `prevailing_wage_violations` is blank or negative -> surface as related evidence only; not every wage-and-hour case is a prevailing-wage violation
- court/litigation evidence while a court field is blank -> surface as a court-research lead; the dedicated court source remains authoritative
- another enforcement category -> retain as evidence; do not automatically map it into `misc_violations`

### Persistence behavior

The existing research persistence layer already supports this safely:

- `EvidenceRecord` rows can be stored for relevant master field names or source-specific evidence labels.
- `normalized_payload` can carry discrepancy categories, summary counts, and follow-up flags.
- because `violation_tracker` owns no master fields, `persist_source_result()` will not create proposed master changes from those evidence records.

That behavior is intentional and is protected by a dedicated regression test.

## Duplicate handling

Violation Tracker documents duplicate and partially duplicate penalty records, and the same underlying matter may also appear in other project sources.

Keep individual Violation Tracker records for provenance, but do not blindly sum penalties.

Preferred within-source key: canonical individual-record URL.

Fallback fingerprint: normalized company + agency + case ID when present + date/year + primary offense type + penalty amount.

If records appear duplicative or overlapping, preserve them individually and flag them for reconciliation rather than silently merging or double-counting them.

## Caching and request discipline

The source does not need high-frequency polling. Reuse results during the same research run and use a conservative refresh cadence.

The production adapter should include:

- a bounded request count per contractor
- reasonable timeout values
- retry/backoff only for transient failures
- an identifiable user agent
- caching within a run
- a hard stop on blocking/rate limiting

## Health check

The health check should be lightweight and should not repeatedly search a real contractor.

Useful invariants include:

- public search page reachable
- expected search-form controls present
- result-table signature still recognizable
- individual record pages still expose expected label/value structure

A health check failure must not be converted into a bidder-level no-match result.

## Production adapter structure

The eventual adapter should live at:

`backend/app/research/sources/violation_tracker.py`

Suggested internal separation:

- HTTP/session client and request throttling
- public search-form builder
- search-response validation
- result-row parser
- individual-record parser
- approved-name/alias search aggregation
- conservative identity matcher
- duplicate/fingerprint helper
- discrepancy classifier

The adapter must return `SourceResult` objects and must never update master bidder rows directly.

## Tests required before enabling the source

Normal CI tests should use fixture HTML, not the live website.

Minimum fixture coverage:

- exact company with zero results
- exact company with one result
- company with several results
- alias/DBA match
- all approved names cleanly return zero results
- one alias succeeds while another search fails -> partial, never no-match
- parent-only match -> ambiguous
- same name with conflicting location
- missing facility address
- duplicate penalty marker
- multi-page results
- incomplete pagination
- malformed/changed result page
- filter silently ignored / unfiltered results returned
- rate-limit or block response
- individual record missing optional fields
- overlapping OSHA/environmental/court evidence

A manually invoked live smoke test may be used during development, but it should not run in CI.

## Current implementation gate

Violation Tracker must remain `not_implemented` until all of the following are true:

1. `violation_tracker.py` exists and follows the `ResearchSource` contract.
2. Search-filter validation and fail-closed completeness behavior are implemented.
3. Approved aliases are searched and aggregated correctly.
4. Fixture tests cover positive, no-match, ambiguous, and failure/partial cases.
5. The adapter is registered in `SOURCE_ADAPTERS`.
6. A representative manual live test succeeds for both a likely match and a clean no-match.
7. Only then may the source metadata change to `ready`.

## Official sources reviewed

- Violation Tracker homepage: https://violationtracker.goodjobsfirst.org/
- User Guide: https://violationtracker.goodjobsfirst.org/pages/user-guide
- Quick Start: https://violationtracker.goodjobsfirst.org/pages/quick-start
- Data Sources: https://violationtracker.goodjobsfirst.org/pages/violation-tracker-data-sources
- Update Log: https://violationtracker.goodjobsfirst.org/pages/update-log
- Good Jobs First Terms of Service: https://goodjobsfirst.org/terms-of-service/
