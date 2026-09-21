# Wisconsin Circuit Court Access (WCCA / CCAP) source research

## Decision

Do **not** implement WCCA as an unattended scraper against the public Wisconsin Circuit Court Access website.

Wisconsin Court System documentation shows that the public WCCA site intentionally uses CAPTCHA and fraud-detection controls to prevent automated extraction/screen scraping. The Court System directs organizations that need automated or bulk access toward its subscription WCCA REST service.

For this project, the recommended implementation is therefore two-stage:

1. **Phase 1 — operator-assisted public WCCA research.** The application prepares each contractor search, opens WCCA for the paralegal, records a structured human-confirmed outcome, retains evidence/provenance, and compares that result to the approved master database. The application does not solve or bypass CAPTCHA and does not simulate an unattended user session.
2. **Phase 2 — official WCCA REST integration if the firm decides the subscription cost is justified.** The REST feed should be normalized/cached locally and matched against only the approved contractors and related-company aliases already in the master database.

This approach preserves the project's core rule: outside research remains evidence until a human approves a proposed master-data change.

## What WCCA / CCAP means

**CCAP** is the Wisconsin court system's Consolidated Court Automation Programs organization/system. It operates the case-management technology used by Wisconsin circuit courts.

**WCCA** is Wisconsin Circuit Court Access, the public-facing service that exposes public circuit-court case information maintained through CCAP.

Primary public entry point:

`https://wcca.wicourts.gov/`

Wisconsin Court System CCAP overview:

`https://www.wicourts.gov/courts/offices/ccap.htm`

## Official access constraints

The Wisconsin Court System announced CAPTCHA controls for WCCA specifically to prevent automated attempts to extract information and screen-scrape the site. It also states that organizations seeking bulk data should use CCAP's subscription automated service.

The Court System later added fraud-detection controls intended to identify scraping/denial-of-service behavior. A suspected automated session can therefore be challenged or blocked even if an HTML workflow happens to work during development.

Consequences for this project:

- No CAPTCHA solving or bypassing.
- No hidden/undocumented endpoint reverse engineering as the production acquisition method.
- No claim that a blocked public search means the contractor has no cases.
- No generic browser-bot fallback.
- Public WCCA should remain a human-driven source unless/until the firm receives an official automated-access route.

Official CAPTCHA announcement:

`https://www.wicourts.gov/news/archives/view.jsp?id=715&year=2015`

Official paid REST agreement:

`https://www.wicourts.gov/courts/resources/docs/RESTagreementpaid.pdf`

The currently published paid agreement reviewed for this design is revision 08/2022 and lists a **$12,500 annual subscription fee** for a non-state subscriber. That price should be confirmed directly with CCAP before any budget decision because the agreement revision itself predates this implementation.

Technical/subscription contact shown in the agreement:

`WCCAREST@wicourts.gov`

## Phase 1 — operator-assisted WCCA workflow

The application should automate the repetitive parts surrounding WCCA while leaving the actual public-site search to the human operator.

For each selected bidder:

1. Build a bounded search plan from `contractor_name` plus explicitly stored `related_companies` aliases.
2. Show the bidder's primary and additional addresses next to the search plan so the operator has identity-corroboration information available.
3. Open the official WCCA public search page in the user's normal browser.
4. The operator performs the required statewide business/party-name search for each planned legal name/alias. Any CAPTCHA remains a normal human WCCA interaction.
5. The operator records the outcome in the application as one of: matched case(s), complete no-match, ambiguous identity, incomplete/partial search, blocked/unavailable, or not checked.
6. For a positive result, capture only the case information necessary for contractor research: searched name, matched party/business name, case number, county, case type/status when relevant, source/case URL when stable, and concise operator notes explaining the identity match.
7. Store the result as immutable research evidence with retrieval time, acquisition method, searched aliases, completeness status, identity status, and operator-confirmed case identifiers.
8. Run the normal comparison/review pipeline. Nothing from WCCA directly edits the approved master record.

Recommended acquisition label:

`operator_assisted_public_wcca`

This is still a meaningful automation gain: the application controls which contractors need research, prepares names/aliases and identity context, records exactly what was checked, compares findings against the prior approved values, and avoids staff maintaining a parallel manual spreadsheet. The one thing it intentionally does not automate is the website interaction that Wisconsin Courts has designed to resist automation.

## Phase 1 evidence model

A positive WCCA evidence record should retain, where available:

- master bidder ID
- primary contractor name
- exact alias/name searched
- matched WCCA party/business name
- case number
- county
- case type and status relevant to the firm's workflow
- source URL / case URL when stable
- search/retrieval timestamp
- identity corroboration used by the operator
- completeness confirmation for all planned aliases
- acquisition method (`operator_assisted_public_wcca`)
- optional operator note

Avoid copying unrelated personal information into the contractor database. The purpose is to establish contractor-related circuit-court evidence, not to reproduce entire court files.

## Phase 1 result semantics

The source must use the existing research status model rather than collapsing every run into Y/N.

A clean no-match is allowed only when every required bidder name/approved alias in the search plan was searched through the intended statewide WCCA scope and the operator explicitly confirms the search completed. If one alias was skipped, WCCA blocked the session, a CAPTCHA/session problem prevented completion, results were truncated, or identity could not be resolved, the result remains partial/blocked/ambiguous rather than becoming `N`.

Examples:

