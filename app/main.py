"""FastAPI surface for the offline DNS zone resolver."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import AliasChoices, BaseModel, Field

from . import store as zone_store
from .resolver import (
    Resolution,
    ZoneError,
    build_zone,
    check_reverse_zone,
    normalize_name,
    resolve,
    resolve_ptr,
    reverse_query_name,
)
from .zonefile import MasterFileError, parse_zone_file
from .zoneupdate import (
    normalize_operation,
    normalize_prerequisite,
    normalize_record,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    zone_store.get_store()  # create the database file and tables on startup
    yield


app = FastAPI(
    title="Offline DNS Zone Resolver",
    version="1.0.0",
    summary="Resolve A and reverse PTR queries against a zone supplied in the "
    "request body.",
    lifespan=lifespan,
)


class RecordIn(BaseModel):
    name: str = Field(..., examples=["www.example.com"])
    type: str = Field(..., examples=["A"], description="A, CNAME, NS or PTR")
    value: str = Field(
        ...,
        examples=["192.0.2.1"],
        description="IPv4 address, or CNAME/NS/PTR target name",
    )


class GlueRecord(BaseModel):
    name: str = Field(..., description="Nameserver target name (absolute)")
    type: str = Field("A", description="Glue record type; only A is supported")
    addresses: list[str] = Field(default_factory=list, description="Glue IPv4 addresses")


class ResolveRequest(BaseModel):
    zone: str = Field(..., examples=["example.com"])
    records: list[RecordIn] = Field(default_factory=list)
    query: str = Field(..., examples=["www.example.com"])
    qtype: str = Field(default="A", description="Only A is supported")


class ResolveResponse(BaseModel):
    zone: str
    query: str
    status: str = Field(
        ...,
        description="NOERROR | NODATA | NXDOMAIN | CNAME_LOOP | REFUSED | REFERRAL",
    )
    chain: list[str] = Field(..., description="Names visited while following CNAMEs")
    addresses: list[str] = Field(default_factory=list, description="Final IPv4 addresses")
    delegation: str | None = Field(
        default=None,
        description="REFERRAL only: the delegation point that owns the answer",
    )
    nameservers: list[str] = Field(
        default_factory=list,
        description="REFERRAL only: deduplicated, sorted NS targets",
    )
    glue: list[GlueRecord] = Field(
        default_factory=list,
        description="REFERRAL only: glue records for in-subtree NS targets",
    )
    error: str | None = None


@app.get("/")
def index() -> dict[str, str]:
    return {
        "service": "offline-dns-resolver",
        "docs": "/docs",
        "resolve": "POST /resolve",
        "reverse": "POST /reverse",
        "zones": "POST /zones",
        "import": "POST /zones/import",
    }


@app.post("/resolve", response_model=ResolveResponse)
def resolve_endpoint(req: ResolveRequest) -> ResolveResponse:
    try:
        qname = normalize_name(req.query)
        zone = build_zone(req.zone, [(r.name, r.type, r.value) for r in req.records])
        result: Resolution = resolve(zone, qname, req.qtype)
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ResolveResponse(
        zone=zone.apex,
        query=qname,
        status=result.status,
        chain=result.chain,
        addresses=result.addresses,
        delegation=result.delegation,
        nameservers=result.nameservers,
        glue=result.glue,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Reverse (PTR) lookups
# ---------------------------------------------------------------------------


class ReverseRequest(BaseModel):
    zone: str = Field(
        ...,
        examples=["168.192.in-addr.arpa"],
        description="in-addr.arpa. or a subdomain with up to four IPv4 octet labels",
    )
    records: list[RecordIn] = Field(default_factory=list)
    address: str = Field(..., examples=["192.168.1.10"], description="IPv4 address")


class ReverseResponse(BaseModel):
    zone: str
    address: str = Field(..., description="Canonical form of the queried IPv4 address")
    query: str = Field(..., description="Generated in-addr.arpa. reverse lookup name")
    status: str = Field(
        ...,
        description="NOERROR | NODATA | NXDOMAIN | CNAME_LOOP | REFUSED | REFERRAL",
    )
    chain: list[str] = Field(..., description="Names visited while following CNAMEs")
    hostnames: list[str] = Field(
        default_factory=list,
        description="Deduplicated, sorted PTR targets; empty unless NOERROR",
    )
    delegation: str | None = Field(
        default=None,
        description="REFERRAL only: the delegation point that owns the answer",
    )
    nameservers: list[str] = Field(
        default_factory=list,
        description="REFERRAL only: deduplicated, sorted NS targets",
    )
    glue: list[GlueRecord] = Field(
        default_factory=list,
        description="REFERRAL only: glue records for in-subtree NS targets",
    )
    error: str | None = None


@app.post("/reverse", response_model=ReverseResponse)
def reverse_endpoint(req: ReverseRequest) -> ReverseResponse:
    try:
        apex = check_reverse_zone(req.zone)
        address, qname = reverse_query_name(req.address)
        zone = build_zone(apex, [(r.name, r.type, r.value) for r in req.records])
        result: Resolution = resolve_ptr(zone, qname)
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ReverseResponse(
        zone=zone.apex,
        address=address,
        query=qname,
        status=result.status,
        chain=result.chain,
        hostnames=result.hostnames,
        delegation=result.delegation,
        nameservers=result.nameservers,
        glue=result.glue,
        error=result.error,
    )


# ---------------------------------------------------------------------------
# Zone snapshots with conditional updates
# ---------------------------------------------------------------------------


class ZoneCreateRequest(BaseModel):
    zone: str = Field(..., examples=["example.com"])
    records: list[RecordIn] = Field(default_factory=list)


class PrerequisiteIn(BaseModel):
    name: str = Field(..., examples=["www.example.com"])
    type: str = Field(..., examples=["A"])
    values: list[str] = Field(
        default_factory=list,
        description="Complete values set for (name, type); empty means the "
        "record set must not exist",
    )


class OperationIn(BaseModel):
    op: str = Field(
        ...,
        validation_alias=AliasChoices("op", "action"),
        description="add | delete | delete_set",
    )
    name: str
    type: str
    value: str | None = Field(
        default=None, description="Required for add/delete, ignored for delete_set"
    )


class ZoneUpdateRequest(BaseModel):
    expected_version: int = Field(..., ge=0)
    prerequisites: list[PrerequisiteIn] = Field(default_factory=list)
    operations: list[OperationIn] = Field(default_factory=list)


class ZoneSnapshot(BaseModel):
    id: str
    version: int
    zone: str
    records: list[RecordIn] = Field(
        default_factory=list,
        description="Normalized, deduplicated records sorted by (name, type, value); "
        "usable directly as the records of POST /resolve",
    )


class ZoneDiff(BaseModel):
    zone: str
    from_version: int
    to_version: int
    removed: list[RecordIn] = Field(
        default_factory=list,
        description="Records present at from_version but not at to_version, "
        "sorted by (name, type, value)",
    )
    added: list[RecordIn] = Field(
        default_factory=list,
        description="Records present at to_version but not at from_version, "
        "sorted by (name, type, value)",
    )


def _snapshot_response(snap: zone_store.Snapshot) -> ZoneSnapshot:
    return ZoneSnapshot(
        id=snap.id,
        version=snap.version,
        zone=snap.zone,
        records=[RecordIn(name=n, type=t, value=v) for n, t, v in snap.records],
    )


@app.post("/zones", response_model=ZoneSnapshot)
def create_zone(req: ZoneCreateRequest) -> ZoneSnapshot:
    try:
        records = {normalize_record(r.name, r.type, r.value) for r in req.records}
        zone = build_zone(req.zone, sorted(records))
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _snapshot_response(zone_store.get_store().create(zone.apex, records))


class ZoneImportRequest(BaseModel):
    zone: str = Field(..., examples=["example.com"])
    text: str = Field(
        ...,
        description="DNS master file subset: A/CNAME/NS/PTR records, $ORIGIN/$TTL "
        "directives, ';' comments and parentheses",
        examples=["$TTL 3600\nwww A 192.0.2.1\n"],
    )


@app.post("/zones/import", response_model=ZoneSnapshot)
def import_zone(req: ZoneImportRequest) -> ZoneSnapshot:
    try:
        parsed = parse_zone_file(req.zone, req.text)
        records = {normalize_record(n, t, v) for n, t, v in parsed}
        zone = build_zone(req.zone, sorted(records))
    except MasterFileError as exc:
        raise HTTPException(
            status_code=422,
            detail={"line": exc.line, "message": exc.message},
        ) from exc
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _snapshot_response(zone_store.get_store().create(zone.apex, records))


@app.get("/zones/{zone_id}", response_model=ZoneSnapshot)
def get_zone(zone_id: str) -> ZoneSnapshot:
    snap = zone_store.get_store().get(zone_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"unknown zone id {zone_id!r}")
    return _snapshot_response(snap)


@app.get("/zones/{zone_id}/diff", response_model=ZoneDiff)
def diff_zone(
    zone_id: str,
    from_version: int = Query(..., ge=0),
    to_version: int = Query(..., ge=0),
) -> ZoneDiff:
    if from_version > to_version:
        raise HTTPException(
            status_code=422,
            detail="from_version must not be greater than to_version",
        )
    try:
        diff = zone_store.get_store().diff(zone_id, from_version, to_version)
    except zone_store.ZoneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"unknown zone id {zone_id!r}"
        ) from exc
    except zone_store.VersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ZoneDiff(
        zone=diff.zone,
        from_version=diff.from_version,
        to_version=diff.to_version,
        removed=[RecordIn(name=n, type=t, value=v) for n, t, v in diff.removed],
        added=[RecordIn(name=n, type=t, value=v) for n, t, v in diff.added],
    )


# ---------------------------------------------------------------------------
# Impact analysis: probe evaluation across two archived versions
# ---------------------------------------------------------------------------


class ProbeIn(BaseModel):
    type: str = Field(..., description="A (forward query) or PTR (reverse query)")
    name: str | None = Field(
        default=None,
        examples=["www.example.com"],
        description="A probes: the query name",
    )
    address: str | None = Field(
        default=None,
        examples=["192.0.2.1"],
        description="PTR probes: the IPv4 address to reverse-map",
    )


class ImpactRequest(BaseModel):
    from_version: int = Field(..., ge=0)
    to_version: int = Field(..., ge=0)
    probes: list[ProbeIn] = Field(default_factory=list)


class ForwardImpact(BaseModel):
    status: str
    chain: list[str]
    addresses: list[str] = Field(default_factory=list)
    delegation: str | None = None
    nameservers: list[str] = Field(default_factory=list)
    glue: list[GlueRecord] = Field(default_factory=list)
    error: str | None = None


class ReverseImpact(BaseModel):
    status: str
    chain: list[str]
    hostnames: list[str] = Field(default_factory=list)
    delegation: str | None = None
    nameservers: list[str] = Field(default_factory=list)
    glue: list[GlueRecord] = Field(default_factory=list)
    error: str | None = None


class ImpactItem(BaseModel):
    type: str
    query: str = Field(..., description="Normalized query name")
    address: str | None = Field(
        default=None, description="PTR probes only: canonical IPv4 address"
    )
    before: ForwardImpact | ReverseImpact = Field(
        ..., description="Outcome evaluated at from_version"
    )
    after: ForwardImpact | ReverseImpact = Field(
        ..., description="Outcome evaluated at to_version"
    )
    changed: bool = Field(
        ...,
        description="True when status, chain, addresses/hostnames, delegation, "
        "nameservers or glue differ between the two versions",
    )


class ZoneImpact(BaseModel):
    id: str
    zone: str
    from_version: int
    to_version: int
    results: list[ImpactItem] = Field(
        default_factory=list,
        description="One entry per probe, in input order, duplicates kept",
    )


def _normalize_probe(probe: ProbeIn) -> tuple[str, str, "str | None"]:
    """Validate one probe; return (type, normalized query name, address)."""
    ptype = probe.type.strip().upper()
    if ptype == "A":
        if probe.name is None:
            raise ZoneError("A probe requires a 'name'")
        return "A", normalize_name(probe.name), None
    if ptype == "PTR":
        if probe.address is None:
            raise ZoneError("PTR probe requires an 'address'")
        address, qname = reverse_query_name(probe.address)
        return "PTR", qname, address
    raise ZoneError(f"unsupported probe type {probe.type!r}; only A and PTR")


def _impact_key(result: Resolution) -> tuple:
    """The fields that decide whether two outcomes differ (error excluded)."""
    return (
        result.status,
        tuple(result.chain),
        tuple(result.addresses),
        tuple(result.hostnames),
        result.delegation,
        tuple(result.nameservers),
        tuple((g["name"], g["type"], tuple(g["addresses"])) for g in result.glue),
    )


def _forward_impact(result: Resolution) -> ForwardImpact:
    return ForwardImpact(
        status=result.status,
        chain=result.chain,
        addresses=result.addresses,
        delegation=result.delegation,
        nameservers=result.nameservers,
        glue=[GlueRecord(**g) for g in result.glue],
        error=result.error,
    )


def _reverse_impact(result: Resolution) -> ReverseImpact:
    return ReverseImpact(
        status=result.status,
        chain=result.chain,
        hostnames=result.hostnames,
        delegation=result.delegation,
        nameservers=result.nameservers,
        glue=[GlueRecord(**g) for g in result.glue],
        error=result.error,
    )


@app.post("/zones/{zone_id}/impact", response_model=ZoneImpact)
def impact_zone(zone_id: str, req: ImpactRequest) -> ZoneImpact:
    # Validate the whole batch first: a single bad probe fails the request.
    try:
        probes = [_normalize_probe(p) for p in req.probes]
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = zone_store.get_store()
    try:
        before_snap = store.snapshot_at(zone_id, req.from_version)
        after_snap = store.snapshot_at(zone_id, req.to_version)
    except zone_store.ZoneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"unknown zone id {zone_id!r}"
        ) from exc
    except zone_store.VersionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if any(ptype == "PTR" for ptype, _, _ in probes):
        try:
            check_reverse_zone(before_snap.zone)
        except ZoneError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    before_zone = build_zone(before_snap.zone, before_snap.records)
    after_zone = build_zone(after_snap.zone, after_snap.records)
    results = []
    for ptype, qname, address in probes:
        if ptype == "A":
            before = resolve(before_zone, qname, "A")
            after = resolve(after_zone, qname, "A")
            item = ImpactItem(
                type="A",
                query=qname,
                before=_forward_impact(before),
                after=_forward_impact(after),
                changed=_impact_key(before) != _impact_key(after),
            )
        else:
            before = resolve_ptr(before_zone, qname)
            after = resolve_ptr(after_zone, qname)
            item = ImpactItem(
                type="PTR",
                query=qname,
                address=address,
                before=_reverse_impact(before),
                after=_reverse_impact(after),
                changed=_impact_key(before) != _impact_key(after),
            )
        results.append(item)
    return ZoneImpact(
        id=zone_id,
        zone=before_snap.zone,
        from_version=req.from_version,
        to_version=req.to_version,
        results=results,
    )


@app.post("/zones/{zone_id}/updates", response_model=ZoneSnapshot)
def update_zone(zone_id: str, req: ZoneUpdateRequest) -> ZoneSnapshot:
    try:
        prerequisites = [
            normalize_prerequisite(p.name, p.type, p.values) for p in req.prerequisites
        ]
        operations = [
            normalize_operation(o.op, o.name, o.type, o.value) for o in req.operations
        ]
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        snap = zone_store.get_store().apply_update(
            zone_id, req.expected_version, prerequisites, operations
        )
    except zone_store.ZoneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"unknown zone id {zone_id!r}"
        ) from exc
    except (zone_store.VersionConflict, zone_store.PrerequisiteFailed) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ZoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _snapshot_response(snap)
