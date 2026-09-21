# U.S. Department of Labor Enforcement Data source

## Purpose

The `dol_enforcement` source should research Department of Labor enforcement records for bidders already present in the approved master database. It is not a general employer-discovery tool, and it must never write directly to the master database.

The firm's legacy source URL points to `https://enforcedata.dol.gov/views/data_catalogs.php`. DOL announced in February 2026 that the old enforcement-data page was being decommissioned and replaced by the modern Open Data Portal.

Current public portal: `https://data.dol.gov/`

Current API base: `https://apiprod.dol.gov/v4`

Keyless dataset catalog: `https://apiprod.dol.gov/v4/datasets`

Recommended first dataset: Wage and Hour Division (WHD) Compliance Action / Enforcement data.

## Research conclusion

Do **not** implement this source by scraping the retired enforcement-data web UI.

The best acquisition path for this project is the official DOL v4 API because:

1. The API is the replacement path DOL now directs users to.
2. It supports machine-readable JSON/CSV/XML responses.
3. It supports field selection, sorting, pagination, and conditional filtering.
4. It lets us request only records relevant to approved bidder names instead of repeatedly downloading or scraping a large national dataset.
5. The dataset catalog can be queried without an API key so the application can verify that expected datasets still exist before a run.
6. The application can preserve an explicit `AUTH_REQUIRED` state when a DOL API key has not been configured instead of returning a false negative.

DOL's current API guide documents a maximum response of 10,000 records or 5 MB per request, whichever comes first, plus `offset`, `fields`, `sort`, `sort_by`, and JSON `filter_object` parameters. The API guide also states that an API key is required for dataset metadata/data requests and warns against exposing that key publicly.

## Why WHD should be Phase 1

The old DOL Enforcement Database covered multiple agencies, including WHD, EBSA, MSHA, OFCCP, and OSHA. This project already has a dedicated OSHA adapter, so OSHA records should not be duplicated through `dol_enforcement`.

WHD is the strongest first fit because its public compliance-action data contains concluded employer investigations and identifies the employer, employer location, violation findings, back wages, employees affected, monetary penalties, findings dates, and law-specific enforcement counts.

The public WHD dataset contains concluded compliance actions since FY 2005. DOL describes it as including whether violations were found, back wages, employees due back wages, and civil money penalties.

WHD also separately publishes government-contract enforcement statistics for laws including the Davis-Bacon and Related Acts (DBRA), Service Contract Act (SCA), and Contract Work Hours and Safety Standards Act (CWHSSA). Those are particularly relevant to this bidder/contractor database.

## Relationship to bidder database fields

The current bidder schema includes:

- `contractor_name`
- `related_companies`
- primary and additional address/city/state/ZIP fields
- `prevailing_wage_violations`
- `dwd`
- `dwd_substance_abuse_plan`
- `mndol_ineligibility`
- `misc_violations`
- `osha`

The current source-to-field map intentionally gives `dol_enforcement` **no owned fields yet**. That is correct and should remain in place until the firm's field semantics are confirmed.

### Strongest candidate mapping

`prevailing_wage_violations` is the clearest potential master-field target.

For a construction-focused bidder database, a confirmed WHD case with a DBRA violation is the most direct federal prevailing-wage signal. WHD also tracks other government-contract statutes such as SCA and CWHSSA, but they should not automatically be treated as equivalent to the firm's `prevailing_wage_violations` field until the firm confirms its intended definition.

Recommended conservative rule during implementation:

- Collect all WHD law-specific findings as evidence.
- Treat confirmed DBRA violations as a **candidate** for `prevailing_wage_violations = Y`.
- Keep SCA, CWHSSA, FLSA, child-labor, FMLA, H-1B/H-2A/H-2B, MSPA, and other findings as evidence until explicit field ownership is approved.
- Do not automatically write to `misc_violations` merely because a DOL case exists.
- Do not map federal DOL results into `dwd` or `dwd_substance_abuse_plan`; those names refer to a different workflow/source and must not be conflated with federal DOL enforcement.
- Do not map DOL OSHA records through this source because OSHA already has its own adapter.
- Do not map federal DOL records into `mndol_ineligibility`.

## Recommended acquisition design

### API configuration

Use a configuration value such as:

`DOL_API_KEY`

The key must never be committed to Git, written into audit logs, persisted in evidence, included in user-visible error messages, or stored in a raw request URL retained by the application.

The official API guide shows the key as the `X-API-KEY` request parameter. Because that can place the credential in a URL, the adapter must aggressively redact it from logging and provenance. Evidence should retain only the safe API endpoint/dataset identity and the non-secret search parameters.

