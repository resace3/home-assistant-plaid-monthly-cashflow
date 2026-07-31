# Plaid Monthly Cashflow

Local Home Assistant Ingress dashboard for monthly Plaid inflow, outflow, and net cashflow, backed by
an append-only transaction ledger.

Configure Plaid credentials in the add-on Configuration tab, save, restart, then open the web UI and connect with Plaid. Use the secret that matches `plaid_env`; Sandbox and Production secrets are not interchangeable.

For Production institutions that require OAuth, configure `plaid_redirect_uri` with a URI that is also registered in your Plaid dashboard.

## Append-only storage

Every transaction Plaid reports is stored permanently. Modified transactions append a new version,
removed transactions append a removal event, and pending transactions stay queryable after they post.
Nothing is deleted, overwritten, truncated, or nulled out. Dashboard totals read a derived view of the
latest state, so a transaction with several versions is still counted once.

The dashboard has no control that deletes local financial history. Disconnecting stops syncing and
clears the stored Plaid access token; it does not remove stored transactions.

This is a guarantee about the application's behaviour, not about the storage underneath it. Anyone with
filesystem access can still modify the SQLite database directly, and uninstalling the add-on removes
`/data`. Keep Home Assistant backups. See `DOCS.md` for the full guarantees, their limits, and backup
recommendations.

## Security

Do not commit Plaid secrets. Use this add-on through Home Assistant Ingress only; do not expose the add-on port directly.

Plaid access tokens are encrypted locally, but the encryption key is stored beside the database in the add-on data directory. Anyone with both files can decrypt the local tokens. Home Assistant backups contain both, so treat them as sensitive.
