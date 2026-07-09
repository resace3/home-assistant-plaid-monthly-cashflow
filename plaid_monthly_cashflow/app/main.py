from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cashflow import monthly_cashflow, summarize_months, top_merchants
from .plaid_client import PlaidClientError, PlaidNotConfiguredError, PlaidService, PlaidSettings
from .security import redact_text, safe_error_message, scrub
from .storage import Storage


LOGGER = logging.getLogger("plaid_monthly_cashflow")
STATIC_DIR = Path(__file__).parent / "static"


DEFAULT_OPTIONS: dict[str, Any] = {
    "plaid_client_id": "",
    "plaid_secret": "",
    "plaid_env": "sandbox",
    "plaid_products": ["transactions"],
    "plaid_country_codes": ["US"],
    "sync_months_back": 12,
    "sync_interval_minutes": 360,
    "local_db_path": "/data/plaid_cashflow.sqlite",
    "currency": "USD",
    "debug_logging": False,
}


@dataclass(frozen=True)
class AddonConfig:
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str
    plaid_products: list[str]
    plaid_country_codes: list[str]
    sync_months_back: int
    sync_interval_minutes: int
    local_db_path: str
    currency: str
    debug_logging: bool

    @property
    def configured(self) -> bool:
        return bool(self.plaid_client_id.strip() and self.plaid_secret.strip())

    def plaid_settings(self) -> PlaidSettings:
        return PlaidSettings(
            client_id=self.plaid_client_id,
            secret=self.plaid_secret,
            environment=self.plaid_env,
            products=self.plaid_products,
            country_codes=self.plaid_country_codes,
            sync_months_back=self.sync_months_back,
            debug_logging=self.debug_logging,
        )


class PublicTokenRequest(BaseModel):
    public_token: str = Field(min_length=1)


