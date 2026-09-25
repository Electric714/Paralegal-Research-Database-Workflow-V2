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

## Verification workflow

The production adapter intentionally mirrors the manual lookup workflow used during live checking of the source.

1. Download and validate the complete official Minnesota master list once for the research run.
2. Normalize each approved bidder name and explicitly stored `related_companies` alias by ignoring capitalization, punctuation, and ordinary legal suffixes such as `LLC` or `Inc.`.
3. Compare those approved names directly against every vendor name on the complete Minnesota list.
4. If no approved name appears on the validated complete list, return `SUCCESS_NO_MATCH` with `CompletenessStatus.COMPLETE` and `verification_status = VERIFIED_NOT_LISTED`. The run summary already treats this as a green clean result.
5. If an exact normalized approved name appears on the list, retain the Minnesota record as a finding even when the address differs. Address data remains evidence; it is not required to prove that the exact listed name exists.
6. Similar-but-different names do not block a clean verification. For example, `#1 TRANSPORTATION LLC` and `A1 TRANSPORTATION LLC` are different normalized names and therefore do not match.
7. A source record explicitly labeled `an individual` still requires identity review before it can be treated as the bidder company.
8. A remembered human `DIFFERENT_ENTITY` judgment remains excluded from the bidder's matches.

This source therefore does not use fuzzy similarity as a reason to withhold a clean result when the bidder's actual approved names are absent from the complete list.

## Field semantics

The intended owned master field remains:

`state_federal_debarment`

The adapter is deliberately positive-only. A confirmed listed record proposes `Y` only when the Minnesota page supplies an explicit `Debarment Date` and that debarment is active on the research date. The following remain evidence-only and do not automatically propose `Y`:

- active suspensions without an explicit debarment date
- expired/historical suspensions or debarments
- records where the prose says `Debarred` but the structured date fields only supply suspension dates
- unresolved individual-person identity matches

A verified Minnesota no-match does not write `N` into the shared state/federal debarment master field; it verifies only that none of the bidder's approved names appear on this complete Minnesota source list.

## Source-record retention

For listed matches, evidence retains the Minnesota record ID, full source name, address/location, owner/officer, all published action dates, cause text, computed current action status, source URL, retrieval artifact hash/path, matched approved name, and exact-name matching basis. Multiple listed Minnesota records can be retained for one bidder while producing at most one master-field proposal.

## Regression coverage

Regression tests specifically cover the live workflow semantics:

- a similar but different listed company name remains a verified clean no-match
- an exact normalized listed name is retained even when the address is different
- an incomplete Minnesota result window can never become a verified clean result
