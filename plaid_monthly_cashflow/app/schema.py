"""Database schema and forward-only, idempotent migrations.

Design rules enforced by this module
------------------------------------
1. Financial history is **append-only**. ``transaction_events`` and
   ``account_observations`` are written with ``INSERT`` only. There is no
   ``DELETE``, ``DROP``, ``TRUNCATE`` or ``UPDATE`` against either table
   anywhere in the application.
2. Legacy tables (``transactions``, ``accounts``, ``sync_log``) are never
   dropped or renamed. Migration *copies* out of them; the originals stay as a
   frozen snapshot of what the pre-ledger add-on held.
3. Every migration is wrapped in a single SQLite transaction and is safe to run
   repeatedly. Re-running is a no-op, not a duplicate import.
4. Current state is derived by *views* over the ledger, never by mutating it.

See DOCS.md for the guarantees and their limits.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .canonical import payload_hash, sha256_text
from .version import APP_VERSION, SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Base schema (v1) -- the original tables, created only when missing.
# ---------------------------------------------------------------------------

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    access_token_encrypted TEXT NOT NULL,
    plaid_env TEXT,
    institution_id TEXT,
    institution_name TEXT,
    cursor TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    item_id TEXT,
    name TEXT,
    official_name TEXT,
    type TEXT,
    subtype TEXT,
    mask TEXT,
    current_balance REAL,
    available_balance REAL,
    iso_currency_code TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id TEXT,
    item_id TEXT,
    date TEXT,
    authorized_date TEXT,
    name TEXT,
    merchant_name TEXT,
    amount REAL,
    iso_currency_code TEXT,
    category_json TEXT,
    personal_finance_category_json TEXT,
    pending INTEGER,
    removed INTEGER DEFAULT 0,
    raw_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    message TEXT,
    added_count INTEGER,
    modified_count INTEGER,
    removed_count INTEGER
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    app_version TEXT
);
"""


# ---------------------------------------------------------------------------
# v2 -- the append-only ledger.
# ---------------------------------------------------------------------------

# Event types recorded in the ledger.
EVENT_ADDED = "added"
EVENT_MODIFIED = "modified"
EVENT_REMOVED = "removed"
EVENT_HISTORICAL_IMPORT = "historical_import"
EVENT_LEGACY_IMPORT = "legacy_import"
EVENT_RECONCILIATION = "reconciliation"

# Event *classes* group the state-bearing event types together. Deduplication
# works on the class, not the type, so that a historical backfill which
# re-delivers a transaction already seen through transactions/sync does not
# create a second ledger row describing the identical state.
CLASS_STATE = "state"
CLASS_REMOVED = "removed"

EVENT_CLASSES: dict[str, str] = {
    EVENT_ADDED: CLASS_STATE,
    EVENT_MODIFIED: CLASS_STATE,
    EVENT_HISTORICAL_IMPORT: CLASS_STATE,
    EVENT_LEGACY_IMPORT: CLASS_STATE,
    EVENT_RECONCILIATION: CLASS_STATE,
    EVENT_REMOVED: CLASS_REMOVED,
}


def event_class(event_type: str) -> str:
    return EVENT_CLASSES.get(event_type, CLASS_STATE)


def event_identity(
    *,
    item_id: str | None,
    transaction_id: str,
    event_type: str,
    payload_digest: str,
    supersedes_event_id: int | None,
) -> str:
    """Deterministic unique key for one ledger row.

    Including ``supersedes_event_id`` -- the id of the previous event for this
    transaction at the moment of insert -- is what makes the constraint both
    safe and correct:

    * An exact retry of an already-stored event resolves to the same
      ``supersedes_event_id`` and the same payload hash, so it collides and is
      ignored. Crash-then-retry never duplicates.
    * A genuine change from Plaid has a different payload hash, so it inserts.
    * A revert *back* to a previously seen payload still inserts, because the
      event it supersedes is different. Without the supersedes component a
      revert would be silently swallowed and current state would be wrong.
    """
    return sha256_text(
        item_id or "",
        transaction_id,
        event_class(event_type),
        payload_digest,
        supersedes_event_id or 0,
    )


