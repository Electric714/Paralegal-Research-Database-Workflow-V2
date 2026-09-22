# Wisconsin Circuit Court Access (WCCA / CCAP)

## Current implementation decision

WCCA is implemented as an **operator-assisted public research source**, not as an unattended scraper.

The application prepares a bounded search plan from the approved bidder record, opens the official Wisconsin Circuit Court Access site for the operator, records structured results, and stores the result in the existing evidence/audit pipeline. It does not solve CAPTCHA, bypass anti-automation controls, reverse-engineer hidden endpoints, or treat a blocked/incomplete search as a negative result.

A future fully automated implementation should use only an official CCAP-supported automated route such as the WCCA REST subscription service after the firm obtains the applicable agreement, credentials, and technical documentation.

## Terminology

**CCAP** is Consolidated Court Automation Programs, the Wisconsin court system technology program that supports the statewide circuit-court case-management environment.

**WCCA** is Wisconsin Circuit Court Access, the public-facing service for public circuit-court case information.

Public WCCA: `https://wcca.wicourts.gov/`

CCAP overview: `https://www.wicourts.gov/courts/offices/ccap.htm`

## Post-merge audit: 2026-09-21

The first merged operator-assisted implementation was reviewed against the supplied bidder database, the earlier V1 project, the V2 research/evidence architecture, and Wisconsin Court System documentation.

The audit found one important semantic problem in the first implementation: a complete public WCCA search with no match was being stored as observed `circuit_court=N`. Automatic writes were already disabled, but the comparison itself was still too strong.

That behavior is now prohibited.

### Why public WCCA no-match cannot mean `circuit_court=N`

Wisconsin Courts' paid WCCA REST agreement expressly states that WCCA information:

- includes only records open to public view;
- excludes confidential, sealed, and redacted information;
- does not comprise the complete court record;
- is only a snapshot of accessible CCAP information;
- may not include older records that predate county CCAP implementation unless they were backloaded; and
- may fail to return all cases when searching by a field/code that was not historically required.

Official agreement: `https://www.wicourts.gov/courts/resources/docs/RESTagreementpaid.pdf`

The WCCA Oversight Committee also adopted online display periods for some categories that differ from the underlying record-retention period. A case can therefore cease to display on WCCA while the underlying court record remains subject to a different retention rule.

Official report: `https://www.wicourts.gov/courts/committees/docs/wccafinalreport2017.pdf`

Official action plan: `https://www.wicourts.gov/courts/committees/docs/wccaactionplan2017.pdf`

Accordingly, this project uses **positive-only master-field comparison semantics** for public WCCA:

- A confirmed WCCA case may support observed `circuit_court=Y` evidence.
- A complete public no-match is stored only as `wcca_public_search = NO_CURRENTLY_DISPLAYED_MATCH` evidence.
- A public no-match never creates `circuit_court=N` evidence.
- A public no-match never contradicts or erases an existing `circuit_court=Y` value.
- Blocked, ambiguous, partial, and incomplete searches create no negative master-field conclusion.

`SourceResultStatus.SUCCESS_NO_MATCH` therefore means the **planned public WCCA search completed with no currently displayed match**. It is a clean negative for that bounded source check, not a statement that no circuit-court record exists.

## Supplied database contract

The supplied example database contains 24 bidder rows and 30 columns.

For the WCCA-related columns:

- `circuit_court`: 9 `Y`, 15 `N`
- `ccap_show150`: 23 blank, 1 `N`

Both columns are validated by the V2 importer as legacy Y/N/blank fields. Their exact business rules are not defined by the column names alone.

### `circuit_court`

The existing Y/N population strongly suggests a legacy boolean decision, but the firm has not yet documented the exact threshold for Y or N. Until that business rule is confirmed, WCCA has no master-field ownership and cannot create a proposed change.

The current implementation may nevertheless show **comparison-only positive evidence** when a confirmed WCCA case exists, because a confirmed displayed case directly establishes that WCCA currently contains a matching circuit-court case for the contractor.

### `ccap_show150`

The supplied database does not contain enough populated values to infer this field safely. The current V2 repository does not define the term. The earlier V1 project recognized aliases such as `ccap show150` / `ccap show 150` but likewise did not define an authoritative rule or implement an authoritative source for the field.

No official Wisconsin Court System material reviewed for this implementation defines a WCCA field or concept named `ccap_show150`.

Therefore:

**Do not infer, calculate, compare, or write `ccap_show150` until the firm explicitly defines what the field means and how it is decided.**

## Information we actually need from WCCA

The contractor database does not need a copy of the full court record. The WCCA evidence layer should retain only what is necessary to establish the contractor match, explain the source finding, and support later review.

For a confirmed positive case, the required identifiers are:

1. **Case number** — required to create confirmed positive case evidence.
2. **Matched party/business name** — required to show which WCCA party was tied to the approved bidder.

Useful supporting fields, when displayed and relevant, are:

- county;
- case type;
- case status;
- filing date;
- disposition;
- stable case/source URL when available; and
- concise operator note explaining identity corroboration or limitations.

The workbench also retains the exact bidder name/aliases searched, bidder master ID, approved addresses used for corroboration, completion status, identity status, retrieval time, acquisition method, and current approved `circuit_court` / `ccap_show150` values.

