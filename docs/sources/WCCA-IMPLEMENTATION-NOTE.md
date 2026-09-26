# WCCA / CCAP adapter implementation status

The primary Wisconsin Circuit Court Access adapter now uses the normal public WCCA website through a persistent local browser session. It does **not** use the paid WCCA REST API and the REST API is **not** the planned acquisition path for this project.

The registered source is `WccaPublicBrowserSource` (adapter `2.0.0`, parser `public-results-v1`). The older `WccaCcapSource` / `WccaOperatorAssistedSource` implementation remains only as the manual workbench fallback and as shared result-normalization logic.

## Acquisition design

The adapter opens the same public WCCA pages a paralegal uses, accepts the normal WCCA site acknowledgement, enters each approved bidder/business name in the public **Business name** search, submits the search, and reads the public `caseSearchResults` table.

The current public result table is validated against these columns:

- Case number
- Filing date
- County name
- Case status
- Name
- Date of birth
- Caption

Case detail links are retained only when they remain on `https://wcca.wicourts.gov`.

WCCA loads invisible hCaptcha on the search page. The application does not solve, inject, bypass, or reverse-engineer hCaptcha. In the normal case, WCCA permits the browser search to proceed without a human challenge and the adapter continues automatically. If WCCA presents an interactive challenge, the normal headed browser remains open for the operator to complete the challenge manually; the adapter then resumes from the resulting public results page. A challenge that is not completed before the configured timeout becomes an explicit `BLOCKED` result, never a no-match.

The browser profile is persisted under `.runtime/browser-profiles/wcca` by default so normal cookies/session state can be reused between searches. Microsoft Edge is preferred on the supported Windows workstation, followed by Chrome and then a Playwright Chromium runtime if one is available.

## Implemented behavior

- `WccaPublicBrowserSource` is registered as source key `wcca` in the common research pipeline.
- A normal research run now performs the public WCCA business searches instead of immediately returning `MANUAL_REVIEW_REQUIRED`.
- The adapter searches the approved contractor name plus explicitly stored related-company aliases and does not silently broaden scope.
- Legal-name commas are preserved. Explicit alias separators remain semicolon, pipe, and newline.
- Every planned name must complete before a public no-match can be classified complete.
- Search results are read from the public WCCA results table and paginated/deduplicated by case number plus matched party.
- Exact normalized party-name matches can become confirmed positive evidence automatically.
- Similar but non-exact party names are retained as candidates and force `AMBIGUOUS_MATCH` / human identity review rather than being accepted automatically.
- Confirmed positive cases retain case number, filing date, county, case status, matched party, caption, and official WCCA URL.
- Confirmed positives can support comparison-only `circuit_court=Y` evidence.
- A complete public WCCA no-match is stored only as `wcca_public_search = NO_CURRENTLY_DISPLAYED_MATCH`; it never becomes `circuit_court=N`.
- A blocked challenge, browser failure, layout change, incomplete pagination, or partial alias search cannot become a false negative.
- If an earlier alias already produced a confirmed positive and a later alias becomes blocked, the positive evidence is retained while the overall source result remains incomplete/blocked.
- `circuit_court` and `ccap_show150` automatic master-field writes remain disabled until the firm's legacy business rules are documented.
- `/wcca-workbench.html` remains available as a manual fallback and evidence-entry/review tool; it is no longer the primary acquisition path.

## Public-layout validation performed 2026-09-26

The live public WCCA site was inspected before implementing this adapter. The current flow was confirmed as:

1. Public WCCA acknowledgement page with an `I agree` button.
2. Simple Case Search page containing `input[name="businessName"]` and `button[name="search"]`.
3. Invisible hCaptcha loaded on the search page.
4. A normal public business-name search was successfully submitted without manually solving a challenge.
5. The returned page contained `table#caseSearchResults` with the expected seven columns, `a.case-link` case links, and normal DataTables pagination.

Those selectors are treated as a source contract. If WCCA changes them, the adapter should return `LAYOUT_CHANGED` rather than reinterpret an unknown page as a clean result.

## Test coverage

Deterministic coverage now tests the live-browser source contract as well as the legacy evidence-normalization rules. The browser-source tests cover complete no-match, exact positive match, similar-name ambiguity, human-challenge timeout, layout change, browser unavailability, retention of earlier positive evidence after a later challenge, registry integration, and the no-REST/no-CAPTCHA-bypass contract.

The CI run after registration of the live browser adapter passed the backend suite, frontend build, and Windows launcher checks with **233 backend tests passing**.

## Remaining acceptance item

The code path is implemented and CI-tested. The remaining acceptance step is a live run of the actual application on the supported Windows workstation against the approved bidder list so the local Edge/Chrome session, WCCA hCaptcha behavior, and result extraction are exercised end-to-end in the same environment the paralegal will use.

Do not replace this public-browser path with the paid WCCA REST API unless the project requirements are explicitly changed in the future.
