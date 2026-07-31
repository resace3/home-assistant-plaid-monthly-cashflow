# Plaid Monthly Cashflow

## Setup

1. Open the add-on Configuration tab.
2. Enter your Plaid Client ID.
3. Enter the Plaid secret for the selected environment.
4. Choose the matching `plaid_env`.
5. Save and restart the add-on.
6. Open the web UI.
7. Connect through Plaid Link.

The add-on shows a Not configured state until Plaid credentials are saved and the add-on is restarted.

If you switch between Sandbox and Production after connecting, reconnect through Plaid Link in the
configured environment. Plaid access tokens cannot move between environments. **Reconnecting never
deletes stored transaction history** — the local ledger keeps everything it has already recorded.

## Append-only transaction storage

Since version 0.2.0 every transaction Plaid reports is written to an **append-only ledger** in
`transaction_events`. The add-on has no production code path that deletes financial history.

### What append-only means here

| Plaid event | What the add-on does |
| --- | --- |
| `added` | Inserts a new ledger event. |
| `modified` | Inserts a **new** ledger event. The previous version stays queryable. |
| `removed` | Inserts a `removed` event. The transaction and every earlier version stay in the database; they are only excluded from current totals. |
| pending becomes posted | Both rows are kept. The posted row records `pending_transaction_id`, and the pending row is flagged superseded so the pair is counted once. |
| historical backfill | Inserts `historical_import` events for history retrieved by date. |
| pre-0.2.0 rows | Imported once as `legacy_import` events. The old `transactions` table is never dropped. |

The application performs no `DELETE FROM`, `DROP TABLE`, `TRUNCATE`, or `VACUUM`, and never deletes
the SQLite file or its WAL/SHM sidecars. Ordinary `UPDATE` is used only for operational metadata that
is not financial history:

- `items` — the Plaid sync cursor, the environment, the active flag, and the encrypted access token
  when you explicitly disconnect.
- `sync_runs` — the completion status of the sync that is currently running.
- `backfill_state` — backfill bookkeeping.

A test suite (`tests/test_no_destructive_operations.py`) statically audits the shipped source on every
CI run and fails if any of those guarantees regress.

### Limits of the guarantee

The guarantee is about **this application's behaviour**, not about the storage underneath it.

- Anyone with filesystem or SSH access to Home Assistant can open `/data/plaid_cashflow.sqlite` with
  any SQLite tool and modify or delete rows. The add-on cannot prevent that.
- Uninstalling the add-on removes `/data`, including the database. Uninstalling is a Supervisor
  action, not something the add-on controls.
- Restoring an older Home Assistant backup replaces the database with the older copy.
- Disk failure, corruption, and flash wear are not addressed by append-only storage.

**This is not forensic immutability.** The optional hash chain below provides tamper *evidence*, not
tamper *proofing*.

### Historical ledger vs latest transaction state

Two different things, deliberately kept separate:

- **Ledger events** (`transaction_events`) — the complete, immutable history. One row per state
  Plaid has ever reported. A transaction that was added, modified twice, then removed has four rows.
- **Latest state** (`transaction_latest_state`, `transaction_current_state`,
  `transaction_active_state`) — SQL **views** derived from the ledger. They pick the newest event per
  transaction. Nothing is cached and nothing is mutable; dropping and recreating a view touches no data.

The dashboard totals read `transaction_active_state`, which excludes removed transactions and pending
rows superseded by their posted counterpart. So a transaction with several historical versions is
counted exactly once, at its latest amount.

### Deduplication

An incoming event is ignored only when it repeats the transaction's **current newest** event of the
same class with a byte-identical canonical payload. Concretely:

- Exact retry of a sync (or a crash-then-retry) → no new row.
- A genuine change from Plaid → new row, because the payload hash changed.
- A revert back to an older payload → new row, because it supersedes a different event.
- A backfill that re-delivers a transaction already synced with identical content → no new row.

JSON is canonicalised (sorted keys, normalised dates and numbers) before hashing, so a difference in
key ordering never looks like a change. The deduplication key is
`sha256(item_id | transaction_id | event_class | payload_hash | supersedes_event_id)`, enforced by a
`UNIQUE` index and applied with `ON CONFLICT DO NOTHING` — never `DO UPDATE`. The ingestion timestamp
is not part of the key.

### Backfill, sync, and display are three separate ranges

| Concept | Option | Default | Effect |
| --- | --- | --- | --- |
| Historical backfill range | `backfill_days` | 730 | How far back the one-time date-based import asks Plaid to go, and the `days_requested` sent at Link time. |
| Ongoing synchronisation | (none) | — | `transactions/sync` with a stored cursor. Incremental and open-ended. |
| Dashboard display range | `sync_months_back` | 12 | How many months the charts and tables show. **Purely a display filter** — it never limits what is stored. |

