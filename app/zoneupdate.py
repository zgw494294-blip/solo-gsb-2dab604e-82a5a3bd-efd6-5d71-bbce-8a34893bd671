"""Conditional zone updates: input normalization, prerequisites, operations.

Everything here is pure. Names follow the same normalization as the resolver
(lowercase, trailing dot, wildcards allowed for record names); values are
normalized per type (canonical IPv4 for A, absolute names for CNAME/NS/PTR).
Anything malformed raises ZoneError, which the HTTP layer maps to 422.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

from .resolver import ZoneError, normalize_name

RECORD_TYPES = ("A", "CNAME", "NS", "PTR")

ADD = "add"
DELETE = "delete"
DELETE_SET = "delete_set"

_OP_ALIASES = {
    "add": ADD,
    "insert": ADD,
    "add_record": ADD,
    "delete": DELETE,
    "del": DELETE,
    "remove": DELETE,
    "delete_record": DELETE,
    "remove_record": DELETE,
    "delete_set": DELETE_SET,
    "del_set": DELETE_SET,
    "delete_rrset": DELETE_SET,
    "delete_all": DELETE_SET,
    "remove_set": DELETE_SET,
    "remove_all": DELETE_SET,
}

Record = tuple[str, str, str]  # (name, type, value), all normalized
Prerequisite = tuple[str, str, frozenset]  # (name, type, values)
Operation = tuple[str, str, str, "str | None"]  # (op, name, type, value)


def normalize_type(rtype: str) -> str:
    rtype_norm = rtype.strip().upper()
    if rtype_norm not in RECORD_TYPES:
        raise ZoneError(f"unsupported record type {rtype!r}; only A, CNAME, NS and PTR")
    return rtype_norm


def normalize_value(rtype: str, value: str) -> str:
    if rtype == "A":
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError as exc:
            raise ZoneError(f"invalid IPv4 address {value!r}") from exc
    return normalize_name(value)


def normalize_record(name: str, rtype: str, value: str) -> Record:
    rtype_norm = normalize_type(rtype)
    return (
        normalize_name(name, allow_wildcard=True),
        rtype_norm,
        normalize_value(rtype_norm, value),
    )


def normalize_prerequisite(
    name: str, rtype: str, values: Iterable[str]
) -> Prerequisite:
    rtype_norm = normalize_type(rtype)
    return (
        normalize_name(name, allow_wildcard=True),
        rtype_norm,
        frozenset(normalize_value(rtype_norm, v) for v in values),
    )


def normalize_operation(
    op: str, name: str, rtype: str, value: "str | None"
) -> Operation:
    op_norm = _OP_ALIASES.get(op.strip().lower())
    if op_norm is None:
        raise ZoneError(f"unknown operation {op!r}")
    if op_norm in (ADD, DELETE):
        if value is None:
            raise ZoneError(f"operation {op_norm!r} requires a value")
        nname, rtype_norm, nvalue = normalize_record(name, rtype, value)
        return op_norm, nname, rtype_norm, nvalue
    return op_norm, normalize_name(name, allow_wildcard=True), normalize_type(rtype), None


def check_prerequisites(
    records: set[Record], prerequisites: Iterable[Prerequisite]
) -> "str | None":
    """Check every prerequisite against the pre-update snapshot.

    Each prerequisite names the complete values set for a (name, type) pair;
    an empty set means the record set must not exist. Returns None when all
    prerequisites hold, otherwise a human-readable failure message.
    """
    current: dict[tuple[str, str], set[str]] = {}
    for name, rtype, value in records:
        current.setdefault((name, rtype), set()).add(value)
    for name, rtype, values in prerequisites:
        actual = current.get((name, rtype), set())
        if actual != set(values):
            return (
                f"prerequisite failed for {name} {rtype}: "
                f"expected values {sorted(values)}, found {sorted(actual)}"
            )
    return None


def apply_operations(
    records: set[Record], operations: Iterable[Operation]
) -> set[Record]:
    """Apply operations in order. Duplicate adds and deletes of missing
    items have no effect; only the resulting final set is validated later."""
    new = set(records)
    for op, name, rtype, value in operations:
        if op == ADD:
            new.add((name, rtype, value))
        elif op == DELETE:
            new.discard((name, rtype, value))
        elif op == DELETE_SET:
            new = {r for r in new if not (r[0] == name and r[1] == rtype)}
        else:  # pragma: no cover - normalize_operation rejects unknown ops
            raise ZoneError(f"unknown operation {op!r}")
    return new
