# Responsible Minnesota source

## Purpose

The `responsible_mn` adapter checks the public Responsible Minnesota **Ineligible Contractors** table for bidders already present in the approved master database and for explicitly approved related-company aliases.

Source URL: `https://responsiblemn.org/ineligible-contractors/`

Responsible Minnesota is a public secondary source that summarizes contractors it considers non-responsible under Minnesota Statute § 16C.285 and links to supporting public records. It is not treated as an authoritative or exhaustive government dataset.

## Acquisition

The adapter performs one HTTPS GET per research run during `prepare()`, validates the expected four-column table, parses the full table into memory, and reuses that snapshot for all selected bidders. It does not perform one network request per contractor and does not require browser automation or an API key.

Expected columns:

- Contractor
- Statutory Provision Rendering Non-Responsible
- Relevant Public Documents/Links
- End Date

The fetched page is hashed with SHA-256 and the hash, retrieval time, source URL, and parsed record count are retained as provenance. If the expected table disappears, dates become malformed, the response is blocked, or the request fails, the adapter fails closed rather than generating negative results.

## Matching

The adapter searches only the bidder's approved `contractor_name` and explicitly stored `related_companies` aliases.

Corporate suffix and punctuation normalization is allowed. An exact normalized match is identity-confirmed. A high-similarity non-exact match is returned as `AMBIGUOUS_MATCH` for human review and cannot modify master data.

Rows ending in `, individually` are normalized so the individual's name can be compared. The known `Company ... and Person, individually` form is split only because the explicit `individually` suffix makes the structure unambiguous; ordinary company names containing `and` or `&` are not generically split.

## End dates

A listing with an end date on or after the current date is treated as current evidence. A listing with no end date remains current source evidence with an unknown end date. A listing whose end date has passed is retained as historical evidence and does not assert current ineligibility.

## Database comparison semantics

`mndol_ineligibility` is the strongest candidate legacy field for this source, so findings are stored as evidence against that field name. **Responsible Minnesota does not currently own that master field.** Automatic field proposals remain disabled until the firm confirms the exact legacy meaning of `mndol_ineligibility`.

A current exact match may therefore store `mndol_ineligibility = Y` as comparison evidence, but it will not create a master-field change proposal. An expired match stores historical evidence without asserting `Y`.

A no-match is deliberately returned with `CompletenessStatus.PARTIAL`. Responsible Minnesota disclaims completeness and states that related entities can be ineligible without being individually listed. Consequently:

- no-match never means the contractor is eligible
- no-match never proposes `N`
- a failed, blocked, malformed, or changed-layout request never becomes a negative result
- the adapter never discovers or automatically adds new related companies to the approved master database

## Source result behavior

- exact normalized current match: `SUCCESS_WITH_FINDINGS`, identity `CONFIRMED`, evidence retained
- exact expired match: `SUCCESS_WITH_FINDINGS`, identity `CONFIRMED`, historical evidence only
- similar non-exact match: `AMBIGUOUS_MATCH`, identity `REVIEW_REQUIRED`
- no exact or review-level match: `SUCCESS_NO_MATCH` with completeness `PARTIAL`
- changed HTML structure: `LAYOUT_CHANGED`
- malformed recognized table: `DATASET_MALFORMED`
- timeout, blocking, HTTP, or network errors: explicit failure state with no negative inference

## Tests

`backend/tests/test_responsible_mn.py` covers table parsing, supporting links, short and four-digit end dates, company-plus-individual rows, exact normalized matching, approved aliases, expired listings, partial no-match semantics, fuzzy review, HTTP failure, malformed dates, and the intentional absence of master-field ownership.
