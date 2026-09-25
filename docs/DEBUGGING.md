# Repeatable debugging checks

From the repository root:

```powershell
# Install dependencies and build without starting the normal app.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -SetupOnly

# Run offline fixtures, build the frontend, then launch a real isolated HTTP server.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1

# Add two live-source checks using an exported bidder database.
.venv\Scripts\python.exe scripts/check.py --live-source mn_pca --live-source responsible_mn --bidder-csv .runtime/approved-bidders-test.csv

# Audit every defined source in isolated processes using the complete bidder CSV.
.venv\Scripts\python.exe scripts/live_audit.py --bidder-csv .runtime/approved-bidders-test.csv --sam-extract ..\SAM_Exclusions_Public_Extract_V2_26264.CSV
```

The CSV path in the last command is a local export made during the initial debugging session, not a repository fixture. Supply your own exported CSV for later sessions. Without `--bidder-csv`, live checks use `Bidder Database-Example.csv`. Live checks import the entire CSV but research only the first two bidders alphabetically. The report names them and records the imported count. Other researchers and known-positive live matches are outside this initial check's coverage.

Each invocation writes `.runtime/checks/latest.json` plus a retained directory containing its report, test/build/server logs, pytest XML, application diagnostics and isolated SQLite database. The controlled workflow uses the three explicitly synthetic bidders in `test_data/`: two exact SAM matches and one no-match with an existing positive master value. It verifies that research does not change master values, approval applies one proposal, dismissal preserves the other record, and no-match does not erase the existing positive value. The fixture SAM extract receives today's date to avoid a test expiring; this does not refresh or relabel real SAM data.

Every `live_audit.py` run also writes `RUN_LOG.md` beside `report.json`. Read it first: it summarizes each source and labels every incomplete result with the source stage, affected bidder samples, source URL when available, exact warning, and artifact folder. The JSON report remains the complete machine-readable record.

Exit code 0 means all requested checks passed. Exit code 1 means a failure or live result needs attention. `live_sources.status = not_checked` means no live-source validation was requested. A green controlled workflow is not evidence that every external source works. The runner itself never edits application code or bypasses access controls; an agent uses its reports to make and verify fixes under `AGENTS.md`.

## Initial verification, 2026-09-25

- Base revision: `717b83a`, with local debugging changes not yet published.
- 196 backend tests passed; frontend production build passed. Five dependency/framework deprecation warnings remain.
- The controlled HTTP workflow passed, including diagnostic severity and run-integrity checks.
- Imported all 24 bidders from the user's existing database into the test workspace; selected the first two for each live source.
- MPCA initially returned an unusable dataset response. Follow-up requests returned a Radware CAPTCHA page. This is an external blocker; the adapter now reports `BLOCKED` with a human-verification explanation. The second bidder remains explicitly unattempted under the existing circuit breaker.
- Responsible Minnesota returned two no-matches with `PARTIAL` completeness. These must not be treated as clean eligibility results or automatic negative updates.
- Browser verification confirmed Dashboard, the 24-row Bidder Database and Diagnostics Console, including the CAPTCHA warning. The automated runner covers HTTP behavior; it does not automate browser clicks.
- Reproduced diagnostics exports omitting `SESSION_EXPIRED` and `PAGINATION_INCOMPLETE`; both now appear in `problematic_tasks`, with regression tests.
- Live WDFI pages split city, state, and ZIP across separate text nodes. The parser now reconstructs that location, converting nine previously ambiguous bidders into confirmed findings while preserving seven genuinely ambiguous results.
- BBB Reader pages can contain a long navigation section before the actual Complaints heading. The complaint parser now anchors to the content heading; the captured zero-complaint page parses successfully.
- Setup now probes Python instead of trusting the executable's existence, and recreates a nonworking virtual environment without clearing its directory. Setup-only and normal healthy-environment setup were exercised. The original moved-folder environment was repaired with the same `uv venv --allow-existing` command.

The original downloaded folder and this Git checkout are separate. Code changes are in `Paralegal-Research-Database-Workflow-V2`, not the older `Paralegal-Research-Database-Workflow-V2-main` download.
