const BACKEND_ORIGIN = "http://127.0.0.1:8300";
const API_ROOT = `${BACKEND_ORIGIN}/api/v1`;
const REFRESH_INTERVAL_MS = 30_000;

const elements = {
  refreshButton: document.querySelector("#refresh-button"),
  lastUpdated: document.querySelector("#last-updated"),
  modeBadge: document.querySelector("#mode-badge"),
  connectionCard: document.querySelector("#connection-card"),
  connectionLabel: document.querySelector("#connection-label"),
  connectionDetail: document.querySelector("#connection-detail"),
  activeProfile: document.querySelector("#active-profile"),
  activeProfileCaption: document.querySelector("#active-profile-caption"),
  paperNetPnl: document.querySelector("#paper-net-pnl"),
  paperPnlCaption: document.querySelector("#paper-pnl-caption"),
  paperTradeCount: document.querySelector("#paper-trade-count"),
  dhanStatus: document.querySelector("#dhan-status"),
  dhanCaption: document.querySelector("#dhan-caption"),
  paperGrossPnl: document.querySelector("#paper-gross-pnl"),
  paperCharges: document.querySelector("#paper-charges"),
  paperWinRate: document.querySelector("#paper-win-rate"),
  paperProfitFactor: document.querySelector("#paper-profit-factor"),
  contextEvaluated: document.querySelector("#context-evaluated"),
  contextAllow: document.querySelector("#context-allow"),
  contextSkip: document.querySelector("#context-skip"),
  contextOutcomes: document.querySelector("#context-outcomes"),
  contextConfluence: document.querySelector("#context-confluence"),
  researchReadiness: document.querySelector("#research-readiness"),
  outcomeGrid: document.querySelector("#outcome-grid"),
  learningSummary: document.querySelector("#learning-summary"),
  profileList: document.querySelector("#profile-list"),
  healthApplication: document.querySelector("#health-application"),
  healthVersion: document.querySelector("#health-version"),
  instrumentStatus: document.querySelector("#instrument-status"),
  profileTemplate: document.querySelector("#profile-template"),
  outcomeTemplate: document.querySelector("#outcome-template"),
  telegramStatus: document.querySelector("#telegram-status"),
  historySource: document.querySelector("#history-source"),
  runBacktestButton: document.querySelector("#run-backtest-button"),
  backtestStrategy: document.querySelector("#backtest-strategy"),
  historyNote: document.querySelector("#history-note"),
  historyTrades: document.querySelector("#history-trades"),
  historyNet: document.querySelector("#history-net"),
  historyWinrate: document.querySelector("#history-winrate"),
  historyPf: document.querySelector("#history-pf"),
  historyMaxdd: document.querySelector("#history-maxdd"),
  historyHold: document.querySelector("#history-hold"),
  equityCurve: document.querySelector("#equity-curve"),
  historyBreakdowns: document.querySelector("#history-breakdowns"),
  historyTableBody: document.querySelector("#history-table-body"),
  historyFootnote: document.querySelector("#history-footnote"),
};

function currency(value) {
  const numeric = Number(value || 0);
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(numeric);
}

function number(value, fallback = "—") {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("en-IN").format(Number(value)) : fallback;
}

function setPnl(element, value) {
  const numeric = Number(value || 0);
  element.textContent = currency(numeric);
  element.classList.toggle("positive", numeric > 0);
  element.classList.toggle("negative", numeric < 0);
}

async function get(path) {
  const response = await fetch(`${API_ROOT}${path}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

async function getHealth() {
  const response = await fetch(`${BACKEND_ORIGIN}/health`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Health check failed (${response.status})`);
  return response.json();
}

function setConnected(health) {
  const paper = health.execution_mode === "PAPER";
  elements.connectionCard.classList.remove("offline");
  elements.connectionLabel.textContent = "OptionMaster connected";
  elements.connectionDetail.textContent = `${health.application} ${health.version} is available on the local machine.`;
  elements.modeBadge.innerHTML = `<span></span> ${paper ? "Paper mode" : "Live mode"}`;
  elements.modeBadge.classList.toggle("attention", !paper);
}

