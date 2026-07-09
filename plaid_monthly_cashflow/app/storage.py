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

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    access_token_encrypted TEXT NOT NULL,
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
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

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
        institution_id: str | None = None,
        institution_name: str | None = None,
    ) -> None:
        encrypted = encrypt_text(access_token, self.key_path)
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO items (
                    item_id, access_token_encrypted, institution_id,
                    institution_name, cursor, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    access_token_encrypted = excluded.access_token_encrypted,
                    institution_id = excluded.institution_id,
                    institution_name = excluded.institution_name,
                    updated_at = excluded.updated_at
                """,
                (item_id, encrypted, institution_id, institution_name, now, now),
            )

    def get_items(self, *, include_tokens: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_id, access_token_encrypted, institution_id, institution_name, cursor, created_at, updated_at "
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
                balances = account.get("balances") or {}
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
                        account.get("name"),
                        account.get("official_name"),
                        str(account.get("type")) if account.get("type") is not None else None,
                        str(account.get("subtype")) if account.get("subtype") is not None else None,
                        account.get("mask"),
                        balances.get("current"),
                        balances.get("available"),
                        balances.get("iso_currency_code") or account.get("iso_currency_code"),
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
                        _json(txn),
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
                    a.name,
                    a.official_name,
                    a.type,
                    a.subtype,
                    a.mask,
                    i.institution_name,
                    a.current_balance,
                    a.iso_currency_code
                FROM accounts a
                LEFT JOIN items i ON i.item_id = a.item_id
                ORDER BY COALESCE(i.institution_name, ''), a.name
                """
            ).fetchall()
        return [dict(row) for row in rows]

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
            "SELECT transaction_id, date, name, merchant_name, amount, account_id, "
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

    def connected_item_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM items").fetchone()
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
        with self.connect() as conn:
            conn.execute("DELETE FROM transactions")
            conn.execute("DELETE FROM accounts")
            conn.execute("DELETE FROM items")
            conn.execute("DELETE FROM sync_log")
            conn.execute("DELETE FROM settings")
