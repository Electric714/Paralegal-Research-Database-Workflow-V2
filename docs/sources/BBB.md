# Better Business Bureau source

## Purpose

The BBB adapter researches only contractors already present in the approved bidder database. It owns one master field:

- `better_business_bureau_complaints`

BBB findings remain research evidence until a paralegal approves a proposed change.

## Acquisition model

The adapter does not crawl BBB broadly and does not automate BBB's interactive `/search` route. Discovery starts from BBB's published business-profile sitemap index:

`https://www.bbb.org/sitemap-business-profiles-index.xml`

The current implementation uses mapped geographic sitemap ranges for FL, IL, MN, MO, OH, and WI. For a selected bidder it:

1. Uses `contractor_name` plus stored `related_companies` aliases from the approved master record.
2. Reads only the configured BBB sitemap blocks for the bidder's state.
3. Ranks plausible profile URLs by business-name similarity, using the city embedded in the URL only as a discovery tie-breaker.
4. Opens only a small number of plausible profiles.
5. Parses identity from the returned live BBB page. The URL slug is never accepted as identity evidence.
6. Compares the live business name, address, city, state, and ZIP against the approved bidder record using the common V2 matching layer.
7. Requires a confirmed identity before reading the profile's direct `/complaints` page.
8. Parses complaint totals only from BBB's `Customer Complaints Summary` section. Complaint narratives are never used to infer the summary count.
9. Stores aggregate complaint counts and SHA-256 hashes of the fetched profile/complaint HTML as provenance. Consumer complaint narratives and full page HTML are not retained.

A previously confirmed BBB profile URL is reused on later runs as a routing shortcut. The live profile is re-fetched and re-scored every time, so stale routing cannot silently remain trusted.

## Stable BBB identity

BBB profile URLs include a name-bearing slug followed by a numeric BBB file identifier, for example:

`example-builders-llc-0694-1000000000`

The adapter now stores `0694-1000000000` as the source record ID instead of the full slug. This keeps identity judgments stable if BBB changes the business-name portion of a URL. Judgments created by the earlier implementation using the full slug remain readable for backward compatibility.

## Field semantics

`better_business_bureau_complaints = Y` is proposed only when a confirmed live BBB profile reports one or more complaints in BBB's rolling three-year summary.

`better_business_bureau_complaints = N` is proposed only when a confirmed profile is reached successfully, the official complaint-summary block parses successfully, and the configured mapped discovery completed without gaps. A direct cached-profile check that reports zero remains partial until mapped discovery is performed.

A missing BBB profile is not treated as `N`. No candidate, unsupported-state coverage, blocked access, parser drift, ambiguous identity, incomplete sitemap coverage, HTTP failures, or timeouts are reported as a clean no-match. They remain unknown/partial and cannot propose a negative change.

## Identity handling

The adapter uses the shared V2 `score_candidate` logic. Strong name plus location corroboration can auto-confirm a profile. Plausible but weaker matches are emitted as `AMBIGUOUS_MATCH` with `REVIEW_REQUIRED` identity status so the normal V2 identity-review workflow can record `SAME_ENTITY` or `DIFFERENT_ENTITY` judgments.

Remembered judgments are honored on later runs. If the business profile is confirmed but the `/complaints` request fails, the result preserves `CONFIRMED` identity while classifying complaint completeness as unknown; it does not erase the identity work already completed.

## Access failures

BBB HTTP 401/403 responses, rate limiting, explicit challenge pages, timeouts, upstream failures, malformed sitemap XML, off-domain redirects, or unrecognized complaint-summary layouts are classified explicitly. They never become a clean negative.

The adapter does not attempt CAPTCHA solving or access-control bypasses.

## Tests

`backend/tests/test_bbb.py` and `backend/tests/test_bbb_hardening.py` cover profile identity parsing, stable record IDs, heading fallback, official-summary-only complaint parsing, positive and zero complaint evidence, partial no-match behavior, ambiguous identity, blocked access, preservation of confirmed identity on complaint-endpoint failure, cached-profile revalidation, legacy identity judgments, `bbb.org`/`www.bbb.org` sitemap hosts, city-based discovery tie-breaking, and explicit empty state mappings.

Real-site operator testing is still required before moving BBB from **Ongoing** to **Completed** on the project checklist.
