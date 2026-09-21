# SAM.gov Federal Exclusions Source

## Purpose

This connector checks contractors already present in the approved bidder database against the official SAM.gov Public Exclusions V2 extract.

It is a targeted federal-exclusion check. It is not a generic SAM entity-registration search and it does not discover new bidders.

## Acquisition

Preferred acquisition is the official SAM.gov Public Entity Management API public-extract endpoint:

`https://api.sam.gov/data-services/v1/extracts`

The adapter requests the latest `EXCLUSION` public extract and caches the returned ZIP locally so one dataset can be searched for every bidder in the research run.

SAM's public-extract API requires an API key. The application therefore supports two acquisition paths:

1. `SAM_API_KEY` is configured: the adapter can download/refresh the current official extract automatically.
2. No API key is configured: the user can upload an official SAM Public Exclusions V2 ZIP or CSV from the Sources page.

Cached extracts live under `backend/data/source_cache/sam/` and are excluded from Git.

## Dataset semantics

The Public Exclusions V2 extract contains active SAM exclusions. The connector currently evaluates `Classification = Firm` records because this application researches contractor/business bidders.

The parser recognizes the documented V2 fields needed for provenance and identity matching, including company name, address, state, ZIP, UEI, excluding agency, exclusion type, active date, termination date, cross-reference, SAM Number, CAGE, NPI, and creation date.

`Additional Comments` is treated as optional and is not required for parsing or matching.

## Bidder field ownership

SAM owns only this bidder field in V2:

`state_federal_debarment`

A confirmed active federal exclusion can propose:

`state_federal_debarment = Y`

The adapter deliberately does **not** propose `N` when a contractor is absent from the active SAM extract. The bidder field represents broader state/federal debarment information, and absence from the current active federal exclusion file is not sufficient evidence to erase an approved value or historical finding.

## Completeness and freshness

A current, successfully parsed official extract can produce a complete SAM source check.

An extract more than two days old, or an extract whose date cannot be established, is treated as partial. A stale or undated extract may still preserve positive evidence, but it cannot produce a clean negative result or an automatic field-change proposal.

Missing credentials with no cached extract produces `AUTH_REQUIRED`, not `SUCCESS_NO_MATCH`.

Malformed extracts produce `DATASET_MALFORMED`.

Network failures, timeouts, and HTTP errors remain explicit source-result states and can never become a negative finding.

## Identity matching

The adapter normalizes contractor names and known related companies, then compares SAM candidate records using company name plus available address, city, state, and ZIP evidence.

Strong name similarity alone is not enough for automatic confirmation. Automatic confirmation requires a very strong name match plus corroborating location evidence.

Possible matches that are not safe to auto-confirm are stored as immutable evidence with `REVIEW_REQUIRED` identity status and shown in the Review page.

A paralegal can mark a candidate as:

- `SAME_ENTITY`
- `DIFFERENT_ENTITY`

That judgment is persisted by bidder + source + SAM record ID and reused on later runs. Confirming one candidate does not automatically reject other plausible SAM records; each materially distinct candidate remains reviewable unless it has its own stored judgment.

## Evidence and audit behavior

Every SAM source interaction is persisted through the common research pipeline:

`research task → source check → evidence snapshot → identity decision → proposed change → human approval → master revision`

The cached source file is referenced in the evidence snapshot with its SHA-256 hash, extract date, record count, adapter version, and parser version.

Evidence snapshots are immutable. Resolving an ambiguous identity creates a new source evaluation and a new snapshot; the original ambiguous snapshot remains in history.

## Testing

SAM has fixture-based tests using the documented Public Exclusions V2 column layout. Tests cover:

- CSV parsing
- ZIP parsing
- firm-only filtering
- confirmed matches
- clean no-match behavior
- ambiguous identities
- stale extracts
- missing API key/cache
- remembered identity judgments
- evidence/proposal creation
- explicit master approval
- idempotent run execution
- immutable identity-review history

The tests do not require the live SAM.gov service.