def _as_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return default
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def load_config() -> AddonConfig:
    options_path = Path(os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json"))
    options = DEFAULT_OPTIONS.copy()

    if options_path.exists():
        with options_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            options.update(loaded)

    # Local development outside Supervisor usually has no /data directory.
    if options["local_db_path"] == "/data/plaid_cashflow.sqlite" and not Path("/data").exists():
        options["local_db_path"] = str(Path.cwd() / "data" / "plaid_cashflow.sqlite")

    plaid_env = str(options.get("plaid_env") or "sandbox").lower()
    if plaid_env not in {"sandbox", "production"}:
        raise RuntimeError("plaid_env must be sandbox or production")

    products = _as_list(options.get("plaid_products"), ["transactions"])
    invalid_products = sorted(set(products) - {"transactions"})
    if invalid_products:
        raise RuntimeError("Only the Plaid transactions product is supported")

    country_codes = _as_list(options.get("plaid_country_codes"), ["US"])
    invalid_countries = sorted(set(country_codes) - {"US", "CA"})
    if invalid_countries:
        raise RuntimeError("Only US and CA country codes are supported")

    return AddonConfig(
        plaid_client_id=str(options.get("plaid_client_id") or ""),
        plaid_secret=str(options.get("plaid_secret") or ""),
        plaid_env=plaid_env,
        plaid_products=products,
        plaid_country_codes=country_codes,
        sync_months_back=max(int(options.get("sync_months_back") or 12), 1),
        sync_interval_minutes=max(int(options.get("sync_interval_minutes") or 360), 5),
        local_db_path=str(options.get("local_db_path") or "/data/plaid_cashflow.sqlite"),
        currency=str(options.get("currency") or "USD").upper(),
        debug_logging=bool(options.get("debug_logging")),
    )


CONFIG = load_config()
logging.basicConfig(level=logging.DEBUG if CONFIG.debug_logging else logging.INFO)
STORAGE = Storage(CONFIG.local_db_path)
PLAID = PlaidService(CONFIG.plaid_settings())
SYNC_LOCK = asyncio.Lock()

app = FastAPI(title="Plaid Monthly Cashflow", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    STORAGE.init_db()
    asyncio.create_task(background_sync_loop())


@app.exception_handler(PlaidNotConfiguredError)
async def plaid_not_configured_handler(_, exc: PlaidNotConfiguredError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(PlaidClientError)
async def plaid_client_handler(_, exc: PlaidClientError):
    return JSONResponse(
        status_code=502,
        content={"detail": safe_error_message(exc, debug=CONFIG.debug_logging)},
    )


async def background_sync_loop() -> None:
    while True:
        await asyncio.sleep(CONFIG.sync_interval_minutes * 60)
        if not CONFIG.configured:
            continue
        if STORAGE.connected_item_count() == 0:
            continue
        try:
            await perform_sync()
        except Exception as exc:
            LOGGER.warning("Background sync failed: %s", redact_text(str(exc)))


def _http_error(exc: Exception, *, status_code: int = 500) -> HTTPException:
    return HTTPException(status_code=status_code, detail=safe_error_message(exc, debug=CONFIG.debug_logging))


def _safe_account_metadata(access_token: str) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    metadata = PLAID.get_item_metadata(access_token)
    item = metadata.get("item") or {}
    institution_id = item.get("institution_id")
    institution_name = PLAID.get_institution_name(institution_id)
    accounts = PLAID.get_accounts(access_token)
    return institution_id, institution_name, accounts


async def perform_sync() -> dict[str, Any]:
    async with SYNC_LOCK:
        sync_id, _ = STORAGE.start_sync_log()
        total_added = 0
        total_modified = 0
        total_removed = 0
        message = "ok"

        try:
            for item in STORAGE.get_items(include_tokens=True):
                access_token = item["access_token"]
                accounts = PLAID.get_accounts(access_token)
                STORAGE.upsert_accounts(item["item_id"], accounts)

                result = PLAID.sync_transactions(
                    access_token=access_token,
                    cursor=item.get("cursor"),
                )
                added = result.get("added") or []
                modified = result.get("modified") or []
                removed = result.get("removed") or []

                STORAGE.upsert_transactions(item["item_id"], added)
                STORAGE.upsert_transactions(item["item_id"], modified)
                STORAGE.mark_transactions_removed(removed)
                if result.get("next_cursor"):
                    STORAGE.update_item_cursor(item["item_id"], result["next_cursor"])

                total_added += len(added)
                total_modified += len(modified)
                total_removed += len(removed)

                if result.get("mode") == "fallback":
                    message = "transactions/get fallback used because transactions/sync was unavailable"

            finished_at = STORAGE.finish_sync_log(
                sync_id,
                status="ok",
                message=message,
                added_count=total_added,
                modified_count=total_modified,
                removed_count=total_removed,
            )
        except Exception as exc:
            finished_at = STORAGE.finish_sync_log(
                sync_id,
                status="error",
                message=safe_error_message(exc, debug=CONFIG.debug_logging),
                added_count=total_added,
                modified_count=total_modified,
                removed_count=total_removed,
            )
            raise

        return {
            "ok": True,
            "new_transactions": total_added,
            "modified_transactions": total_modified,
            "removed_transactions": total_removed,
            "total_transactions": STORAGE.transaction_count(),
            "last_sync_at": finished_at,
        }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "configured": CONFIG.configured,
        "plaid_env": CONFIG.plaid_env,
        "connected_items": STORAGE.connected_item_count(),
        "transaction_count": STORAGE.transaction_count(),
        "last_sync_at": STORAGE.last_sync_at(),
    }


@app.post("/api/link-token")
async def link_token() -> dict[str, str]:
    if not CONFIG.configured:
        raise HTTPException(
            status_code=400,
            detail="Add your Plaid Client ID, Secret, and environment in the Home Assistant add-on Configuration tab, save, and restart the add-on.",
        )
    return {"link_token": PLAID.create_link_token()}


@app.post("/api/exchange-public-token")
async def exchange_public_token(payload: PublicTokenRequest) -> dict[str, Any]:
    if not CONFIG.configured:
        raise HTTPException(status_code=400, detail="Plaid is not configured.")

    try:
        exchange = PLAID.exchange_public_token(payload.public_token)
        access_token = str(exchange["access_token"])
        item_id = str(exchange["item_id"])
        institution_id, institution_name, accounts = _safe_account_metadata(access_token)
        STORAGE.save_item(
            item_id=item_id,
            access_token=access_token,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        STORAGE.upsert_accounts(item_id, accounts)
        sync_result = await perform_sync()
        return scrub({"ok": True, "item_id": item_id, "sync": sync_result})
    except Exception as exc:
        raise _http_error(exc, status_code=502)


@app.post("/api/sync")
async def sync_now() -> dict[str, Any]:
    if not CONFIG.configured:
        raise HTTPException(status_code=400, detail="Plaid is not configured.")
    if STORAGE.connected_item_count() == 0:
        return {
            "ok": True,
            "new_transactions": 0,
            "modified_transactions": 0,
            "removed_transactions": 0,
            "total_transactions": 0,
            "last_sync_at": STORAGE.last_sync_at(),
        }
    try:
        return await perform_sync()
    except Exception as exc:
        raise _http_error(exc, status_code=502)


@app.get("/api/accounts")
async def accounts() -> list[dict[str, Any]]:
    return STORAGE.list_accounts()


@app.get("/api/transactions")
async def transactions(
    months_back: int | None = Query(default=None, ge=1, le=120),
    limit: int | None = Query(default=500, ge=1, le=5000),
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    return STORAGE.list_transactions(months_back=months_back, limit=limit, account_id=account_id)


@app.get("/api/monthly-cashflow")
async def monthly_cashflow_endpoint(
    months_back: int | None = Query(default=None, ge=1, le=120),
) -> dict[str, Any]:
    month_count = months_back or CONFIG.sync_months_back
    rows = STORAGE.list_transactions(months_back=month_count, limit=None)
    months = monthly_cashflow(rows, months_back=month_count)
    return {
        "currency": CONFIG.currency,
        "months": months,
        "summary": summarize_months(months),
    }


@app.get("/api/top-merchants")
async def top_merchants_endpoint(
    months_back: int | None = Query(default=None, ge=1, le=120),
    direction: str = Query(default="outflow"),
) -> list[dict[str, Any]]:
    if direction not in {"inflow", "outflow"}:
        raise HTTPException(status_code=400, detail="direction must be inflow or outflow")
    rows = STORAGE.list_transactions(months_back=months_back or CONFIG.sync_months_back, limit=None)
    return top_merchants(rows, direction=direction, limit=10)


@app.delete("/api/disconnect")
async def disconnect() -> dict[str, Any]:
    STORAGE.delete_all_plaid_data()
    return {"ok": True}
