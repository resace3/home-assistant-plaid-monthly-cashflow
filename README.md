# Home Assistant Plaid Monthly Cashflow

A local Home Assistant add-on that connects to Plaid and visualizes monthly inflow, outflow, and net cashflow.

This repository is designed to be added to Home Assistant OS / Supervisor as a public, secret-free add-on repository. Plaid credentials are entered only in the Home Assistant add-on Configuration panel and are never committed to GitHub.

## What it does

- Runs as a Supervisor add-on with Home Assistant Ingress.
- Lets you connect accounts through Plaid Link.
- Pulls Plaid transactions with cursor-based `transactions/sync`, committing each page atomically with its cursor.
- Stores every transaction Plaid reports in an append-only ledger that is never deleted, overwritten, or truncated.
- Performs a one-time date-based historical backfill so the ledger is not limited to the dashboard's display range.
- Stores Plaid access tokens locally in the add-on data directory.
- Encrypts access tokens with a locally generated Fernet key stored outside this repository.
- Calculates monthly inflow, outflow, and net cashflow.
- Renders a light, readable dashboard inside Home Assistant.
- Supports Plaid Sandbox first and Plaid Production when you change the add-on configuration.

## Screenshots placeholder

Screenshots can be added after installing the add-on and opening the Ingress dashboard:

- Not configured state
- Connected dashboard
- Monthly cashflow chart
- Mobile layout

## Security model

- Do not commit Plaid secrets.
- Do not paste Plaid secrets into GitHub issues, logs, or Codex chat.
- Enter `plaid_client_id`, `plaid_secret`, and `plaid_env` only in the Home Assistant add-on Configuration tab.
- `plaid_secret` uses Home Assistant's `password` schema so the Supervisor UI masks it.
- Plaid secrets stay server-side in `/data/options.json` and are never returned to the browser.
- Plaid access tokens are encrypted before they are written to SQLite.
- The encryption key is generated locally at runtime as `local_key.key` next to the configured SQLite database.
- Anyone with both the SQLite database and `local_key.key` can decrypt local Plaid access tokens.
- The add-on is intended to be accessed only through Home Assistant Ingress.
- Direct add-on port exposure is not recommended and should never be internet-facing or exposed on an untrusted LAN.
- The dashboard never displays Plaid secrets, Plaid access tokens, public tokens, or full account numbers.
- The dashboard loads Plaid Link from Plaid. It does not load non-Plaid third-party dashboard scripts.
- Credential-shaped fields are stripped from Plaid payloads before they are stored. Ordinary financial fields are kept in full, because the ledger exists to retain them.
- Plaid sync cursors appear in the audit log only as a short SHA-256 fingerprint, never in cleartext.
- The diagnostics screen and API return aggregate counters only, never transaction details.
- Per-transaction detail is opt-in (`show_transaction_details`, default off) and stays behind Ingress. Enabling it changes what the add-on returns over HTTP, not what it stores.
- Full account numbers are never stored. The masked suffix Plaid returns is retained as account metadata.
- Do not paste logs, screenshots, database files, keys, or Home Assistant backups into GitHub issues or AI chats if they may contain secrets or financial data.

## Plaid setup

Create a Plaid developer account and obtain the client ID and the secret for the environment you want to use.

Use Sandbox for first setup. Sandbox uses fake institutions and fake transaction data. Production uses real bank data and should only be enabled after you are comfortable with the local storage and disconnect behavior.

Make sure `plaid_env` matches the secret you paste. Use the Sandbox secret with `sandbox` and the Production secret with `production`.

Plaid access tokens are tied to the environment in which they were created. If you switch between Sandbox and Production, the dashboard hides the old totals until you reconnect through Plaid Link in the configured environment. Changing only the secret does not convert an existing connection. Reconnecting never deletes stored transaction history.

Required Plaid product:

- `transactions`

Supported country codes in this add-on:

- `US`
- `CA`

## Sandbox setup

1. Open the add-on Configuration tab in Home Assistant.
2. Set `plaid_env` to `sandbox`.
3. Paste the Plaid client ID for Sandbox into `plaid_client_id`.
4. Paste the Plaid secret for Sandbox into `plaid_secret`.
5. Save the configuration.
6. Restart the add-on.
7. Open the web UI.
8. Click `Connect with Plaid`.
9. Use Plaid's Sandbox test institution and credentials.

## Production setup

