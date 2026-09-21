# Paralegal Research Database Workflow V2

## Quick Start — Windows

Download or clone the repository, then double-click:

**`START_HERE.bat`**

The proof-of-concept launcher assumes the computer has no Python, Node.js, npm, or project dependencies installed. On first launch it downloads everything it needs into the project folder, creates an isolated `.venv`, installs dependencies, builds the React interface, starts one hidden local application process, waits for it to become healthy, and opens the default browser automatically.

There should be no separate backend/frontend console windows and no URL copying. The application runs at:

**http://127.0.0.1:8000**

Use **`STOP_HERE.bat`** to stop the hidden local application process.

Downloaded runtimes live under `.runtime/`, the Python environment lives under `.venv/`, and both are ignored by Git. The first launch requires internet access; later launches reuse the local runtimes.

## Project Completion Checklist / Agent Coordination Board

This is the shared project map for humans and coding agents. Update this section whenever work starts, finishes, becomes blocked, or moves back into active development.

A task belongs under **Completed** only when the relevant work is merged to `main`, tests pass, and the basic workflow has been manually verified. Move a task from **To Do** to **Ongoing** before beginning substantial work and include the branch or PR when one exists.

### Completed

- [x] **UI / App Shell** — Windows one-click launcher and local application shell
- [x] **Database Import / Export** — bidder CSV import/export with complete imported-row preservation
- [x] **Bidder Database / Record Handling** — stable bidder identity, import validation, and complete bidder records
- [x] **Research Backend Foundation** — bidder × source research tasks with explicit result/completeness states
- [x] **Evidence / Provenance System** — research evidence stored separately from the approved master database
- [x] **Identity Matching System** — conservative contractor matching with remembered SAME_ENTITY / DIFFERENT_ENTITY judgments
- [x] **Approve / Dismiss Review Workflow** — proposed changes require human approval before modifying master data
- [x] **Audit / Revision History** — master revisions and audit events retained
- [x] **Automated Backend Testing / CI** — fixture-test pattern and pytest CI coverage for the research foundation
- [x] **GSA State Suspension / Debarment Directory — No Direct Integration Needed** — evaluated as a legacy/meta-directory of links to state suspension/debarment sources, not a contractor-level dataset. The old GSA OIG directory URL is no longer a usable data source. Do not build a GSA scraper/adapter; integrate the relevant authoritative state sources directly instead.

### Ongoing

- [x] **Research Run Summary Dashboard** — persisted run reconciliation with completed/no-match/ambiguous/partial/blocked/failed/not-checked counts, per-source and bidder × source drill-down, source-owned-field change display, integrity checks, and safety tests. Merged in PR #27; CI passed.

- [ ] **WDFI** — Wisconsin Department of Financial Institutions. Branch: `feature/wdfi-corporate-records`. Intended owned field: `dfi`
- [ ] **SAM.gov** — Public Exclusions V2 CSV/ZIP workflow and identity-matching hardening are merged; final operator/end-to-end verification of the preferred manual extract workflow remains
- [ ] **OSHA** — OSHA Establishment Search adapter from PR #5 is merged to `main`; CI passes and the source picker is ready. Final representative real-bidder/end-to-end verification remains before marking the source complete
- [ ] **BBB** — Targeted business-profile research and post-merge hardening are merged to `main`. Intended owned field: `better_business_bureau_complaints`. Final representative real-bidder/end-to-end verification remains before marking the source complete
- [ ] **WCRB** — Wisconsin Compensation Rating Bureau; intended owned fields: `wc`, `wc_date`
- [ ] **WCCA / CCAP** — operator-assisted WCCA workbench is implemented and post-merge semantics are hardened. Confirmed displayed cases are positive-only comparison evidence for `circuit_court`; a public WCCA no-match never becomes `circuit_court=N`. `ccap_show150` remains undefined/write-disabled pending the firm's legacy rule. See `docs/sources/WCCA.md`. Representative real-bidder verification remains before completion
- [ ] **Violation Tracker** — enforcement evidence; exact bidder-field ownership still needs confirmation
- [ ] **U.S. Department of Labor Enforcement Data** — use the official DOL Open Data Portal v4 API rather than scraping the retired enforcement-data site. DOL API accounts are free; the `/v4/datasets` catalog is keyless, while metadata/data requests require a private API key that must never be committed or logged. Phase 1 will target Wage and Hour Division enforcement/compliance data with conservative bidder identity matching. Branch: `feature/dol-enforcement-api`. Automatic field ownership remains disabled until the firm's semantics are confirmed; `prevailing_wage_violations` is the strongest candidate mapping for confirmed Davis-Bacon and Related Acts findings.
- [ ] **Wisconsin DOT Contractor Information** — automatic WisDOT/HCCI official-PDF refresh, immutable caching, debarment/vendor matching, and Finals Status evidence are implemented in PR #19 (`feature/wisdot-auto-refresh-implementation`). Confirmed debarred/suspended/ineligible matches may propose `state_federal_debarment = Y`; WisDOT no-match never proposes `N`; Finals Status remains evidence-only. Final representative real-bidder/end-to-end verification remains before marking complete.
- [ ] **PACER — Problems (Ongoing)** — intended owned field: `federal_court`. The official PACER API/search is fee-based, so this project will not use it. Free alternatives reviewed so far do not provide an adequate replacement for the firm's PACER workflow. Keep this item open until a reliable no-cost acquisition method is identified; do not implement paid PACER API access.
- [ ] **Minnesota Debarred Vendors** — intended owned field: `state_federal_debarment`
- [ ] **Responsible Minnesota** — acquisition path and exact bidder-field ownership still needs confirmation
- [ ] **Minnesota PCA Enforcement Actions** — intended owned field: `environmental_violations`
- [ ] **Retry / Re-run Controls** — operator-friendly retry of failed or partial source tasks without duplicating evidence
- [ ] **Review / Diagnostics UI Polish** — continue improving the end-to-end review and diagnostics experience as real sources are integrated

