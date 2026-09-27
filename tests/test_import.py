"""Tests for POST /zones/import (DNS master file subset import)."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import store as zone_store
from app.main import app


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "zones.db"
    monkeypatch.setenv("DNS_DB_PATH", str(path))
    zone_store.reset_store()
    yield path
    zone_store.reset_store()


@pytest.fixture()
def client(db_path):
    with TestClient(app) as c:
        yield c


def import_zone(client, zone="example.com", text=""):
    return client.post("/zones/import", json={"zone": zone, "text": text})


def stored_zone_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy path: response shape, normalization, dedup, sorting, version 0
# ---------------------------------------------------------------------------


def test_import_basic_a_record(client):
    r = import_zone(client, text="www A 192.0.2.1\n")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"]
    assert body["version"] == 0
    assert body["zone"] == "example.com."
    assert body["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"}
    ]


def test_relative_names_and_targets_expand_against_origin(client):
    text = (
        "@   A 192.0.2.1\n"
        "www A 192.0.2.2\n"
        "alias CNAME www\n"
        "other.example.com. A 192.0.2.3\n"
        "abs CNAME other.example.com.\n"
        "mail CNAME @\n"
    )
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "abs.example.com.", "type": "CNAME", "value": "other.example.com."},
        {"name": "alias.example.com.", "type": "CNAME", "value": "www.example.com."},
        {"name": "example.com.", "type": "A", "value": "192.0.2.1"},
        {"name": "mail.example.com.", "type": "CNAME", "value": "example.com."},
        {"name": "other.example.com.", "type": "A", "value": "192.0.2.3"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"},
    ]


def test_import_normalizes_deduplicates_and_sorts(client):
    text = (
        "WWW A 192.0.2.1\n"
        "www.example.com. A 192.0.2.1\n"
        "alias CNAME WWW.Example.COM.\n"
        "www a 192.0.2.1\n"
    )
    r = import_zone(client, zone="Example.COM", text=text)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone"] == "example.com."
    assert body["records"] == [
        {"name": "alias.example.com.", "type": "CNAME", "value": "www.example.com."},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
    ]


def test_empty_and_comment_only_text_create_empty_zone(client):
    for text in ("", "\n\n", "; nothing here\n  ; another comment\n"):
        r = import_zone(client, text=text)
        assert r.status_code == 200, r.text
        assert r.json()["records"] == []


def test_comment_may_contain_special_characters(client):
    text = 'www A 192.0.2.1 ; comment with "quotes" \\ and (parens) and $\n'
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"}
    ]


def test_wildcard_record_imported(client):
    r = import_zone(client, text="*.wild A 203.0.113.9\n")
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "*.wild.example.com.", "type": "A", "value": "203.0.113.9"}
    ]


def test_imported_snapshot_is_archived_at_version_zero(client):
    z = import_zone(client, text="www A 192.0.2.1\n").json()
    assert client.get(f"/zones/{z['id']}").json() == z
    r = client.get(
        f"/zones/{z['id']}/diff", params={"from_version": 0, "to_version": 0}
    )
    assert r.status_code == 200
    assert r.json()["removed"] == []
    assert r.json()["added"] == []
    # the imported zone can be updated like any other snapshot
    r = client.post(
        f"/zones/{z['id']}/updates",
        json={
            "expected_version": 0,
            "prerequisites": [],
            "operations": [
                {"op": "add", "name": "b.example.com", "type": "A", "value": "192.0.2.2"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1


def test_import_delegation_with_glue_and_resolve(client):
    text = (
        "del NS ns1.del\n"
        "del NS ns2.elsewhere.example.net.\n"
        "ns1.del A 198.51.100.10\n"
    )
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["records"] == [
        {"name": "del.example.com.", "type": "NS", "value": "ns1.del.example.com."},
        {
            "name": "del.example.com.",
            "type": "NS",
            "value": "ns2.elsewhere.example.net.",
        },
        {"name": "ns1.del.example.com.", "type": "A", "value": "198.51.100.10"},
    ]
    r = client.post(
        "/resolve",
        json={
            "zone": body["zone"],
            "records": body["records"],
            "query": "host.del.example.com",
        },
    )
    assert r.json()["status"] == "REFERRAL"
    assert r.json()["glue"] == [
        {"name": "ns1.del.example.com.", "type": "A", "addresses": ["198.51.100.10"]}
    ]


# ---------------------------------------------------------------------------
# $ORIGIN and owner name inheritance
# ---------------------------------------------------------------------------


def test_origin_directive_switches_suffix(client):
    text = (
        "www A 192.0.2.1\n"
        "$ORIGIN sub.example.com.\n"
        "www A 192.0.2.2\n"
        "$ORIGIN deeper\n"
        "www A 192.0.2.3\n"
        "$ORIGIN example.com.\n"
        "back A 192.0.2.4\n"
    )
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    names = {rec["name"] for rec in r.json()["records"]}
    assert names == {
        "www.example.com.",
        "www.sub.example.com.",
        "www.deeper.sub.example.com.",
        "back.example.com.",
    }


def test_blank_first_column_reuses_previous_expanded_name(client):
    text = "www A 192.0.2.1\n\tA 192.0.2.2\n   A 192.0.2.3\n"
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.3"},
    ]


def test_origin_switch_does_not_change_inherited_name(client):
    text = (
        "www A 192.0.2.1\n"
        "$ORIGIN sub.example.com.\n"
        "  A 192.0.2.2\n"
    )
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    # the inherited name stays www.example.com. even though origin moved
    assert r.json()["records"] == [
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.2"},
    ]


def test_first_record_without_name_rejected(client):
    r = import_zone(client, text="  A 192.0.2.1\n")
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 1


def test_first_record_without_name_after_directives_rejected(client):
    r = import_zone(client, text="$TTL 300\n$ORIGIN example.com.\n  A 192.0.2.1\n")
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 3


# ---------------------------------------------------------------------------
# TTL and class
# ---------------------------------------------------------------------------


def test_ttl_and_class_in_any_order_are_accepted_not_stored(client):
    text = (
        "$TTL 300\n"
        "www 3600 IN A 192.0.2.1\n"
        "mail IN 3600 A 192.0.2.2\n"
        "web IN A 192.0.2.3\n"
        "api 60 A 192.0.2.4\n"
        "low in a 192.0.2.5\n"
    )
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    records = r.json()["records"]
    assert {rec["name"] for rec in records} == {
        "www.example.com.",
        "mail.example.com.",
        "web.example.com.",
        "api.example.com.",
        "low.example.com.",
    }
    # TTL is validated only: nothing but name/type/value is stored
    assert all(set(rec) == {"name", "type", "value"} for rec in records)


@pytest.mark.parametrize(
    "text",
    [
        "$TTL abc\n",
        "$TTL\n",
        "$TTL 1 2\n",
        "www 60 60 A 192.0.2.1\n",
        "www IN IN A 192.0.2.1\n",
        "www -5 A 192.0.2.1\n",
    ],
)
def test_invalid_ttl_or_class_rejected(client, text):
    r = import_zone(client, text=text)
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 1


# ---------------------------------------------------------------------------
# Parentheses
# ---------------------------------------------------------------------------


def test_parentheses_join_physical_lines(client):
    text = (
        "www A (\n"
        "    ; comment inside parens\n"
        "    192.0.2.1 )\n"
        "alias CNAME ( www )\n"
        "del NS (\n"
        "    ns1.del )\n"
        "ns1.del A 198.51.100.10\n"
    )
    r = import_zone(client, text=text)
    assert r.status_code == 200, r.text
    assert r.json()["records"] == [
        {"name": "alias.example.com.", "type": "CNAME", "value": "www.example.com."},
        {"name": "del.example.com.", "type": "NS", "value": "ns1.del.example.com."},
        {"name": "ns1.del.example.com.", "type": "A", "value": "198.51.100.10"},
        {"name": "www.example.com.", "type": "A", "value": "192.0.2.1"},
    ]


def test_nested_parentheses_rejected_with_record_start_line(client):
    text = "www A 192.0.2.1\nmail A (\n  192.0.2.2 ( ) )\n"
    r = import_zone(client, text=text)
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 2


def test_unbalanced_closing_parenthesis_rejected(client):
    r = import_zone(client, text="www A 192.0.2.1\n)\n")
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 2


def test_unclosed_parenthesis_rejected_with_record_start_line(client):
    text = "www A 192.0.2.1\nalias CNAME (\n  www\n"
    r = import_zone(client, text=text)
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 2


# ---------------------------------------------------------------------------
# Rejected constructs: quotes, escapes, other directives and types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'www A "192.0.2.1"\n',
        'www A 192.0.2.1 ; ok\nmail TXT "hi"\n',
        "www\\ A 192.0.2.1\n",
        "www A 192.0.2.\\49\n",
    ],
)
def test_quotes_and_escapes_rejected(client, text):
    assert import_zone(client, text=text).status_code == 422


@pytest.mark.parametrize("directive", ["$INCLUDE", "$GENERATE", "$ORIGINx", "$foo"])
def test_unknown_directives_rejected(client, directive):
    r = import_zone(client, text=f"{directive} something\n")
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 1


@pytest.mark.parametrize("rtype", ["TXT", "AAAA", "SOA", "MX", "CAA"])
def test_unsupported_record_types_rejected(client, rtype):
    r = import_zone(client, text=f"www {rtype} whatever\n")
    assert r.status_code == 422
    assert r.json()["detail"]["line"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "www A\n",  # missing value
        "www\n",  # missing type
        "www 3600\n",  # TTL then nothing
        "www A 192.0.2.1 extra\n",  # extra token
        "$ORIGIN\n",  # missing argument
        "$ORIGIN a.example.com. b.example.com.\n",  # extra argument
        "$ORIGIN bad..name\n",  # invalid origin
        "()\n",  # empty record
    ],
)
def test_malformed_records_rejected(client, text):
    r = import_zone(client, text=text)
    assert r.status_code == 422
    assert "line" in r.json()["detail"]


def test_syntax_error_reports_logical_record_start_line(client):
    text = (
        "www A 192.0.2.1\n"  # line 1
        "\n"  # line 2
        "; comment\n"  # line 3
        "bad TXT (\n"  # line 4: logical record starts here
        "  hello )\n"  # line 5
    )
    r = import_zone(client, text=text)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["line"] == 4
    assert "TXT" in detail["message"]


# ---------------------------------------------------------------------------
# Zone-level validation and atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "@ NS ns1.example.net.\n",  # NS at the apex
        "alias CNAME other.org.\n",  # CNAME target outside the zone
        "www A 999.1.1.1\n",  # invalid IPv4
        "other.org. A 192.0.2.1\n",  # record name outside the zone
        "x A 192.0.2.1\nx CNAME x\n",  # CNAME conflicts with A
        "outer NS ns1.example.net.\ninner.outer NS ns2.example.net.\n",  # nested
    ],
)
def test_zone_validation_reuses_existing_rules(client, db_path, text):
    r = import_zone(client, text=text)
    assert r.status_code == 422
    # zone-level failures keep the plain string detail of POST /zones
    assert isinstance(r.json()["detail"], str)
    assert stored_zone_count(db_path) == 0


def test_syntax_error_creates_no_snapshot(client, db_path):
    r = import_zone(client, text="www TXT 192.0.2.1\n")
    assert r.status_code == 422
    assert stored_zone_count(db_path) == 0


def test_invalid_zone_name_rejected(client):
    r = import_zone(client, zone="bad..zone", text="www A 192.0.2.1\n")
    assert r.status_code == 422
