# WCCA / CCAP adapter implementation status

The public Wisconsin Circuit Court Access adapter is implemented directly on `main` as `WccaCcapSource` (adapter `1.0.0`, parser `operator-v3`). The previous `WccaOperatorAssistedSource` name remains as a compatibility alias only.

## Why the adapter is operator-assisted

The public WCCA site intentionally uses CAPTCHA and anti-scraping controls. The production adapter therefore does not attempt CAPTCHA solving, undocumented endpoint reverse engineering, or unattended screen scraping. A fully unattended transport should be added only through the official CCAP WCCA REST subscription service after the firm obtains the applicable agreement, credentials, and technical documentation.

Operator assistance is the acquisition method, not an incomplete code path. The adapter itself owns the full V2 source contract: bounded search planning, explicit completion state, identity validation, evidence normalization, provenance, comparison semantics, and persistence through the normal research pipeline.

## Implemented pieces

- `WccaCcapSource` is registered as source key `wcca` in the common research pipeline.
- `/wcca-workbench.html` prepares the approved bidder name, explicit related-company aliases, and approved address context and opens the official WCCA site for the operator.
- `/api/sources/wcca/plans`, `/api/sources/wcca/result`, and `/api/sources/wcca/status` support the workbench and persistence workflow.
- A public WCCA run initially returns `MANUAL_REVIEW_REQUIRED`; submitting the completed workbench converts the same source task into a classified findings/no-match/ambiguous/partial/blocked result.
- Complete no-match requires every planned bidder/alias name to be confirmed searched plus explicit operator completion confirmation.
- Search names outside the approved bidder/related-company scope do not count toward completeness.
- Confirmed positive evidence requires case number and matched party/business name.
- A claimed positive whose matched party falls outside the approved bidder/related-company scope is forced to review and cannot emit `circuit_court=Y` evidence.
- Duplicate WCCA case numbers are normalized. Exact duplicate evidence is ignored; conflicting duplicate party identities force manual review.
- Optional case URLs are retained only when they use the official `https://wcca.wicourts.gov` host. Off-domain URLs are discarded with an explicit warning.
- Confirmed positive cases retain case-level provenance and can support comparison-only `circuit_court=Y` evidence.
- A complete public WCCA no-match is stored only as `wcca_public_search = NO_CURRENTLY_DISPLAYED_MATCH`; it never becomes `circuit_court=N`.
- `circuit_court` and `ccap_show150` automatic master-field writes remain disabled until the firm's legacy business rules are documented.
- The public WCCA CAPTCHA/anti-scraping controls are never bypassed.

## Test coverage

Deterministic tests cover search-plan alias handling, source-registry integration, operator handoff, complete and incomplete no-match behavior, confirmed positives, partial positives, required case identifiers, official case-URL enforcement, out-of-scope party protection, conflicting duplicate-case protection, unexpected search names, and the rule that WCCA evidence cannot create master proposals while field ownership is disabled.

## Remaining source-completion items

The adapter implementation itself is complete. The WCCA source should remain **Ongoing** on the project board until the non-code acceptance items are satisfied:

1. Confirm the firm's exact operational rule for `circuit_court`.
2. Define the legacy `ccap_show150` field, if it is still needed.
3. Walk the workbench through at least one representative known-positive bidder and one representative public no-match bidder.
4. Confirm the workbench captures the information the paralegals actually use without collecting unnecessary court data.
5. Enable any future master-field ownership only after those business rules are written and fixture-tested.

This distinction is intentional: **adapter complete** does not mean **source acceptance complete**, and it does not justify weakening WCCA's public-record limitations or bypassing the site's access controls.
