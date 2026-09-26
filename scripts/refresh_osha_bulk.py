"""Verify or refresh DOL's complete OSHA inspection snapshot and local search index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.research.sources.osha_bulk import DOL_OSHA_BULK_URL  # noqa: E402
from app.research.sources.osha_operational import (  # noqa: E402
    default_operational_cache_dir,
    inspect_operational_snapshot,
    refresh_operational_osha_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify/refresh the local OSHA inspection index from the official U.S. "
            "Department of Labor complete dataset. Existing snapshots are first checked "
            "with the remote ETag/Last-Modified identity, so an unchanged 1+ GiB ZIP is "
            "not downloaded again. Changed downloads are staged and promoted only after "
            "validation succeeds."
        )
    )
    parser.add_argument(
        "--source-archive",
        type=Path,
        help="Build from an operator-provided OSHA_inspection.zip instead of the live DOL object.",
    )
    parser.add_argument(
        "--reuse-cached-archive",
        action="store_true",
        help=(
            "Compatibility flag. Normal refresh already reuses the cached archive when "
            "the official remote ETag/Last-Modified proves it is unchanged."
        ),
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Redownload the official OSHA ZIP even when the cached remote identity still matches.",
    )
    parser.add_argument(
        "--delete-archive",
        action="store_true",
        help="Delete the downloaded ZIP after the SQLite index has been built successfully.",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Print the current OSHA snapshot/index status without changing anything.",
    )
    args = parser.parse_args()

    if args.source_archive is not None and not args.source_archive.exists():
        parser.error(f"Source archive does not exist: {args.source_archive}")
    if args.source_archive is not None and args.force_download:
        parser.error("--source-archive and --force-download cannot be combined")

    cache_dir = default_operational_cache_dir()
    if args.status_only:
        print(json.dumps(inspect_operational_snapshot(cache_dir), indent=2, sort_keys=True))
        return 0

    print(f"OSHA official bulk source: {DOL_OSHA_BULK_URL}")
    print(f"Operational OSHA cache: {cache_dir}")
    if args.source_archive is None:
        if args.force_download:
            print("Forcing a new official complete-dataset download and validated index rebuild...")
        else:
            print(
                "Checking the official remote ETag/Last-Modified first; the large ZIP is "
                "downloaded only when missing or changed..."
            )
    else:
        print(f"Building the local index from operator-provided archive: {args.source_archive}")

    metadata = refresh_operational_osha_index(
        cache_dir=cache_dir,
        source_archive=args.source_archive,
        force_download=bool(args.force_download),
        keep_archive=not args.delete_archive,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("OSHA snapshot/index verification complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())