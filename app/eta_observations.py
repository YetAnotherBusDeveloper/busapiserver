"""Passive store of the ETA predictions the realtime service already fetches.

Nothing here talks to TDX. ``RealtimeService`` hands over the raw rows of a
refresh it was doing anyway; a background thread writes them to a SQLite file
of its own, so a slow or broken disk can cost observations but never a
response. No request, account, device or address ever reaches this module:
``submit`` takes a city, a route and upstream rows, nothing else.

What a stored snapshot is: every N1 row TDX returned for one route in one
refresh. ``(city, routeid, source, src_update_at)`` names it, with two caveats
recorded on the row rather than assumed away:

* ``truncated`` - the batch request hit its ``$top`` cap, so rows for this
  route may be missing. A later, larger copy of the same snapshot replaces it.
* ``src_update_min_at`` - feeds that push per-vehicle updates carry a different
  source time on each row; ``src_update_at`` is the newest and each prediction
  keeps its own lag in ``src_age``.
"""

from __future__ import annotations

import errno
import glob
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Callable, Iterable
from urllib.parse import quote

from app.logging_utils import get_logger


LOGGER = get_logger("eta_observations")

SCHEMA_VERSION = 1
SOURCE_TDX = "tdx"

# auto_vacuum only takes effect on a file that has no tables yet and is not in
# WAL mode, so it has to be the first statement a new database ever sees.
_PRAGMA_NEW_FILE = "PRAGMA auto_vacuum = INCREMENTAL;"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS eta_snapshots (
    snapshot_id       INTEGER PRIMARY KEY,
    city              TEXT NOT NULL,
    routeid           TEXT NOT NULL,
    source            TEXT NOT NULL,
    src_update_at     INTEGER NOT NULL,  -- newest upstream SrcUpdateTime in the snapshot
    src_update_min_at INTEGER NOT NULL,  -- oldest; equal to src_update_at for whole-package feeds
    tdx_update_at     INTEGER,           -- TDX UpdateTime
    observed_at       INTEGER NOT NULL,  -- when this server received it
    stop_rows         INTEGER NOT NULL,  -- upstream rows for the route
    eta_rows          INTEGER NOT NULL,  -- predictions stored
    truncated         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (city, routeid, source, src_update_at)
);
CREATE INDEX IF NOT EXISTS idx_eta_snapshots_src_update_at ON eta_snapshots(src_update_at);

CREATE TABLE IF NOT EXISTS eta_predictions (
    snapshot_id         INTEGER NOT NULL,
    subroute_uid        TEXT NOT NULL,
    direction           INTEGER NOT NULL,
    stop_seq            INTEGER NOT NULL,
    -- Position in this stop's list within this snapshot only. It is not an
    -- identity: the bus at position 0 now is a different bus a minute later.
    -- Follow a vehicle across snapshots by plate, never by this column.
    list_pos            INTEGER NOT NULL,
    stop_uid            TEXT NOT NULL,
    plate               TEXT,              -- NULL when upstream gave none
    eta_seconds         INTEGER NOT NULL,  -- EstimateTime exactly as received
    stop_status         INTEGER,           -- StopStatus exactly as received
    vehicle_stop_status INTEGER,
    is_last_bus         INTEGER,
    src_age             INTEGER,           -- snapshot src_update_at minus this row's; NULL when 0
    PRIMARY KEY (snapshot_id, subroute_uid, direction, stop_seq, list_pos)
) WITHOUT ROWID;