function setOffline(error) {
  elements.connectionCard.classList.add("offline");
  elements.connectionLabel.textContent = "Backend unavailable";
  elements.connectionDetail.textContent = "Start OptionMaster on port 8300, then refresh this dashboard.";
  elements.modeBadge.innerHTML = "<span></span> Waiting for backend";
  elements.modeBadge.classList.add("attention");
  elements.lastUpdated.textContent = error.message;
}

function renderPaperPerformance(performance) {
  setPnl(elements.paperNetPnl, performance.net_pnl);
  elements.paperTradeCount.textContent = number(performance.closed_trades);
  elements.paperPnlCaption.textContent = `${number(performance.closed_trades)} completed paper trade${performance.closed_trades === 1 ? "" : "s"}, after costs`;
  elements.paperGrossPnl.textContent = currency(performance.gross_pnl);
  elements.paperCharges.textContent = currency(performance.charges);
  elements.paperWinRate.textContent = `${Number(performance.win_rate_pct || 0).toFixed(1)}%`;
  elements.paperProfitFactor.textContent = performance.profit_factor == null ? "—" : Number(performance.profit_factor).toFixed(2);
}

function renderContextShadow(summary, outcomes) {
  elements.contextEvaluated.textContent = number(summary.total_evaluations);
  elements.contextAllow.textContent = number(summary.would_allow);
  elements.contextSkip.textContent = number(summary.would_skip);
  elements.contextOutcomes.textContent = `${number(outcomes.linked_closed_trades)} / ${number(outcomes.recommended_minimum_sample)}`;
  elements.contextConfluence.textContent = `${Number(summary.average_confluence_score || 0).toFixed(0)} / 100`;
}

function renderOutcomeResearch(report) {
  elements.outcomeGrid.replaceChildren();
  elements.researchReadiness.textContent = report.ready_for_feature_review
    ? "Ready for review"
    : `${number(report.linked_closed_trades)} / ${number(report.recommended_minimum_sample)} linked outcomes`;
  elements.researchReadiness.classList.toggle("ready", Boolean(report.ready_for_feature_review));
  const groups = [
    ["Signal freshness", report.freshness],
    ["Confluence score", report.confluence],
    ["Room to structure", report.structure_room],
  ];
  groups.forEach(([title, buckets]) => {
    const fragment = elements.outcomeTemplate.content.cloneNode(true);
    fragment.querySelector("h3").textContent = title;
    const rows = fragment.querySelector(".outcome-rows");
    buckets.forEach((bucket) => {
      const row = document.createElement("div");
      row.className = "outcome-row";
      row.innerHTML = `<span>${bucket.label}</span><strong>${number(bucket.closed_trades)} trades · ${currency(bucket.net_pnl)}</strong>`;
      rows.append(row);
    });
    elements.outcomeGrid.append(fragment);
  });
}

