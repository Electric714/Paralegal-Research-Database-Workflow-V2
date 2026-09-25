# Debugging V2

Work directly on the current branch unless the user requests another branch. Preserve unrelated changes. Read README.md for source ownership and master/evidence safety rules.

## Commands (from the repository root on Windows)

- Setup and build without launching: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -SetupOnly`
- Launch: `START_HERE.bat`; stop: `STOP_HERE.bat`.
- Full controlled regression: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1`
- Backend only: `.venv\Scripts\python.exe -m pytest backend/tests -q`
- Selected live checks: `.venv\Scripts\python.exe scripts/check.py --live-source mn_pca --live-source responsible_mn --bidder-csv path\to\bidders.csv`

The authoritative check report is `.runtime/checks/latest.json`. Each invocation retains its own report, pytest XML, build/test/server logs, diagnostics, and isolated SQLite database under `.runtime/checks/<run-id>/`. Reports include the commit and working-tree state. These artifacts are ignored by Git and may contain bidder details.

## Completion gates

Reproduce before editing. After meaningful changes, run relevant tests and rerun the affected application workflow. Do not declare a fix complete merely because unit tests pass. The full controlled check must pass: backend tests, frontend build, real HTTP server startup, CSV import, SAM fixture acquisition/comparison, positive/no-match outcomes, run reconciliation, approve/dismiss, export, and no ERROR/CRITICAL diagnostics. Research must not mutate master data before approval; dismissal and no-match must preserve existing values.

Live checks are separate from deterministic fixtures. The runner imports the supplied bidder CSV into its isolated database and tests the first two bidders in alphabetical order against each selected source. It does not assert that these are known positive matches. `not_checked`, `needs_attention`, timeouts, missing credentials, blocked sites, incomplete research, and ambiguity are not live-source passes. Inspect source diagnostics and document scope; never mark all researchers verified from a small sample.

Continue run/diagnose/fix/retest for up to 20 iterations. After three occurrences of the same failure without progress, reassess the cause. Stop for a genuine external blocker or a material behavior decision and record evidence. Do not disable tests, swallow failures, remove functionality, reinterpret unknown as No, or weaken identity/field-ownership rules to pass a check. Ordinary fixes and reruns do not need repeated approval. Do not publish or push unless requested.
