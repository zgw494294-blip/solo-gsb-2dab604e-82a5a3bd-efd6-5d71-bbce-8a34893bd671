"""SQLite persistence for zone snapshots with conditional, atomic updates.

A zone snapshot keeps the full input record set (records below a delegation
cut included, even though they are not authoritative) so it can be fed back
into ``POST /resolve`` or modified by later updates. Every committed
version is archived: creation stores version 0 and each successful update
stores the new version in the same write transaction, so a failed update
never leaves history behind. Zones that predate history tracking get a
single baseline at their current version; earlier versions are not
reconstructed. Tables are created automatically on first use; the database
path comes from ``DNS_DB_PATH`` (default ``./zones.db``).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from .resolver import build_zone
from .zoneupdate import (
    Operation,
    Prerequisite,
    Record,
    apply_operations,
    check_prerequisites,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id TEXT PRIMARY KEY,
    zone TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS zone_records (
    zone_id TEXT NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE (zone_id, name, type, value)
);
CREATE TABLE IF NOT EXISTS zone_versions (
    zone_id TEXT NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    PRIMARY KEY (zone_id, version)
);
CREATE TABLE IF NOT EXISTS zone_version_records (
    zone_id TEXT NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE (zone_id, version, name, type, value)
);
"""


class ZoneNotFound(KeyError):
    """Unknown zone id."""


class VersionConflict(RuntimeError):
    """expected_version does not match the stored version."""


class PrerequisiteFailed(RuntimeError):
    """A prerequisite does not hold for the current snapshot."""


class VersionNotFound(KeyError):
    """The zone has no archived snapshot for the requested version."""


@dataclass
class Snapshot:
    id: str
    zone: str
    version: int
    records: list[Record] = field(default_factory=list)  # sorted by (name, type, value)


@dataclass
class Diff:
    zone: str
    from_version: int
    to_version: int
    removed: list[Record] = field(default_factory=list)  # sorted by (name, type, value)
    added: list[Record] = field(default_factory=list)  # sorted by (name, type, value)


class ZoneStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._backfill_baselines()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _write_txn(self):
        # BEGIN IMMEDIATE takes the database write lock up front, so
        # concurrent writers serialize here: for a given expected_version at
        # most one transaction can commit.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _archive_version(self, zone_id: str, version: int, rows: list[Record]) -> None:
        """Insert the archived record set for one version (inside a write txn)."""
        self._conn.execute(
            "INSERT INTO zone_versions (zone_id, version) VALUES (?, ?)",
            (zone_id, version),
        )
        self._conn.executemany(
            "INSERT INTO zone_version_records (zone_id, version, name, type, value)"
            " VALUES (?, ?, ?, ?, ?)",
            [(zone_id, version, n, t, v) for n, t, v in rows],
        )

    def _backfill_baselines(self) -> None:
        """Archive a baseline for zones that predate version history.

        Zones stored before history tracking was introduced get a single
        archived snapshot at their current version; earlier versions are
        not reconstructed.
        """
        with self._write_txn():
            rows = self._conn.execute(
                "SELECT id, version FROM zones z"
                " WHERE NOT EXISTS"
                " (SELECT 1 FROM zone_versions v WHERE v.zone_id = z.id)"
            ).fetchall()
            for row in rows:
                records = [
                    (r["name"], r["type"], r["value"])
                    for r in self._conn.execute(
                        "SELECT name, type, value FROM zone_records WHERE zone_id = ?",
                        (row["id"],),
                    )
                ]
                self._archive_version(row["id"], row["version"], records)

    def create(self, apex: str, records: set[Record]) -> Snapshot:
        zone_id = uuid.uuid4().hex
        rows = sorted(records)
        with self._write_txn():
            self._conn.execute(
                "INSERT INTO zones (id, zone, version) VALUES (?, ?, 0)",
                (zone_id, apex),
            )
            self._conn.executemany(
                "INSERT INTO zone_records (zone_id, name, type, value)"
                " VALUES (?, ?, ?, ?)",
                [(zone_id, n, t, v) for n, t, v in rows],
            )
            self._archive_version(zone_id, 0, rows)
        return Snapshot(zone_id, apex, 0, rows)

    def get(self, zone_id: str) -> "Snapshot | None":
        with self._lock:
            row = self._conn.execute(
                "SELECT zone, version FROM zones WHERE id = ?", (zone_id,)
            ).fetchone()
            if row is None:
                return None
            rows = [
                (r["name"], r["type"], r["value"])
                for r in self._conn.execute(
                    "SELECT name, type, value FROM zone_records WHERE zone_id = ?"
                    " ORDER BY name, type, value",
                    (zone_id,),
                )
            ]
        return Snapshot(zone_id, row["zone"], row["version"], rows)

    def apply_update(
        self,
        zone_id: str,
        expected_version: int,
        prerequisites: list[Prerequisite],
        operations: list[Operation],
    ) -> Snapshot:
        """Check version and prerequisites against the pre-update snapshot,
        apply operations, validate the final zone and commit atomically.

        Any exception raised here rolls the transaction back, so a failed
        update never changes the stored data or the version.
        """
        with self._write_txn():
            row = self._conn.execute(
                "SELECT zone, version FROM zones WHERE id = ?", (zone_id,)
            ).fetchone()
            if row is None:
                raise ZoneNotFound(zone_id)
            if row["version"] != expected_version:
                raise VersionConflict(
                    f"expected version {expected_version}, "
                    f"current version is {row['version']}"
                )
            current = {
                (r["name"], r["type"], r["value"])
                for r in self._conn.execute(
                    "SELECT name, type, value FROM zone_records WHERE zone_id = ?",
                    (zone_id,),
                )
            }
            failure = check_prerequisites(current, prerequisites)
            if failure is not None:
                raise PrerequisiteFailed(failure)
            new_records = apply_operations(current, operations)
            # Intermediate states may conflict; only the final zone is
            # validated. ZoneError propagates and rolls everything back.
            build_zone(row["zone"], sorted(new_records))
            cur = self._conn.execute(
                "UPDATE zones SET version = version + 1 WHERE id = ? AND version = ?",
                (zone_id, expected_version),
            )
            if cur.rowcount != 1:  # pragma: no cover - serialized by the write lock
                raise VersionConflict(f"zone {zone_id!r} was modified concurrently")
            rows = sorted(new_records)
            self._conn.execute(
                "DELETE FROM zone_records WHERE zone_id = ?", (zone_id,)
            )
            self._conn.executemany(
                "INSERT INTO zone_records (zone_id, name, type, value)"
                " VALUES (?, ?, ?, ?)",
                [(zone_id, n, t, v) for n, t, v in rows],
            )
            # Archived in the same transaction: a failed update rolls back
            # and never leaves a history version behind.
            self._archive_version(zone_id, expected_version + 1, rows)
            return Snapshot(zone_id, row["zone"], expected_version + 1, rows)

    def diff(self, zone_id: str, from_version: int, to_version: int) -> Diff:
        """Set difference between two archived versions of a zone.

        Both versions must have been archived. The result is computed from
        the two complete record sets (delegation-subtree records included)
        and sorted by (name, type, value); diffing a version against itself
        yields empty ``removed``/``added`` lists.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT zone FROM zones WHERE id = ?", (zone_id,)
            ).fetchone()
            if row is None:
                raise ZoneNotFound(zone_id)
            for version in (from_version, to_version):
                archived = self._conn.execute(
                    "SELECT 1 FROM zone_versions WHERE zone_id = ? AND version = ?",
                    (zone_id, version),
                ).fetchone()
                if archived is None:
                    raise VersionNotFound(
                        f"zone {zone_id!r} has no archived version {version}"
                    )

            def records_at(version: int) -> set[Record]:
                return {
                    (r["name"], r["type"], r["value"])
                    for r in self._conn.execute(
                        "SELECT name, type, value FROM zone_version_records"
                        " WHERE zone_id = ? AND version = ?",
                        (zone_id, version),
                    )
                }

            before = records_at(from_version)
            after = records_at(to_version)
        return Diff(
            zone=row["zone"],
            from_version=from_version,
            to_version=to_version,
            removed=sorted(before - after),
            added=sorted(after - before),
        )


_store: "ZoneStore | None" = None
_store_guard = threading.Lock()


def get_store() -> ZoneStore:
    global _store
    with _store_guard:
        if _store is None:
            _store = ZoneStore(os.environ.get("DNS_DB_PATH", "zones.db"))
        return _store


def reset_store() -> None:
    """Drop the cached store (used by tests to switch database files)."""
    global _store
    with _store_guard:
        if _store is not None:
            _store.close()
            _store = None