Plaid does not offer unlimited history. An Item only exposes as much as it was linked for, so the
backfill stores whatever the API actually returns and reports the earliest and latest dates retrieved
on the Diagnostics screen. If Plaid says transactions are not ready yet, the backfill retries later
and never deletes or resets anything in the meantime. The existing sync cursor is never reset.

## Diagnostics

The **Diagnostics** button on the dashboard shows aggregate counters only: total ledger events,
distinct transaction ids, active/pending/removed counts, linked accounts, the overall date range, last
sync results, schema version, backfill status, and integrity checks. It never shows transaction names,
amounts, account identifiers, raw payloads, tokens, or secrets.

The same data is available at `GET /api/diagnostics` through Ingress.

## Transaction detail (opt-in)

Set `show_transaction_details: true` in the add-on Configuration tab to add a **Transactions** button
to the dashboard. It is **off by default**.

When enabled you get, for every transaction:

- date, authorized date, and datetimes
- name, merchant name, and the original bank description
- amount, currency, and direction
- pending / posted / removed / superseded status
- payment channel, transaction type and code, check number
- category, personal finance category, counterparties, location, payment metadata, website
- which account and institution it came from, shown as a name plus Plaid's masked suffix

Selecting a row shows **every stored version** of that transaction — the amount, date, merchant and
pending state Plaid reported at each point, not just the latest. This is the append-only ledger made
visible.

The list can be searched, limited to a month range, and optionally include removed transactions.

### What enabling it does and does not change

It does **not** change what is stored, only what the add-on will return over HTTP. Specifically:

- The endpoints stay behind Home Assistant Ingress; the same source check, CSRF header requirement,
  and security headers apply. Nothing becomes reachable from outside Home Assistant.
- Credential-shaped fields were stripped before the data was ever written, so there is nothing
  sensitive left to redact in the response.
- Full account numbers are still never stored or shown. Only Plaid's masked suffix.
- Search is parameterised; the query text never reaches the SQL statement.
- Result sizes are bounded (max 2000 rows per request).
- Turning the option back off returns the endpoints to `404`.

The trade-off is real and worth stating: with this on, anyone who can open your Home Assistant
dashboard can read your full transaction history. Leave it off if other people have access to your
Home Assistant account.

`PLAID_CASHFLOW_ENABLE_TRANSACTIONS_API=1` still works as an environment override for local
development.

## Backups

Append-only storage protects you from the *application* losing data. It does not protect you from
losing the file. Back up regularly:

1. Use Home Assistant's built-in backups (Settings → System → Backups). Add-on backups include `/data`,
   so they contain the SQLite database **and** `local_key.key`.
2. Because a backup contains both the database and the key that decrypts your Plaid access tokens,
   treat backups as sensitive. Store them somewhere you would be comfortable storing bank statements.
3. Keep more than one generation. A backup taken after an accidental uninstall is not useful.
4. Do not upload backups, database files, or `local_key.key` to GitHub issues, forums, or AI chats.

## Optional tamper evidence

Each ledger row stores `prev_ledger_hash`, `payload_hash`, and `ledger_hash`, forming a hash chain over
the ledger. The Diagnostics screen verifies the chain and reports breaks.

This detects a later out-of-band edit made by someone who did not also recompute the chain. It does
**not** prevent tampering: an administrator with write access to the database can rewrite both the
rows and the chain. Rows imported from the pre-0.2.0 schema are not chained, because their original
content cannot be reconstructed.

## Security

Plaid secrets are read from Home Assistant add-on options and are not sent to the browser. Plaid access
tokens are encrypted locally and stored in the add-on data directory.

Use this add-on through Home Assistant Ingress only. Do not expose the add-on port directly to the
internet or to an untrusted LAN.

The local encryption key is stored beside the SQLite database. Anyone with both the database and
`local_key.key` can decrypt local Plaid access tokens.

The dashboard loads Plaid Link from Plaid. It does not load non-Plaid third-party dashboard scripts.

Credential-shaped fields (`access_token`, `secret`, `client_secret`, `public_token`, `api_key`, …) are
stripped from any payload before it is written to SQLite. Ordinary financial fields are kept in full.
Plaid sync cursors appear in the audit log only as a short SHA-256 fingerprint, never in cleartext.

### Disconnecting

**Disconnect stops syncing. It does not delete anything.**

The dashboard no longer offers a control that erases local financial history; the old
"Disconnect and delete local data" button has been removed. `DELETE /api/disconnect` now:

- asks Plaid to deactivate the Item,
- clears the stored encrypted access token,
- marks the item inactive so background syncing stops.

It does not touch transaction events, account observations, institution metadata, sync history, audit
history, the encryption key, or the database files.

Do not paste logs, screenshots, database files, keys, or Home Assistant backups into GitHub issues or
AI chats if they may contain secrets or financial data.
