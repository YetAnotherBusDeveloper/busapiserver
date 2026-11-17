"""Operator-provisioned demo credentials, isolated from application logins.

    python -m app.mcp_review setup --username yabus-review
    python -m app.mcp_review disable

Passwords are entered through getpass, never command arguments or output.
Hosts without a terminal can provision at startup from a private bootstrap file.
No demo account or login is enabled without explicit operator configuration.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
from datetime import datetime, timezone

from app.account_sync_service import upsert_sync_document
from app.auth_service import generate_snowflake
from app.config import Settings, get_settings
from app.db import get_connection, init_app_db
from app.migrate_favorite_routeids import route_key_for_routeid

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_review_account (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))
);
"""
ITERATIONS = 600_000
BOOTSTRAP_FILENAME = "mcp-review-bootstrap.json"


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256$600000$" + "$".join(
        base64.b64encode(value).decode("ascii") for value in (salt, key)
    )


def _verify_password(password: str, encoded: str) -> bool:
    if not 20 <= len(password) <= 256:
        return False
    try:
        algorithm, iterations, salt, key = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or iterations != str(ITERATIONS):
            return False
        decoded_salt = base64.b64decode(salt, validate=True)
        expected = base64.b64decode(key, validate=True)
        if len(decoded_salt) != 32 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), decoded_salt, ITERATIONS
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, UnicodeError):
        return False


def _valid_password_hash(encoded: str) -> bool:
    try:
        algorithm, iterations, salt, key = encoded.split("$")
        return (
            algorithm == "pbkdf2_sha256"
            and iterations == str(ITERATIONS)
            and len(base64.b64decode(salt, validate=True)) == 32
            and len(base64.b64decode(key, validate=True)) == 32
        )
    except (ValueError, UnicodeError):
        return False


def _isolated_account(db, account_id: int) -> bool:
    row = db.execute("SELECT role FROM accounts WHERE id=?", (account_id,)).fetchone()
    if row is None or row["role"] != "user":
        return False
    return not any(
        db.execute(
            f"SELECT 1 FROM {table} WHERE account_id=? LIMIT 1", (account_id,)
        ).fetchone()
        for table in (
            "account_oauth_identities",
            "account_devices",
            "account_device_tokens",
        )
    )


def active_review_account(settings: Settings):
    with get_connection(settings.app_db_path) as db:
        row = db.execute(
            "SELECT account_id,username,password_hash FROM mcp_review_account "
            "WHERE singleton=1 AND enabled=1"
        ).fetchone()
        if row is not None and _isolated_account(db, row["account_id"]):
            return row
    return None


def create_review_session(
    settings: Settings, username: str, password: str
) -> str | None:
    row = active_review_account(settings)
    if row is None:
        return None
    password_ok = _verify_password(password, row["password_hash"])
    username_ok = hmac.compare_digest(
        username.encode("utf-8"), row["username"].encode("utf-8")
    )
    if not password_ok or not username_ok:
        return None
    session = secrets.token_urlsafe(32)
    with get_connection(settings.app_db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT account_id,password_hash FROM mcp_review_account "
            "WHERE singleton=1 AND enabled=1"
        ).fetchone()
        if (
            current is None
            or current["account_id"] != row["account_id"]
            or not hmac.compare_digest(current["password_hash"], row["password_hash"])
            or not _isolated_account(db, row["account_id"])
        ):
            return None
        db.execute(
            "DELETE FROM mcp_login_sessions WHERE expires_at<=?", (int(time.time()),)
        )
        db.execute(
            "INSERT INTO mcp_login_sessions VALUES(?,?,?)",
            (
                hashlib.sha256(session.encode()).hexdigest(),
                row["account_id"],
                int(time.time()) + 600,
            ),
        )
        db.commit()
    return session


def _revoke_demo_access(db, account_id: int) -> None:
    db.execute("DELETE FROM mcp_login_sessions WHERE account_id=?", (account_id,))
    db.execute(
        "UPDATE mcp_grants SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL",
        (int(time.time()), account_id),
    )


def provision_review_account(settings: Settings, username: str, password: str) -> int:
    if not 20 <= len(password) <= 256:
        raise ValueError("Use a unique demo password containing 20-256 characters.")
    return _provision_review_account(settings, username, _hash_password(password))