LEDGER_SCHEMA = """
-- Immutable, append-only ledger of every transaction state Plaid has ever
-- reported to this add-on. Rows are only ever INSERTed.
CREATE TABLE IF NOT EXISTS transaction_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_identity TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    event_class TEXT NOT NULL,
    supersedes_event_id INTEGER,

    plaid_transaction_id TEXT NOT NULL,
    pending_transaction_id TEXT,
    account_id TEXT,
    item_id TEXT,

    sync_batch_id TEXT,
    cursor_fingerprint TEXT,

    txn_date TEXT,
    authorized_date TEXT,
    txn_datetime TEXT,
    authorized_datetime TEXT,

    name TEXT,
    merchant_name TEXT,
    original_description TEXT,
    amount REAL,
    iso_currency_code TEXT,
    unofficial_currency_code TEXT,
    pending INTEGER,
    payment_channel TEXT,
    transaction_type TEXT,
    transaction_code TEXT,

    category_json TEXT,
    category_id TEXT,
    personal_finance_category_json TEXT,
    personal_finance_category_icon_url TEXT,
    counterparties_json TEXT,
    location_json TEXT,
    payment_meta_json TEXT,
    website TEXT,
    logo_url TEXT,
    merchant_entity_id TEXT,
    check_number TEXT,
    account_owner TEXT,

    raw_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,

    received_at TEXT,
    inserted_at TEXT NOT NULL,
    plaid_env TEXT,
    app_version TEXT,
    schema_version INTEGER,

    prev_ledger_hash TEXT,
    ledger_hash TEXT
);

CREATE INDEX IF NOT EXISTS ix_txn_events_txn
    ON transaction_events (plaid_transaction_id, event_id);
CREATE INDEX IF NOT EXISTS ix_txn_events_item
    ON transaction_events (item_id, event_id);
CREATE INDEX IF NOT EXISTS ix_txn_events_account
    ON transaction_events (account_id, txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_events_date
    ON transaction_events (txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_events_pending_link
    ON transaction_events (pending_transaction_id);
CREATE INDEX IF NOT EXISTS ix_txn_events_class
    ON transaction_events (plaid_transaction_id, event_class, event_id);

-- Append-only observations of account and institution metadata. Balances move,
-- so a changed observation is a new row rather than an overwrite.
CREATE TABLE IF NOT EXISTS account_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_identity TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    item_id TEXT,
    institution_id TEXT,
    institution_name TEXT,
    name TEXT,
    official_name TEXT,
    type TEXT,
    subtype TEXT,
    mask TEXT,
    iso_currency_code TEXT,
    unofficial_currency_code TEXT,
    current_balance REAL,
    available_balance REAL,
    limit_balance REAL,
    source TEXT,
    raw_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    plaid_env TEXT,
    app_version TEXT
);

CREATE INDEX IF NOT EXISTS ix_account_obs_account
    ON account_observations (account_id, observation_id);
CREATE INDEX IF NOT EXISTS ix_account_obs_item
    ON account_observations (item_id, observation_id);

-- Append-only per-item sync audit. Only operational columns (finished_at,
-- status, counts) are UPDATEd, and only for the run that is still in flight.
CREATE TABLE IF NOT EXISTS sync_runs (
    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    item_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    mode TEXT,
    starting_cursor_fingerprint TEXT,
    ending_cursor_fingerprint TEXT,
    added_count INTEGER NOT NULL DEFAULT 0,
    modified_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    inserted_event_count INTEGER NOT NULL DEFAULT 0,
    duplicate_event_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    earliest_transaction_date TEXT,
    latest_transaction_date TEXT,
    error_class TEXT,
    error_message TEXT,
    app_version TEXT
);

CREATE INDEX IF NOT EXISTS ix_sync_runs_item
    ON sync_runs (item_id, sync_id);
CREATE INDEX IF NOT EXISTS ix_sync_runs_status
    ON sync_runs (status, sync_id);

-- Operational bookkeeping for the one-time historical backfill. Contains no
-- financial history, so ordinary UPDATE is appropriate here.
CREATE TABLE IF NOT EXISTS backfill_state (
    item_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    requested_start_date TEXT,
    requested_end_date TEXT,
    earliest_transaction_date TEXT,
    latest_transaction_date TEXT,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    last_error TEXT
);
"""


