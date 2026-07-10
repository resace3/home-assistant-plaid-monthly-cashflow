from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.plaid_client import PlaidService, PlaidSettings
from app.security import redact_text, safe_error_message, scrub
from app.storage import Storage


ACTION_HEADERS = {main.MUTATION_HEADER: "1"}


def ingress_client(host: str = "172.30.32.2") -> TestClient:
    return TestClient(main.app, client=(host, 50000))


def configure_test_app(tmp_path: Path, *, fake_plaid=None, configured: bool = True) -> Storage:
    storage = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    storage.init_db()
    main.STORAGE = storage
    main.PLAID = fake_plaid or FakePlaid()
    main.CONFIG = main.AddonConfig(
        plaid_client_id="client-id-for-tests" if configured else "",
        plaid_secret="secret-for-tests" if configured else "",
        plaid_env="sandbox",
        plaid_redirect_uri="",
        plaid_products=["transactions"],
        plaid_country_codes=["US"],
        sync_months_back=12,
        sync_interval_minutes=360,
        local_db_path=str(tmp_path / "plaid_cashflow.sqlite"),
        currency="USD",
        debug_logging=False,
    )
    return storage


class CapturePlaidClient:
    def __init__(self) -> None:
        self.link_request = None

    def link_token_create(self, request):
        self.link_request = request
        return {"link_token": "link-sandbox-test"}