Do not copy unrelated personal information, full filings, transcripts, sensitive identifiers, or unnecessary party details into the contractor database. Wisconsin Courts states that WCCA provides case information such as parties, dates, filings, and orders but does not provide the full case documents; official documents remain with the circuit court.

Redaction/public-information FAQ: `https://www.wicourts.gov/services/attorney/redact/faq.htm`

## Identity rules

Court-party names require conservative attribution. A case caption or similar name alone is not enough when identity is uncertain.

A confirmed positive currently requires:

- at least one WCCA case record;
- a case number for each recorded confirmed case;
- the matched party/business name for each recorded confirmed case; and
- explicit operator confirmation that the case belongs to the approved contractor.

Addresses and locations from the approved bidder record are displayed to the operator as corroborating context. If identity is uncertain, the result remains `AMBIGUOUS_MATCH` / `REVIEW_REQUIRED` and no `circuit_court=Y` comparison evidence is created.

Remembered WCCA-specific SAME_ENTITY / DIFFERENT_ENTITY reuse is a possible future enhancement. The current operator-assisted WCCA flow records operator identity confirmation with the evidence but does not yet automatically reuse the common identity-judgment table for WCCA case parties.

## Search completeness rules

The search plan is created from `contractor_name` plus explicitly stored `related_companies` aliases. Semicolon, pipe, and newline are treated as explicit alias separators; commas remain part of legal names.

A public WCCA no-match is classified `SUCCESS_NO_MATCH` only when every planned search name was confirmed searched and the operator explicitly confirms completion. If an alias was skipped, the session was interrupted, WCCA blocked access, or the operator cannot determine identity, the result remains partial/blocked/ambiguous.

A confirmed positive case may still be retained when the overall alias search is incomplete. In that situation the result is `PARTIAL_RESULTS`, because the positive case is useful evidence but the full search cannot be represented as complete.

## Evidence records

Confirmed positive research stores two layers of evidence:

- `circuit_court = Y` — comparison-only positive master-field observation, never an automatic proposal under the current mapping;
- `wcca_case = <case number>` — one immutable evidence record per entered case, including the matched party and supporting case metadata.

A complete public no-match stores:

- `wcca_public_search = NO_CURRENTLY_DISPLAYED_MATCH`

It stores **no** `circuit_court=N` evidence.

WCCA's source field mapping remains intentionally empty until the firm's legacy semantics are documented.

## Public-site automation constraints

Wisconsin Courts implemented CAPTCHA and fraud-detection controls to reduce automated extraction/screen scraping from WCCA. This project must not work around those controls.

Official CAPTCHA announcement: `https://www.wicourts.gov/news/archives/view.jsp?id=715&year=2015`

Consequences:

- no CAPTCHA solving or bypass;
- no generic unattended browser bot against public WCCA;
- no undocumented endpoint reverse engineering as the production method;
- no conversion of access challenges into no-match results; and
- no claim that a temporarily working scraper is a reliable integration.

## Official REST option

The published paid WCCA REST subscription agreement reviewed for this design is revision 08/2022. It describes a REST download interface, data-protection/update duties, and substantial limitations on the data. The revision reviewed lists a $12,500 annual non-state subscription fee; current price and terms must be confirmed directly with CCAP before relying on that amount.

Agreement: `https://www.wicourts.gov/courts/resources/docs/RESTagreementpaid.pdf`

The transport layer should be replaced with an official REST adapter only after the firm obtains supported access and technical documentation. The current evidence, identity, comparison, and review contracts can remain largely unchanged.

## Tests / acceptance gates

The current offline coverage verifies:

- legal-name commas are preserved and aliases are deduplicated;
- public WCCA is operator-assisted rather than scraped;
- skipped aliases cannot produce a complete no-match;
- complete public no-match creates source-level evidence but never `circuit_court=N`;
- confirmed positive cases require case number and matched party/business name;
- confirmed cases retain case-level provenance;
- confirmed positives can be retained while a broader search remains partial;
- WCCA cannot create a master proposal while its field ownership is disabled; and
- the packaged application entry point exposes the WCCA API and returns positive-only/no-negative comparison semantics.

WCCA remains **Ongoing**, not Completed, until:

1. The firm defines `circuit_court` operationally.
2. The firm defines `ccap_show150` operationally.
3. The paralegals' actual WCCA search procedure is confirmed, including any name variants, date/case-type filters, and statewide/county practices.
4. At least one known-positive bidder and one expected public no-match bidder are manually tested through the workbench.
5. The operator confirms the workbench captures enough information without adding unnecessary court data.
6. Any future field ownership is implemented only after those definitions are written and fixture-tested.

## Primary official references

- WCCA public site: `https://wcca.wicourts.gov/`
- CCAP overview: `https://www.wicourts.gov/courts/offices/ccap.htm`
- WCCA CAPTCHA / screen-scraping announcement: `https://www.wicourts.gov/news/archives/view.jsp?id=715&year=2015`
- WCCA Oversight Committee final report: `https://www.wicourts.gov/courts/committees/docs/wccafinalreport2017.pdf`
- WCCA Oversight action plan: `https://www.wicourts.gov/courts/committees/docs/wccaactionplan2017.pdf`
- WCCA paid REST agreement: `https://www.wicourts.gov/courts/resources/docs/RESTagreementpaid.pdf`
- Wisconsin Courts redaction/public-information FAQ: `https://www.wicourts.gov/services/attorney/redact/faq.htm`
