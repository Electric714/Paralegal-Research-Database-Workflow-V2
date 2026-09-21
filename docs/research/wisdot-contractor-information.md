# WisDOT / HCCI Contractor Information Research

## Scope

This note documents the initial acquisition and field-mapping research for the Wisconsin Department of Transportation (WisDOT) Highway Construction Contract Information (HCCI) source. The implementation target is the existing Paralegal Research Database Workflow V2 research pipeline. Research must remain limited to contractors already present in the approved bidder database and their stored related-company aliases.

WisDOT = Wisconsin Department of Transportation.

HCCI = Highway Construction Contract Information.

The legacy project URL remains valid and points to WisDOT's current Contracting Information page:

https://wisconsindot.gov/Pages/doing-bus/contractors/hcci/cntrct-info.aspx

## Primary finding

This source should not be implemented as a per-contractor HTML scraper. WisDOT publishes several official reports at stable URLs. The preferred design is to download/cache each useful report once per refresh, preserve the raw artifact and hash, parse it into local structured records, build contractor-name/address indexes, and compare the approved bidder database against those local records.

## Useful official WisDOT datasets / reports

### 1. Debarred, Suspended and Ineligible Contractors

Stable source:

https://wisconsindot.gov/hccidocs/debar.pdf

The report includes contractor/person name, address, effective date, termination date, action, restricted area, acting agency, and cause code. The current report is explicitly prepared and issued by WisDOT.

This is the cleanest direct mapping in the bidder schema. A confirmed current match can support a positive `state_federal_debarment = Y` proposal. A WisDOT no-match must NOT propose `N`, because `state_federal_debarment` is a combined field that can also be supported by SAM.gov and other state/federal sources.

Suggested normalized fields:

- contractor_name
- address
- city/state/zip
- effective_date
- termination_date
- action (`Debarment`, `Suspended`, `Ineligible`)
- restricted_area
- acting_agency
- cause_code
- report_date
- source_url

### 2. All Contractors

Stable source:

https://wisconsindot.gov/hccidocs/contracting-info/allcont.pdf

The report includes WisDOT vendor ID, contractor name/address, phone/fax, bid-bond expiration, prequalification expiration, and rated capacities. It is a very large report and is therefore especially suitable for a single download/cache plus local index rather than repeated web requests.

This is valuable for identity resolution and supporting evidence. It can provide a WisDOT vendor identifier and additional/current address data. Presence in this list should not by itself change any existing master field until the firm's intended field semantics are confirmed.

### 3. Prequalified Contractors

Stable source:

https://wisconsindot.gov/hccidocs/prequal.pdf

The report includes vendor ID, contractor name/address, phone/fax, bid-bond expiration, prequalification expiration, and rated work capacities. WisDOT states that highway-construction bidders are prequalified to establish competency and responsibility to perform the work.

This should initially be evidence-only because the current bidder schema has no dedicated WisDOT-prequalification field.

### 4. Finals Status Statewide Report

Current stable report path observed during research:

https://wisconsindot.gov/hcciupload/finals-status-statewide-report.pdf

This report is highly relevant to the existing bidder field `public_works_projects_budget_time_quality_complaint`. It includes contractor names, contract/project IDs, project descriptions, actual completion/final-acceptance information, remarks, and coded project/finals issues.

Examples observed in the current report include:

- `CNQI` = Construction Quality Issues - Repairs to be Addressed
- `PLFC` = Project Engineer Delay / Finals Corrections Needed
- `WCLC` = Wage Claim / Investigate Labor Compliance
- `SFST` = Staff Shortages
- `DNRP` = DNR WPDES Permit Coverage Not Terminated

The report also includes free-text remarks that can distinguish contractor-caused issues from agency/staff delays. Because not every code represents contractor fault, this report must initially be evidence-only for `public_works_projects_budget_time_quality_complaint`. Do not infer `Y` from every non-empty code set. Automatic proposals should wait until the firm confirms which codes/remarks count under that field.