- Complete search + confirmed contractor case: `SUCCESS_WITH_FINDINGS`, identity `CONFIRMED`, completeness `COMPLETE`.
- Complete search across every planned alias + no candidates: `SUCCESS_NO_MATCH`, completeness `COMPLETE`.
- Same/similar business name with inadequate corroboration: `AMBIGUOUS_MATCH` or `MANUAL_REVIEW_REQUIRED`.
- Site/session challenge prevents completion: `BLOCKED` or `PARTIAL_RESULTS`.
- Search started but aliases/pages remain unchecked: `PARTIAL_RESULTS`.

## Identity matching

Court-party names are especially sensitive to false positives because legal names can be common and a case caption alone may not identify the same contractor.

The operator should begin with exact/canonical contractor and related-company names. When WCCA provides enough business-party information for corroboration, compare it with the master bidder's Wisconsin/location/address context. If the result cannot be tied to the bidder confidently, save it as an ambiguous candidate for review rather than attributing the case automatically.

Remembered `SAME_ENTITY` / `DIFFERENT_ENTITY` judgments can later be reused for stable WCCA case/party identifiers through the existing identity-review system.

## Master-field ownership

The repository currently reserves these fields for WCCA:

- `circuit_court`
- `ccap_show150`

The field names alone are **not enough to define their business semantics**.

### `circuit_court`

The likely legacy meaning is whether a matched circuit-court record exists, because the example database uses Y/N values. However, that interpretation must be confirmed with the firm's existing workflow before the adapter is allowed to propose `Y` or `N` automatically.

Until confirmed, WCCA findings should be collected as evidence and comparisons should remain review-only.

### `ccap_show150`

The current repository, README, field mapping, and supplied example database do not define what `ccap_show150` means. The sample data also does not provide enough examples to infer it safely.

**Do not guess this field. Do not write to it until the firm defines the legacy rule.**

Once its meaning is confirmed, document the rule here and add explicit fixtures for positive, negative, ambiguous, and legacy-value cases before enabling proposals.

## Phase 2 — official WCCA REST adapter

If the firm subscribes to official automated access, replace only the acquisition layer. Keep the same identity/evidence/comparison/review contract used by Phase 1.

Recommended design:

1. Authenticate through the official subscriber mechanism supplied by CCAP.
2. Retrieve only data permitted by the subscription and published technical specification.
3. Persist a raw snapshot or request artifact/hash and retrieval timestamp when practical.
4. Normalize business-party/case records into an internal WCCA record model.
5. Build a local searchable index/cache so repeated contractor comparisons do not repeatedly hit WCCA.
6. Query the local normalized cache only for approved master contractors and approved aliases.
7. Apply conservative identity matching.
8. Produce the same `SourceResult` evidence and review proposals as the operator-assisted implementation.
9. Respect the agreement's security, availability, downstream-update, and redistribution restrictions.

The exact REST endpoints and response schema should not be invented from the public website. Implement the HTTP client only after the firm obtains the subscriber technical documentation/credentials or CCAP supplies a supported test route.

Suggested class boundary after subscription:

`WccaRestSource(ResearchSource)`

The normalized parsing/matching layer should be separate from the transport so fixtures can be tested without making live court requests.

## Comparison behavior

The approved master remains the baseline. WCCA evidence should be compared to the current approved values only after acquisition and identity classification.

A new confirmed case can create a review proposal only for a field whose semantics the firm has confirmed. An existing `Y` should not be erased because a later public search was blocked or incomplete. A clean no-match should never erase prior court evidence unless the firm's legacy workflow explicitly defines that behavior and the operator understands why a prior case would no longer appear.

Case-level evidence should remain historical even after a master-field decision is approved, so the audit trail can explain what caused the field to change.

## Tests required before source completion

Phase 1 should have offline tests for:

- primary-name positive case
- related-company/alias positive case
- complete no-match
- skipped alias -> partial, never negative
- ambiguous same/similar business name
- operator-blocked/incomplete search
- retained prior positive when refresh is partial/blocked
- evidence provenance serialization
- field-ownership enforcement
- unresolved `ccap_show150` producing no proposal

If Phase 2 is implemented, add sanitized REST fixtures covering schema validation, pagination/completeness, authentication/session failures, duplicate case/party records, parser/layout/schema changes, and cache/snapshot behavior.

## Acceptance gates / unresolved items

Before `wcca` is marked ready:

1. Confirm with the firm what `circuit_court` means operationally.
2. Confirm exactly what `ccap_show150` means.
3. Manually document the current public WCCA search steps used by the paralegals, including whether they search business name only, individual names, counties, case types, date ranges, or all statewide results.
4. Implement the operator-assisted evidence form and source result handling.
5. Test with at least one known positive contractor and one expected no-match contractor.
6. Confirm that a complete human search can be distinguished from a skipped/blocked/partial search.
7. If full automation is required, obtain the official REST subscription/technical specification before implementing network automation.

## Official references

- Wisconsin Court System — CCAP: `https://www.wicourts.gov/courts/offices/ccap.htm`
- Wisconsin Court System — WCCA public site: `https://wcca.wicourts.gov/`
- Wisconsin Court System — CAPTCHA/screen-scraping announcement: `https://www.wicourts.gov/news/archives/view.jsp?id=715&year=2015`
- Wisconsin Court System — paid WCCA REST agreement: `https://www.wicourts.gov/courts/resources/docs/RESTagreementpaid.pdf`
- Wisconsin Court System — WCCA oversight/policy report: `https://www.wicourts.gov/courts/committees/docs/wccafinalreport2017.pdf`
