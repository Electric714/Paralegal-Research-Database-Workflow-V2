# SAM.gov Federal Exclusions Source

## Purpose

This connector checks contractors already present in the approved bidder database against the official SAM.gov Public Exclusions V2 extract.

It is a targeted federal-exclusion check. It is not a generic SAM entity-registration search and it does not discover new bidders.

## Acquisition

V2 uses the official downloadable SAM Public Exclusions V2 ZIP/CSV as an **upload-and-compare** source. The application does not use a SAM API key and does not scrape the public SAM search UI.

The user downloads the current official exclusions extract from SAM.gov and uploads it through the Sources/Research workflow. The backend parses the file once, caches it locally, and compares all selected bidders against that local dataset.

Cached extracts live under `backend/data/source_cache/sam/` and are excluded from Git. Uploaded source bytes are stored immutably in content-addressed subdirectories so a later upload with the same official filename cannot overwrite evidence referenced by an earlier research snapshot.

When multiple cached extracts exist, the loader selects the valid file with the newest official extract date rather than simply choosing whichever file happened to be uploaded last. Legacy flat cache files remain readable.

## Dataset semantics

The Public Exclusions V2 extract contains active SAM exclusions. The connector currently evaluates `Classification = Firm` records because this application researches contractor/business bidders.

The parser recognizes the documented V2 fields needed for provenance and identity matching, including company name, address, state, ZIP, UEI, excluding agency, exclusion type, active date, termination date, cross-reference, SAM Number, CAGE, NPI, and creation date.

`Additional Comments` is optional and is not required for parsing or matching.

## Bidder field ownership

SAM owns only this bidder field in V2:

`state_federal_debarment`

A confirmed active federal exclusion can propose:

`state_federal_debarment = Y`

The adapter deliberately does **not** propose `N` when a contractor is absent from the active SAM extract. Absence from the current federal exclusion file does not prove that the contractor is generally clean, licensed, legitimate, or free of other state/federal restrictions.

## Completeness and freshness

A current, successfully parsed official extract can produce a complete SAM source check.

An extract more than two days old, or an extract whose date cannot be established, is treated as partial. A stale or undated extract may still preserve positive evidence, but it cannot produce a clean negative result or an automatic field-change proposal.

If no uploaded extract is available, the source returns `SOURCE_UNAVAILABLE`, never `SUCCESS_NO_MATCH`.

Malformed extracts produce `DATASET_MALFORMED`.

## Identity matching

The registered runtime adapter is `SamUploadedExclusionsSource`.

The matcher normalizes contractor names and approved related-company aliases, then compares SAM candidates using company name plus available address, city, state, and ZIP evidence.

Exact normalized company-name/alias matches are always kept visible for review even when the SAM address is different. This prevents a moved company or stale address from being silently converted into `SUCCESS_NO_MATCH`.

Fuzzy company-name candidates must be typo-level similar. Generic shared words such as `Electric`, `Roofing`, `Services`, or `Builders` are not enough to create a review candidate.

Automatic identity confirmation requires an exact approved name/alias plus meaningful address corroboration. A shared city/state or shared ZIP by itself is not sufficient. ZIP-supported confirmation also requires the same street number and a reasonably similar normalized address.

Near-exact fuzzy names with strong address evidence remain human-review items rather than automatic exclusion findings.

A paralegal can mark a candidate as:

- `SAME_ENTITY`
- `DIFFERENT_ENTITY`

That judgment is persisted by bidder + source + SAM record ID and reused on later runs.

## Evidence and audit behavior

Every SAM source interaction is persisted through the common research pipeline:

`research task → source check → evidence snapshot → identity decision → proposed change → human approval → master revision`

The cached source file is referenced in the evidence snapshot with its SHA-256 hash, extract date, record count, adapter version, and parser version.

Evidence snapshots are immutable. A later SAM upload cannot overwrite the source bytes referenced by an earlier snapshot. Resolving an ambiguous identity creates a new source evaluation and snapshot while preserving the original history.

## Testing

SAM has fixture-based tests covering:

- CSV and ZIP parsing
- firm-only filtering
- confirmed matches
- clean no-match behavior
- stale or missing extracts
- ambiguous identities
- the A-1 Duran Roofing and ABEL Electric false-positive regressions
- exact-name records with changed/conflicting addresses
- same-name/same-ZIP records on different streets
- immutable same-filename cache uploads
- newest-extract-date cache selection
- legacy flat-cache compatibility
- remembered identity judgments
- evidence/proposal creation
- explicit master approval
- idempotent run execution
- immutable identity-review history

The tests do not require the live SAM.gov service.
