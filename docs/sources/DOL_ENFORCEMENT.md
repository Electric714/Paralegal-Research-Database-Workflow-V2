# U.S. Department of Labor Enforcement Data

## Decision

Use the official U.S. Department of Labor Open Data Portal v4 API. Do not scrape the retired `enforcedata.dol.gov` interface.

- Portal: `https://data.dol.gov/`
- API base: `https://apiprod.dol.gov/v4`
- Keyless dataset catalog: `https://apiprod.dol.gov/v4/datasets`
- Local credential name: `DOL_API_KEY`

DOL announced in February 2026 that the new Open Data Portal replaces the old enforcement-data page. DOL describes API registration as free. An API key is required for metadata/data calls and must not be committed to a public repository or exposed in logs.

## Phase 1 scope

Phase 1 targets Wage and Hour Division (WHD) enforcement/compliance actions because this is the strongest match to the firm's contractor/bidder research workflow. The adapter discovers the current WHD compliance/enforcement dataset from the keyless v4 dataset catalog instead of hard-coding a legacy v1 endpoint.

The project already has a dedicated OSHA source, so `dol_enforcement` does not use DOL as a second OSHA collector.

## API behavior followed from DOL documentation

The adapter follows the v4 API structure documented by DOL:

`https://apiprod.dol.gov/v4/get/<agency>/<endpoint>/<format>`

Metadata uses:

`https://apiprod.dol.gov/v4/get/<agency>/<endpoint>/json/metadata`

DOL documents these relevant request parameters:

- `X-API-KEY` for authenticated metadata/data requests
- `limit` and `offset` for pagination
- `fields` for optional field selection
- `sort` / `sort_by`
- `filter_object` for conditional filtering
- supported filter operators include `eq`, `neq`, `gt`, `lt`, `in`, `not_in`, and `like`
- filters may combine AND/OR conditions

The current implementation uses JSON responses, `filter_object`, `limit`, and `offset`. It does not persist the authenticated request URL because DOL documents the API key as a request parameter.

## Credential handling

Set `DOL_API_KEY` in the local process environment. The key must never be:

- committed to Git
- stored in evidence snapshots
- written into audit logs
- included in warnings or exception text
- retained in source URLs

If `DOL_API_KEY` is missing, the source returns `AUTH_REQUIRED`; it never converts missing credentials into a no-match result.

## Dataset discovery and schema validation

Before bidder queries, the adapter:

1. Walks the keyless `/v4/datasets` catalog.
2. Selects the strongest WHD compliance/enforcement dataset candidate.
3. Uses the returned agency abbreviation and `api_url` to build v4 metadata/data endpoints.
4. Retrieves metadata with the configured API key.
5. Requires a recognized employer-name field before research can continue.
6. Resolves known identity-field variants for employer name, address, city, state, ZIP, and case identifier.
7. Fails visibly if the dataset/catalog structure no longer matches expectations.

This keeps the integration resilient to DOL changing the current dataset endpoint name while still refusing to silently guess when the schema changes materially.

## Bidder search strategy

The approved bidder database controls scope. The adapter searches only:

- `contractor_name`
- explicitly stored `related_companies`

It builds a bounded set of raw and normalized company-name variants. It refuses to issue very short generic one-word searches. For each variant it builds a DOL `filter_object` using the available employer-name fields and, when supported by the dataset and master record, the bidder state.

All returned records are deduplicated by the DOL case identifier when available. Pagination continues with `limit` and `offset` until the API-reported total is satisfied or the safety limit is reached.

## Identity matching

The adapter uses the project's shared company matching functions and master address data. Auto-confirmation is intentionally conservative.

Strong automatic confirmation generally requires an exact or very high employer-name match plus meaningful location corroboration such as matching ZIP, address, or city+state. Plausible records without sufficient location support return `AMBIGUOUS_MATCH` and require review.

A source match is never inferred merely because the API returned a similar company name.

## Comparison to the bidder database

The existing bidder schema includes `prevailing_wage_violations`, `dwd`, `dwd_substance_abuse_plan`, `mndol_ineligibility`, and `misc_violations` among other fields.

`dol_enforcement` intentionally owns **no master fields yet** in `field_mappings.py`.

The strongest candidate mapping identified during research is:

- confirmed Davis-Bacon and Related Acts (DBRA) violations -> candidate evidence for `prevailing_wage_violations = Y`

The adapter currently creates that evidence record when a confirmed bidder match has a positive DBRA violation count, but the common proposal service will not create a master change because `dol_enforcement` has no field ownership. This is deliberate until the firm confirms the exact meaning of `prevailing_wage_violations`.

Other WHD findings remain in the normalized evidence payload and are not automatically mapped to `misc_violations`, `dwd`, `dwd_substance_abuse_plan`, or `mndol_ineligibility`.

## Result semantics

- Missing/invalid credentials -> `AUTH_REQUIRED`
- Rate limit -> `BLOCKED`
- DOL outage -> `SOURCE_UNAVAILABLE`
- Unexpected catalog/schema -> `LAYOUT_CHANGED` or `DATASET_MALFORMED`
- Fully completed search with no plausible employer -> `SUCCESS_NO_MATCH`
- Plausible but unconfirmed employer -> `AMBIGUOUS_MATCH`
- Confirmed employer with violation findings -> `SUCCESS_WITH_FINDINGS`
- Confirmed employer with compliance records but no positive violation count -> `SUCCESS_COMPLETE`
- Safety-limit/pagination truncation -> `PARTIAL_RESULTS`

A failed, blocked, ambiguous, unauthorized, or partial result is never treated as a clean negative.

## Merge gate

The adapter is registered on `feature/dol-enforcement-api`, but the source-picker entry intentionally remains `not_implemented` until live operator verification is completed with a real DOL API key. Before merging/marking ready:

1. Run the complete backend test suite.
2. Verify catalog discovery against the live v4 API.
3. Verify metadata resolution against the live WHD dataset.
4. Test at least one known positive bidder and one expected no-match bidder.
5. Confirm the API key never appears in persisted evidence, logs, warnings, or UI-visible URLs.
6. Confirm a complete no-match does not manufacture a bidder-field `N`.
7. Confirm DBRA evidence does not create a proposed master change while field ownership remains disabled.
8. Only then change the picker status to `ready` and merge the PR into `main`.
