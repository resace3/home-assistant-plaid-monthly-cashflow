"""Append-only ledger behaviour.

Covers: empty init, added/modified/removed ingestion, exact-retry dedup,
pending-to-posted linkage, multiple accounts and Items, Unicode, null optional
fields, currency handling, and large histories.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.schema import (
    EVENT_ADDED,
    EVENT_MODIFIED,
    EVENT_REMOVED,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    REQUIRED_VIEWS,
)
from app.storage import Storage
from conftest import synthetic_account, synthetic_transaction


def _objects(store: Storage, kind: str) -> set[str]:
    with store.connect() as conn:
        return {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
        }


def test_empty_database_initialises_full_schema(tmp_path: Path) -> None:
    store = Storage(str(tmp_path / "empty.sqlite"))
    store.init_db()

    assert set(REQUIRED_TABLES) <= _objects(store, "table")
    assert set(REQUIRED_VIEWS) <= _objects(store, "view")
    assert set(REQUIRED_INDEXES) <= _objects(store, "index")
    assert store.event_count() == 0
    assert store.transaction_count() == 0
    assert store.integrity_report()["ok"] is True


def test_added_transaction_is_stored_with_full_payload(storage: Storage) -> None:
    txn = synthetic_transaction()
    inserted, duplicates = storage.append_transaction_events(
        item_id="item_synth_1", transactions=[txn], event_type=EVENT_ADDED
    )

    assert (inserted, duplicates) == (1, 0)
    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM transaction_events").fetchone()

    stored = json.loads(row["raw_json"])
    # Every ordinary Plaid field survives, including nested structures.
    assert stored["merchant_name"] == "Synthetic Market"
    assert stored["location"]["city"] == "Testville"
    assert stored["counterparties"][0]["entity_id"] == "syn_merchant_entity_1"
    assert stored["personal_finance_category"]["detailed"] == "FOOD_AND_DRINK_GROCERIES"
    assert row["payment_channel"] == "in store"
    assert row["category_id"] == "19047000"
    assert row["personal_finance_category_icon_url"].startswith("https://")
    assert row["original_description"] == "SYNTHETIC MARKET #0001"
    assert row["txn_datetime"] == "2026-07-02T14:05:00Z"
    assert row["event_type"] == EVENT_ADDED
    assert row["payload_hash"] and row["event_identity"] and row["ledger_hash"]


def test_exact_retry_creates_no_duplicate_event(storage: Storage) -> None:
    txn = synthetic_transaction()
    first = storage.append_transaction_events(
        item_id="item_synth_1", transactions=[txn], event_type=EVENT_ADDED
    )
    # Same payload delivered again, and again with the key order shuffled --
    # canonicalisation must make both look identical.
    reordered = dict(reversed(list(txn.items())))
    second = storage.append_transaction_events(
        item_id="item_synth_1", transactions=[txn], event_type=EVENT_ADDED
    )
    third = storage.append_transaction_events(
        item_id="item_synth_1", transactions=[reordered], event_type=EVENT_ADDED
    )

    assert first == (1, 0)
    assert second == (0, 1)
    assert third == (0, 1)
    assert storage.event_count() == 1


def test_modified_transaction_appends_a_new_immutable_version(storage: Storage) -> None:
    original = synthetic_transaction(amount=42.50, merchant_name="Synthetic Market")
    storage.append_transaction_events(
        item_id="item_synth_1", transactions=[original], event_type=EVENT_ADDED
    )
    changed = synthetic_transaction(
        amount=47.99, merchant_name="Synthetic Market Downtown", date="2026-07-03"
    )
    inserted, _ = storage.append_transaction_events(
        item_id="item_synth_1", transactions=[changed], event_type=EVENT_MODIFIED
    )

    assert inserted == 1
    assert storage.event_count() == 2

    history = storage.transaction_history("txn_synthetic_1")
    assert [event["event_type"] for event in history] == [EVENT_ADDED, EVENT_MODIFIED]
    # The earlier version is still fully queryable -- nothing was overwritten.
    assert history[0]["amount"] == 42.50
    assert history[0]["merchant_name"] == "Synthetic Market"
    assert history[1]["amount"] == 47.99
    assert history[1]["supersedes_event_id"] == history[0]["event_id"]

    current = storage.list_transactions()
    assert len(current) == 1
    assert current[0]["amount"] == 47.99


def test_reverting_to_a_previous_payload_still_records_an_event(storage: Storage) -> None:
    """A revert must not be swallowed by content-only deduplication."""
    original = synthetic_transaction(amount=10.0)
    changed = synthetic_transaction(amount=20.0)

    storage.append_transaction_events(item_id="i", transactions=[original], event_type=EVENT_ADDED)
    storage.append_transaction_events(item_id="i", transactions=[changed], event_type=EVENT_MODIFIED)
    inserted, _ = storage.append_transaction_events(
        item_id="i", transactions=[original], event_type=EVENT_MODIFIED
    )

    assert inserted == 1
    assert storage.event_count() == 3
    assert storage.list_transactions()[0]["amount"] == 10.0


def test_removed_transaction_creates_event_without_deleting_history(storage: Storage) -> None:
    txn = synthetic_transaction()
    storage.append_transaction_events(item_id="i", transactions=[txn], event_type=EVENT_ADDED)
    storage.append_transaction_events(
        item_id="i",
        transactions=[{"transaction_id": "txn_synthetic_1"}],
        event_type=EVENT_REMOVED,
    )

    assert storage.event_count() == 2
    # The original version is retained in full.
    history = storage.transaction_history("txn_synthetic_1")
    assert history[0]["amount"] == 42.5
    assert history[1]["event_type"] == EVENT_REMOVED
    # ...but it no longer counts towards current state.
    assert storage.list_transactions() == []
    assert storage.transaction_count() == 0
    assert storage.aggregate_diagnostics()["removed_transactions"] == 1


def test_removed_then_readded_transaction_becomes_active_again(storage: Storage) -> None:
    txn = synthetic_transaction()
    storage.append_transaction_events(item_id="i", transactions=[txn], event_type=EVENT_ADDED)
    storage.append_transaction_events(
        item_id="i", transactions=[{"transaction_id": txn["transaction_id"]}], event_type=EVENT_REMOVED
    )
    storage.append_transaction_events(
        item_id="i", transactions=[synthetic_transaction(amount=55.0)], event_type=EVENT_ADDED
    )

    assert storage.transaction_count() == 1
    assert storage.event_count() == 3


def test_pending_to_posted_linkage_is_preserved_and_not_double_counted(storage: Storage) -> None:
    pending = synthetic_transaction(
        "txn_synth_pending", amount=31.00, pending=True, date="2026-07-01"
    )
    posted = synthetic_transaction(
        "txn_synth_posted",
        amount=33.75,
        pending=False,
        date="2026-07-03",
        pending_transaction_id="txn_synth_pending",
        merchant_name="Synthetic Market Downtown",
    )

    storage.append_transaction_events(item_id="i", transactions=[pending], event_type=EVENT_ADDED)
    storage.append_transaction_events(item_id="i", transactions=[posted], event_type=EVENT_ADDED)

    # 1. The pending version was observed and is still queryable.
    assert storage.transaction_history("txn_synth_pending")[0]["amount"] == 31.00

    # 2. The link from posted back to pending is recorded.
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT pending_transaction_id FROM transaction_events WHERE plaid_transaction_id = ?",
            ("txn_synth_posted",),
        ).fetchone()
    assert row["pending_transaction_id"] == "txn_synth_pending"

    # 3. The pair counts once, using the posted amount and date.
    current = storage.list_transactions()
    assert [row["transaction_id"] for row in current] == ["txn_synth_posted"]
    assert current[0]["amount"] == 33.75
    assert storage.aggregate_diagnostics()["superseded_pending_transactions"] == 1
    assert storage.event_count() == 2


def test_multiple_accounts_and_items_stay_separated(storage: Storage) -> None:
    storage.append_transaction_events(
        item_id="item_a",
        transactions=[
            synthetic_transaction("txn_a1", account_id="acc_a_checking"),
            synthetic_transaction("txn_a2", account_id="acc_a_savings", amount=-500.0),
        ],
        event_type=EVENT_ADDED,
    )
    storage.append_transaction_events(
        item_id="item_b",
        transactions=[synthetic_transaction("txn_b1", account_id="acc_b_credit", amount=88.0)],
        event_type=EVENT_ADDED,
    )
    storage.record_account_observations("item_a", [synthetic_account("acc_a_checking")])
    storage.record_account_observations("item_a", [synthetic_account("acc_a_savings", name="Synthetic Savings")])
    storage.record_account_observations("item_b", [synthetic_account("acc_b_credit", name="Synthetic Card")])

    assert storage.event_count() == 3
    assert storage.account_count() == 3
    assert len(storage.list_transactions(account_id="acc_a_checking")) == 1

    with storage.connect() as conn:
        by_item = dict(
            conn.execute(
                "SELECT item_id, COUNT(*) FROM transaction_events GROUP BY item_id"
            ).fetchall()
        )
    assert by_item == {"item_a": 2, "item_b": 1}


def test_unicode_and_special_characters_round_trip(storage: Storage) -> None:
    tricky = "Café Ñoño 北京 🏦 O'Brien \"quoted\" <tag> ; DROP TABLE transactions;--"
    txn = synthetic_transaction("txn_unicode", name=tricky, merchant_name=tricky)
    storage.append_transaction_events(item_id="i", transactions=[txn], event_type=EVENT_ADDED)

    stored = storage.list_transactions()[0]
    assert stored["name"] == tricky
    assert stored["merchant_name"] == tricky
    # The parameterised insert treated the SQL-looking text as data.
    assert storage.event_count() == 1
    with storage.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        raw = conn.execute("SELECT raw_json FROM transaction_events").fetchone()[0]
    assert json.loads(raw)["name"] == tricky


def test_null_optional_fields_are_accepted(storage: Storage) -> None:
    sparse = {
        "transaction_id": "txn_sparse",
        "account_id": "acc_synthetic_checking",
        "amount": 5.0,
        "date": "2026-07-05",
        "name": None,
        "merchant_name": None,
        "iso_currency_code": None,
        "pending": None,
        "category": None,
        "personal_finance_category": None,
        "location": None,
        "counterparties": None,
    }
    inserted, _ = storage.append_transaction_events(
        item_id="i", transactions=[sparse], event_type=EVENT_ADDED
    )

    assert inserted == 1
    row = storage.list_transactions()[0]
    assert row["name"] is None
    assert row["category"] is None
    assert row["pending"] == 0


def test_currency_fields_are_retained(storage: Storage) -> None:
    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction("txn_usd", iso_currency_code="USD"),
            synthetic_transaction(
                "txn_crypto", iso_currency_code=None, unofficial_currency_code="MATIC"
            ),
        ],
        event_type=EVENT_ADDED,
    )

    with storage.connect() as conn:
        rows = {
            str(row["plaid_transaction_id"]): (row["iso_currency_code"], row["unofficial_currency_code"])
            for row in conn.execute(
                "SELECT plaid_transaction_id, iso_currency_code, unofficial_currency_code "
                "FROM transaction_events"
            )
        }
    assert rows["txn_usd"] == ("USD", None)
    assert rows["txn_crypto"] == (None, "MATIC")


def test_transaction_without_id_is_skipped_without_failing_the_batch(storage: Storage) -> None:
    inserted, _ = storage.append_transaction_events(
        item_id="i",
        transactions=[{"amount": 1.0}, synthetic_transaction("txn_ok")],
        event_type=EVENT_ADDED,
    )
    assert inserted == 1
    assert storage.event_count() == 1


@pytest.mark.parametrize("count", [2000])
def test_large_transaction_history(storage: Storage, count: int) -> None:
    batch = [
        synthetic_transaction(
            f"txn_bulk_{index:05d}",
            amount=float(index % 500) + 0.99,
            date=f"2026-{(index % 12) + 1:02d}-{(index % 28) + 1:02d}",
        )
        for index in range(count)
    ]
    inserted, duplicates = storage.append_transaction_events(
        item_id="i", transactions=batch, event_type=EVENT_ADDED
    )

    assert (inserted, duplicates) == (count, 0)
    assert storage.event_count() == count
    assert storage.transaction_count() == count
    # A full replay of the same history adds nothing.
    assert storage.append_transaction_events(
        item_id="i", transactions=batch, event_type=EVENT_ADDED
    ) == (0, count)
    assert storage.integrity_report()["ok"] is True


def test_account_observations_are_append_only(storage: Storage) -> None:
    storage.record_account_observations(
        "item_a", [synthetic_account(current=100.0)], institution_id="ins_synth", institution_name="Synthetic Bank"
    )
    # An identical observation collapses.
    storage.record_account_observations(
        "item_a", [synthetic_account(current=100.0)], institution_id="ins_synth", institution_name="Synthetic Bank"
    )
    # A changed balance appends without destroying the earlier reading.
    storage.record_account_observations(
        "item_a", [synthetic_account(current=250.0)], institution_id="ins_synth", institution_name="Synthetic Bank"
    )

    with storage.connect() as conn:
        balances = [
            row[0]
            for row in conn.execute(
                "SELECT current_balance FROM account_observations ORDER BY observation_id"
            )
        ]
        latest = conn.execute("SELECT * FROM account_latest_state").fetchone()

    assert balances == [100.0, 250.0]
    assert latest["current_balance"] == 250.0
    assert latest["institution_name"] == "Synthetic Bank"
    assert latest["mask"] == "0000"
    assert storage.account_count() == 1


def test_hash_chain_detects_out_of_band_modification(storage: Storage, tmp_path: Path) -> None:
    storage.append_transaction_events(
        item_id="i",
        transactions=[synthetic_transaction(f"txn_{index}") for index in range(5)],
        event_type=EVENT_ADDED,
    )
    assert storage.verify_ledger_hash_chain()["ok"] is True

    # Simulate an administrator editing SQLite directly, which the application
    # itself never does. This is tamper *evidence*, not tamper proofing.
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute("UPDATE transaction_events SET payload_hash = 'tampered' WHERE event_id = 3")
        conn.commit()

    report = storage.verify_ledger_hash_chain()
    assert report["ok"] is False
    assert report["break_count"] >= 1
