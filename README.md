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

This section is the shared implementation map for humans and coding agents. Update it whenever work starts, changes state, or is merged so another agent can immediately see what is actually being worked on.

Status convention:

- `[x] DONE` — merged to `main`, tests pass, and the feature has been manually verified enough to trust its basic workflow.
- `[ ] ONGOING` — actively being implemented, debugged, or verified. Add the branch and/or PR when available.
- `[ ] TODO` — not actively implemented yet.
- `[ ] BLOCKED` — work cannot safely continue until a dependency, access issue, field definition, or source limitation is resolved. State the blocker on the same line.

Agent coordination rule: change a task from `TODO` to `ONGOING` before beginning substantial work and add the working branch/PR. A branch name by itself does **not** mean implementation exists. Do not mark a source `DONE` merely because a scraper returns a page; the source must satisfy the source completion gates below.

### Platform and Workflow Foundation

- [x] **DONE — Windows one-click application shell and local launcher** (`START_HERE.bat` / `STOP_HERE.bat`)
- [x] **DONE — Bidder CSV import/export with complete imported-row preservation**
- [x] **DONE — Stable bidder identity and import validation**
- [x] **DONE — Research task model: bidder × source tasks with explicit result/completeness states**
- [x] **DONE — Separate research evidence/provenance storage; research cannot silently overwrite the approved master database**
- [x] **DONE — Conservative contractor identity matching and remembered SAME_ENTITY / DIFFERENT_ENTITY judgments**
- [x] **DONE — Proposed-change workflow with human approve/dismiss actions, revision history, and audit events**
- [x] **DONE — Backend fixture-test pattern and CI pytest coverage for the research foundation**
- [ ] **ONGOING — End-to-end review/diagnostics UX polish as real sources are added**
- [ ] **TODO — Cross-source research-run summary showing completed, no-match, ambiguous, partial, blocked, and failed counts in one place**
- [ ] **TODO — Operator-friendly retry/re-run controls for failed or partial source tasks without duplicating evidence**
- [ ] **TODO — Final clean-machine regression pass of install → import → research → review → approve/dismiss → export**

### Source Implementation Checklist

Keep this list in the same order as the firm's source list so it remains easy to reconcile with the existing workflow.

