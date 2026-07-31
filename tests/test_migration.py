"""Migration from the pre-ledger schema.

The user's production database already holds real transactions, so these tests
build a synthetic database with the *exact* legacy schema and prove that
migrating it preserves every row, runs safely more than once, and never clears
a field.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.schema import SCHEMA_VERSION
from app.storage import Storage
from conftest import synthetic_transaction

# Byte-for-byte the schema shipped by add-on 0.1.8.
LEGACY_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE items (
    item_id TEXT PRIMARY KEY,
    access_token_encrypted TEXT NOT NULL,
    plaid_env TEXT,
    institution_id TEXT,
    institution_name TEXT,
    cursor TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE accounts (
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
CREATE TABLE transactions (
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
CREATE TABLE sync_log (
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


def build_legacy_db(path: Path, *, transactions: int = 25, with_raw_json: bool = False) -> None:
    """Create a synthetic database in the legacy 0.1.8 shape."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO items (item_id, access_token_encrypted, plaid_env, cursor, created_at, updated_at) "
        "VALUES ('item_legacy', 'not-a-real-token', 'sandbox', 'legacy-cursor', '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO accounts (account_id, item_id, name, type, subtype, mask, updated_at) "
        "VALUES ('acc_legacy', 'item_legacy', 'Legacy Checking', 'depository', 'checking', '4321', "
        "'2026-01-01T00:00:00Z')"
    )
    for index in range(transactions):
        removed = 1 if index % 10 == 9 else 0
        pending = 1 if index % 7 == 6 else 0
        raw = '{"transaction_id": "txn_legacy_%d", "amount": %d.5}' % (index, index) if with_raw_json else None
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_id, item_id, date, name, merchant_name, "
            "amount, iso_currency_code, category_json, pending, removed, raw_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"txn_legacy_{index}",
                "acc_legacy",
                "item_legacy",
                f"2026-0{(index % 6) + 1}-15",
                f"Legacy Transaction {index}",
                f"Legacy Merchant {index % 4}",
                float(index) + 0.5,
                "USD",
                '["Shops"]',
                pending,
                removed,
                raw,
                "2026-06-01T00:00:00Z",
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "plaid_cashflow.sqlite"
    build_legacy_db(path)
    return path


def test_migration_preserves_every_legacy_row(legacy_db: Path) -> None:
    store = Storage(str(legacy_db))
    store.init_db()

    with store.connect() as conn:
        legacy_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        legacy_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        imported = conn.execute(
            "SELECT COUNT(DISTINCT plaid_transaction_id) FROM transaction_events"
        ).fetchone()[0]
        removed_events = conn.execute(
            "SELECT COUNT(*) FROM transaction_events WHERE event_type = 'removed'"
        ).fetchone()[0]

    # The legacy tables are untouched -- migration copies, never moves.
    assert legacy_count == 25
    assert legacy_accounts == 1
    assert imported == 25
    # Rows previously flagged removed=1 became removal *events*, and their
    # pre-removal version is still present.
    assert removed_events == 2
    assert store.aggregate_diagnostics()["legacy_import_event_count"] == 25
    assert store.schema_version() == SCHEMA_VERSION


def test_migration_is_idempotent(legacy_db: Path) -> None:
    store = Storage(str(legacy_db))
    store.init_db()
    first_events = store.event_count()
    first_observations = store.aggregate_diagnostics()["account_observation_count"]

    for _ in range(3):
        store.init_db()

    assert store.event_count() == first_events
    assert store.aggregate_diagnostics()["account_observation_count"] == first_observations
    with store.connect() as conn:
        migrations = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert migrations == SCHEMA_VERSION
    assert store.integrity_report()["ok"] is True