If no key is configured:

- return `AUTH_REQUIRED`
- set completeness to `UNKNOWN` or `NOT_APPLICABLE`
- explain that DOL API credentials are missing
- do not manufacture a clean negative

### Dataset discovery and schema validation

During `prepare()` or health check:

1. Query the keyless `/v4/datasets` catalog.
2. Confirm that the expected WHD enforcement dataset is still published.
3. Record catalog metadata/freshness when available.
4. With an API key, query the dataset metadata endpoint before relying on the parser.
5. Verify the required identity and enforcement fields exist.
6. If required fields have disappeared or changed type/name, return a visible dataset/schema failure rather than silently dropping information.

Do not hard-code assumptions from the retired site without validating the live v4 metadata.

### Contractor search strategy

The approved master database controls scope.

For each bidder:

1. Build approved search names from `contractor_name` plus explicitly stored `related_companies`.
2. Preserve the raw legal names for server-side searching; also build controlled variants for punctuation and legal-suffix differences.
3. Query employer-name fields in WHD (legal/trade name) and constrain by bidder state when practical.
4. Prefer one API request containing an `OR` across approved names/trade-name conditions plus an `AND` state condition, rather than blindly issuing many broad calls.
5. If the strict pass returns nothing, allow one bounded relaxed-name pass only for meaningful multi-token names; never search generic words such as `CONSTRUCTION`, `ELECTRIC`, `SERVICES`, or `BUILDERS` by themselves.
6. Fully paginate all returned candidates before classifying the search as complete.
7. Perform final entity matching locally using the existing contractor-name/address matching utilities.

### Identity confirmation

Useful WHD identity fields include employer legal/trade name and employer street/city/state/ZIP information. The adapter should compare those against both primary and additional master addresses.

Recommended rules:

- exact approved name/alias + meaningful ZIP/address corroboration: eligible for automatic identity confirmation
- exact name + matching city/state but incomplete address: strong candidate, depending on uniqueness
- fuzzy/near-exact name: human review unless additional evidence is exceptionally strong
- generic-name overlap only: reject or require review
- conflicting state/ZIP: never auto-confirm
- multiple plausible entities: `AMBIGUOUS_MATCH`

Existing remembered SAME_ENTITY / DIFFERENT_ENTITY judgments should be reused if the DOL record has a stable record/case identity suitable for that mechanism.

## Evidence model

For every confirmed or review-worthy WHD case, preserve as much of the following as the live schema provides:

- DOL agency: WHD
- dataset identifier/version or catalog metadata
- source case ID
- legal employer name
- trade name
- street address
- city/state/ZIP
- NAICS code/description
- findings start/end dates
- total violation count
- employees affected/in violation
- total back wages agreed to pay
- civil money penalty information
- law-specific violation counts
- law-specific back wages/penalties where available
- repeat/willful indicators where available
- contractor/alias searched
- identity match score/reasons
- retrieval timestamp
- safe dataset/API URL with credentials stripped
- adapter/parser versions
- pagination/completeness state

Do not collapse several enforcement cases into a single undocumented `Y`. The review UI should be able to show the individual cases that support any proposed master-field change.

## Comparison behavior

### Confirmed positive

If a WHD case is confidently matched to a bidder and contains relevant violations:

- store the complete case evidence
- aggregate a bidder-level summary only as a convenience; never discard the case-level records
- if field semantics have been approved, create a proposed change separately from the evidence
- never update the master automatically

For `prevailing_wage_violations`, the safest initial proposal rule is a confirmed DBRA violation count greater than zero **after** the firm confirms that this field is intended to represent federal DBRA findings.

### No matching record

A completed search with zero plausible records may be classified `SUCCESS_NO_MATCH`, but it should not automatically propose `prevailing_wage_violations = N`.

Reasons:

- an employer may appear under an unrecorded legal/trade name
- the public dataset includes concluded actions, not every open investigation
- absence of a public enforcement record is not equivalent to proof that no violation ever occurred

### Partial/failure cases

The adapter must never convert these into a negative:

- missing API key
- 401/403 authentication failure
- 429/rate limit
- timeout/network failure
- dataset unavailable
- schema/metadata mismatch
- incomplete pagination
- malformed response
- ambiguous company identity
- stale/unknown dataset freshness where completeness cannot be established

Use the project's existing explicit result states such as `AUTH_REQUIRED`, `HTTP_ERROR`, `TIMEOUT`, `PARTIAL_RESULTS`, `PAGINATION_INCOMPLETE`, `DATASET_MALFORMED`, or `AMBIGUOUS_MATCH`.

## API versus bulk download

