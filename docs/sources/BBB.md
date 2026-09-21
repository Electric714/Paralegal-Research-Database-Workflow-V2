# Better Business Bureau source

## Purpose

The BBB adapter researches only contractors already present in the approved bidder database. It owns one master field:

- `better_business_bureau_complaints`

BBB findings remain research evidence until a paralegal approves a proposed change.

## Acquisition model

The adapter does not crawl BBB broadly and does not automate BBB's interactive `/search` route. Discovery starts from BBB's published business-profile sitemap index:

`https://www.bbb.org/sitemap-business-profiles-index.xml`

The current proof of concept uses previously validated geographic sitemap ranges for FL, IL, MN, MO, OH, and WI. For a selected bidder it:

1. Uses `contractor_name` plus stored `related_companies` aliases from the approved master record.
2. Reads only the mapped BBB sitemap blocks for the bidder's state.
3. Ranks plausible profile URLs from their published slugs.
4. Opens only a small number of plausible profiles.
5. Parses the live profile identity and compares business name, address, city, state, and ZIP against the master bidder using the common V2 matching layer.
6. Requires a confirmed identity before reading the profile's direct `/complaints` page.
7. Stores the aggregate three-year complaint count and 12-month closed-complaint count as evidence. Consumer complaint narratives are not retained.

A previously confirmed BBB profile URL is reused on later runs as a routing shortcut. The live profile is re-fetched and re-scored every time, so a stale URL or changed bidder identity cannot silently remain trusted.

## Field semantics

`better_business_bureau_complaints = Y` is proposed only when a confirmed BBB profile reports one or more complaints in BBB's rolling three-year summary.

`better_business_bureau_complaints = N` is proposed only when a confirmed profile is reached successfully, the complaint summary parses successfully, and the relevant mapped sitemap discovery completed without gaps.

A missing BBB profile is not treated as `N`. No candidate, unsupported-state coverage, blocked access, parser drift, ambiguous identity, incomplete sitemap coverage, HTTP failures, or timeouts remain unknown/partial and cannot propose a negative change.

## Identity handling

The adapter uses the shared V2 `score_candidate` logic. Strong name plus location corroboration can auto-confirm a profile. Plausible but weaker matches are emitted as `AMBIGUOUS_MATCH` with `REVIEW_REQUIRED` identity status so the normal V2 identity-review workflow can record `SAME_ENTITY` or `DIFFERENT_ENTITY` judgments.

Remembered judgments are honored on later runs, but a `SAME_ENTITY` judgment still routes through the current live profile and complaint page rather than reusing stale complaint data.

## Access failures

BBB HTTP 401/403 responses, rate limiting, explicit challenge pages, timeouts, upstream failures, malformed sitemap XML, or unrecognized complaint-summary layouts are classified explicitly. They never become a clean negative.

The adapter does not attempt CAPTCHA solving or access-control bypasses.

## Tests

`backend/tests/test_bbb.py` covers:

- profile identity parsing;
- complaint-summary parsing;
- positive complaint evidence (`Y`);
- confirmed zero-complaint evidence (`N`);
- no-candidate results remaining partial instead of becoming a false negative;
- ambiguous same-name/wrong-location handling;
- blocked profile access;
- reuse and live revalidation of a previously confirmed profile URL.

Real-site operator testing is still required before moving BBB from **Ongoing** to **Completed** on the project checklist.
