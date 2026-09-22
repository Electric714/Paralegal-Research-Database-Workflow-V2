# WCCA review checklist

For the next review pass:

- Launch the app with `START_HERE.bat` and open the WCCA source/workbench.
- Verify a WCCA research run creates `MANUAL_REVIEW_REQUIRED` tasks rather than scraping the public site.
- Test a known positive contractor and confirm case evidence, provenance, identity status, and comparison display.
- Test a known no-match contractor and verify a clean negative requires every planned alias plus explicit completion confirmation.
- Confirm blocked/partial/ambiguous searches never become clean negatives.
- Confirm no WCCA evidence can change the approved master while `circuit_court` and `ccap_show150` semantics remain unconfirmed.
- Define the legacy meaning of `ccap_show150` before enabling any writes to that field.