# The latest known state of every transaction, derived from the ledger.
#
# ``state`` is the newest state-class event (added / modified / imported).
# ``removals`` is the newest removal event. A transaction counts as removed
# only when its removal is newer than its newest state event, so a Plaid
# remove-then-re-add sequence resolves correctly.
LEDGER_VIEWS = """
DROP VIEW IF EXISTS transaction_latest_state;
CREATE VIEW transaction_latest_state AS
WITH ids AS (
    SELECT DISTINCT plaid_transaction_id FROM transaction_events
),
newest_state AS (
    SELECT e.*
    FROM transaction_events e
    JOIN (
        SELECT plaid_transaction_id, MAX(event_id) AS event_id
        FROM transaction_events
        WHERE event_class = 'state'
        GROUP BY plaid_transaction_id
    ) m ON m.event_id = e.event_id
),
newest_removal AS (
    SELECT plaid_transaction_id, MAX(event_id) AS removal_event_id
    FROM transaction_events
    WHERE event_class = 'removed'
    GROUP BY plaid_transaction_id
)
SELECT
    ids.plaid_transaction_id                          AS transaction_id,
    s.event_id                                        AS state_event_id,
    r.removal_event_id                                AS removal_event_id,
    s.event_type                                      AS event_type,
    s.item_id                                         AS item_id,
    s.account_id                                      AS account_id,
    s.pending_transaction_id                          AS pending_transaction_id,
    s.txn_date                                        AS date,
    s.authorized_date                                 AS authorized_date,
    s.txn_datetime                                    AS datetime,
    s.name                                            AS name,
    s.merchant_name                                   AS merchant_name,
    s.amount                                          AS amount,
    s.iso_currency_code                               AS iso_currency_code,
    s.unofficial_currency_code                        AS unofficial_currency_code,
    s.category_json                                   AS category_json,
    s.category_id                                     AS category_id,
    s.personal_finance_category_json                  AS personal_finance_category_json,
    s.personal_finance_category_icon_url              AS personal_finance_category_icon_url,
    s.payment_channel                                 AS payment_channel,
    s.authorized_datetime                             AS authorized_datetime,
    s.original_description                            AS original_description,
    s.transaction_type                                AS transaction_type,
    s.transaction_code                                AS transaction_code,
    s.counterparties_json                             AS counterparties_json,
    s.location_json                                   AS location_json,
    s.payment_meta_json                               AS payment_meta_json,
    s.website                                         AS website,
    s.logo_url                                        AS logo_url,
    s.merchant_entity_id                              AS merchant_entity_id,
    s.check_number                                    AS check_number,
    s.account_owner                                   AS account_owner,
    s.raw_json                                        AS raw_json,
    s.inserted_at                                     AS first_seen_at,
    COALESCE(s.pending, 0)                            AS pending,
    CASE
        WHEN r.removal_event_id IS NOT NULL
             AND (s.event_id IS NULL OR r.removal_event_id > s.event_id)
        THEN 1 ELSE 0
    END                                               AS removed
FROM ids
LEFT JOIN newest_state  s ON s.plaid_transaction_id = ids.plaid_transaction_id
LEFT JOIN newest_removal r ON r.plaid_transaction_id = ids.plaid_transaction_id;

-- Adds pending-to-posted linkage on top of latest state.
--
-- When Plaid posts a pending transaction it usually issues a *new*
-- transaction_id and points at the old one through pending_transaction_id.
-- The pending row must stay in the ledger and stay queryable, but it must not
-- be counted a second time in dashboard totals, so it is flagged superseded.
DROP VIEW IF EXISTS transaction_current_state;
CREATE VIEW transaction_current_state AS
SELECT
    ls.*,
    CASE WHEN EXISTS (
        SELECT 1 FROM transaction_latest_state posted
        WHERE posted.pending_transaction_id = ls.transaction_id
          AND posted.removed = 0
    ) THEN 1 ELSE 0 END AS superseded
FROM transaction_latest_state ls;

-- The rows the dashboard is allowed to total: newest state, not removed, not
-- superseded by a posted counterpart.
DROP VIEW IF EXISTS transaction_active_state;
CREATE VIEW transaction_active_state AS
SELECT * FROM transaction_current_state
WHERE removed = 0 AND superseded = 0;

-- Newest metadata observation per account, derived from the append-only
-- observation table.
DROP VIEW IF EXISTS account_latest_state;
CREATE VIEW account_latest_state AS
SELECT o.*
FROM account_observations o
JOIN (
    SELECT account_id, MAX(observation_id) AS observation_id
    FROM account_observations
    GROUP BY account_id
) m ON m.observation_id = o.observation_id;
"""


