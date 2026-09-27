"""Tests for POST /zones/{id}/impact: probe evaluation across two archived versions."""
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


def impact(client, zone_id, from_version, to_version, probes):
    return client.post(
        f"/zones/{zone_id}/impact",
        json={
            "from_version": from_version,
            "to_version": to_version,
            "probes": probes,
        },
    )


def rec(name, rtype, value):
    return {"name": name, "type": rtype, "value": value}


def add(name, rtype, value):
    return {"op": "add", "name": name, "type": rtype, "value": value}


def delete(name, rtype, value):
    return {"op": "delete", "name": name, "type": rtype, "value": value}


def forward(status, chain, addresses, delegation=None, nameservers=(), glue=(), error=None):
    return {
        "status": status,
        "chain": chain,
        "addresses": addresses,
        "delegation": delegation,
        "nameservers": list(nameservers),
        "glue": list(glue),
        "error": error,
    }


# ---------------------------------------------------------------------------
# Basic semantics
# ---------------------------------------------------------------------------


def test_a_probe_reports_before_after_and_changed(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    r = update(
        client, z["id"], 0,
        operations=[
            delete("www.example.com", "A", "192.0.2.1"),
            add("www.example.com", "A", "192.0.2.2"),
        ],
    )
    assert r.status_code == 200, r.text
    r = impact(client, z["id"], 0, 1, [{"type": "A", "name": "WWW.Example.com"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == z["id"]
    assert body["zone"] == "example.com."
    assert body["from_version"] == 0
    assert body["to_version"] == 1
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert item["type"] == "A"
    assert item["query"] == "www.example.com."  # normalized query name
    assert item["before"] == forward("NOERROR", ["www.example.com."], ["192.0.2.1"])
    assert item["after"] == forward("NOERROR", ["www.example.com."], ["192.0.2.2"])
    assert item["changed"] is True


def test_unchanged_probe_reports_changed_false(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    r = update(
        client, z["id"], 0,
        operations=[add("other.example.com", "A", "192.0.2.9")],
    )
    assert r.status_code == 200, r.text
    body = impact(client, z["id"], 0, 1, [{"type": "A", "name": "www.example.com"}]).json()
    item = body["results"][0]
    assert item["before"] == item["after"]
    assert item["changed"] is False


def test_probe_order_and_duplicates_are_preserved(client):
    z = create(
        client,
        records=[
            rec("www.example.com", "A", "192.0.2.1"),
            rec("alias.example.com", "CNAME", "www.example.com"),
        ],
    )
    update(
        client, z["id"], 0,
        operations=[add("www.example.com", "A", "192.0.2.2")],
    )
    probes = [
        {"type": "A", "name": "www.example.com"},
        {"type": "A", "name": "alias.example.com"},
        {"type": "A", "name": "www.example.com"},  # duplicate kept
    ]
    body = impact(client, z["id"], 0, 1, probes).json()
    queries = [item["query"] for item in body["results"]]
    assert queries == [
        "www.example.com.",
        "alias.example.com.",
        "www.example.com.",
    ]
    assert body["results"][0] == body["results"][2]
    assert body["results"][0]["changed"] is True
    # the alias record itself is unchanged, but its CNAME target gained an address
    assert body["results"][1]["changed"] is True


def test_empty_probe_list_is_allowed(client):
    z = create(client)
    body = impact(client, z["id"], 0, 0, []).json()
    assert body["results"] == []


def test_same_version_never_changes(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    body = impact(
        client, z["id"], 0, 0,
        [
            {"type": "A", "name": "www.example.com"},
            {"type": "A", "name": "missing.example.com"},
        ],
    ).json()
    assert [item["changed"] for item in body["results"]] == [False, False]
    assert body["results"][0]["before"] == body["results"][0]["after"]


def test_reversed_version_order_is_allowed(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    update(
        client, z["id"], 0,
        operations=[
            delete("www.example.com", "A", "192.0.2.1"),
            add("www.example.com", "A", "192.0.2.2"),
        ],
    )
    body = impact(
        client, z["id"], 1, 0, [{"type": "A", "name": "www.example.com"}]
    ).json()
    assert body["from_version"] == 1
    assert body["to_version"] == 0
    item = body["results"][0]
    assert item["before"]["addresses"] == ["192.0.2.2"]
    assert item["after"]["addresses"] == ["192.0.2.1"]
    assert item["changed"] is True


def test_probe_type_is_case_insensitive(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    r = impact(client, z["id"], 0, 0, [{"type": "a", "name": "www.example.com"}])
    assert r.status_code == 200
    assert r.json()["results"][0]["type"] == "A"


# ---------------------------------------------------------------------------
# Indirect changes: CNAME, wildcard, empty non-terminals, delegation
# ---------------------------------------------------------------------------


def test_cname_chain_change_counts_even_with_same_final_address(client):
    z = create(
        client,
        records=[
            rec("alias.example.com", "CNAME", "www.example.com"),
            rec("www.example.com", "A", "192.0.2.1"),
            rec("other.example.com", "A", "192.0.2.1"),
        ],
    )
    update(
        client, z["id"], 0,
        operations=[
            delete("alias.example.com", "CNAME", "www.example.com"),
            add("alias.example.com", "CNAME", "other.example.com"),
        ],
    )
    item = impact(client, z["id"], 0, 1, [{"type": "A", "name": "alias.example.com"}]).json()[
        "results"
    ][0]
    assert item["before"]["chain"] == ["alias.example.com.", "www.example.com."]
    assert item["after"]["chain"] == ["alias.example.com.", "other.example.com."]
    # same final address, but the chain differs
    assert item["before"]["addresses"] == item["after"]["addresses"] == ["192.0.2.1"]
    assert item["changed"] is True


def test_wildcard_addition_changes_unmatched_names(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    update(
        client, z["id"], 0,
        operations=[add("*.example.com", "A", "203.0.113.9")],
    )
    item = impact(client, z["id"], 0, 1, [{"type": "A", "name": "stray.example.com"}]).json()[
        "results"
    ][0]
    assert item["before"]["status"] == "NXDOMAIN"
    assert item["after"] == forward("NOERROR", ["stray.example.com."], ["203.0.113.9"])
    assert item["changed"] is True


def test_empty_nonterminal_blocks_wildcard(client):
    z = create(client, records=[rec("*.example.com", "A", "203.0.113.9")])
    update(
        client, z["id"], 0,
        operations=[add("x.a.b.example.com", "A", "192.0.2.50")],
    )
    # no record was ever added at a.b.example.com itself: it became an empty
    # non-terminal, which blocks the wildcard
    item = impact(client, z["id"], 0, 1, [{"type": "A", "name": "a.b.example.com"}]).json()[
        "results"
    ][0]
    assert item["before"] == forward("NOERROR", ["a.b.example.com."], ["203.0.113.9"])
    assert item["after"] == forward("NODATA", ["a.b.example.com."], [])
    assert item["changed"] is True


def test_new_delegation_turns_answer_into_referral(client):
    z = create(client, records=[rec("host.sub.example.com", "A", "192.0.2.7")])
    update(
        client, z["id"], 0,
        operations=[add("sub.example.com", "NS", "ns1.elsewhere.example.net")],
    )
    item = impact(
        client, z["id"], 0, 1, [{"type": "A", "name": "host.sub.example.com"}]
    ).json()["results"][0]
    assert item["before"] == forward(
        "NOERROR", ["host.sub.example.com."], ["192.0.2.7"]
    )
    assert item["after"] == forward(
        "REFERRAL",
        ["host.sub.example.com."],
        [],
        delegation="sub.example.com.",
        nameservers=["ns1.elsewhere.example.net."],
    )
    assert item["changed"] is True


def test_glue_change_is_detected_under_unchanged_delegation(client):
    z = create(
        client,
        records=[
            rec("del.example.com", "NS", "ns1.del.example.com"),
            rec("ns1.del.example.com", "A", "198.51.100.10"),
        ],
    )
    update(
        client, z["id"], 0,
        operations=[
            delete("ns1.del.example.com", "A", "198.51.100.10"),
            add("ns1.del.example.com", "A", "198.51.100.11"),
        ],
    )
    item = impact(
        client, z["id"], 0, 1, [{"type": "A", "name": "host.del.example.com"}]
    ).json()["results"][0]
    assert item["before"]["status"] == item["after"]["status"] == "REFERRAL"
    assert item["before"]["glue"] == [
        {"name": "ns1.del.example.com.", "type": "A", "addresses": ["198.51.100.10"]}
    ]
    assert item["after"]["glue"] == [
        {"name": "ns1.del.example.com.", "type": "A", "addresses": ["198.51.100.11"]}
    ]
    assert item["changed"] is True


def test_cname_loop_in_one_version_is_reported(client):
    z = create(
        client,
        records=[
            rec("a.example.com", "CNAME", "www.example.com"),
            rec("www.example.com", "A", "192.0.2.1"),
        ],
    )
    update(
        client, z["id"], 0,
        operations=[
            delete("www.example.com", "A", "192.0.2.1"),
            add("www.example.com", "CNAME", "a.example.com"),
        ],
    )
    item = impact(client, z["id"], 0, 1, [{"type": "A", "name": "a.example.com"}]).json()[
        "results"
    ][0]
    assert item["before"]["status"] == "NOERROR"
    assert item["after"]["status"] == "CNAME_LOOP"
    assert item["after"]["chain"] == [
        "a.example.com.",
        "www.example.com.",
        "a.example.com.",
    ]
    assert item["after"]["error"]
    assert item["changed"] is True


# ---------------------------------------------------------------------------
# PTR probes
# ---------------------------------------------------------------------------


def reverse_zone(client):
    return create(
        client,
        zone="168.192.in-addr.arpa",
        records=[rec("10.1.168.192.in-addr.arpa", "PTR", "host.example.com")],
    )


def test_ptr_probe_reports_hostnames_before_and_after(client):
    z = reverse_zone(client)
    update(
        client, z["id"], 0,
        operations=[add("10.1.168.192.in-addr.arpa", "PTR", "alias.example.com")],
    )
    r = impact(client, z["id"], 0, 1, [{"type": "PTR", "address": "192.168.1.10"}])
    assert r.status_code == 200, r.text
    item = r.json()["results"][0]
    assert item["type"] == "PTR"
    assert item["query"] == "10.1.168.192.in-addr.arpa."
    assert item["address"] == "192.168.1.10"
    assert item["before"] == {
        "status": "NOERROR",
        "chain": ["10.1.168.192.in-addr.arpa."],
        "hostnames": ["host.example.com."],
        "delegation": None,
        "nameservers": [],
        "glue": [],
        "error": None,
    }
    assert item["after"]["hostnames"] == ["alias.example.com.", "host.example.com."]
    assert item["changed"] is True


def test_ptr_probe_outside_reverse_zone_is_refused_not_an_error(client):
    z = reverse_zone(client)
    item = impact(client, z["id"], 0, 0, [{"type": "PTR", "address": "10.0.0.1"}]).json()[
        "results"
    ][0]
    assert item["query"] == "1.0.0.10.in-addr.arpa."
    assert item["before"]["status"] == "REFUSED"
    assert item["changed"] is False


def test_mixed_a_and_ptr_probes_in_one_batch(client):
    z = create(
        client,
        zone="168.192.in-addr.arpa",
        records=[
            rec("10.1.168.192.in-addr.arpa", "PTR", "host.example.com"),
            rec("10.1.168.192.in-addr.arpa", "A", "192.0.2.1"),
        ],
    )
    update(
        client, z["id"], 0,
        operations=[add("10.1.168.192.in-addr.arpa", "A", "192.0.2.2")],
    )
    body = impact(
        client, z["id"], 0, 1,
        [
            {"type": "PTR", "address": "192.168.1.10"},
            {"type": "A", "name": "10.1.168.192.in-addr.arpa"},
        ],
    ).json()
    ptr_item, a_item = body["results"]
    assert ptr_item["type"] == "PTR"
    assert ptr_item["changed"] is False
    assert a_item["type"] == "A"
    assert a_item["before"]["addresses"] == ["192.0.2.1"]
    assert a_item["after"]["addresses"] == ["192.0.2.1", "192.0.2.2"]
    assert a_item["changed"] is True


def test_ptr_probe_against_forward_zone_is_422(client):
    z = create(client)  # example.com is not a reverse zone
    r = impact(client, z["id"], 0, 0, [{"type": "PTR", "address": "192.168.1.10"}])
    assert r.status_code == 422


def test_ptr_probe_against_too_deep_reverse_zone_is_422(client):
    z = create(client, zone="1.2.3.4.5.in-addr.arpa")  # five octet labels
    r = impact(client, z["id"], 0, 0, [{"type": "PTR", "address": "1.2.3.4"}])
    assert r.status_code == 422
    # A probes against the same zone still work
    r = impact(client, z["id"], 0, 0, [{"type": "A", "name": "6.5.4.3.2.1.in-addr.arpa"}])
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Error handling: 404 and 422, all-or-nothing batches
# ---------------------------------------------------------------------------


def test_unknown_zone_returns_404(client):
    assert impact(client, "no-such-zone", 0, 0, []).status_code == 404


def test_unarchived_version_returns_404(client):
    z = create(client)
    probes = [{"type": "A", "name": "www.example.com"}]
    assert impact(client, z["id"], 0, 1, probes).status_code == 404
    assert impact(client, z["id"], 1, 0, probes).status_code == 404
    assert impact(client, z["id"], 1, 1, probes).status_code == 404


@pytest.mark.parametrize(
    "probe",
    [
        {"type": "MX", "name": "www.example.com"},
        {"type": "A"},  # missing name
        {"type": "PTR"},  # missing address
        {"type": "A", "name": "bad..name"},
        {"type": "A", "name": "*.example.com"},  # wildcards are not query names
        {"type": "A", "name": ""},
        {"type": "PTR", "address": "999.1.1.1"},
        {"type": "PTR", "address": "not-an-ip"},
        {"name": "www.example.com"},  # missing type
    ],
)
def test_invalid_probes_return_422(client, probe):
    z = create(client)
    assert impact(client, z["id"], 0, 0, [probe]).status_code == 422


def test_one_bad_probe_fails_the_whole_batch(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    r = impact(
        client, z["id"], 0, 0,
        [
            {"type": "A", "name": "www.example.com"},  # valid
            {"type": "PTR"},  # invalid: no address
        ],
    )
    assert r.status_code == 422
    # no partial results are returned
    assert "results" not in r.json()


@pytest.mark.parametrize(
    "body",
    [
        {"from_version": -1, "to_version": 0, "probes": []},
        {"from_version": 0, "to_version": -1, "probes": []},
        {"from_version": "abc", "to_version": 0, "probes": []},
        {"to_version": 0, "probes": []},  # missing from_version
        {"from_version": 0, "probes": []},  # missing to_version
        {"from_version": 0, "to_version": 0, "probes": "not-a-list"},
    ],
)
def test_invalid_request_parameters_return_422(client, body):
    z = create(client)
    assert client.post(f"/zones/{z['id']}/impact", json=body).status_code == 422


# ---------------------------------------------------------------------------
# Read-only behaviour
# ---------------------------------------------------------------------------


def test_impact_does_not_change_snapshot_or_history(client):
    z = create(client, records=[rec("www.example.com", "A", "192.0.2.1")])
    update(
        client, z["id"], 0,
        operations=[add("www.example.com", "A", "192.0.2.2")],
    )
    snap_before = client.get(f"/zones/{z['id']}").json()
    diff_before = client.get(
        f"/zones/{z['id']}/diff", params={"from_version": 0, "to_version": 1}
    ).json()
    for _ in range(2):
        r = impact(client, z["id"], 0, 1, [{"type": "A", "name": "www.example.com"}])
        assert r.status_code == 200
    assert client.get(f"/zones/{z['id']}").json() == snap_before
    assert (
        client.get(
            f"/zones/{z['id']}/diff", params={"from_version": 0, "to_version": 1}
        ).json()
        == diff_before
    )
    # no new version was archived by the impact calls
    assert (
        client.get(
            f"/zones/{z['id']}/diff", params={"from_version": 1, "to_version": 2}
        ).status_code
        == 404
    )
