"""Tests for the conditional zone update module (/zones, /zones/{id}/updates)."""
from concurrent.futures import ThreadPoolExecutor

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


# ---------------------------------------------------------------------------
# POST /zones and GET /zones/{id}
# ---------------------------------------------------------------------------


def test_create_returns_id_and_version_zero(client):
    body = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    assert body["id"]
    assert body["version"] == 0
    assert body["zone"] == "example.com."
    assert body["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"}
    ]


def test_create_normalizes_and_deduplicates_records(client):
    body = create(
        client,
        zone="Example.COM",
        records=[
            {"name": "WWW.Example.com", "type": "a", "value": "192.0.2.1"},
            {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
            {"name": "alias.example.com", "type": "CNAME", "value": "WWW.Example.COM"},
        ],
    )
    assert body["zone"] == "example.com."
    assert body["records"] == [
        {"name": "alias.example.com.", "type": "CNAME", "value": "www.example.com."},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
    ]


def test_get_zone_returns_snapshot(client):
    created = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    r = client.get(f"/zones/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created


def test_get_unknown_zone_returns_404(client):
    assert client.get("/zones/no-such-zone").status_code == 404


def test_create_with_invalid_zone_returns_422(client):
    r = client.post(
        "/zones",
        json={
            "zone": "example.com",
            "records": [
                {"name": "example.com", "type": "NS", "value": "ns1.example.net"}
            ],
        },
    )
    assert r.status_code == 422


def test_create_with_invalid_record_returns_422(client):
    r = client.post(
        "/zones",
        json={
            "zone": "example.com",
            "records": [{"name": "x.example.com", "type": "A", "value": "999.1.1.1"}],
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /zones/{id}/updates
# ---------------------------------------------------------------------------


def test_update_add_delete_and_delete_set(client):
    z = create(
        client,
        records=[
            {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
            {"name": "old.example.com", "type": "A", "value": "192.0.2.9"},
            {"name": "old.example.com", "type": "A", "value": "192.0.2.10"},
        ],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.2"},
            {"op": "delete", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
            {"op": "delete_set", "name": "old.example.com", "type": "A"},
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"}
    ]
    # the returned snapshot is persisted
    assert client.get(f"/zones/{z['id']}").json() == body


def test_operations_are_applied_in_order(client):
    z = create(client)
    # delete_set first, then add: the added record survives
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "delete_set", "name": "www.example.com", "type": "A"},
            {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
        ],
    )
    assert r.status_code == 200
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"}
    ]
    # add first, then delete_set: nothing survives
    r = update(
        client,
        z["id"],
        1,
        operations=[
            {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.2"},
            {"op": "delete_set", "name": "www.example.com", "type": "A"},
        ],
    )
    assert r.status_code == 200
    assert r.json()["records"] == []


def test_duplicate_add_and_missing_delete_are_noops(client):
    z = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
            {"op": "delete", "name": "www.example.com", "type": "A", "value": "192.0.2.99"},
            {"op": "delete_set", "name": "ghost.example.com", "type": "A"},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["records"] == z["records"]


def test_version_conflict_returns_409_and_leaves_state_untouched(client):
    z = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    r = update(
        client,
        z["id"],
        3,
        operations=[
            {"op": "add", "name": "a.example.com", "type": "A", "value": "192.0.2.2"}
        ],
    )
    assert r.status_code == 409
    snap = client.get(f"/zones/{z['id']}").json()
    assert snap["version"] == 0
    assert snap["records"] == z["records"]


def test_same_version_second_update_is_rejected(client):
    z = create(client)
    payload_op = {"op": "add", "name": "a.example.com", "type": "A", "value": "192.0.2.2"}
    assert update(client, z["id"], 0, operations=[payload_op]).status_code == 200
    assert update(client, z["id"], 0, operations=[payload_op]).status_code == 409


def test_prerequisites_checked_against_pre_update_snapshot(client):
    z = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    r = update(
        client,
        z["id"],
        0,
        prerequisites=[
            # names/types are normalized; values are deduplicated sets
            {"name": "WWW.example.com", "type": "a", "values": ["192.0.2.1", "192.0.2.1"]},
            # empty set: the record set must not exist
            {"name": "ghost.example.com", "type": "A", "values": []},
        ],
        operations=[
            {"op": "delete", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
            {"op": "add", "name": "www.example.com", "type": "A", "value": "192.0.2.2"},
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"}
    ]


def test_prerequisite_mismatch_returns_409_and_leaves_state_untouched(client):
    z = create(
        client,
        records=[{"name": "www.example.com", "type": "A", "value": "192.0.2.1"}],
    )
    failing = [
        # record set exists but the prerequisite demands it absent
        {"name": "www.example.com", "type": "A", "values": []},
        # values differ
        {"name": "www.example.com", "type": "A", "values": ["192.0.2.2"]},
        # superset of the stored values
        {"name": "www.example.com", "type": "A", "values": ["192.0.2.1", "192.0.2.2"]},
    ]
    for prereq in failing:
        r = update(
            client,
            z["id"],
            0,
            prerequisites=[prereq],
            operations=[
                {"op": "add", "name": "a.example.com", "type": "A", "value": "192.0.2.5"}
            ],
        )
        assert r.status_code == 409, prereq
    snap = client.get(f"/zones/{z['id']}").json()
    assert snap["version"] == 0
    assert snap["records"] == z["records"]


def test_prerequisite_normalizes_wildcard_and_ns_values(client):
    z = create(
        client,
        records=[
            {"name": "*.wild.example.com", "type": "A", "value": "203.0.113.9"},
            {"name": "del.example.com", "type": "NS", "value": "NS1.Example.NET"},
        ],
    )
    assert {r["value"] for r in z["records"]} == {"203.0.113.9", "ns1.example.net."}
    r = update(
        client,
        z["id"],
        0,
        prerequisites=[
            {"name": "*.WILD.example.com", "type": "A", "values": ["203.0.113.9"]},
            {"name": "del.example.com", "type": "ns", "values": ["ns1.example.NET."]},
        ],
        operations=[{"op": "delete_set", "name": "*.wild.example.com", "type": "A"}],
    )
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "del.example.com.", "type": "NS", "value": "ns1.example.net."}
    ]


def test_intermediate_conflict_is_allowed(client):
    z = create(
        client,
        records=[
            {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
            {"name": "x.example.com", "type": "A", "value": "192.0.2.5"},
        ],
    )
    # after the first operation x.example.com holds both A and CNAME, which is
    # illegal; the second operation fixes it, so the final zone is valid.
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "add", "name": "x.example.com", "type": "CNAME", "value": "www.example.com"},
            {"op": "delete", "name": "x.example.com", "type": "A", "value": "192.0.2.5"},
        ],
    )
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
        {"name": "x.example.com.", "type": "CNAME", "value": "www.example.com."},
    ]


def test_final_zone_invalid_returns_422_and_leaves_state_untouched(client):
    z = create(
        client,
        records=[{"name": "x.example.com", "type": "A", "value": "192.0.2.5"}],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "add", "name": "x.example.com", "type": "CNAME", "value": "x.example.com"}
        ],
    )
    assert r.status_code == 422
    snap = client.get(f"/zones/{z['id']}").json()
    assert snap["version"] == 0
    assert snap["records"] == z["records"]


