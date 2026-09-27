"""Parser for the DNS master file subset accepted by POST /zones/import.

Supported grammar (a strict subset of RFC 1035 master files):

- blank lines and ``;`` comments (a comment runs to the end of the line);
- non-nested parentheses joining one logical record across physical lines;
- ``$ORIGIN`` and ``$TTL`` directives (every other directive is rejected);
- A, CNAME, NS and PTR records only, with an optional non-negative decimal
  TTL and an optional ``IN`` class before the type, in any order;
- ``@`` for the current origin; owner names, CNAME/NS/PTR targets and the
  ``$ORIGIN`` argument without a trailing dot are relative to the current
  origin (initially the zone itself);
- a logical record whose first character is whitespace omits the owner
  name and reuses the previous record's already expanded name; switching
  origin does not change that name, and omitting the name on the first
  record is an error.

Quoted strings and escape sequences are rejected. TTLs (record-level and
``$TTL``) are validated but not stored. The returned ``(name, type,
value)`` tuples are expanded to absolute names but otherwise unvalidated:
normalization and zone validation are shared with POST /zones.
"""
from __future__ import annotations

import re

from .resolver import ZoneError, normalize_name

_TTL_RE = re.compile(r"[0-9]+")
_CLASS = "IN"
_TYPES = ("A", "CNAME", "NS", "PTR")


class MasterFileError(ValueError):
    """Syntax error in the imported master file.

    ``line`` is the 1-based number of the physical line where the
    offending logical record starts.
    """

    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.message = message
        self.line = line


def _logical_records(text: str):
    """Yield ``(start_line, leading_blank, body)`` for each logical record.

    Comments are stripped per physical line; parentheses only join lines
    and stay in the body for the caller to drop. ``leading_blank`` tells
    whether the record's first character is whitespace (owner omitted).
    """
    buf = ""
    start = 0
    blank = False
    in_parens = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(";", 1)[0]
        if not in_parens:
            if not line.strip():
                continue
            buf = line
            start = lineno
            blank = line[0] in (" ", "\t")
        else:
            buf += " " + line
        for ch in line:
            if ch == "(":
                if in_parens:
                    raise MasterFileError("nested parentheses are not allowed", start)
                in_parens = True
            elif ch == ")":
                if not in_parens:
                    raise MasterFileError("unbalanced closing parenthesis", start)
                in_parens = False
        if not in_parens:
            if buf.strip():
                yield start, blank, buf
            buf = ""
    if in_parens:
        raise MasterFileError("unclosed parenthesis", start)


def _expand_name(token: str, origin: str) -> str:
    """Expand ``token`` to an absolute name against the current origin."""
    if token == "@":
        return origin
    if token.endswith("."):
        return token
    return f"{token}.{origin}"


def _check_ttl(token: str, start: int) -> None:
    if not _TTL_RE.fullmatch(token):
        raise MasterFileError(
            f"invalid TTL {token!r}; want a non-negative decimal", start
        )


def parse_zone_file(zone: str, text: str) -> list[tuple[str, str, str]]:
    """Parse ``text`` (master file subset) into ``(name, type, value)`` tuples.

    Owner names and CNAME/NS/PTR targets are expanded to absolute names using
    the running origin (initially ``zone``). The tuples are not normalized
    or validated beyond syntax; that is shared with POST /zones.
    """
    origin = normalize_name(zone)  # invalid zone -> ZoneError, as in POST /zones
    records: list[tuple[str, str, str]] = []
    last_name: "str | None" = None
    for start, blank, body in _logical_records(text):
        if '"' in body or "\\" in body:
            raise MasterFileError(
                "quoted strings and escapes are not supported", start
            )
        tokens = body.replace("(", " ").replace(")", " ").split()
        if not tokens:
            raise MasterFileError("empty record", start)
        head = tokens[0]
        if head.startswith("$"):
            directive = head.upper()
            if directive == "$ORIGIN":
                if len(tokens) != 2:
                    raise MasterFileError("$ORIGIN takes exactly one argument", start)
                try:
                    origin = normalize_name(_expand_name(tokens[1], origin))
                except ZoneError as exc:
                    raise MasterFileError(f"invalid $ORIGIN: {exc}", start) from exc
            elif directive == "$TTL":
                if len(tokens) != 2:
                    raise MasterFileError("$TTL takes exactly one argument", start)
                _check_ttl(tokens[1], start)
            else:
                raise MasterFileError(f"unsupported directive {head!r}", start)
            continue
        if blank:
            if last_name is None:
                raise MasterFileError("first record omits the owner name", start)
            name = last_name
            rest = tokens
        else:
            name = _expand_name(head, origin)
            last_name = name
            rest = tokens[1:]
        ttl_seen = False
        class_seen = False
        while rest:
            tok = rest[0]
            if _TTL_RE.fullmatch(tok):
                if ttl_seen:
                    raise MasterFileError("duplicate TTL", start)
                ttl_seen = True
            elif tok.upper() == _CLASS:
                if class_seen:
                    raise MasterFileError("duplicate IN class", start)
                class_seen = True
            else:
                break
            rest.pop(0)
        if not rest:
            raise MasterFileError("missing record type", start)
        rtype = rest.pop(0).upper()
        if rtype not in _TYPES:
            raise MasterFileError(
                f"unsupported record type {rtype!r}; only A, CNAME, NS and PTR", start
            )
        if not rest:
            raise MasterFileError(f"missing value for {rtype} record", start)
        if len(rest) > 1:
            raise MasterFileError(
                f"unexpected extra tokens after {rtype} value:"
                f" {' '.join(rest[1:])}",
                start,
            )
        value = rest[0]
        if rtype != "A":
            value = _expand_name(value, origin)
        records.append((name, rtype, value))
    return records
