const state = {
  health: null,
  monthly: null,
  accountCount: 0,
  merchants: [],
  diagnostics: null,
  diagnosticsVisible: false,
  cashflowChart: null,
  netChart: null,
};

const $ = (id) => document.getElementById(id);

const VERSIONED_ENTRY = /\/v\d+\/$/;

function apiUrl(path) {
  const current = new URL(".", window.location.href);
  const base = VERSIONED_ENTRY.test(current.pathname) ? new URL("../", current) : current;
  return new URL(path.replace(/^\//, ""), base).toString();
}

async function fetchJson(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(method === "GET" ? {} : { "X-Plaid-Cashflow-Action": "1" }),
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    const detail = payload?.detail || payload?.error || "Request failed";
    throw new Error(detail);
  }
  return payload;
}

function showAlert(message, type = "error") {
  const alert = $("alert");
  alert.textContent = message;
  alert.className = `alert ${type}`;
  alert.hidden = false;
}

function clearAlert() {
  const alert = $("alert");
  alert.hidden = true;
  alert.textContent = "";
}

function setStatus(label, kind) {
  const pill = $("statusPill");
  pill.textContent = label;
  pill.className = `status-pill status-${kind}`;
}

function formatMoney(value, currency = "USD") {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(Number(value || 0));
}

function formatDateTime(value) {
  if (!value) return "Never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function textNode(tagName, text, className = "") {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function emptyMessage(text) {
  const node = document.createElement("div");
  node.className = "empty";
  node.textContent = text;
  return node;
}

function boundedPercent(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return 0;
  return Math.max(0, Math.min(number, 100));
}

function updateStatusAndSetup() {
  const health = state.health;
  if (!health) {
    setStatus("Error", "error");
    return;
  }

  $("envValue").textContent = health.plaid_env || "sandbox";
  $("itemsValue").textContent = health.connected_items ?? 0;
  $("transactionsValue").textContent = health.transaction_count ?? 0;
  $("eventsValue").textContent = health.transaction_event_count ?? 0;

  if (!health.configured) {
    setStatus("Not configured", "warning");
    $("setupText").textContent =
      "Add your Plaid Client ID, Secret, and environment in the Home Assistant add-on Configuration tab, save, and restart the add-on.";
    $("connectButton").disabled = true;
    $("syncButton").disabled = true;
    return;
  }

  $("connectButton").disabled = false;
  $("syncButton").disabled = false;

  if (health.connection_requires_reset) {
    setStatus("Reconnect required", "warning");
    $("setupText").textContent =
      `This connection was created in ${health.connection_environment || "another Plaid environment"}, ` +
      `but the add-on is configured for ${health.plaid_env}. Reconnect with Plaid in the configured ` +
      `environment. Stored transaction history is preserved either way.`;
    $("connectButton").disabled = true;
    $("syncButton").disabled = true;
    return;
  }

  if ((health.connected_items || 0) === 0) {
    setStatus("Not connected", "neutral");
    $("setupText").textContent = "Plaid keys are configured. Connect an account to start syncing transaction data.";
    return;
  }

  setStatus("Connected", "ok");
  $("setupText").textContent = "Connected. Sync now or refresh the dashboard to update totals.";
}

function renderMetrics() {
  const currency = state.monthly?.currency || "USD";
  const summary = state.monthly?.summary || {};
  $("totalInflow").textContent = formatMoney(summary.total_inflow, currency);
  $("totalOutflow").textContent = formatMoney(summary.total_outflow, currency);
  $("netCashflow").textContent = formatMoney(summary.net, currency);
  $("avgNet").textContent = formatMoney(summary.average_monthly_net, currency);
  $("lastSync").textContent = formatDateTime(state.health?.last_sync_at);
  $("accountCount").textContent = state.accountCount;

  $("netCashflow").className = Number(summary.net || 0) >= 0 ? "positive" : "negative";
  $("avgNet").className = Number(summary.average_monthly_net || 0) >= 0 ? "positive" : "negative";
}

function renderTable() {
  const body = $("cashflowTable");
  const rows = state.monthly?.months || [];
  const currency = state.monthly?.currency || "USD";

  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No transactions yet.";
    row.appendChild(cell);
    body.replaceChildren(row);
    return;
  }

  const tableRows = rows.map((month) => {
    const row = document.createElement("tr");
    const netClass = Number(month.net || 0) >= 0 ? "positive" : "negative";
    row.appendChild(textNode("td", month.month || ""));
    row.appendChild(textNode("td", formatMoney(month.inflow, currency), "positive"));
    row.appendChild(textNode("td", formatMoney(month.outflow, currency), "negative"));
    row.appendChild(textNode("td", formatMoney(month.net, currency), netClass));
    row.appendChild(textNode("td", String(month.transaction_count ?? 0)));
    return row;
  });
  body.replaceChildren(...tableRows);
}

