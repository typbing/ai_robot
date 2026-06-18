const DEFAULT_API = "http://100.94.190.35:8787";
const apiInput = document.querySelector("#apiBase");
const saveButton = document.querySelector("#saveApi");

const els = {
  equity: document.querySelector("#equity"),
  dailyPnl: document.querySelector("#dailyPnl"),
  openCount: document.querySelector("#openCount"),
  paperService: document.querySelector("#paperService"),
  updatedAt: document.querySelector("#updatedAt"),
  positions: document.querySelector("#positions"),
  dailyState: document.querySelector("#dailyState"),
  trades: document.querySelector("#trades"),
  rejects: document.querySelector("#rejects"),
  events: document.querySelector("#events"),
};

function apiBase() {
  return (localStorage.getItem("aiRobotApiBase") || DEFAULT_API).replace(/\/$/, "");
}

function saveApiBase() {
  localStorage.setItem("aiRobotApiBase", apiInput.value.trim().replace(/\/$/, ""));
  load();
}

function money(value) {
  const n = Number(value || 0);
  return `${n.toFixed(4)} USDT`;
}

function price(value) {
  const n = Number(value || 0);
  return n.toFixed(4);
}

function signed(value) {
  const n = Number(value || 0);
  return `${n >= 0 ? "+" : ""}${n.toFixed(4)} USDT`;
}

function clsForNumber(value) {
  const n = Number(value || 0);
  if (n > 0) return "positive";
  if (n < 0) return "negative";
  return "";
}

function shortTime(value) {
  if (!value) return "--";
  const d = new Date(value);
  if (Number.isNaN(d.valueOf())) return value;
  return d.toLocaleString();
}

function text(value, fallback = "--") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function badge(label, kind) {
  return `<span class="badge ${kind || ""}">${text(label)}</span>`;
}

function renderPositions(positions) {
  if (!positions.length) {
    els.positions.innerHTML = `<div class="row muted">No open positions.</div>`;
    return;
  }
  els.positions.innerHTML = positions
    .map((p) => {
      const symbol = p.symbol || p.instId;
      const rawSide = p.side || p.posSide || (Number(p.pos || 0) >= 0 ? "LONG" : "SHORT");
      const side = text(rawSide).toLowerCase();
      const entry = p.entry_price || p.avgPx;
      const takeProfit = p.take_profit_price || p.tpTriggerPx || "";
      const stopLoss = p.stop_loss_price || p.slTriggerPx || "";
      const notional = p.notional_usdt || p.notionalUsd || p.notional;
      const size = p.size || p.pos || "";
      return `
        <div class="row">
          <div class="row-title">
            <span>${text(symbol)}</span>
            ${badge(text(rawSide), side)}
          </div>
          <div class="row-meta">
            <span>Entry ${price(entry)}</span>
            <span>TP ${takeProfit ? price(takeProfit) : "--"}</span>
            <span>SL ${stopLoss ? price(stopLoss) : "--"}</span>
            <span>Notional ${notional ? money(notional) : "--"}</span>
            <span>Size ${text(size)}</span>
            <span>AI ${Number(p.ai_confidence || 0).toFixed(2)}</span>
          </div>
        </div>`;
    })
    .join("");
}

function renderDaily(daily, account) {
  const items = [
    ["Date", daily.date],
    ["Starting equity", money(daily.starting_equity_usdt)],
    ["Current equity", money(account.equity_usdt)],
    ["Gross PnL", signed(daily.daily_gross_pnl_usdt)],
    ["Fees", money(daily.daily_fees_usdt)],
    ["Trades", daily.trades_count || 0],
    ["Wins / Losses", `${daily.wins || 0} / ${daily.losses || 0}`],
    ["Stopped", daily.stop_trading_today ? text(daily.stop_reason, "yes") : "no"],
  ];
  els.dailyState.innerHTML = items
    .map(([k, v]) => `<div><span>${k}</span><strong>${v}</strong></div>`)
    .join("");
}