### 5. Contract Logs

WisDOT publishes contract logs and a FY2014-present archive. WisDOT describes the log as tracking contracts from award to execution and including the project description, awarded contractor, bonding information, dates documents were sent/returned, execution date, and other execution details. The archive is useful for project-history evidence but does not directly establish a complaint or violation.

## Comparison strategy against the bidder database

Use the same approved-master identity inputs already available in `ContractorContext`:

- `contractor_name`
- `related_companies`
- primary address/city/state/zip
- additional address/city/state/zip

Build normalized aliases with the existing shared matching utilities. Match local WisDOT records conservatively using name + address/state/ZIP evidence. Preserve WisDOT vendor IDs once a high-confidence match is found so later runs can use them as an additional identity anchor.

Do not treat name-only matches as confirmed when the name is common or when address evidence conflicts.

## Proposed source behavior

The first implementation should treat WisDOT as one source adapter backed by multiple cached sub-datasets.

Recommended refresh flow:

1. Download each configured official PDF once.
2. Record retrieval timestamp, response metadata, report date if present, SHA-256 hash, source URL, parser version, and local artifact path.
3. Extract text from the PDFs using a text-capable PDF parser (prefer a pure-Python dependency such as `pypdf` unless table extraction proves insufficient).
4. Validate expected report headers before accepting the artifact.
5. Parse structured records into separate in-memory/local indexes for debarment, all-contractors/prequalification, and finals-status data.
6. For each bidder, search the local indexes using approved master names/aliases and address evidence.
7. Return normal `SourceResult` statuses and evidence through the common research pipeline.

## Field ownership recommendation

Initial ownership should remain conservative:

- `state_federal_debarment`: WisDOT may eventually own positive `Y` proposals only when a current confirmed debarred/suspended/ineligible record matches. WisDOT no-match must remain evidence/no-match only and must never propose `N` because this field combines multiple sources.
- `public_works_projects_budget_time_quality_complaint`: evidence-only until the firm confirms which Finals Status codes/remarks count as a positive.
- All contractor/prequalification data: evidence-only unless a dedicated master field is later added.

The current `wisdot` field mapping should therefore stay empty during the research phase rather than prematurely granting automatic write authority.

## Completeness / failure rules

- A failed PDF download is `FAILED` or `BLOCKED`, never a clean negative.
- A PDF whose expected header/layout is not recognized is a parser/layout failure, never a clean negative.
- If only some configured WisDOT datasets refresh successfully, return `PARTIAL` rather than silently using the missing portion as negative evidence.
- A debarment no-match can be a clean negative only for the WisDOT debarment sub-check, not for the combined `state_federal_debarment` master field.
- Preserve all prior approved master values when the source fails or is incomplete.
- Never bypass authentication, CAPTCHA, or access controls.

## Test plan

Offline fixtures should cover at least:

- debarment PDF text with one confirmed contractor
- suspended/ineligible records
- debarment no-match
- same-name/address-conflict ambiguity
- all-contractors/prequalification record parsing
- finals-status contractor with `CNQI`
- finals-status record whose issue is clearly agency/staff-caused (for example `SFST`) so it is not automatically treated as contractor fault
- malformed/changed PDF layout
- partial refresh where one PDF fails
- regression proving WisDOT no-match cannot propose `state_federal_debarment = N`

## Recommended implementation order

1. Build shared WisDOT PDF download/cache/provenance support.
2. Implement and test the debarment parser/matcher first because its meaning is clear and directly useful.
3. Add All Contractors / Prequalified Contractors as identity and evidence enrichment.
4. Add Finals Status parsing as evidence-only.
5. After the firm's interpretation of `public_works_projects_budget_time_quality_complaint` is confirmed, add narrowly defined proposal rules for the approved set of issue codes/remarks.
6. Register the source as ready only after live representative positive/no-match testing and CI pass.
