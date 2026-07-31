from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cashflow import monthly_cashflow, summarize_months, top_merchants
from .plaid_client import (
    PlaidClientError,
    PlaidNotConfiguredError,
    PlaidNotReadyError,
    PlaidService,
    PlaidSettings,
)
from .security import classify_error, redact_text, safe_error_message, scrub
from .storage import Storage
from .version import APP_VERSION, INGRESS_ENTRY

LOGGER = logging.getLogger("plaid_monthly_cashflow")
STATIC_DIR = Path(__file__).parent / "static"
MUTATION_HEADER = "X-Plaid-Cashflow-Action"
DEFAULT_INGRESS_CIDRS = ("172.30.32.2/32",)
INGRESS_CIDRS_ENV = "PLAID_CASHFLOW_TRUSTED_INGRESS_CIDRS"
DEV_DIRECT_ENV = "PLAID_CASHFLOW_ALLOW_DEV_DIRECT"
TRANSACTIONS_API_ENV = "PLAID_CASHFLOW_ENABLE_TRANSACTIONS_API"
CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.plaid.com; "
    "connect-src 'self' https://*.plaid.com; "
    "frame-src https://*.plaid.com; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Content-Security-Policy": CSP,
}


DEFAULT_OPTIONS: dict[str, Any] = {
    "plaid_client_id": "",
    "plaid_secret": "",
    "plaid_env": "sandbox",
    "plaid_redirect_uri": "",
    "plaid_products": ["transactions"],
    "plaid_country_codes": ["US"],
    "sync_months_back": 12,
    "backfill_days": 730,
    "enable_historical_backfill": True,
    "sync_interval_minutes": 360,
    "local_db_path": "/data/plaid_cashflow.sqlite",
    "currency": "USD",
    "show_transaction_details": False,
    "debug_logging": False,
}


@dataclass(frozen=True)
class AddonConfig:
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str
    plaid_redirect_uri: str
    plaid_products: list[str]
    plaid_country_codes: list[str]
    # Dashboard display range only. It never limits what the ledger stores.
    sync_months_back: int
    # Historical backfill range, independent of the display range.
    backfill_days: int
    enable_historical_backfill: bool
    sync_interval_minutes: int
    local_db_path: str
    currency: str
    # Opt-in. When false the per-transaction API returns 404 and the dashboard
    # shows no transaction-level screen.
    show_transaction_details: bool
    debug_logging: bool

    @property
    def transaction_details_enabled(self) -> bool:
        return self.show_transaction_details or os.environ.get(TRANSACTIONS_API_ENV) == "1"

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
            backfill_days=self.backfill_days,
            redirect_uri=self.plaid_redirect_uri,
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


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _development_mode_allowed() -> bool:
    return os.environ.get(DEV_DIRECT_ENV) == "1" or not Path("/data").exists()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _validate_db_path(value: Any) -> str:
    db_path = str(value or "/data/plaid_cashflow.sqlite").strip()
    if db_path == "/data/plaid_cashflow.sqlite" and _development_mode_allowed():
        db_path = str(Path.cwd() / "data" / "plaid_cashflow.sqlite")

    path = Path(db_path)
    if not db_path or path.name in {"", ".", ".."}:
        raise RuntimeError("local_db_path must point to a SQLite file")
    if path == path.anchor:
        raise RuntimeError("local_db_path must not be a filesystem root")

    if not _development_mode_allowed():
        data_dir = Path("/data").resolve(strict=False)
        resolved = path.resolve(strict=False)
        if not _is_relative_to(resolved, data_dir):
            raise RuntimeError("local_db_path must stay under /data")
    return db_path


def _validate_redirect_uri(value: Any) -> str:
    redirect_uri = str(value or "").strip()
    if not redirect_uri:
        return ""
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("plaid_redirect_uri must be blank or an HTTPS URL")
    return redirect_uri