def test_migration_preserves_existing_raw_json(tmp_path: Path) -> None:
    path = tmp_path / "raw.sqlite"
    build_legacy_db(path, transactions=5, with_raw_json=True)
    store = Storage(str(path))
    store.init_db()

    with store.connect() as conn:
        legacy_raw = conn.execute(
            "SELECT raw_json FROM transactions WHERE transaction_id = 'txn_legacy_1'"
        ).fetchone()[0]
        ledger_raw = conn.execute(
            "SELECT raw_json FROM transaction_events WHERE plaid_transaction_id = 'txn_legacy_1'"
        ).fetchone()[0]

    # The legacy column is never cleared, and the payload carries into the ledger.
    assert legacy_raw is not None
    assert '"amount":1.5' in ledger_raw.replace(" ", "")


def test_startup_never_clears_raw_json_or_account_metadata(tmp_path: Path) -> None:
    """The regression this whole change exists to prevent."""
    path = tmp_path / "clear.sqlite"
    build_legacy_db(path, transactions=3, with_raw_json=True)
    store = Storage(str(path))

    for _ in range(5):
        store.init_db()
        store.reconcile_item_environments()

    with store.connect() as conn:
        raw_present = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE raw_json IS NOT NULL"
        ).fetchone()[0]
        account = conn.execute("SELECT * FROM accounts WHERE account_id = 'acc_legacy'").fetchone()
        ledger_raw = conn.execute(
            "SELECT COUNT(*) FROM transaction_events WHERE raw_json IS NOT NULL AND raw_json != ''"
        ).fetchone()[0]

    assert raw_present == 3
    assert account["name"] == "Legacy Checking"
    assert account["mask"] == "4321"
    assert account["type"] == "depository"
    assert ledger_raw == 3


def test_data_survives_repeated_restarts(legacy_db: Path) -> None:
    """Restart == constructing Storage again and running init_db()."""
    Storage(str(legacy_db)).init_db()
    baseline = Storage(str(legacy_db))
    events = baseline.event_count()
    accounts = baseline.account_count()
    transactions = baseline.transaction_count()

    for _ in range(4):
        restarted = Storage(str(legacy_db))
        restarted.init_db()
        restarted.reconcile_item_environments()
        assert restarted.event_count() >= events
        assert restarted.account_count() >= accounts
        assert restarted.transaction_count() == transactions
        assert restarted.aggregate_diagnostics()["events_with_raw_json"] == events


def test_new_events_coexist_with_migrated_history(legacy_db: Path) -> None:
    from app.schema import EVENT_ADDED

    store = Storage(str(legacy_db))
    store.init_db()
    before = store.event_count()

    store.append_transaction_events(
        item_id="item_legacy",
        transactions=[synthetic_transaction("txn_new_after_migration")],
        event_type=EVENT_ADDED,
    )

    assert store.event_count() == before + 1
    assert store.integrity_report()["ok"] is True


def test_migration_rolls_back_cleanly_on_failure(legacy_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import schema as schema_module

    def explode(conn, now):
        conn.execute(
            "INSERT INTO transaction_events (event_identity, event_type, event_class, "
            "plaid_transaction_id, raw_json, payload_hash, inserted_at) "
            "VALUES ('x', 'added', 'state', 'txn_partial', '{}', 'h', '2026-01-01T00:00:00Z')"
        )
        raise RuntimeError("synthetic migration failure")

    store = Storage(str(legacy_db))
    with store.connect() as conn:
        schema_module.exec_script(conn, schema_module.BASE_SCHEMA)
        schema_module.exec_script(conn, schema_module.LEDGER_SCHEMA)

    monkeypatch.setattr(
        schema_module,
        "MIGRATIONS",
        ((1, "base_schema", schema_module._migration_001_base), (2, "boom", explode)),
    )

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        store.init_db()

    with store.connect() as conn:
        partial = conn.execute(
            "SELECT COUNT(*) FROM transaction_events WHERE plaid_transaction_id = 'txn_partial'"
        ).fetchone()[0]
        recorded = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 2").fetchone()[0]
        legacy_intact = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    # Nothing partial was committed, the failed version was not recorded, and
    # the original financial rows are untouched.
    assert partial == 0
    assert recorded == 0
    assert legacy_intact == 25
