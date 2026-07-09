# Plaid Monthly Cashflow

Local Home Assistant Ingress dashboard for monthly Plaid inflow, outflow, and net cashflow.

Configure Plaid credentials in the add-on Configuration tab, save, restart, then open the web UI and connect with Plaid. Use the secret that matches `plaid_env`; Sandbox and Production secrets are not interchangeable.

For Production institutions that require OAuth, configure `plaid_redirect_uri` with a URI that is also registered in your Plaid dashboard.

Do not commit Plaid secrets. Do not expose this add-on directly outside Home Assistant Ingress.
