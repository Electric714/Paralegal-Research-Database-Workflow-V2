# Paralegal Research Database Workflow V2

## Quick Start — Windows

Download or clone the repository, then double-click:

**`START_HERE.bat`**

The proof-of-concept launcher assumes the computer has no Python, Node.js, npm, or project dependencies installed. On first launch it downloads everything it needs into the project folder, creates an isolated `.venv`, installs dependencies, builds the React interface, starts one hidden local application process, waits for it to become healthy, and opens the default browser automatically.

There should be no separate backend/frontend console windows and no URL copying. The application runs at:

**http://127.0.0.1:8000**

Use **`STOP_HERE.bat`** to stop the hidden local application process.

Downloaded runtimes live under `.runtime/`, the Python environment lives under `.venv/`, and both are ignored by Git. The first launch requires internet access; later launches reuse the local runtimes.

For this milestone, the 14 research sources are intentionally present as **Not Implemented**. The current build is for validating the application shell, bidder CSV import/export, database browsing, research-run setup, review workflow scaffolding, source catalog, and diagnostics before implementing the external sources one at a time.

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