REQUIRED_TABLES = (
    "settings",
    "items",
    "accounts",
    "transactions",
    "sync_log",
    "schema_migrations",
    "transaction_events",
    "account_observations",
    "sync_runs",
    "backfill_state",
)

REQUIRED_VIEWS = (
    "transaction_latest_state",
    "transaction_current_state",
    "transaction_active_state",
    "account_latest_state",
)

REQUIRED_INDEXES = (
    "ix_txn_events_txn",
    "ix_txn_events_item",
    "ix_txn_events_account",
    "ix_txn_events_date",
    "ix_txn_events_pending_link",
    "ix_txn_events_class",
    "ix_account_obs_account",
    "ix_account_obs_item",
    "ix_sync_runs_item",
    "ix_sync_runs_status",
)


def split_statements(script: str) -> list[str]:
    """Split trusted DDL into individual statements.

    ``sqlite3.Connection.executescript`` implicitly COMMITs any transaction in
    flight, which would silently break the atomicity of a migration. Migrations
    therefore execute statement by statement inside an explicit transaction.

    The splitter only has to cope with the DDL in this module: single-quoted
    literals and ``--`` line comments. It is not a general SQL parser and is
    never fed user input.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_string = False
    in_comment = False
    index = 0

    while index < len(script):
        char = script[index]
        if in_comment:
            if char == "\n":
                in_comment = False
                buffer.append(char)
            index += 1
            continue
        if in_string:
            buffer.append(char)
            if char == "'":
                # '' is an escaped quote inside a SQLite string literal.
                if index + 1 < len(script) and script[index + 1] == "'":
                    buffer.append("'")
                    index += 2
                    continue
                in_string = False
            index += 1
            continue
        if char == "'":
            in_string = True
            buffer.append(char)
            index += 1
            continue
        if char == "-" and index + 1 < len(script) and script[index + 1] == "-":
            in_comment = True
            index += 2
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def exec_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute trusted DDL without disturbing the surrounding transaction."""
    for statement in split_statements(script):
        conn.execute(statement)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migration_001_base(conn: sqlite3.Connection, now: str) -> None:
    """Bring a pre-existing database up to the last pre-ledger shape.

    This is intentionally additive only. The destructive statements that used
    to live in ``Storage.init_db`` -- clearing ``transactions.raw_json`` and
    nulling every account metadata column on every single startup -- are gone
    and are not replaced by anything.
    """
    exec_script(conn, BASE_SCHEMA)
    _add_column_if_missing(conn, "items", "plaid_env", "plaid_env TEXT")
    # Disconnect must be able to stop syncing without erasing anything, so an
    # item carries an explicit lifecycle instead of being deleted.
    _add_column_if_missing(conn, "items", "active", "active INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(conn, "items", "disconnected_at", "disconnected_at TEXT")


def _migration_002_ledger(conn: sqlite3.Connection, now: str) -> None:
    """Create the ledger and import every existing legacy row exactly once."""
    exec_script(conn, LEDGER_SCHEMA)
    exec_script(conn, LEDGER_VIEWS)
    _import_legacy_transactions(conn, now)
    _import_legacy_accounts(conn, now)


def _import_legacy_transactions(conn: sqlite3.Connection, now: str) -> None:
    """Copy rows from the legacy ``transactions`` table into the ledger.

    The legacy table is left completely untouched -- this reads from it and
    never writes to or drops it. Import is idempotent because every generated
    row carries a deterministic ``event_identity``; a second run collides and
    is ignored.

    Legacy rows that were flagged ``removed = 1`` produce two events: a
    ``legacy_import`` state event holding whatever fields survive, followed by
    a ``removed`` event. That keeps the pre-removal version queryable instead
    of destroying it, which is exactly what the old in-place UPDATE did.
    """
    if "transactions" not in _existing_tables(conn):
        return

    columns = _column_names(conn, "transactions")
    rows = conn.execute("SELECT * FROM transactions ORDER BY rowid").fetchall()

    for row in rows:
        data = {key: row[key] for key in columns}
        transaction_id = data.get("transaction_id")
        if not transaction_id:
            continue

        # Preserve the original raw payload when one survived; otherwise
        # reconstruct the best available payload from the legacy columns.
        raw = _decode_json(data.get("raw_json"))
        payload = raw if isinstance(raw, dict) and raw else _legacy_payload(data)

        digest = payload_hash(payload)
        _insert_legacy_event(
            conn,
            payload=payload,
            digest=digest,
            transaction_id=str(transaction_id),
            item_id=data.get("item_id"),
            account_id=data.get("account_id"),
            event_type=EVENT_LEGACY_IMPORT,
            now=now,
            legacy=data,
        )

        if int(data.get("removed") or 0):
            removal_payload = {"transaction_id": str(transaction_id), "source": "legacy_removed_flag"}
            _insert_legacy_event(
                conn,
                payload=removal_payload,
                digest=payload_hash(removal_payload),
                transaction_id=str(transaction_id),
                item_id=data.get("item_id"),
                account_id=data.get("account_id"),
                event_type=EVENT_REMOVED,
                now=now,
                legacy=data,
            )


def _legacy_payload(data: dict) -> dict:
    """Rebuild a Plaid-shaped payload from the legacy column set."""
    payload = {
        "transaction_id": data.get("transaction_id"),
        "account_id": data.get("account_id"),
        "date": data.get("date"),
        "authorized_date": data.get("authorized_date"),
        "name": data.get("name"),
        "merchant_name": data.get("merchant_name"),
        "amount": data.get("amount"),
        "iso_currency_code": data.get("iso_currency_code"),
        "category": _decode_json(data.get("category_json")),
        "personal_finance_category": _decode_json(data.get("personal_finance_category_json")),
        "pending": bool(data.get("pending")),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _insert_legacy_event(
    conn: sqlite3.Connection,
    *,
    payload: dict,
    digest: str,
    transaction_id: str,
    item_id: object,
    account_id: object,
    event_type: str,
    now: str,
    legacy: dict,
) -> None:
    from .canonical import canonical_json

    supersedes = _latest_event_id(conn, transaction_id)
    identity = event_identity(
        item_id=str(item_id) if item_id else None,
        transaction_id=transaction_id,
        event_type=event_type,
        payload_digest=digest,
        supersedes_event_id=supersedes,
    )
    conn.execute(
        """
        INSERT INTO transaction_events (
            event_identity, event_type, event_class, supersedes_event_id,
            plaid_transaction_id, account_id, item_id,
            txn_date, authorized_date, name, merchant_name, amount,
            iso_currency_code, category_json, personal_finance_category_json,
            pending, raw_json, payload_hash, received_at, inserted_at,
            app_version, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_identity) DO NOTHING
        """,
        (
            identity,
            event_type,
            event_class(event_type),
            supersedes,
            transaction_id,
            account_id,
            item_id,
            legacy.get("date"),
            legacy.get("authorized_date"),
            legacy.get("name"),
            legacy.get("merchant_name"),
            legacy.get("amount"),
            legacy.get("iso_currency_code"),
            legacy.get("category_json"),
            legacy.get("personal_finance_category_json"),
            1 if legacy.get("pending") else 0,
            canonical_json(payload),
            digest,
            legacy.get("updated_at"),
            now,
            APP_VERSION,
            SCHEMA_VERSION,
        ),
    )


def _import_legacy_accounts(conn: sqlite3.Connection, now: str) -> None:
    """Copy the legacy ``accounts`` rows into the observation table.

    Most installations will find these columns already NULL: the old
    ``init_db`` wiped account metadata on every startup and ``upsert_accounts``
    then wrote NULL back. The account/item linkage is still worth keeping, and
    real metadata repopulates on the next sync now that both destructive paths
    are gone.
    """
    if "accounts" not in _existing_tables(conn):
        return

    from .canonical import canonical_json

    columns = _column_names(conn, "accounts")
    for row in conn.execute("SELECT * FROM accounts ORDER BY rowid").fetchall():
        data = {key: row[key] for key in columns}
        account_id = data.get("account_id")
        if not account_id:
            continue
        payload = {key: value for key, value in data.items() if value is not None}
        digest = payload_hash(payload)
        identity = sha256_text(data.get("item_id") or "", account_id, digest, "legacy_import")
        conn.execute(
            """
            INSERT INTO account_observations (
                observation_identity, account_id, item_id, name, official_name,
                type, subtype, mask, iso_currency_code, current_balance,
                available_balance, source, raw_json, payload_hash, observed_at,
                app_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_identity) DO NOTHING
            """,
            (
                identity,
                account_id,
                data.get("item_id"),
                data.get("name"),
                data.get("official_name"),
                data.get("type"),
                data.get("subtype"),
                data.get("mask"),
                data.get("iso_currency_code"),
                data.get("current_balance"),
                data.get("available_balance"),
                EVENT_LEGACY_IMPORT,
                canonical_json(payload),
                digest,
                data.get("updated_at") or now,
                APP_VERSION,
            ),
        )


def _latest_event_id(conn: sqlite3.Connection, transaction_id: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(event_id) AS event_id FROM transaction_events WHERE plaid_transaction_id = ?",
        (transaction_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _decode_json(value: object) -> object:
    if not value:
        return None
    try:
        import json

        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection, str], None]], ...] = (
    (1, "base_schema", _migration_001_base),
    (2, "append_only_transaction_ledger", _migration_002_ledger),
)


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    if "schema_migrations" not in _existing_tables(conn):
        return set()
    return {int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def current_version(conn: sqlite3.Connection) -> int:
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def migrate(conn: sqlite3.Connection, *, now: str) -> list[int]:
    """Apply every outstanding migration atomically.

    Each migration runs inside its own explicit transaction. On failure the
    transaction is rolled back and the exception propagates, so a partially
    applied migration can never be recorded as complete.
    """
    exec_script(conn, BASE_SCHEMA)
    done = applied_versions(conn)
    applied: list[int] = []

    for version, name, handler in MIGRATIONS:
        if version in done:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            handler(conn, now)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, app_version) VALUES (?, ?, ?, ?)",
                (version, name, now, APP_VERSION),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)

    # Views are cheap and are recreated on every start so that an add-on
    # upgrade picks up a corrected definition without needing a new migration.
    # Recreating a view touches no rows.
    if current_version(conn) >= 2:
        exec_script(conn, LEDGER_VIEWS)

    return applied
