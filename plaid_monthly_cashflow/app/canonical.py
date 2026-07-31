"""Deterministic JSON canonicalisation and hashing for the append-only ledger.

Every ledger row carries a ``payload_hash``. That hash is the *only* thing the
ingest path uses to decide whether Plaid sent us something genuinely new, so it
has to be stable across processes, restarts, Python versions and key ordering.

Canonicalisation rules:

* Mapping keys are sorted, so ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` hash the
  same. Plaid's client library does not guarantee key order.
* ``date`` / ``datetime`` / ``Decimal`` are normalised to strings and floats so
  that an object decoded by plaid-python hashes identically to the same object
  decoded from stored JSON text.
* Floats that hold an exact integer value are *not* collapsed to ints; SQLite
  and JSON both round-trip them, and collapsing would make ``1.0`` and ``1``
  hash alike for an amount, which is a change we would rather see.
* ``-0.0`` is normalised to ``0.0`` because JSON round-tripping is not reliable
  about the sign of zero.
* Separators are compact and ``ensure_ascii`` is off, so Unicode merchant names
  hash by their code points rather than by escape sequence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def canonicalize(value: Any) -> Any:
    """Recursively convert ``value`` into JSON-safe, comparison-stable data."""
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, bool):
        # bool must precede int: bool is a subclass of int.
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int)):
        return value
    # plaid-python model objects and anything else exotic degrade to their
    # string form rather than blowing up an otherwise good sync.
    if hasattr(value, "to_dict"):
        return canonicalize(value.to_dict())
    return str(value)


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text used for storage and hashing."""
    return json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON form of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_and_hash(value: Any) -> tuple[str, str]:
    """Return ``(canonical_json, sha256)`` in one pass.

    The ingest path needs both for every transaction; canonicalising twice
    doubles the CPU cost of a large historical backfill for no benefit.
    """
    text = canonical_json(value)
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_text(*parts: Any) -> str:
    """SHA-256 over a ``|``-joined tuple of parts, used for identity keys."""
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