function renderFallbackBars(targetId, rows, key, currency) {
  const target = $(targetId);
  if (!rows.length) {
    target.replaceChildren(emptyMessage("No chart data yet."));
    return;
  }

  const max = Math.max(...rows.map((row) => Math.abs(Number(row[key] || 0))), 1);
  const renderedRows = rows.map((row) => {
    const value = Number(row[key] || 0);
    const width = boundedPercent(Math.max((Math.abs(value) / max) * 100, value === 0 ? 2 : 8));
    const outer = document.createElement("div");
    outer.className = "fallback-row";
    outer.appendChild(textNode("span", row.month || ""));

    const track = document.createElement("div");
    track.className = "fallback-track";
    const bar = document.createElement("div");
    bar.className = value >= 0 ? "bar positive-bg" : "bar negative-bg";
    bar.style.width = `${width}%`;
    track.appendChild(bar);

    outer.appendChild(track);
    outer.appendChild(textNode("strong", formatMoney(value, currency)));
    return outer;
  });
  target.replaceChildren(...renderedRows);
}

function renderCharts() {
  const rows = state.monthly?.months || [];
  const currency = state.monthly?.currency || "USD";
  if (state.cashflowChart) state.cashflowChart.destroy();
  if (state.netChart) state.netChart.destroy();
  state.cashflowChart = null;
  state.netChart = null;
  $("cashflowFallback").hidden = false;
  $("netFallback").hidden = false;
  $("cashflowChart").hidden = true;
  $("netChart").hidden = true;
  renderFallbackBars("cashflowFallback", rows, "outflow", currency);
  renderFallbackBars("netFallback", rows, "net", currency);
}

function renderMerchants() {
  const target = $("merchantList");
  const merchants = state.merchants || [];
  const currency = state.monthly?.currency || "USD";
  if (!merchants.length) {
    target.replaceChildren(emptyMessage("No merchant totals yet."));
    return;
  }

  const max = Math.max(...merchants.map((merchant) => Number(merchant.amount || 0)), 1);
  const renderedRows = merchants.map((merchant) => {
    const width = boundedPercent(Math.max((Number(merchant.amount || 0) / max) * 100, 8));
    const row = document.createElement("div");
    row.className = "merchant-row";

    const info = document.createElement("div");
    info.appendChild(textNode("strong", merchant.merchant || "Unknown merchant"));
    info.appendChild(textNode("span", `${merchant.transaction_count ?? 0} transactions`));

    const amount = textNode("div", formatMoney(merchant.amount, currency), "merchant-amount");
    const track = document.createElement("div");
    track.className = "merchant-track";
    const bar = document.createElement("div");
    bar.style.width = `${width}%`;
    track.appendChild(bar);

    row.appendChild(info);
    row.appendChild(amount);
    row.appendChild(track);
    return row;
  });
  target.replaceChildren(...renderedRows);
}

function renderAll() {
  updateStatusAndSetup();
  renderMetrics();
  renderTable();
  renderCharts();
  renderMerchants();
  renderDiagnostics();
}

async function loadDashboard({ quiet = false } = {}) {
  if (!quiet) setStatus("Loading", "loading");
  clearAlert();
  try {
    const health = await fetchJson("api/health");
    state.health = health;
    if (health.connection_requires_reset) {
      state.monthly = { currency: "USD", months: [], summary: {} };
      state.accountCount = 0;
      state.merchants = [];
      renderAll();
      showAlert(
        `The saved Plaid connection belongs to ${health.connection_environment || "another environment"}. ` +
          `Reconnect in ${health.plaid_env}. Stored transaction history is preserved.`
      );
      return;
    }
    const [monthly, accountSummary, merchants] = await Promise.all([
      fetchJson("api/monthly-cashflow"),
      fetchJson("api/accounts"),
      fetchJson("api/top-merchants?direction=outflow"),
    ]);
    state.monthly = monthly;
    state.accountCount = Number(accountSummary?.count || 0);
    state.merchants = merchants || [];
    renderAll();
  } catch (error) {
    setStatus("Error", "error");
    showAlert(error.message || "Dashboard failed to load.");
    state.monthly = { currency: "USD", months: [], summary: {} };
    state.accountCount = 0;
    state.merchants = [];
    renderMetrics();
    renderTable();
    renderCharts();
    renderMerchants();
  }
}

async function connectWithPlaid() {
  clearAlert();
  if (!window.Plaid) {
    showAlert("Plaid Link did not load. Check network access and refresh.");
    return;
  }

  try {
    const { link_token: linkToken } = await fetchJson("api/link-token", { method: "POST" });
    const handler = window.Plaid.create({
      token: linkToken,
      onSuccess: async (publicToken) => {
        setStatus("Syncing", "loading");
        try {
          await fetchJson("api/exchange-public-token", {
            method: "POST",
            body: JSON.stringify({ public_token: publicToken }),
          });
          await loadDashboard({ quiet: true });
        } catch (error) {
          setStatus("Error", "error");
          showAlert(error.message || "Plaid token exchange failed.");
        }
      },
      onExit: (err) => {
        if (err) showAlert(err.display_message || err.error_message || "Plaid Link exited before connecting.");
      },
    });
    handler.open();
  } catch (error) {
    showAlert(error.message || "Could not create Plaid Link token.");
  }
}