function renderProfiles(profiles, evaluations, activeProfile) {
  elements.profileList.replaceChildren();
  const eligibleCount = evaluations.filter((item) => item.eligible_for_paper_promotion).length;
  elements.learningSummary.textContent = eligibleCount ? `${eligibleCount} candidate ready` : "Baseline protected";
  elements.learningSummary.classList.toggle("ready", eligibleCount > 0);

  profiles.forEach((profile) => {
    const evaluation = evaluations.find((item) => item.profile.id === profile.id);
    const fragment = elements.profileTemplate.content.cloneNode(true);
    const root = fragment.querySelector(".profile-row");
    const title = fragment.querySelector("h3");
    const badge = fragment.querySelector(".profile-badge");
    const description = fragment.querySelector(".profile-description");
    const evidence = fragment.querySelector(".profile-evidence");
    const outcome = fragment.querySelector(".profile-outcome");
    const isActive = activeProfile.id === profile.id;
    const eligible = evaluation?.eligible_for_paper_promotion;

    title.textContent = profile.name;
    badge.textContent = isActive ? "Active" : profile.baseline ? "Reference" : "Candidate";
    badge.classList.toggle("active", isActive);
    description.textContent = profile.description;
    if (evaluation) {
      const performance = evaluation.performance;
      evidence.textContent = `${number(performance.closed_trades)} closed paper trades · ${currency(performance.net_pnl)} net · PF ${performance.profit_factor == null ? "—" : Number(performance.profit_factor).toFixed(2)}`;
      outcome.textContent = eligible ? "Eligible for paper promotion" : evaluation.reasons[0] || "Under review";
      outcome.classList.toggle("eligible", Boolean(eligible));
    } else {
      evidence.textContent = "Evidence has not loaded yet.";
      outcome.textContent = "Under review";
    }
    root.classList.toggle("is-active", isActive);
    elements.profileList.append(fragment);
  });
}

// ---------------------------------------------------------------------------
// History & analysis
// ---------------------------------------------------------------------------

const historyState = { runs: [], paperTrades: [], selected: "paper" };

async function post(path, body) {
  const response = await fetch(`${API_ROOT}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function normalizePaperTrade(trade) {
  const closed = trade.status !== "OPEN";
  const opened = new Date(trade.opened_at);
  const exited = trade.closed_at ? new Date(trade.closed_at) : null;
  return {
    date: opened,
    exit: exited,
    symbol: trade.symbol,
    contract: `${trade.strike} ${trade.side}`,
    entryPrice: trade.entry_price,
    exitPrice: trade.exit_price,
    exitReason: closed ? trade.status : "OPEN",
    holdMinutes: exited ? (exited - opened) / 60000 : null,
    net: closed ? Number(trade.realized_pnl ?? 0) : Number(trade.unrealized_pnl ?? 0),
    closed,
  };
}

function normalizeBacktestTrade(trade) {
  return {
    date: new Date(trade.entry_time),
    exit: new Date(trade.exit_time),
    symbol: trade.symbol,
    contract: `${trade.strike} ${trade.side}`,
    entryPrice: trade.entry_price,
    exitPrice: trade.exit_price,
    exitReason: trade.exit_reason,
    holdMinutes: trade.hold_minutes,
    net: Number(trade.net_pnl ?? 0),
    closed: true,
  };
}

function computeAnalysis(trades) {
  const closed = trades.filter((trade) => trade.closed);
  const wins = closed.filter((trade) => trade.net > 0);
  const losses = closed.filter((trade) => trade.net < 0);
  const profit = wins.reduce((sum, trade) => sum + trade.net, 0);
  const loss = Math.abs(losses.reduce((sum, trade) => sum + trade.net, 0));
  let running = 0;
  let peak = 0;
  let maxDrawdown = 0;
  const curve = [0];
  closed
    .slice()
    .sort((a, b) => a.date - b.date)
    .forEach((trade) => {
      running += trade.net;
      curve.push(running);
      peak = Math.max(peak, running);
      maxDrawdown = Math.max(maxDrawdown, peak - running);
    });
  const holds = closed.filter((trade) => Number.isFinite(trade.holdMinutes));
  return {
    trades: closed.length,
    net: closed.reduce((sum, trade) => sum + trade.net, 0),
    winRate: closed.length ? (wins.length / closed.length) * 100 : 0,
    profitFactor: loss > 0 ? profit / loss : null,
    maxDrawdown,
    averageHold: holds.length
      ? holds.reduce((sum, trade) => sum + trade.holdMinutes, 0) / holds.length
      : null,
    curve,
  };
}

function renderEquityCurve(curve) {
  const svg = elements.equityCurve;
  svg.replaceChildren();
  if (curve.length < 2) return;
  const width = 800;
  const height = 220;
  const pad = 10;
  const minimum = Math.min(...curve, 0);
  const maximum = Math.max(...curve, 0);
  const span = maximum - minimum || 1;
  const x = (index) => pad + (index / (curve.length - 1)) * (width - pad * 2);
  const y = (value) => height - pad - ((value - minimum) / span) * (height - pad * 2);
  const points = curve.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`);
  const namespace = "http://www.w3.org/2000/svg";
  const zero = document.createElementNS(namespace, "line");
  zero.setAttribute("x1", pad);
  zero.setAttribute("x2", width - pad);
  zero.setAttribute("y1", y(0));
  zero.setAttribute("y2", y(0));
  zero.setAttribute("class", "equity-zero");
  svg.append(zero);
  const area = document.createElementNS(namespace, "polygon");
  area.setAttribute(
    "points",
    `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points.join(" ")} ${x(curve.length - 1).toFixed(1)},${y(0).toFixed(1)}`,
  );
  area.setAttribute("class", "equity-area");
  svg.append(area);
  const line = document.createElementNS(namespace, "polyline");
  line.setAttribute("points", points.join(" "));
  line.setAttribute("class", `equity-line ${curve[curve.length - 1] >= 0 ? "up" : "down"}`);
  svg.append(line);
}

