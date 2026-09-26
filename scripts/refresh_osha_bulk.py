"""Download DOL's complete OSHA inspection dataset and build the local search index."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.research.sources.osha_bulk import (  # noqa: E402
    DOL_OSHA_BULK_URL,
    refresh_official_bulk_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the local OSHA inspection index from the official U.S. Department "
            "of Labor complete-dataset download. The initial download is large; bidder "
            "research is local and fast after the index is built."
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
            "Use only for rebuilding/debugging; a normal refresh downloads the official file again."
        ),
    )
    parser.add_argument(
        "--delete-archive",
        action="store_true",
        help="Delete the downloaded ZIP after the SQLite index has been built successfully.",
    )
    args = parser.parse_args()

    if args.source_archive is not None and not args.source_archive.exists():
        parser.error(f"Source archive does not exist: {args.source_archive}")

    print(f"OSHA official bulk source: {DOL_OSHA_BULK_URL}")
    if args.source_archive is None:
        if args.reuse_cached_archive:
            print("Reusing the cached official OSHA ZIP and rebuilding the local index...")
        else:
            print("Downloading the current official complete dataset and rebuilding the local index...")
    else:
        print(f"Building the local index from: {args.source_archive}")

    metadata = refresh_official_bulk_index(
        source_archive=args.source_archive,
        force_download=args.source_archive is None and not args.reuse_cached_archive,
        keep_archive=not args.delete_archive,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    print("OSHA bulk index refresh complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
