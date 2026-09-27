"""Tests for POST /reverse (PTR reverse lookups) and PTR record support."""
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


ZONE = "168.192.in-addr.arpa"
RECORDS = [
    {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."},
    {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "alias.example.com."},
    # duplicate value: deduplicated in the answer
    {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."},
    # PTR targets may live outside the zone
    {"name": "20.1.168.192.in-addr.arpa", "type": "PTR", "value": "other.example.net."},
]


def reverse(client, address, records=RECORDS, zone=ZONE):
    return client.post(
        "/reverse",
        json={"zone": zone, "records": records, "address": address},
    )


# ---------------------------------------------------------------------------
# Happy path: query name generation, dedup/sort, response shape
# ---------------------------------------------------------------------------


def test_ptr_query_returns_deduped_sorted_hostnames(client):
    r = reverse(client, "192.168.1.10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["zone"] == "168.192.in-addr.arpa."
    assert body["address"] == "192.168.1.10"
    # octets reversed under in-addr.arpa.
    assert body["query"] == "10.1.168.192.in-addr.arpa."
    assert body["chain"] == ["10.1.168.192.in-addr.arpa."]
    # deduplicated and sorted
    assert body["hostnames"] == ["alias.example.com.", "host.example.com."]
    assert body["delegation"] is None
    assert body["nameservers"] == []
    assert body["glue"] == []
    assert body["error"] is None


def test_ptr_target_may_be_outside_zone(client):
    r = reverse(client, "192.168.1.20")
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["hostnames"] == ["other.example.net."]


def test_names_are_normalized(client):
    r = reverse(
        client,
        "192.168.1.10",
        records=[
            {"name": "10.1.168.192.IN-ADDR.ARPA", "type": "ptr", "value": "HOST.Example.COM"}
        ],
        zone="168.192.In-Addr.ARPA",
    )
    body = r.json()
    assert body["zone"] == "168.192.in-addr.arpa."
    assert body["hostnames"] == ["host.example.com."]


@pytest.mark.parametrize(
    "zone",
    [
        "in-addr.arpa",
        "192.in-addr.arpa",
        "168.192.in-addr.arpa",
        "1.168.192.in-addr.arpa",
        "10.1.168.192.in-addr.arpa",
    ],
)
def test_zone_may_have_up_to_four_octet_labels(client, zone):
    records = [rec for rec in RECORDS if rec["name"].startswith("10.1")]
    r = reverse(client, "192.168.1.10", records=records, zone=zone)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NOERROR"
    assert r.json()["hostnames"] == ["alias.example.com.", "host.example.com."]


def test_address_outside_zone_is_refused(client):
    r = reverse(client, "10.0.0.1")
    body = r.json()
    assert body["status"] == "REFUSED"
    assert body["query"] == "1.0.0.10.in-addr.arpa."
    assert body["hostnames"] == []
    assert body["error"]


# ---------------------------------------------------------------------------
# NODATA / NXDOMAIN
# ---------------------------------------------------------------------------


def test_nodata_when_name_has_only_an_a_record(client):
    records = RECORDS + [
        {"name": "5.1.168.192.in-addr.arpa", "type": "A", "value": "192.0.2.1"}
    ]
    r = reverse(client, "192.168.1.5", records=records)
    body = r.json()
    assert body["status"] == "NODATA"
    assert body["hostnames"] == []


def test_nodata_via_cname_to_empty_non_terminal(client):
    # 2.168.192.in-addr.arpa. exists only as the parent of 1.2.168.192....
    records = RECORDS + [
        {
            "name": "11.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "2.168.192.in-addr.arpa",
        },
        {"name": "1.2.168.192.in-addr.arpa", "type": "PTR", "value": "x.example.com."},
    ]
    r = reverse(client, "192.168.1.11", records=records)
    body = r.json()
    assert body["status"] == "NODATA"
    assert body["chain"] == [
        "11.1.168.192.in-addr.arpa.",
        "2.168.192.in-addr.arpa.",
    ]


def test_nxdomain(client):
    r = reverse(client, "192.168.1.99")
    body = r.json()
    assert body["status"] == "NXDOMAIN"
    assert body["hostnames"] == []


# ---------------------------------------------------------------------------
# Wildcards and CNAMEs follow the forward-resolution rules
# ---------------------------------------------------------------------------


def test_wildcard_cname_is_followed(client):
    records = RECORDS + [
        {
            "name": "*.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "10.1.168.192.in-addr.arpa",
        }
    ]
    r = reverse(client, "192.168.1.99", records=records)
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["chain"] == [
        "99.1.168.192.in-addr.arpa.",
        "10.1.168.192.in-addr.arpa.",
    ]
    assert body["hostnames"] == ["alias.example.com.", "host.example.com."]


def test_wildcard_a_record_gives_nodata_for_ptr(client):
    records = RECORDS + [
        {"name": "*.1.168.192.in-addr.arpa", "type": "A", "value": "203.0.113.9"}
    ]
    r = reverse(client, "192.168.1.99", records=records)
    body = r.json()
    assert body["status"] == "NODATA"
    assert body["hostnames"] == []


def test_empty_non_terminal_blocks_upper_wildcard(client):
    records = [
        {"name": "*.168.192.in-addr.arpa", "type": "A", "value": "203.0.113.9"},
        {"name": "5.2.168.192.in-addr.arpa", "type": "PTR", "value": "x.example.com."},
    ]
    # closest encloser of 9.2.168.192... is the empty node 2.168.192...,
    # so the upper wildcard must not apply
    r = reverse(client, "192.168.2.9", records=records)
    assert r.json()["status"] == "NXDOMAIN"


def test_cname_chain_to_ptr(client):
    records = [
        {
            "name": "10.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "10.2.168.192.in-addr.arpa",
        },
        {"name": "10.2.168.192.in-addr.arpa", "type": "PTR", "value": "real.example.com."},
    ]
    r = reverse(client, "192.168.1.10", records=records)
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["chain"] == [
        "10.1.168.192.in-addr.arpa.",
        "10.2.168.192.in-addr.arpa.",
    ]
    assert body["hostnames"] == ["real.example.com."]


def test_cname_loop(client):
    records = [
        {
            "name": "10.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "11.1.168.192.in-addr.arpa",
        },
        {
            "name": "11.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "10.1.168.192.in-addr.arpa",
        },
    ]
    r = reverse(client, "192.168.1.10", records=records)
    body = r.json()
    assert body["status"] == "CNAME_LOOP"
    assert body["chain"] == [
        "10.1.168.192.in-addr.arpa.",
        "11.1.168.192.in-addr.arpa.",
        "10.1.168.192.in-addr.arpa.",
    ]
    assert body["hostnames"] == []
    assert body["error"]


def test_cname_target_outside_zone_still_rejected(client):
    records = [
        {"name": "10.1.168.192.in-addr.arpa", "type": "CNAME", "value": "other.example.net."}
    ]
    assert reverse(client, "192.168.1.10", records=records).status_code == 422


# ---------------------------------------------------------------------------
# Delegation: referrals carry no hostnames
# ---------------------------------------------------------------------------

DELEGATION_RECORDS = [
    {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."},
    {"name": "2.168.192.in-addr.arpa", "type": "NS", "value": "ns1.2.168.192.in-addr.arpa"},
    {"name": "2.168.192.in-addr.arpa", "type": "NS", "value": "ns2.example.net."},
    {"name": "ns1.2.168.192.in-addr.arpa", "type": "A", "value": "198.51.100.1"},
    # inert below the cut: never authoritative
    {"name": "10.2.168.192.in-addr.arpa", "type": "PTR", "value": "hidden.example.com."},
]


def test_referral_returns_no_hostnames(client):
    r = reverse(client, "192.168.2.10", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "2.168.192.in-addr.arpa."
    assert body["nameservers"] == ["ns1.2.168.192.in-addr.arpa.", "ns2.example.net."]
    assert body["glue"] == [
        {"name": "ns1.2.168.192.in-addr.arpa.", "type": "A", "addresses": ["198.51.100.1"]}
    ]
    assert body["hostnames"] == []
    assert body["chain"] == ["10.2.168.192.in-addr.arpa."]


def test_referral_at_delegation_point_via_cname(client):
    records = DELEGATION_RECORDS[1:] + [  # drop the PTR at 10.1... to allow the CNAME
        {
            "name": "10.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "2.168.192.in-addr.arpa",
        }
    ]
    r = reverse(client, "192.168.1.10", records=records)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["chain"] == [
        "10.1.168.192.in-addr.arpa.",
        "2.168.192.in-addr.arpa.",
    ]
    assert body["hostnames"] == []


# ---------------------------------------------------------------------------
# PTR record validation (422)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["999.1.1.1", "not-an-ip", "1.2.3", "192.168.01.1", "192.168.1.1/24", ""],
)
def test_invalid_address_returns_422(client, address):
    assert reverse(client, address).status_code == 422


@pytest.mark.parametrize(
    "zone",
    [
        "example.com",  # not a reverse zone
        "1.2.3.4.5.in-addr.arpa",  # five octet labels
        "256.in-addr.arpa",  # octet out of range
        "01.in-addr.arpa",  # leading zero
        "a.in-addr.arpa",  # non-numeric label
    ],
)
def test_zone_restricted_to_in_addr_arpa(client, zone):
    assert reverse(client, "192.168.1.10", zone=zone).status_code == 422


@pytest.mark.parametrize(
    "owner",
    [
        "1.168.192.in-addr.arpa",  # only three octet labels
        "300.1.168.192.in-addr.arpa",  # octet out of range
        "010.1.168.192.in-addr.arpa",  # leading zero
        "*.1.168.192.in-addr.arpa",  # wildcard not allowed
        "10.1.167.192.in-addr.arpa",  # outside the zone
        "host.example.com",  # not a reversed IPv4 name at all
    ],
)
def test_invalid_ptr_owner_returns_422(client, owner):
    records = [{"name": owner, "type": "PTR", "value": "host.example.com."}]
    assert reverse(client, "192.168.1.10", records=records).status_code == 422


def test_ptr_owner_with_five_labels_rejected(client):
    records = [
        {"name": "1.10.1.168.192.in-addr.arpa", "type": "PTR", "value": "h.example.com."}
    ]
    r = reverse(client, "192.168.1.10", records=records, zone="in-addr.arpa")
    assert r.status_code == 422


def test_ptr_conflicts_with_cname(client):
    records = [
        {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "h.example.com."},
        {
            "name": "10.1.168.192.in-addr.arpa",
            "type": "CNAME",
            "value": "20.1.168.192.in-addr.arpa",
        },
    ]
    assert reverse(client, "192.168.1.10", records=records).status_code == 422


def test_ptr_conflicts_with_ns(client):
    records = [
        {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "h.example.com."},
        {"name": "10.1.168.192.in-addr.arpa", "type": "NS", "value": "ns1.example.net."},
    ]
    assert reverse(client, "192.168.1.10", records=records).status_code == 422


def test_unsupported_record_type_returns_422(client):
    records = [
        {"name": "10.1.168.192.in-addr.arpa", "type": "TXT", "value": "hello"}
    ]
    assert reverse(client, "192.168.1.10", records=records).status_code == 422


def test_ptr_and_a_may_share_a_name(client):
    records = [
        {"name": "10.1.168.192.in-addr.arpa", "type": "A", "value": "192.0.2.1"},
        {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."},
    ]
    r = reverse(client, "192.168.1.10", records=records)
    assert r.json()["status"] == "NOERROR"
    assert r.json()["hostnames"] == ["host.example.com."]
    # ...and the A record stays authoritative for forward queries
    r = client.post(
        "/resolve",
        json={
            "zone": ZONE,
            "records": records,
            "query": "10.1.168.192.in-addr.arpa",
            "qtype": "A",
        },
    )
    assert r.json()["status"] == "NOERROR"
    assert r.json()["addresses"] == ["192.0.2.1"]


def test_forward_resolve_still_rejects_ptr_qtype(client):
    r = client.post(
        "/resolve",
        json={"zone": ZONE, "records": RECORDS, "query": "10.1.168.192.in-addr.arpa", "qtype": "PTR"},
    )
    assert r.status_code == 422


def test_a_query_on_ptr_only_name_is_nodata(client):
    r = client.post(
        "/resolve",
        json={"zone": ZONE, "records": RECORDS, "query": "10.1.168.192.in-addr.arpa"},
    )
    assert r.json()["status"] == "NODATA"


# ---------------------------------------------------------------------------
# PTR records through snapshots, conditional updates, import and diff
# ---------------------------------------------------------------------------


def create(client, zone=ZONE, records=None):
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


def test_ptr_records_via_zone_creation_and_reverse(client):
    z = create(
        client,
        records=[
            {"name": "10.1.168.192.IN-ADDR.ARPA", "type": "ptr", "value": "HOST.Example.com"},
        ],
    )
    assert z["zone"] == "168.192.in-addr.arpa."
    assert z["records"] == [
        {"name": "10.1.168.192.in-addr.arpa.", "type": "PTR", "value": "host.example.com."}
    ]
    # the snapshot records feed straight back into /reverse
    r = reverse(client, "192.168.1.10", records=z["records"], zone=z["zone"])
    assert r.json()["status"] == "NOERROR"
    assert r.json()["hostnames"] == ["host.example.com."]


def test_ptr_record_in_forward_zone_rejected(client):
    r = client.post(
        "/zones",
        json={
            "zone": "example.com",
            "records": [{"name": "www.example.com", "type": "PTR", "value": "h.example.com."}],
        },
    )
    assert r.status_code == 422


def test_ptr_conditional_update(client):
    z = create(
        client,
        records=[
            {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "host.example.com."}
        ],
    )
    r = update(
        client,
        z["id"],
        0,
        prerequisites=[
            # values are normalized before the set comparison
            {"name": "10.1.168.192.in-addr.arpa", "type": "ptr", "values": ["HOST.example.COM"]}
        ],
        operations=[
            {
                "op": "add",
                "name": "10.1.168.192.in-addr.arpa",
                "type": "PTR",
                "value": "alias.example.com",
            },
            {
                "op": "delete",
                "name": "10.1.168.192.in-addr.arpa",
                "type": "PTR",
                "value": "host.example.com",
            },
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["records"] == [
        {"name": "10.1.168.192.in-addr.arpa.", "type": "PTR", "value": "alias.example.com."}
    ]
    r = reverse(client, "192.168.1.10", records=body["records"], zone=body["zone"])
    assert r.json()["hostnames"] == ["alias.example.com."]


def test_ptr_update_with_invalid_owner_returns_422_and_keeps_version(client):
    z = create(client)
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {"op": "add", "name": "1.168.192.in-addr.arpa", "type": "PTR", "value": "h.example.com."}
        ],
    )
    assert r.status_code == 422
    assert client.get(f"/zones/{z['id']}").json()["version"] == 0


def test_ptr_import_with_relative_targets(client):
    text = (
        "$TTL 3600\n"
        "$ORIGIN 168.192.in-addr.arpa.\n"
        "10.1 PTR host.example.com.\n"
        "20.1 PTR other\n"
        "30.1 300 IN PTR @\n"
    )
    r = client.post("/zones/import", json={"zone": "168.192.in-addr.arpa", "text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["records"] == [
        {"name": "10.1.168.192.in-addr.arpa.", "type": "PTR", "value": "host.example.com."},
        {
            "name": "20.1.168.192.in-addr.arpa.",
            "type": "PTR",
            "value": "other.168.192.in-addr.arpa.",
        },
        {
            "name": "30.1.168.192.in-addr.arpa.",
            "type": "PTR",
            "value": "168.192.in-addr.arpa.",
        },
    ]
    # imported snapshot is archived at version 0 and resolves via /reverse
    r = client.get(
        f"/zones/{body['id']}/diff", params={"from_version": 0, "to_version": 0}
    )
    assert r.status_code == 200
    r = reverse(client, "192.168.1.10", records=body["records"], zone=body["zone"])
    assert r.json()["hostnames"] == ["host.example.com."]


def test_ptr_records_participate_in_diff(client):
    z = create(
        client,
        records=[
            {"name": "10.1.168.192.in-addr.arpa", "type": "PTR", "value": "a.example.com."}
        ],
    )
    r = update(
        client,
        z["id"],
        0,
        operations=[
            {
                "op": "delete",
                "name": "10.1.168.192.in-addr.arpa",
                "type": "PTR",
                "value": "a.example.com.",
            },
            {
                "op": "add",
                "name": "10.1.168.192.in-addr.arpa",
                "type": "PTR",
                "value": "b.example.com.",
            },
        ],
    )
    assert r.status_code == 200, r.text
    r = client.get(
        f"/zones/{z['id']}/diff", params={"from_version": 0, "to_version": 1}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == [
        {"name": "10.1.168.192.in-addr.arpa.", "type": "PTR", "value": "a.example.com."}
    ]
    assert body["added"] == [
        {"name": "10.1.168.192.in-addr.arpa.", "type": "PTR", "value": "b.example.com."}
    ]