function renderBreakdowns(trades) {
  elements.historyBreakdowns.replaceChildren();
  const closed = trades.filter((trade) => trade.closed);
  const groupings = [
    ["By symbol", (trade) => trade.symbol],
    ["By entry hour", (trade) => `${String(trade.date.getHours()).padStart(2, "0")}:00`],
    ["By exit", (trade) => trade.exitReason],
  ];
  groupings.forEach(([title, keyOf]) => {
    const buckets = new Map();
    closed.forEach((trade) => {
      const key = keyOf(trade);
      const bucket = buckets.get(key) ?? { trades: 0, wins: 0, net: 0 };
      bucket.trades += 1;
      bucket.wins += trade.net > 0 ? 1 : 0;
      bucket.net += trade.net;
      buckets.set(key, bucket);
    });
    const fragment = elements.outcomeTemplate.content.cloneNode(true);
    fragment.querySelector("h3").textContent = title;
    const rows = fragment.querySelector(".outcome-rows");
    [...buckets.entries()]
      .sort((a, b) => String(a[0]).localeCompare(String(b[0])))
      .forEach(([label, bucket]) => {
        const row = document.createElement("div");
        row.className = "outcome-row";
        row.innerHTML = `<span>${label}</span><strong>${bucket.trades} trades · ${Math.round((bucket.wins / bucket.trades) * 100)}% win · ${currency(bucket.net)}</strong>`;
        rows.append(row);
      });
    elements.historyBreakdowns.append(fragment);
  });
}

function renderHistoryTable(trades) {
  const body = elements.historyTableBody;
  body.replaceChildren();
  if (!trades.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty-cell">No trades for this source yet.</td></tr>';
    return;
  }
  const formatTime = (value) =>
    value
      ? new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(value)
      : "—";
  const formatDate = (value) =>
    new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short" }).format(value);
  trades
    .slice()
    .sort((a, b) => b.date - a.date)
    .slice(0, 200)
    .forEach((trade) => {
      const row = document.createElement("tr");
      const pnlClass = trade.net > 0 ? "positive" : trade.net < 0 ? "negative" : "";
      row.innerHTML = `
        <td>${formatDate(trade.date)}</td>
        <td>${trade.symbol}</td>
        <td>${trade.contract}</td>
        <td>${formatTime(trade.date)}</td>
        <td>${formatTime(trade.exit)}</td>
        <td>${Number.isFinite(trade.holdMinutes) ? `${Math.round(trade.holdMinutes)}m` : "—"}</td>
        <td>${trade.exitReason.replaceAll("_", " ")}</td>
        <td class="num">${Number(trade.entryPrice).toFixed(2)}</td>
        <td class="num">${trade.exitPrice == null ? "—" : Number(trade.exitPrice).toFixed(2)}</td>
        <td class="num ${pnlClass}">${currency(trade.net)}</td>`;
      body.append(row);
    });
}