def _provision_review_account(
    settings: Settings,
    username: str,
    password_hash: str,
    *,
    keep_existing: bool = False,
) -> int:
    if not re.fullmatch(r"[A-Za-z0-9@._+-]{3,120}", username):
        raise ValueError("Use a 3-120 character demo username without spaces.")
    if not _valid_password_hash(password_hash):
        raise ValueError("Invalid demo password hash format.")
    now = int(time.time())
    with get_connection(settings.app_db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT account_id,username,password_hash,enabled "
            "FROM mcp_review_account WHERE singleton=1"
        ).fetchone()
        if existing:
            account_id = existing["account_id"]
            if not _isolated_account(db, account_id):
                raise ValueError(
                    "The demo account is no longer isolated. Provisioning refused."
                )
            same_credentials = existing["username"] == username and hmac.compare_digest(
                existing["password_hash"], password_hash
            )
            if keep_existing and same_credentials and existing["enabled"]:
                return account_id
            if not keep_existing or not same_credentials:
                _revoke_demo_access(db, account_id)
        else:
            account_id = generate_snowflake(settings)
            db.execute(
                "INSERT INTO accounts(id,role,created_at,updated_at) VALUES(?,'user',?,?)",
                (account_id, now, now),
            )
        db.execute(
            "INSERT INTO mcp_review_account VALUES(1,?,?,?,0) "
            "ON CONFLICT(singleton) DO UPDATE SET "
            "username=excluded.username,password_hash=excluded.password_hash,enabled=0",
            (account_id, username, password_hash),
        )
        db.commit()

    # Seed only public transit identifiers in the new, unlinked demo account.
    # The account remains disabled if the sample document cannot be written.
    def boarding(raw_stop_id, stop_name):
        return {
            "type": "boarding",
            "provider": "tpe",
            "routeKey": route_key_for_routeid("TPE157463"),
            "routeId": "TPE157463",
            "routeName": "307莒光往撫遠街",
            "pathId": 0,
            "stopId": int(raw_stop_id),
            "rawStopId": raw_stop_id,
            "stopName": stop_name,
        }

    upsert_sync_document(
        settings,
        account_id=account_id,
        namespace="favorites",
        schema_version=2,
        payload={
            "groups": {
                "送審示範": [
                    boarding("194687", "臺北客運板橋前站(藝文)"),
                    boarding("194688", "新月橋"),
                ],
                "測試其他": [
                    {
                        "type": "route",
                        "provider": "tpe",
                        "routeKey": route_key_for_routeid("TPE157462"),
                        "routeId": "TPE157462",
                        "routeName": "307莒光往板橋前站",
                    }
                ],
            },
            "groupKinds": {"送審示範": "boarding", "測試其他": "route"},
        },
        client_modified_at=datetime.now(timezone.utc).isoformat(),
        conflict_policy="client_wins",
    )
    with get_connection(settings.app_db_path) as db:
        updated = db.execute(
            "UPDATE mcp_review_account SET enabled=1 WHERE account_id=? AND password_hash=?",
            (account_id, password_hash),
        )
        if updated.rowcount != 1:
            raise ValueError(
                "Demo credentials changed concurrently; provisioning was not enabled."
            )
        db.commit()
    return account_id


def bootstrap_review_account(settings: Settings) -> int | None:
    """Apply a private file at startup; identical configuration preserves grants."""
    path = settings.project_dir / BOOTSTRAP_FILENAME
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Cannot read the private demo bootstrap file.") from None
    try:
        if len(raw) > 4096:
            raise ValueError
        config = json.loads(raw)
        if not isinstance(config, dict) or set(config) != {
            "username",
            "password_hash",
            "enabled",
        }:
            raise ValueError
        if (
            not isinstance(config["enabled"], bool)
            or not isinstance(config["username"], str)
            or not re.fullmatch(r"[A-Za-z0-9@._+-]{3,120}", config["username"])
            or not isinstance(config["password_hash"], str)
            or not _valid_password_hash(config["password_hash"])
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        # Never include raw configuration or parser errors in startup logs.
        raise ValueError("Invalid private demo bootstrap configuration.") from None
    if not config["enabled"]:
        disable_review_account(settings)
        return None
    return _provision_review_account(
        settings, config["username"], config["password_hash"], keep_existing=True
    )


def disable_review_account(settings: Settings) -> None:
    with get_connection(settings.app_db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT account_id FROM mcp_review_account WHERE singleton=1"
        ).fetchone()
        if row:
            db.execute("UPDATE mcp_review_account SET enabled=0 WHERE singleton=1")
            _revoke_demo_access(db, row["account_id"])
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "setup", help="Create or rotate isolated demo credentials"
    )
    setup.add_argument("--username", default="yabus-review")
    commands.add_parser(
        "disable", help="Disable the demo login and revoke its authorizations"
    )
    args = parser.parse_args()
    if args.command == "setup" and not sys.stdin.isatty():
        parser.error(
            "Run setup in an interactive terminal so the password can be hidden."
        )
    password = None
    if args.command == "setup":
        password = getpass.getpass("Dedicated demo password (20-256 characters): ")
        if password != getpass.getpass("Confirm demo password: "):
            parser.error("Passwords did not match.")
    # Use normal runtime configuration loading; never display its values.
    from app.mcp_auth import MCPAuthorizationProvider

    settings = get_settings()
    init_app_db(settings.app_db_path)
    MCPAuthorizationProvider(settings).initialize()
    if args.command == "setup":
        try:
            provision_review_account(settings, args.username, password)
        except ValueError as exc:
            parser.error(str(exc))
        print("Demo login provisioned at /oauth/review-login with sample favorites.")
        print(
            "Enter the demo credentials only in the submission portal's secure fields."
        )
    else:
        disable_review_account(settings)
        print("Demo login disabled; its sessions and authorizations were revoked.")


if __name__ == "__main__":
    main()
