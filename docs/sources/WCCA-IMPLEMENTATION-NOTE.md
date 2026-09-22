# WCCA implementation status

The operator-assisted WCCA workflow is implemented on `feature/wcca-ccap-research` and is intended to merge into `main` after CI validation.

Implemented pieces:

- `WccaOperatorAssistedSource` registers WCCA in the research source pipeline without scraping the public court site.
- Public WCCA runs stop in `MANUAL_REVIEW_REQUIRED` until an operator completes the guided workflow.
- `/wcca-workbench.html` prepares contractor names, related-company aliases, and master address context; links to the official WCCA site; records findings/no-match/ambiguous/partial/blocked outcomes; and submits structured evidence.
- `/api/sources/wcca/plans`, `/api/sources/wcca/result`, and `/api/sources/wcca/status` support the workbench.
- Complete no-match requires every planned name to be checked and explicit operator completion confirmation.
- Confirmed positive cases retain case number, county, party name, case type/status, optional case URL, operator note, provenance, and completeness/identity classifications.
- `circuit_court` evidence is compared with the current master value but cannot create a proposal yet.
- `ccap_show150` remains write-disabled because its legacy meaning is still undefined.
- The public WCCA CAPTCHA/anti-scraping controls are not bypassed.

Remaining review items:

1. Confirm the firm's exact `circuit_court` rule.
2. Define `ccap_show150`.
3. Walk through the workbench with a known positive contractor and a known no-match contractor.
4. Decide whether the workbench should remain a dedicated page or be folded directly into the React Research screen.
