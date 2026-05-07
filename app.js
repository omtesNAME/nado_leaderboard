"use strict";

const DEFAULT_PERIOD = "1m";
const DEFAULT_TOP = 20;

let state = {
  data: null,
  period: DEFAULT_PERIOD,
  top: DEFAULT_TOP,
  walletAddress: "",
};

const tabs = document.querySelectorAll(".tab:not(.top-btn)");
const topBtn = document.getElementById("top-btn");
const topMenu = document.getElementById("top-menu");
const topOptions = document.querySelectorAll(".top-option");
const lastUpdatedEl = document.getElementById("last-updated");
const loadingEl = document.getElementById("loading");
const errorEl = document.getElementById("error-msg");
const tableWrap = document.getElementById("table-wrap");
const tbody = document.getElementById("leaderboard-body");
const walletInput = document.getElementById("wallet-input");
const walletBtn = document.getElementById("wallet-search-btn");
const searchMsg = document.getElementById("search-msg");
const statsRow = document.getElementById("stats-row");

(async function init() {
  setupTabs();
  setupTopSelect();
  setupWalletSearch();

  tabs.forEach(t => t.classList.remove("active"));
  document.querySelector(`[data-period="${DEFAULT_PERIOD}"]`).classList.add("active");

  await loadData();
})();

async function loadData() {
  showLoading();
  try {
    const res = await fetch("leaderboard.json?_=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    state.data = await res.json();
    updateLastUpdated();
    updateStats();
    render();
  } catch (e) {
    showError("Failed to load leaderboard.json. Run fetch.py first, use a local web server, or check GitHub Actions.");
  }
}

function render() {
  if (!state.data) return;

  const rows = getPeriodRows();
  const limited = rows.slice(0, state.top);

  const wallet = state.walletAddress.toLowerCase().trim();
  let userRow = null;
  let userRank = null;
  if (wallet) {
    const found = rows.find(r => r.address.toLowerCase() === wallet);
    if (found) {
      userRow = found;
      userRank = found.rank;
    }
  }

  tbody.innerHTML = "";

  limited.forEach(row => {
    const isUser = wallet && row.address.toLowerCase() === wallet;
    tbody.appendChild(buildRow(row, isUser));
  });

  if (wallet) {
    if (userRow && userRank > state.top) {
      const sep = document.createElement("tr");
      sep.className = "separator";
      sep.innerHTML = `<td colspan="5">...</td>`;
      tbody.appendChild(sep);
      tbody.appendChild(buildRow(userRow, true));
      searchMsg.textContent = `Your rank: #${userRank}`;
    } else if (!userRow) {
      searchMsg.textContent = "Address not found in this period";
    } else {
      searchMsg.textContent = `Your rank: #${userRank}`;
    }
  } else {
    searchMsg.textContent = "";
  }

  showTable();
}

function buildRow(row, highlight) {
  const tr = document.createElement("tr");
  if (highlight) tr.classList.add("highlight");

  const pnl = row.realized_pnl;
  const pnlClass = pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "pnl-zero";
  const pnlSign = pnl > 0 ? "+" : "";

  tr.innerHTML = `
    <td class="rank">${row.rank}</td>
    <td class="address">${escHtml(row.display_address)}</td>
    <td class="${pnlClass}">${pnlSign}${fmtUsd(pnl)}</td>
    <td class="vol">${fmtUsd(row.volume)}</td>
    <td class="fee">${fmtUsd(row.fees)}</td>
  `;
  return tr;
}

function getPeriodRows() {
  return (state.data?.periods?.[state.period] || []).map(r => ({ ...r }));
}

function setupTabs() {
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      tabs.forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      state.period = tab.dataset.period;
      render();
    });
  });
}

function setupTopSelect() {
  topBtn.addEventListener("click", e => {
    e.stopPropagation();
    topMenu.classList.toggle("hidden");
  });

  topOptions.forEach(opt => {
    opt.addEventListener("click", () => {
      state.top = parseInt(opt.dataset.value, 10);
      topBtn.textContent = opt.textContent;
      topMenu.classList.add("hidden");
      render();
    });
  });

  document.addEventListener("click", () => topMenu.classList.add("hidden"));
}

function setupWalletSearch() {
  walletBtn.addEventListener("click", doWalletSearch);
  walletInput.addEventListener("keydown", e => {
    if (e.key === "Enter") doWalletSearch();
  });
}

function doWalletSearch() {
  state.walletAddress = walletInput.value.trim();
  render();
}

function updateLastUpdated() {
  if (!state.data?.last_updated) return;
  const then = new Date(state.data.last_updated);
  const diffMs = Date.now() - then.getTime();
  const mins = Math.floor(diffMs / 60000);
  let label;
  if (mins < 1) label = "just now";
  else if (mins === 1) label = "1 minute ago";
  else if (mins < 60) label = `${mins} minutes ago`;
  else {
    const hrs = Math.floor(mins / 60);
    label = hrs === 1 ? "1 hour ago" : `${hrs} hours ago`;
  }
  lastUpdatedEl.textContent = "last updated: " + label;
}

function updateStats() {
  statsRow.classList.remove("hidden");
}

function fmtUsd(val) {
  const abs = Math.abs(val);
  const formatted = abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (val < 0 ? "-$" : "$") + formatted;
}

function escHtml(str) {
  return str.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showLoading() {
  loadingEl.classList.remove("hidden");
  tableWrap.classList.add("hidden");
  errorEl.classList.add("hidden");
}

function showTable() {
  loadingEl.classList.add("hidden");
  tableWrap.classList.remove("hidden");
  errorEl.classList.add("hidden");
}

function showError(msg) {
  loadingEl.classList.add("hidden");
  tableWrap.classList.add("hidden");
  errorEl.classList.remove("hidden");
  errorEl.textContent = msg;
}
