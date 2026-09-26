"""Provision a real OSHA snapshot, run bidder research, and enforce OSHA-specific acceptance gates."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.research.sources.osha_operational import (  # noqa: E402
    default_operational_cache_dir,
    inspect_operational_snapshot,
    refresh_operational_osha_index,
)


REVIEW_STATUSES = {"AMBIGUOUS_MATCH", "MANUAL_REVIEW_REQUIRED"}
COMPLETE_SUCCESS_STATUSES = {
    "SUCCESS_COMPLETE",
    "SUCCESS_NO_MATCH",
    "SUCCESS_WITH_FINDINGS",
    *REVIEW_STATUSES,
}
FATAL_STATUSES = {
    "BLOCKED",
    "TIMEOUT",
    "HTTP_ERROR",
    "AUTH_REQUIRED",
    "SESSION_EXPIRED",
    "PARSER_FAILURE",
    "LAYOUT_CHANGED",
    "DATASET_MALFORMED",
    "PAGINATION_INCOMPLETE",
    "SOURCE_UNAVAILABLE",
    "NOT_CHECKED",
    "PARTIAL_RESULTS",
}


def _bidder_count(path: Path, limit: int) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        count = sum(1 for row in csv.DictReader(stream) if any(str(value or "").strip() for value in row.values()))
    return min(count, limit) if limit > 0 else count


def _latest_report() -> Path | None:
    path = ROOT / ".runtime" / "live-audits" / "latest.json"
    return path if path.exists() else None


def _acceptance_from_report(report: dict[str, Any], expected_bidders: int, raw_exit_code: int) -> dict[str, Any]:
    sources = [item for item in report.get("sources", []) if item.get("source") == "osha"]
    if len(sources) != 1:
        return {
            "passed": False,
            "raw_exit_code": raw_exit_code,
            "reasons": [f"Expected exactly one OSHA source result, found {len(sources)}."],
        }

    source = sources[0]
    checks = list(source.get("checks") or [])
    reasons: list[str] = []
    status_counts: dict[str, int] = {}
    completeness_counts: dict[str, int] = {}
    review_count = 0
    fatal_checks: list[dict[str, Any]] = []

    for check in checks:
        status = str(check.get("status") or "NOT_CHECKED")
        completeness = str(check.get("completeness_status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
        completeness_counts[completeness] = completeness_counts.get(completeness, 0) + 1
        if status in REVIEW_STATUSES:
            review_count += 1
        if status in FATAL_STATUSES or status not in COMPLETE_SUCCESS_STATUSES or completeness != "COMPLETE":
            fatal_checks.append(
                {
                    "contractor_name": check.get("contractor_name"),
                    "status": status,
                    "completeness_status": completeness,
                    "warnings": check.get("warnings") or [],
                }
            )

    if len(checks) != expected_bidders:
        reasons.append(f"Expected {expected_bidders} bidder checks, report contains {len(checks)}.")
    if fatal_checks:
        reasons.append(f"{len(fatal_checks)} bidder check(s) were failed, partial, incomplete, or not checked.")
    if source.get("status") in {"timed_out", "worker_failed", "failed", "missing_result"}:
        reasons.append(f"OSHA audit worker ended as {source.get('status')!r}.")
    if source.get("master_preserved") is not True:
        reasons.append("Master database preservation verification did not complete successfully.")

    acquisition_methods = sorted(
        {
            str(check.get("acquisition_method"))
            for check in checks
            if check.get("acquisition_method")
        }
    )
    if acquisition_methods != ["official_dol_bulk_index"]:
        reasons.append(
            "Not every OSHA task used the official DOL bulk index acquisition path: "
            + ", ".join(acquisition_methods or ["<missing>"])
        )

    return {
        "passed": not reasons,
        "raw_exit_code": raw_exit_code,
        "source_worker_status": source.get("status"),
        "expected_bidders": expected_bidders,
        "reported_checks": len(checks),
        "status_counts": status_counts,
        "completeness_counts": completeness_counts,
        "review_count": review_count,
        "fatal_checks": fatal_checks,
        "master_preserved": source.get("master_preserved"),
        "elapsed_seconds": source.get("elapsed_seconds"),
        "evidence_count": source.get("evidence_count"),
        "proposal_count": source.get("proposal_count"),
        "acquisition_methods": acquisition_methods,
        "reasons": reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bidder-csv", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="0 audits every imported bidder")
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds allowed for bidder execution after the OSHA snapshot is ready",
    )
    parser.add_argument(
        "--refresh",
        choices=("auto", "force", "skip"),
        default="auto",
        help=(
            "auto verifies/refreshes a missing or stale snapshot, force redownloads current data, "
            "skip uses the existing snapshot exactly as-is"
        ),
    )
    args = parser.parse_args()
    if not args.bidder_csv.exists():
        parser.error(f"Bidder CSV does not exist: {args.bidder_csv}")

    expected_bidders = _bidder_count(args.bidder_csv, args.limit)
    cache_dir = default_operational_cache_dir()
    before = inspect_operational_snapshot(cache_dir)
    preflight = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "refresh_mode": args.refresh,
        "cache_dir": str(cache_dir),
        "expected_bidders": expected_bidders,
        "before": before,
        "action": "reuse",
    }

    should_refresh = args.refresh == "force" or (
        args.refresh == "auto" and (not before["ready"] or before["stale"])
    )
    if should_refresh:
        preflight["action"] = "refresh"
        try:
            preflight["refresh_metadata"] = refresh_operational_osha_index(
                cache_dir=cache_dir,
                force_download=(args.refresh == "force"),
                keep_archive=True,
            )
        except Exception as exc:
            preflight["error"] = f"{type(exc).__name__}: {exc}"
            preflight["after"] = inspect_operational_snapshot(cache_dir)
            preflight["finished_at"] = datetime.now(timezone.utc).isoformat()
            output = ROOT / ".runtime" / "osha-preflight-latest.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(preflight, indent=2), encoding="utf-8")
            print(json.dumps(preflight, indent=2), file=sys.stderr)
            return 2

    after = inspect_operational_snapshot(cache_dir)
    preflight["after"] = after
    preflight["finished_at"] = datetime.now(timezone.utc).isoformat()
    output = ROOT / ".runtime" / "osha-preflight-latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    print("OSHA snapshot preflight:")
    print(json.dumps(preflight, indent=2))

    if not after["ready"]:
        print("OSHA snapshot is not ready; bidder audit was not started.", file=sys.stderr)
        return 2
    if after["stale"]:
        print(
            "OSHA snapshot is stale. Positive findings may still be usable, but clean no-matches must remain partial.",
            file=sys.stderr,
        )

    command = [
        sys.executable,
        str(ROOT / "scripts" / "live_audit.py"),
        "--source",
        "osha",
        "--bidder-csv",
        str(args.bidder_csv.resolve()),
        "--limit",
        str(args.limit),
        "--timeout",
        str(args.timeout),
    ]
    print("Starting isolated OSHA bidder audit after snapshot provisioning completed...")
    raw_exit_code = subprocess.call(command, cwd=ROOT)

    report_path = _latest_report()
    if report_path is None:
        print("OSHA audit did not produce .runtime/live-audits/latest.json", file=sys.stderr)
        return raw_exit_code or 3
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Unable to read OSHA audit report: {exc}", file=sys.stderr)
        return raw_exit_code or 3

    acceptance = _acceptance_from_report(report, expected_bidders, raw_exit_code)
    acceptance["report_path"] = str(report_path)
    acceptance["evaluated_at"] = datetime.now(timezone.utc).isoformat()
    acceptance_path = ROOT / ".runtime" / "osha-acceptance-latest.json"
    acceptance_path.write_text(json.dumps(acceptance, indent=2), encoding="utf-8")
    print("OSHA acceptance result:")
    print(json.dumps(acceptance, indent=2))

    # Generic live_audit intentionally returns nonzero for AMBIGUOUS_MATCH. OSHA's
    # source acceptance is different: a complete identity-review case means the data
    # was acquired safely and the human-review gate worked. Only acquisition/
    # completeness/master-preservation failures fail this wrapper.
    return 0 if acceptance["passed"] else (raw_exit_code or 4)


if __name__ == "__main__":
    raise SystemExit(main())