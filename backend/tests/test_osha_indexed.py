from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import zipfile

import httpx

from app.research.sources.osha_bulk import INDEX_FILENAME, METADATA_FILENAME, build_bulk_index
from app.research.sources.osha_indexed import IndexedOshaEstablishmentSource, bounded_fts_queries


HEADER = "activity_nr,reporting_id,estab_name,site_address,site_city,site_state,site_zip,insp_type,insp_scope,sic_code,naics_code,open_date,mail_street,mail_city,mail_state,mail_zip,nr_violations"


def make_index(tmp_path: Path) -> None:
    rows = [
        "100000001,0521700,American Contractors and Associates LLC,100 Main St,Milwaukee,WI,53202,Planned,Complete,1542,236220,2025-01-01,100 Main St,Milwaukee,WI,53202,1",
    ]
    for number in range(40):
        rows.append(
            f"20000{number:04d},0521700,American Construction Services {number} LLC,{number} Broad St,Madison,WI,53703,Planned,Complete,1542,236220,2024-01-01,{number} Broad St,Madison,WI,53703,0"
        )
        rows.append(
            f"30000{number:04d},0521700,National Contractors Group {number} LLC,{number} Other St,Green Bay,WI,54301,Planned,Complete,1542,236220,2024-01-01,{number} Other St,Green Bay,WI,54301,0"
        )
    archive = tmp_path / "OSHA_inspection.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("osha_inspection_000.csv", HEADER + "\n" + "\n".join(rows) + "\n")
    build_bulk_index(
        archive,
        index_path=tmp_path / INDEX_FILENAME,
        metadata_path=tmp_path / METADATA_FILENAME,
        downloaded_at=datetime.now(timezone.utc),
        archive_sha256="fixture-sha",
    )


def no_network_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"indexed bidder lookup unexpectedly used network: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_multiword_query_plan_never_falls_back_to_generic_single_tokens():
    queries = bounded_fts_queries("american contractors and associates")

    assert queries[0] == '"american" AND "contractors" AND "associates"'
    assert '"contractors"' not in queries
    assert '"american"' not in queries
    assert '"associates"' not in queries
    assert len(queries) <= 7


def test_exact_multiword_bidder_uses_normalized_name_fast_path(tmp_path):
    make_index(tmp_path)
    source = IndexedOshaEstablishmentSource(cache_dir=tmp_path, client=no_network_client())
    source.prepare()

    rows, complete = source._query_candidates("AMERICAN CONTRACTORS AND ASSOCIATES LLC", "WI")

    assert complete is True
    assert [row.activity_number for row in rows] == ["100000001"]
    metric = source._query_diagnostics[("american contractors and associates", "WI")]
    assert metric["exact_fast_path"] is True
    assert metric["queries_executed"] == ["normalized_name_exact"]
    assert metric["raw_candidate_count"] == 1


def test_nonexact_multiword_lookup_stays_on_compound_queries(tmp_path):
    make_index(tmp_path)
    source = IndexedOshaEstablishmentSource(cache_dir=tmp_path, client=no_network_client())
    source.prepare()

    source._query_candidates("AMERICAN CONTRACTORS ASSOCIATES GROUP LLC", "WI")

    metric = source._query_diagnostics[("american contractors associates group", "WI")]
    assert metric["exact_fast_path"] is False
    assert metric["queries_executed"][0] == "normalized_name_exact"
    assert metric["queries_executed"][1] == '"american" AND "contractors" AND "associates" AND "group"'
    assert all(" AND " in query for query in metric["queries_executed"][2:])
    assert all(query != '"contractors"' for query in metric["queries_executed"])
