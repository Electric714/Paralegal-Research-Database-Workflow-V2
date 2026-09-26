"""Run a real OSHA audit with source provisioning outside the per-source timeout."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.research.sources.osha_operational import (  # noqa: E402
    default_operational_cache_dir,
    inspect_operational_snapshot,
    refresh_operational_osha_index,
)


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
            "auto refreshes a missing/stale snapshot, force always downloads current data, "
            "skip uses the existing snapshot exactly as-is"
        ),
    )
    args = parser.parse_args()
    if not args.bidder_csv.exists():
        parser.error(f"Bidder CSV does not exist: {args.bidder_csv}")

    cache_dir = default_operational_cache_dir()
    before = inspect_operational_snapshot(cache_dir)
    preflight = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "refresh_mode": args.refresh,
        "cache_dir": str(cache_dir),
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
                force_download=True,
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
            "OSHA snapshot is stale. The audit may still surface positive findings, but clean no-matches will remain partial.",
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
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())