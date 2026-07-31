"""Shared synthetic fixtures.

Every value in this file is invented. No real financial data, no real Plaid
credentials, and no real institution names are used anywhere in the tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Allow `import app...` when pytest is run from the repository root without
# PYTHONPATH being set (CI sets it; local runs often do not).
ADDON_ROOT = Path(__file__).resolve().parents[1] / "plaid_monthly_cashflow"
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))


def synthetic_transaction(
    transaction_id: str = "txn_synthetic_1",
    *,
    account_id: str = "acc_synthetic_checking",
    date: str = "2026-07-02",
    name: str = "Synthetic Grocery Run",
    merchant_name: str | None = "Synthetic Market",
    amount: float = 42.5,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    iso_currency_code: str | None = "USD",
    unofficial_currency_code: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A full-shaped, entirely fictional Plaid transaction payload."""
    payload: dict[str, Any] = {
        "transaction_id": transaction_id,
        "pending_transaction_id": pending_transaction_id,
        "account_id": account_id,
        "account_owner": None,
        "amount": amount,
        "iso_currency_code": iso_currency_code,
        "unofficial_currency_code": unofficial_currency_code,
        "category": ["Shops", "Supermarkets and Groceries"],
        "category_id": "19047000",
        "check_number": None,
        "date": date,
        "datetime": f"{date}T14:05:00Z",
        "authorized_date": date,
        "authorized_datetime": f"{date}T13:59:00Z",
        "location": {
            "address": "1 Fictional Way",
            "city": "Testville",
            "region": "CA",
            "postal_code": "00000",
            "country": "US",
            "lat": None,
            "lon": None,
            "store_number": None,
        },
        "name": name,
        "merchant_name": merchant_name,
        "merchant_entity_id": "syn_merchant_entity_1",
        "logo_url": None,
        "website": "synthetic-market.example",
        "original_description": "SYNTHETIC MARKET #0001",
        "payment_meta": {
            "by_order_of": None,
            "payee": None,
            "payer": None,
            "payment_method": None,
            "payment_processor": None,
            "ppd_id": None,
            "reason": None,
            "reference_number": None,
        },
        "payment_channel": "in store",
        "pending": pending,
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_GROCERIES",
            "confidence_level": "VERY_HIGH",
        },
        "personal_finance_category_icon_url": "https://plaid-category-icons.example/groceries.png",
        "counterparties": [
            {
                "name": "Synthetic Market",
                "type": "merchant",
                "logo_url": None,
                "website": "synthetic-market.example",
                "entity_id": "syn_merchant_entity_1",
                "confidence_level": "VERY_HIGH",
            }
        ],
        "transaction_code": None,
        "transaction_type": "place",
    }
    payload.update(overrides)
    return payload


def synthetic_account(
    account_id: str = "acc_synthetic_checking",
    *,
    name: str = "Synthetic Checking",
    mask: str = "0000",
    current: float = 1234.56,
    available: float = 1200.00,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "name": name,
        "official_name": f"{name} Account",
        "type": "depository",
        "subtype": "checking",
        "mask": mask,
        "balances": {
            "available": available,
            "current": current,
            "limit": None,
            "iso_currency_code": "USD",
            "unofficial_currency_code": None,
        },
    }


@pytest.fixture()
def storage(tmp_path: Path):
    from app.storage import Storage

    store = Storage(str(tmp_path / "plaid_cashflow.sqlite"))
    store.init_db()
    return store
