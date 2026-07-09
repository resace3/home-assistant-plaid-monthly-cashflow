const state = {
  health: null,
  monthly: null,
  accounts: [],
  merchants: [],
  cashflowChart: null,
  netChart: null,
};

const $ = (id) => document.getElementById(id);

function apiUrl(path) {
  const base = new URL(".", window.location.href);
  return new URL(path.replace(/^\//, ""), base).toString();
}

async function fetchJson(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: {
      "Content-Type": "application/json",
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
  $("accountCount").textContent = state.accounts.length;

  $("netCashflow").className = Number(summary.net || 0) >= 0 ? "positive" : "negative";
  $("avgNet").className = Number(summary.average_monthly_net || 0) >= 0 ? "positive" : "negative";
}

function renderTable() {
  const body = $("cashflowTable");
  const rows = state.monthly?.months || [];
  const currency = state.monthly?.currency || "USD";

  if (rows.length === 0) {
    body.innerHTML = '<tr><td colspan="5">No transactions yet.</td></tr>';
    return;
  }

  body.innerHTML = rows
    .map((month) => {
      const netClass = Number(month.net || 0) >= 0 ? "positive" : "negative";
      return `
        <tr>
          <td>${month.month}</td>
          <td class="positive">${formatMoney(month.inflow, currency)}</td>
          <td class="negative">${formatMoney(month.outflow, currency)}</td>
          <td class="${netClass}">${formatMoney(month.net, currency)}</td>
          <td>${month.transaction_count}</td>
        </tr>
      `;
    })
    .join("");
}

function renderFallbackBars(targetId, rows, key, currency) {
  const target = $(targetId);
  if (!rows.length) {
    target.innerHTML = '<div class="empty">No chart data yet.</div>';
    return;
  }

  const max = Math.max(...rows.map((row) => Math.abs(Number(row[key] || 0))), 1);
  target.innerHTML = rows
    .map((row) => {
      const value = Number(row[key] || 0);
      const width = Math.max((Math.abs(value) / max) * 100, value === 0 ? 2 : 8);
      const klass = value >= 0 ? "bar positive-bg" : "bar negative-bg";
      return `
        <div class="fallback-row">
          <span>${row.month}</span>
          <div class="fallback-track"><div class="${klass}" style="width:${width}%"></div></div>
          <strong>${formatMoney(value, currency)}</strong>
        </div>
      `;
    })
    .join("");
}

function renderCharts() {
  const rows = state.monthly?.months || [];
  const currency = state.monthly?.currency || "USD";
  const chartAvailable = Boolean(window.Chart);

  $("cashflowFallback").hidden = chartAvailable;
  $("netFallback").hidden = chartAvailable;
  $("cashflowChart").hidden = !chartAvailable;
  $("netChart").hidden = !chartAvailable;

  if (!chartAvailable) {
    renderFallbackBars("cashflowFallback", rows, "outflow", currency);
    renderFallbackBars("netFallback", rows, "net", currency);
    return;
  }

  const labels = rows.map((row) => row.month);
  const inflow = rows.map((row) => row.inflow);
  const outflow = rows.map((row) => row.outflow);
  const net = rows.map((row) => row.net);

  const moneyTick = (value) => formatMoney(value, currency);

  if (state.cashflowChart) state.cashflowChart.destroy();
  state.cashflowChart = new Chart($("cashflowChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Inflow",
          data: inflow,
          backgroundColor: "#15803d",
          borderRadius: 4,
        },
        {
          label: "Outflow",
          data: outflow,
          backgroundColor: "#dc2626",
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom" },
        tooltip: {
          callbacks: { label: (context) => `${context.dataset.label}: ${formatMoney(context.raw, currency)}` },
        },
      },
      scales: {
        y: { ticks: { callback: moneyTick }, grid: { color: "#e5e7eb" } },
        x: { grid: { display: false } },
      },
    },
  });

  if (state.netChart) state.netChart.destroy();
  state.netChart = new Chart($("netChart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Net",
          data: net,
          borderColor: "#2563eb",
          backgroundColor: "rgba(37, 99, 235, 0.12)",
          tension: 0.25,
          pointRadius: 3,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (context) => formatMoney(context.raw, currency) } },
      },
      scales: {
        y: { ticks: { callback: moneyTick }, grid: { color: "#e5e7eb" } },
        x: { grid: { display: false } },
      },
    },
  });
}

function renderMerchants() {
  const target = $("merchantList");
  const merchants = state.merchants || [];
  const currency = state.monthly?.currency || "USD";
  if (!merchants.length) {
    target.innerHTML = '<div class="empty">No merchant totals yet.</div>';
    return;
  }

  const max = Math.max(...merchants.map((merchant) => Number(merchant.amount || 0)), 1);
  target.innerHTML = merchants
    .map((merchant) => {
      const width = Math.max((Number(merchant.amount || 0) / max) * 100, 8);
      return `
        <div class="merchant-row">
          <div>
            <strong>${merchant.merchant}</strong>
            <span>${merchant.transaction_count} transactions</span>
          </div>
          <div class="merchant-amount">${formatMoney(merchant.amount, currency)}</div>
          <div class="merchant-track"><div style="width:${width}%"></div></div>
        </div>
      `;
    })
    .join("");
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
    const [health, monthly, accounts, merchants] = await Promise.all([
      fetchJson("api/health"),
      fetchJson("api/monthly-cashflow"),
      fetchJson("api/accounts"),
      fetchJson("api/top-merchants?direction=outflow"),
    ]);
    state.health = health;
    state.monthly = monthly;
    state.accounts = accounts || [];
    state.merchants = merchants || [];
    renderAll();
  } catch (error) {
    setStatus("Error", "error");
    showAlert(error.message || "Dashboard failed to load.");
    state.monthly = { currency: "USD", months: [], summary: {} };
    state.accounts = [];
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
    "Delete local Plaid tokens, cursors, accounts, transactions, and sync history from this add-on?"
  );
  if (!confirmed) return;

  clearAlert();
  try {
    await fetchJson("api/disconnect", { method: "DELETE" });
    await loadDashboard({ quiet: true });
    showAlert("Local Plaid data deleted.", "ok");
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
