# Wisconsin DFI Corporate Records source

## Purpose

The `wdfi` adapter researches the Wisconsin Department of Financial Institutions Corporate Records Search (CRIS) for bidders already present in the approved master database. It is not a company-discovery crawler and it never writes directly to the master.

Public search entry point: `https://apps.dfi.wi.gov/apps/corpsearch/search.aspx`

The adapter uses the public corporate-record HTML interface. It does not use authentication, bypass access controls, solve challenges, or treat a blocked/partial response as a clean result.

## Acquisition flow

For each selected bidder the adapter:

1. Builds a bounded list from `contractor_name` and `related_companies` without splitting commas inside legal names.
2. Runs WDFI advanced entity-name searches using exact-phrase mode, current and old names, and both active and inactive entities.
3. Parses DFI entity ID, entity name/type, registered effective date, status, status date, and the entity-detail link.
4. Scores plausible results conservatively.
5. Opens only the strongest candidate detail pages to obtain the registered/principal office information used for identity corroboration.
6. Auto-confirms only an exact primary bidder-name match with corroborating master location data (or an exact primary name when the master has no location at all).
7. Sends uncertain or multiple plausible matches to the existing identity-review workflow instead of guessing.
8. Saves the normalized WDFI record as immutable research evidence and, when allowed, proposes a `dfi` field change for separate human approval.

A remembered `SAME_ENTITY` or `DIFFERENT_ENTITY` judgment is reused on later runs for the same bidder and WDFI entity ID.

## Field ownership and legacy semantics

WDFI owns the bidder `dfi` field only for Wisconsin-primary bidders in this implementation.

Known active/good-standing WDFI statuses such as `Organized`, `Registered`, `Incorporated/Qualified/Registered`, and `Restored to Good Standing` map to the legacy master value `Y`.

Other WDFI statuses are mapped to a readable status plus status date, for example `Delinquent as of 10/1/24`. When the existing master already expresses the same WDFI status and date using a different legacy date/capitalization format, the existing representation is preserved so Review is not flooded with cosmetic changes.

For bidders whose primary state is not Wisconsin, WDFI matches remain evidence only. The supplied legacy `dfi` field can contain combined home-state and Wisconsin-registration wording (for example, a home-state active status plus a WDFI revocation/not-found clause). Replacing that entire field from WDFI alone could destroy valid home-state context, so the adapter deliberately does not create an automatic `dfi` proposal for those rows.

## Negative and failure semantics

A complete WDFI search with no plausible entity produces `SUCCESS_NO_MATCH` evidence but does not invent `dfi = N` and does not erase an existing master value.

If WDFI returns a blocked response, timeout, HTTP error, incomplete result table, or an identity that cannot be safely resolved, the result remains blocked/partial/ambiguous. Blank means unknown; a source failure is never converted into a negative finding.

## Evidence retained

The normalized snapshot retains, when available:

- bidder/search name and query URL
- WDFI entity ID
- legal entity name and any matched old-name hint
- entity type
- registered effective date
- current WDFI status and status date
- principal and registered-office text
- parsed address/city/state/ZIP used for corroboration
- candidate match scores
- source detail URL
- acquisition, adapter, and parser versions
- warnings and completeness/identity classifications

The source-specific data remains research evidence until the normal Review workflow approves a proposed master-field change.
