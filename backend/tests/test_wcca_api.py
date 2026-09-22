from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app.main_with_wcca import app


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    with TestClient(app) as client:
        yield client, tmp_path


def _load_bidder(tmp_path) -> int:
    db.replace_master_database(
        "master.csv",
        [
            "id",
            "contractor_name",
            "related_companies",
            "address_1",
            "city",
            "state",
            "zip",
            "circuit_court",
            "ccap_show150",
        ],
        [{
            "id": "42",
            "contractor_name": "ACME ELECTRIC, LLC",
            "related_companies": "ACME SERVICES LLC",
            "address_1": "123 Main St",
            "city": "Madison",
            "state": "WI",
            "zip": "53703",
            "circuit_court": "Y",
            "ccap_show150": "",
        }],
        str(tmp_path / "master.csv"),
    )
    return db.active_bidder_ids()[0]


def test_packaged_app_exposes_wcca_status(api_client):
    client, _ = api_client
    response = client.get("/api/sources/wcca/status")
    assert response.status_code == 200
    item = response.json()["item"]
    assert item["mode"] == "operator_assisted"
    assert item["automatic_public_scraping"] is False
    assert item["negative_field_updates"] is False


def test_wcca_plan_exposes_existing_master_values(api_client):
    client, tmp_path = api_client
    bidder_id = _load_bidder(tmp_path)
    response = client.get(f"/api/sources/wcca/plans?bidder_ids={bidder_id}")
    assert response.status_code == 200
    plan = response.json()["items"][0]
    assert plan["search_names"] == ["ACME ELECTRIC, LLC", "ACME SERVICES LLC"]
    assert plan["master_values"] == {"circuit_court": "Y", "ccap_show150": ""}


def test_public_no_match_does_not_contradict_existing_circuit_court_y(api_client):
    client, tmp_path = api_client
    bidder_id = _load_bidder(tmp_path)
    plan = client.get(f"/api/sources/wcca/plans?bidder_ids={bidder_id}").json()["items"][0]

    response = client.post(
        "/api/sources/wcca/result",
        json={
            "bidder_id": bidder_id,
            "searched_names": plan["search_names"],
            "outcome": "no_match",
            "cases": [],
            "operator_confirmed_complete": True,
            "identity_confirmed": False,
        },
    )
    assert response.status_code == 200
    item = response.json()["item"]
    assert item["status"] == "SUCCESS_NO_MATCH"
    assert item["comparison"]["current_value"] == "Y"
    assert item["comparison"]["observed_value"] is None
    assert item["comparison"]["different"] is False
    assert item["comparison"]["comparison_status"] == "public_no_match_not_master_negative"
    assert item["comparison"]["proposal_created"] is False

    with db.connect() as conn:
        records = conn.execute(
            """
            SELECT er.field_name, er.observed_value
            FROM evidence_records er
            JOIN evidence_snapshots es ON es.id = er.evidence_snapshot_id
            WHERE es.bidder_id=? AND es.source_key='wcca'
            ORDER BY er.id
            """,
            (bidder_id,),
        ).fetchall()
    assert [(row["field_name"], row["observed_value"]) for row in records] == [
        ("wcca_public_search", "NO_CURRENTLY_DISPLAYED_MATCH")
    ]
