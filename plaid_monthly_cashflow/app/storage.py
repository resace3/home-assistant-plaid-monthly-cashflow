from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from dateutil.relativedelta import relativedelta

from .security import decrypt_text, encrypt_text


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, sort_keys=True)


def _safe_date(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:10]


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.key_path = self.db_path.with_name("local_key.key")

    def _sidecar_paths(self) -> list[Path]:
        return [Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")]

    def _chmod_private(self, *paths: Path) -> None:
        for path in paths:
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    def _safe_unlink(self, path: Path) -> None:
        try:
            db_parent = self.db_path.parent.resolve(strict=False)
            resolved = path.resolve(strict=False)
        except OSError:
            return
        if resolved.parent != db_parent:
            return
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except OSError:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
            self._chmod_private(self.db_path, *self._sidecar_paths())

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA secure_delete=ON;
                PRAGMA journal_mode=WAL;

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
                """
            )
            item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
            if "plaid_env" not in item_columns:
                conn.execute("ALTER TABLE items ADD COLUMN plaid_env TEXT")
            conn.execute("UPDATE transactions SET raw_json = NULL WHERE raw_json IS NOT NULL")
            conn.execute(
                """
                UPDATE accounts
                SET name = NULL,
                    official_name = NULL,
                    type = NULL,
                    subtype = NULL,
                    mask = NULL,
                    current_balance = NULL,
                    available_balance = NULL,
                    iso_currency_code = NULL
                """
            )
        self._chmod_private(self.db_path, *self._sidecar_paths())

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

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
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO items (
                    item_id, access_token_encrypted, plaid_env, institution_id,
                    institution_name, cursor, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    access_token_encrypted = excluded.access_token_encrypted,
                    plaid_env = excluded.plaid_env,
                    institution_id = excluded.institution_id,
                    institution_name = excluded.institution_name,
                    updated_at = excluded.updated_at
                """,
                (item_id, encrypted, plaid_env, institution_id, institution_name, now, now),
            )

    def get_items(self, *, include_tokens: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_id, access_token_encrypted, plaid_env, institution_id, institution_name, cursor, created_at, updated_at "
                "FROM items ORDER BY created_at"
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            encrypted = item.pop("access_token_encrypted", None)
            if include_tokens and encrypted:
                item["access_token"] = decrypt_text(str(encrypted), self.key_path)
            items.append(item)
        return items

    def reconcile_item_environments(self) -> None:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_id, access_token_encrypted FROM items WHERE plaid_env IS NULL OR plaid_env = ''"
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
                "SELECT DISTINCT plaid_env FROM items WHERE plaid_env IS NOT NULL AND plaid_env != '' ORDER BY plaid_env"
            ).fetchall()
            unknown_count = conn.execute(
                "SELECT COUNT(*) AS count FROM items WHERE plaid_env IS NULL OR plaid_env = ''"
            ).fetchone()
        environments = [str(row["plaid_env"]) for row in rows]
        if unknown_count and int(unknown_count["count"]) > 0:
            environments.append("unknown")
        return environments[0] if len(environments) == 1 else "mixed" if environments else None

    def connection_requires_reset(self, configured_env: str) -> bool:
        environment = self.connection_environment()
        return environment is not None and environment != configured_env

    def update_item_cursor(self, item_id: str, cursor: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE items SET cursor = ?, updated_at = ? WHERE item_id = ?",
                (cursor, utc_now(), item_id),
            )

    def upsert_accounts(self, item_id: str, accounts: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connect() as conn:
            for account in accounts:
                conn.execute(
                    """
                    INSERT INTO accounts (
                        account_id, item_id, name, official_name, type, subtype, mask,
                        current_balance, available_balance, iso_currency_code, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        item_id = excluded.item_id,
                        name = excluded.name,
                        official_name = excluded.official_name,
                        type = excluded.type,
                        subtype = excluded.subtype,
                        mask = excluded.mask,
                        current_balance = excluded.current_balance,
                        available_balance = excluded.available_balance,
                        iso_currency_code = excluded.iso_currency_code,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account.get("account_id"),
                        item_id,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        now,
                    ),
                )

    def upsert_transactions(self, item_id: str, transactions: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connect() as conn:
            for txn in transactions:
                conn.execute(
                    """
                    INSERT INTO transactions (
                        transaction_id, account_id, item_id, date, authorized_date, name,
                        merchant_name, amount, iso_currency_code, category_json,
                        personal_finance_category_json, pending, removed, raw_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(transaction_id) DO UPDATE SET
                        account_id = excluded.account_id,
                        item_id = excluded.item_id,
                        date = excluded.date,
                        authorized_date = excluded.authorized_date,
                        name = excluded.name,
                        merchant_name = excluded.merchant_name,
                        amount = excluded.amount,
                        iso_currency_code = excluded.iso_currency_code,
                        category_json = excluded.category_json,
                        personal_finance_category_json = excluded.personal_finance_category_json,
                        pending = excluded.pending,
                        removed = 0,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        txn.get("transaction_id"),
                        txn.get("account_id"),
                        item_id,
                        _safe_date(txn.get("date")),
                        _safe_date(txn.get("authorized_date")),
                        txn.get("name"),
                        txn.get("merchant_name"),
                        txn.get("amount"),
                        txn.get("iso_currency_code"),
                        _json(txn.get("category")),
                        _json(txn.get("personal_finance_category")),
                        1 if txn.get("pending") else 0,
                        None,
                        now,
                    ),
                )

    def mark_transactions_removed(self, removed: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.connect() as conn:
            for item in removed:
                transaction_id = item.get("transaction_id")
                if transaction_id:
                    conn.execute(
                        "UPDATE transactions SET removed = 1, updated_at = ? WHERE transaction_id = ?",
                        (now, transaction_id),
                    )

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.account_id,
                    a.type,
                    a.subtype
                FROM accounts a
                ORDER BY a.account_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def account_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM accounts").fetchone()
        return int(row["count"] if row else 0)

    def list_transactions(
        self,
        *,
        months_back: int | None = None,
        limit: int | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["removed = 0"]
        params: list[Any] = []
        if months_back is not None and months_back > 0:
            start = date.today().replace(day=1) - relativedelta(months=months_back - 1)
            clauses.append("date >= ?")
            params.append(start.isoformat())
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)

        sql = (
            "SELECT date, name, merchant_name, amount, "
            "category_json, personal_finance_category_json, iso_currency_code, pending, removed "
            f"FROM transactions WHERE {' AND '.join(clauses)} ORDER BY date DESC, updated_at DESC"
        )
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        transactions = []
        for row in rows:
            item = dict(row)
            item["direction"] = "outflow" if (item.get("amount") or 0) > 0 else "inflow" if (item.get("amount") or 0) < 0 else "neutral"
            item["category"] = json.loads(item.pop("category_json") or "null")
            item["personal_finance_category"] = json.loads(item.pop("personal_finance_category_json") or "null")
            transactions.append(item)
        return transactions

    def transaction_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM transactions WHERE removed = 0").fetchone()
        return int(row["count"] if row else 0)

    def connected_item_count(self, plaid_env: str | None = None) -> int:
        with self.connect() as conn:
            if plaid_env is None:
                row = conn.execute("SELECT COUNT(*) AS count FROM items").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM items WHERE plaid_env = ?",
                    (plaid_env,),
                ).fetchone()
        return int(row["count"] if row else 0)

    def last_sync_at(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT finished_at FROM sync_log WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["finished_at"])

    def start_sync_log(self) -> tuple[int, str]:
        started_at = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_log (started_at, status, added_count, modified_count, removed_count) "
                "VALUES (?, 'running', 0, 0, 0)",
                (started_at,),
            )
            sync_id = int(cursor.lastrowid)
        return sync_id, started_at

    def finish_sync_log(
        self,
        sync_id: int,
        *,
        status: str,
        message: str,
        added_count: int,
        modified_count: int,
        removed_count: int,
    ) -> str:
        finished_at = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sync_log
                SET finished_at = ?, status = ?, message = ?,
                    added_count = ?, modified_count = ?, removed_count = ?
                WHERE id = ?
                """,
                (finished_at, status, message, added_count, modified_count, removed_count, sync_id),
            )
        return finished_at

    def delete_all_plaid_data(self) -> None:
        try:
            with self.connect() as conn:
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("DELETE FROM transactions")
                conn.execute("DELETE FROM accounts")
                conn.execute("DELETE FROM items")
                conn.execute("DELETE FROM sync_log")
                conn.execute("DELETE FROM settings")
                conn.commit()
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
                try:
                    conn.execute("VACUUM")
                except sqlite3.DatabaseError:
                    pass
        finally:
            for path in [self.db_path, *self._sidecar_paths(), self.key_path]:
                self._safe_unlink(path)
            self.init_db()
