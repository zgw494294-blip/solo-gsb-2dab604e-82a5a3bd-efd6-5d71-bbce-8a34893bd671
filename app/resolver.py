"""Offline DNS zone resolution logic.

A queries and PTR (reverse) queries are handled. Names are case-insensitive
and normalized with a trailing dot. Wildcard matching follows RFC 1034
semantics: find the closest encloser (longest existing ancestor, empty
non-terminals included) and look for a ``*`` record directly below it.

Zone delegation: multiple NS records may be placed at any non-apex,
non-wildcard name ("delegation point"). NS targets are normalized to absolute
names and may live outside the zone. A name may not hold NS together with
A/CNAME/PTR records and delegations may not nest. The delegation boundary is
checked first, so a query at or below a delegation point yields a REFERRAL
even when an exact A record or an upper wildcard would otherwise match. An A
record at an in-subtree NS target acts as glue; glue is only returned inside a
referral and is never authoritative on its own.

PTR records belong to reverse zones (``in-addr.arpa.`` or a subdomain of it
with up to four octet labels). Their owners must be complete four-octet
reversed IPv4 names (``d.c.b.a.in-addr.arpa.``), never wildcards; multiple
PTR values per name are allowed and their targets may leave the zone. A PTR
record may not share its name with a CNAME or an NS record (sharing with A is
allowed). Wildcard, empty-node, CNAME and delegation rules are otherwise
identical to forward resolution.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Iterable

NOERROR = "NOERROR"
NODATA = "NODATA"
NXDOMAIN = "NXDOMAIN"
CNAME_LOOP = "CNAME_LOOP"
REFUSED = "REFUSED"
REFERRAL = "REFERRAL"

_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_OCTET_RE = re.compile(r"[0-9]+")

REVERSE_SUFFIX = "in-addr.arpa."


class ZoneError(ValueError):
    """Invalid zone data or invalid query."""


def normalize_name(name: str, *, allow_wildcard: bool = False) -> str:
    """Lowercase ``name`` and ensure a trailing dot. Raise ZoneError if invalid."""
    if not isinstance(name, str):
        raise ZoneError("domain name must be a string")
    n = name.strip().rstrip(".").lower()
    if not n:
        raise ZoneError("domain name must not be empty")
    wildcard = False
    if n.startswith("*."):
        if not allow_wildcard:
            raise ZoneError(f"wildcard not allowed here: {name!r}")
        wildcard = True
        n = n[2:]
        if not n:
            raise ZoneError("wildcard record must have a parent domain")
    elif "*" in n:
        raise ZoneError(f"'*' is only allowed as the leftmost label: {name!r}")
    for label in n.split("."):
        if not _LABEL_RE.fullmatch(label):
            raise ZoneError(f"invalid label {label!r} in domain name {name!r}")
    fqdn = ("*." if wildcard else "") + n + "."
    if len(fqdn) > 253:
        raise ZoneError(f"domain name too long: {name!r}")
    return fqdn


def _is_below(name: str, parent: str) -> bool:
    """True if ``name`` is a strict descendant of ``parent``."""
    return name.endswith("." + parent)


def _ipv4(address: str, *, where: str) -> str:
    try:
        return str(ipaddress.IPv4Address(address))
    except ipaddress.AddressValueError as exc:
        raise ZoneError(f"invalid IPv4 address {address!r} for {where}") from exc


def _check_octet(label: str, *, where: str) -> None:
    """Require a decimal IPv4 octet label: ``0``-``255``, no leading zeros."""
    if (
        not _OCTET_RE.fullmatch(label)
        or int(label) > 255
        or (len(label) > 1 and label.startswith("0"))
    ):
        raise ZoneError(f"invalid IPv4 octet label {label!r} in {where}")


def check_reverse_zone(zone_name: str) -> str:
    """Normalize ``zone_name`` and require a valid reverse lookup zone.

    Only ``in-addr.arpa.`` itself or a subdomain with up to four numeric
    IPv4 octet labels (each 0-255, no leading zeros) is accepted.
    """
    apex = normalize_name(zone_name)
    if apex == REVERSE_SUFFIX:
        return apex
    if not apex.endswith("." + REVERSE_SUFFIX):
        raise ZoneError(
            f"reverse zone must be {REVERSE_SUFFIX} or a subdomain of it: {zone_name!r}"
        )
    labels = apex[: -len("." + REVERSE_SUFFIX)].split(".")
    if len(labels) > 4:
        raise ZoneError(f"reverse zone {apex} has more than four octet labels")
    for label in labels:
        _check_octet(label, where=apex)
    return apex


def reverse_query_name(address: str) -> tuple[str, str]:
    """Return ``(canonical address, in-addr.arpa. query name)`` for ``address``.

    The query name lists the four octets in reverse order, e.g.
    ``192.0.2.1`` becomes ``1.2.0.192.in-addr.arpa.``.
    """
    try:
        canonical = str(ipaddress.IPv4Address(address))
    except ipaddress.AddressValueError as exc:
        raise ZoneError(f"invalid IPv4 address {address!r}") from exc
    qname = ".".join(reversed(canonical.split("."))) + "." + REVERSE_SUFFIX
    return canonical, qname


def _check_ptr_owner(name: str) -> None:
    """Validate a PTR owner: full four-octet reversed IPv4 name, no wildcard."""
    if name.startswith("*."):
        raise ZoneError(f"wildcard PTR records are not allowed: {name}")
    if not name.endswith("." + REVERSE_SUFFIX):
        raise ZoneError(f"PTR owner {name} is not under {REVERSE_SUFFIX}")
    labels = name[: -len("." + REVERSE_SUFFIX)].split(".")
    if len(labels) != 4:
        raise ZoneError(
            f"PTR owner {name} must be a full four-octet reversed IPv4 name"
        )
    for label in labels:
        _check_octet(label, where=name)


class Zone:
    """A DNS zone: authoritative records, delegations and glue.

    Only records above every delegation cut are authoritative. Anything below a
    cut is out of the zone's authority: A records there are kept only when they
    serve as glue for an NS target of the enclosing cut.
    """

    def __init__(self, apex: str):
        self.apex = apex
        # authoritative name -> {"A": [addresses]}, {"PTR": [targets]},
        # {"CNAME": target}, or any combination of A and PTR
        self.records: dict[str, dict[str, object]] = {}
        # every authoritative existing name, including empty non-terminals and
        # the apex, plus the delegation points themselves
        self.nodes: set[str] = {apex}
        # delegation point -> deduplicated, sorted NS targets
        self.delegations: dict[str, list[str]] = {}
        # delegation point -> {ns target -> [addresses]}
        self.glue: dict[str, dict[str, list[str]]] = {}

    def in_zone(self, name: str) -> bool:
        return name == self.apex or name.endswith("." + self.apex)

    def _add_node(self, name: str) -> None:
        node = name
        while True:
            self.nodes.add(node)
            if node == self.apex:
                return
            node = node.split(".", 1)[1]

    def covering_delegation(self, name: str) -> str | None:
        """Delegation point equal to or strictly above ``name``, if any.

        Cuts never nest, so at most one cut can match; the longest one wins.
        """
        for cut in self.delegations:
            if name == cut or _is_below(name, cut):
                return cut
        return None

    def add_a(self, name: str, address: str) -> None:
        addr = _ipv4(address, where=name)
        existing = self.records.get(name)
        if existing is not None and "CNAME" in existing:
            raise ZoneError(f"A record at {name} conflicts with existing CNAME record")
        rec = self.records.setdefault(name, {})
        addrs = rec.setdefault("A", [])
        if addr not in addrs:
            addrs.append(addr)
        self._add_node(name)

    def add_cname(self, name: str, target: str) -> None:
        if name in self.records:
            raise ZoneError(f"CNAME record at {name} conflicts with existing record(s)")
        if not self.in_zone(target):
            raise ZoneError(f"CNAME target {target} is outside zone {self.apex}")
        self.records[name] = {"CNAME": target}
        self._add_node(name)

    def add_ptr(self, name: str, target: str) -> None:
        existing = self.records.get(name)
        if existing is not None and "CNAME" in existing:
            raise ZoneError(f"PTR record at {name} conflicts with existing CNAME record")
        rec = self.records.setdefault(name, {})
        targets = rec.setdefault("PTR", [])
        if target not in targets:
            targets.append(target)
        self._add_node(name)


def build_zone(zone_name: str, records: Iterable[tuple[str, str, str]]) -> Zone:
    """Build and validate a Zone from ``(name, type, value)`` record tuples."""
    apex = normalize_name(zone_name)
    zone = Zone(apex)

    # name -> {"A": [values], "CNAME": [targets], "NS": [targets], "PTR": [targets]}
    parsed: dict[str, dict[str, list[str]]] = {}
    for raw_name, raw_type, raw_value in records:
        rtype = raw_type.strip().upper()
        if rtype not in ("A", "CNAME", "NS", "PTR"):
            raise ZoneError(
                f"unsupported record type {raw_type!r}; only A, CNAME, NS and PTR"
            )
        name = normalize_name(raw_name, allow_wildcard=True)
        if not zone.in_zone(name):
            raise ZoneError(f"record name {name} is outside zone {apex}")
        if rtype == "PTR":
            _check_ptr_owner(name)
        parsed.setdefault(name, {"A": [], "CNAME": [], "NS": [], "PTR": []})[
            rtype
        ].append(raw_value)

    cuts = [name for name, kinds in parsed.items() if kinds["NS"]]
    for cut in cuts:
        if cut == apex:
            raise ZoneError(f"NS records are not allowed at the zone apex {apex}")
        if cut.startswith("*."):
            raise ZoneError(f"wildcard NS records are not allowed: {cut}")
        kinds = parsed[cut]
        if kinds["A"]:
            raise ZoneError(f"NS record at {cut} conflicts with A record(s) at the same name")
        if kinds["CNAME"]:
            raise ZoneError(f"NS record at {cut} conflicts with CNAME record at the same name")
        if kinds["PTR"]:
            raise ZoneError(f"NS record at {cut} conflicts with PTR record(s) at the same name")

    def enclosing_cut(name: str) -> str | None:
        best = None
        for cut in cuts:
            if name == cut or _is_below(name, cut):
                if best is None or len(cut) > len(best):
                    best = cut
        return best

    # CNAME exclusivity also applies to inert records buried under a cut.
    # CNAME+NS can only occur at a cut name and is already rejected above.
    for name, kinds in parsed.items():
        if kinds["CNAME"] and kinds["A"]:
            raise ZoneError(f"CNAME record at {name} conflicts with A record(s)")
        if kinds["CNAME"] and kinds["PTR"]:
            raise ZoneError(f"CNAME record at {name} conflicts with PTR record(s)")

    # Populate delegations and glue; collect authoritative records.
    for cut in cuts:
        targets = sorted({normalize_name(raw_target) for raw_target in parsed[cut]["NS"]})
        zone.delegations[cut] = targets
        zone.glue[cut] = {target: [] for target in targets}
        zone._add_node(cut)

    for name, kinds in parsed.items():
        cut = enclosing_cut(name)
        if cut is None or name == cut:
            # Above every cut (or the cut node itself, which only carries NS).
            for value in kinds["A"]:
                zone.add_a(name, value)
            for raw_target in kinds["CNAME"]:
                zone.add_cname(name, normalize_name(raw_target))
            for raw_target in kinds["PTR"]:
                zone.add_ptr(name, normalize_name(raw_target))
            continue
        # Below a delegation cut: records are not authoritative. A records are
        # kept only as glue when the name is an in-subtree NS target; anything
        # else (other A records, CNAMEs, PTRs, ...) is inert.
        glue = zone.glue[cut]
        if name in glue:
            for value in kinds["A"]:
                addr = _ipv4(value, where=name)
                if addr not in glue[name]:
                    glue[name].append(addr)

    # Delegations cannot nest (this also rejects NS records below a cut).
    for cut in zone.delegations:
        for other in zone.delegations:
            if other != cut and _is_below(cut, other):
                raise ZoneError(
                    f"delegation at {cut} is nested inside delegation at {other}"
                )

    return zone


def _wildcard_records(zone: Zone, name: str) -> dict[str, object] | None:
    """Records of ``*.<closest encloser of name>``, or None.

    The closest encloser is the longest ancestor of ``name`` that exists in
    the zone. Empty non-terminals exist, so the search never crosses them to
    reach a wildcard higher up. Delegation points are nodes too, and the
    delegation boundary is checked before wildcarding, so a wildcard can never
    match a name at or below a cut.
    """
    node = name
    while node not in zone.nodes:
        node = node.split(".", 1)[1]
    return zone.records.get("*." + node)


def _referral(zone: Zone, cut: str, chain: list[str]) -> "Resolution":
    """Build the REFERRAL answer for delegation point ``cut``."""
    targets = list(zone.delegations[cut])
    glue = [
        {"name": target, "type": "A", "addresses": sorted(zone.glue[cut][target])}
        for target in targets
        if zone.glue[cut][target]
    ]
    return Resolution(
        status=REFERRAL,
        chain=list(chain),
        delegation=cut,
        nameservers=targets,
        glue=glue,
    )


@dataclass
class Resolution:
    status: str
    chain: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    hostnames: list[str] = field(default_factory=list)
    error: str | None = None
    delegation: str | None = None
    nameservers: list[str] = field(default_factory=list)
    glue: list[dict[str, object]] = field(default_factory=list)


def _walk(zone: Zone, qname: str, qtype: str) -> Resolution:
    """Walk ``qname`` (normalized, in-zone) looking for records of ``qtype``.

    Shared by A and PTR resolution: delegation cuts are checked first, then
    exact names, empty non-terminals and wildcards, following CNAMEs until a
    record set of the requested type, a dead end or a loop is found.
    """
    # The delegation boundary is checked before anything else, so neither an
    # exact record nor an upper wildcard can bypass it.
    cut = zone.covering_delegation(qname)
    if cut is not None:
        return _referral(zone, cut, [qname])

    chain = [qname]
    visited = {qname}
    current = qname
    while True:
        cut = zone.covering_delegation(current)
        if cut is not None:
            # The CNAME chain crossed into a delegated subtree: keep the chain
            # walked so far and refer instead of answering authoritatively.
            return _referral(zone, cut, chain)
        rec = zone.records.get(current)
        if rec is None:
            if current in zone.nodes:
                # empty non-terminal: exists, but holds no records
                return Resolution(status=NODATA, chain=chain)
            rec = _wildcard_records(zone, current)
            if rec is None:
                return Resolution(status=NXDOMAIN, chain=chain)
        values = rec.get(qtype)
        if values:
            if qtype == "PTR":
                return Resolution(
                    status=NOERROR, chain=chain, hostnames=sorted(values)
                )
            return Resolution(status=NOERROR, chain=chain, addresses=list(values))
        target = rec.get("CNAME")
        if target is None:
            # the name exists but holds no records of the requested type
            return Resolution(status=NODATA, chain=chain)
        chain.append(target)
        if target in visited:
            return Resolution(
                status=CNAME_LOOP,
                chain=chain,
                error="CNAME loop detected: " + " -> ".join(chain),
            )
        visited.add(target)
        current = target


def resolve(zone: Zone, query: str, qtype: str = "A") -> Resolution:
    """Resolve an A query for ``query`` inside ``zone``."""
    if qtype.strip().upper() != "A":
        raise ZoneError(f"unsupported query type {qtype!r}; only A queries are handled")
    qname = normalize_name(query)
    if not zone.in_zone(qname):
        return Resolution(
            status=REFUSED,
            error=f"query name {qname} is outside zone {zone.apex}",
        )
    return _walk(zone, qname, "A")


def resolve_ptr(zone: Zone, query: str) -> Resolution:
    """Resolve a PTR query for the reverse lookup name ``query``."""
    qname = normalize_name(query)
    if not zone.in_zone(qname):
        return Resolution(
            status=REFUSED,
            error=f"query name {qname} is outside zone {zone.apex}",
        )
    return _walk(zone, qname, "PTR")