### Preferred: targeted v4 API

Use the API first for this desktop workflow because the application researches only approved bidders and their aliases. It minimizes transfer, allows targeted filtering, is easier to re-run, and gives cleaner failure/pagination semantics.

### Optional future fallback: official bulk dataset

The portal may expose complete downloadable datasets. A bulk-cache mode can be useful later if API-key management or rate limits become operationally painful, but it should not be the first implementation because the entire national dataset is substantially larger than the tiny subset needed for bidder comparison.

If a bulk mode is added later, it should follow the SAM pattern: download/load once, hash the artifact, index approved bidder names locally, record dataset freshness, and search the local cache. Do not redownload a full national file once per bidder.

## Other DOL agencies

The old Enforcement Database included WHD, EBSA, MSHA, OFCCP, and OSHA.

Recommended phased approach:

### Phase 1 — WHD

Implement and validate WHD employer compliance actions first. This is the closest fit to contractor labor/prevailing-wage research and the current bidder schema.

### Phase 2 — OFCCP evidence

OFCCP publishes federal-contractor compliance evaluations and complaint-investigation data. Add it as evidence-only unless the firm identifies a specific master field it owns.

### Phase 3 — EBSA / MSHA if the firm actually uses them

Do not add complexity merely because the old DOL portal exposed these agencies. Confirm that paralegals use those records for bidder review before implementing them.

### OSHA

Do not duplicate OSHA through this adapter. The project already has a dedicated OSHA source with its own source-specific matching and evidence behavior.

## Proposed implementation files

When research is accepted, implementation should likely add:

- `backend/app/research/sources/dol_enforcement.py`
- `backend/tests/test_dol_enforcement.py`
- `backend/tests/fixtures/dol_whd_*.json`
- `dol_enforcement` registration in `backend/app/research/source_registry.py`
- source catalog URL correction in `backend/app/sources.py`
- UI/API-key diagnostic support if not already generic

Keep `backend/app/research/field_mappings.py` evidence-only until the `prevailing_wage_violations` semantics are explicitly confirmed.

## Test plan

At minimum cover:

1. exact legal-name + ZIP match with a confirmed violation
2. trade-name / related-company alias match
3. same-name company in a different state
4. generic/common-word false positive
5. multiple plausible DOL employers -> manual review
6. multiple WHD cases for one confirmed bidder
7. confirmed DBRA violation evidence
8. FLSA-only case that must **not** automatically become a prevailing-wage proposal
9. zero-result complete query
10. missing API key -> `AUTH_REQUIRED`
11. 401/403 auth failure
12. 429/retry exhaustion
13. timeout
14. malformed/schema-changed response
15. pagination across multiple result pages
16. incomplete pagination -> never clean negative
17. API key is absent from stored URLs/logging/evidence
18. existing master value is never overwritten without Review approval

## Open decisions before automatic field proposals

The implementation can collect WHD evidence without answering these questions, but automatic proposals should wait for the firm to confirm:

1. Does `prevailing_wage_violations` mean federal DBRA only?
2. Should SCA also count as a prevailing-wage violation for this database?
3. Should CWHSSA or Public Contracts Act findings be included in that field or remain `misc_violations`/evidence only?
4. Is the field intended to represent historical existence of any violation, or only a current/recent period?
5. Does a prior resolved case remain `Y` permanently?

Until those meanings are confirmed, DOL enforcement should collect and display evidence without owning a master field.

## Sources researched

Authoritative/current:

- DOL Open Data Portal: `https://data.dol.gov/`
- DOL v4 dataset catalog: `https://apiprod.dol.gov/v4/datasets`
- DOL API User Guide: `https://www.dataportal.dol.gov/pdf/dol-api-user-guide.pdf`
- DOL February 18, 2026 Open Data Portal announcement: `https://www.dol.gov/newsroom/releases/oasam/oasam20260218`
- WHD compliance-action dataset metadata on Data.gov: `https://catalog.data.gov/dataset/wage-and-hour-division-compliance-action-data`
- WHD Additional Resources / Enforcement Database description: `https://www.dol.gov/agencies/whd/data/charts/additional-resources`
- WHD government-contract enforcement statistics: `https://www.dol.gov/agencies/whd/data/charts/government-contracts`
- OFCCP compliance-evaluation and complaint-investigation data: `https://www.dol.gov/agencies/ofccp/foia/library/Compliance-Evaluations-and-Complaint-Investigations`

Historical schema cross-check only:

- Archived official DOL Developer WHD Compliance documentation in `USDepartmentofLabor/Developer`. Live v4 metadata must be treated as source of truth during implementation.
