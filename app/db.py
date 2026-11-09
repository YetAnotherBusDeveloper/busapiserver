from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


MAIN_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS routes (
    routeid     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_en     TEXT
);

CREATE TABLE IF NOT EXISTS paths (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    PRIMARY KEY (routeid, pathid),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stops (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    stopid      TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq),
    FOREIGN KEY (routeid, pathid) REFERENCES paths(routeid, pathid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS path_points (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq),
    FOREIGN KEY (routeid, pathid) REFERENCES paths(routeid, pathid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_routes_name ON routes(name);
CREATE INDEX IF NOT EXISTS idx_stops_routeid ON stops(routeid);
CREATE INDEX IF NOT EXISTS idx_stops_stopid ON stops(stopid);
CREATE INDEX IF NOT EXISTS idx_path_points_routeid ON path_points(routeid);

-- Physical bus stations are authority-scoped. A station contains one or more
-- stable TDX StopUID/StopID sides, while the existing stops table remains
-- route/path scoped for ETA lookup and offline compatibility.
CREATE TABLE IF NOT EXISTS stations (
    city_code   TEXT NOT NULL,
    station_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (city_code, station_id)
);

CREATE TABLE IF NOT EXISTS station_stops (
    city_code    TEXT NOT NULL,
    station_id   TEXT NOT NULL,
    stop_uid     TEXT NOT NULL,
    stop_id      TEXT NOT NULL,
    side_id      TEXT NOT NULL,
    side_order   INTEGER NOT NULL,
    side_label   TEXT NOT NULL,
    direction    TEXT,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    PRIMARY KEY (city_code, station_id, side_id),
    FOREIGN KEY (city_code, station_id)
        REFERENCES stations(city_code, station_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_station_stops_raw_stop
    ON station_stops(city_code, stop_id);
CREATE INDEX IF NOT EXISTS idx_station_stops_uid
    ON station_stops(city_code, stop_uid);

CREATE TABLE IF NOT EXISTS operators (
    operator_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_en     TEXT,
    code        TEXT,
    phone       TEXT,
    email       TEXT,
    url         TEXT
);

CREATE TABLE IF NOT EXISTS route_operators (
    routeid     TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (routeid, operator_id),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE,
    FOREIGN KEY (operator_id) REFERENCES operators(operator_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_route_operators_routeid ON route_operators(routeid);
CREATE INDEX IF NOT EXISTS idx_route_operators_operator ON route_operators(operator_id);

-- Maps every TDX SubRouteUID (in the local namespace, i.e. INT-prefixed for
-- intercity) to the canonical routeid that absorbed it. Some authorities give
-- each direction of a route its own SubRouteUID; sync_static merges those into
-- one route with two paths, and this table remembers the original ids so that
-- realtime lookups can still query TDX and so that stale client routeids keep
-- resolving. Singleton routes get an identity row (subroute_uid == routeid).
CREATE TABLE IF NOT EXISTS route_subroutes (
    subroute_uid TEXT PRIMARY KEY,
    routeid      TEXT NOT NULL,
    direction    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_route_subroutes_routeid ON route_subroutes(routeid);

-- Maps a TDX RouteUID (raw, never INT-prefixed) and direction to the local
-- routeid serving it. Needed because 雙北 realtime feeds stopped populating
-- SubRouteUID: their items carry only RouteUID + Direction, so both the
-- outbound $filter and the inbound bucketing must be able to translate
-- RouteUID-level ids. A RouteUID shared by several distinct routes (公路客運
-- lettered variants, Taipei 區間 families) is ambiguous and gets no rows for
-- the conflicting directions — resolution simply fails closed for those.
CREATE TABLE IF NOT EXISTS route_uids (
    route_uid TEXT NOT NULL,
    direction INTEGER NOT NULL DEFAULT 0,
    routeid   TEXT NOT NULL,
    PRIMARY KEY (route_uid, direction, routeid),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_route_uids_routeid ON route_uids(routeid);

CREATE TABLE IF NOT EXISTS route_schedules (
    routeid       TEXT NOT NULL,
    subroute_uid  TEXT NOT NULL DEFAULT '',
    direction     INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL,             -- 'frequency' or 'timetable'
    seq           INTEGER NOT NULL DEFAULT 0,
    service_days  TEXT NOT NULL,             -- JSON {mon..sun, holiday}
    payload       TEXT NOT NULL,             -- JSON payload
    PRIMARY KEY (routeid, subroute_uid, direction, kind, seq),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_route_schedules_routeid ON route_schedules(routeid);

CREATE TABLE IF NOT EXISTS stop_travel_times (
    routeid       TEXT NOT NULL,
    direction     INTEGER NOT NULL DEFAULT 0,
    from_seq      INTEGER NOT NULL,
    to_seq        INTEGER NOT NULL,
    avg_seconds   REAL NOT NULL,
    sample_count  INTEGER NOT NULL DEFAULT 1,
    source        TEXT NOT NULL DEFAULT 'timetable'
                   CHECK (source IN ('timetable', 'eta')),
    PRIMARY KEY (routeid, direction, from_seq, to_seq, source),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_stop_travel_times_routeid ON stop_travel_times(routeid);

CREATE TABLE IF NOT EXISTS tdx_fetch_state (
    resource_key    TEXT PRIMARY KEY,
    last_modified   TEXT,
    last_status     INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL,
    last_updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_tdx_fetch_state_checked_at ON tdx_fetch_state(last_checked_at);

CREATE TABLE IF NOT EXISTS database_versions (
    name         TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""

# Application data unrelated to static route sync. These tables live in a
# separate database file (app.db) so that the weekly static sync, which
# atomically replaces the static database, never touches user/auth/analytics
# data and cannot race with concurrent API writes.
APP_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS request_analytics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at    INTEGER NOT NULL,
    method          TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    path            TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    client_family   TEXT NOT NULL,
    platform_name   TEXT,
    system_name     TEXT,
    system_version  TEXT,
    app_version     TEXT,
    app_commit_hash TEXT,
    browser_name    TEXT,
    browser_version TEXT,
    user_agent      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_request_analytics_requested_at
    ON request_analytics(requested_at);
CREATE INDEX IF NOT EXISTS idx_request_analytics_endpoint
    ON request_analytics(endpoint);
CREATE INDEX IF NOT EXISTS idx_request_analytics_client_family
    ON request_analytics(client_family);
CREATE INDEX IF NOT EXISTS idx_request_analytics_app_version
    ON request_analytics(app_version);
CREATE INDEX IF NOT EXISTS idx_request_analytics_browser_name
    ON request_analytics(browser_name);

CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY,
    role       TEXT NOT NULL DEFAULT 'user'
               CHECK (role IN ('admin', 'mod', 'user')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_oauth_identities (
    provider         TEXT NOT NULL
                     CHECK (provider IN ('discord', 'google')),
    provider_user_id TEXT NOT NULL,
    account_id       INTEGER NOT NULL,
    email            TEXT,
    display_name     TEXT,
    avatar_url       TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (provider, provider_user_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_oauth_identities_account
    ON account_oauth_identities(account_id);

CREATE TABLE IF NOT EXISTS account_devices (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    device_key   TEXT NOT NULL UNIQUE,
    device_name  TEXT,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_devices_account
    ON account_devices(account_id);

CREATE TABLE IF NOT EXISTS account_device_tokens (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    device_id    INTEGER NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL,
    revoked_at   INTEGER,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES account_devices(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_device_tokens_account
    ON account_device_tokens(account_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_device_tokens_active_device
    ON account_device_tokens(device_id)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_oauth_states (
    state_hash   TEXT PRIMARY KEY,
    provider     TEXT NOT NULL
                 CHECK (provider IN ('discord', 'google')),
    platform     TEXT NOT NULL
                 CHECK (platform IN ('web', 'app')),
    redirect_uri TEXT NOT NULL,
    device_key   TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    used_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_auth_oauth_states_expires_at
    ON auth_oauth_states(expires_at);

CREATE TABLE IF NOT EXISTS auth_link_state_contexts (
    state_hash        TEXT PRIMARY KEY,
    target_account_id INTEGER NOT NULL,
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    FOREIGN KEY (state_hash) REFERENCES auth_oauth_states(state_hash) ON DELETE CASCADE,
    FOREIGN KEY (target_account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_link_state_contexts_expires_at
    ON auth_link_state_contexts(expires_at);

CREATE TABLE IF NOT EXISTS auth_pending_account_merges (
    token_hash        TEXT PRIMARY KEY,
    target_account_id INTEGER NOT NULL,
    source_account_id INTEGER NOT NULL,
    provider          TEXT NOT NULL
                      CHECK (provider IN ('discord', 'google')),
    provider_user_id  TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    consumed_at       INTEGER,
    FOREIGN KEY (target_account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (source_account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_pending_account_merges_target
    ON auth_pending_account_merges(target_account_id);
CREATE INDEX IF NOT EXISTS idx_auth_pending_account_merges_expires_at
    ON auth_pending_account_merges(expires_at);

CREATE TABLE IF NOT EXISTS announcements (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    content        TEXT NOT NULL,
    content_type   TEXT NOT NULL DEFAULT 'markdown'
                   CHECK (content_type IN ('markdown')),
    author         TEXT,
    created_at     INTEGER NOT NULL,
    expire_at      INTEGER,
    behavior_json  TEXT NOT NULL,
    targets_json   TEXT,
    sound_url      TEXT,
    embed_json     TEXT,
    actions_json   TEXT,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcements_created_at
    ON announcements(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_announcements_expire_at
    ON announcements(expire_at);
CREATE TABLE IF NOT EXISTS announcement_push_tokens (
    token        TEXT PRIMARY KEY,
    platform     TEXT NOT NULL
                 CHECK (platform IN ('android', 'web')),
    user_agent   TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcement_push_tokens_platform
    ON announcement_push_tokens(platform);
CREATE INDEX IF NOT EXISTS idx_announcement_push_tokens_last_seen_at
    ON announcement_push_tokens(last_seen_at DESC);

CREATE TABLE IF NOT EXISTS announcement_reactions (
    announcement_id TEXT    NOT NULL,
    account_id      INTEGER NOT NULL,
    emoji           TEXT    NOT NULL,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (announcement_id, account_id, emoji),
    FOREIGN KEY (announcement_id) REFERENCES announcements(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id)      REFERENCES accounts(id)      ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_announcement_reactions_announcement_id
    ON announcement_reactions(announcement_id);

CREATE TABLE IF NOT EXISTS feedbacks (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    client_family   TEXT NOT NULL,
    platform_name   TEXT,
    system_name     TEXT,
    system_version  TEXT,
    app_version     TEXT,
    app_commit_hash TEXT,
    browser_name    TEXT,
    browser_version TEXT,
    user_agent      TEXT NOT NULL,
    category        TEXT,
    route_context   TEXT,
    contact         TEXT,
    diagnostics_json TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedbacks_created_at
    ON feedbacks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedbacks_account
    ON feedbacks(account_id);

CREATE TABLE IF NOT EXISTS account_sync_documents (
    account_id               INTEGER NOT NULL,
    namespace                TEXT NOT NULL,
    schema_version           INTEGER NOT NULL,
    payload_json             TEXT NOT NULL,
    payload_size_bytes       INTEGER NOT NULL,
    content_hash             TEXT NOT NULL,
    revision                 INTEGER NOT NULL,
    created_at               INTEGER NOT NULL,
    updated_at               INTEGER NOT NULL,
    last_synced_at           INTEGER NOT NULL,
    last_client_modified_at  TEXT,
    PRIMARY KEY (account_id, namespace),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_account_sync_documents_account
    ON account_sync_documents(account_id);
CREATE INDEX IF NOT EXISTS idx_account_sync_documents_updated_at
    ON account_sync_documents(updated_at DESC);

-- Change-detection stamps for refresh_database_versions. Lives here rather than
-- in the static database because the weekly sync replaces that file wholesale.
CREATE TABLE IF NOT EXISTS database_version_fingerprints (
    name          TEXT PRIMARY KEY,
    db_path       TEXT NOT NULL,
    file_size     INTEGER NOT NULL,
    file_mtime_ns INTEGER NOT NULL,
    file_inode    INTEGER NOT NULL DEFAULT 0,
    shape_digest  TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL,
    checked_at    INTEGER NOT NULL
);
"""

DOWNLOAD_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS routes (
    routeid        TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    name_en        TEXT,
    city_code      TEXT NOT NULL,
    path_name      TEXT,
    path_name_en   TEXT,
    operator_names TEXT
);

CREATE TABLE IF NOT EXISTS paths (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    PRIMARY KEY (routeid, pathid),
    FOREIGN KEY (routeid) REFERENCES routes(routeid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_routes_name ON routes(name);
CREATE INDEX IF NOT EXISTS idx_routes_city_code ON routes(city_code);
CREATE INDEX IF NOT EXISTS idx_paths_routeid ON paths(routeid);
"""

CITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stops (
    routeid     TEXT NOT NULL,
    pathid      INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    stopid      TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_en     TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (routeid, pathid, seq)
);

CREATE INDEX IF NOT EXISTS idx_stops_routeid ON stops(routeid);
CREATE INDEX IF NOT EXISTS idx_stops_stopid ON stops(stopid);
"""


def _configure_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute("PRAGMA journal_mode = WAL;")
    connection.execute("PRAGMA busy_timeout = 5000;")
    return connection


def connect(db_path: str | Path) -> sqlite3.Connection:
    return _configure_connection(sqlite3.connect(Path(db_path)))


@contextmanager
def get_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a database without writing to it.

    Deliberately skips the ``PRAGMA journal_mode = WAL`` that
    ``_configure_connection`` applies: on a database that is not already in WAL
    mode that pragma rewrites the file header, so merely measuring a file would
    change the size/mtime that change detection records for it.
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000;")
    return connection


@contextmanager
def get_readonly_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    try:
        connection = connect_readonly(db_path)
    except sqlite3.OperationalError:
        connection = connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as connection:
        _drop_legacy_inter_tables(connection)
        connection.executescript(MAIN_SCHEMA_SQL)
        connection.commit()


def init_app_db(db_path: str | Path) -> None:
    """Initialize the application database (auth, analytics, announcements, etc.).

    This database is independent of the static route database and is never
    replaced by the weekly static sync.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as connection:
        connection.executescript(APP_SCHEMA_SQL)
        _migrate_account_devices_add_device_name(connection)
        _migrate_feedback_optional_details(connection)
        connection.commit()


# Tables that hold application data (auth, analytics, announcements, feedback,
# account sync). These live in the application database, separate from the
# static route database. Order matters for FK-safe inserts (parents first).
APP_TABLES = (
    "accounts",
    "account_oauth_identities",
    "account_devices",
    "account_device_tokens",
    "auth_oauth_states",
    "auth_link_state_contexts",
    "auth_pending_account_merges",
    "request_analytics",
    "announcements",
    "announcement_push_tokens",
    "announcement_reactions",
    "feedbacks",
    "account_sync_documents",
)


def migrate_app_tables_from_main(main_db_path: str | Path, app_db_path: str | Path) -> bool:
    """One-time migration: move legacy application data out of the static db.

    Older deployments stored auth/analytics/announcement/feedback/sync tables in
    the main (static) database. This copies any such rows into the dedicated
    application database when the application database does not yet contain them,
    then drops the legacy tables from the main database so the static sync no
    longer carries them.

    Returns True if any data was migrated.
    """
    main_path = Path(main_db_path)
    if not main_path.exists():
        return False

    migrated_any = False
    with get_connection(main_path) as main_conn, get_connection(app_db_path) as app_conn:
        for table in APP_TABLES:
            legacy_exists = main_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if legacy_exists is None:
                continue

            rows = main_conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue

            # Only seed the app db when its table is currently empty, to
            # avoid clobbering newer data already written to the app db.
            app_count = app_conn.execute(
                f"SELECT COUNT(*) AS c FROM {table}"
            ).fetchone()["c"]
            if app_count == 0:
                column_names = [col["name"] for col in main_conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()]
                cols_csv = ", ".join(column_names)
                placeholders = ", ".join("?" for _ in column_names)
                app_conn.executemany(
                    f"INSERT OR IGNORE INTO {table} ({cols_csv}) VALUES ({placeholders})",
                    [tuple(row) for row in rows],
                )
                migrated_any = True

        app_conn.commit()

        # Drop legacy application tables from the static database so the weekly
        # static sync stops carrying stale data. Disable FK enforcement and drop
        # in reverse (child-first) order to avoid constraint violations.
        main_conn.execute("PRAGMA foreign_keys = OFF;")
        for table in reversed(APP_TABLES):
            main_conn.execute(f"DROP TABLE IF EXISTS {table}")
        main_conn.execute("PRAGMA foreign_keys = ON;")
        main_conn.commit()

    return migrated_any


def _migrate_feedback_optional_details(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(feedbacks)").fetchall()
    }
    for name in ("category", "route_context", "contact", "diagnostics_json"):
        if name not in columns:
            connection.execute(f"ALTER TABLE feedbacks ADD COLUMN {name} TEXT")


def _migrate_account_devices_add_device_name(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(account_devices)").fetchall()
    }
    if "device_name" not in columns:
        connection.execute("ALTER TABLE account_devices ADD COLUMN device_name TEXT")


def _drop_legacy_inter_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS inter_route_schedules;
        DROP TABLE IF EXISTS inter_route_operators;
        DROP TABLE IF EXISTS inter_path_points;
        DROP TABLE IF EXISTS inter_stops;
        DROP TABLE IF EXISTS inter_paths;
        DROP TABLE IF EXISTS inter_routes;
        """
    )


def init_city_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as connection:
        connection.executescript(CITY_SCHEMA_SQL)
        connection.commit()


def export_download_db(source_db_path: str | Path, target_db_path: str | Path) -> None:
    source_path = Path(source_db_path)
    target_path = Path(target_db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    if temp_path.exists():
        temp_path.unlink()

    with get_connection(source_path) as source_connection, get_connection(temp_path) as target_connection:
        target_connection.executescript(DOWNLOAD_SCHEMA_SQL)

        routes = source_connection.execute(
            """
            SELECT
                routes.routeid AS routeid,
                routes.name AS name,
                routes.name_en AS name_en,
                SUBSTR(routes.routeid, 1, 3) AS city_code,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(path_name, ' / ')
                        FROM (
                            SELECT DISTINCT p.name AS path_name
                            FROM paths p
                            WHERE p.routeid = routes.routeid
                              AND TRIM(COALESCE(p.name, '')) <> ''
                            ORDER BY p.pathid ASC
                        )
                    ),
                    ''
                ) AS path_name,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(path_name_en, ' / ')
                        FROM (
                            SELECT DISTINCT p.name_en AS path_name_en
                            FROM paths p
                            WHERE p.routeid = routes.routeid
                              AND TRIM(COALESCE(p.name_en, '')) <> ''
                            ORDER BY p.pathid ASC
                        )
                    ),
                    ''
                ) AS path_name_en,
                COALESCE(
                    (
                        SELECT GROUP_CONCAT(op_name, ' / ')
                        FROM (
                            SELECT o.name AS op_name
                            FROM route_operators ro
                            JOIN operators o ON o.operator_id = ro.operator_id
                            WHERE ro.routeid = routes.routeid
                              AND TRIM(COALESCE(o.name, '')) <> ''
                            ORDER BY ro.seq ASC, o.name ASC
                        )
                    ),
                    ''
                ) AS operator_names
            FROM routes
            ORDER BY routes.routeid
            """
        ).fetchall()
        target_connection.executemany(
            """
            INSERT INTO routes (routeid, name, name_en, city_code, path_name, path_name_en, operator_names)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["name"],
                    row["name_en"],
                    row["city_code"],
                    row["path_name"],
                    row["path_name_en"],
                    row["operator_names"],
                )
                for row in routes
            ],
        )
        paths = source_connection.execute(
            """
            SELECT routeid, pathid, name, name_en
            FROM paths
            ORDER BY routeid, pathid
            """
        ).fetchall()
        target_connection.executemany(
            """
            INSERT INTO paths (routeid, pathid, name, name_en)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    row["routeid"],
                    row["pathid"],
                    row["name"],
                    row["name_en"],
                )
                for row in paths
            ],
        )
        target_connection.commit()

    temp_path.replace(target_path)


def delete_main_routes_by_prefix(connection: sqlite3.Connection, prefix: str) -> None:
    connection.execute(
        "DELETE FROM routes WHERE routeid LIKE ?",
        (f"{prefix}%",),
    )


def has_route_subroutes_table(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'route_subroutes' LIMIT 1"
    ).fetchone()
    return row is not None


def load_route_subroute_map(connection: sqlite3.Connection) -> dict[str, str]:
    """Map every known SubRouteUID to the canonical routeid that absorbed it.

    Returns an empty mapping on databases predating ``route_subroutes`` so that
    callers degrade to identity behaviour instead of failing.
    """
    if not has_route_subroutes_table(connection):
        return {}

    rows = connection.execute("SELECT subroute_uid, routeid FROM route_subroutes").fetchall()
    return {row["subroute_uid"]: row["routeid"] for row in rows}


def load_route_subroute_expansion(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Map every canonical routeid to the SubRouteUIDs it must be queried by."""
    if not has_route_subroutes_table(connection):
        return {}

    rows = connection.execute(
        """
        SELECT routeid, subroute_uid
        FROM route_subroutes
        ORDER BY routeid, direction, subroute_uid
        """
    ).fetchall()

    expansion: dict[str, list[str]] = {}
    for row in rows:
        expansion.setdefault(row["routeid"], []).append(row["subroute_uid"])
    return expansion


def has_route_uids_table(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'route_uids' LIMIT 1"
    ).fetchone()
    return row is not None


def load_route_uid_rows(
    connection: sqlite3.Connection,
) -> list[tuple[str, int, str]]:
    """Every (route_uid, direction, routeid) row, deterministically ordered.

    Returns an empty list on databases predating ``route_uids`` so that
    callers degrade to derivation/identity behaviour instead of failing.
    """
    if not has_route_uids_table(connection):
        return []

    rows = connection.execute(
        """
        SELECT route_uid, direction, routeid
        FROM route_uids
        ORDER BY route_uid, direction, routeid
        """
    ).fetchall()
    return [(row["route_uid"], row["direction"], row["routeid"]) for row in rows]


def clear_inter_db(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM inter_routes")


def clear_city_db(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM stops")


def route_exists(connection: sqlite3.Connection, routeid: str) -> bool:
    return _route_exists_in_table(connection, routeid, routes_table="routes")


def inter_route_exists(connection: sqlite3.Connection, routeid: str) -> bool:
    return _route_exists_in_table(connection, routeid, routes_table="inter_routes")


def _route_exists_in_table(
    connection: sqlite3.Connection,
    routeid: str,
    *,
    routes_table: str,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {routes_table} WHERE routeid = ? LIMIT 1",
        (routeid,),
    ).fetchone()
    return row is not None


def path_exists(connection: sqlite3.Connection, routeid: str, pathid: int) -> bool:
    return _path_exists_in_table(connection, routeid, pathid, paths_table="paths")


def inter_path_exists(connection: sqlite3.Connection, routeid: str, pathid: int) -> bool:
    return _path_exists_in_table(connection, routeid, pathid, paths_table="inter_paths")


def _path_exists_in_table(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
    *,
    paths_table: str,
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {paths_table} WHERE routeid = ? AND pathid = ? LIMIT 1",
        (routeid, pathid),
    ).fetchone()
    return row is not None


def load_path_points(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
) -> list[tuple[float, float]]:
    return _load_path_points_from_table(
        connection,
        routeid,
        pathid,
        path_points_table="path_points",
    )


def load_inter_path_points(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
) -> list[tuple[float, float]]:
    return _load_path_points_from_table(
        connection,
        routeid,
        pathid,
        path_points_table="inter_path_points",
    )


def _load_path_points_from_table(
    connection: sqlite3.Connection,
    routeid: str,
    pathid: int,
    *,
    path_points_table: str,
) -> list[tuple[float, float]]:
    rows = connection.execute(
        f"""
        SELECT lat, lon
        FROM {path_points_table}
        WHERE routeid = ? AND pathid = ?
        ORDER BY seq
        """,
        (routeid, pathid),
    ).fetchall()
    return [(float(row["lat"]), float(row["lon"])) for row in rows]


def load_route_static(connection: sqlite3.Connection, routeid: str) -> dict | None:
    return _load_route_static_from_tables(
        connection,
        routeid,
        routes_table="routes",
        paths_table="paths",
        stops_table="stops",
    )


def load_inter_route_static(connection: sqlite3.Connection, routeid: str) -> dict | None:
    return _load_route_static_from_tables(
        connection,
        routeid,
        routes_table="inter_routes",
        paths_table="inter_paths",
        stops_table="inter_stops",
    )


def _load_route_static_from_tables(
    connection: sqlite3.Connection,
    routeid: str,
    *,
    routes_table: str,
    paths_table: str,
    stops_table: str,
) -> dict | None:
    route_row = connection.execute(
        f"SELECT routeid, name, name_en FROM {routes_table} WHERE routeid = ?",
        (routeid,),
    ).fetchone()
    if route_row is None:
        return None

    path_rows = connection.execute(
        f"""
        SELECT pathid, name, name_en
        FROM {paths_table}
        WHERE routeid = ?
        ORDER BY pathid
        """,
        (routeid,),
    ).fetchall()

    stop_rows = connection.execute(
        f"""
        SELECT pathid, seq, stopid, name, name_en, lat, lon
        FROM {stops_table}
        WHERE routeid = ?
        ORDER BY pathid, seq
        """,
        (routeid,),
    ).fetchall()

    paths = {}
    for row in path_rows:
        paths[row["pathid"]] = {
            "pathid": row["pathid"],
            "name": row["name"],
            "name_en": row["name_en"],
            "stops": [],
            "stop_index": {},
        }

    for row in stop_rows:
        path = paths.setdefault(
            row["pathid"],
            {
                "pathid": row["pathid"],
                "name": f"Path {row['pathid']}",
                "name_en": None,
                "stops": [],
                "stop_index": {},
            },
        )
        stop_data = {
            "seq": row["seq"],
            "stopid": row["stopid"],
            "name": row["name"],
            "name_en": row["name_en"],
            "lat": row["lat"],
            "lon": row["lon"],
        }
        path["stops"].append(stop_data)
        path["stop_index"][row["stopid"]] = stop_data

    return {
        "routeid": route_row["routeid"],
        "name": route_row["name"],
        "name_en": route_row["name_en"],
        "paths": paths,
    }


def load_tdx_fetch_state(connection: sqlite3.Connection, resource_key: str) -> dict | None:
    row = connection.execute(
        """
        SELECT resource_key, last_modified, last_status, last_checked_at, last_updated_at
        FROM tdx_fetch_state
        WHERE resource_key = ?
        """,
        (resource_key,),
    ).fetchone()
    if row is None:
        return None

    return {
        "resource_key": row["resource_key"],
        "last_modified": row["last_modified"],
        "last_status": row["last_status"],
        "last_checked_at": row["last_checked_at"],
        "last_updated_at": row["last_updated_at"],
    }


def save_tdx_fetch_state(
    connection: sqlite3.Connection,
    resource_key: str,
    *,
    last_modified: str | None,
    last_status: int,
    last_checked_at: int,
    last_updated_at: int | None,
) -> None:
    connection.execute(
        """
        INSERT INTO tdx_fetch_state
            (resource_key, last_modified, last_status, last_checked_at, last_updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
            last_modified = excluded.last_modified,
            last_status = excluded.last_status,
            last_checked_at = excluded.last_checked_at,
            last_updated_at = excluded.last_updated_at
        """,
        (resource_key, last_modified, last_status, last_checked_at, last_updated_at),
    )


def _iter_table_rows_as_bytes(
    connection: sqlite3.Connection,
    table_name: str,
) -> Iterable[bytes]:
    """Reference serialization for :func:`hash_tables`, row by row.

    ``hash_tables`` reads in batches instead, which is faster but must stay
    byte-for-byte identical to this; ``test_database_versions.py`` asserts that
    equivalence against this function.
    """
    columns_info = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    column_names = [row["name"] for row in columns_info]
    if not column_names:
        return

    columns_sql = ", ".join(f'"{name}"' for name in column_names)
    order_sql = ", ".join(f'"{name}"' for name in column_names)
    rows = connection.execute(
        f'SELECT {columns_sql} FROM "{table_name}" ORDER BY {order_sql}'
    )
    for row in rows:
        values = tuple(row[name] for name in column_names)
        yield repr(values).encode("utf-8")


_HASH_FETCH_BATCH_SIZE = 50_000


def _hash_table_rows(hasher, connection: sqlite3.Connection, table_name: str) -> None:
    """Fold every row of ``table_name`` into ``hasher``.

    Equivalent to hashing :func:`_iter_table_rows_as_bytes` row by row -- with
    ``row_factory`` unset, ``repr()`` of the row tuple is exactly what that
    helper yields -- but fetching in batches makes it ~1.4x faster on the
    multi-million-row tables.

    Do not change this serialization or the ORDER BY. Both feed every stored
    content_hash, so any change bumps all database versions at once and forces
    every client to re-download the databases.
    """
    previous_factory = connection.row_factory
    try:
        connection.row_factory = sqlite3.Row
        columns_info = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        column_names = [row["name"] for row in columns_info]
        if not column_names:
            return

        columns_sql = ", ".join(f'"{name}"' for name in column_names)
        order_sql = ", ".join(f'"{name}"' for name in column_names)
        connection.row_factory = None
        cursor = connection.execute(
            f'SELECT {columns_sql} FROM "{table_name}" ORDER BY {order_sql}'
        )
        while True:
            rows = cursor.fetchmany(_HASH_FETCH_BATCH_SIZE)
            if not rows:
                break
            hasher.update(b"".join(repr(row).encode("utf-8") + b"\n" for row in rows))
    finally:
        connection.row_factory = previous_factory


def hash_tables(db_path: str | Path, table_names: tuple[str, ...]) -> str:
    hasher = hashlib.sha256()
    with get_readonly_connection(db_path) as connection:
        for table_name in table_names:
            hasher.update(f"table:{table_name}\n".encode("utf-8"))
            _hash_table_rows(hasher, connection, table_name)
    return hasher.hexdigest()


# Tables whose contents define each published database version. Order is part of
# the hash input and must not change (see _hash_table_rows).
MAIN_VERSION_TABLES = (
    "routes",
    "paths",
    "stops",
    "path_points",
    "operators",
    "route_operators",
    "route_schedules",
    "stations",
    "station_stops",
)
DOWNLOAD_VERSION_TABLES = ("routes", "paths")
CITY_VERSION_TABLES = ("stops",)

# Tables large enough that summing text lengths costs more than it is worth.
# path_points holds coordinates, so its numeric totals already capture changes.
_SHAPE_DIGEST_NUMERIC_ONLY_TABLES = frozenset({"path_points"})


def _shape_digest(db_path: str | Path, table_names: tuple[str, ...]) -> str:
    """Summarize the hashed tables cheaply, ignoring everything else in the file.

    Aggregates row counts, numeric totals and text-length totals into one digest.
    Runtime writes to non-hashed tables (tdx_fetch_state, stop_travel_times) move
    the file's mtime but cannot move these numbers, which is what lets a boot
    after serving traffic skip the full hash.

    This is a fingerprint, not a proof: a change preserving every count and total
    would pass. It is only ever consulted for the main database, only after the
    file-identity check fails, and never by sync_static.
    """
    hasher = hashlib.sha256()
    with get_readonly_connection(db_path) as connection:
        for table_name in table_names:
            columns_info = connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            column_names = [row["name"] for row in columns_info]
            if not column_names:
                hasher.update(f"table:{table_name}:absent\n".encode("utf-8"))
                continue

            selects = ["COUNT(*)"]
            include_text_lengths = table_name not in _SHAPE_DIGEST_NUMERIC_ONLY_TABLES
            for name in column_names:
                selects.append(f'TOTAL(CAST("{name}" AS REAL))')
                if include_text_lengths:
                    selects.append(f'TOTAL(LENGTH(CAST("{name}" AS TEXT)))')
            row = connection.execute(
                f'SELECT {", ".join(selects)} FROM "{table_name}"'
            ).fetchone()
            hasher.update(f"table:{table_name}:{tuple(row)!r}\n".encode("utf-8"))
    return hasher.hexdigest()


def _stat_fingerprint(db_path: Path) -> tuple[int, int, int] | None:
    """Return ``(size, mtime_ns, inode)`` for a database file, or None if absent."""
    try:
        stat_result = db_path.stat()
    except OSError:
        return None
    return (
        stat_result.st_size,
        stat_result.st_mtime_ns,
        getattr(stat_result, "st_ino", 0) or 0,
    )


def _load_version_fingerprints(app_db_path: str | Path) -> dict[str, sqlite3.Row]:
    try:
        with get_readonly_connection(app_db_path) as connection:
            rows = connection.execute(
                """
                SELECT name, db_path, file_size, file_mtime_ns, file_inode,
                       shape_digest, content_hash
                FROM database_version_fingerprints
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {row["name"]: row for row in rows}


def _store_version_fingerprint(
    connection: sqlite3.Connection,
    name: str,
    db_path: Path,
    stat_fingerprint: tuple[int, int, int] | None,
    shape_digest: str,
    content_hash: str,
    now: int,
) -> None:
    if stat_fingerprint is None:
        return
    file_size, file_mtime_ns, file_inode = stat_fingerprint
    connection.execute(
        """
        INSERT INTO database_version_fingerprints
            (name, db_path, file_size, file_mtime_ns, file_inode,
             shape_digest, content_hash, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            db_path = excluded.db_path,
            file_size = excluded.file_size,
            file_mtime_ns = excluded.file_mtime_ns,
            file_inode = excluded.file_inode,
            shape_digest = excluded.shape_digest,
            content_hash = excluded.content_hash,
            checked_at = excluded.checked_at
        """,
        (
            name,
            str(db_path),
            file_size,
            file_mtime_ns,
            file_inode,
            shape_digest,
            content_hash,
            now,
        ),
    )


def _fingerprint_matches_file(
    row: sqlite3.Row | None,
    db_path: Path,
    stat_fingerprint: tuple[int, int, int] | None,
) -> bool:
    """True when the file is byte-for-byte the one the fingerprint was taken from."""
    if row is None or stat_fingerprint is None:
        return False
    return (
        row["db_path"] == str(db_path)
        and row["file_size"] == stat_fingerprint[0]
        and row["file_mtime_ns"] == stat_fingerprint[1]
        and row["file_inode"] == stat_fingerprint[2]
    )


def _load_versioned_names(main_db_path: str | Path) -> set[str]:
    try:
        with get_readonly_connection(main_db_path) as connection:
            rows = connection.execute("SELECT name FROM database_versions").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def _upsert_database_version(
    connection: sqlite3.Connection,
    name: str,
    content_hash: str,
    now: int,
    *,
    force: bool = False,
) -> dict[str, int | str | bool]:
    row = connection.execute(
        "SELECT name, version, content_hash, updated_at FROM database_versions WHERE name = ?",
        (name,),
    ).fetchone()

    if row is None:
        connection.execute(
            """
            INSERT INTO database_versions (name, version, content_hash, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (name, 1, content_hash, now),
        )
        return {"name": name, "version": 1, "updated_at": now, "changed": True}

    current_version = int(row["version"])
    current_hash = row["content_hash"]
    if current_hash == content_hash and not force:
        return {
            "name": name,
            "version": current_version,
            "updated_at": int(row["updated_at"]),
            "changed": False,
        }

    next_version = current_version + 1
    connection.execute(
        """
        UPDATE database_versions
        SET version = ?, content_hash = ?, updated_at = ?
        WHERE name = ?
        """,
        (next_version, content_hash, now, name),
    )
    return {"name": name, "version": next_version, "updated_at": now, "changed": True}


def _build_version_entries(
    main_db_path: str | Path,
    download_db_path: str | Path,
    city_db_paths: dict[str, Path] | None,
) -> list[tuple[str, Path, tuple[str, ...]]]:
    entries: list[tuple[str, Path, tuple[str, ...]]] = [
        ("main", Path(main_db_path), MAIN_VERSION_TABLES),
        ("download", Path(download_db_path), DOWNLOAD_VERSION_TABLES),
    ]
    for city_name, city_path in sorted((city_db_paths or {}).items()):
        entries.append((city_name, Path(city_path), CITY_VERSION_TABLES))
    return entries


def refresh_database_versions(
    main_db_path: str | Path,
    *,
    download_db_path: str | Path,
    city_db_paths: dict[str, Path] | None = None,
    force: bool = False,
    app_db_path: str | Path | None = None,
    trust_fingerprints: bool = False,
) -> list[dict[str, int | str | bool]]:
    """Recompute each database's content hash and bump its version when it changed.

    Hashing every row of the static databases takes about ten seconds, and on a
    normal restart it produces the hash already stored -- static data only changes
    via sync_static, which refreshes versions itself. The optional arguments let a
    caller skip that work:

    ``app_db_path`` -- where to persist change-detection fingerprints. Passing it
    alone changes nothing observable.

    ``trust_fingerprints`` -- consult those fingerprints and skip hashing any
    database that has not changed. sync_static deliberately leaves this False so
    the authoritative post-sync hash is always exact, while still refreshing the
    stamps so the next boot is fast.
    """
    entries = _build_version_entries(main_db_path, download_db_path, city_db_paths)

    use_gate = app_db_path is not None and trust_fingerprints and not force
    stored: dict[str, sqlite3.Row] = {}
    versioned_names: set[str] = set()
    if use_gate:
        stored = _load_version_fingerprints(app_db_path)
        versioned_names = _load_versioned_names(main_db_path)

    hashes: dict[str, str] = {}
    shape_digests: dict[str, str] = {}
    skipped: set[str] = set()

    for name, db_path, tables in entries:
        if not db_path.exists():
            continue

        row = stored.get(name) if use_gate else None
        gate_open = row is not None and name in versioned_names
        if gate_open:
            stat_fingerprint = _stat_fingerprint(db_path)
            if _fingerprint_matches_file(row, db_path, stat_fingerprint):
                skipped.add(name)
                continue

            # The file moved but that may only be a write to a non-hashed table.
            if name == "main" and row["shape_digest"]:
                digest = _shape_digest(db_path, tables)
                shape_digests[name] = digest
                if digest == row["shape_digest"]:
                    skipped.add(name)
                    continue

        hashes[name] = hash_tables(db_path, tables)
        if name == "main" and name not in shape_digests:
            shape_digests[name] = _shape_digest(db_path, tables)

    if not hashes:
        if app_db_path is not None and skipped:
            _restamp_fingerprints(app_db_path, entries, skipped, stored, shape_digests)
        return []

    now = int(time.time())
    with get_connection(main_db_path) as connection:
        results = [
            _upsert_database_version(connection, name, content_hash, now, force=force)
            for name, content_hash in hashes.items()
        ]
        connection.commit()

    # The upsert above just wrote to the main database, changing its own size and
    # mtime. Stat only now: the connection has been closed, so WAL contents have
    # been checkpointed into the file. Stamping any earlier records a value the
    # checkpoint immediately invalidates, and every later boot would rehash.
    if app_db_path is not None:
        with get_connection(app_db_path) as app_connection:
            for name, db_path, _tables in entries:
                if name in hashes:
                    content_hash = hashes[name]
                elif name in skipped:
                    content_hash = stored[name]["content_hash"]
                else:
                    continue
                _store_version_fingerprint(
                    app_connection,
                    name,
                    db_path,
                    _stat_fingerprint(db_path),
                    _resolve_shape_digest(name, shape_digests, stored),
                    content_hash,
                    now,
                )
            app_connection.commit()

    return results


def _resolve_shape_digest(
    name: str,
    shape_digests: dict[str, str],
    stored: dict[str, sqlite3.Row],
) -> str:
    """Keep the previously stored digest when this run had no reason to compute one.

    A database skipped on file identity alone never needs its shape digest, but
    overwriting it with an empty string would disable that fallback for every
    later run.
    """
    digest = shape_digests.get(name)
    if digest is not None:
        return digest
    row = stored.get(name)
    return "" if row is None else row["shape_digest"]


def _restamp_fingerprints(
    app_db_path: str | Path,
    entries: list[tuple[str, Path, tuple[str, ...]]],
    skipped: set[str],
    stored: dict[str, sqlite3.Row],
    shape_digests: dict[str, str],
) -> None:
    """Re-stamp databases that were skipped, so the next boot skips them sooner.

    Without this, a database that only passed the shape-digest check would repeat
    that scan on every boot forever.
    """
    now = int(time.time())
    with get_connection(app_db_path) as connection:
        for name, db_path, _tables in entries:
            if name not in skipped:
                continue
            row = stored.get(name)
            if row is None:
                continue
            _store_version_fingerprint(
                connection,
                name,
                db_path,
                _stat_fingerprint(db_path),
                shape_digests.get(name, row["shape_digest"]),
                row["content_hash"],
                now,
            )
        connection.commit()


def main_db_unchanged(main_db_path: str | Path, app_db_path: str | Path) -> bool:
    """True when the static database matches the fingerprint last recorded for it."""
    main_path = Path(main_db_path)
    if not main_path.exists():
        return False
    row = _load_version_fingerprints(app_db_path).get("main")
    if row is None or "main" not in _load_versioned_names(main_db_path):
        return False
    if _fingerprint_matches_file(row, main_path, _stat_fingerprint(main_path)):
        return True
    if not row["shape_digest"]:
        return False
    return _shape_digest(main_path, MAIN_VERSION_TABLES) == row["shape_digest"]


def export_download_db_if_stale(
    main_db_path: str | Path,
    target_db_path: str | Path,
    app_db_path: str | Path,
) -> bool:
    """Rebuild the download database unless the static database is unchanged.

    Returns True when an export ran. Skipping also keeps the download database's
    own mtime stable, so its fingerprint keeps matching too.
    """
    if Path(target_db_path).exists() and main_db_unchanged(main_db_path, app_db_path):
        return False
    export_download_db(main_db_path, target_db_path)
    return True


def load_database_version(connection: sqlite3.Connection, name: str) -> dict | None:
    row = connection.execute(
        "SELECT name, version, updated_at FROM database_versions WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": int(row["version"]),
        "updated_at": int(row["updated_at"]),
    }
