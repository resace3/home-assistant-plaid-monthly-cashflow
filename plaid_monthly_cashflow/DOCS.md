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

## Security

Plaid secrets are read from Home Assistant add-on options and are not sent to the browser. Plaid access tokens are encrypted locally and stored in the add-on data directory.
