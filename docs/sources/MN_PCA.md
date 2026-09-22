# Minnesota PCA Enforcement Actions

## Purpose

This adapter answers one narrow bidder-database question: whether an approved bidder or explicitly stored related-company alias has a confirmed Minnesota Pollution Control Agency enforcement record relevant to the master field `environmental_violations`.

## Acquisition

Primary source: the official MPCA "Enforcement actions with penalties" Tableau data portal.

The adapter requests the portal's structured CSV export once per research-source instance, validates the expected regulated-party and violation columns, hashes the downloaded bytes, and performs bidder matching locally. It does not scrape annual PDF reports and does not perform one remote request per bidder.

If MPCA returns HTML, blocks access, times out, changes the dataset schema, or otherwise prevents a validated full extract, the adapter fails closed. Those states can never become a clean negative.

## Matching and field semantics

Research scope is limited to `contractor_name` plus explicitly stored `related_companies` aliases. Corporate punctuation/suffix normalization is allowed. Exact normalized identity can produce a confirmed finding. Very similar but non-exact names are review-required and cannot propose a master change.

Confirmed findings emit evidence for `environmental_violations = Y`. Dates, location, violation text, penalty, case type, source party text, and dataset hash remain supporting evidence.

A complete no-match is research status only. It never proposes `environmental_violations = N`, and it never clears an existing positive value.

## Reliability

The CSV loader requires a recognizable company/regulated-party column and violation column. Each record receives a deterministic fingerprint ID. The full downloaded artifact receives SHA-256 provenance. Failed, malformed, blocked, or ambiguous results are not negatives.

The source is registered as `mn_pca` in the common V2 research pipeline.
