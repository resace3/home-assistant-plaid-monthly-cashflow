from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .security import safe_error_message, scrub


class PlaidClientError(RuntimeError):
    pass


class PlaidNotConfiguredError(PlaidClientError):
    pass


class PlaidNotReadyError(PlaidClientError):
    """Plaid is still preparing transaction data for this Item.

    Raised instead of a generic failure so callers can retry later without
    treating the run as a hard error -- and, critically, without ever deleting
    or resetting anything that is already stored.
    """


@dataclass(frozen=True)
class PlaidSettings:
    client_id: str
    secret: str
    environment: str
    products: list[str]
    country_codes: list[str]
    sync_months_back: int
    backfill_days: int = 730
    redirect_uri: str = ""
    debug_logging: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip() and self.secret.strip())

    @property
    def link_days_requested(self) -> int:
        """Days of history to request at Link time.

        Plaid caps ``days_requested`` at 730. The dashboard's display range
        (``sync_months_back``) must not constrain how much history the
        permanent ledger is allowed to hold, so Link always asks for the
        backfill range instead.
        """
        return min(max(self.backfill_days, 1), 730)


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
        if _is_product_not_ready(exc):
            raise PlaidNotReadyError(
                "Plaid transactions are not ready yet. Wait a few minutes and sync again."
            ) from exc
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
                    days_requested=self.settings.link_days_requested
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

    def remove_item(self, access_token: str) -> bool:
        """Ask Plaid to deactivate the Item. Local history is unaffected."""
        try:
            from plaid.model.item_remove_request import ItemRemoveRequest

            self._get_client().item_remove(ItemRemoveRequest(access_token=access_token))
            return True
        except Exception:
            # Losing the remote deactivation must not block the local
            # disconnect, and it must never escalate into a data deletion.
            return False

    def sync_transaction_pages(
        self,
        *,
        access_token: str,
        cursor: str | None,
        page_size: int = 500,
        max_pages: int = 200,
    ) -> Iterator[dict[str, Any]]:
        """Yield one ``transactions/sync`` page at a time.

        Pages are yielded rather than accumulated so the caller can commit each
        page's events *together with* its cursor. That is what makes cursor
        advancement crash-safe: the cursor never moves ahead of durably stored
        events, and a crash mid-run simply replays the last page, which then
        deduplicates.
        """
        client = self._get_client()
        if not hasattr(client, "transactions_sync"):
            yield from self._transactions_get_pages(
                access_token=access_token,
                start_date=self.backfill_start_date(),
                end_date=date.today(),
                mode="fallback",
            )
            return

        try:
            from plaid.model.transactions_sync_request import TransactionsSyncRequest
        except ImportError as exc:  # pragma: no cover - plaid package guarantees this
            raise PlaidClientError("The plaid-python package is not installed.") from exc

        next_cursor = cursor
        pages = 0
        while pages < max_pages:
            request_args: dict[str, Any] = {"access_token": access_token, "count": page_size}
            # Plaid rejects an explicit null cursor; omitting it means "from the
            # beginning of this Item's available history".
            if next_cursor:
                request_args["cursor"] = next_cursor

            try:
                response = _to_dict(
                    self._get_client().transactions_sync(TransactionsSyncRequest(**request_args))
                ) or {}
            except Exception as exc:
                self._raise_clean(exc)
                return

            next_cursor = response.get("next_cursor")
            has_more = bool(response.get("has_more"))
            pages += 1
            yield {
                "mode": "sync",
                "added": response.get("added") or [],
                "modified": response.get("modified") or [],
                "removed": response.get("removed") or [],
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
            if not has_more:
                return

    def backfill_start_date(self) -> date:
        return date.today() - timedelta(days=max(self.settings.backfill_days, 1))

    def historical_transaction_pages(
        self,
        *,
        access_token: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield pages of ``transactions/get`` for a first-time backfill.

        Plaid only holds as much history as the Item was linked for, so an
        early ``start_date`` simply returns whatever is available rather than
        failing. The caller reports the earliest and latest dates actually
        retrieved.
        """
        yield from self._transactions_get_pages(
            access_token=access_token,
            start_date=start_date or self.backfill_start_date(),
            end_date=end_date or date.today(),
            mode="backfill",
        )

    def _transactions_get_pages(
        self,
        *,
        access_token: str,
        start_date: date,
        end_date: date,
        mode: str,
        page_size: int = 500,
        max_pages: int = 500,
        max_retries: int = 3,
    ) -> Iterator[dict[str, Any]]:
        try:
            from plaid.model.transactions_get_request import TransactionsGetRequest
            from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
        except ImportError as exc:  # pragma: no cover
            raise PlaidClientError("The plaid-python package is not installed.") from exc

        offset = 0
        total: int | None = None
        pages = 0

        while pages < max_pages and (total is None or offset < total):
            request = TransactionsGetRequest(
                access_token=access_token,
                start_date=start_date,
                end_date=end_date,
                options=TransactionsGetRequestOptions(count=page_size, offset=offset),
            )
            attempt = 0
            while True:
                try:
                    response = _to_dict(self._get_client().transactions_get(request)) or {}
                    break
                except Exception as exc:
                    # Retry only the transient "still preparing" case, and only
                    # a bounded number of times. Nothing stored is touched.
                    if _is_product_not_ready(exc) and attempt < max_retries - 1:
                        attempt += 1
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    self._raise_clean(exc)
                    return

            batch = response.get("transactions") or []
            total = int(response.get("total_transactions") or (offset + len(batch)))
            offset += len(batch)
            pages += 1
            yield {
                "mode": mode,
                "added": batch,
                "modified": [],
                "removed": [],
                "next_cursor": None,
                "has_more": offset < total and bool(batch),
                "total": total,
            }
            if not batch:
                return


def _is_product_not_ready(exc: BaseException) -> bool:
    body = getattr(exc, "body", None)
    if body:
        try:
            code = str(json.loads(body).get("error_code") or "")
        except (TypeError, ValueError):
            code = ""
        if code in {"PRODUCT_NOT_READY", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}:
            return True
    lowered = str(exc).lower()
    return "product_not_ready" in lowered or "transactions not ready" in lowered


def plaid_error_payload(exc: Exception) -> dict[str, Any]:
    message = safe_error_message(exc)
    body = getattr(exc, "body", None)
    if body:
        try:
            return {"error": message, "plaid": scrub(json.loads(body))}
        except ValueError:
            pass
    return {"error": message}
