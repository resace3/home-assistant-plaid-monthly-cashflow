"""API surface, ingress hardening, config validation, and packaging checks.

Carried forward from the 0.1.8 suite and updated for the append-only ledger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from test_sync_engine import ACTION_HEADERS, FakePlaid, configure, ingress_client, page

from app import main
from app.plaid_client import PlaidService, PlaidSettings
from app.storage import Storage
from app.version import APP_VERSION, INGRESS_ENTRY, SCHEMA_VERSION
from conftest import synthetic_account, synthetic_transaction

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDON_ROOT = REPO_ROOT / "plaid_monthly_cashflow"


class CapturePlaidClient:
    def __init__(self) -> None:
        self.link_request = None

    def link_token_create(self, request):
        self.link_request = request
        return {"link_token": "link-synthetic-token"}


class CaptureTransactionsSyncClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

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
            "added": [synthetic_transaction("txn_after_cursor")],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-after-second-page",
            "has_more": False,
        }


def service(**overrides) -> PlaidService:
    settings = PlaidSettings(
        client_id="synthetic-client-id",
        secret="synthetic-secret",
        environment="sandbox",
        products=["transactions"],
        country_codes=["US"],
        sync_months_back=12,
        **overrides,
    )
    return PlaidService(settings)


def test_link_token_requests_the_backfill_history_range() -> None:
    capture = CapturePlaidClient()
    plaid = service(backfill_days=730, redirect_uri="https://example.test/plaid-oauth")
    plaid._client = capture

    assert plaid.create_link_token() == "link-synthetic-token"

    payload = capture.link_request.to_dict()
    assert payload["products"] == ["transactions"]
    assert payload["country_codes"] == ["US"]
    # The dashboard's 12-month display range no longer caps requested history.
    assert payload["transactions"]["days_requested"] == 730
    assert payload["redirect_uri"] == "https://example.test/plaid-oauth"


def test_first_sync_omits_null_cursor_then_paginates() -> None:
    capture = CaptureTransactionsSyncClient()
    plaid = service()
    plaid._client = capture

    pages = list(plaid.sync_transaction_pages(access_token="synthetic-access-token", cursor=None))

    assert "cursor" not in capture.requests[0]
    assert capture.requests[1]["cursor"] == "cursor-after-first-page"
    assert len(pages) == 2
    assert pages[-1]["next_cursor"] == "cursor-after-second-page"
    assert pages[-1]["added"][0]["transaction_id"] == "txn_after_cursor"


def test_configured_app_exchanges_token_syncs_and_summarizes(tmp_path: Path) -> None:
    plaid = FakePlaid(
        [
            page(
                [
                    synthetic_transaction(
                        "txn_paycheck",
                        amount=-2500.0,
                        date="2026-07-01",
                        name="Payroll",
                        merchant_name="Synthetic Employer",
                    ),
                    synthetic_transaction("txn_groceries", amount=125.25, date="2026-07-02"),
                ],
                cursor="cursor-after-first-sync",
            )
        ]
    )
    configure(tmp_path, plaid)

    with ingress_client() as client:
        health = client.get("/api/health").json()
        assert health["configured"] is True
        assert health["app_version"] == APP_VERSION
        assert client.post("/api/link-token", headers=ACTION_HEADERS).json() == {
            "link_token": "link-synthetic-token"
        }

        exchange = client.post(
            "/api/exchange-public-token",
            headers=ACTION_HEADERS,
            json={"public_token": "public-token-for-tests"},
        )
        assert exchange.status_code == 200
        assert exchange.json()["sync"]["new_transactions"] == 2
        assert "item_id" not in exchange.json()

        assert client.get("/api/accounts").json() == {"count": 1}

        monthly = client.get("/api/monthly-cashflow").json()
        assert monthly["summary"]["total_inflow"] == 2500.0
        assert monthly["summary"]["total_outflow"] == 125.25
        assert monthly["summary"]["net"] == 2374.75

        merchants = client.get("/api/top-merchants?direction=outflow").json()
        assert merchants == [
            {"merchant": "Synthetic Market", "amount": 125.25, "transaction_count": 1}
        ]


def test_api_accounts_response_minimized(tmp_path: Path) -> None:
    storage = configure(tmp_path, FakePlaid([]))
    storage.record_account_observations(
        "item_synthetic",
        [synthetic_account(name="Private Checking", mask="1234")],
        institution_name="Private Bank",
    )

    with ingress_client() as client:
        response = client.get("/api/accounts")

    assert response.status_code == 200
    assert response.json() == {"count": 1}
    for forbidden in ("account_id", "mask", "Private", "1234"):
        assert forbidden not in response.text


def test_api_transactions_absent_or_minimized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.schema import EVENT_ADDED

    storage = configure(tmp_path, FakePlaid([]))
    storage.append_transaction_events(
        item_id="item_synthetic",
        transactions=[synthetic_transaction("txn_min", amount=9.87)],
        event_type=EVENT_ADDED,
    )

    with ingress_client() as client:
        assert client.get("/api/transactions").status_code == 404

    monkeypatch.setenv(main.TRANSACTIONS_API_ENV, "1")
    with ingress_client() as client:
        response = client.get("/api/transactions")

    assert response.status_code == 200
    for forbidden in ("transaction_id", "account_id", "raw_json"):
        assert forbidden not in response.text
    assert response.json()[0]["merchant_name"] == "Synthetic Market"


def test_state_changing_routes_require_mutation_header(tmp_path: Path) -> None:
    configure(tmp_path, FakePlaid([]))

    with ingress_client() as client:
        assert client.post("/api/sync").status_code == 403
        assert client.post("/api/sync", headers=ACTION_HEADERS).status_code == 200
        assert client.delete("/api/disconnect").status_code == 403
        assert client.delete("/api/disconnect", headers=ACTION_HEADERS).status_code == 200
        assert client.post(
            "/api/sync", headers={**ACTION_HEADERS, "Origin": "https://evil.example"}
        ).status_code == 403


def test_security_headers_present(tmp_path: Path) -> None:
    configure(tmp_path, FakePlaid([]))

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
    configure(tmp_path, FakePlaid([]))
    with TestClient(main.app, client=("203.0.113.10", 50000)) as client:
        assert client.get("/api/health").status_code == 403


def test_ingress_middleware_allows_home_assistant_proxy_source(tmp_path: Path) -> None:
    configure(tmp_path, FakePlaid([]))
    with ingress_client() as client:
        assert client.get("/api/health").status_code == 200


def test_frontend_does_not_use_inner_html_with_untrusted_data() -> None:
    for path in Path(main.STATIC_DIR).glob("*.js"):
        text = path.read_text(encoding="utf-8")
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert unsafe not in text


def test_index_references_versioned_assets_and_no_third_party_charts() -> None:
    index = (Path(main.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr" not in index
    assert "chart.js" not in index.lower()
    assert "https://cdn.plaid.com/link/v2/stable/link-initialize.js" in index
    assert f"app-{APP_VERSION}.js" in index
    assert f"styles-{APP_VERSION}.css" in index


def test_destructive_dashboard_control_is_gone() -> None:
    index = (Path(main.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    app_js = (Path(main.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    assert "disconnectButton" not in index
    assert "Disconnect and delete local data" not in index
    assert "disconnectButton" not in app_js
    assert "diagnosticsButton" in index


def test_versioned_ingress_entry_serves_dashboard(tmp_path: Path) -> None:
    configure(tmp_path, FakePlaid([]))
    with ingress_client() as client:
        response = client.get(INGRESS_ENTRY)
    assert response.status_code == 200
    assert f"app-{APP_VERSION}.js" in response.text


def test_versions_are_consistent_across_the_addon() -> None:
    config = yaml.safe_load((ADDON_ROOT / "config.yaml").read_text(encoding="utf-8"))

    assert config["version"] == APP_VERSION
    assert config["ingress_entry"] == INGRESS_ENTRY
    assert SCHEMA_VERSION == 2


def test_version_stamped_assets_are_served_from_the_source_files(tmp_path: Path) -> None:
    configure(tmp_path, FakePlaid([]))
    source_js = (Path(main.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    source_css = (Path(main.STATIC_DIR) / "styles.css").read_text(encoding="utf-8")

    with ingress_client() as client:
        script = client.get(f"/static/app-{APP_VERSION}.js")
        stylesheet = client.get(f"/static/styles-{APP_VERSION}.css")
        # A stale version must not resolve.
        missing = client.get("/static/app-0.1.8.js")

    def normalize(text: str) -> str:
        return text.replace("\r\n", "\n")

    assert script.status_code == 200
    assert normalize(script.text) == normalize(source_js)
    assert stylesheet.status_code == 200
    assert normalize(stylesheet.text) == normalize(source_css)
    assert missing.status_code == 404


def test_config_yaml_options_match_defaults() -> None:
    config = yaml.safe_load((ADDON_ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert set(config["options"]) == set(main.DEFAULT_OPTIONS)
    assert set(config["schema"]) == set(main.DEFAULT_OPTIONS)
    assert config["options"]["backfill_days"] == 730
    assert config["ingress"] is True


def test_invalid_config_values_are_rejected() -> None:
    base = main.DEFAULT_OPTIONS.copy()
    with pytest.raises(RuntimeError, match="plaid_env"):
        main._build_config({**base, "plaid_env": "development"})
    with pytest.raises(RuntimeError, match="plaid_redirect_uri"):
        main._build_config({**base, "plaid_redirect_uri": "javascript:alert(1)"})
    with pytest.raises(RuntimeError, match="currency"):
        main._build_config({**base, "currency": "USD<script>"})
    with pytest.raises(RuntimeError, match="sync_months_back"):
        main._build_config({**base, "sync_months_back": 240})
    with pytest.raises(RuntimeError, match="backfill_days"):
        main._build_config({**base, "backfill_days": 5000})


def test_dashboard_range_and_backfill_range_are_independent() -> None:
    config = main._build_config({**main.DEFAULT_OPTIONS, "sync_months_back": 6, "backfill_days": 730})
    assert config.sync_months_back == 6
    assert config.backfill_days == 730
    assert config.plaid_settings().link_days_requested == 730


def test_environment_mismatch_message_promises_no_deletion(tmp_path: Path) -> None:
    storage = configure(tmp_path, FakePlaid([]))
    with storage.writer() as conn:
        conn.execute("UPDATE items SET plaid_env = 'sandbox'")
    main.CONFIG = main.AddonConfig(**{**main.CONFIG.__dict__, "plaid_env": "production"})

    with ingress_client() as client:
        health = client.get("/api/health")
        sync = client.post("/api/sync", headers=ACTION_HEADERS)
        link = client.post("/api/link-token", headers=ACTION_HEADERS)

    assert health.json()["connection_requires_reset"] is True
    assert health.json()["connection_environment"] == "sandbox"
    assert sync.status_code == 409
    assert link.status_code == 409
    detail = sync.json()["detail"]
    assert "Disconnect and delete local data" not in detail
    assert "preserved" in detail
    assert health.json()["transaction_event_count"] == 0


def test_legacy_item_environment_is_inferred_from_access_token(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    storage.init_db()
    storage.save_item(
        item_id="item_legacy_env",
        access_token="access-sandbox-synthetic-token",
        plaid_env="sandbox",
    )
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute("UPDATE items SET plaid_env = NULL")
        conn.commit()

    storage.reconcile_item_environments()

    assert storage.connection_environment() == "sandbox"
    assert storage.connection_requires_reset("production") is True
    assert storage.connection_requires_reset("sandbox") is False