1. [ ] **ONGOING — Wisconsin Department of Financial Institutions (WDFI)** — branch `feature/wdfi-corporate-records` exists, but it currently has no implementation commits ahead of `main`. Intended owned field: `dfi`. Next: implement targeted corporate-record acquisition, identity matching, evidence capture, conservative proposal behavior, fixtures, and manual verification.
2. [ ] **ONGOING — OSHA Establishment Search** — PR #5 (`feature/osha-establishment-research`) is open with the source adapter, parsing, identity checks, inspection-detail evidence, and tests. Next: reconcile/merge the PR, manually verify representative bidder searches, and keep `osha_severe_violations` / `years` evidence-only until those field semantics are confirmed.
3. [ ] **TODO — Wisconsin Compensation Rating Bureau (WCRB)** — intended owned fields: `wc`, `wc_date`. **Recommended next new source after the currently active SAM/OSHA/WDFI/BBB work is stabilized.** First step: determine the most reliable WCRB acquisition path and the exact meaning of the firm's `wc` and `wc_date` fields before allowing proposals.
4. [ ] **TODO — Wisconsin Circuit Court Access / CCAP (WCCA)** — intended owned fields: `circuit_court`, `ccap_show150`. Requires a source-specific court-search design, pagination/completeness handling, entity matching, and explicit rules for what qualifies as a proposed field change.
5. [ ] **TODO — PACER** — intended owned field: `federal_court`. Authenticated/possibly fee-sensitive source; define permitted access, query-cost controls, session handling, and human-review behavior before implementation.
6. [ ] **ONGOING — SAM.gov Public Exclusions** — core official Public Exclusions V2 CSV/ZIP workflow is merged to `main`, and the false-positive identity-matching hardening from PR #6 is also merged. Next: finish end-to-end operator verification of the manual extract upload path, regression-test false positives against real sample bidders, and remove/disable any remaining API-key-first UX that conflicts with the preferred uploaded daily extract workflow.
7. [ ] **ONGOING — Better Business Bureau (BBB)** — branch `feature/bbb-targeted-research` exists, but it currently has no implementation commits ahead of `main`. Intended owned field: `better_business_bureau_complaints`. Next: implement targeted bidder lookup, stable profile selection, complaint evidence extraction, ambiguity handling, tests, and manual verification.
8. [ ] **TODO — Violation Tracker** — useful enforcement evidence source. Automatic field ownership is intentionally unassigned until the firm confirms which bidder field(s) this source is allowed to update; evidence collection may be implemented before proposal ownership.
9. [ ] **TODO — Wisconsin DOT Contractor Information** — source acquisition and exact bidder-field ownership still need to be defined before automatic proposals are allowed.
10. [ ] **TODO — U.S. Department of Labor Enforcement Data** — likely useful for multiple labor/enforcement fields, but field ownership is intentionally withheld until the firm's definitions are confirmed. Prefer official downloadable/data-catalog paths when available.
11. [ ] **TODO — GSA OIG State Suspension & Debarment Directory** — treat primarily as a directory/meta-source for authoritative state debarment sources unless a direct contractor-level dataset is identified. No bidder-field ownership is currently assigned.
12. [ ] **TODO — Minnesota Debarred Vendors** — intended owned field: `state_federal_debarment`. Determine current authoritative acquisition format, matching rules, completeness, and freshness behavior.
13. [ ] **TODO — Responsible Minnesota** — source acquisition and exact bidder-field ownership still need to be confirmed; evidence-only until mapping is approved.
14. [ ] **TODO — Minnesota Pollution Control Agency Enforcement Actions** — intended owned field: `environmental_violations`. Prefer official structured/downloadable enforcement data when available and preserve enforcement-action provenance.

### Source Completion Gates

A source moves from `ONGOING` to `DONE` only when all applicable gates are satisfied:

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

Finish and verify the four active source tracks first: **SAM.gov → OSHA → WDFI → BBB**. After those are stable, implement **WCRB** next because it has a narrow, already-defined field scope (`wc`, `wc_date`) and fits the source-specific adapter pattern cleanly. Then proceed to **WCCA/CCAP**, followed by structured enforcement/debarment datasets such as **Violation Tracker, Wisconsin DOT, DOL Enforcement, Minnesota Debarment, and Minnesota PCA**. Leave **PACER** until the authenticated-access and cost/session model is explicitly designed rather than bolting login automation onto the general scraper pipeline.

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
7. Better Business Bureau — http://www.bbb.org/wisconsin
8. Violation Tracker — https://violationtracker.goodjobsfirst.org/
9. Wisconsin DOT contractor information — http://wisconsindot.gov/Pages/doing-bus/contractors/hcci/cntrct-info.aspx
10. U.S. Department of Labor Enforcement Data — https://enforcedata.dol.gov/views/data_catalogs.php
11. GSA OIG state suspension/debarment directory — https://www.gsaig.gov/content/suspension-and-debarment-sites-state
12. Minnesota debarred vendors — http://www.mmd.admin.state.mn.us/debarredreport.asp
13. Responsible Minnesota — http://responsiblemn.org/
14. Minnesota Pollution Control Agency enforcement actions — https://www.pca.state.mn.us/regulations/quarterly-summary-enforcement-actions

Use the most reliable acquisition method appropriate for each source. Some may use an API, some an official downloadable dataset, some HTML, some PDF parsing, some browser automation, and some authenticated access. Do not force every source through one generic scraper. For example, SAM.gov publishes official public exclusions data that may be better suited to local download/cache/matching than repeated per-contractor API calls.

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