### To Do

- [ ] **Final Clean-Machine End-to-End Test** — install → import → research → review → approve/dismiss → export

### Source Completion Gates

A source moves from **Ongoing** to **Completed** only when all applicable gates are satisfied:

1. Acquisition method is source-specific, documented, and uses the most reliable available official/public path rather than forcing a generic scraper.
2. Research scope is limited to contractors already present in the approved bidder database plus explicitly stored related-company aliases.
3. Source adapter is registered in the common research pipeline and reports explicit success/no-match/ambiguous/partial/blocked/failed states.
4. Parser or dataset loader validates the expected layout and fails visibly when the source changes.
5. Identity matching is conservative; ambiguous matches require human review and cannot automatically change master data.
6. Useful evidence keeps source provenance, retrieval time, searched contractor, source record/details, and raw-artifact/hash information when practical.
7. A clean negative is produced only when the source check is complete enough to support it. Blocked, stale, partial, failed, or ambiguous checks never become false negatives.
8. Source-to-field ownership is explicitly confirmed. A source may collect useful evidence without being allowed to propose a master-field change.
9. Offline fixtures cover normal parsing, positive match, no-match, ambiguity, and at least the major expected failure/partial cases.
10. The source has been manually tested with representative real bidder records, including at least one likely match and one no-match where practical.
11. Changes are merged to `main`, CI passes, and the application/source picker reports the source as ready only after the implementation is actually present on `main`.

### Recommended Implementation Order

Finish and verify the active source tracks first: **SAM.gov → OSHA → WDFI → BBB → WCRB → WCCA/CCAP → Violation Tracker → DOL Enforcement → Wisconsin DOT → Minnesota Debarment**. DOL Enforcement should use the official v4 Open Data API and begin with WHD enforcement data rather than scraping the retired Enforcement Data site. Wisconsin DOT uses automatic official-PDF refresh and local cached matching rather than per-bidder scraping. Then proceed to remaining structured enforcement/debarment sources such as **Minnesota PCA**. The former **GSA State Suspension / Debarment Directory** is not an implementation target; use the underlying authoritative state sources directly. **PACER remains an ongoing problem item until a reliable no-cost acquisition method is identified; do not implement the fee-based PACER API/search workflow.**

## Project Goal

This project exists to automate a real paralegal research workflow for a law firm. The firm already has an approved bidder/contractor database, currently maintained in CSV/Excel form. Staff manually take companies from that database, search a fixed set of government, court, regulatory, business, and compliance websites, compare what they find to the current record, and update the database when new information appears. Several people repeatedly perform this work and it is slow, repetitive, and difficult to keep fully current.

The goal of this application is to automate as much of that research-and-comparison process as possible. The program should import the firm's existing bidder database, research only those existing companies across the sources the firm already uses, collect findings as separate evidence, compare that evidence to the approved master record, and show the paralegal what changed. The user can then approve or dismiss proposed changes individually or in bulk. Only approved changes update the master database.

The intended workflow is:

**Import master database → choose contractors/sources → run research → collect evidence → compare against current values → show new/different information → human approves or dismisses → update/export approved database → repeat later to detect new changes**

This is not a generic web scraper and it is not intended to discover random companies. The approved bidder database controls the research scope.

## Core Rule

**Approved master database ≠ outside research evidence.**

