"""The opt-in per-transaction detail screens.

The whole point of these endpoints is to show the owner their own financial
records in full, so these tests check two things at once: that the data really
is complete, and that turning it on does not widen anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_sync_engine import FakePlaid, configure, ingress_client

from app import main
from app.schema import EVENT_ADDED, EVENT_MODIFIED, EVENT_REMOVED
from app.security import CREDENTIAL_FIELD_NAMES
from conftest import synthetic_account, synthetic_transaction


def enabled(tmp_path: Path, **overrides):
    return configure(tmp_path, FakePlaid([]), show_transaction_details=True, **overrides)


def seed(storage) -> None:
    storage.record_account_observations(
        "item_synthetic",
        [synthetic_account("acc_synthetic_checking", name="Everyday Checking", mask="0000")],
        institution_id="ins_synthetic",
        institution_name="Synthetic Bank",
    )
    storage.append_transaction_events(
        item_id="item_synthetic",
        transactions=[
            synthetic_transaction("txn_one", amount=42.5, date="2026-07-02"),
            synthetic_transaction("txn_two", amount=-1200.0, date="2026-07-01", name="Payroll"),
        ],
        event_type=EVENT_ADDED,
    )


def test_endpoints_are_absent_unless_explicitly_enabled(tmp_path: Path) -> None:
    storage = configure(tmp_path, FakePlaid([]))
    seed(storage)

    with ingress_client() as client:
        assert client.get("/api/transactions").status_code == 404
        assert client.get("/api/transactions/txn_one/versions").status_code == 404
        # The dashboard is told not to offer the screen at all.
        assert client.get("/api/health").json()["transaction_details_enabled"] is False


def test_enabling_the_option_exposes_full_transaction_detail(tmp_path: Path) -> None:
    storage = enabled(tmp_path)
    seed(storage)

    with ingress_client() as client:
        assert client.get("/api/health").json()["transaction_details_enabled"] is True
        response = client.get("/api/transactions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    row = next(item for item in payload["transactions"] if item["transaction_id"] == "txn_one")

    # Every stored financial field is present and exact.
    assert row["date"] == "2026-07-02"
    assert row["authorized_date"] == "2026-07-02"
    assert row["datetime"] == "2026-07-02T14:05:00Z"
    assert row["name"] == "Synthetic Grocery Run"
    assert row["merchant_name"] == "Synthetic Market"
    assert row["original_description"] == "SYNTHETIC MARKET #0001"
    assert row["amount"] == 42.5
    assert row["iso_currency_code"] == "USD"
    assert row["payment_channel"] == "in store"
    assert row["category"] == ["Shops", "Supermarkets and Groceries"]
    assert row["personal_finance_category"]["detailed"] == "FOOD_AND_DRINK_GROCERIES"
    assert row["location"]["city"] == "Testville"
    assert row["counterparties"][0]["name"] == "Synthetic Market"
    assert row["website"] == "synthetic-market.example"
    assert row["direction"] == "outflow"
    assert row["pending"] == 0

    # Account provenance is shown as a readable label.
    assert row["account"]["name"] == "Everyday Checking"
    assert row["account"]["mask"] == "0000"
    assert row["account"]["institution_name"] == "Synthetic Bank"


def test_version_history_shows_every_stored_version(tmp_path: Path) -> None:
    storage = enabled(tmp_path)
    seed(storage)
    storage.append_transaction_events(
        item_id="item_synthetic",
        transactions=[synthetic_transaction("txn_one", amount=47.99, merchant_name="Renamed Market")],
        event_type=EVENT_MODIFIED,
    )

    with ingress_client() as client:
        response = client.get("/api/transactions/txn_one/versions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version_count"] == 2
    first, second = payload["versions"]

    # The superseded version is still fully readable -- that is the point.
    assert first["event_type"] == EVENT_ADDED
    assert first["amount"] == 42.5
    assert first["merchant_name"] == "Synthetic Market"
    assert second["event_type"] == EVENT_MODIFIED
    assert second["amount"] == 47.99
    assert second["merchant_name"] == "Renamed Market"
    assert second["supersedes_event_id"] == first["event_id"]
    # The complete original Plaid payload is available for each version.
    assert first["raw"]["personal_finance_category"]["primary"] == "FOOD_AND_DRINK"


def test_unknown_transaction_is_not_found(tmp_path: Path) -> None:
    enabled(tmp_path)
    with ingress_client() as client:
        assert client.get("/api/transactions/txn_does_not_exist/versions").status_code == 404


def test_removed_transactions_are_hidden_by_default_but_retrievable(tmp_path: Path) -> None:
    storage = enabled(tmp_path)
    seed(storage)
    storage.append_transaction_events(
        item_id="item_synthetic",
        transactions=[{"transaction_id": "txn_two"}],
        event_type=EVENT_REMOVED,
    )

    with ingress_client() as client:
        default = client.get("/api/transactions").json()
        with_removed = client.get("/api/transactions?include_removed=true").json()
        versions = client.get("/api/transactions/txn_two/versions").json()

    assert [row["transaction_id"] for row in default["transactions"]] == ["txn_one"]
    assert any(row["transaction_id"] == "txn_two" and row["removed"] == 1 for row in with_removed["transactions"])
    # Its pre-removal detail is intact.
    assert versions["versions"][0]["amount"] == -1200.0


def test_search_is_parameterised_and_cannot_inject_sql(tmp_path: Path) -> None:
    storage = enabled(tmp_path)
    seed(storage)

    with ingress_client() as client:
        hit = client.get("/api/transactions?search=Payroll").json()
        broad = client.get("/api/transactions?search=Synthetic Market").json()
        miss = client.get("/api/transactions?search=nothing-matches-this").json()
        injection = client.get("/api/transactions?search=%27%3B DROP TABLE transaction_events%3B--").json()

    assert hit["count"] == 1
    assert broad["count"] == 2
    assert miss["count"] == 0
    assert injection["count"] == 0
    # The table is still there.
    assert storage.event_count() == 2


def test_detail_responses_contain_no_credentials(tmp_path: Path) -> None:
    storage = enabled(tmp_path)
    txn = synthetic_transaction("txn_secretish")
    txn["access_token"] = "access-" + "sandbox-" + "0000synthetic0000"
    txn["client_secret"] = "secret-" + "production-" + "0000synthetic0000"
    storage.append_transaction_events(
        item_id="item_synthetic", transactions=[txn], event_type=EVENT_ADDED
    )

    with ingress_client() as client:
        listing = client.get("/api/transactions")
        versions = client.get("/api/transactions/txn_secretish/versions")

    for response in (listing, versions):
        body = response.text
        assert "access-sandbox-" not in body
        assert "secret-production-" not in body
        assert "synthetic-access-token" not in body
        assert "synthetic-secret" not in body
        payload = json.loads(body)

        def keys(node: object) -> set[str]:
            if isinstance(node, dict):
                return set(node) | {key for value in node.values() for key in keys(value)}
            if isinstance(node, list):
                return {key for value in node for key in keys(value)}
            return set()

        assert not (keys(payload) & CREDENTIAL_FIELD_NAMES)


def test_detail_endpoints_are_still_ingress_only(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    enabled(tmp_path)
    with TestClient(main.app, client=("203.0.113.10", 50000)) as client:
        assert client.get("/api/transactions").status_code == 403
        assert client.get("/api/transactions/txn_one/versions").status_code == 403


def test_limits_are_bounded(tmp_path: Path) -> None:
    storage = enabled(tmp_path)
    storage.append_transaction_events(
        item_id="item_synthetic",
        transactions=[synthetic_transaction(f"txn_bulk_{index}") for index in range(30)],
        event_type=EVENT_ADDED,
    )

    with ingress_client() as client:
        assert client.get("/api/transactions?limit=5").json()["count"] == 5
        # Out-of-range limits are rejected rather than silently clamped.
        assert client.get("/api/transactions?limit=99999").status_code == 422
        assert client.get("/api/transactions?limit=0").status_code == 422


def test_option_defaults_to_off_in_the_addon_manifest() -> None:
    import yaml

    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "plaid_monthly_cashflow" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert config["options"]["show_transaction_details"] is False
    assert config["schema"]["show_transaction_details"] == "bool"
    assert main.DEFAULT_OPTIONS["show_transaction_details"] is False
