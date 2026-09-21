from __future__ import annotations

from pathlib import Path

from app.research.sources.sam_exclusions import load_cached_extract, store_uploaded_extract


FIXTURE = Path(__file__).parent / "fixtures" / "sam_exclusions_v2_sample.csv"


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_same_official_filename_never_overwrites_previous_bytes(tmp_path):
    cache_dir = tmp_path / "sam-cache"
    filename = "SAM_Exclusions_Public_Extract_V2_26263.CSV"
    first_bytes = _fixture_bytes()
    second_bytes = first_bytes.replace(b"ACME ELECTRIC", b"ACME POWER", 1)

    first = store_uploaded_extract(first_bytes, filename, cache_dir=cache_dir)
    second = store_uploaded_extract(second_bytes, filename, cache_dir=cache_dir)

    assert first.path != second.path
    assert first.path.read_bytes() == first_bytes
    assert second.path.read_bytes() == second_bytes
    assert first.sha256 != second.sha256


def test_cache_chooses_newest_official_extract_date_not_latest_upload(tmp_path):
    cache_dir = tmp_path / "sam-cache"
    newer = store_uploaded_extract(
        _fixture_bytes(),
        "SAM_Exclusions_Public_Extract_V2_26263.CSV",
        cache_dir=cache_dir,
    )
    # Upload an older extract afterwards. Filesystem recency must not make it active.
    store_uploaded_extract(
        _fixture_bytes(),
        "SAM_Exclusions_Public_Extract_V2_26250.CSV",
        cache_dir=cache_dir,
    )

    selected = load_cached_extract(cache_dir=cache_dir)

    assert selected is not None
    assert selected.extract_date == newer.extract_date
    assert selected.path.name == newer.path.name
    assert selected.sha256 == newer.sha256


def test_legacy_flat_cache_file_is_still_readable(tmp_path):
    cache_dir = tmp_path / "sam-cache"
    cache_dir.mkdir(parents=True)
    filename = "SAM_Exclusions_Public_Extract_V2_26263.CSV"
    legacy_path = cache_dir / filename
    legacy_path.write_bytes(_fixture_bytes())

    selected = load_cached_extract(cache_dir=cache_dir)

    assert selected is not None
    assert selected.path == legacy_path
    assert selected.extract_date is not None
    assert selected.extract_date.isoformat() == "2026-09-20"