async function syncNow() {
  clearAlert();
  setStatus("Syncing", "loading");
  try {
    const result = await fetchJson("api/sync", { method: "POST" });
    await loadDashboard({ quiet: true });
    if (state.diagnosticsVisible) {
      state.diagnostics = await fetchJson("api/diagnostics");
      renderDiagnostics();
    }
    showAlert(
      `Sync complete. ${result.inserted_events ?? 0} new ledger events, ` +
        `${result.duplicate_events ?? 0} exact duplicates ignored.`,
      "ok"
    );
  } catch (error) {
    setStatus("Error", "error");
    showAlert(error.message || "Sync failed.");
  }
}

const DIAGNOSTIC_LABELS = [
  ["total_transaction_events", "Total immutable transaction events"],
  ["distinct_transaction_ids", "Distinct Plaid transaction IDs"],
  ["active_transactions", "Current active transactions"],
  ["pending_transactions", "Pending transactions"],
  ["removed_transactions", "Removed transactions (retained)"],
  ["superseded_pending_transactions", "Pending rows superseded by a posted transaction"],
  ["modified_event_count", "Modification events recorded"],
  ["removed_event_count", "Removal events recorded"],
  ["legacy_import_event_count", "Legacy rows imported into the ledger"],
  ["historical_import_event_count", "Historical backfill events"],
  ["linked_accounts", "Linked accounts"],
  ["accounts_with_metadata", "Accounts with stored metadata"],
  ["account_observation_count", "Account metadata observations"],
  ["events_with_raw_json", "Events retaining full Plaid JSON"],
  ["earliest_transaction_date", "Earliest transaction date"],
  ["latest_transaction_date", "Latest transaction date"],
  ["schema_version", "Database schema version"],
  ["app_version", "Add-on version"],
];

function diagnosticRow(label, value) {
  const row = document.createElement("tr");
  row.appendChild(textNode("th", label));
  row.appendChild(textNode("td", value === null || value === undefined ? "—" : String(value)));
  return row;
}

function renderDiagnostics() {
  const panel = $("diagnosticsPanel");
  panel.hidden = !state.diagnosticsVisible;
  if (!state.diagnosticsVisible) return;

  const body = $("diagnosticsTable");
  const data = state.diagnostics;
  if (!data) {
    body.replaceChildren(diagnosticRow("Status", "Loading..."));
    return;
  }

  const aggregates = data.aggregates || {};
  const rows = DIAGNOSTIC_LABELS.map(([key, label]) => diagnosticRow(label, aggregates[key]));

  const lastSync = data.last_sync || {};
  rows.push(diagnosticRow("Last sync status", lastSync.status));
  rows.push(diagnosticRow("Last sync finished", formatDateTime(lastSync.finished_at)));
  rows.push(diagnosticRow("Last sync added events", lastSync.added_count));
  rows.push(diagnosticRow("Last sync modified events", lastSync.modified_count));
  rows.push(diagnosticRow("Last sync removed events", lastSync.removed_count));
  rows.push(diagnosticRow("Last sync new ledger rows", lastSync.inserted_event_count));
  rows.push(diagnosticRow("Last sync exact duplicates ignored", lastSync.duplicate_event_count));
  rows.push(diagnosticRow("Initial backfill complete", data.backfill_complete ? "Yes" : "No"));
  rows.push(
    diagnosticRow("Integrity problems detected", data.integrity?.ok ? "None" : "Yes — see below")
  );
  rows.push(
    diagnosticRow(
      "Ledger hash chain",
      data.hash_chain?.ok
        ? `Verified (${data.hash_chain.events_checked ?? 0} events)`
        : "Break detected"
    )
  );
  body.replaceChildren(...rows);

  const checks = data.integrity?.checks || [];
  const target = $("integrityList");
  if (!checks.length) {
    target.replaceChildren(emptyMessage("No integrity checks reported."));
    return;
  }
  const items = checks.map((check) => {
    const row = document.createElement("div");
    row.className = check.ok ? "integrity-row ok" : "integrity-row bad";
    row.appendChild(textNode("span", check.ok ? "PASS" : "FAIL", "integrity-flag"));
    row.appendChild(textNode("span", check.check));
    if (check.detail) row.appendChild(textNode("span", check.detail, "integrity-detail"));
    return row;
  });
  target.replaceChildren(...items);
}

async function toggleDiagnostics() {
  state.diagnosticsVisible = !state.diagnosticsVisible;
  renderDiagnostics();
  if (!state.diagnosticsVisible) return;
  try {
    state.diagnostics = await fetchJson("api/diagnostics");
  } catch (error) {
    showAlert(error.message || "Diagnostics failed to load.");
    state.diagnostics = null;
  }
  renderDiagnostics();
}

function bindActions() {
  $("connectButton").addEventListener("click", connectWithPlaid);
  $("syncButton").addEventListener("click", syncNow);
  $("refreshButton").addEventListener("click", () => loadDashboard());
  $("diagnosticsButton").addEventListener("click", toggleDiagnostics);
}

bindActions();
loadDashboard();
