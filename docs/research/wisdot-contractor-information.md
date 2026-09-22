# WisDOT / HCCI Contractor Information

WisDOT means Wisconsin Department of Transportation. HCCI means Highway Construction Contract Information.

## Acquisition strategy

WisDOT is implemented as an automatic official-file source, not a per-contractor site scraper. A research run refreshes four public PDFs once, stores immutable SHA-256-addressed source artifacts, reuses cached extracted text when the bytes are unchanged, and then compares selected bidders against local parsed records.

Official inputs:

- Debarred, Suspended and Ineligible Contractors: `https://wisconsindot.gov/hccidocs/debar.pdf`
- All Contractors: `https://wisconsindot.gov/hccidocs/contracting-info/allcont.pdf`
- Prequalified Contractors: `https://wisconsindot.gov/hccidocs/prequal.pdf`
- Finals Status Statewide Report: `https://wisconsindot.gov/hcciupload/finals-status-statewide-report.pdf`

The runtime cache is under `backend/data/source_cache/wisdot/`. Raw PDFs are never overwritten; each content hash gets its own file. `latest.json` is only a pointer to the most recently accepted artifact for a dataset. When a live refresh fails, the adapter may use the last validated cached artifact, but the research result is marked partial so stale data cannot become a clean negative or an automatic proposal.

## Comparison behavior

The adapter uses only contractors already present in the approved bidder database and their stored `related_companies` aliases. Matching uses company name plus primary address/city/state/ZIP evidence. Exact names with corroborating location evidence can be confirmed automatically; same-name records with conflicting or inadequate location evidence require human review.

The All Contractors and Prequalified Contractors reports provide WisDOT vendor IDs and identity/address evidence. They do not directly own a master field.

The Finals Status report is evidence-only. Codes such as `CNQI`, `PLFC`, `WCLC`, `SFST`, `DNRP`, and `OTHR` plus the associated remarks are retained for review. The adapter does not automatically set `public_works_projects_budget_time_quality_complaint`, because some codes and remarks reflect agency/staff/permit delays rather than contractor fault.

## Debarment field rule

A confirmed current match in the WisDOT Debarred/Suspended/Ineligible report may emit `state_federal_debarment = Y` for human approval.

A WisDOT no-match never emits `state_federal_debarment = N`. That field combines multiple sources, including SAM.gov and other state/federal exclusion evidence. WisDOT is therefore positive-only for that master field.

## Failure semantics

- Blocked, timed-out, malformed, or changed-layout downloads never become negative evidence.
- All four PDFs validate expected report markers before their contents are trusted.
- If any configured dataset cannot be refreshed and only a cached copy is available, the overall result is partial.
- If no usable dataset is available, the source returns an explicit unavailable/HTTP/timeout/blocked state.
- Raw source artifact paths, hashes, retrieval timestamps, URLs, and whether the artifact came from cache are preserved in evidence snapshots.

## Runtime flow

`prepare()` performs the source refresh once per research run, then builds the local parsed records. Every bidder task reuses those prepared records rather than issuing another WisDOT request.

The intended workflow is:

**automatic WisDOT refresh -> immutable cache -> PDF text extraction -> layout validation -> local indexes -> bidder/alias matching -> evidence -> comparison -> human approval**