-- Stops with no prediction (not yet departed, last bus gone, ...). One row per
-- change of (stop_status, next_bus_at), keyed by upstream time so two workers
-- seeing the same change store it once. A repeated state is harmless: a row
-- means "this was the state then", not "it changed then".
CREATE TABLE IF NOT EXISTS eta_stop_states (
    city          TEXT NOT NULL,
    subroute_uid  TEXT NOT NULL,
    direction     INTEGER NOT NULL,
    stop_seq      INTEGER NOT NULL,
    src_update_at INTEGER NOT NULL,
    routeid       TEXT NOT NULL,
    stop_uid      TEXT NOT NULL,
    observed_at   INTEGER NOT NULL,
    stop_status   INTEGER,
    next_bus_at   INTEGER,           -- NextBusTime shown while no bus is predicted
    PRIMARY KEY (city, subroute_uid, direction, stop_seq, src_update_at)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_eta_stop_states_src_update_at ON eta_stop_states(src_update_at);
"""

_HAS_PREDICTION = ("has-prediction",)
# Primary SQLite result codes (extended code & 0xFF).
_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_CORRUPT, _SQLITE_FULL, _SQLITE_NOTADB = 5, 6, 11, 13, 26
# Quarantined copies are never deleted here; stop making them at this many.
_MAX_QUARANTINED_COPIES = 3


@dataclass(frozen=True)
class EtaObservationConfig:
    db_path: Path
    cities: frozenset[str]
    retention_seconds: int = 30 * 86400
    # Budget for the database file plus its -wal and -shm companions.
    max_bytes: int = 1024**3
    # Stop writing while the volume has less than this free, whoever used it.
    min_free_bytes: int = 2 * 1024**3
    # Budget for every quarantined copy together, -wal and -shm included.
    # Copies are never deleted here, so this is what bounds them. 0 means a
    # damaged database is never set aside automatically.
    quarantine_max_bytes: int = 1024**3
    queue_capacity: int = 256
    batch_size: int = 64
    flush_interval: float = 0.5
    maintenance_interval: float = 600.0
    wal_limit_bytes: int = 16 * 1024**2


def _unix(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _plate(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned if cleaned and cleaned != "-1" else None


@dataclass
class _Prepared:
    city: str
    routeid: str
    observed_at: int
    src_update_at: int
    src_update_min_at: int
    tdx_update_at: int | None
    stop_rows: int
    truncated: bool
    predictions: list[tuple]
    states: list[tuple]
    predicted_stops: list[tuple]


def prepare_snapshot(
    city: str, routeid: str, items: Iterable[dict[str, Any]], *, observed_at: int, truncated: bool,
) -> _Prepared | None:
    """Turn raw TDX N1 rows for one route into rows to store.

    Returns None when no row carries an upstream time: without one the snapshot
    cannot be told apart from a re-read of the same data.
    """
    rows = [item for item in items if isinstance(item, dict)]
    row_times = [_unix(item.get("SrcUpdateTime")) or _unix(item.get("UpdateTime")) for item in rows]
    known = [value for value in row_times if value is not None]
    if not known:
        return None
    newest, oldest = max(known), min(known)
    tdx_times = [value for value in (_unix(item.get("UpdateTime")) for item in rows) if value is not None]

    predictions: dict[tuple, tuple] = {}
    states: dict[tuple, tuple] = {}
    for item, row_time in zip(rows, row_times):
        subroute = str(item.get("SubRouteUID") or item.get("RouteUID") or "")
        stop_uid = str(item.get("StopUID") or item.get("StopID") or "")
        seq = _int(item.get("StopSequence"))
        if not subroute or not stop_uid or seq is None:
            continue
        direction = _int(item.get("Direction"))
        direction = 255 if direction is None else direction
        status = _int(item.get("StopStatus"))
        age = None if row_time is None or row_time == newest else newest - row_time

        estimates = [e for e in (item.get("Estimates") or []) if isinstance(e, dict)]
        if not estimates and item.get("EstimateTime") is not None:
            estimates = [item]
        stored = 0
        for position, estimate in enumerate(estimates):
            eta = _int(estimate.get("EstimateTime"))
            if eta is None:
                continue
            last = estimate.get("IsLastBus")
            predictions[(subroute, direction, seq, position)] = (
                subroute, direction, seq, position, stop_uid, _plate(estimate.get("PlateNumb")), eta,
                status, _int(estimate.get("VehicleStopStatus")),
                None if last is None else int(bool(last)), age,
            )
            stored += 1
        if stored == 0:
            states[(subroute, direction, seq)] = (
                subroute, direction, seq, row_time if row_time is not None else newest,
                stop_uid, status, _unix(item.get("NextBusTime")),
            )
    return _Prepared(
        city=city, routeid=routeid, observed_at=observed_at, src_update_at=newest,
        src_update_min_at=oldest, tdx_update_at=max(tdx_times) if tdx_times else None,
        stop_rows=len(rows), truncated=truncated,
        predictions=list(predictions.values()), states=list(states.values()),
        predicted_stops=sorted({key[:3] for key in predictions}),
    )


def classify_error(error: BaseException) -> str:
    """Sort a storage failure into ``busy``, ``full``, ``suspect`` or ``other``.

    ``suspect`` only means SQLite said CORRUPT or NOTADB once. It is a reason
    to check the file, never on its own a reason to move it: see
    ``EtaObservationStore.quarantine``. Locks, a full disk and I/O errors
    (``other``) are retried later and leave the file where it is.
    """
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", None)
        if code is not None:
            primary = code & 0xFF
            if primary in (_SQLITE_BUSY, _SQLITE_LOCKED):
                return "busy"
            if primary == _SQLITE_FULL:
                return "full"
            if primary in (_SQLITE_CORRUPT, _SQLITE_NOTADB):
                return "suspect"
            return "other"
        # Errors raised without a result code (older bindings, wrappers).
        message = str(error).lower()
        if "locked" in message or "busy" in message:
            return "busy"
        if "database or disk is full" in message:
            return "full"
        if "malformed" in message or "not a database" in message:
            return "suspect"
        return "other"
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "full"
    return "other"


class EtaObservationStore:
    """The SQLite side. Used only from the collector's thread."""

    def __init__(
        self,
        config: EtaObservationConfig,
        *,
        clock: Callable[[], float] = time.time,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._connection: sqlite3.Connection | None = None
        # Cross-process "this database is in use" marker: an empty SQLite file
        # beside the database. Every store holds a shared lock on it for as
        # long as it has the database open; quarantine needs the exclusive
        # lock, so the file is never moved from under another worker. The OS
        # drops the lock when a process dies, however it dies.
        self._guard_path = Path(str(config.db_path) + "-guard")
        self._guard: sqlite3.Connection | None = None
        # Last stored (stop_status, next_bus_at) per stop, so an unchanged
        # "not yet departed" is not rewritten every refresh.
        self._last_state: dict[tuple, tuple] = {}
        self._seeded: set[tuple] = set()
        self.auto_vacuum = False

    # -- connection -------------------------------------------------------
    def open(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        path = self.config.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._hold_guard()
        connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        try:
            # Any number of processes may open the same new file at once. The
            # immediate transaction makes one of them create the schema while
            # the rest wait and then find it there.
            connection.execute("PRAGMA busy_timeout = 5000")
            if connection.execute("PRAGMA page_count").fetchone()[0] == 0:
                connection.execute(_PRAGMA_NEW_FILE)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(f"PRAGMA journal_size_limit = {int(self.config.wal_limit_bytes)}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError(f"eta observation db is schema {version}; this build knows {SCHEMA_VERSION}")
                for statement in _SCHEMA.split(";\n"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            self.auto_vacuum = connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        return connection

    def _hold_guard(self) -> None:
        if self._guard is not None:
            return
        guard = sqlite3.connect(self._guard_path, isolation_level=None, timeout=5.0, check_same_thread=False)
        try:
            # An open read transaction keeps the shared lock until close().
            guard.execute("BEGIN")
            guard.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()
        except BaseException:
            guard.close()
            raise
        self._guard = guard

    def close(self) -> None:
        connection, self._connection = self._connection, None
        guard, self._guard = self._guard, None
        for handle in (connection, guard):
            if handle is not None:
                try:
                    handle.close()
                except sqlite3.Error:
                    pass

    def _verify(self) -> str:
        """``healthy``, ``corrupt`` or ``unknown`` for the file as it is on disk."""
        uri = "file:" + quote(self.config.db_path.as_posix(), safe="/:") + "?mode=ro"
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
            if hasattr(connection, "setconfig"):
                # Looking must not fold the WAL into a file we may be about to
                # set aside as evidence.
                connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            rows = connection.execute("PRAGMA integrity_check(1)").fetchall()
        except sqlite3.Error as error:
            return "corrupt" if classify_error(error) == "suspect" else "unknown"
        finally:
            if connection is not None:
                connection.close()
        return "healthy" if rows == [("ok",)] else "corrupt"

    # The next three only list a directory: safe from any thread.
    def _quarantined_files(self) -> list[Path]:
        path = self.config.db_path
        return list(path.parent.glob(f"{glob.escape(path.name)}.corrupt-*"))

    def quarantined_copies(self) -> int:
        return sum(1 for entry in self._quarantined_files() if not entry.name.endswith(("-wal", "-shm")))

    def quarantine_bytes(self) -> int:
        """Bytes held by every quarantined copy, ``-wal``/``-shm`` included."""
        total = 0
        for entry in self._quarantined_files():
            try:
                total += entry.stat().st_size
            except FileNotFoundError:
                pass  # removed by an operator since it was listed
        return total

    def quarantine(self) -> tuple[str, Path | None]:
        """Set a damaged database aside, if it really is damaged and nobody else has it open.

        Returns ``("moved", copy)`` or, with the file left exactly where it
        was, ``("in_use", None)`` (another store still has it open),
        ``("healthy", None)`` (it passes an integrity check, e.g. another
        worker already replaced it), ``("unknown", None)`` (it could not be
        checked: locked, I/O error, ...), ``("copies_over_budget", None)``
        (damaged, but together with the copies already kept it would exceed
        ``quarantine_max_bytes``) or ``("low_disk_space", None)`` (damaged,
        but the volume is below ``min_free_bytes``, or its free space cannot
        be read).

        Nothing is deleted. The ``-wal`` file can hold committed rows that are
        not in the main file yet, so it travels with it under the same new
        name and the copy opens as one database.
        """
        self.close()
        self._last_state.clear()
        self._seeded.clear()
        path = self.config.db_path
        if not path.exists():
            return "healthy", None
        lock = sqlite3.connect(self._guard_path, isolation_level=None, timeout=2.0)
        try:
            try:
                lock.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                return "in_use", None
            try:
                verdict = self._verify()
                if verdict != "corrupt":
                    return verdict, None
                # Renaming costs no space, but the copy then stays for good
                # while a new database grows in its place. Decided under the
                # exclusive lock, so two workers cannot both find room for
                # "one more".
                moving = self.disk_bytes()
                if self.quarantine_bytes() + moving > self.config.quarantine_max_bytes:
                    return "copies_over_budget", None
                if self.config.min_free_bytes > 0:
                    free = self.free_bytes()
                    if free is None or free < self.config.min_free_bytes:
                        return "low_disk_space", None
                stamp = int(self._clock())
                target = path.with_name(f"{path.name}.corrupt-{stamp}")
                attempt = 0
                while target.exists():
                    attempt += 1
                    target = path.with_name(f"{path.name}.corrupt-{stamp}-{attempt}")
                # Raises (and moves nothing) where the OS refuses to rename a
                # file some other program still has open.
                os.replace(path, target)
                for suffix in ("-wal", "-shm"):
                    companion = Path(str(path) + suffix)
                    try:
                        os.replace(companion, str(target) + suffix)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        # Left behind, it is discarded by the next database
                        # created here rather than applied to it.
                        LOGGER.warning("eta observation %s could not follow its database: %s", companion.name, error)
                return "moved", target
            finally:
                lock.execute("ROLLBACK")
        finally:
            lock.close()

    # -- space ------------------------------------------------------------
    def disk_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(str(self.config.db_path) + suffix)
            except OSError:
                pass
        return total

    def free_bytes(self) -> int | None:
        try:
            return int(self._free_bytes(self.config.db_path.parent))
        except OSError:
            return None

    # -- writes -----------------------------------------------------------
    def _seed_states(self, connection: sqlite3.Connection, city: str, subroute: str, direction: int) -> None:
        key = (city, subroute, direction)
        if key in self._seeded:
            return
        self._seeded.add(key)
        # After a restart, pick up where the file left off instead of storing
        # every stop's unchanged state again.
        for seq, status, next_bus, _ in connection.execute(
            "SELECT stop_seq, stop_status, next_bus_at, MAX(src_update_at) FROM eta_stop_states"
            " WHERE city = ? AND subroute_uid = ? AND direction = ? GROUP BY stop_seq",
            key,
        ):
            self._last_state[(city, subroute, direction, seq)] = (status, next_bus)

    def write(self, batch: list[_Prepared]) -> dict[str, int]:
        connection = self.open()
        counts = {"snapshots": 0, "predictions": 0, "states": 0, "duplicates": 0, "replaced": 0}
        pending_state: dict[tuple, tuple] = {}
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in batch:
                existing = connection.execute(
                    "SELECT snapshot_id, stop_rows FROM eta_snapshots"
                    " WHERE city = ? AND routeid = ? AND source = ? AND src_update_at = ?",
                    (item.city, item.routeid, SOURCE_TDX, item.src_update_at),
                ).fetchone()
                if existing is not None and existing[1] >= item.stop_rows:
                    counts["duplicates"] += 1
                    continue
                values = (
                    item.src_update_min_at, item.tdx_update_at, item.observed_at, item.stop_rows,
                    len(item.predictions), int(item.truncated),
                )
                if existing is not None:
                    # Same upstream data, but the earlier copy was cut short.
                    snapshot_id = existing[0]
                    connection.execute("DELETE FROM eta_predictions WHERE snapshot_id = ?", (snapshot_id,))
                    connection.execute(
                        "UPDATE eta_snapshots SET src_update_min_at = ?, tdx_update_at = ?, observed_at = ?,"
                        " stop_rows = ?, eta_rows = ?, truncated = ? WHERE snapshot_id = ?",
                        (*values, snapshot_id),
                    )
                    counts["replaced"] += 1
                else:
                    snapshot_id = connection.execute(
                        "INSERT INTO eta_snapshots (city, routeid, source, src_update_at, src_update_min_at,"
                        " tdx_update_at, observed_at, stop_rows, eta_rows, truncated)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (item.city, item.routeid, SOURCE_TDX, item.src_update_at, *values),
                    ).lastrowid
                    counts["snapshots"] += 1
                connection.executemany(
                    "INSERT INTO eta_predictions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(snapshot_id, *row) for row in item.predictions],
                )
                counts["predictions"] += len(item.predictions)

                # A stop that has a prediction again has left its stored state;
                # when it falls back to "not yet departed" that is a new row.
                for subroute, direction, seq in item.predicted_stops:
                    pending_state[(item.city, subroute, direction, seq)] = _HAS_PREDICTION
                changed = []
                for subroute, direction, seq, row_time, stop_uid, status, next_bus in item.states:
                    self._seed_states(connection, item.city, subroute, direction)
                    key = (item.city, subroute, direction, seq)
                    value = (status, next_bus)
                    if pending_state.get(key, self._last_state.get(key)) == value:
                        continue
                    pending_state[key] = value
                    changed.append(
                        (item.city, subroute, direction, seq, row_time, item.routeid, stop_uid,
                         item.observed_at, status, next_bus)
                    )
                if changed:
                    cursor = connection.executemany(
                        "INSERT OR IGNORE INTO eta_stop_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", changed,
                    )
                    counts["states"] += max(cursor.rowcount, 0)
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        # Only remember states once they are durably stored.
        self._last_state.update(pending_state)
        return counts

    # -- maintenance ------------------------------------------------------
    def _delete_before(self, cutoff: int, *, deadline: float) -> int:
        connection = self.open()
        removed = 0
        while time.monotonic() < deadline:
            ids = [row[0] for row in connection.execute(
                "SELECT snapshot_id FROM eta_snapshots WHERE src_update_at < ? LIMIT 200", (cutoff,),
            )]
            if not ids:
                break
            marks = ",".join("?" * len(ids))
            # Short transactions: a reader or another worker is never locked
            # out for longer than one small batch.
            with connection:
                connection.execute(f"DELETE FROM eta_predictions WHERE snapshot_id IN ({marks})", ids)
                connection.execute(f"DELETE FROM eta_snapshots WHERE snapshot_id IN ({marks})", ids)
            removed += len(ids)
        # States are stored on change only, so the newest row before the cutoff
        # still describes the stop at the cutoff. Keep that one per stop.
        with connection:
            connection.execute(
                "DELETE FROM eta_stop_states WHERE src_update_at < ?1 AND EXISTS ("
                " SELECT 1 FROM eta_stop_states newer WHERE newer.city = eta_stop_states.city"
                " AND newer.subroute_uid = eta_stop_states.subroute_uid"
                " AND newer.direction = eta_stop_states.direction"
                " AND newer.stop_seq = eta_stop_states.stop_seq"
                " AND newer.src_update_at > eta_stop_states.src_update_at"
                " AND newer.src_update_at <= ?1)",
                (cutoff,),
            )
        return removed

    def _reclaim(self) -> None:
        connection = self.open()
        # Fold the WAL back into the database, then hand freed pages back to
        # the filesystem. TRUNCATE gives up quietly if a reader is in the way;
        # the next run tries again.
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.OperationalError:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        if self.auto_vacuum:
            connection.execute("PRAGMA incremental_vacuum(4000)").fetchall()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def maintain(self, *, budget_seconds: float = 5.0) -> dict[str, int]:
        deadline = time.monotonic() + budget_seconds
        now = int(self._clock())
        result = {"expired": self._delete_before(now - self.config.retention_seconds, deadline=deadline), "evicted": 0}
        self._reclaim()
        # Over budget even after retention: give up the oldest hours first.
        # Recent observations are worth more than a full 30 days of them.
        target = int(self.config.max_bytes * 0.9)
        while self.disk_bytes() > self.config.max_bytes and time.monotonic() < deadline:
            connection = self.open()
            oldest = connection.execute("SELECT MIN(src_update_at) FROM eta_snapshots").fetchone()[0]
            if oldest is None:
                break
            removed = self._delete_before(oldest + 3600, deadline=deadline)
            result["evicted"] += removed
            self._reclaim()
            if not self.auto_vacuum:
                # Freed pages are reused but the file cannot shrink, so the
                # file size stops being a measure of what is stored.
                free = connection.execute("PRAGMA freelist_count").fetchone()[0]
                size = connection.execute("PRAGMA page_size").fetchone()[0]
                if self.disk_bytes() - free * size <= target:
                    break
            elif self.disk_bytes() <= target:
                break
            if removed == 0:
                break
        return result

    def reusable_bytes(self) -> int:
        connection = self.open()
        return (
            connection.execute("PRAGMA freelist_count").fetchone()[0]
            * connection.execute("PRAGMA page_size").fetchone()[0]
        )


class EtaObservationCollector:
    """Bounded queue in front of the store; every failure stays in here."""

    def __init__(
        self,
        config: EtaObservationConfig,
        *,
        clock: Callable[[], float] = time.time,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._store = EtaObservationStore(config, clock=clock, free_bytes=free_bytes)
        self._queue: Queue[tuple] = Queue(maxsize=max(1, config.queue_capacity))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle = threading.Lock()
        self._lock = threading.Lock()
        self._shutdown_deadline = float("inf")
        self._retry_at = 0.0
        self._next_maintenance = 0.0
        self._last_quarantine = float("-inf")
        self._stats: dict[str, Any] = {
            "state": "stopped",
            "paused_reason": None,
            "submitted": 0,
            "dropped_queue_full": 0,
            "dropped_while_paused": 0,
            "dropped_on_shutdown": 0,
            "skipped_no_source_time": 0,
            "snapshots_written": 0,
            "snapshots_replaced": 0,
            "snapshots_duplicate": 0,
            "predictions_written": 0,
            "stop_states_written": 0,
            "write_failures": 0,
            "snapshots_lost_to_failures": 0,
            "maintenance_failures": 0,
            "expired_snapshots": 0,
            "evicted_snapshots": 0,
            "corruption_suspected": 0,
            "corruption_recoveries": 0,
            "quarantine_deferred": 0,
            # Why a database reported as damaged was left where it is.
            "quarantine_blocked": None,
            "last_error": None,
            "last_error_at": None,
            "last_write_at": None,
            "last_maintenance_at": None,
            "started_at": None,
            "disk_bytes": None,
            "free_bytes": None,
        }

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._lifecycle:
            if self._thread is not None:
                return  # a second start must not spawn a second writer
            self._stop.clear()
            self._set(state="running", started_at=int(self._clock()))
            self._thread = threading.Thread(target=self._run, name="eta-observations", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 6.0) -> bool:
        with self._lifecycle:
            thread = self._thread
            if thread is None:
                return True
            self._shutdown_deadline = time.monotonic() + timeout
            self._stop.set()
            thread.join(timeout=timeout)
            finished = not thread.is_alive()
            if finished:
                self._thread = None
                self._set(state="stopped")
            else:
                LOGGER.warning("eta observation shutdown deadline reached pending=%s", self._queue.qsize())
            return finished

    # -- producer side ----------------------------------------------------
    def wants(self, city: str | None) -> bool:
        return city in self.config.cities and not self._stop.is_set()

    def submit(self, city: str, routeid: str, items: list[dict[str, Any]], *, truncated: bool = False) -> bool:
        """Queue one route's raw upstream rows. Never blocks, never raises."""
        try:
            if not items or not self.wants(city):
                return False
            if self._stats["paused_reason"] is not None:
                self._bump("dropped_while_paused")
                return False
            # The caller keeps using (and may extend) its list; the dicts
            # themselves are only ever read.
            self._queue.put_nowait((city, routeid, int(self._clock()), list(items), bool(truncated)))
            self._bump("submitted")
            return True
        except Full:
            dropped = self._bump("dropped_queue_full")
            if dropped & (dropped - 1) == 0:  # 1, 2, 4, 8, ... so an outage does not flood the log
                LOGGER.warning("eta observation queue full dropped=%s", dropped)
            return False
        except Exception:
            LOGGER.exception("eta observation submit failed")
            return False

    # -- status -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._stats)
        try:
            copies, held = self._store.quarantined_copies(), self._store.quarantine_bytes()
        except OSError:
            copies = held = None
        snapshot.update(
            enabled=True,
            # Every counter here belongs to the collector of this one process.
            # Behind several workers a request reaches one of them; nothing
            # in this answer is a total across workers.
            worker_pid=os.getpid(),
            counters_scope="single_worker",
            queued=self._queue.qsize(),
            queue_capacity=self._queue.maxsize,
            cities=sorted(self.config.cities),
            db_path=str(self.config.db_path),
            retention_days=round(self.config.retention_seconds / 86400, 2),
            max_bytes=self.config.max_bytes,
            min_free_bytes=self.config.min_free_bytes,
            quarantine_max_bytes=self.config.quarantine_max_bytes,
            quarantined_copies=copies,
            quarantine_bytes=held,
            space_reclaim=self._store.auto_vacuum,
        )
        return snapshot

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._stats.update(values)

    def _bump(self, key: str, amount: int = 1) -> int:
        with self._lock:
            self._stats[key] += amount
            return self._stats[key]

    def _fail(self, key: str, error: BaseException) -> None:
        with self._lock:
            self._stats[key] += 1
            self._stats["last_error"] = f"{type(error).__name__}: {error}"[:300]
            self._stats["last_error_at"] = int(self._clock())

    # -- writer thread ----------------------------------------------------
    def _run(self) -> None:
        try:
            self._check_space()
        except Exception as error:  # never let the thread die before its loop
            self._fail("maintenance_failures", error)
        # Space was just checked; the first clean-up waits a full interval so
        # a restart loop does not spend its time on maintenance.
        self._next_maintenance = time.monotonic() + self.config.maintenance_interval
        while not self._stop.is_set() or not self._queue.empty():
            if time.monotonic() >= self._shutdown_deadline:
                break
            batch = self._take_batch()
            if batch:
                self._write(batch)
            if not self._stop.is_set() and time.monotonic() >= self._next_maintenance:
                self._maintain()
        pending = self._queue.qsize()
        if pending:
            self._bump("dropped_on_shutdown", pending)
            LOGGER.warning("eta observation shutdown discarded queued snapshots count=%s", pending)
        self._store.close()

    def _take_batch(self) -> list[tuple]:
        try:
            batch = [self._queue.get(timeout=self.config.flush_interval)]
        except Empty:
            return []
        deadline = time.monotonic() + self.config.flush_interval
        while len(batch) < self.config.batch_size:
            try:
                if self._stop.is_set():
                    batch.append(self._queue.get_nowait())
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    batch.append(self._queue.get(timeout=remaining))
            except Empty:
                break
        return batch

    def _write(self, batch: list[tuple]) -> None:
        if time.monotonic() < self._retry_at:
            self._bump("snapshots_lost_to_failures", len(batch))
            return
        try:
            prepared = []
            for city, routeid, observed_at, items, truncated in batch:
                item = prepare_snapshot(city, routeid, items, observed_at=observed_at, truncated=truncated)
                if item is None:
                    self._bump("skipped_no_source_time")
                else:
                    prepared.append(item)
            if not prepared:
                return
            counts = self._store.write(prepared)
        except Exception as error:
            self._fail("write_failures", error)
            self._bump("snapshots_lost_to_failures", len(batch))
            self._after_failure(error)
            return
        with self._lock:
            self._stats["snapshots_written"] += counts["snapshots"]
            self._stats["snapshots_replaced"] += counts["replaced"]
            self._stats["snapshots_duplicate"] += counts["duplicates"]
            self._stats["predictions_written"] += counts["predictions"]
            self._stats["stop_states_written"] += counts["states"]
            self._stats["last_write_at"] = int(self._clock())

    def _after_failure(self, error: BaseException) -> None:
        failures = self._stats["write_failures"]
        if failures & (failures - 1) == 0:
            LOGGER.warning("eta observation write failed failures=%s error=%s", failures, error)
        self._handle_storage_error(error)

    def _handle_storage_error(self, error: BaseException, *, backoff: bool = True) -> None:
        kind = classify_error(error)
        self._store.close()
        if backoff:
            # Whatever it was, a persistent fault costs one failed attempt per
            # interval, not one per batch. Locks and I/O errors stop here.
            self._retry_at = time.monotonic() + 30.0
        if kind == "full":
            self._pause("disk_full")
        elif kind == "suspect":
            self._bump("corruption_suspected")
            self._recover_from_corruption()

    def _recover_from_corruption(self) -> None:
        # One rebuild an hour and a few copies at most: a disk that keeps
        # corrupting the file should stop the collector, not fill the folder.
        if time.monotonic() - self._last_quarantine < 3600:
            self._pause_corrupt("too_soon")
            return
        if self._store.quarantined_copies() >= _MAX_QUARANTINED_COPIES:
            self._pause_corrupt("too_many_copies")
            return
        try:
            outcome, moved = self._store.quarantine()
        except Exception as quarantine_error:
            self._fail("maintenance_failures", quarantine_error)
            outcome, moved = "failed", None
        if outcome == "moved":
            self._last_quarantine = time.monotonic()
            self._retry_at = 0.0
            self._bump("corruption_recoveries")
            LOGGER.error("eta observation db failed its integrity check; kept as %s and starting fresh", moved)
        elif outcome == "healthy":
            LOGGER.warning("eta observation db reported corruption but passes its integrity check; left in place")
        elif outcome in ("copies_over_budget", "low_disk_space"):
            # Confirmed damaged, but one more kept copy does not fit. Losing
            # observations is cheaper than filling the volume.
            self._pause_corrupt(outcome)
        else:
            self._bump("quarantine_deferred")
            LOGGER.warning("eta observation db may be corrupt; left in place for now reason=%s", outcome)

    def _pause_corrupt(self, blocked: str) -> None:
        if self._stats["quarantine_blocked"] != blocked:
            LOGGER.error(
                "eta observation db reported corruption and was left in place reason=%s; needs an operator", blocked,
            )
        self._set(quarantine_blocked=blocked)
        self._pause("corrupt")

    def _pause(self, reason: str) -> None:
        if self._stats["paused_reason"] != reason:
            LOGGER.warning("eta observation collection paused reason=%s", reason)
        self._set(state="paused", paused_reason=reason)

    def _check_space(self) -> None:
        used = self._store.disk_bytes()
        free = self._store.free_bytes()
        self._set(disk_bytes=used, free_bytes=free)
        reason = self._stats["paused_reason"]
        if free is not None and free < self.config.min_free_bytes:
            self._pause("low_disk_space")
        elif used > self.config.max_bytes and used - self._reusable() > self.config.max_bytes:
            self._pause("size_limit")
        elif reason in ("low_disk_space", "size_limit", "disk_full"):
            LOGGER.info("eta observation collection resumed after %s", reason)
            self._set(state="running", paused_reason=None)

    def _reusable(self) -> int:
        try:
            return self._store.reusable_bytes()
        except Exception:
            return 0

    def _maintain(self) -> None:
        self._next_maintenance = time.monotonic() + self.config.maintenance_interval
        if self._stats["paused_reason"] == "corrupt":
            return
        try:
            result = self._store.maintain()
            with self._lock:
                self._stats["expired_snapshots"] += result["expired"]
                self._stats["evicted_snapshots"] += result["evicted"]
                self._stats["last_maintenance_at"] = int(self._clock())
            if result["evicted"]:
                LOGGER.warning("eta observation db over size budget; evicted oldest snapshots=%s", result["evicted"])
        except Exception as error:
            self._fail("maintenance_failures", error)
            # A failed clean-up is no reason to skip the next write.
            self._handle_storage_error(error, backoff=False)
        try:
            self._check_space()
        except Exception as error:
            self._fail("maintenance_failures", error)
        stats = self.status()
        LOGGER.info(
            "eta observations state=%s written=%s duplicate=%s dropped=%s failures=%s disk_mb=%.1f",
            stats["state"], stats["snapshots_written"], stats["snapshots_duplicate"],
            stats["dropped_queue_full"] + stats["dropped_while_paused"], stats["write_failures"],
            (stats["disk_bytes"] or 0) / 1e6,
        )


def build_collector(settings: Any) -> EtaObservationCollector | None:
    """The collector for these settings, or None when the feature is off."""
    if not getattr(settings, "eta_observations_enabled", False):
        return None
    return EtaObservationCollector(
        EtaObservationConfig(
            db_path=Path(settings.eta_observations_db_path),
            cities=frozenset(settings.eta_observations_cities),
            retention_seconds=max(1, int(settings.eta_observations_retention_days * 86400)),
            max_bytes=max(1, int(settings.eta_observations_max_bytes)),
            min_free_bytes=max(0, int(settings.eta_observations_min_free_bytes)),
            quarantine_max_bytes=max(0, int(settings.eta_observations_quarantine_max_bytes)),
        )
    )
