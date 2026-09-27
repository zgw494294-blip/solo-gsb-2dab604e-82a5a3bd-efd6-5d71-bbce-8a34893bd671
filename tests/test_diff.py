"""Tests for version history archiving and GET /zones/{id}/diff."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import store as zone_store
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DNS_DB_PATH", str(tmp_path / "zones.db"))
    zone_store.reset_store()
    with TestClient(app) as c:
        yield c
    zone_store.reset_store()


def create(client, zone="example.com", records=None):
    r = client.post("/zones", json={"zone": zone, "records": records or []})
    assert r.status_code == 200, r.text
    return r.json()


def update(client, zone_id, expected_version, operations=(), prerequisites=()):
    return client.post(
        f"/zones/{zone_id}/updates",
        json={
            "expected_version": expected_version,
            "prerequisites": list(prerequisites),
            "operations": list(operations),
        },
    )


def diff(client, zone_id, from_version, to_version):
    return client.get(
        f"/zones/{zone_id}/diff",
        params={"from_version": from_version, "to_version": to_version},
    )


def record(name, rtype, value):
    return {"name": name, "type": rtype, "value": value}


# ---------------------------------------------------------------------------
# Basic diff semantics
# ---------------------------------------------------------------------------


def test_create_archives_version_zero_and_same_version_diff_is_empty(client):
    z = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    r = diff(client, z["id"], 0, 0)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "zone": "example.com.",
        "from_version": 0,
        "to_version": 0,
        "removed": [],
        "added": [],
    }


def test_empty_zone_version_zero_is_archived(client):
    # an archived version with an empty record set must still be queryable
    z = create(client)
    r = diff(client, z["id"], 0, 0)
    assert r.status_code == 200
    assert r.json()["removed"] == []
    assert r.json()["added"] == []


def test_diff_after_update_lists_removed_and_added(client):
    z = create(
        client,
        records=[
            {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
            {"name": "gone.example.com", "type": "A", "value": "192.0.2.9"},
        ],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "delete", "name": "gone.example.com", "type": "A", "value": "192.0.2.9"},
            {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.2"},
        ],
    )
    assert r.status_code == 200, r.text
    r = diff(client, z["id"], 0, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone"] == "example.com."
    assert body["from_version"] == 0
    assert body["to_version"] == 1
    assert body["removed"] == [
        record("gone.example.com.", "A", "192.0.2.9")
    ]
    assert body["added"] == [
        record("www.example.com.", "A", "192.0.2.2")
    ]


def test_diff_results_are_sorted_by_name_type_value(client):
    z = create(
        client,
        records=[
            {"name": "zebra.example.com", "type": "A", "value": "192.0.2.9"},
            {"name": "alpha.example.com", "type": "A", "value": "192.0.2.8"},
            {"name": "keep.example.com", "type": "A", "value": "192.0.2.100"},
            {"name": "alpha.example.com", "type": "A", "value": "192.0.2.1"},
        ],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "delete_set", "name": "alpha.example.com", "type": "A"},
            {"op": "delete", "name": "zebra.example.com", "type": "A", "value": "192.0.2.9"},
            {"op": "add", "name": "mid.example.com", "type": "A", "value": "192.0.2.30"},
            {"op": "add", "name": "mid.example.com", "type": "A", "value": "192.0.2.2"},
        ],
    )
    assert r.status_code == 200, r.text
    body = diff(client, z["id"], 0, 1).json()
    assert body["removed"] == [
        record("alpha.example.com.", "A", "192.0.2.1"),
        record("alpha.example.com.", "A", "192.0.2.8"),
        record("zebra.example.com.", "A", "192.0.2.9"),
    ]
    assert body["added"] == [
        record("mid.example.com.", "A", "192.0.2.2"),
        record("mid.example.com.", "A", "192.0.2.30"),
    ]
    # records unchanged between the two versions appear on neither side
    assert all(rec["name"] != "keep.example.com." for rec in body["removed"] + body["added"])


def test_diff_across_multiple_versions_compares_endpoint_sets(client):
    z = create(
        client,
        records=[{"name": "a.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    update(
        client, z["id"], 0,
        operations=[
            {"op": "add", "name": "b.example.com", "type": "A", "value": "192.0.2.2"}
        ],
    )
    update(
        client, z["id"], 1,
        operations=[
            {"op": "delete", "name": "a.example.com", "type": "A", "value": "192.0.2.1"},
            {"op": "add", "name": "c.example.com", "type": "A", "value": "192.0.2.3"},
        ],
    )
    body = diff(client, z["id"], 0, 2).json()
    assert body["removed"] == [record("a.example.com.", "A", "192.0.2.1")]
    assert body["added"] == [
        record("b.example.com.", "A", "192.0.2.2"),
        record("c.example.com.", "A", "192.0.2.3"),
    ]
    # a later version compared against itself is an empty diff
    body = diff(client, z["id"], 2, 2).json()
    assert body["removed"] == []
    assert body["added"] == []
    # and the intermediate version remains individually queryable
    body = diff(client, z["id"], 0, 1).json()
    assert body["added"] == [record("b.example.com.", "A", "192.0.2.2")]
    assert body["removed"] == []


# ---------------------------------------------------------------------------
# Delegation subtrees participate in diffs
# ---------------------------------------------------------------------------


def test_delegation_subtree_records_participate_in_diff(client):
    z = create(
        client,
        records=[
            {"name": "del.example.com", "type": "NS", "value": "ns1.del.example.com"},
            {"name": "ns1.del.example.com", "type": "A", "value": "198.51.100.1"},
            {"name": "host.del.example.com", "type": "A", "value": "198.51.100.77"},
        ],
    )
    r = update(
        client, z["id"], 0,
        operations=[
            {"op": "add", "name": "host.del.example.com", "type": "A", "value": "198.51.100.78"},
            {"op": "delete_set", "name": "del.example.com", "type": "NS"},
        ],
    )
    assert r.status_code == 200, r.text
    body = diff(client, z["id"], 0, 1).json()
    assert body["removed"] == [
        record("del.example.com.", "NS", "ns1.del.example.com."),
    ]
    assert body["added"] == [
        record("host.del.example.com.", "A", "198.51.100.78"),
    ]
    # glue and inert subtree records were present at both endpoints
    present = {
        (r_["name"], r_["type"], r_["value"])
        for r_ in body["removed"] + body["added"]
    }
    assert ("ns1.del.example.com.", "A", "198.51.100.1") not in present
    assert ("host.del.example.com.", "A", "198.51.100.77") not in present


# ---------------------------------------------------------------------------
# Failed updates never leave history
# ---------------------------------------------------------------------------


def test_failed_updates_leave_no_history(client):
    z = create(
        client,
        records=[{"name": "x.example.com", "type": "A", "value": "192.0.2.5"}],
    )
    # 409: version mismatch
    r = update(
        client, z["id"], 3,
        operations=[
            {"op": "add", "name": "a.example.com", "type": "A", "value": "192.0.2.2"}
        ],
    )
    assert r.status_code == 409
    # 409: prerequisite failure
    r = update(
        client, z["id"], 0,
        prerequisites=[{"name": "ghost.example.com", "type": "A", "values": ["1.2.3.4"]}],
        operations=[
            {"op": "add", "name": "a.example.com", "type": "A", "value": "192.0.2.2"}
        ],
    )
    assert r.status_code == 409
    # 422: final zone invalid (CNAME + A coexist)
    r = update(
        client, z["id"], 0,
        operations=[
            {"op": "add", "name": "x.example.com", "type": "CNAME", "value": "x.example.com"}
        ],
    )
    assert r.status_code == 422
    # version 1 was never archived
    assert diff(client, z["id"], 0, 1).status_code == 404
    assert diff(client, z["id"], 1, 1).status_code == 404
    # ...and the zone itself is still at version 0
    snap = client.get(f"/zones/{z['id']}").json()
    assert snap["version"] == 0


# ---------------------------------------------------------------------------
# Error handling: 404 and 422
# ---------------------------------------------------------------------------


def test_diff_unknown_zone_returns_404(client):
    assert diff(client, "no-such-zone", 0, 0).status_code == 404


def test_diff_unarchived_version_returns_404(client):
    z = create(client)
    assert diff(client, z["id"], 0, 1).status_code == 404
    assert diff(client, z["id"], 1, 1).status_code == 404
    assert diff(client, z["id"], 99, 99).status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"from_version": -1, "to_version": 0},
        {"from_version": 0, "to_version": -1},
        {"from_version": 2, "to_version": 1},
        {"from_version": "abc", "to_version": 0},
        {"to_version": 0},  # missing from_version
        {"from_version": 0},  # missing to_version
    ],
)
def test_diff_invalid_params_return_422(client, params):
    z = create(client)
    assert client.get(f"/zones/{z['id']}/diff", params=params).status_code == 422


def test_diff_param_ordering_error_reports_422_even_for_missing_zone(client):
    # validation of the from/to relationship happens before zone lookup
    r = client.get(
        "/zones/no-such-zone/diff",
        params={"from_version": 2, "to_version": 1},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Persistence across restarts
# ---------------------------------------------------------------------------


def test_history_survives_store_restart(client):
    z = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    r = update(
        client, z["id"], 0,
        operations=[
            {"op": "add", "name": "a.example.com", "type": "A", "value": "192.0.2.2"}
        ],
    )
    assert r.status_code == 200
    # simulate a process restart: drop the cached connection and reopen
    # the same SQLite file
    zone_store.reset_store()
    zone_store.get_store()
    r = diff(client, z["id"], 0, 1)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == [record("a.example.com.", "A", "192.0.2.2")]
    assert r.json()["removed"] == []
    # older archived versions remain available too
    assert diff(client, z["id"], 0, 0).status_code == 200


# ---------------------------------------------------------------------------
# Migration for zones created before history tracking
# ---------------------------------------------------------------------------


def test_preexisting_zone_gets_baseline_at_current_version(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE zones (
                id TEXT PRIMARY KEY,
                zone TEXT NOT NULL,
                version INTEGER NOT NULL
            );
            CREATE TABLE zone_records (
                zone_id TEXT NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE (zone_id, name, type, value)
            );
            INSERT INTO zones (id, zone, version) VALUES ('z1', 'example.com.', 2);
            INSERT INTO zone_records (zone_id, name, type, value)
                VALUES ('z1', 'www.example.com.', 'A', '192.0.2.1');
            -- a preexisting empty zone as well
            INSERT INTO zones (id, zone, version) VALUES ('z2', 'empty.example.com.', 5);
            """
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("DNS_DB_PATH", str(db))
    zone_store.reset_store()
    try:
        with TestClient(app) as c:
            # baseline archived at the current version, same-version diff empty
            r = c.get("/zones/z1/diff", params={"from_version": 2, "to_version": 2})
            assert r.status_code == 200
            assert r.json() == {
                "zone": "example.com.",
                "from_version": 2,
                "to_version": 2,
                "removed": [],
                "added": [],
            }
            # earlier versions are not reconstructed
            assert c.get(
                "/zones/z1/diff",
                params={"from_version": 0, "to_version": 2},
            ).status_code == 404
            assert c.get(
                "/zones/z1/diff",
                params={"from_version": 1, "to_version": 1},
            ).status_code == 404
            # the empty legacy zone also got a baseline marker
            r = c.get("/zones/z2/diff", params={"from_version": 5, "to_version": 5})
            assert r.status_code == 200
            assert r.json()["removed"] == []
            assert r.json()["added"] == []
            # a successful update archives the next version normally
            r = c.post(
                "/zones/z1/updates",
                json={
                    "expected_version": 2,
                    "prerequisites": [],
                    "operations": [
                        {
                            "op": "add",
                            "name": "new.example.com",
                            "type": "A",
                            "value": "192.0.2.3",
                        }
                    ],
                },
            )
            assert r.status_code == 200, r.text
            body = c.get(
                "/zones/z1/diff",
                params={"from_version": 2, "to_version": 3},
            ).json()
            assert body["added"] == [record("new.example.com.", "A", "192.0.2.3")]
            assert body["removed"] == []
            # baseline-vs-current still refuses the fabricated early version
            assert c.get(
                "/zones/z1/diff",
                params={"from_version": 0, "to_version": 3},
            ).status_code == 404
    finally:
        zone_store.reset_store()
