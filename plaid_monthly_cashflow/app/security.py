from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


SENSITIVE_FIELD_NAMES = {
    "access_token",
    "access_token_encrypted",
    "secret",
    "plaid_secret",
    "public_token",
    "client_secret",
    "api_key",
}

TOKEN_PATTERNS = [
    re.compile(r"access-[A-Za-z0-9_-]+"),
    re.compile(r"public-[A-Za-z0-9_-]+"),
    re.compile(r"secret-[A-Za-z0-9_-]+"),
]


def redact_text(value: str) -> str:
    redacted = value
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_FIELD_NAMES:
                safe[key] = "[redacted]"
            else:
                safe[key] = scrub(item)
        return safe
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


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
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    return Fernet(key)


def encrypt_text(value: str, key_path: str | Path) -> str:
    return get_fernet(key_path).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(value: str, key_path: str | Path) -> str:
    return get_fernet(key_path).decrypt(value.encode("utf-8")).decode("utf-8")