def _build_config(options: dict[str, Any]) -> AddonConfig:
    plaid_env = str(options.get("plaid_env") or "sandbox").lower()
    if plaid_env not in {"sandbox", "production"}:
        raise RuntimeError("plaid_env must be sandbox or production")

    products = _as_list(options.get("plaid_products"), ["transactions"])
    invalid_products = sorted(set(products) - {"transactions"})
    if invalid_products:
        raise RuntimeError("Only the Plaid transactions product is supported")

    country_codes = [code.upper() for code in _as_list(options.get("plaid_country_codes"), ["US"])]
    invalid_countries = sorted(set(country_codes) - {"US", "CA"})
    if invalid_countries:
        raise RuntimeError("Only US and CA country codes are supported")

    currency = str(options.get("currency") or "USD").upper().strip()
    if not re.fullmatch(r"[A-Z]{3,8}", currency):
        raise RuntimeError("currency must be 3 to 8 uppercase letters")

    return AddonConfig(
        plaid_client_id=str(options.get("plaid_client_id") or ""),
        plaid_secret=str(options.get("plaid_secret") or ""),
        plaid_env=plaid_env,
        plaid_redirect_uri=_validate_redirect_uri(options.get("plaid_redirect_uri")),
        plaid_products=products,
        plaid_country_codes=country_codes,
        sync_months_back=_bounded_int(
            options.get("sync_months_back") or 12,
            name="sync_months_back",
            minimum=1,
            maximum=120,
        ),
        backfill_days=_bounded_int(
            options.get("backfill_days") or 730,
            name="backfill_days",
            minimum=30,
            maximum=730,
        ),
        enable_historical_backfill=bool(options.get("enable_historical_backfill", True)),
        sync_interval_minutes=_bounded_int(
            options.get("sync_interval_minutes") or 360,
            name="sync_interval_minutes",
            minimum=15,
            maximum=1440,
        ),
        local_db_path=_validate_db_path(options.get("local_db_path")),
        currency=currency,
        show_transaction_details=bool(options.get("show_transaction_details")),
        debug_logging=bool(options.get("debug_logging")),
    )


