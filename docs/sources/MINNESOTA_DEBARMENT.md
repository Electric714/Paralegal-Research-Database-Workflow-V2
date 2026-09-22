# Minnesota Suspended/Debarred Vendors

## Official source

Minnesota Office of State Procurement:

https://mn.gov/admin/osp/government/suspended-debarred/

This replaces the retired `mmd.admin.state.mn.us/debarredreport.asp` URL.

The Office of State Procurement page is the authoritative public master list used by this adapter. It currently renders the complete vendor list in ordinary server-side HTML and publishes, where present, vendor name, address, owner/officer, suspension dates, debarment dates, reinstatement-eligible date, and cause.

## Acquisition

The adapter performs one ordinary HTTP GET during `prepare()` for the whole research run. It does not submit one network request per bidder and does not use browser automation.

The downloaded HTML is SHA-256 hashed and stored immutably under the application's source cache. Every bidder result from that prepared dataset carries the same artifact hash/path so the research evidence remains traceable to the exact page bytes used for the comparison.

A clean no-match is allowed only when the source page passes completeness validation. The parser requires the Minnesota `Results 1 - N of N` count, requires the page to expose the complete result window, requires every listed vendor name to have its detail block, and requires the number of parsed records to match the reported count. Missing counts, missing detail blocks, incomplete result windows, malformed dates, HTTP failures, and layout changes are explicit non-negative statuses.

## Identity matching

Research is limited to the approved bidder name and explicitly stored `related_companies` aliases.

Candidate generation is name-first:

1. Exact normalized bidder/alias names are retained even when the Minnesota address differs, so a moved business becomes identity review rather than a false no-match.
2. Fuzzy candidates require typo-level name similarity plus at least one shared distinctive name token.
3. Generic business words such as `construction`, `contracting`, `services`, and `electric` cannot by themselves create a fuzzy candidate.
4. Automatic confirmation requires an exact approved name/alias plus meaningful street-level location corroboration. City/state or ZIP alone is not enough.
5. Near-exact fuzzy matches remain manual review even when the address looks strong.
6. Records explicitly labeled `an individual` are never automatically treated as the bidder company.
7. Human `SAME_ENTITY` and `DIFFERENT_ENTITY` judgments use the normal V2 identity-review system and are remembered by source record ID.

## Field semantics

The intended owned master field remains:

`state_federal_debarment`

The adapter is deliberately positive-only. A confirmed source record proposes `Y` only when the Minnesota page supplies an explicit `Debarment Date` and that debarment is active on the research date. The following remain evidence-only and do not automatically propose `Y`:

- active suspensions without an explicit debarment date
- expired/historical suspensions or debarments
- records where the prose says `Debarred` but the structured date fields only supply suspension dates
- unresolved/ambiguous identity matches

A clean Minnesota no-match never proposes `N`; absence from this state list is not proof that the bidder has never been debarred elsewhere.

## Source-record retention

For confirmed matches, evidence retains the Minnesota record ID, full source name, address/location, owner/officer, all published action dates, cause text, computed current action status, source URL, retrieval artifact hash/path, identity score components, and matching basis. Multiple confirmed Minnesota records can be retained for one bidder while producing at most one master-field proposal.

## Completion gate

The implementation has offline parser/matching/failure-state tests. Keep the source under **Ongoing** until it is manually verified against representative real bidder records from the firm's approved database, including a likely match and a no-match, and the merged `main` build is exercised end to end.