def test_final_nested_delegation_returns_422(client):
    z = create(
        client,
        records=[
            {"name": "outer.example.com", "type": "NS", "value": "ns1.example.net"}
        ],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {
                "op": "add",
                "name": "inner.outer.example.com",
                "type": "NS",
                "value": "ns2.example.net",
            }
        ],
    )
    assert r.status_code == 422
    assert client.get(f"/zones/{z['id']}").json()["version"] == 0


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "add", "name": "www.example.com", "type": "A", "value": "999.1.1.1"},
        {"op": "add", "name": "www.example.com", "type": "TXT", "value": "hello"},
        {"op": "add", "name": "www.example.com", "type": "A"},  # missing value
        {"op": "replace", "name": "www.example.com", "type": "A", "value": "192.0.2.1"},
        {"op": "add", "name": "bad name", "type": "A", "value": "192.0.2.1"},
        {"op": "add", "name": "other.org", "type": "A", "value": "192.0.2.1"},
    ],
)
def test_invalid_operation_returns_422(client, operation):
    z = create(client)
    r = update(client, z["id"], 0, operations=[operation])
    assert r.status_code == 422
    assert client.get(f"/zones/{z['id']}").json()["version"] == 0


def test_invalid_prerequisite_returns_422(client):
    z = create(client)
    r = update(
        client,
        z["id"],
        0,
        prerequisites=[{"name": "www.example.com", "type": "AAAA", "values": []}],
    )
    assert r.status_code == 422