def load_config() -> AddonConfig:
    options_path = Path(os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json"))
    options = DEFAULT_OPTIONS.copy()

    if options_path.exists():
        with options_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            options.update(loaded)

    return _build_config(options)


CONFIG = load_config()
logging.basicConfig(level=logging.DEBUG if CONFIG.debug_logging else logging.INFO)
STORAGE = Storage(CONFIG.local_db_path)
PLAID = PlaidService(CONFIG.plaid_settings())
SYNC_LOCK = asyncio.Lock()
BACKGROUND_SYNC_TASK: asyncio.Task | None = None

app = FastAPI(title="Plaid Monthly Cashflow", version=APP_VERSION)


def _trusted_ingress_networks() -> list[ipaddress._BaseNetwork]:
    configured = os.environ.get(INGRESS_CIDRS_ENV)
    cidrs = [item.strip() for item in configured.split(",")] if configured else list(DEFAULT_INGRESS_CIDRS)
    networks: list[ipaddress._BaseNetwork] = []
    for cidr in cidrs:
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            LOGGER.warning("Ignoring invalid trusted ingress CIDR: %s", redact_text(cidr))
    return networks or [ipaddress.ip_network(DEFAULT_INGRESS_CIDRS[0])]


def _request_client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _is_loopback_or_test_client(host: str) -> bool:
    if host in {"testclient", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_trusted_ingress_source(request: Request) -> bool:
    host = _request_client_host(request)
    try:
        client_ip = ipaddress.ip_address(host)
    except ValueError:
        return os.environ.get(DEV_DIRECT_ENV) == "1" and _is_loopback_or_test_client(host)

    if any(client_ip in network for network in _trusted_ingress_networks()):
        return True
    return os.environ.get(DEV_DIRECT_ENV) == "1" and client_ip.is_loopback


def _origin_or_referer_matches_host(request: Request) -> bool:
    request_host = (request.headers.get("host") or "").lower()
    for header_name in ("origin", "referer"):
        value = request.headers.get(header_name)
        if not value:
            continue
        parsed_host = urlparse(value).netloc.lower()
        if parsed_host and request_host and parsed_host != request_host:
            return False
    return True


def _state_changing_api_request(request: Request) -> bool:
    return request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/")


def _add_security_headers(response):
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


@app.middleware("http")
async def privacy_security_middleware(request: Request, call_next):
    path = request.scope.get("path", "")
    if path.startswith("//"):
        request.scope["path"] = "/" + path.lstrip("/")

    if not _is_trusted_ingress_source(request):
        return _add_security_headers(JSONResponse(status_code=403, content={"detail": "Forbidden"}))

    # Kept nested rather than collapsed: the outer test selects the requests
    # that need CSRF protection, the inner test is the protection itself.
    if _state_changing_api_request(request):  # noqa: SIM102
        if request.headers.get(MUTATION_HEADER) != "1" or not _origin_or_referer_matches_host(request):
            return _add_security_headers(JSONResponse(status_code=403, content={"detail": "Forbidden"}))

    response = await call_next(request)
    return _add_security_headers(response)


# Frontend assets are referenced by a version-stamped filename so that neither
# the browser nor the Home Assistant Ingress proxy can serve a stale bundle
# after an add-on upgrade. The stamped names are served from the single source
# file rather than duplicated at build time, so local development and the
# container behave identically. These routes are registered before the /static
# mount so they take precedence.
@app.get(f"/static/app-{APP_VERSION}.js", include_in_schema=False)
async def versioned_script() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")


@app.get(f"/static/styles-{APP_VERSION}.css", include_in_schema=False)
async def versioned_stylesheet() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup() -> None:
    STORAGE.init_db()
    STORAGE.reconcile_item_environments()
    # Keep a reference so the loop task is not garbage collected mid-flight.
    global BACKGROUND_SYNC_TASK
    BACKGROUND_SYNC_TASK = asyncio.create_task(background_sync_loop())


@app.exception_handler(PlaidNotConfiguredError)
async def plaid_not_configured_handler(_, exc: PlaidNotConfiguredError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(PlaidNotReadyError)
async def plaid_not_ready_handler(_, exc: PlaidNotReadyError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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
        if STORAGE.connected_item_count(CONFIG.plaid_env) == 0 or STORAGE.connection_requires_reset(CONFIG.plaid_env):
            continue
        try:
            await perform_sync()
        except Exception as exc:
            LOGGER.warning("Background sync failed: %s", redact_text(str(exc)))


def _http_error(exc: Exception, *, status_code: int = 500) -> HTTPException:
    return HTTPException(status_code=status_code, detail=safe_error_message(exc, debug=CONFIG.debug_logging))


def _account_metadata(access_token: str) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Fetch accounts plus the institution they belong to.

    The institution is recorded so every ledger row can be traced back to its
    source. It is never returned to the browser.
    """
    accounts = PLAID.get_accounts(access_token)
    institution_id = None
    institution_name = None
    try:
        metadata = PLAID.get_item_metadata(access_token) or {}
        institution_id = (metadata.get("item") or {}).get("institution_id")
        institution_name = PLAID.get_institution_name(institution_id)
    except Exception:  # pragma: no cover - metadata is best effort
        LOGGER.debug("Institution metadata unavailable for an item")
    return institution_id, institution_name, accounts


def _date_bounds(pages_dates: list[str]) -> tuple[str | None, str | None]:
    valid = sorted(value for value in pages_dates if value)
    return (valid[0], valid[-1]) if valid else (None, None)


def _sync_one_item(item: dict[str, Any], batch_id: str) -> dict[str, Any]:
    """Sync a single Plaid Item, committing one page at a time.

    Every page is written with :meth:`Storage.commit_sync_page`, which stores
    the page's events and advances the cursor inside one transaction. If a
    later page fails, the earlier pages stay committed and the cursor points at
    the last page that succeeded -- no rollback of already-durable history.
    """
    item_id = str(item["item_id"])
    access_token = item["access_token"]
    starting_cursor = item.get("cursor")

    sync_id = STORAGE.start_sync_run(
        batch_id=batch_id, item_id=item_id, starting_cursor=starting_cursor, mode="sync"
    )

    added = modified = removed = 0
    inserted = duplicates = pages = 0
    dates: list[str] = []
    ending_cursor = starting_cursor
    mode = "sync"

    try:
        institution_id, institution_name, accounts = _account_metadata(access_token)
        STORAGE.record_account_observations(
            item_id,
            accounts,
            institution_id=institution_id,
            institution_name=institution_name,
            plaid_env=CONFIG.plaid_env,
        )

        for page in PLAID.sync_transaction_pages(access_token=access_token, cursor=starting_cursor):
            mode = str(page.get("mode") or mode)
            page_added = page.get("added") or []
            page_modified = page.get("modified") or []
            page_removed = page.get("removed") or []

            page_inserted, page_duplicates = STORAGE.commit_sync_page(
                item_id=item_id,
                added=page_added,
                modified=page_modified,
                removed=page_removed,
                next_cursor=page.get("next_cursor"),
                # The transactions/get fallback has no cursor to advance.
                advance_cursor=mode == "sync" and bool(page.get("next_cursor")),
                batch_id=batch_id,
                plaid_env=CONFIG.plaid_env,
            )

            added += len(page_added)
            modified += len(page_modified)
            removed += len(page_removed)
            inserted += page_inserted
            duplicates += page_duplicates
            pages += 1
            dates.extend(
                str(txn.get("date"))[:10]
                for txn in list(page_added) + list(page_modified)
                if txn.get("date")
            )
            if page.get("next_cursor"):
                ending_cursor = page["next_cursor"]

        earliest, latest = _date_bounds(dates)
        STORAGE.finish_sync_run(
            sync_id,
            status="ok",
            mode=mode,
            ending_cursor=ending_cursor,
            added_count=added,
            modified_count=modified,
            removed_count=removed,
            inserted_event_count=inserted,
            duplicate_event_count=duplicates,
            page_count=pages,
            earliest_transaction_date=earliest,
            latest_transaction_date=latest,
        )
    except Exception as exc:
        earliest, latest = _date_bounds(dates)
        STORAGE.finish_sync_run(
            sync_id,
            status="error",
            mode=mode,
            ending_cursor=ending_cursor,
            added_count=added,
            modified_count=modified,
            removed_count=removed,
            inserted_event_count=inserted,
            duplicate_event_count=duplicates,
            page_count=pages,
            earliest_transaction_date=earliest,
            latest_transaction_date=latest,
            error_class=classify_error(exc),
            error_message=safe_error_message(exc, debug=CONFIG.debug_logging),
        )
        raise

    return {
        "added": added,
        "modified": modified,
        "removed": removed,
        "inserted_events": inserted,
        "duplicate_events": duplicates,
        "pages": pages,
        "mode": mode,
    }


def _backfill_one_item(item: dict[str, Any], batch_id: str) -> dict[str, Any]:
    """Run the one-time date-based historical import for an Item.

    This is deliberately separate from cursor-based syncing and never touches
    the Plaid cursor: resetting a cursor is not required to backfill and would
    risk re-delivering history the ledger already holds under a different
    identity.
    """
    item_id = str(item["item_id"])
    state = STORAGE.get_backfill_state(item_id) or {}
    if str(state.get("status") or "") == "complete":
        return {"status": "already_complete", "imported": 0}

    start_date = PLAID.backfill_start_date()
    end_date = date.today()
    STORAGE.start_backfill(item_id, start_date=start_date.isoformat(), end_date=end_date.isoformat())

    imported = 0
    duplicates = 0
    dates: list[str] = []
    try:
        for page in PLAID.historical_transaction_pages(
            access_token=item["access_token"], start_date=start_date, end_date=end_date
        ):
            batch = page.get("added") or []
            page_inserted, page_duplicates = STORAGE.append_historical_transactions(
                item_id=item_id,
                transactions=batch,
                batch_id=batch_id,
                plaid_env=CONFIG.plaid_env,
            )
            imported += page_inserted
            duplicates += page_duplicates
            dates.extend(str(txn.get("date"))[:10] for txn in batch if txn.get("date"))
    except PlaidNotReadyError as exc:
        # Retry later. Nothing already stored is removed or reset.
        STORAGE.finish_backfill(
            item_id, status="pending", transaction_count=imported, last_error=classify_error(exc)
        )
        return {"status": "pending", "imported": imported, "duplicates": duplicates}
    except Exception as exc:
        STORAGE.finish_backfill(
            item_id, status="error", transaction_count=imported, last_error=classify_error(exc)
        )
        raise

    earliest, latest = _date_bounds(dates)
    STORAGE.finish_backfill(
        item_id,
        status="complete",
        transaction_count=imported,
        earliest=earliest,
        latest=latest,
    )
    return {
        "status": "complete",
        "imported": imported,
        "duplicates": duplicates,
        "earliest_transaction_date": earliest,
        "latest_transaction_date": latest,
    }


async def perform_sync(*, include_backfill: bool | None = None) -> dict[str, Any]:
    """Sync every active Item. Only one sync may run at a time."""
    if SYNC_LOCK.locked():
        raise HTTPException(status_code=409, detail="A sync is already running.")

    async with SYNC_LOCK:
        if STORAGE.connection_requires_reset(CONFIG.plaid_env):
            raise HTTPException(status_code=409, detail=_environment_reset_message())

        run_backfill = CONFIG.enable_historical_backfill if include_backfill is None else include_backfill
        # The Plaid SDK and sqlite3 are both blocking. Running the whole sync
        # inline would pin the event loop for the duration, which on a
        # first-run backfill of several Items is minutes -- during which the
        # dashboard, /api/health and /api/diagnostics all stop responding and
        # the add-on looks dead. Hand the blocking work to a worker thread so
        # the server keeps serving while a long sync runs.
        return await asyncio.to_thread(_run_sync_blocking, run_backfill)


def _run_sync_blocking(run_backfill: bool) -> dict[str, Any]:
    """The synchronous body of a sync run. Executed off the event loop."""
    batch_id = STORAGE.new_batch_id()
    totals = {"added": 0, "modified": 0, "removed": 0, "inserted_events": 0, "duplicate_events": 0}
    backfilled = 0

    for item in STORAGE.get_items(include_tokens=True):
        if run_backfill:
            try:
                result = _backfill_one_item(item, batch_id)
                backfilled += int(result.get("imported") or 0)
            except Exception as exc:
                # A failed backfill must not abort ongoing syncing.
                LOGGER.warning("Historical backfill deferred: %s", redact_text(classify_error(exc)))
        outcome = _sync_one_item(item, batch_id)
        totals["added"] += outcome["added"]
        totals["modified"] += outcome["modified"]
        totals["removed"] += outcome["removed"]
        totals["inserted_events"] += outcome["inserted_events"]
        totals["duplicate_events"] += outcome["duplicate_events"]

    return {
        "ok": True,
        "new_transactions": totals["added"],
        "modified_transactions": totals["modified"],
        "removed_transactions": totals["removed"],
        "inserted_events": totals["inserted_events"] + backfilled,
        "duplicate_events": totals["duplicate_events"],
        "backfilled_events": backfilled,
        "total_transactions": STORAGE.transaction_count(),
        "total_transaction_events": STORAGE.event_count(),
        "last_sync_at": STORAGE.last_sync_at(),
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/v018")
@app.get("/v018/")
@app.get(INGRESS_ENTRY.rstrip("/"))
@app.get(INGRESS_ENTRY)
async def versioned_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    connection_environment = STORAGE.connection_environment()
    connection_requires_reset = STORAGE.connection_requires_reset(CONFIG.plaid_env)
    return {
        "ok": True,
        "configured": CONFIG.configured,
        "app_version": APP_VERSION,
        "plaid_env": CONFIG.plaid_env,
        "connected_items": STORAGE.connected_item_count(CONFIG.plaid_env),
        "transaction_count": 0 if connection_requires_reset else STORAGE.transaction_count(),
        "transaction_event_count": STORAGE.event_count(),
        "last_sync_at": STORAGE.last_sync_at(),
        "connection_environment": connection_environment,
        "connection_requires_reset": connection_requires_reset,
        "transaction_details_enabled": CONFIG.transaction_details_enabled,
    }


def _collect_diagnostics() -> dict[str, Any]:
    return {
        "ok": True,
        "app_version": APP_VERSION,
        "aggregates": STORAGE.aggregate_diagnostics(),
        "last_sync": STORAGE.last_sync_summary(),
        "backfill_complete": STORAGE.backfill_complete(),
        "integrity": STORAGE.integrity_report(),
        "hash_chain": STORAGE.verify_ledger_hash_chain(),
        "append_only": True,
    }


@app.get("/api/diagnostics")
async def diagnostics() -> dict[str, Any]:
    """Aggregate-only diagnostics.

    Deliberately returns no transaction names, amounts, dates beyond the
    overall range, account identifiers, raw JSON, tokens, or secrets.
    """
    # Diagnostics runs a dozen aggregate queries plus a hash-chain walk. Those
    # are blocking SQLite calls, so they go to a worker thread rather than
    # stalling the event loop for every other request.
    payload = await asyncio.to_thread(_collect_diagnostics)
    # Belt and braces: the response is scrubbed even though nothing sensitive
    # is selected, so a future field cannot leak by accident.
    return scrub(payload)


def _environment_reset_message() -> str:
    linked_environment = STORAGE.connection_environment() or "another environment"
    return (
        f"The saved Plaid connection belongs to {linked_environment}, but the add-on is configured for "
        f"{CONFIG.plaid_env}. Disconnect the Plaid connection, then reconnect with Plaid. "
        "Stored transaction history is always preserved."
    )


@app.post("/api/link-token")
async def link_token() -> dict[str, str]:
    if not CONFIG.configured:
        raise HTTPException(
            status_code=400,
            detail="Add your Plaid Client ID, Secret, and environment in the Home Assistant add-on Configuration tab, save, and restart the add-on.",
        )
    if STORAGE.connection_requires_reset(CONFIG.plaid_env):
        raise HTTPException(status_code=409, detail=_environment_reset_message())
    return {"link_token": PLAID.create_link_token()}


@app.post("/api/exchange-public-token")
async def exchange_public_token(payload: PublicTokenRequest) -> dict[str, Any]:
    if not CONFIG.configured:
        raise HTTPException(status_code=400, detail="Plaid is not configured.")

    try:
        exchange = PLAID.exchange_public_token(payload.public_token)
        access_token = str(exchange["access_token"])
        item_id = str(exchange["item_id"])
        institution_id, institution_name, accounts = _account_metadata(access_token)
        STORAGE.save_item(
            item_id=item_id,
            access_token=access_token,
            plaid_env=CONFIG.plaid_env,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        STORAGE.record_account_observations(
            item_id,
            accounts,
            institution_id=institution_id,
            institution_name=institution_name,
            plaid_env=CONFIG.plaid_env,
        )
        sync_result = await perform_sync()
        return scrub({"ok": True, "sync": sync_result})
    except Exception as exc:
        raise _http_error(exc, status_code=502) from exc


@app.post("/api/sync")
async def sync_now() -> dict[str, Any]:
    if not CONFIG.configured:
        raise HTTPException(status_code=400, detail="Plaid is not configured.")
    if STORAGE.connection_requires_reset(CONFIG.plaid_env):
        raise HTTPException(status_code=409, detail=_environment_reset_message())
    if STORAGE.connected_item_count(CONFIG.plaid_env) == 0:
        return {
            "ok": True,
            "new_transactions": 0,
            "modified_transactions": 0,
            "removed_transactions": 0,
            "inserted_events": 0,
            "duplicate_events": 0,
            "backfilled_events": 0,
            # Even with no active connection the ledger keeps its history.
            "total_transactions": STORAGE.transaction_count(),
            "total_transaction_events": STORAGE.event_count(),
            "last_sync_at": STORAGE.last_sync_at(),
        }
    try:
        return await perform_sync()
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc, status_code=502) from exc


@app.get("/api/accounts")
async def accounts() -> dict[str, int]:
    return {"count": STORAGE.account_count()}


def _require_transaction_details() -> None:
    """Gate the per-transaction screens behind an explicit opt-in.

    Off by default. When off the endpoints return 404 rather than 403, so an
    add-on that has not opted in does not advertise that the data exists.
    """
    if not CONFIG.transaction_details_enabled:
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/api/transactions")
async def transactions(
    months_back: int | None = Query(default=None, ge=1, le=120),
    limit: int | None = Query(default=200, ge=1, le=2000),
    account_id: str | None = Query(default=None, max_length=128),
    search: str | None = Query(default=None, max_length=128),
    include_removed: bool = Query(default=False),
) -> dict[str, Any]:
    """Full detail of each transaction, for the owner's own inspection.

    Reachable only through Home Assistant Ingress, only when
    ``show_transaction_details`` is enabled. Returns the complete stored
    financial record; credential-shaped fields were stripped before the data
    was ever written, so there is nothing here to redact.
    """
    _require_transaction_details()
    rows = await asyncio.to_thread(
        STORAGE.list_transaction_details,
        months_back=months_back,
        limit=limit,
        account_id=account_id,
        search=search,
        include_removed=include_removed,
    )
    return {"currency": CONFIG.currency, "count": len(rows), "transactions": rows}


@app.get("/api/transactions/{transaction_id}/versions")
async def transaction_versions(transaction_id: str) -> dict[str, Any]:
    """Every stored version of one transaction, oldest first.

    This is what append-only buys you: the amount, date, merchant and pending
    state Plaid reported at each point, not just the latest.
    """
    _require_transaction_details()
    if not transaction_id or len(transaction_id) > 128:
        raise HTTPException(status_code=400, detail="Invalid transaction id")
    versions = await asyncio.to_thread(STORAGE.transaction_versions, transaction_id)
    if not versions:
        raise HTTPException(status_code=404, detail="Not found")
    return {"transaction_id": transaction_id, "version_count": len(versions), "versions": versions}


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
    """Stop syncing and forget Plaid access tokens. History is preserved.

    This endpoint used to call ``delete_all_plaid_data()``, which deleted every
    transaction, account, sync-log and settings row, removed the SQLite file
    and its WAL/SHM sidecars, deleted the local encryption key, and recreated
    an empty database. All of that is gone.

    What remains is a credential-only disconnect: Plaid is asked to deactivate
    the Item, the encrypted access token is cleared, and the item is marked
    inactive so background syncing stops. Not one row of financial history is
    touched. There is no production code path that deletes financial history.
    """
    disconnected = 0
    for item in STORAGE.get_items(include_tokens=True):
        token = item.get("access_token")
        if token:
            PLAID.remove_item(token)
        if STORAGE.deactivate_item(str(item["item_id"]), clear_token=True):
            disconnected += 1

    return {
        "ok": True,
        "disconnected_items": disconnected,
        "financial_history_preserved": True,
        "transaction_event_count": STORAGE.event_count(),
        "detail": (
            "Plaid syncing stopped and stored access tokens were cleared. "
            "All transaction history remains in the local database."
        ),
    }