class CaptureTransactionsSyncClient:
    def __init__(self) -> None:
        self.requests = []

    def transactions_sync(self, request):
        payload = request.to_dict()
        self.requests.append(payload)
        if len(self.requests) == 1:
            return {
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": "cursor-after-first-page",
                "has_more": True,
            }
        return {
            "added": [
                {
                    "transaction_id": "txn_after_cursor",
                    "account_id": "acc_checking",
                    "date": "2026-07-03",
                    "name": "Coffee",
                    "amount": 4.5,
                    "pending": False,
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-after-second-page",
            "has_more": False,
        }


class FakePlaid:
    def create_link_token(self) -> str:
        return "link-sandbox-test"

    def exchange_public_token(self, public_token: str) -> dict[str, str]:
        assert public_token == "public-token-for-tests"
        return {"access_token": "token-for-tests", "item_id": "item-for-tests"}

    def get_item_metadata(self, access_token: str) -> dict:
        assert access_token == "token-for-tests"
        return {"item": {"institution_id": "ins_test"}}

    def get_institution_name(self, institution_id: str | None) -> str | None:
        assert institution_id == "ins_test"
        return "Sandbox Bank"

    def get_accounts(self, access_token: str) -> list[dict]:
        assert access_token == "token-for-tests"
        return [
            {
                "account_id": "acc_checking",
                "name": "Plaid Checking",
                "type": "depository",
                "subtype": "checking",
                "balances": {"current": 1200.0, "available": 1190.0, "iso_currency_code": "USD"},
            }
        ]

    def sync_transactions(self, *, access_token: str, cursor: str | None) -> dict:
        assert access_token == "token-for-tests"
        assert cursor is None
        return {
            "mode": "sync",
            "added": [
                {
                    "transaction_id": "txn_paycheck",
                    "account_id": "acc_checking",
                    "date": "2026-07-01",
                    "name": "Payroll",
                    "merchant_name": "Employer",
                    "amount": -2500.0,
                    "iso_currency_code": "USD",
                    "pending": False,
                },
                {
                    "transaction_id": "txn_groceries",
                    "account_id": "acc_checking",
                    "date": "2026-07-02",
                    "name": "Groceries",
                    "merchant_name": "Market",
                    "amount": 125.25,
                    "iso_currency_code": "USD",
                    "pending": False,
                },
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-after-first-sync",
        }


def test_link_token_requests_configured_transaction_history() -> None:
    capture = CapturePlaidClient()
    service = PlaidService(
        PlaidSettings(
            client_id="client-id-for-tests",
            secret="secret-for-tests",
            environment="sandbox",
            products=["transactions"],
            country_codes=["US"],
            sync_months_back=12,
            redirect_uri="https://example.test/plaid-oauth",
        )
    )
    service._client = capture

    assert service.create_link_token() == "link-sandbox-test"

    payload = capture.link_request.to_dict()
    assert payload["products"] == ["transactions"]
    assert payload["country_codes"] == ["US"]
    assert payload["transactions"]["days_requested"] == 372
    assert payload["redirect_uri"] == "https://example.test/plaid-oauth"


def test_first_transactions_sync_omits_null_cursor_then_paginates() -> None:
    capture = CaptureTransactionsSyncClient()
    service = PlaidService(
        PlaidSettings(
            client_id="client-id-for-tests",
            secret="secret-for-tests",
            environment="sandbox",
            products=["transactions"],
            country_codes=["US"],
            sync_months_back=12,
        )
    )
    service._client = capture

    result = service.sync_transactions(access_token="access-token-for-tests", cursor=None)

    assert "cursor" not in capture.requests[0]
    assert capture.requests[1]["cursor"] == "cursor-after-first-page"
    assert result["next_cursor"] == "cursor-after-second-page"
    assert result["added"][0]["transaction_id"] == "txn_after_cursor"


def test_configured_app_exchanges_token_syncs_and_summarizes(tmp_path: Path) -> None:
    configure_test_app(tmp_path)

    with ingress_client() as client:
        health = client.get("/api/health").json()
        assert health["configured"] is True
        assert client.post("/api/link-token", headers=ACTION_HEADERS).json() == {"link_token": "link-sandbox-test"}

        exchange = client.post(
            "/api/exchange-public-token",
            headers=ACTION_HEADERS,
            json={"public_token": "public-token-for-tests"},
        )
        assert exchange.status_code == 200
        assert exchange.json()["sync"]["new_transactions"] == 2
        assert "item_id" not in exchange.json()

        accounts = client.get("/api/accounts").json()
        assert accounts == {"count": 1}

        monthly = client.get("/api/monthly-cashflow").json()
        assert monthly["summary"]["total_inflow"] == 2500.0
        assert monthly["summary"]["total_outflow"] == 125.25
        assert monthly["summary"]["net"] == 2374.75

        merchants = client.get("/api/top-merchants?direction=outflow").json()
        assert merchants == [{"merchant": "Market", "amount": 125.25, "transaction_count": 1}]


def test_redaction_scrubs_tokens_and_ids() -> None:
    access = "access-" + "sandbox-" + "abc123def456"
    public = "public-" + "production-" + "abc123def456"
    long_token = "A" * 36
    payload = {
        "access_token": access,
        "item_id": "item-for-tests",
        "nested": [{"account_id": "account-for-tests", "message": f"bad {public} {long_token}"}],
    }

    redacted = scrub(payload)
    assert redacted["access_token"] == "[redacted]"
    assert redacted["item_id"] == "[redacted]"
    assert redacted["nested"][0]["account_id"] == "[redacted]"
    assert access not in json.dumps(redacted)
    assert public not in json.dumps(redacted)
    assert long_token not in json.dumps(redacted)
    assert redact_text(f"value {access}") == "value [redacted]"


def test_safe_error_message_redacts_plaid_body() -> None:
    access = "access-" + "sandbox-" + "abc123def456"
    secret = "secret-" + "production-" + "abc123def456"

    class FakePlaidError(Exception):
        body = json.dumps(
            {
                "error_code": "INVALID_API_KEYS",
                "error_message": f"bad keys {access} {secret}",
                "account_id": "account-for-tests",
                "item_id": "item-for-tests",
                "request_id": "request-for-tests",
            }
        )

    debug_message = safe_error_message(FakePlaidError(), debug=True)
    assert access not in debug_message
    assert secret not in debug_message
    assert "[redacted]" in debug_message
    assert safe_error_message(FakePlaidError(), debug=False) == (
        "Plaid rejected the configured keys. Check that the client ID, secret, and environment match."
    )


def test_storage_does_not_store_raw_json_by_default(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    storage.init_db()
    storage.upsert_transactions(
        "item-for-tests",
        [
            {
                "transaction_id": "txn-for-tests",
                "account_id": "account-for-tests",
                "date": "2026-07-01",
                "name": "Synthetic transaction",
                "merchant_name": "Synthetic merchant",
                "amount": 12.34,
                "pending": False,
                "routing_number": "not-persisted",
                "raw_extra": {"secret": "not-persisted"},
            }
        ],
    )

    with sqlite3.connect(storage.db_path) as conn:
        row = conn.execute("SELECT raw_json FROM transactions WHERE transaction_id = ?", ("txn-for-tests",)).fetchone()
    assert row is not None
    assert row[0] is None


def test_delete_all_plaid_data_removes_rows_and_rotates_or_removes_key(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    storage.init_db()
    storage.save_item(item_id="item-for-tests", access_token="token-for-tests", plaid_env="sandbox")
    storage.upsert_accounts("item-for-tests", [{"account_id": "account-for-tests", "name": "Should not persist"}])
    storage.upsert_transactions(
        "item-for-tests",
        [
            {
                "transaction_id": "txn-for-tests",
                "account_id": "account-for-tests",
                "date": "2026-07-01",
                "name": "Synthetic transaction",
                "amount": 1,
            }
        ],
    )
    original_key = storage.key_path.read_bytes()

    storage.delete_all_plaid_data()

    assert storage.connected_item_count() == 0
    assert storage.account_count() == 0
    assert storage.transaction_count() == 0
    assert not storage.key_path.exists()
    for sidecar in storage._sidecar_paths():
        assert sidecar.parent == storage.db_path.parent

    storage.save_item(item_id="item-for-tests", access_token="token-for-tests", plaid_env="sandbox")
    assert storage.key_path.read_bytes() != original_key


def test_api_accounts_response_minimized(tmp_path: Path) -> None:
    storage = configure_test_app(tmp_path)
    storage.upsert_accounts(
        "item-for-tests",
        [
            {
                "account_id": "account-for-tests",
                "name": "Private checking",
                "official_name": "Private official checking",
                "mask": "1234",
                "balances": {"current": 1000, "available": 900},
            }
        ],
    )

    with ingress_client() as client:
        response = client.get("/api/accounts")

    assert response.status_code == 200
    assert response.json() == {"count": 1}
    assert "account_id" not in response.text
    assert "mask" not in response.text
    assert "Private" not in response.text


def test_api_transactions_absent_or_minimized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = configure_test_app(tmp_path)
    storage.upsert_transactions(
        "item-for-tests",
        [
            {
                "transaction_id": "txn-for-tests",
                "account_id": "account-for-tests",
                "date": "2026-07-01",
                "name": "Synthetic transaction",
                "merchant_name": "Synthetic merchant",
                "amount": 9.87,
            }
        ],
    )

    with ingress_client() as client:
        assert client.get("/api/transactions").status_code == 404

    monkeypatch.setenv(main.TRANSACTIONS_API_ENV, "1")
    with ingress_client() as client:
        response = client.get("/api/transactions")

    assert response.status_code == 200
    assert "transaction_id" not in response.text
    assert "account_id" not in response.text
    assert "raw_json" not in response.text
    assert response.json()[0]["merchant_name"] == "Synthetic merchant"


def test_state_changing_routes_require_mutation_header(tmp_path: Path) -> None:
    configure_test_app(tmp_path)

    with ingress_client() as client:
        assert client.post("/api/sync").status_code == 403
        assert client.post("/api/sync", headers=ACTION_HEADERS).status_code == 200
        assert client.delete("/api/disconnect").status_code == 403
        assert client.delete("/api/disconnect", headers=ACTION_HEADERS).status_code == 200
        assert client.post(
            "/api/sync",
            headers={**ACTION_HEADERS, "Origin": "https://evil.example"},
        ).status_code == 403


def test_security_headers_present(tmp_path: Path) -> None:
    configure_test_app(tmp_path)

    with ingress_client() as client:
        response = client.get("/api/health")
        html = client.get("/")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "https://cdn.plaid.com" in response.headers["content-security-policy"]
    assert "cdn.jsdelivr" not in response.headers["content-security-policy"]
    assert html.headers["cache-control"] == "no-store"


def test_ingress_middleware_denies_untrusted_source(tmp_path: Path) -> None:
    configure_test_app(tmp_path)

    with ingress_client("203.0.113.10") as client:
        response = client.get("/api/health")

    assert response.status_code == 403


def test_ingress_middleware_allows_home_assistant_proxy_source(tmp_path: Path) -> None:
    configure_test_app(tmp_path)

    with ingress_client("172.30.32.2") as client:
        response = client.get("/api/health")

    assert response.status_code == 200


def test_frontend_does_not_use_inner_html_with_untrusted_data() -> None:
    static_dir = Path(main.STATIC_DIR)
    for path in static_dir.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "innerHTML" not in text
        assert "outerHTML" not in text
        assert "insertAdjacentHTML" not in text
        assert "document.write" not in text


def test_no_external_chart_cdn_in_index() -> None:
    index = (Path(main.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr" not in index
    assert "chart.js" not in index.lower()
    assert "https://cdn.plaid.com/link/v2/stable/link-initialize.js" in index
    assert "app-0.1.8.js" in index
    assert "styles-0.1.8.css" in index


def test_versioned_ingress_entry_serves_dashboard(tmp_path: Path) -> None:
    configure_test_app(tmp_path)

    with ingress_client() as client:
        response = client.get("/v018/")

    assert response.status_code == 200
    assert "app-0.1.8.js" in response.text


def test_invalid_config_values_are_rejected() -> None:
    base = main.DEFAULT_OPTIONS.copy()
    with pytest.raises(RuntimeError, match="plaid_env"):
        main._build_config({**base, "plaid_env": "development"})
    with pytest.raises(RuntimeError, match="plaid_redirect_uri"):
        main._build_config({**base, "plaid_redirect_uri": "javascript:alert(1)"})
    with pytest.raises(RuntimeError, match="currency"):
        main._build_config({**base, "currency": "USD<script>"})
    with pytest.raises(RuntimeError, match="sync_months_back"):
        main._build_config({**base, "sync_months_back": 36})


def test_environment_mismatch_requires_disconnect_and_reconnect(tmp_path: Path) -> None:
    storage = configure_test_app(tmp_path)
    storage.save_item(
        item_id="sandbox-item-for-tests",
        access_token="access-sandbox-token-for-tests",
        plaid_env="sandbox",
    )
    main.CONFIG = main.AddonConfig(
        **{**main.CONFIG.__dict__, "plaid_env": "production"}
    )

    with ingress_client() as client:
        health = client.get("/api/health")
        sync = client.post("/api/sync", headers=ACTION_HEADERS)
        link = client.post("/api/link-token", headers=ACTION_HEADERS)

    assert health.status_code == 200
    assert health.json()["connection_requires_reset"] is True
    assert health.json()["connection_environment"] == "sandbox"
    assert health.json()["connected_items"] == 0
    assert health.json()["transaction_count"] == 0
    assert sync.status_code == 409
    assert link.status_code == 409
    assert "Disconnect and delete local data" in sync.json()["detail"]


def test_legacy_item_environment_is_inferred_from_access_token(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    storage.init_db()
    storage.save_item(
        item_id="legacy-item-for-tests",
        access_token="access-sandbox-token-for-tests",
        plaid_env="sandbox",
    )
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute("UPDATE items SET plaid_env = NULL")
        conn.commit()

    storage.reconcile_item_environments()

    assert storage.connection_environment() == "sandbox"
    assert storage.connection_requires_reset("production") is True
    assert storage.connection_requires_reset("sandbox") is False


def test_disconnect_clears_environment_mismatch(tmp_path: Path) -> None:
    storage = configure_test_app(tmp_path)
    storage.save_item(
        item_id="sandbox-item-for-tests",
        access_token="access-sandbox-token-for-tests",
        plaid_env="sandbox",
    )
    main.CONFIG = main.AddonConfig(
        **{**main.CONFIG.__dict__, "plaid_env": "production"}
    )

    with ingress_client() as client:
        assert client.delete("/api/disconnect", headers=ACTION_HEADERS).status_code == 200
        health = client.get("/api/health").json()

    assert health["connection_requires_reset"] is False
    assert health["connection_environment"] is None
    assert health["connected_items"] == 0
