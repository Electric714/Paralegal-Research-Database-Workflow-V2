# OSHA research source

## Why the acquisition path changed

The per-contractor OSHA Establishment Search remains useful for manual verification, but it is not reliable enough to be the primary batch-research path. A live 24-bidder audit on September 26, 2026 recorded repeated `ReadTimeout` responses and HTTP 504 from the OSHA ORDS establishment-search endpoints. Even with bounded retries, two bidder checks consumed the source worker's five-minute budget.

The production `osha` adapter therefore uses the U.S. Department of Labor Open Data Portal's official complete OSHA inspection dataset as its primary acquisition path. The dataset is downloaded once, validated, and indexed locally with SQLite/FTS. Bidder research then runs against the complete local inspection snapshot instead of issuing historical-window HTTP searches for every bidder.

Dataset page: `https://data.dol.gov/datasets/10334`

Official complete-dataset ZIP: `https://data.dol.gov/data-catalog/OSHA/inspection/OSHA_inspection.zip`

Registered adapter: `backend/app/research/sources/osha_bulk.py`

Legacy/live resilient adapter retained for focused diagnostics: `backend/app/research/sources/osha_resilient.py`

## Refreshing OSHA data

On Windows, after the project has been launched at least once, double-click:

`REFRESH_OSHA_DATA.bat`

Or run:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_osha_bulk.py
```

A normal refresh redownloads the current official complete dataset and rebuilds the local SQLite index. To rebuild the index from an existing cached ZIP without downloading the current snapshot, use:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_osha_bulk.py --reuse-cached-archive
```

For testing or an operator-provided copy of the official ZIP:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_osha_bulk.py --source-archive C:\path\to\OSHA_inspection.zip
```

The generated source cache lives under `backend/data/source_cache/osha/` and is not approved master data.

## Safety and completeness rules

The loader validates that the downloaded archive is a ZIP and that each inspection CSV retains the expected OSHA activity-number and establishment-name fields. A missing or malformed index fails visibly and cannot become a bidder no-match.

A fresh, complete local snapshot may support `SUCCESS_NO_MATCH`. A snapshot older than the configured freshness limit cannot produce a current clean negative: no-match results are downgraded to `PARTIAL_RESULTS` until the dataset is refreshed. Positive inspection evidence can still be shown from a stale snapshot with a freshness warning.

Bidder matching remains restricted to the approved contractor name and stored related-company aliases. Ambiguous identities remain review cases. The OSHA source owns only the legacy `osha` finding flag; `osha_severe_violations` and `years` remain evidence-only until the firm's exact field semantics are confirmed.

## Live verification gate

Do not mark OSHA complete solely because fixture tests pass. Completion still requires a real refresh of the official DOL ZIP followed by a representative/all-bidder application run showing that the local bulk path finishes within the run budget, preserves the master database, returns useful findings/no-matches/review cases, and never converts stale/incomplete acquisition into a false negative.