function renderHistory(trades, note) {
  const analysis = computeAnalysis(trades);
  elements.historyTrades.textContent = number(analysis.trades);
  setPnl(elements.historyNet, analysis.net);
  elements.historyWinrate.textContent = `${analysis.winRate.toFixed(1)}%`;
  elements.historyPf.textContent = analysis.profitFactor == null ? "—" : analysis.profitFactor.toFixed(2);
  elements.historyMaxdd.textContent = currency(-analysis.maxDrawdown);
  elements.historyHold.textContent = analysis.averageHold == null ? "—" : `${analysis.averageHold.toFixed(1)} min`;
  elements.historyNote.textContent = note;
  renderEquityCurve(analysis.curve);
  renderBreakdowns(trades);
  renderHistoryTable(trades);
}

function renderHistorySourceOptions() {
  const select = elements.historySource;
  const previous = historyState.selected;
  select.replaceChildren();
  const paperOption = document.createElement("option");
  paperOption.value = "paper";
  paperOption.textContent = `Paper trades (${historyState.paperTrades.length})`;
  select.append(paperOption);
  historyState.runs.forEach((run) => {
    const option = document.createElement("option");
    option.value = run.id;
    const when = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(run.created_at));
    option.textContent = `Backtest ${when} · ${run.summary.trades} trades · ${currency(run.summary.net_pnl)}`;
    select.append(option);
  });
  select.value = [...select.options].some((option) => option.value === previous) ? previous : "paper";
  historyState.selected = select.value;
}

async function showSelectedHistory() {
  if (historyState.selected === "paper") {
    renderHistory(
      historyState.paperTrades.map(normalizePaperTrade),
      "Forward paper trades recorded by OptionMaster, marked against live Dhan quotes, net of charges.",
    );
    return;
  }
  try {
    const run = await get(`/backtests/runs/${encodeURIComponent(historyState.selected)}`);
    const isReversal = run.request?.strategy === "stored-reversal-v1";
    const params = (isReversal ? run.request?.reversal : run.request?.params) ?? {};
    const trigger = isReversal
      ? `sweep of ${params.sweep_lookback_bars}-bar extreme by ≥ ${params.min_sweep_pct}%, reclaim ≥ ${params.reclaim_pct}% of range`
      : `momentum ≥ ${params.momentum_threshold_pct}% / ${params.momentum_lookback_minutes}m`;
    renderHistory(
      run.trades.map(normalizeBacktestTrade),
      `Backtest of ${run.strategy_id} on stored Dhan snapshots (${run.summary.first_day ?? "—"} → ${run.summary.last_day ?? "—"}): ` +
        `${trigger}, SL ${params.stop_loss_pct}%, target ${params.target_pct}%, ` +
        `hold ≤ ${params.max_hold_minutes}m, entries ${params.entry_start}–${params.entry_end} IST. Net of all charges.`,
    );
  } catch (error) {
    elements.historyNote.textContent = `Could not load that backtest run: ${error.message}`;
  }
}

async function loadHistory() {
  const [runs, paperTrades, dataStatus] = await Promise.all([
    get("/backtests/runs").catch(() => []),
    get("/paper-trades").catch(() => []),
    get("/backtests/data-status").catch(() => null),
  ]);
  historyState.runs = runs;
  historyState.paperTrades = paperTrades;
  renderHistorySourceOptions();
  await showSelectedHistory();
  if (dataStatus?.directory_available) {
    const symbols = Object.entries(dataStatus.symbols)
      .map(([symbol, count]) => `${symbol} (${count}d)`)
      .join(", ");
    elements.historyFootnote.textContent = `Stored Dhan snapshots: ${symbols} · ${dataStatus.first_day} → ${dataStatus.last_day}. Fills are simulated at stored bid/ask; results include the full NSE cost schedule.`;
  } else {
    elements.historyFootnote.textContent = "Stored Dhan snapshot directory is not available; backtests need the strategy-lab options archive.";
  }
}

