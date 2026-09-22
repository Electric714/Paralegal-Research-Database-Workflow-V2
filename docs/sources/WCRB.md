# WCRB Coverage Lookup Source

## Purpose

This connector will research contractors already present in the approved bidder database against the Wisconsin Compensation Rating Bureau (WCRB) public Coverage Lookup.

The source is intended to support the bidder database's workers-compensation fields without discovering new contractors and without treating an inaccessible or incomplete lookup as evidence of no coverage.

## Public source

Primary source:

`https://www.wcrb.org/coverage-lookup/`

WCRB states that Coverage Lookup is available to employees, attorneys, and others seeking insurance-carrier claims-processing information for a particular coverage, injury, or illness date. WCRB states that employer and carrier information is updated weekly and that the product contains the last 20 years of data.

The application currently uses a legacy ASP.NET form with a notice/disclaimer and stateful postback workflow. The rendered page also includes a Microsoft Ajax `NoBot` control. This means the source must be integrated deliberately rather than treated as a simple stateless GET endpoint.

## Source warning that must be preserved

WCRB explicitly warns that search results contain employer names and addresses reported on a policy and do not, by themselves, guarantee binding coverage for a particular date. WCRB also explicitly says that failure to find an employer must **not** be assumed to mean the employer lacked workers-compensation coverage.

That warning is part of the connector contract.

A no-match result from WCRB must therefore never automatically propose:

`wc = N`

A blocked, timed-out, malformed, layout-changed, incomplete, or otherwise failed lookup must likewise never become a negative finding.

## Bidder field ownership

The common field mapping currently assigns WCRB these fields:

- `wc`
- `wc_date`

The sample bidder database strongly suggests that `wc_date` may represent the date staff last checked workers-compensation information rather than a policy effective date. That meaning must be confirmed before the adapter is allowed to propose automatic `wc_date` changes.

Until that meaning is confirmed, the first production adapter should be conservative:

- preserve the lookup/retrieval date in evidence provenance
- allow a confirmed positive WCRB identity/coverage finding to support `wc = Y`
- do not automatically propose `wc = N`
- do not automatically propose a `wc_date` value solely because a lookup was attempted

## Acquisition strategy

Preferred order:

1. Validate whether the normal WCRB ASP.NET postback workflow can be performed reliably with an ordinary stateful HTTP client while preserving cookies and server-provided hidden fields.
2. Do not synthesize or bypass anti-bot challenge values. If the site's normal workflow requires client-side behavior that cannot be reproduced safely with ordinary HTTP requests, use a normal browser-backed acquisition path instead of bypassing the control.
3. If WCRB presents a CAPTCHA, authentication requirement, or other access control, do not bypass it. Return an explicit blocked/manual-review state.
4. Keep acquisition source-specific; do not route WCRB through a generic scraper.

The connector should submit only the minimum contractor information necessary for a targeted lookup. It should research only bidders selected from the approved master database.

## Identity matching

Positive records must be matched conservatively against:

- contractor name
- known related companies
- street address when present
- city
- state
- ZIP

Strong name similarity without corroborating location evidence should remain reviewable rather than being auto-confirmed.

If WCRB returns multiple plausible records, store each candidate as evidence and require human identity review instead of choosing one silently.

## Result classification

The adapter must preserve the common research result contract.

Expected examples:

- completed lookup with confirmed positive record: `SUCCESS_WITH_FINDINGS`, `CONFIRMED`, `COMPLETE`
- completed lookup with no returned employer: preserve the completed-search evidence but do not create `wc = N`; absence is not proof of no coverage
- multiple plausible identities: `AMBIGUOUS_MATCH` / `REVIEW_REQUIRED`
- disclaimer/session or anti-bot flow cannot be completed normally: `BLOCKED` or `MANUAL_REVIEW_REQUIRED`
- authentication unexpectedly required: `AUTH_REQUIRED`
- timeout/network failure: `TIMEOUT`, `HTTP_ERROR`, or `SOURCE_UNAVAILABLE`
- unrecognized page/result layout: `LAYOUT_CHANGED`
- incomplete pagination/results: `PARTIAL_RESULTS` or `PAGINATION_INCOMPLETE`

No failure or partial state may produce an automatic proposal.

## Evidence to retain

For each useful WCRB result retain, where available:

- exact searched contractor name
- search date / coverage date used by the lookup
- returned employer name
- returned employer address
- carrier / claims-processing information
- policy/coverage identifiers exposed by the public result
- result URL or source page
- retrieval timestamp
- identity score and match explanation
- adapter/parser version
- warnings from the WCRB disclaimer

Do not store or expose credentials because the public Coverage Lookup should not require them.

## Implementation acceptance criteria

The WCRB source is not `ready` until all of the following are true:

1. The normal public lookup flow has been validated against the live site without bypassing CAPTCHA/authentication or fabricating anti-bot values.
2. Saved HTML fixtures exist for at least one positive result, one no-result response, and one malformed/layout-change response.
3. Parser tests cover employer identity and returned coverage/carrier details.
4. A confirmed positive match can create `wc = Y` evidence through the common evidence pipeline.
5. A no-match response cannot create `wc = N` evidence or a proposal.
6. Blocked/timeout/layout/session failures cannot become clean negatives.
7. Ambiguous company matches require human review.
8. `wc_date` proposal behavior remains disabled until the firm's field meaning is explicitly confirmed.
9. The source is registered in `source_registry.py` and marked `ready` in `backend/app/sources.py` only after the above tests pass.

## Next engineering step

Perform a short live workflow capture of the normal Coverage Lookup interaction to identify the exact ASP.NET postback fields and result markup. Use that capture to build fixture-based parser tests first, then implement the source adapter. Do not begin by guessing control names or declaring the source complete from the landing page alone.