Research must never silently overwrite the firm's approved data. Failed, blocked, partial, ambiguous, or incomplete research must never be treated as a clean result. Blank means unknown, not "No." A source failure cannot erase an existing value. Ambiguous company matches must be shown for human review rather than automatically accepted.

Every useful finding should retain provenance where possible: source, URL or dataset, retrieval time, contractor searched, source record/details, identity match information, and enough context for a paralegal to understand why a change is being proposed.

## Main Sources Used by the Firm

These are the primary sites the firm's staff currently checks:

1. Wisconsin Department of Financial Institutions — https://www.wdfi.org/
2. OSHA Establishment Search — https://www.osha.gov/pls/imis/establishment.html
3. Wisconsin Compensation Rating Bureau — https://www.wcrb.org/
4. Wisconsin Circuit Court Access / CCAP — https://wcca.wicourts.gov/index.xsl
5. PACER — https://pacer.login.uscourts.gov/csologin/login.jsf
6. SAM.gov — https://www.sam.gov/SAM/
7. Better Business Bureau — https://www.bbb.org/
8. Violation Tracker — https://violationtracker.goodjobsfirst.org/
9. Wisconsin DOT contractor information — https://wisconsindot.gov/Pages/doing-bus/contractors/hcci/cntrct-info.aspx
10. U.S. Department of Labor Enforcement Data — https://data.dol.gov/ (official API: https://apiprod.dol.gov/v4)
11. GSA OIG state suspension/debarment directory — legacy/meta-directory only; no direct integration is needed. Use the authoritative state sources it was intended to point to instead.
12. Minnesota debarred vendors — http://www.mmd.admin.state.mn.us/debarredreport.asp
13. Responsible Minnesota — http://responsiblemn.org/
14. Minnesota Pollution Control Agency enforcement actions — https://www.pca.state.mn.us/regulations/quarterly-summary-enforcement-actions

Use the most reliable acquisition method appropriate for each source. Some may use an API, some an official downloadable dataset, some HTML, some PDF parsing, some browser automation, and some authenticated access. Do not force every source through one generic scraper. For example, SAM.gov publishes official public exclusions data that may be better suited to local download/cache/matching than repeated per-contractor API calls. DOL Enforcement should use the official Open Data Portal v4 API; its API accounts are free, but API keys are private credentials and must never be committed to Git or exposed in logs/evidence. State suspension/debarment research should use the relevant authoritative state sources directly rather than treating the former GSA OIG directory as a contractor dataset.

## Bidder Database Schema

The example bidder database currently contains 30 supported fields. The application preserves the complete imported row, not just the summary columns shown in the main table.

The expected example fields are:

`id`, `contractor_name`, `related_companies`, `address_1`, `city`, `state`, `zip`, `additional_address`, `additional_address_city`, `additional_address_state`, `additional_address_zip`, `dfi`, `wc`, `wc_date`, `osha_severe_violations`, `years`, `osha`, `state_federal_debarment`, `mndol_ineligibility`, `public_works_projects_budget_time_quality_complaint`, `federal_court`, `circuit_court`, `ccap_show150`, `environmental_violations`, `prevailing_wage_violations`, `dwd`, `dwd_substance_abuse_plan`, `better_business_bureau_complaints`, `misc_violations`, `tax_liability`.

The main bidder grid is intentionally a readable summary. Clicking a bidder opens the complete record with all 30 expected fields grouped by identity/address, business/coverage, safety/eligibility/public works, and courts/regulatory/complaints. Additional imported columns are preserved and displayed separately. CSV export preserves the active import's original column structure.

The exact meaning and ownership of each field should follow the firm's existing workflow. Do not guess that a source can update a field simply because the data sounds related. Source-to-field mappings should be explicit and tested.

## Reliability Requirements

A successful source check means more than "the scraper did not crash." The application should know whether the source was reached, whether the intended search completed, whether all required pages/results were processed, whether the contractor identity was confirmed, and whether the evidence is complete enough to support a proposed change.

The system must clearly distinguish successful checks from blocked access, timeouts, HTTP errors, login/session problems, parser/layout changes, incomplete pagination, malformed datasets, ambiguous matches, and other partial failures. These states should remain visible to the user instead of collapsing into misleading Yes/No values.

## What Success Looks Like

A mature version should let a paralegal import the firm's existing bidder database, run research across the same sources staff currently checks manually, and receive a clear review queue showing what is new, what changed, what could not be checked, and what requires human judgment. The long-term objective is to move staff away from repeatedly searching the same sites company-by-company and toward reviewing meaningful new findings and exceptions.

Future contributors and coding agents should use this README as the source of truth for the project's purpose. Preserve the master/evidence separation, prioritize reliable source-specific acquisition, keep an audit trail, and build toward reducing the firm's recurring manual research workload rather than merely making individual scrapers work.
