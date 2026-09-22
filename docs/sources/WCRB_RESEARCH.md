# WCRB Integration Research

Status: research only. This branch does not register or enable a WCRB source adapter.

## What WCRB stands for

**WCRB = Wisconsin Compensation Rating Bureau.**

WCRB operates the public Wisconsin worker's compensation Coverage Lookup used by employees, attorneys, health care providers, and others to identify an employer's worker's compensation insurance carrier. Wisconsin DWD links directly to the WCRB lookup.

Primary public lookup:

- https://www.wcrb.org/coverage-lookup/
- Wisconsin DWD description: https://dwd.wisconsin.gov/wc/er-ins-lookup.htm

## What the public Coverage Lookup provides

According to WCRB and Wisconsin DWD:

- employer and carrier information is updated weekly;
- WCRB states that the public product contains the last 20 years of data;
- users can search using employer name and employer address;
- DWD says the lookup can be used with an accident/coverage date and can also determine whether an employer has a current Wisconsin worker's compensation policy;
- results provide insurance carrier contact information;
- the lookup also covers employers approved to self-insure.

The lookup is organized around the official employer name and address reported on the worker's compensation policy. Legal entity form, ownership, and multiple business locations can affect the name/address under which an employer appears.

## Critical source semantics

WCRB explicitly warns that a failed search must **not** be treated as proof that an employer lacks worker's compensation coverage.

Therefore:

- a confirmed current-coverage result can be positive evidence;
- a no-result response must not automatically produce `wc = N`;
- a blocked, failed, partial, ambiguous, or layout-changed search can never become a negative finding;
- source retrieval status and identity confidence must remain visible to the operator.

WCRB also states that public lookup information is informational and that only a certified policy can establish specific binding coverage for a particular named insured/date. The database should retain this source as research evidence, not treat it as an infallible legal certification.

## API / downloadable-data research

No public API or public bulk coverage dataset was found for the Coverage Lookup.

WCRB does publish a documented `WCUnderwriting` web service, but it is not the public Coverage Lookup. It provides experience-modification information and is available to licensed Wisconsin carrier members after enrollment and execution of a WCRB Web Services Agreement. It is therefore not an appropriate acquisition route for this paralegal database.

Relevant WCRB page:

- https://www.wcrb.org/services/

A separate public WCPAP Coverage ID Lookup exists, but it is intended to find a Coverage ID for a WCPAP application. It is potentially useful as corroborating identity information but is not a substitute for the Coverage Lookup and should not be used to infer current worker's compensation coverage.

## Technical reconnaissance of the public lookup

The public Coverage Lookup is a legacy ASP.NET Web Forms application.

Observed characteristics from the live page HTML:

- the page posts back to `https://www.wcrb.org/coverage-lookup/`;
- it uses ASP.NET AJAX / Web Forms resources;
- the disclaimer has an explicit `ctl00$body$btnDisclaimerAccept` submit control;
- the page references employer-name and employer-address search controls;
- the rendered application contains a Microsoft Ajax `NoBotBehavior` client control;
- the site says it is optimized for Chrome.

Because the form is stateful and contains an anti-automation control, manually synthesizing hidden ASP.NET state or generating a fake NoBot client state is not recommended. That would be brittle and would violate this project's rule against bypassing CAPTCHA/anti-bot/authentication mechanisms.

## Recommended acquisition method

### Recommendation: source-specific Playwright/Chromium adapter

Use a real browser engine for WCRB rather than a generic HTML scraper or reverse-engineered POST client.

The adapter should perform only the normal public workflow:

1. Open the WCRB Coverage Lookup in Chromium.
2. Accept the public notice/disclaimer through the visible form control.
3. Start a new search if required by the site's state.
4. Search the approved bidder's contractor name.
5. Search explicitly stored related-company aliases only when needed.
6. Use bidder address/city/ZIP as corroboration and/or the site's address-search mode when name results are ambiguous.
7. Parse only the returned search/result/detail content required for the selected bidder.
8. Preserve source URL, retrieval time, searched name/address, matched WCRB employer name/address, carrier information, coverage dates/status shown by the site, self-insured indication if shown, and the source HTML or a hash/snapshot when practical.
9. Close/reuse the browser session conservatively and throttle searches.

The browser adapter must not attempt to solve, synthesize, or bypass a CAPTCHA or anti-bot challenge. JavaScript that the site normally executes in Chrome may execute normally. If WCRB presents an interactive CAPTCHA, refuses the automated browser, or otherwise prevents the normal workflow, return `BLOCKED` or `MANUAL_REVIEW_REQUIRED` instead of bypassing the control.

### Why Playwright is preferred over direct HTTP for Phase 1

Direct `httpx` would be lighter, but it would require reproducing ASP.NET state/postback data and the NoBot client behavior. That is more brittle and risks becoming an anti-bot bypass. A real Chromium session naturally maintains cookies, JavaScript state, hidden form values, and postbacks exactly as the public application expects.

After a functioning browser implementation is captured and tested, we can inspect ordinary browser network traffic to determine whether WCRB exposes a stable, public, non-protected request that can safely replace Playwright. Do not start by reverse-engineering the anti-bot mechanism.

## Relationship to the existing V2 architecture

