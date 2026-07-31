"""SQLite persistence for the add-on.

Financial history lives in ``transaction_events`` and ``account_observations``
and is **append-only**: this module contains no ``DELETE``, ``DROP``,
``TRUNCATE`` or ``VACUUM`` against any table, and no ``UPDATE`` against either
of those two tables.

Ordinary ``UPDATE`` is used only for operational metadata that is not financial
history: the Plaid sync cursor, sync-run completion status, backfill
bookkeeping, and the encrypted access token when the user explicitly
disconnects an item.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dateutil.relativedelta import relativedelta

from . import schema
from .canonical import canonical_and_hash, canonical_json, payload_hash, sha256_text
from .schema import (
    EVENT_ADDED,
    EVENT_HISTORICAL_IMPORT,
    EVENT_MODIFIED,
    EVENT_REMOVED,
    event_class,
    event_identity,
)
from .security import decrypt_text, encrypt_text, fingerprint, strip_secrets
from .version import APP_VERSION, SCHEMA_VERSION

LOGGER = logging.getLogger("plaid_monthly_cashflow.storage")

BUSY_TIMEOUT_MS = 10_000


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_json(value)


def _safe_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)[:10]
    return text or None


def _datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    return text or None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Storage:
    """Append-only storage for Plaid transaction history."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.key_path = self.db_path.with_name("local_key.key")

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    def _sidecar_paths(self) -> list[Path]:
        return [Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]

    def _chmod_private(self, *paths: Path) -> None:
        for path in paths:
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection in explicit-transaction (autocommit) mode.

        ``isolation_level = None`` disables the sqlite3 module's implicit
        transaction handling so that migrations and sync batches can control
        exactly where a commit happens. Anything that writes must go through
        :meth:`transaction`.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()
            self._chmod_private(self.db_path, *self._sidecar_paths())

    @contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Run a block inside one atomic ``BEGIN IMMEDIATE`` transaction."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        """Convenience: a connection plus a single atomic transaction."""
        with self.connect() as conn, self.transaction(conn):
            yield conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_db(self) -> list[int]:
        """Create or migrate the schema.

        This method used to clear ``transactions.raw_json`` and null out every
        account metadata column on every single start. Both statements are gone
        and nothing replaces them: startup is now strictly non-destructive.
        """
        with self.connect() as conn:
            applied = schema.migrate(conn, now=utc_now())
        self._chmod_private(self.db_path, *self._sidecar_paths())
        if applied:
            LOGGER.info("Applied database migrations: %s", applied)
        return applied

    def schema_version(self) -> int:
        with self.connect() as conn:
            return schema.current_version(conn)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def set_setting(self, key: str, value: str) -> None:
        with self.writer() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def save_item(
        self,
        *,
        item_id: str,
        access_token: str,
        plaid_env: str,
        institution_id: str | None = None,
        institution_name: str | None = None,
    ) -> None:
        encrypted = encrypt_text(access_token, self.key_path)
        now = utc_now()
        with self.writer() as conn:
            conn.execute(
                """
                INSERT INTO items (
                    item_id, access_token_encrypted, plaid_env, institution_id,
                    institution_name, cursor, created_at, updated_at, active
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 1)
                ON CONFLICT(item_id) DO UPDATE SET
                    access_token_encrypted = excluded.access_token_encrypted,
                    plaid_env = excluded.plaid_env,
                    institution_id = COALESCE(excluded.institution_id, items.institution_id),
                    institution_name = COALESCE(excluded.institution_name, items.institution_name),
                    active = 1,
                    disconnected_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (item_id, encrypted, plaid_env, institution_id, institution_name, now, now),
            )

    def get_items(
        self,
        *,
        include_tokens: bool = False,
        only_active: bool = True,
    ) -> list[dict[str, Any]]:
        clause = "WHERE active = 1" if only_active else ""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_id, access_token_encrypted, plaid_env, institution_id, "
                "institution_name, cursor, created_at, updated_at, active, disconnected_at "
                f"FROM items {clause} ORDER BY created_at"
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            encrypted = item.pop("access_token_encrypted", None)
            item["has_access_token"] = bool(encrypted)
            if include_tokens and encrypted:
                try:
                    item["access_token"] = decrypt_text(str(encrypted), self.key_path)
                except Exception:
                    # A token that cannot be decrypted stops that item syncing.
                    # It never justifies touching stored financial history.
                    LOGGER.warning("Stored Plaid token for an item could not be decrypted")
                    continue
            items.append(item)
        return items

    def reconcile_item_environments(self) -> None:
        with self.writer() as conn:
            rows = conn.execute(
                "SELECT item_id, access_token_encrypted FROM items "
                "WHERE (plaid_env IS NULL OR plaid_env = '') AND access_token_encrypted != ''"
            ).fetchall()
            for row in rows:
                try:
                    token = decrypt_text(str(row["access_token_encrypted"]), self.key_path)
                except Exception:
                    continue
                environment = None
                if token.startswith("access-sandbox-"):
                    environment = "sandbox"
                elif token.startswith("access-production-"):
                    environment = "production"
                if environment:
                    conn.execute(
                        "UPDATE items SET plaid_env = ?, updated_at = ? WHERE item_id = ?",
                        (environment, utc_now(), row["item_id"]),
                    )

    def connection_environment(self) -> str | None:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT plaid_env FROM items "
                "WHERE active = 1 AND plaid_env IS NOT NULL AND plaid_env != '' ORDER BY plaid_env"
            ).fetchall()
            unknown = conn.execute(
                "SELECT COUNT(*) AS count FROM items "
                "WHERE active = 1 AND (plaid_env IS NULL OR plaid_env = '')"
            ).fetchone()
        environments = [str(row["plaid_env"]) for row in rows]
        if unknown and int(unknown["count"]) > 0:
            environments.append("unknown")
        return environments[0] if len(environments) == 1 else "mixed" if environments else None

    def connection_requires_reset(self, configured_env: str) -> bool:
        environment = self.connection_environment()
        return environment is not None and environment != configured_env

    def update_item_cursor(self, item_id: str, cursor: str | None) -> None:
        """Advance the stored sync cursor. Operational metadata, not history."""
        with self.writer() as conn:
            self._update_cursor(conn, item_id, cursor)

    @staticmethod
    def _update_cursor(conn: sqlite3.Connection, item_id: str, cursor: str | None) -> None:
        conn.execute(
            "UPDATE items SET cursor = ?, updated_at = ? WHERE item_id = ?",
            (cursor, utc_now(), item_id),
        )

    def deactivate_item(self, item_id: str, *, clear_token: bool = True) -> bool:
        """Stop syncing an item and optionally forget its access token.

        This is the *only* disconnect behaviour. It deliberately touches no
        transaction, account, sync, or audit history: the ledger keeps every
        event that was ever recorded for the item, and the local encryption key
        is left in place because other items may still need it.
        """
        now = utc_now()
        with self.writer() as conn:
            row = conn.execute("SELECT item_id FROM items WHERE item_id = ?", (item_id,)).fetchone()
            if row is None:
                return False
            if clear_token:
                conn.execute(
                    "UPDATE items SET active = 0, disconnected_at = ?, updated_at = ?, "
                    "access_token_encrypted = '' WHERE item_id = ?",
                    (now, now, item_id),
                )
            else:
                conn.execute(
                    "UPDATE items SET active = 0, disconnected_at = ?, updated_at = ? WHERE item_id = ?",
                    (now, now, item_id),
                )
        return True

    def connected_item_count(self, plaid_env: str | None = None) -> int:
        with self.connect() as conn:
            if plaid_env is None:
                row = conn.execute("SELECT COUNT(*) AS count FROM items WHERE active = 1").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM items WHERE active = 1 AND plaid_env = ?",
                    (plaid_env,),
                ).fetchone()
        return int(row["count"] if row else 0)

    # ------------------------------------------------------------------
    # Account observations (append-only)
    # ------------------------------------------------------------------

    def record_account_observations(
        self,
        item_id: str,
        accounts: Sequence[dict[str, Any]],
        *,
        institution_id: str | None = None,
        institution_name: str | None = None,
        plaid_env: str | None = None,
    ) -> int:
        """Append one observation row per account whose metadata has changed.

        Identical repeat observations collapse onto the same identity and are
        ignored, so a quiet account does not accumulate a row per sync. A
        changed balance or renamed account appends a new row and leaves every
        earlier observation intact.
        """
        with self.writer() as conn:
            return self._record_account_observations(
                conn,
                item_id,
                accounts,
                institution_id=institution_id,
                institution_name=institution_name,
                plaid_env=plaid_env,
            )

    def _record_account_observations(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        accounts: Sequence[dict[str, Any]],
        *,
        institution_id: str | None,
        institution_name: str | None,
        plaid_env: str | None,
    ) -> int:
        now = utc_now()
        inserted = 0
        for account in accounts:
            account_id = account.get("account_id")
            if not account_id:
                continue
            safe = strip_secrets(account)
            balances = safe.get("balances") or {}
            record = dict(safe)
            record["item_id"] = item_id
            if institution_id:
                record["institution_id"] = institution_id
            if institution_name:
                record["institution_name"] = institution_name

            digest = payload_hash(record)
            identity = sha256_text(item_id, account_id, digest, "observation")
            cursor = conn.execute(
                """
                INSERT INTO account_observations (
                    observation_identity, account_id, item_id, institution_id,
                    institution_name, name, official_name, type, subtype, mask,
                    iso_currency_code, unofficial_currency_code, current_balance,
                    available_balance, limit_balance, source, raw_json,
                    payload_hash, observed_at, plaid_env, app_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_identity) DO NOTHING
                """,
                (
                    identity,
                    str(account_id),
                    item_id,
                    institution_id,
                    institution_name,
                    safe.get("name"),
                    safe.get("official_name"),
                    safe.get("type"),
                    safe.get("subtype"),
                    safe.get("mask"),
                    balances.get("iso_currency_code"),
                    balances.get("unofficial_currency_code"),
                    _as_float(balances.get("current")),
                    _as_float(balances.get("available")),
                    _as_float(balances.get("limit")),
                    "plaid_accounts_balance_get",
                    canonical_json(record),
                    digest,
                    now,
                    plaid_env,
                    APP_VERSION,
                ),
            )
            inserted += 1 if cursor.rowcount else 0
        return inserted

    # ------------------------------------------------------------------
    # Transaction ledger (append-only)
    # ------------------------------------------------------------------

    def append_transaction_events(
        self,
        *,
        item_id: str,
        transactions: Sequence[dict[str, Any]],
        event_type: str,
        batch_id: str | None = None,
        cursor_fp: str | None = None,
        plaid_env: str | None = None,
        received_at: str | None = None,
    ) -> tuple[int, int]:
        """Append events in their own transaction. Returns (inserted, duplicates)."""
        with self.writer() as conn:
            return self._append_events(
                conn,
                item_id=item_id,
                transactions=transactions,
                event_type=event_type,
                batch_id=batch_id,
                cursor_fp=cursor_fp,
                plaid_env=plaid_env,
                received_at=received_at,
            )

    def _append_events(
        self,
        conn: sqlite3.Connection,
        *,
        item_id: str,
        transactions: Sequence[dict[str, Any]],
        event_type: str,
        batch_id: str | None,
        cursor_fp: str | None,
        plaid_env: str | None,
        received_at: str | None,
    ) -> tuple[int, int]:
        inserted = 0
        duplicates = 0
        now = utc_now()
        klass = event_class(event_type)

        for txn in transactions:
            transaction_id = txn.get("transaction_id")
            if not transaction_id:
                # A payload with no transaction id cannot be placed in the
                # ledger or reconciled later; skipping it destroys nothing.
                LOGGER.warning("Skipping a Plaid payload with no transaction_id")
                continue
            transaction_id = str(transaction_id)

            # Never persist credential-shaped fields, but keep every ordinary
            # financial field exactly as Plaid sent it.
            safe_payload = strip_secrets(txn)
            canonical_payload, digest = canonical_and_hash(safe_payload)

            # Deduplication rule: an event is a duplicate only when it repeats
            # the transaction's *current* newest event of the same class with
            # a byte-identical canonical payload. Anything else -- including a
            # revert to an older payload -- is genuinely new and is appended.
            latest = conn.execute(
                "SELECT event_id, event_class, payload_hash FROM transaction_events "
                "WHERE plaid_transaction_id = ? ORDER BY event_id DESC LIMIT 1",
                (transaction_id,),
            ).fetchone()
            supersedes = int(latest["event_id"]) if latest else None
            if latest and latest["event_class"] == klass and latest["payload_hash"] == digest:
                duplicates += 1
                continue

            identity = event_identity(
                item_id=item_id,
                transaction_id=transaction_id,
                event_type=event_type,
                payload_digest=digest,
                supersedes_event_id=supersedes,
            )
            prev_hash = self._last_ledger_hash(conn)
            ledger_hash = sha256_text(prev_hash or "", identity, digest, now)

            cursor = conn.execute(
                """
                INSERT INTO transaction_events (
                    event_identity, event_type, event_class, supersedes_event_id,
                    plaid_transaction_id, pending_transaction_id, account_id, item_id,
                    sync_batch_id, cursor_fingerprint,
                    txn_date, authorized_date, txn_datetime, authorized_datetime,
                    name, merchant_name, original_description, amount,
                    iso_currency_code, unofficial_currency_code, pending,
                    payment_channel, transaction_type, transaction_code,
                    category_json, category_id, personal_finance_category_json,
                    personal_finance_category_icon_url, counterparties_json,
                    location_json, payment_meta_json, website, logo_url,
                    merchant_entity_id, check_number, account_owner,
                    raw_json, payload_hash, received_at, inserted_at,
                    plaid_env, app_version, schema_version,
                    prev_ledger_hash, ledger_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                ON CONFLICT(event_identity) DO NOTHING
                """,
                (
                    identity,
                    event_type,
                    klass,
                    supersedes,
                    transaction_id,
                    safe_payload.get("pending_transaction_id"),
                    safe_payload.get("account_id"),
                    item_id,
                    batch_id,
                    cursor_fp,
                    _safe_date(safe_payload.get("date")),
                    _safe_date(safe_payload.get("authorized_date")),
                    _datetime_text(safe_payload.get("datetime")),
                    _datetime_text(safe_payload.get("authorized_datetime")),
                    safe_payload.get("name"),
                    safe_payload.get("merchant_name"),
                    safe_payload.get("original_description"),
                    _as_float(safe_payload.get("amount")),
                    safe_payload.get("iso_currency_code"),
                    safe_payload.get("unofficial_currency_code"),
                    None if safe_payload.get("pending") is None else (1 if safe_payload.get("pending") else 0),
                    safe_payload.get("payment_channel"),
                    safe_payload.get("transaction_type"),
                    safe_payload.get("transaction_code"),
                    _json(safe_payload.get("category")),
                    safe_payload.get("category_id"),
                    _json(safe_payload.get("personal_finance_category")),
                    safe_payload.get("personal_finance_category_icon_url"),
                    _json(safe_payload.get("counterparties")),
                    _json(safe_payload.get("location")),
                    _json(safe_payload.get("payment_meta")),
                    safe_payload.get("website"),
                    safe_payload.get("logo_url"),
                    safe_payload.get("merchant_entity_id"),
                    safe_payload.get("check_number"),
                    safe_payload.get("account_owner"),
                    canonical_payload,
                    digest,
                    received_at,
                    now,
                    plaid_env,
                    APP_VERSION,
                    SCHEMA_VERSION,
                    prev_hash,
                    ledger_hash,
                ),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                # The unique identity index caught a concurrent or replayed
                # insert. Nothing was modified; count it and move on.
                duplicates += 1

        return inserted, duplicates

    @staticmethod
    def _last_ledger_hash(conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT ledger_hash FROM transaction_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else (None if row[0] is None else str(row[0]))

    def commit_sync_page(
        self,
        *,
        item_id: str,
        added: Sequence[dict[str, Any]],
        modified: Sequence[dict[str, Any]],
        removed: Sequence[dict[str, Any]],
        next_cursor: str | None,
        advance_cursor: bool,
        batch_id: str,
        plaid_env: str | None,
        received_at: str | None = None,
    ) -> tuple[int, int]:
        """Persist one ``transactions/sync`` page and its cursor atomically.

        The cursor moves in the *same* transaction that stores the page's
        events, so the cursor can never run ahead of durably stored history. A
        crash mid-page rolls the whole page back and the next run re-requests
        it from the previous cursor; the replayed events then deduplicate.
        """
        cursor_fp = fingerprint(next_cursor)
        with self.writer() as conn:
            ins_a, dup_a = self._append_events(
                conn,
                item_id=item_id,
                transactions=added,
                event_type=EVENT_ADDED,
                batch_id=batch_id,
                cursor_fp=cursor_fp,
                plaid_env=plaid_env,
                received_at=received_at,
            )
            ins_m, dup_m = self._append_events(
                conn,
                item_id=item_id,
                transactions=modified,
                event_type=EVENT_MODIFIED,
                batch_id=batch_id,
                cursor_fp=cursor_fp,
                plaid_env=plaid_env,
                received_at=received_at,
            )
            ins_r, dup_r = self._append_events(
                conn,
                item_id=item_id,
                transactions=removed,
                event_type=EVENT_REMOVED,
                batch_id=batch_id,
                cursor_fp=cursor_fp,
                plaid_env=plaid_env,
                received_at=received_at,
            )
            if advance_cursor:
                self._update_cursor(conn, item_id, next_cursor)
        return ins_a + ins_m + ins_r, dup_a + dup_m + dup_r

    def append_historical_transactions(
        self,
        *,
        item_id: str,
        transactions: Sequence[dict[str, Any]],
        batch_id: str,
        plaid_env: str | None,
    ) -> tuple[int, int]:
        return self.append_transaction_events(
            item_id=item_id,
            transactions=transactions,
            event_type=EVENT_HISTORICAL_IMPORT,
            batch_id=batch_id,
            plaid_env=plaid_env,
        )

    # ------------------------------------------------------------------
    # Reads: current state
    # ------------------------------------------------------------------

    def list_transactions(
        self,
        *,
        months_back: int | None = None,
        limit: int | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Latest, non-removed, non-superseded state of each transaction."""
        clauses: list[str] = []
        params: list[Any] = []
        if months_back is not None and months_back > 0:
            start = date.today().replace(day=1) - relativedelta(months=months_back - 1)
            clauses.append("date >= ?")
            params.append(start.isoformat())
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT transaction_id, date, name, merchant_name, amount, category_json, "
            "personal_finance_category_json, iso_currency_code, pending, removed, "
            "superseded, account_id "
            f"FROM transaction_active_state {where} ORDER BY date DESC, state_event_id DESC"
        )
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        transactions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            amount = item.get("amount") or 0
            item["direction"] = "outflow" if amount > 0 else "inflow" if amount < 0 else "neutral"
            item["category"] = json.loads(item.pop("category_json") or "null")
            item["personal_finance_category"] = json.loads(
                item.pop("personal_finance_category_json") or "null"
            )
            transactions.append(item)
        return transactions

    def transaction_history(self, transaction_id: str) -> list[dict[str, Any]]:
        """Every stored version of one transaction, oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_id, event_type, event_class, supersedes_event_id, txn_date, "
                "amount, name, merchant_name, pending, payload_hash, inserted_at "
                "FROM transaction_events WHERE plaid_transaction_id = ? ORDER BY event_id",
                (transaction_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def transaction_count(self) -> int:
        """Count of transactions in the current dashboard-visible state."""
        return self._scalar("SELECT COUNT(*) FROM transaction_active_state")

    def event_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM transaction_events")

    def account_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM account_latest_state")

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT account_id, type, subtype FROM account_latest_state ORDER BY account_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0] if row and row[0] is not None else 0)

    # ------------------------------------------------------------------
    # Sync audit (append-only rows; only in-flight status is updated)
    # ------------------------------------------------------------------

    def new_batch_id(self) -> str:
        return uuid.uuid4().hex

    def start_sync_run(
        self,
        *,
        batch_id: str,
        item_id: str | None,
        starting_cursor: str | None,
        mode: str | None = None,
    ) -> int:
        started_at = utc_now()
        with self.writer() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_runs (batch_id, item_id, started_at, status, mode, "
                "starting_cursor_fingerprint, app_version) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (batch_id, item_id, started_at, mode, fingerprint(starting_cursor), APP_VERSION),
            )
            return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        sync_id: int,
        *,
        status: str,
        ending_cursor: str | None = None,
        added_count: int = 0,
        modified_count: int = 0,
        removed_count: int = 0,
        inserted_event_count: int = 0,
        duplicate_event_count: int = 0,
        page_count: int = 0,
        earliest_transaction_date: str | None = None,
        latest_transaction_date: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        mode: str | None = None,
    ) -> str:
        """Close out an in-flight sync run.

        This UPDATE only ever touches the operational columns of the run that
        is still executing. A failed run never rolls back events that an
        earlier run already committed -- every batch commits independently.
        """
        finished_at = utc_now()
        with self.writer() as conn:
            conn.execute(
                """
                UPDATE sync_runs SET
                    finished_at = ?, status = ?, mode = COALESCE(?, mode),
                    ending_cursor_fingerprint = ?,
                    added_count = ?, modified_count = ?, removed_count = ?,
                    inserted_event_count = ?, duplicate_event_count = ?, page_count = ?,
                    earliest_transaction_date = ?, latest_transaction_date = ?,
                    error_class = ?, error_message = ?
                WHERE sync_id = ?
                """,
                (
                    finished_at,
                    status,
                    mode,
                    fingerprint(ending_cursor),
                    added_count,
                    modified_count,
                    removed_count,
                    inserted_event_count,
                    duplicate_event_count,
                    page_count,
                    earliest_transaction_date,
                    latest_transaction_date,
                    error_class,
                    error_message,
                    sync_id,
                ),
            )
        return finished_at

    def last_sync_at(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(finished_at) AS finished_at FROM sync_runs WHERE status = 'ok'"
            ).fetchone()
            if row is None or row["finished_at"] is None:
                # Fall back to the legacy table so history from before the
                # ledger upgrade still shows a last-sync time.
                row = conn.execute(
                    "SELECT MAX(finished_at) AS finished_at FROM sync_log WHERE status = 'ok'"
                ).fetchone()
        return None if row is None or row["finished_at"] is None else str(row["finished_at"])

    def last_sync_summary(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sync_id, item_id, started_at, finished_at, status, mode, added_count, "
                "modified_count, removed_count, inserted_event_count, duplicate_event_count, "
                "page_count, earliest_transaction_date, latest_transaction_date, error_class, "
                "error_message FROM sync_runs ORDER BY sync_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        summary = dict(row)
        # item_id identifies a Plaid Item; it never leaves the add-on.
        summary.pop("item_id", None)
        return summary

    # ------------------------------------------------------------------
    # Backfill bookkeeping (operational metadata)
    # ------------------------------------------------------------------

    def get_backfill_state(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM backfill_state WHERE item_id = ?", (item_id,)).fetchone()
        return None if row is None else dict(row)

    def start_backfill(self, item_id: str, *, start_date: str, end_date: str) -> None:
        now = utc_now()
        with self.writer() as conn:
            conn.execute(
                """
                INSERT INTO backfill_state (
                    item_id, status, requested_start_date, requested_end_date,
                    attempts, started_at
                ) VALUES (?, 'running', ?, ?, 1, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    status = 'running',
                    requested_start_date = excluded.requested_start_date,
                    requested_end_date = excluded.requested_end_date,
                    attempts = backfill_state.attempts + 1,
                    started_at = excluded.started_at,
                    last_error = NULL
                """,
                (item_id, start_date, end_date, now),
            )

    def finish_backfill(
        self,
        item_id: str,
        *,
        status: str,
        transaction_count: int = 0,
        earliest: str | None = None,
        latest: str | None = None,
        last_error: str | None = None,
    ) -> None:
        with self.writer() as conn:
            conn.execute(
                """
                UPDATE backfill_state SET
                    status = ?, finished_at = ?, transaction_count = ?,
                    earliest_transaction_date = ?, latest_transaction_date = ?, last_error = ?
                WHERE item_id = ?
                """,
                (status, utc_now(), transaction_count, earliest, latest, last_error, item_id),
            )

    def backfill_complete(self) -> bool:
        """True when every active item has a completed backfill record."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM items WHERE active = 1) AS items,
                    (SELECT COUNT(*) FROM backfill_state b
                     JOIN items i ON i.item_id = b.item_id
                     WHERE i.active = 1 AND b.status = 'complete') AS done
                """
            ).fetchone()
        if row is None or int(row["items"]) == 0:
            return False
        return int(row["done"]) >= int(row["items"])

    # ------------------------------------------------------------------
    # Diagnostics and integrity
    # ------------------------------------------------------------------

    def aggregate_diagnostics(self) -> dict[str, Any]:
        """Aggregate-only counters. Returns no names, amounts, ids, or raw JSON."""
        with self.connect() as conn:
            def scalar(sql: str) -> Any:
                row = conn.execute(sql).fetchone()
                return None if row is None else row[0]

            earliest = scalar("SELECT MIN(date) FROM transaction_active_state")
            latest = scalar("SELECT MAX(date) FROM transaction_active_state")
            return {
                "total_transaction_events": int(scalar("SELECT COUNT(*) FROM transaction_events") or 0),
                "distinct_transaction_ids": int(
                    scalar("SELECT COUNT(DISTINCT plaid_transaction_id) FROM transaction_events") or 0
                ),
                "active_transactions": int(scalar("SELECT COUNT(*) FROM transaction_active_state") or 0),
                "pending_transactions": int(
                    scalar("SELECT COUNT(*) FROM transaction_active_state WHERE pending = 1") or 0
                ),
                "removed_transactions": int(
                    scalar("SELECT COUNT(*) FROM transaction_current_state WHERE removed = 1") or 0
                ),
                "superseded_pending_transactions": int(
                    scalar("SELECT COUNT(*) FROM transaction_current_state WHERE superseded = 1") or 0
                ),
                "modified_event_count": int(
                    scalar("SELECT COUNT(*) FROM transaction_events WHERE event_type = 'modified'") or 0
                ),
                "removed_event_count": int(
                    scalar("SELECT COUNT(*) FROM transaction_events WHERE event_type = 'removed'") or 0
                ),
                "legacy_import_event_count": int(
                    scalar("SELECT COUNT(*) FROM transaction_events WHERE event_type = 'legacy_import'") or 0
                ),
                "historical_import_event_count": int(
                    scalar(
                        "SELECT COUNT(*) FROM transaction_events WHERE event_type = 'historical_import'"
                    )
                    or 0
                ),
                "account_observation_count": int(
                    scalar("SELECT COUNT(*) FROM account_observations") or 0
                ),
                "linked_accounts": int(scalar("SELECT COUNT(*) FROM account_latest_state") or 0),
                "accounts_with_metadata": int(
                    scalar("SELECT COUNT(*) FROM account_latest_state WHERE name IS NOT NULL") or 0
                ),
                "events_with_raw_json": int(
                    scalar("SELECT COUNT(*) FROM transaction_events WHERE raw_json IS NOT NULL AND raw_json != ''")
                    or 0
                ),
                "legacy_transaction_rows": int(scalar("SELECT COUNT(*) FROM transactions") or 0),
                "legacy_account_rows": int(scalar("SELECT COUNT(*) FROM accounts") or 0),
                "earliest_transaction_date": earliest,
                "latest_transaction_date": latest,
                "schema_version": schema.current_version(conn),
                "expected_schema_version": SCHEMA_VERSION,
                "app_version": APP_VERSION,
            }

    def integrity_report(self) -> dict[str, Any]:
        """Read-only integrity checks. Runs no statement that modifies data."""
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail})

        try:
            with self.connect() as conn:
                add("database_opens", True)

                objects = {
                    str(row["name"]): str(row["type"])
                    for row in conn.execute(
                        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view','index')"
                    ).fetchall()
                }
                missing_tables = [name for name in schema.REQUIRED_TABLES if objects.get(name) != "table"]
                add("required_tables_exist", not missing_tables, ", ".join(missing_tables))

                missing_views = [name for name in schema.REQUIRED_VIEWS if objects.get(name) != "view"]
                add("required_views_exist", not missing_views, ", ".join(missing_views))

                missing_indexes = [name for name in schema.REQUIRED_INDEXES if objects.get(name) != "index"]
                add("required_indexes_exist", not missing_indexes, ", ".join(missing_indexes))

                total, distinct_identity = conn.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT event_identity) FROM transaction_events"
                ).fetchone()
                add(
                    "no_duplicate_event_identities",
                    int(total or 0) == int(distinct_identity or 0),
                    f"{total} events, {distinct_identity} identities",
                )

                events = int(conn.execute("SELECT COUNT(*) FROM transaction_events").fetchone()[0] or 0)
                states = int(
                    conn.execute("SELECT COUNT(*) FROM transaction_latest_state").fetchone()[0] or 0
                )
                add(
                    "event_count_covers_state",
                    events >= states,
                    f"{events} events, {states} transactions",
                )

                orphan_accounts = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM transaction_events "
                        "WHERE account_id IS NULL AND json_extract(raw_json, '$.account_id') IS NOT NULL"
                    ).fetchone()[0]
                    or 0
                )
                add("events_keep_account_id", orphan_accounts == 0, f"{orphan_accounts} missing")

                untraceable = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM account_latest_state a "
                        "WHERE a.item_id IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.item_id = a.item_id)"
                    ).fetchone()[0]
                    or 0
                )
                add("accounts_trace_to_item", untraceable == 0, f"{untraceable} untraceable")

                cursorless = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM items i WHERE i.active = 1 "
                        "AND (i.cursor IS NULL OR i.cursor = '') "
                        "AND EXISTS (SELECT 1 FROM sync_runs s WHERE s.item_id = i.item_id AND s.status = 'ok')"
                    ).fetchone()[0]
                    or 0
                )
                add("synced_items_have_cursor", cursorless == 0, f"{cursorless} without cursor")

                last = conn.execute(
                    "SELECT status FROM sync_runs ORDER BY sync_id DESC LIMIT 1"
                ).fetchone()
                add(
                    "last_sync_succeeded",
                    last is None or str(last["status"]) == "ok",
                    "no syncs yet" if last is None else str(last["status"]),
                )

                version = schema.current_version(conn)
                add("migration_version_current", version == SCHEMA_VERSION, f"v{version}")

                conn.execute("SELECT COUNT(*) FROM transaction_current_state").fetchone()
                add("latest_state_view_queryable", True)

                quick = conn.execute("PRAGMA quick_check(1)").fetchone()
                add("sqlite_quick_check", str(quick[0]).lower() == "ok", str(quick[0])[:120])
        except Exception as exc:  # pragma: no cover - defensive
            add("database_opens", False, type(exc).__name__)

        problems = [check["check"] for check in checks if not check["ok"]]
        return {"ok": not problems, "problems": problems, "checks": checks}

    def verify_ledger_hash_chain(self, *, limit: int | None = None) -> dict[str, Any]:
        """Recompute the optional tamper-evidence hash chain.

        This detects *later* edits made directly against SQLite by someone who
        did not also recompute the chain. It is tamper *evidence*, not tamper
        proofing: an administrator with write access to the file can rewrite
        both the rows and the chain. See DOCS.md.
        """
        sql = (
            "SELECT event_id, event_identity, payload_hash, inserted_at, prev_ledger_hash, ledger_hash "
            "FROM transaction_events ORDER BY event_id"
        )
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        checked = 0
        breaks: list[int] = []
        with self.connect() as conn:
            previous: str | None = None
            for row in conn.execute(sql, params):
                if row["ledger_hash"] is None:
                    # Rows imported before the chain existed are not linked.
                    previous = row["ledger_hash"]
                    continue
                expected = sha256_text(
                    row["prev_ledger_hash"] or "",
                    row["event_identity"],
                    row["payload_hash"],
                    row["inserted_at"],
                )
                if expected != row["ledger_hash"] or (
                    checked and row["prev_ledger_hash"] != previous
                ):
                    breaks.append(int(row["event_id"]))
                previous = str(row["ledger_hash"])
                checked += 1
        return {"ok": not breaks, "events_checked": checked, "break_count": len(breaks)}