def test_negative_expected_version_returns_422(client):
    z = create(client)
    assert update(client, z["id"], -1).status_code == 422


def test_update_unknown_zone_returns_404(client):
    assert update(client, "no-such-zone", 0).status_code == 404


# ---------------------------------------------------------------------------
# Delegation subtrees and interplay with /resolve
# ---------------------------------------------------------------------------

DELEGATION_RECORDS = [
    {"name": "del.example.com", "type": "NS", "value": "ns1.del.example.com"},
    {"name": "ns1.del.example.com", "type": "A", "value": "198.51.100.1"},
    # inert below the cut: kept in the snapshot, never authoritative
    {"name": "host.del.example.com", "type": "A", "value": "198.51.100.77"},
]


def test_delegation_subtree_records_are_preserved(client):
    z = create(client, records=DELEGATION_RECORDS)
    names = {r["name"] for r in z["records"]}
    assert names == {
        "del.example.com.",
        "ns1.del.example.com.",
        "host.del.example.com.",
    }
    # the snapshot records can be fed straight into /resolve
    r = client.post(
        "/resolve",
        json={
            "zone": z["zone"],
            "records": z["records"],
            "query": "host.del.example.com",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "REFERRAL"


def test_adding_record_below_delegation_cut_is_kept(client):
    z = create(
        client,
        records=[{"name": "del.example.com", "type": "NS", "value": "ns1.example.net"}],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "add", "name": "host.del.example.com", "type": "A", "value": "198.51.100.7"}
        ],
    )
    assert r.status_code == 200
    assert {r_["name"] for r_ in r.json()["records"]} == {
        "del.example.com.",
        "host.del.example.com.",
    }


def test_removing_delegation_exposes_subtree_records(client):
    z = create(client, records=DELEGATION_RECORDS)
    r = update(
        client,
        z["id"],
        0,
        operations=[{"op": "delete_set", "name": "del.example.com", "type": "NS"}],
    )
    assert r.status_code == 200
    body = r.json()
    r = client.post(
        "/resolve",
        json={
            "zone": body["zone"],
            "records": body["records"],
            "query": "host.del.example.com",
        },
    )
    assert r.json()["status"] == "NOERROR"
    assert r.json()["addresses"] == ["198.51.100.77"]


# ---------------------------------------------------------------------------
# Concurrency: at most one update per version may succeed
# ---------------------------------------------------------------------------


def test_concurrent_same_version_at_most_one_success(tmp_path, monkeypatch):
    monkeypatch.setenv("DNS_DB_PATH", str(tmp_path / "zones.db"))
    zone_store.reset_store()
    try:
        store = zone_store.get_store()
        snap = store.create("example.com.", {("www.example.com.", "A", "192.0.2.1")})

        outcomes = []

        def worker(i):
            try:
                store.apply_update(
                    snap.id,
                    0,
                    [],
                    [("add", f"h{i}.example.com.", "A", f"192.0.2.{i + 2}")],
                )
            except zone_store.VersionConflict:
                outcomes.append("conflict")
            else:
                outcomes.append("ok")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))

        assert outcomes.count("ok") == 1
        assert outcomes.count("conflict") == 7
        final = store.get(snap.id)
        assert final.version == 1
        assert len(final.records) == 2  # original record + exactly one added
    finally:
        zone_store.reset_store()
