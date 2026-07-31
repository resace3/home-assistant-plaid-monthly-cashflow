"""Redaction, secret handling, and the diagnostics/health API surface."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from test_sync_engine import ACTION_HEADERS, FakePlaid, configure, ingress_client, page

from app import main
from app.schema import EVENT_ADDED
from app.security import classify_error, fingerprint, redact_text, safe_error_message, strip_secrets
from app.storage import Storage
from conftest import synthetic_account, synthetic_transaction

# Fabricated credential-shaped strings, assembled at runtime so no literal
# token-looking value is committed to the repository.
FAKE_ACCESS_TOKEN = "access-" + "sandbox-" + "0000synthetic0000"
FAKE_SECRET = "secret-" + "production-" + "0000synthetic0000"


def test_strip_secrets_removes_credentials_but_keeps_financial_fields() -> None:
    payload = {
        "transaction_id": "txn_1",
        "account_id": "acc_1",
        "amount": 12.34,
        "merchant_name": "Synthetic Market",
        "mask": "0000",
        "access_token": FAKE_ACCESS_TOKEN,
        "nested": {"secret": FAKE_SECRET, "location": {"city": "Testville"}},
        "counterparties": [{"name": "Synthetic Market", "api_key": "should-vanish"}],
    }

    cleaned = strip_secrets(payload)
    serialized = json.dumps(cleaned)

    # Credentials are gone.
    assert "access_token" not in cleaned
    assert "secret" not in cleaned["nested"]
    assert "api_key" not in cleaned["counterparties"][0]
    assert FAKE_ACCESS_TOKEN not in serialized
    assert FAKE_SECRET not in serialized

    # Financial fields are untouched -- the ledger exists to keep these.
    assert cleaned["amount"] == 12.34
    assert cleaned["merchant_name"] == "Synthetic Market"
    assert cleaned["transaction_id"] == "txn_1"
    assert cleaned["account_id"] == "acc_1"
    assert cleaned["mask"] == "0000"
    assert cleaned["nested"]["location"]["city"] == "Testville"


def test_credentials_in_a_payload_never_reach_sqlite(storage: Storage) -> None:
    txn = synthetic_transaction("txn_leaky")
    txn["access_token"] = FAKE_ACCESS_TOKEN
    txn["client_secret"] = FAKE_SECRET

    storage.append_transaction_events(item_id="i", transactions=[txn], event_type=EVENT_ADDED)

    raw_bytes = Path(storage.db_path).read_bytes()
    assert FAKE_ACCESS_TOKEN.encode() not in raw_bytes
    assert FAKE_SECRET.encode() not in raw_bytes
    with storage.connect() as conn:
        stored = json.loads(conn.execute("SELECT raw_json FROM transaction_events").fetchone()[0])
    assert "access_token" not in stored
    assert stored["merchant_name"] == "Synthetic Market"


def test_cursor_is_stored_only_as_a_fingerprint_in_the_audit_log(tmp_path: Path) -> None:
    import asyncio

    cursor_value = "plaid-cursor-" + "0000synthetic0000"
    plaid = FakePlaid([page([synthetic_transaction("txn_fp")], cursor=cursor_value)])
    storage = configure(tmp_path, plaid)
    asyncio.run(main.perform_sync())

    with storage.connect() as conn:
        run = conn.execute("SELECT * FROM sync_runs ORDER BY sync_id DESC LIMIT 1").fetchone()
        event_fp = conn.execute("SELECT cursor_fingerprint FROM transaction_events").fetchone()[0]

    assert run["ending_cursor_fingerprint"] == fingerprint(cursor_value)
    assert run["ending_cursor_fingerprint"] != cursor_value
    assert event_fp != cursor_value
    assert cursor_value not in json.dumps(dict(run))
    assert fingerprint(None) is None


def test_diagnostics_endpoint_exposes_aggregates_only(tmp_path: Path) -> None:
    import asyncio

    plaid = FakePlaid(
        [page([synthetic_transaction("txn_diag", name="Very Private Merchant Name")], cursor="c1")],
        accounts=[synthetic_account(name="Very Private Account", mask="9876")],
    )
    configure(tmp_path, plaid)
    asyncio.run(main.perform_sync())

    with ingress_client() as client:
        response = client.get("/api/diagnostics")

    assert response.status_code == 200
    body = response.text
    payload = response.json()

    assert payload["aggregates"]["total_transaction_events"] == 1
    assert payload["aggregates"]["active_transactions"] == 1
    assert payload["integrity"]["ok"] is True
    assert payload["hash_chain"]["ok"] is True

    # No private content of any kind.
    for forbidden in (
        "Very Private Merchant Name",
        "Very Private Account",
        "9876",
        "txn_diag",
        "acc_synthetic_checking",
        "item_synthetic",
        "synthetic-access-token",
        "Testville",
        "FOOD_AND_DRINK",
    ):
        assert forbidden not in body, forbidden

    # No stored Plaid payload is echoed back -- only a count of how many
    # events retain one.
    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {key for value in node.values() for key in keys(value)}
        if isinstance(node, list):
            return {key for value in node for key in keys(value)}
        return set()

    assert "raw_json" not in keys(payload)
    assert payload["aggregates"]["events_with_raw_json"] == 1


def test_diagnostics_labels_survive_the_output_scrubber(storage: Storage) -> None:
    """Check names must stay readable after redaction.

    ``redact_text`` masks any 32+ character token-shaped run, so a long
    snake_case identifier gets replaced by "[redacted]" and the diagnostics
    screen shows a nameless row. Names are static and non-sensitive, so the
    fix is to keep them short rather than to weaken redaction.
    """
    from app.security import scrub

    report = storage.integrity_report()
    for check in report["checks"]:
        name = check["check"]
        assert len(name) < 32, f"{name} is long enough to be redacted"
        assert scrub({"check": name})["check"] == name


def test_health_endpoint_has_no_private_fields(tmp_path: Path) -> None:
    import asyncio

    plaid = FakePlaid([page([synthetic_transaction("txn_health")], cursor="c1")])
    configure(tmp_path, plaid)
    asyncio.run(main.perform_sync())

    with ingress_client() as client:
        response = client.get("/api/health")

    body = response.text
    assert response.json()["transaction_event_count"] == 1
    for forbidden in ("txn_health", "synthetic-access-token", "acc_synthetic_checking", "item_synthetic"):
        assert forbidden not in body


def test_sync_response_carries_no_secrets(tmp_path: Path) -> None:
    plaid = FakePlaid([page([synthetic_transaction("txn_resp")], cursor="c1")])
    configure(tmp_path, plaid)

    with ingress_client() as client:
        response = client.post("/api/sync", headers=ACTION_HEADERS)

    assert response.status_code == 200
    for forbidden in ("synthetic-access-token", "synthetic-secret", "synthetic-client-id", "c1"):
        assert forbidden not in response.text


def test_logs_contain_no_secrets_during_a_failed_sync(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio

    plaid = FakePlaid([page([synthetic_transaction("txn_logfail")], cursor="c1")], fail_on_page=0)
    configure(tmp_path, plaid)

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        asyncio.run(main.perform_sync())

    text = caplog.text
    for forbidden in ("synthetic-access-token", "synthetic-secret", FAKE_ACCESS_TOKEN, FAKE_SECRET):
        assert forbidden not in text


def test_safe_error_message_redacts_plaid_body() -> None:
    class FakePlaidError(Exception):
        body = json.dumps(
            {
                "error_code": "INVALID_API_KEYS",
                "error_message": f"bad keys {FAKE_ACCESS_TOKEN} {FAKE_SECRET}",
                "item_id": "item-synthetic",
            }
        )

    debug_message = safe_error_message(FakePlaidError(), debug=True)
    assert FAKE_ACCESS_TOKEN not in debug_message
    assert FAKE_SECRET not in debug_message
    assert "[redacted]" in debug_message
    assert safe_error_message(FakePlaidError(), debug=False) == (
        "Plaid rejected the configured keys. Check that the client ID, secret, and environment match."
    )


def test_classify_error_returns_non_sensitive_labels() -> None:
    class PlaidNotReady(Exception):
        body = json.dumps({"error_code": "PRODUCT_NOT_READY", "error_message": "still preparing"})

    assert classify_error(PlaidNotReady()) == "PRODUCT_NOT_READY"
    assert classify_error(TimeoutError("connection timed out")) == "TIMEOUT"
    assert classify_error(RuntimeError(f"boom {FAKE_ACCESS_TOKEN}")) == "RuntimeError"
    assert FAKE_ACCESS_TOKEN not in classify_error(RuntimeError(f"boom {FAKE_ACCESS_TOKEN}"))


def test_redact_text_masks_token_shapes() -> None:
    assert redact_text(f"value {FAKE_ACCESS_TOKEN}") == "value [redacted]"
    assert FAKE_SECRET not in redact_text(f"oops {FAKE_SECRET}")


def test_transactions_api_stays_disabled_by_default(tmp_path: Path) -> None:
    plaid = FakePlaid([page([synthetic_transaction("txn_hidden")], cursor="c1")])
    configure(tmp_path, plaid)

    with ingress_client() as client:
        assert client.get("/api/transactions").status_code == 404