WCRB should remain a normal `ResearchSource` adapter from the application's point of view. The browser is only the source-specific acquisition implementation.

Expected result states should include at minimum:

- `SUCCESS_WITH_FINDINGS`
- `SUCCESS_NO_MATCH` (informational only; never sufficient to propose `wc = N`)
- `AMBIGUOUS_MATCH`
- `PARTIAL_RESULTS`
- `BLOCKED`
- `TIMEOUT`
- `HTTP_ERROR` / `SOURCE_UNAVAILABLE`
- `LAYOUT_CHANGED` / `PARSER_FAILURE`
- `MANUAL_REVIEW_REQUIRED`

The adapter must use the common evidence/provenance/review pipeline and must never write directly to the approved bidder row.

## Bidder matching strategy

The lookup should be constrained to companies already in the approved bidder database.

For each bidder:

1. Search the canonical `contractor_name`.
2. Compare returned employer names using the existing normalized-name matcher.
3. Corroborate with address, city, state, and ZIP whenever available.
4. If multiple plausible WCRB records remain, return `AMBIGUOUS_MATCH`; do not pick whichever result happens to appear first.
5. Search `related_companies` only as explicit aliases from the master database.
6. Remember operator SAME_ENTITY / DIFFERENT_ENTITY decisions through the existing identity-judgment system.

A strong candidate for automatic confirmation is a near-exact normalized company-name match plus matching Wisconsin address/ZIP evidence. Name-only fuzzy matches should remain reviewable.

## What to collect as evidence

Until the exact live result markup is captured, the adapter should be designed to preserve all useful source fields instead of prematurely discarding them. Candidate evidence includes:

- WCRB employer/legal name;
- employer address/location;
- current coverage indication;
- policy/coverage effective and expiration dates if displayed;
- insurance carrier name;
- carrier address and phone;
- self-insured status if displayed;
- coverage/policy identifier if displayed;
- searched name and address;
- source/result URL;
- retrieval timestamp;
- raw-result snapshot/hash;
- match score and identity evidence.

Wisconsin DWD specifically confirms that the public lookup supplies carrier name, address, and telephone number.

## Mapping to the bidder database

V2 currently assigns WCRB ownership of:

- `wc`
- `wc_date`

### `wc`

Proposed safe behavior:

- confirmed current WCRB coverage -> evidence can support `wc = Y`;
- no result -> **do not propose `wc = N`**;
- ambiguous/blocked/partial failure -> no `wc` proposal;
- an explicit WCRB result that affirmatively states no current coverage would need to be captured and reviewed before deciding whether it is sufficient for `wc = N`.

### `wc_date`

The current repository does not define the business meaning of `wc_date` precisely. The example bidder database strongly suggests that it may be the date the worker's-comp status was last researched/verified, but that is an inference, not a confirmed field definition.

Do **not** automatically map a WCRB policy effective/expiration date into `wc_date`.

Before implementation owns this field, confirm with the firm whether `wc_date` means:

- date the paralegal last checked worker's-comp status;
- policy effective date;
- policy expiration date; or
- something else.

The evidence system already stores retrieval timestamps independently, so WCRB can be implemented safely even while `wc_date` ownership remains disabled.

## Playwright packaging impact

V2 currently uses `httpx` and does not include Playwright. Adding Playwright means the Windows one-click launcher must also ensure a compatible Chromium runtime is available locally. The implementation should keep the browser runtime inside the project-managed runtime/cache rather than depend on a random system Chrome installation.

The old V1 repository already contains a source-specific Playwright/Chromium collection pattern, but its generic browser security layer blocks non-GET requests. WCRB necessarily uses form POSTs, so that implementation cannot simply be copied. A V2 WCRB browser adapter should allow only the exact same-origin navigation/form requests required by the WCRB lookup and should continue blocking unrelated navigation, downloads, WebSockets, or cross-site actions unless specifically required and reviewed.

## Recommended implementation sequence

1. **Confirm `wc_date` semantics with the firm.**
2. Add a minimal WCRB Playwright spike on this branch only.
3. Run the normal lookup manually through Playwright for two or three known bidders and capture the post-disclaimer search and result markup.
4. Save sanitized offline fixtures for positive, no-result, ambiguous/multiple-result, and layout/failure cases.
5. Build result parser tests before enabling live execution.
6. Implement conservative identity matching against bidder name + address/ZIP.
7. Persist full WCRB evidence through the common research pipeline.
8. Allow confirmed positive coverage to propose `wc = Y` only.
9. Keep `wc_date` proposals disabled until its meaning is confirmed.
10. Never produce `wc = N` from a simple no-result lookup.
11. Add source-picker readiness only after live representative bidder testing and CI pass.

## Research conclusion

The best Phase-1 integration is **not** WCRB's restricted carrier web service and **not** a generic scraper. It is a narrowly scoped WCRB-specific Playwright adapter using the public Coverage Lookup exactly as a normal Chrome user would, with strict bidder-only searching, conservative identity confirmation, and explicit blocked/ambiguous states.

The principal unresolved business question is the exact meaning of `wc_date`. The principal technical unknown is the exact post-disclaimer result markup, which should be captured through a normal browser session before implementation begins.
