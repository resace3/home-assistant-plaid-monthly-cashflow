from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

# Credential-bearing keys that must never reach SQLite, logs, or API responses.
#
# This set is deliberately much narrower than SENSITIVE_FIELD_NAMES below.
# SENSITIVE_FIELD_NAMES governs what leaves the add-on over HTTP; this set
# governs what we refuse to *store*. Ordinary financial fields (amount, date,
# merchant, account_id, transaction_id, mask, ...) are intentionally absent:
# the append-only ledger exists precisely to retain them.
CREDENTIAL_FIELD_NAMES = {
    "access_token",
    "access_token_encrypted",
    "api_key",
    "authorization",
    "client_secret",
    "link_token",
    "local_key",
    "password",
    "plaid_secret",
    "processor_token",
    "public_token",
    "refresh_token",
    "secret",
}

SENSITIVE_FIELD_NAMES = {
    "account_id",
    "account_number",
    "access_token",
    "access_token_encrypted",
    "api_key",
    "authorization",
    "client_secret",
    "cursor",
    "item_id",
    "link_token",
    "local_key",
    "mask",
    "password",
    "plaid_secret",
    "public_token",
    "routing_number",
    "secret",
    "transaction_id",
}

TOKEN_PATTERNS = [
    re.compile(r"\b(?:access|public|secret)-(?:sandbox|development|production)-[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:access|public|secret)-[A-Za-z0-9_-]{6,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{43}=\b"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _normalized_field_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if _normalized_field_name(key) in SENSITIVE_FIELD_NAMES:
                safe[key] = "[redacted]"
            else:
                safe[key] = scrub(item)
        return safe
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def strip_secrets(value: Any) -> Any:
    """Remove credential fields from a payload while keeping financial fields.

    Used on the raw Plaid transaction/account objects before they are written to
    the append-only ledger. Plaid does not normally echo credentials inside a
    transaction, but if a future API version ever did we must not persist it.

    Unlike :func:`scrub`, this does **not** touch string values: running the
    token regexes over a merchant name or a transaction id would destroy the
    financial history the ledger is supposed to preserve.
    """
    if isinstance(value, dict):
        return {
            key: strip_secrets(item)
            for key, item in value.items()
            if _normalized_field_name(key) not in CREDENTIAL_FIELD_NAMES
        }
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


def fingerprint(value: str | None, *, length: int = 16) -> str | None:
    """Return a short, non-reversible fingerprint of an opaque value.

    Plaid sync cursors are opaque credentials-adjacent strings. The audit log
    needs to show that the cursor *changed* without ever recording the cursor
    itself, so it stores a truncated SHA-256 instead.
    """
    if value is None or value == "":
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def classify_error(exc: BaseException) -> str:
    """Map an exception onto a coarse, non-sensitive error class for auditing."""
    message = str(exc).lower()
    body = getattr(exc, "body", None)
    if body:
        try:
            parsed = json.loads(body)
            code = parsed.get("error_code")
            if code:
                return str(code)[:64]
        except (TypeError, ValueError):
            pass
    if "product_not_ready" in message or "not ready" in message:
        return "PRODUCT_NOT_READY"
    if "rate_limit" in message or "too many requests" in message:
        return "RATE_LIMIT"
    if "timeout" in message or "timed out" in message:
        return "TIMEOUT"
    if "connection" in message or "network" in message:
        return "NETWORK"
    if "database is locked" in message or "database locked" in message:
        return "DATABASE_LOCKED"
    return type(exc).__name__[:64]


def safe_error_message(exc: Exception, *, debug: bool = False) -> str:
    message = str(exc)
    body = getattr(exc, "body", None)
    if body:
        try:
            parsed = json.loads(body)
            error_code = parsed.get("error_code")
            error_message = parsed.get("error_message") or parsed.get("display_message")
            if error_code and error_message:
                message = f"{error_code}: {error_message}"
            elif error_message:
                message = str(error_message)
        except (TypeError, ValueError):
            message = str(body)

    message = redact_text(message)
    if debug:
        return message

    lowered = message.lower()
    if "invalid_api_keys" in lowered or "invalid api" in lowered:
        return "Plaid rejected the configured keys. Check that the client ID, secret, and environment match."
    if "product_not_ready" in lowered or "transactions not ready" in lowered:
        return "Plaid transactions are not ready yet. Wait a few minutes and sync again."
    if "item_login_required" in lowered:
        return "Plaid says this item needs to be reconnected."
    if "rate_limit" in lowered or "too many requests" in lowered:
        return "Plaid rate limit reached. Wait and try syncing again."
    if "connection" in lowered or "timeout" in lowered or "network" in lowered:
        return "Network error while contacting Plaid. Try again shortly."
    return message


def get_fernet(key_path: str | Path) -> Fernet:
    path = Path(key_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        key = path.read_bytes()
    else:
        key = Fernet.generate_key()
        path.write_bytes(key)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)

    return Fernet(key)


def encrypt_text(value: str, key_path: str | Path) -> str:
    return get_fernet(key_path).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(value: str, key_path: str | Path) -> str:
    return get_fernet(key_path).decrypt(value.encode("utf-8")).decode("utf-8")