async function runBacktestNow() {
  elements.runBacktestButton.disabled = true;
  elements.runBacktestButton.textContent = "Running…";
  try {
    const run = await post("/backtests/run", {
      strategy: elements.backtestStrategy?.value || "stored-scalp-v1",
    });
    historyState.selected = run.id;
    await loadHistory();
  } catch (error) {
    elements.historyNote.textContent = `Backtest failed: ${error.message}`;
  } finally {
    elements.runBacktestButton.disabled = false;
    elements.runBacktestButton.textContent = "Run backtest";
  }
}

function renderHealth(health, dhan, instruments, activeProfile) {
  elements.activeProfile.textContent = activeProfile.name;
  elements.activeProfileCaption.textContent = activeProfile.baseline ? "Reference profile active in paper mode" : "Candidate profile active in paper mode";
  elements.dhanStatus.textContent = dhan.configured ? "Connected" : "Not connected";
  elements.dhanStatus.classList.toggle("positive", dhan.configured);
  elements.dhanCaption.textContent = dhan.configured ? "Dhan credentials are available" : "Add Dhan credentials to enable market data";
  elements.healthApplication.textContent = health.application || "OptionMaster";
  elements.healthVersion.textContent = health.version || "—";
  elements.instrumentStatus.textContent = instruments.available ? "Ready" : "Refresh required";
  elements.instrumentStatus.classList.toggle("positive", instruments.available);
}

function renderAlerts(alerts) {
  const configured = Boolean(alerts?.telegram_configured);
  elements.telegramStatus.textContent = configured ? "On (open/close)" : "Not configured";
  elements.telegramStatus.classList.toggle("positive", configured);
}

async function refreshDashboard() {
  elements.refreshButton.disabled = true;
  elements.refreshButton.textContent = "Refreshing…";
  try {
    const health = await getHealth();
    setConnected(health);
    const [dhan, activeProfile, profiles, paper, instruments, contextShadow, contextOutcomes, alerts] = await Promise.all([
      get("/dhan/status"),
      get("/learning/active-profile"),
      get("/learning/profiles"),
      get("/reports/paper-performance"),
      get("/instruments/nse/status"),
      get("/reports/context-shadow"),
      get("/reports/context-outcomes"),
      get("/alerts/status").catch(() => null),
    ]);
    const evaluations = await Promise.all(
      profiles.map((profile) => get(`/learning/profiles/${encodeURIComponent(profile.id)}/evaluation`).catch(() => null)),
    );
    renderHealth(health, dhan, instruments, activeProfile);
    renderAlerts(alerts);
    renderPaperPerformance(paper);
    renderContextShadow(contextShadow, contextOutcomes);
    renderOutcomeResearch(contextOutcomes);
    renderProfiles(profiles, evaluations.filter(Boolean), activeProfile);
    await loadHistory();
    elements.lastUpdated.textContent = `Updated ${new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  } catch (error) {
    setOffline(error instanceof Error ? error : new Error("Unable to refresh dashboard."));
  } finally {
    elements.refreshButton.disabled = false;
    elements.refreshButton.textContent = "Refresh";
  }
}

elements.refreshButton.addEventListener("click", refreshDashboard);
elements.runBacktestButton.addEventListener("click", runBacktestNow);
elements.historySource.addEventListener("change", () => {
  historyState.selected = elements.historySource.value;
  showSelectedHistory();
});
refreshDashboard();
window.setInterval(refreshDashboard, REFRESH_INTERVAL_MS);
