from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta

from .security import safe_error_message


class PlaidClientError(RuntimeError):
    pass


class PlaidNotConfiguredError(PlaidClientError):
    pass


@dataclass(frozen=True)
class PlaidSettings:
    client_id: str
    secret: str
    environment: str
    products: list[str]
    country_codes: list[str]
    sync_months_back: int
    redirect_uri: str = ""
    debug_logging: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip() and self.secret.strip())


def _to_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {key: _to_dict(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    return value


class PlaidService:
    def __init__(self, settings: PlaidSettings) -> None:
        self.settings = settings
        self._client: Any | None = None

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def _require_configured(self) -> None:
        if not self.configured:
            raise PlaidNotConfiguredError(
                "Add your Plaid Client ID, Secret, and environment in the Home Assistant add-on Configuration tab, save, and restart the add-on."
            )

    def _get_client(self) -> Any:
        self._require_configured()
        if self._client is not None:
            return self._client

        try:
            from plaid import ApiClient, Configuration, Environment
            from plaid.api import plaid_api
        except ImportError as exc:
            raise PlaidClientError("The plaid-python package is not installed.") from exc

        if self.settings.environment == "sandbox":
            host = Environment.Sandbox
        elif self.settings.environment == "production":
            host = Environment.Production
        else:
            raise PlaidClientError("Unsupported Plaid environment.")

        configuration = Configuration(
            host=host,
            api_key={
                "clientId": self.settings.client_id,
                "secret": self.settings.secret,
            },
        )
        self._client = plaid_api.PlaidApi(ApiClient(configuration))
        return self._client

    def _raise_clean(self, exc: Exception) -> None:
        raise PlaidClientError(safe_error_message(exc, debug=self.settings.debug_logging)) from exc

    def create_link_token(self) -> str:
        self._require_configured()
        try:
            from plaid.model.country_code import CountryCode
            from plaid.model.link_token_create_request import LinkTokenCreateRequest
            from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
            from plaid.model.link_token_transactions import LinkTokenTransactions
            from plaid.model.products import Products

            products = [Products(product) for product in self.settings.products]
            request_args = {
                "products": products,
                "client_name": "Home Assistant Plaid Monthly Cashflow",
                "country_codes": [CountryCode(code) for code in self.settings.country_codes],
                "language": "en",
                "user": LinkTokenCreateRequestUser(client_user_id="home-assistant-local-user"),
            }
            if "transactions" in self.settings.products:
                request_args["transactions"] = LinkTokenTransactions(
                    days_requested=min(max(self.settings.sync_months_back * 31, 1), 730)
                )
            if self.settings.redirect_uri.strip():
                request_args["redirect_uri"] = self.settings.redirect_uri.strip()

            request = LinkTokenCreateRequest(
                **request_args,
            )
            response = self._get_client().link_token_create(request)
            return str(response["link_token"])
        except Exception as exc:
            self._raise_clean(exc)

    def exchange_public_token(self, public_token: str) -> dict[str, Any]:
        self._require_configured()
        try:
            from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

            response = self._get_client().item_public_token_exchange(
                ItemPublicTokenExchangeRequest(public_token=public_token)
            )
            return _to_dict(response)
        except Exception as exc:
            self._raise_clean(exc)

    def get_item_metadata(self, access_token: str) -> dict[str, Any]:
        try:
            from plaid.model.item_get_request import ItemGetRequest

            response = self._get_client().item_get(ItemGetRequest(access_token=access_token))
            return _to_dict(response) or {}
        except Exception:
            return {}

    def get_institution_name(self, institution_id: str | None) -> str | None:
        if not institution_id:
            return None
        try:
            from plaid.model.country_code import CountryCode
            from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest

            request = InstitutionsGetByIdRequest(
                institution_id=institution_id,
                country_codes=[CountryCode(code) for code in self.settings.country_codes],
            )
            response = self._get_client().institutions_get_by_id(request)
            data = _to_dict(response) or {}
            institution = data.get("institution") or {}
            return institution.get("name")
        except Exception:
            return None

    def get_accounts(self, access_token: str) -> list[dict[str, Any]]:
        try:
            from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

            response = self._get_client().accounts_balance_get(
                AccountsBalanceGetRequest(access_token=access_token)
            )
            data = _to_dict(response) or {}
            return data.get("accounts") or []
        except Exception as exc:
            self._raise_clean(exc)

    def sync_transactions(
        self,
        *,
        access_token: str,
        cursor: str | None,
    ) -> dict[str, Any]:
        client = self._get_client()
        if hasattr(client, "transactions_sync"):
            return self._transactions_sync(access_token=access_token, cursor=cursor)
        return self._transactions_get_fallback(access_token=access_token)

    def _transactions_sync(self, *, access_token: str, cursor: str | None) -> dict[str, Any]:
        try:
            from plaid.model.transactions_sync_request import TransactionsSyncRequest

            added: list[dict[str, Any]] = []
            modified: list[dict[str, Any]] = []
            removed: list[dict[str, Any]] = []
            next_cursor = cursor
            has_more = True

            while has_more:
                request = TransactionsSyncRequest(
                    access_token=access_token,
                    cursor=next_cursor,
                    count=500,
                )
                response = _to_dict(self._get_client().transactions_sync(request)) or {}
                added.extend(response.get("added") or [])
                modified.extend(response.get("modified") or [])
                removed.extend(response.get("removed") or [])
                next_cursor = response.get("next_cursor")
                has_more = bool(response.get("has_more"))

            return {
                "mode": "sync",
                "added": added,
                "modified": modified,
                "removed": removed,
                "next_cursor": next_cursor,
            }
        except Exception as exc:
            self._raise_clean(exc)

    def _transactions_get_fallback(self, *, access_token: str) -> dict[str, Any]:
        try:
            from plaid.model.transactions_get_request import TransactionsGetRequest
            from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

            end_date = date.today()
            start_date = end_date - relativedelta(months=self.settings.sync_months_back)
            transactions: list[dict[str, Any]] = []
            offset = 0
            total = None

            while total is None or offset < total:
                request = TransactionsGetRequest(
                    access_token=access_token,
                    start_date=start_date,
                    end_date=end_date,
                    options=TransactionsGetRequestOptions(count=500, offset=offset),
                )
                response = _to_dict(self._get_client().transactions_get(request)) or {}
                batch = response.get("transactions") or []
                transactions.extend(batch)
                total = int(response.get("total_transactions") or len(transactions))
                offset += len(batch)
                if not batch:
                    break

            return {
                "mode": "fallback",
                "added": transactions,
                "modified": [],
                "removed": [],
                "next_cursor": None,
            }
        except Exception as exc:
            self._raise_clean(exc)


def plaid_error_payload(exc: Exception) -> dict[str, Any]:
    message = safe_error_message(exc)
    body = getattr(exc, "body", None)
    if body:
        try:
            return {"error": message, "plaid": json.loads(body)}
        except ValueError:
            pass
    return {"error": message}