function renderTrades(trades) {
  if (!trades.length) {
    els.trades.innerHTML = `<div class="row muted">No trades recorded.</div>`;
    return;
  }
  const rows = trades
    .slice()
    .reverse()
    .map((t) => {
      const pnl = t.event === "CLOSE" ? signed(t.net_pnl_usdt) : "";
      return `
        <tr>
          <td>${shortTime(t.timestamp)}</td>
          <td>${text(t.event)}</td>
          <td>${text(t.symbol)}</td>
          <td>${text(t.side)}</td>
          <td>${price(t.price)}</td>
          <td>${money(t.notional_usdt)}</td>
          <td class="${clsForNumber(t.net_pnl_usdt)}">${pnl}</td>
          <td>${text(t.exit_reason, "")}</td>
        </tr>`;
    })
    .join("");
  els.trades.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Time</th><th>Event</th><th>Symbol</th><th>Side</th>
          <th>Price</th><th>Notional</th><th>Net</th><th>Reason</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderRejects(rejects) {
  const rows = rejects.slice(-8).reverse();
  if (!rows.length) {
    els.rejects.innerHTML = `<div class="row muted">No rejects recorded.</div>`;
    return;
  }
  els.rejects.innerHTML = rows
    .map(
      (r) => `
      <div class="row">
        <div class="row-title">
          <span>${text(r.symbol, "account")}</span>
          ${badge(text(r.reject_reason, "reject"), "failed")}
        </div>
        <div class="row-meta">
          <span>${shortTime(r.timestamp)}</span>
          <span>${text(r.candidate_action, "")}</span>
        </div>
      </div>`,
    )
    .join("");
}

function renderEvents(errors, notifications) {
  const combined = [
    ...errors.map((e) => ({ ...e, kind: "error", title: "error", body: e.error })),
    ...notifications.map((n) => ({ ...n, kind: n.status, body: n.error || n.reason || n.response })),
  ]
    .slice(-8)
    .reverse();
  if (!combined.length) {
    els.events.innerHTML = `<div class="row muted">No errors or notifications.</div>`;
    return;
  }
  els.events.innerHTML = combined
    .map(
      (e) => `
      <div class="row">
        <div class="row-title">
          <span>${text(e.title, e.kind)}</span>
          ${badge(text(e.kind || e.status), e.kind)}
        </div>
        <div class="row-meta">
          <span>${shortTime(e.timestamp)}</span>
          <span>${text(e.body, "")}</span>
        </div>
      </div>`,
    )
    .join("");
}

function render(data) {
  const account = data.account || {};
  const daily = data.daily || {};
  const positions = Array.isArray(account.open_positions) ? account.open_positions : [];
  const paperState = data.service?.paper || "unknown";
  const liveState = data.service?.live || "unknown";

  els.equity.textContent = money(account.equity_usdt);
  els.dailyPnl.textContent = signed(daily.daily_net_pnl_usdt);
  els.dailyPnl.className = clsForNumber(daily.daily_net_pnl_usdt);
  els.openCount.textContent = String(positions.length);
  els.paperService.innerHTML = `${badge(`paper ${paperState}`, paperState)} ${badge(`live ${liveState}`, liveState)}`;
  els.updatedAt.textContent = shortTime(data.timestamp);

  renderPositions(positions);
  renderDaily(daily, account);
  renderTrades(Array.isArray(data.trades) ? data.trades : []);
  renderRejects(Array.isArray(data.rejects) ? data.rejects : []);
  renderEvents(Array.isArray(data.errors) ? data.errors : [], Array.isArray(data.notifications) ? data.notifications : []);
}

function renderLoadError(error) {
  els.events.innerHTML = `
    <div class="row">
      <div class="row-title">
        <span>Dashboard API</span>
        ${badge("failed", "failed")}
      </div>
      <div class="row-meta">
        <span>${text(error.message, error)}</span>
      </div>
    </div>`;
}

async function load() {
  apiInput.value = apiBase();
  try {
    const response = await fetch(`${apiBase()}/api/status`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    try {
      const snapshot = await fetch("./status.json", { cache: "no-store" });
      if (!snapshot.ok) throw error;
      const data = await snapshot.json();
      render(data);
      els.events.insertAdjacentHTML(
        "afterbegin",
        `<div class="row">
          <div class="row-title">
            <span>Static snapshot</span>
            ${badge("offline", "inactive")}
          </div>
          <div class="row-meta">
            <span>Live API unavailable; showing the latest committed snapshot.</span>
          </div>
        </div>`,
      );
    } catch (snapshotError) {
      renderLoadError(error);
    }
  }
}

saveButton.addEventListener("click", saveApiBase);
apiInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveApiBase();
});

load();
setInterval(load, 30000);
