from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ZONE = "example.com"
RECORDS = [
    {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
    {"name": "www.example.com", "type": "A", "value": "192.0.2.2"},
    {"name": "alias.example.com", "type": "CNAME", "value": "www.example.com"},
    {"name": "loop-a.example.com", "type": "CNAME", "value": "loop-b.example.com"},
    {"name": "loop-b.example.com", "type": "CNAME", "value": "loop-a.example.com"},
    {"name": "*.wild.example.com", "type": "A", "value": "203.0.113.9"},
    {"name": "host.sub.wild.example.com", "type": "A", "value": "203.0.113.10"},
    {"name": "dead.example.com", "type": "CNAME", "value": "ghost.example.com"},
    {"name": "cname-wild.example.com", "type": "CNAME", "value": "www.example.com"},
]


def post(query, records=RECORDS, zone=ZONE, qtype="A"):
    return client.post(
        "/resolve",
        json={"zone": zone, "records": records, "query": query, "qtype": qtype},
    )


def test_simple_a_query():
    r = post("www.example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "NOERROR"
    assert sorted(body["addresses"]) == ["192.0.2.1", "192.0.2.2"]
    assert body["chain"] == ["www.example.com."]
    assert body["query"] == "www.example.com."
    assert body["zone"] == "example.com."


def test_names_are_case_insensitive_and_get_trailing_dot():
    r = post("WWW.Example.COM")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["query"] == "www.example.com."
    assert sorted(body["addresses"]) == ["192.0.2.1", "192.0.2.2"]


def test_cname_chain_to_a_records():
    r = post("alias.example.com")
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["chain"] == ["alias.example.com.", "www.example.com."]
    assert sorted(body["addresses"]) == ["192.0.2.1", "192.0.2.2"]


def test_cname_loop_returns_error_and_closed_chain():
    r = post("loop-a.example.com")
    body = r.json()
    assert body["status"] == "CNAME_LOOP"
    assert body["chain"] == [
        "loop-a.example.com.",
        "loop-b.example.com.",
        "loop-a.example.com.",
    ]
    assert body["addresses"] == []
    assert body["error"]


def test_cname_to_missing_name_is_nxdomain():
    r = post("dead.example.com")
    body = r.json()
    assert body["status"] == "NXDOMAIN"
    assert body["chain"] == ["dead.example.com.", "ghost.example.com."]


def test_nxdomain():
    r = post("nope.example.com")
    body = r.json()
    assert body["status"] == "NXDOMAIN"
    assert body["addresses"] == []


def test_nodata_for_empty_non_terminal():
    # sub.wild.example.com exists only as the parent of host.sub.wild.example.com
    r = post("sub.wild.example.com")
    body = r.json()
    assert body["status"] == "NODATA"
    assert body["addresses"] == []


def test_wildcard_match():
    r = post("anything.wild.example.com")
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["addresses"] == ["203.0.113.9"]


def test_wildcard_matches_multiple_labels_below_closest_encloser():
    r = post("a.b.wild.example.com")
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["addresses"] == ["203.0.113.9"]


def test_empty_non_terminal_blocks_upper_wildcard():
    # other.sub.wild.example.com: closest encloser is the empty node
    # sub.wild.example.com, so *.wild.example.com must NOT apply.
    r = post("other.sub.wild.example.com")
    body = r.json()
    assert body["status"] == "NXDOMAIN"
    assert body["addresses"] == []


def test_exact_match_beats_wildcard():
    r = post("host.sub.wild.example.com")
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["addresses"] == ["203.0.113.10"]


def test_cname_conflict_rejected():
    records = [
        {"name": "x.example.com", "type": "A", "value": "192.0.2.5"},
        {"name": "x.example.com", "type": "CNAME", "value": "www.example.com"},
    ]
    r = post("x.example.com", records=records)
    assert r.status_code == 422


def test_double_cname_rejected():
    records = [
        {"name": "x.example.com", "type": "CNAME", "value": "www.example.com"},
        {"name": "x.example.com", "type": "CNAME", "value": "alias.example.com"},
    ]
    r = post("x.example.com", records=records)
    assert r.status_code == 422


def test_cname_target_outside_zone_rejected():
    records = [{"name": "x.example.com", "type": "CNAME", "value": "other.org"}]
    r = post("x.example.com", records=records)
    assert r.status_code == 422


def test_record_outside_zone_rejected():
    records = [{"name": "other.org", "type": "A", "value": "192.0.2.5"}]
    r = post("www.example.com", records=records)
    assert r.status_code == 422


def test_invalid_ipv4_rejected():
    records = [{"name": "x.example.com", "type": "A", "value": "999.1.1.1"}]
    r = post("x.example.com", records=records)
    assert r.status_code == 422


def test_non_a_query_rejected():
    r = post("www.example.com", qtype="AAAA")
    assert r.status_code == 422


def test_query_outside_zone_refused():
    r = post("www.other.org")
    body = r.json()
    assert body["status"] == "REFUSED"


def test_apex_query_nodata_when_apex_has_no_records():
    r = post("example.com")
    body = r.json()
    assert body["status"] == "NODATA"


def test_wildcard_cname_is_followed():
    records = RECORDS + [
        {"name": "*.cw.example.com", "type": "CNAME", "value": "cname-wild.example.com"}
    ]
    r = post("foo.cw.example.com", records=records)
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["chain"] == [
        "foo.cw.example.com.",
        "cname-wild.example.com.",
        "www.example.com.",
    ]
    assert sorted(body["addresses"]) == ["192.0.2.1", "192.0.2.2"]


# ---------------------------------------------------------------------------
# Zone delegation (NS records / REFERRAL)
# ---------------------------------------------------------------------------

DELEGATION_RECORDS = [
    {"name": "www.example.com", "type": "A", "value": "192.0.2.1"},
    {"name": "*.wild.example.com", "type": "A", "value": "203.0.113.9"},
    {"name": "del.example.com", "type": "NS", "value": "NS1.DEL.example.com"},
    # multiple NS records at the same non-apex, non-wildcard name
    {"name": "del.example.com", "type": "NS", "value": "ns2.del.example.com"},
    {"name": "del.example.com", "type": "NS", "value": "ns2.del.example.com"},
    {"name": "del.example.com", "type": "NS", "value": "ns3.outside.example.net"},
    # glue: A records at the in-subtree NS targets
    {"name": "ns1.del.example.com", "type": "A", "value": "198.51.100.1"},
    {"name": "ns1.del.example.com", "type": "A", "value": "198.51.100.2"},
    {"name": "ns2.del.example.com", "type": "A", "value": "198.51.100.3"},
    # inert records below the cut: not glue, never authoritative
    {"name": "host.del.example.com", "type": "A", "value": "198.51.100.77"},
    {"name": "deep.del.example.com", "type": "CNAME", "value": "www.example.com"},
    {"name": "other.example.com", "type": "NS", "value": "a.ns.example"},
    {"name": "other.example.com", "type": "NS", "value": "b.ns.example"},
    {"name": "zone.example.com", "type": "A", "value": "198.51.100.9"},
]


def test_referral_at_delegation_point():
    r = post("del.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."
    assert body["query"] == "del.example.com."
    assert body["addresses"] == []
    assert body["chain"] == ["del.example.com."]
    assert body["error"] is None
    # deduped and sorted
    assert body["nameservers"] == [
        "ns1.del.example.com.",
        "ns2.del.example.com.",
        "ns3.outside.example.net.",
    ]
    glue = {g["name"]: g["addresses"] for g in body["glue"]}
    assert set(glue) == {"ns1.del.example.com.", "ns2.del.example.com."}
    assert glue["ns1.del.example.com."] == ["198.51.100.1", "198.51.100.2"]
    assert glue["ns2.del.example.com."] == ["198.51.100.3"]
    assert all(g["type"] == "A" for g in body["glue"])


def test_referral_inside_delegated_subtree():
    r = post("host.del.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."
    assert body["chain"] == ["host.del.example.com."]
    assert body["addresses"] == []
    # the inert in-subtree A record must not leak into the referral as glue
    glue_names = {g["name"] for g in body["glue"]}
    assert "host.del.example.com." not in glue_names


def test_referral_for_deep_name_in_subtree():
    r = post("a.b.del.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."


def test_delegation_beats_exact_a_record():
    # ns1.del.example.com has glue A records, but querying it directly
    # must still return the referral, never those addresses authoritatively.
    r = post("ns1.del.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."
    assert body["addresses"] == []


def test_delegation_beats_upper_wildcard():
    # anything under del.example.com is referred; no wildcard sits at or below
    # the cut, and even a wildcard above the cut must not match into it.
    r = post("x.del.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."


def test_wildcard_above_unrelated_cut_still_works():
    # the other delegation does not cover *.wild.example.com
    r = post("anything.wild.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["addresses"] == ["203.0.113.9"]


def test_glue_target_above_cut_is_authoritative_outside_referral():
    # other.example.com delegates to out-of-subtree nameservers; a plain
    # in-zone name above any cut answers normally as an authoritative A.
    r = post("zone.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "NOERROR"
    assert body["addresses"] == ["198.51.100.9"]


def test_referral_without_glue_for_outside_targets():
    r = post("other.example.com", records=DELEGATION_RECORDS)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "other.example.com."
    assert body["nameservers"] == ["a.ns.example.", "b.ns.example."]
    assert body["glue"] == []
    assert body["addresses"] == []


def test_cname_chain_into_subtree_keeps_chain_and_returns_referral():
    records = DELEGATION_RECORDS + [
        {"name": "jump.example.com", "type": "CNAME", "value": "host.del.example.com"}
    ]
    r = post("jump.example.com", records=records)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."
    assert body["chain"] == [
        "jump.example.com.",
        "host.del.example.com.",
    ]
    assert body["addresses"] == []


def test_cname_chain_landing_on_delegation_point_refers():
    records = DELEGATION_RECORDS + [
        {"name": "jump2.example.com", "type": "CNAME", "value": "del.example.com"}
    ]
    r = post("jump2.example.com", records=records)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["delegation"] == "del.example.com."
    assert body["chain"] == ["jump2.example.com.", "del.example.com."]


def test_wildcard_cname_into_subtree_refers():
    records = DELEGATION_RECORDS + [
        {"name": "*.w2.example.com", "type": "CNAME", "value": "x.del.example.com"}
    ]
    r = post("foo.w2.example.com", records=records)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["chain"] == ["foo.w2.example.com.", "x.del.example.com."]


def test_ns_at_apex_rejected():
    records = [
        {"name": "example.com", "type": "NS", "value": "ns1.example.net"},
    ]
    r = post("example.com", records=records)
    assert r.status_code == 422


def test_wildcard_ns_rejected():
    records = [
        {"name": "*.del.example.com", "type": "NS", "value": "ns1.example.net"},
    ]
    r = post("del.example.com", records=records)
    assert r.status_code == 422


def test_ns_with_a_at_same_name_rejected():
    records = [
        {"name": "del.example.com", "type": "NS", "value": "ns1.example.net"},
        {"name": "del.example.com", "type": "A", "value": "192.0.2.10"},
    ]
    r = post("del.example.com", records=records)
    assert r.status_code == 422


def test_ns_with_cname_at_same_name_rejected():
    records = [
        {"name": "del.example.com", "type": "NS", "value": "ns1.example.net"},
        {"name": "del.example.com", "type": "CNAME", "value": "www.example.com"},
    ]
    r = post("del.example.com", records=records)
    assert r.status_code == 422


def test_nested_delegation_rejected():
    records = [
        {"name": "outer.example.com", "type": "NS", "value": "ns1.example.net"},
        {"name": "inner.outer.example.com", "type": "NS", "value": "ns2.example.net"},
    ]
    r = post("outer.example.com", records=records)
    assert r.status_code == 422


def test_ns_target_normalized_to_absolute():
    records = [
        {"name": "del.example.com", "type": "NS", "value": "NS1.Example.NET"},
    ]
    r = post("del.example.com", records=records)
    body = r.json()
    assert body["status"] == "REFERRAL"
    assert body["nameservers"] == ["ns1.example.net."]

