# Plaid Monthly Cashflow

Local Home Assistant Ingress dashboard for monthly Plaid inflow, outflow, and net cashflow.

Configure Plaid credentials in the add-on Configuration tab, save, restart, then open the web UI and connect with Plaid. Use the secret that matches `plaid_env`; Sandbox and Production secrets are not interchangeable.

For Production institutions that require OAuth, configure `plaid_redirect_uri` with a URI that is also registered in your Plaid dashboard.

Do not commit Plaid secrets. Use this add-on through Home Assistant Ingress only; do not expose the add-on port directly.

Plaid access tokens are encrypted locally, but the encryption key is stored beside the database in the add-on data directory. Anyone with both files can decrypt the local tokens. Disconnect deletes local cached add-on data and removes the local key, but old Home Assistant backups may still contain older data.