1. Confirm the Sandbox flow works first.
2. Open the add-on Configuration tab.
3. Set `plaid_env` to `production`.
4. Replace the Sandbox credential with the Plaid secret for Production.
5. Save the configuration.
6. Restart the add-on.
7. Connect through Plaid Link yourself.

Some Production institutions use OAuth. If Plaid reports that a redirect URI is required, add an HTTPS redirect URI in your Plaid dashboard and enter the same value in `plaid_redirect_uri`, then save and restart the add-on.

Production connects to real financial institutions and real bank data. This add-on is for personal visualization only and does not provide financial advice.

## Home Assistant installation

1. In Home Assistant, go to Settings > Add-ons > Add-on Store.
2. Click the three-dot menu.
3. Choose Repositories.
4. Add the GitHub repo URL:

   `https://github.com/resace3/home-assistant-plaid-monthly-cashflow`

5. Find `Plaid Monthly Cashflow`.
6. Install the add-on.
7. Open Configuration.
8. Add Plaid client ID, Plaid secret, and environment.
9. Save.
10. Start the add-on.
11. Toggle `Show in sidebar`.
12. Open the web UI.
13. Connect with Plaid.

## Configuration options

| Option | Default | Description |
| --- | --- | --- |
| `plaid_client_id` | `""` | Plaid client ID from your Plaid dashboard. |
| `plaid_secret` | `""` | Plaid environment secret. Masked by Home Assistant. |
| `plaid_env` | `sandbox` | `sandbox` or `production`. |
| `plaid_redirect_uri` | `""` | Optional Plaid OAuth redirect URI for Production institutions that require OAuth. Leave blank for Sandbox and non-OAuth flows. |
| `plaid_products` | `["transactions"]` | Plaid products requested for Link. |
| `plaid_country_codes` | `["US"]` | Plaid country codes. |
| `sync_months_back` | `12` | Dashboard display range in months. A display filter only; it never limits what is stored. |
| `backfill_days` | `730` | Historical backfill range in days, and the `days_requested` value sent to Plaid Link. Plaid caps this at 730. |
| `enable_historical_backfill` | `true` | Run the one-time date-based historical import for each connected Item. |
| `show_transaction_details` | `false` | Adds a Transactions screen showing full per-transaction detail and every stored version. Off by default; see DOCS.md for the trade-off. |
| `sync_interval_minutes` | `360` | Background sync interval while the add-on is running. |
| `local_db_path` | `/data/plaid_cashflow.sqlite` | SQLite database path inside the add-on data directory. |
| `currency` | `USD` | Display currency for dashboard totals. |
| `debug_logging` | `false` | Enables detailed server logs without printing secrets or access tokens. |

`local_db_path` is restricted to `/data` in the add-on runtime. Local development outside Home Assistant can use a workspace data path when `/data` is not present.

## How monthly inflow/outflow is calculated

Plaid transaction amounts are usually positive for outflows and negative for inflows.

- `amount > 0` is outflow.
- `amount < 0` is inflow.
- `amount == 0` is neutral.
- Monthly inflow is the sum of absolute values for negative amounts.
- Monthly outflow is the sum of positive amounts.
- Net cashflow is `inflow - outflow`.
- Pending and removed transactions are excluded from monthly calculations. Both remain stored permanently.
- A transaction with several historical versions is counted once, at its latest version.
- A pending transaction that has since posted is not counted alongside its posted counterpart.
- Missing months are filled with zero values so charts do not skip gaps.

## Local storage

The default SQLite database path is:

`/data/plaid_cashflow.sqlite`

Append-only financial history:

- `transaction_events` - the immutable ledger. One row per transaction state Plaid has ever reported.
- `account_observations` - append-only account and institution metadata, including balances over time.

Derived views (no stored data, rebuilt on start):

- `transaction_latest_state` - newest event per transaction.
- `transaction_current_state` - adds pending-to-posted linkage.
- `transaction_active_state` - what the dashboard totals: not removed, not superseded.
- `account_latest_state` - newest observation per account.

Operational tables:

- `settings`, `items`, `sync_runs`, `backfill_state`, `schema_migrations`

Preserved legacy tables (retained, never dropped, no longer written):

- `transactions`, `accounts`, `sync_log`

Plaid access tokens are encrypted with a local Fernet key stored beside the database as
`local_key.key`. That key is generated at runtime and must not be committed.

