"""Single source of truth for the add-on and database schema versions.

``APP_VERSION`` must stay in sync with ``config.yaml``. A test asserts this so a
version bump cannot silently drift from the Supervisor manifest.
"""

from __future__ import annotations

APP_VERSION = "0.2.0"

# Bumped whenever a new migration is appended to app.schema.MIGRATIONS.
SCHEMA_VERSION = 2

# The ingress entry path in config.yaml is derived from APP_VERSION so that
# Home Assistant cannot serve a cached copy of an older dashboard bundle.
INGRESS_ENTRY = "/v" + APP_VERSION.replace(".", "") + "/"
