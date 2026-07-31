"""Sync orchestration: pagination, cursor safety, concurrency, and disconnect."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main
from app.schema import EVENT_ADDED
from app.storage import Storage
from conftest import synthetic_account, synthetic_transaction

ACTION_HEADERS = {main.MUTATION_HEADER: "1"}


class FakePlaid:
    """Synthetic stand-in for PlaidService. Returns invented data only."""

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        *,
        accounts: list[dict[str, Any]] | None = None,
        fail_on_page: int | None = None,
        historical: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.pages = pages if pages is not None else []
        self.accounts = accounts if accounts is not None else [synthetic_account()]
        self.fail_on_page = fail_on_page
        self.historical = historical or []
        self.cursors_seen: list[str | None] = []
        self.sync_calls = 0
        self.removed_items: list[str] = []

    # -- metadata -------------------------------------------------------
    def get_accounts(self, access_token: str) -> list[dict[str, Any]]:
        return self.accounts

    def get_item_metadata(self, access_token: str) -> dict[str, Any]:
        return {"item": {"institution_id": "ins_synthetic"}}

    def get_institution_name(self, institution_id: str | None) -> str | None:
        return "Synthetic Bank" if institution_id else None

    def create_link_token(self) -> str:
        return "link-synthetic-token"

    def exchange_public_token(self, public_token: str) -> dict[str, str]:
        return {"access_token": "synthetic-access-token", "item_id": "item_synthetic"}

    def remove_item(self, access_token: str) -> bool:
        self.removed_items.append(access_token)
        return True

    # -- transactions ---------------------------------------------------
    def backfill_start_date(self):
        from datetime import date, timedelta

        return date.today() - timedelta(days=730)

    def sync_transaction_pages(self, *, access_token: str, cursor: str | None, **_: Any) -> Iterator[dict]:
        self.sync_calls += 1
        self.cursors_seen.append(cursor)
        start = 0
        if cursor is not None:
            # Resume from the page that follows the supplied cursor.
            for index, page in enumerate(self.pages):
                if page.get("next_cursor") == cursor:
                    start = index + 1
                    break
        for index in range(start, len(self.pages)):
            if self.fail_on_page is not None and index == self.fail_on_page:
                raise RuntimeError("synthetic Plaid failure on page %d" % index)
            yield self.pages[index]

    def historical_transaction_pages(self, *, access_token: str, **_: Any) -> Iterator[dict]:
        for batch in self.historical:
            yield {"mode": "backfill", "added": batch, "modified": [], "removed": [], "next_cursor": None}


def page(added=None, modified=None, removed=None, cursor="cursor-1", has_more=False) -> dict[str, Any]:
    return {
        "mode": "sync",
        "added": added or [],
        "modified": modified or [],
        "removed": removed or [],
        "next_cursor": cursor,
        "has_more": has_more,
    }


def configure(tmp_path: Path, plaid: FakePlaid, **config_overrides: Any) -> Storage:
    storage = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    storage.init_db()
    main.STORAGE = storage
    main.PLAID = plaid
    main.SYNC_LOCK = asyncio.Lock()
    defaults: dict[str, Any] = dict(
        plaid_client_id="synthetic-client-id",
        plaid_secret="synthetic-secret",
        plaid_env="sandbox",
        plaid_redirect_uri="",
        plaid_products=["transactions"],
        plaid_country_codes=["US"],
        sync_months_back=12,
        backfill_days=730,
        enable_historical_backfill=False,
        sync_interval_minutes=360,
        local_db_path=str(tmp_path / "plaid_cashflow.sqlite"),
        currency="USD",
        show_transaction_details=False,
        debug_logging=False,
    )
    defaults.update(config_overrides)
    main.CONFIG = main.AddonConfig(**defaults)
    storage.save_item(item_id="item_synthetic", access_token="synthetic-access-token", plaid_env="sandbox")
    return storage


def ingress_client() -> TestClient:
    return TestClient(main.app, client=("172.30.32.2", 50000))


def test_pagination_walks_every_page_and_stores_all_events(tmp_path: Path) -> None:
    plaid = FakePlaid(
        [
            page([synthetic_transaction("txn_p1_a"), synthetic_transaction("txn_p1_b")], cursor="c1", has_more=True),
            page([synthetic_transaction("txn_p2_a")], cursor="c2", has_more=True),
            page([synthetic_transaction("txn_p3_a")], cursor="c3", has_more=False),
        ]
    )
    storage = configure(tmp_path, plaid)

    result = asyncio.run(main.perform_sync())

    assert result["new_transactions"] == 4
    assert result["inserted_events"] == 4
    assert storage.event_count() == 4
    assert storage.get_items()[0]["cursor"] == "c3"
    summary = storage.last_sync_summary()
    assert summary["page_count"] == 3
    assert summary["status"] == "ok"


def test_cursor_advances_only_with_the_committed_batch(tmp_path: Path) -> None:
    plaid = FakePlaid(
        [
            page([synthetic_transaction("txn_ok_1")], cursor="c1", has_more=True),
            page([synthetic_transaction("txn_ok_2")], cursor="c2", has_more=True),
            page([synthetic_transaction("txn_never")], cursor="c3", has_more=False),
        ],
        fail_on_page=2,
    )
    storage = configure(tmp_path, plaid)

    with pytest.raises(RuntimeError, match="synthetic Plaid failure"):
        asyncio.run(main.perform_sync())

    # Pages 0 and 1 committed; the cursor points at the last durable page.
    assert storage.event_count() == 2
    assert storage.get_items()[0]["cursor"] == "c2"
    # The failed page contributed nothing.
    assert storage.transaction_history("txn_never") == []

    summary = storage.last_sync_summary()
    assert summary["status"] == "error"
    assert summary["error_class"] == "RuntimeError"
    assert "synthetic-access-token" not in str(summary)


def test_failed_first_page_leaves_cursor_untouched(tmp_path: Path) -> None:
    plaid = FakePlaid([page([synthetic_transaction("txn_x")], cursor="c1")], fail_on_page=0)
    storage = configure(tmp_path, plaid)
    storage.update_item_cursor("item_synthetic", "cursor-before-failure")

    with pytest.raises(RuntimeError):
        asyncio.run(main.perform_sync())

    assert storage.get_items()[0]["cursor"] == "cursor-before-failure"
    assert storage.event_count() == 0


def test_retry_after_crash_replays_without_duplicating(tmp_path: Path) -> None:
    """A crash after insert but before the run finished must be safe to retry."""
    pages = [
        page([synthetic_transaction("txn_r1")], cursor="c1", has_more=True),
        page([synthetic_transaction("txn_r2")], cursor="c2", has_more=False),
    ]
    plaid = FakePlaid(list(pages), fail_on_page=1)
    storage = configure(tmp_path, plaid)

    with pytest.raises(RuntimeError):
        asyncio.run(main.perform_sync())
    after_crash = storage.event_count()

    # Simulate the process restarting and Plaid re-delivering from the last
    # durable cursor, including the page that was already stored.
    main.PLAID = FakePlaid(list(pages))
    main.PLAID.pages = pages
    storage.update_item_cursor("item_synthetic", None)  # worst case: cursor lost
    main.SYNC_LOCK = asyncio.Lock()
    result = asyncio.run(main.perform_sync())

    assert after_crash == 1
    assert storage.event_count() == 2
    assert result["duplicate_events"] >= 1
    assert storage.integrity_report()["ok"] is True


def test_exact_resync_is_a_no_op(tmp_path: Path) -> None:
    pages = [page([synthetic_transaction("txn_stable")], cursor="c1")]
    plaid = FakePlaid(pages)
    storage = configure(tmp_path, plaid)

    asyncio.run(main.perform_sync())
    baseline = storage.event_count()

    # Plaid replays the same page (cursor deliberately rewound).
    storage.update_item_cursor("item_synthetic", None)
    main.SYNC_LOCK = asyncio.Lock()
    result = asyncio.run(main.perform_sync())

    assert storage.event_count() == baseline
    assert result["inserted_events"] == 0
    assert result["duplicate_events"] == 1


def test_concurrent_sync_attempts_are_rejected(tmp_path: Path) -> None:
    plaid = FakePlaid([page([synthetic_transaction("txn_c1")], cursor="c1")])
    configure(tmp_path, plaid)

    async def scenario() -> tuple[Any, Any]:
        await main.SYNC_LOCK.acquire()
        try:
            with pytest.raises(Exception) as excinfo:
                await main.perform_sync()
            return excinfo.value, None
        finally:
            main.SYNC_LOCK.release()

    error, _ = asyncio.run(scenario())
    assert getattr(error, "status_code", None) == 409
    assert "already running" in str(getattr(error, "detail", ""))


def test_database_lock_is_waited_out_rather_than_failing(tmp_path: Path) -> None:
    """A competing writer must be tolerated via busy_timeout, not an error."""
    storage = Storage(str(tmp_path / "lock.sqlite"))
    storage.init_db()

    holding = threading.Event()

    def hold_write_lock() -> None:
        blocker = sqlite3.connect(storage.db_path, timeout=5)
        blocker.isolation_level = None
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO settings (key, value) VALUES ('lock_probe', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        holding.set()
        time.sleep(0.75)
        blocker.execute("COMMIT")
        blocker.close()

    thread = threading.Thread(target=hold_write_lock, daemon=True)
    thread.start()
    assert holding.wait(timeout=5)
    started = time.time()
    inserted, _ = storage.append_transaction_events(
        item_id="i", transactions=[synthetic_transaction("txn_after_lock")], event_type=EVENT_ADDED
    )
    elapsed = time.time() - started

    thread.join(timeout=5)
    assert inserted == 1
    assert elapsed >= 0.5  # it waited instead of raising
    assert storage.event_count() == 1


def test_disconnect_stops_syncing_without_deleting_history(tmp_path: Path) -> None:
    plaid = FakePlaid([page([synthetic_transaction("txn_keep_1"), synthetic_transaction("txn_keep_2")], cursor="c1")])
    storage = configure(tmp_path, plaid)
    asyncio.run(main.perform_sync())

    before_events = storage.event_count()
    before_transactions = storage.transaction_count()
    before_accounts = storage.account_count()
    key_before = storage.key_path.read_bytes()

    with ingress_client() as client:
        response = client.delete("/api/disconnect", headers=ACTION_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["financial_history_preserved"] is True
    assert body["disconnected_items"] == 1

    # Nothing financial was removed.
    assert storage.event_count() == before_events
    assert storage.transaction_count() == before_transactions
    assert storage.account_count() == before_accounts
    assert storage.db_path.exists()
    assert storage.key_path.exists()
    assert storage.key_path.read_bytes() == key_before

    # Syncing stopped and the token is gone.
    assert storage.connected_item_count() == 0
    with storage.connect() as conn:
        row = conn.execute("SELECT active, access_token_encrypted FROM items").fetchone()
    assert row["active"] == 0
    assert row["access_token_encrypted"] == ""
    assert plaid.removed_items == ["synthetic-access-token"]

    # And the history is still queryable after a restart.
    restarted = Storage(str(storage.db_path))
    restarted.init_db()
    assert restarted.event_count() == before_events
    assert restarted.transaction_count() == before_transactions


def test_sync_runs_off_the_event_loop(tmp_path: Path) -> None:
    """A long sync must not stall the dashboard.

    The Plaid SDK and sqlite3 both block. Running a first-run backfill inline
    pinned the event loop for minutes, during which /api/health and the whole
    dashboard stopped responding and the add-on looked dead.
    """
    plaid = FakePlaid([page([synthetic_transaction("txn_thread")], cursor="c1")])
    configure(tmp_path, plaid)

    loop_thread = threading.current_thread().name
    observed: list[str] = []
    original = main._sync_one_item

    def record(item, batch_id):
        observed.append(threading.current_thread().name)
        return original(item, batch_id)

    main._sync_one_item = record
    try:
        asyncio.run(main.perform_sync())
    finally:
        main._sync_one_item = original

    assert observed and all(name != loop_thread for name in observed)


def test_hash_chain_check_is_bounded_by_default(storage: Storage) -> None:
    """Chain verification must not get slower forever as the ledger grows."""
    storage.append_transaction_events(
        item_id="i",
        transactions=[synthetic_transaction(f"txn_chain_{index}") for index in range(50)],
        event_type=EVENT_ADDED,
    )

    bounded = storage.verify_ledger_hash_chain(limit=10)
    assert bounded["ok"] is True
    assert bounded["events_checked"] == 10
    assert bounded["total_events"] == 50
    assert bounded["partial"] is True

    full = storage.verify_ledger_hash_chain(limit=None)
    assert full["events_checked"] == 50
    assert full["partial"] is False


def test_no_storage_method_deletes_financial_history() -> None:
    """Guard against a destructive helper being reintroduced."""
    assert not hasattr(Storage, "delete_all_plaid_data")
    assert not hasattr(Storage, "mark_transactions_removed")
    assert not hasattr(Storage, "upsert_transactions")


def test_historical_backfill_imports_and_records_range(tmp_path: Path) -> None:
    plaid = FakePlaid(
        [page([synthetic_transaction("txn_recent", date="2026-07-20")], cursor="c1")],
        historical=[
            [
                synthetic_transaction("txn_hist_1", date="2024-09-01"),
                synthetic_transaction("txn_hist_2", date="2025-03-15"),
            ],
            [synthetic_transaction("txn_hist_3", date="2026-01-05")],
        ],
    )
    storage = configure(tmp_path, plaid, enable_historical_backfill=True)

    result = asyncio.run(main.perform_sync())

    assert result["backfilled_events"] == 3
    assert storage.event_count() == 4
    state = storage.get_backfill_state("item_synthetic")
    assert state["status"] == "complete"
    assert state["earliest_transaction_date"] == "2024-09-01"
    assert state["latest_transaction_date"] == "2026-01-05"
    assert storage.backfill_complete() is True

    # The backfill never touches the sync cursor.
    assert storage.get_items()[0]["cursor"] == "c1"

    # Running again does not re-import.
    main.SYNC_LOCK = asyncio.Lock()
    second = asyncio.run(main.perform_sync())
    assert second["backfilled_events"] == 0
    assert storage.event_count() == 4


def test_backfill_content_matching_a_sync_event_is_not_duplicated(tmp_path: Path) -> None:
    same = synthetic_transaction("txn_overlap", date="2026-05-05")
    plaid = FakePlaid([page([same], cursor="c1")], historical=[[same]])
    storage = configure(tmp_path, plaid, enable_historical_backfill=True)

    asyncio.run(main.perform_sync())

    # One transaction, one ledger row -- the backfill recognised identical state.
    assert storage.event_count() == 1
    assert storage.transaction_count() == 1
