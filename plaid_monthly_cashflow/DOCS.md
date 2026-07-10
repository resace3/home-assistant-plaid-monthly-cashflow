# Plaid Monthly Cashflow

## Setup

1. Open the add-on Configuration tab.
2. Enter your Plaid Client ID.
3. Enter the Plaid secret for the selected environment.
4. If you changed between Sandbox and Production after connecting, open the dashboard, delete the old local connection, and reconnect. Plaid access tokens cannot move between environments.
5. Choose the matching `plaid_env`.
6. Save and restart the add-on.
7. Open the web UI.
8. Connect through Plaid Link.

The add-on shows a Not configured state until Plaid credentials are saved and the add-on is restarted.

## Security

Plaid secrets are read from Home Assistant add-on options and are not sent to the browser. Plaid access tokens are encrypted locally and stored in the add-on data directory.

Use this add-on through Home Assistant Ingress only. Do not expose the add-on port directly to the internet or to an untrusted LAN.

The local encryption key is stored beside the SQLite database. Anyone with both the database and `local_key.key` can decrypt local Plaid access tokens.

The dashboard loads Plaid Link from Plaid. It does not load non-Plaid third-party dashboard scripts.

Disconnect deletes local cached add-on data and removes the local encryption key, then recreates an empty database. This does not delete anything at your bank, your Plaid developer account, your Plaid app, or old Home Assistant backups. Delete old backups separately if they may contain older cached Plaid data.

Do not paste logs, screenshots, database files, keys, or Home Assistant backups into GitHub issues or AI chats if they may contain secrets or financial data.
