from __future__ import annotations

import hashlib
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = BASE_DIR / "data" / "evidence"


def store_artifact(*, source_key: str, research_run_id: int, bidder_id: int, filename: str, data: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename) or "artifact.bin"
    directory = ARTIFACT_DIR / source_key / str(research_run_id) / str(bidder_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest[:16]}_{safe_name}"
    if not path.exists():
        path.write_bytes(data)
    relative = path.relative_to(BASE_DIR).as_posix()
    return relative, digest


def verify_artifact(relative_path: str, expected_sha256: str) -> bool:
    path = BASE_DIR / relative_path
    if not path.exists() or not path.is_file():
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest == expected_sha256
