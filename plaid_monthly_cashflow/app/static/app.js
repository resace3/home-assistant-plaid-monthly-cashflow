const state = {
  health: null,
  monthly: null,
  accountCount: 0,
  merchants: [],
  cashflowChart: null,
  netChart: null,
};

const $ = (id) => document.getElementById(id);

function apiUrl(path) {
  const current = new URL(".", window.location.href);
  const base = current.pathname.endsWith("/v018/") ? new URL("../", current) : current;
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
      `but the add-on is configured for ${health.plaid_env}. Delete local data, then reconnect with Plaid.`;
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
          `Delete local data and reconnect in ${health.plaid_env}.`
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
    await fetchJson("api/sync", { method: "POST" });
    await loadDashboard({ quiet: true });
  } catch (error) {
    setStatus("Error", "error");
    showAlert(error.message || "Sync failed.");
  }
}

async function disconnect() {
  const confirmed = window.confirm(
    "Delete local cached Plaid data from this add-on and rotate the local encryption key?"
  );
  if (!confirmed) return;

  clearAlert();
  try {
    await fetchJson("api/disconnect", { method: "DELETE" });
    await loadDashboard({ quiet: true });
    showAlert("Local cached Plaid data deleted.", "ok");
  } catch (error) {
    showAlert(error.message || "Disconnect failed.");
  }
}

function bindActions() {
  $("connectButton").addEventListener("click", connectWithPlaid);
  $("syncButton").addEventListener("click", syncNow);
  $("refreshButton").addEventListener("click", () => loadDashboard());
  $("disconnectButton").addEventListener("click", disconnect);
}

bindActions();
loadDashboard();
