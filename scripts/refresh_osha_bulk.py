"""Download DOL's complete OSHA inspection dataset and build the local search index."""
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
            "Refresh the local OSHA inspection index from the official U.S. Department "
            "of Labor complete-dataset download. The download/index is staged and only "
            "promoted after validation succeeds."
        )
    )
    parser.add_argument(
        "--source-archive",
        type=Path,
        help="Build from an already-downloaded OSHA_inspection.zip instead of downloading it.",
    )
    parser.add_argument(
        "--reuse-cached-archive",
        action="store_true",
        help=(
            "Reuse the cached OSHA ZIP instead of downloading a current snapshot. "
            "This rebuilds the index but preserves the archive's original freshness timestamp."
        ),
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Explicitly download a current official OSHA snapshot (normal refresh behavior).",
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
    if args.source_archive is not None and args.reuse_cached_archive:
        parser.error("--source-archive and --reuse-cached-archive cannot be combined")

    cache_dir = default_operational_cache_dir()
    if args.status_only:
        print(json.dumps(inspect_operational_snapshot(cache_dir), indent=2, sort_keys=True))
        return 0

    print(f"OSHA official bulk source: {DOL_OSHA_BULK_URL}")
    print(f"Operational OSHA cache: {cache_dir}")
    if args.source_archive is None:
        if args.reuse_cached_archive:
            print("Rebuilding from the cached OSHA ZIP without changing its freshness date...")
        else:
            print("Downloading a current official complete dataset, validating it, and rebuilding the local index...")
    else:
        print(f"Building the local index from: {args.source_archive}")

    metadata = refresh_operational_osha_index(
        cache_dir=cache_dir,
        source_archive=args.source_archive,
        force_download=(
            args.source_archive is None
            and (args.force_download or not args.reuse_cached_archive)
        ),
        keep_archive=not args.delete_archive,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("OSHA bulk index refresh complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())