# Home Assistant Plaid Monthly Cashflow

A local Home Assistant add-on that connects to Plaid and visualizes monthly inflow, outflow, and net cashflow.

This repository is designed to be added to Home Assistant OS / Supervisor as a public, secret-free add-on repository. Plaid credentials are entered only in the Home Assistant add-on Configuration panel and are never committed to GitHub.

## What it does

- Runs as a Supervisor add-on with Home Assistant Ingress.
- Lets you connect accounts through Plaid Link.
- Pulls Plaid transactions with cursor-based `transactions/sync` when available.
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
- The dashboard never displays Plaid secrets, Plaid access tokens, public tokens, or full account numbers.
- Do not expose this add-on directly to the internet outside Home Assistant Ingress.

## Plaid setup

Create a Plaid developer account and obtain the client ID and the secret for the environment you want to use.

Use Sandbox for first setup. Sandbox uses fake institutions and fake transaction data. Production uses real bank data and should only be enabled after you are comfortable with the local storage and disconnect behavior.

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
| `plaid_products` | `["transactions"]` | Plaid products requested for Link. |
| `plaid_country_codes` | `["US"]` | Plaid country codes. |
| `sync_months_back` | `12` | Number of months used for fallback transaction pulls and default dashboard range. |
| `sync_interval_minutes` | `360` | Background sync interval while the add-on is running. |
| `local_db_path` | `/data/plaid_cashflow.sqlite` | SQLite database path inside the add-on data directory. |
| `currency` | `USD` | Display currency for dashboard totals. |
| `debug_logging` | `false` | Enables detailed server logs without printing secrets or access tokens. |

## How monthly inflow/outflow is calculated

Plaid transaction amounts are usually positive for outflows and negative for inflows.

- `amount > 0` is outflow.
- `amount < 0` is inflow.
- `amount == 0` is neutral.
- Monthly inflow is the sum of absolute values for negative amounts.
- Monthly outflow is the sum of positive amounts.
- Net cashflow is `inflow - outflow`.
- Pending and removed transactions are excluded from monthly calculations.
- Missing months are filled with zero values so charts do not skip gaps.

## Local storage

The default SQLite database path is:

`/data/plaid_cashflow.sqlite`

Tables:

- `settings`
- `items`
- `accounts`
- `transactions`
- `sync_log`

Plaid access tokens are encrypted with a local Fernet key stored beside the database as `local_key.key`. That key is generated at runtime and must not be committed.

## Disconnecting and deleting data

The dashboard includes `Disconnect and delete local data`.

That action deletes:

- Encrypted Plaid access tokens
- Plaid cursors
- Linked account metadata
- Cached transactions
- Sync log entries

It does not delete your Plaid developer account, your Plaid app, or anything at your bank.

## Troubleshooting

### The dashboard says Not configured

Add your Plaid Client ID, Secret, and environment in the Home Assistant add-on Configuration tab, save, and restart the add-on.

### Plaid says the secret is invalid

Check that `plaid_env` matches the credential type. Use Sandbox credentials with `sandbox` and Production credentials with `production`.

### Transactions are not ready

Plaid can need time before initial transactions are available. Wait a few minutes and click `Sync now`.

### The add-on installs but the page is blank

Open the add-on log and check that Uvicorn started on `0.0.0.0:8099`. Also check browser console errors for blocked CDN access. The page still renders tables and empty states if Chart.js is unavailable.

### The repository does not appear in the add-on store

Confirm the public repository contains `repository.yaml` at the root and `plaid_monthly_cashflow/config.yaml` in the add-on folder.

## Development

Local syntax checks:

```bash
python -m compileall -q plaid_monthly_cashflow/app
python - <<'PY'
import yaml
for path in ["repository.yaml", "plaid_monthly_cashflow/config.yaml"]:
    with open(path, "r", encoding="utf-8") as handle:
        yaml.safe_load(handle)
    print(f"{path}: ok")
PY
```

The add-on folder also includes `DOCS.md` and an add-on-local `README.md`. Those files are optional in the original requested tree, but they improve Home Assistant add-on store presentation.

## Disclaimer

This add-on is for personal finance visualization. It is not financial advice, accounting advice, tax advice, or investment advice. Sandbox uses fake data. Production uses real bank data that you choose to connect through Plaid Link.
