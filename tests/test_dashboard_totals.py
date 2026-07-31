"""Dashboard totals derive from latest applicable state only."""

from __future__ import annotations

from app.cashflow import monthly_cashflow, summarize_months
from app.schema import EVENT_ADDED, EVENT_MODIFIED, EVENT_REMOVED
from app.storage import Storage
from conftest import synthetic_transaction


def totals(storage: Storage) -> dict[str, float]:
    rows = storage.list_transactions(months_back=None, limit=None)
    return summarize_months(monthly_cashflow(rows, months_back=None))


def test_modified_transaction_counts_once_at_its_latest_amount(storage: Storage) -> None:
    storage.append_transaction_events(
        item_id="i",
        transactions=[synthetic_transaction("txn_m", amount=100.0, date="2026-05-10")],
        event_type=EVENT_ADDED,
    )
    storage.append_transaction_events(
        item_id="i",
        transactions=[synthetic_transaction("txn_m", amount=125.0, date="2026-05-10")],
        event_type=EVENT_MODIFIED,
    )

    summary = totals(storage)
    assert storage.event_count() == 2  # both versions retained
    assert summary["total_outflow"] == 125.0
    assert summary["total_inflow"] == 0.0
    assert summary["net"] == -125.0


def test_removed_transactions_are_retained_but_excluded_from_totals(storage: Storage) -> None:
    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction("txn_keep", amount=40.0, date="2026-05-01"),
            synthetic_transaction("txn_drop", amount=60.0, date="2026-05-02"),
        ],
        event_type=EVENT_ADDED,
    )
    storage.append_transaction_events(
        item_id="i",
        transactions=[{"transaction_id": "txn_drop"}],
        event_type=EVENT_REMOVED,
    )

    summary = totals(storage)
    assert summary["total_outflow"] == 40.0
    # The removed transaction and its pre-removal version are both still stored.
    assert storage.event_count() == 3
    assert len(storage.transaction_history("txn_drop")) == 2
    assert storage.transaction_history("txn_drop")[0]["amount"] == 60.0


def test_linked_pending_and_posted_are_not_double_counted(storage: Storage) -> None:
    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction("txn_pending", amount=20.0, date="2026-05-01", pending=True)
        ],
        event_type=EVENT_ADDED,
    )
    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction(
                "txn_posted",
                amount=22.5,
                date="2026-05-03",
                pending=False,
                pending_transaction_id="txn_pending",
            )
        ],
        event_type=EVENT_ADDED,
    )

    summary = totals(storage)
    assert summary["total_outflow"] == 22.5
    rows = storage.list_transactions()
    assert len(rows) == 1


def test_same_transaction_id_updated_in_place_counts_once(storage: Storage) -> None:
    """Plaid may keep the id and flip pending to false on the same row."""
    storage.append_transaction_events(
        item_id="i",
        transactions=[synthetic_transaction("txn_same", amount=30.0, date="2026-05-01", pending=True)],
        event_type=EVENT_ADDED,
    )
    storage.append_transaction_events(
        item_id="i",
        transactions=[synthetic_transaction("txn_same", amount=31.0, date="2026-05-01", pending=False)],
        event_type=EVENT_MODIFIED,
    )

    summary = totals(storage)
    assert summary["total_outflow"] == 31.0
    assert storage.event_count() == 2
    # The pending version is still queryable after posting.
    history = storage.transaction_history("txn_same")
    assert history[0]["pending"] == 1
    assert history[0]["amount"] == 30.0


def test_inflow_and_outflow_split_and_month_range(storage: Storage) -> None:
    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction("txn_pay", amount=-2500.0, date="2026-04-01"),
            synthetic_transaction("txn_rent", amount=1500.0, date="2026-04-02"),
            synthetic_transaction("txn_food", amount=250.0, date="2026-05-02"),
            synthetic_transaction("txn_zero", amount=0.0, date="2026-05-03"),
        ],
        event_type=EVENT_ADDED,
    )

    months = monthly_cashflow(storage.list_transactions(), months_back=None)
    by_month = {row["month"]: row for row in months}
    assert by_month["2026-04"]["inflow"] == 2500.0
    assert by_month["2026-04"]["outflow"] == 1500.0
    assert by_month["2026-04"]["net"] == 1000.0
    assert by_month["2026-05"]["outflow"] == 250.0


def test_current_state_view_scales_linearly(storage: Storage) -> None:
    """Guard against the O(n^2) correlated-subquery form returning.

    The original ``superseded`` definition re-evaluated the latest-state view
    once per row. At ~2,500 events a single COUNT over the view took seconds
    and the diagnostics endpoint timed out in production.
    """
    import time

    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction(f"txn_scale_{index}", date="2026-05-01", amount=float(index))
            for index in range(1500)
        ],
        event_type=EVENT_ADDED,
    )

    started = time.monotonic()
    with storage.connect() as conn:
        conn.execute("SELECT COUNT(*) FROM transaction_active_state").fetchone()
        conn.execute("SELECT COUNT(*) FROM transaction_current_state WHERE superseded = 1").fetchone()
    elapsed = time.monotonic() - started

    # Generous ceiling: the correlated form took several seconds at this size.
    assert elapsed < 2.0, f"current-state views degraded to {elapsed:.2f}s"


def test_dashboard_range_does_not_limit_stored_history(storage: Storage) -> None:
    """sync_months_back is a display filter, not a retention policy."""
    storage.append_transaction_events(
        item_id="i",
        transactions=[
            synthetic_transaction("txn_old", amount=10.0, date="2019-01-15"),
            synthetic_transaction("txn_new", amount=20.0, date="2026-07-15"),
        ],
        event_type=EVENT_ADDED,
    )

    assert storage.event_count() == 2
    assert len(storage.list_transactions(months_back=None)) == 2
    # A narrow display window hides the old row without removing it.
    recent = storage.list_transactions(months_back=1)
    assert all(row["date"] >= "2026-07-01" for row in recent)
    assert storage.event_count() == 2
    assert storage.aggregate_diagnostics()["earliest_transaction_date"] == "2019-01-15"
