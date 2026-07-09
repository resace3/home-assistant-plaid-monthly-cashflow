from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.plaid_client import PlaidService, PlaidSettings
from app.storage import Storage


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
    main.STORAGE = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    main.STORAGE.init_db()
    main.PLAID = FakePlaid()
    main.CONFIG = main.AddonConfig(
        plaid_client_id="client-id-for-tests",
        plaid_secret="secret-for-tests",
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

    with TestClient(main.app) as client:
        health = client.get("/api/health").json()
        assert health["configured"] is True
        assert client.post("/api/link-token").json() == {"link_token": "link-sandbox-test"}

        exchange = client.post(
            "/api/exchange-public-token",
            json={"public_token": "public-token-for-tests"},
        )
        assert exchange.status_code == 200
        assert exchange.json()["sync"]["new_transactions"] == 2

        accounts = client.get("/api/accounts").json()
        assert accounts[0]["institution_name"] == "Sandbox Bank"

        monthly = client.get("/api/monthly-cashflow").json()
        assert monthly["summary"]["total_inflow"] == 2500.0
        assert monthly["summary"]["total_outflow"] == 125.25
        assert monthly["summary"]["net"] == 2374.75

        merchants = client.get("/api/top-merchants?direction=outflow").json()
        assert merchants == [{"merchant": "Market", "amount": 125.25, "transaction_count": 1}]