This encryption protects against copying only the SQLite database. It does not protect tokens if
someone has both the database and `local_key.key`, or if an old Home Assistant backup contains both
files.

## Append-only guarantees

Every transaction Plaid reports is preserved permanently:

- Modified transactions append a new version; the earlier version stays queryable.
- Removed transactions append a `removed` event; the transaction and all earlier versions stay stored.
- Pending transactions stay stored after they post, linked through `pending_transaction_id`.
- The complete Plaid payload is kept as canonical JSON, minus credential-shaped fields.
- Exact retries are deduplicated by a canonical payload hash, so replaying a sync creates no duplicates.

The application contains no `DELETE FROM`, `DROP TABLE`, `TRUNCATE`, or `VACUUM` against any table, and
never deletes database files. A static audit enforces this on every CI run.

Ordinary `UPDATE` is used only for operational metadata that is not financial history: the Plaid sync
cursor, the completion status of a running sync, backfill bookkeeping, and the encrypted access token
when you explicitly disconnect.

**Limits.** This is a guarantee about the application, not about the storage underneath it. Anyone with
SSH or filesystem access can edit the SQLite file directly; uninstalling the add-on removes `/data`;
restoring an older Home Assistant backup replaces the database. Back up regularly and keep several
generations. See `plaid_monthly_cashflow/DOCS.md` for the full statement of guarantees, backup
recommendations, and the optional hash-chain tamper evidence - which is tamper *evidence*, not forensic
immutability.

## Historical ledger vs latest transaction state

- **Ledger events** are the complete history. A transaction that was added, modified twice, then
  removed has four rows and all four remain queryable.
- **Latest state** is a set of SQL views over the ledger that pick the newest event per transaction.
  Nothing is cached and nothing is mutable.

Dashboard totals read `transaction_active_state`, so a transaction with several historical versions is
counted exactly once at its latest amount, removed transactions are excluded, and a pending row
superseded by its posted counterpart is not double-counted.

## Disconnecting

`DELETE /api/disconnect` stops syncing without deleting anything. It asks Plaid to deactivate the Item,
clears the stored encrypted access token, and marks the item inactive.

It does **not** delete transaction events, account history, institution history, sync history, audit
history, the encryption key, or database files. The dashboard no longer exposes any control that
deletes local financial history.

To remove local financial data you must do it yourself outside the add-on - for example by uninstalling
the add-on, which removes `/data`. That is a Home Assistant Supervisor action and is deliberately
outside this dashboard.

Disconnecting does not affect your Plaid developer account, your bank, or old Home Assistant backups.
Delete old backups separately if they may contain cached Plaid data.

## Troubleshooting

### The dashboard says Not configured

Add your Plaid Client ID, Secret, and environment in the Home Assistant add-on Configuration tab, save, and restart the add-on.

### Plaid says the secret is invalid

Check that `plaid_env` matches the credential type. Use Sandbox credentials with `sandbox` and Production credentials with `production`.

### Plaid says a redirect URI is required

Register the HTTPS redirect URI in the Plaid dashboard and enter the exact same value in `plaid_redirect_uri`, then save and restart the add-on.

### Transactions are not ready

Plaid can need time before initial transactions are available. Wait a few minutes and click `Sync now`.

### The add-on installs but the page is blank

Open the add-on log and check that Uvicorn started on `0.0.0.0:8099`. The dashboard should be opened through Home Assistant Ingress, not through a direct container or host port.

### The repository does not appear in the add-on store

Confirm the public repository contains `repository.yaml` at the root and `plaid_monthly_cashflow/config.yaml` in the add-on folder.

## Development

Local checks:

```bash
python -m pip install -r plaid_monthly_cashflow/requirements.txt pytest httpx pyyaml ruff
```

```bash
python -m compileall -q plaid_monthly_cashflow/app
```

```bash
python -m ruff check plaid_monthly_cashflow/app tests
```

```bash
PYTHONPATH=plaid_monthly_cashflow python -m pytest -q
```

Tests use synthetic fixtures only. No real financial data and no real Plaid credentials appear anywhere
in the test suite.

The add-on folder also includes `DOCS.md` and an add-on-local `README.md`. Those files are optional in the original requested tree, but they improve Home Assistant add-on store presentation.

## Disclaimer

This add-on is for personal finance visualization. It is not financial advice, accounting advice, tax advice, or investment advice. Sandbox uses fake data. Production uses real bank data that you choose to connect through Plaid Link.
