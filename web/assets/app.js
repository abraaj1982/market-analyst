/* ==========================================================================
   Market Analyst — dashboard logic
   Reads from /api only and computes nothing itself: there is one source of
   truth for every number, and it is the server.
   ========================================================================== */
"use strict";

const State = {
  rows: [],
  selected: null,
  timeframe: "4h",
  sortKey: "confidence",
  sortDir: -1,
  chart: null,
  candleSeries: null,
  macdChart: null,
  macdSeries: {},
  priceLines: [],
  currentAnalysis: null,
  tz: "Asia/Muscat",
  companies: [],
  selectedCompany: null,
  indicatorSeries: {},
  indicatorsOn: { ma: true, supertrend: false, ichimoku: false, macd: false },
  chatHistory: [],
};

const $ = (id) => document.getElementById(id);
const fmtPct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const fmtNum = (v, d = 5) =>
  v === null || v === undefined || v === "" ? "—"
    : Number(v).toLocaleString("en-US", { maximumFractionDigits: d });

const DIRECTION = {
  1: { label: "Bullish", cls: "bull", icon: "▲" },
  0: { label: "Neutral", cls: "neutral", icon: "—" },
  "-1": { label: "Bearish", cls: "bear", icon: "▼" },
};
const GRADE_CLASS = { "A+": "g-Aplus", A: "g-A", B: "g-B", C: "g-C", NO_TRADE: "g-NO" };
const REGIMES = {
  trending: "Trending", ranging: "Ranging", quiet: "Quiet", high_volatility: "High volatility",
};
const ENGINE_LABELS = {
  trend: "Multi-timeframe trend",
  ict_smc: "ICT / liquidity & structure",
  classic_ta: "Classical technical analysis",
  indicators: "Technical indicators",
  macro: "Macro & intermarket",
  cot: "COT positioning",
  volume_seasonality: "Volume & seasonality",
  fundamentals: "Fundamentals",
  news: "News & economic calendar",
  dividends: "Dividend quality",
  sentiment: "News sentiment",
};

const ARABIC = /[؀-ۿ]/;

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let message = `${path} → ${res.status}`;
    try { message = (await res.json()).detail || message; } catch { /* keep default */ }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  try {
    const health = await api("/api/health");
    State.tz = health.timezone || State.tz;
    $("profileLine").textContent =
      `Profile: ${health.profile} · config ${health.config_version}` +
      (health.offline ? " · synthetic data mode" : "");
    $("version").textContent = health.version;
  } catch {
    $("profileLine").textContent = "Cannot reach the server";
  }
  tickClock();
  setInterval(tickClock, 1000);
  await refresh();
  setInterval(refresh, 60000);
}

function tickClock() {
  $("clock").textContent = new Intl.DateTimeFormat("en-GB", {
    timeZone: State.tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
}

async function refresh() {
  try {
    State.rows = await api("/api/analyses");
  } catch {
    $("tbody").innerHTML = `<tr><td colspan="8" class="notice">Could not load analyses.</td></tr>`;
  }
  renderKpis();
  renderTable();
  renderStats();
  if (State.selected) {
    await loadTimeframeAnalysis(State.selected, State.timeframe);
    await loadChart(State.selected);
    await loadIndicators(State.selected);
  }
  await loadCompanies();
}

/* ------------------------------------------------------------------- KPIs */

function renderKpis() {
  const rows = State.rows;
  const actionable = rows.filter((r) => r.actionable);
  const blocked = rows.filter((r) => !r.actionable && r.blocking_failures.length);
  const newest = rows.reduce((a, r) => (!a || r.as_of > a ? r.as_of : a), null);
  const time = (iso) =>
    new Intl.DateTimeFormat("en-GB", {
      timeZone: State.tz, hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(new Date(iso));

  $("kpis").innerHTML = [
    kpi("Qualified setups", actionable.length,
        actionable.length ? actionable.map((r) => r.symbol).join(", ") : "Standing aside is a decision"),
    kpi("Symbols watched", rows.length, "from watchlist.yaml"),
    kpi("Blocked by a gate", blocked.length, "a high score alone is not enough"),
    kpi("Last updated", newest ? time(newest) : "—", "refreshed on the scheduler's clock"),
  ].join("");
}

const kpi = (label, value, hint) =>
  `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${escapeHtml(hint)}</div></div>`;

/* ------------------------------------------------------------------ table */

function renderTable() {
  const term = $("search").value.trim().toLowerCase();
  const onlyActionable = $("onlyActionable").checked;

  const rows = State.rows.filter((r) => {
    if (onlyActionable && !r.actionable) return false;
    if (!term) return true;
    return r.symbol.toLowerCase().includes(term) || (r.name || "").toLowerCase().includes(term);
  });

  rows.sort((a, b) => {
    const x = a[State.sortKey], y = b[State.sortKey];
    if (typeof x === "string") return State.sortDir * x.localeCompare(y, "en");
    return State.sortDir * ((x ?? 0) - (y ?? 0));
  });

  if (!rows.length) {
    $("tbody").innerHTML = `<tr><td colspan="8" class="notice">No matching rows.</td></tr>`;
    return;
  }

  $("tbody").innerHTML = rows.map((r) => {
    const d = DIRECTION[String(r.direction)] || DIRECTION[0];
    const colour = r.direction > 0 ? "var(--bull)" : r.direction < 0 ? "var(--bear)" : "var(--neutral)";
    const status = r.actionable
      ? `<span class="ok">Qualified</span>`
      : r.blocking_failures.length
      // Show that the gate FAILED. Printing its name alone reads as if it passed.
      ? `<span class="blocked" title="${escapeHtml(r.blocking_failures[0].detail)}">Blocked — ${escapeHtml(r.blocking_failures[0].label)}</span>`
      : `<span class="neutral">—</span>`;
    return `
      <tr data-symbol="${r.symbol}" class="${State.selected === r.symbol ? "selected" : ""}">
        <td class="sym">${r.symbol}<small>${escapeHtml(r.name || "")}</small></td>
        <td class="${d.cls}">${d.icon} ${d.label}</td>
        <td>
          <div class="conf">
            <div class="conf-track"><div class="conf-fill" style="width:${Math.round((r.confidence || 0) * 100)}%;background:${colour}"></div></div>
            <span class="num">${fmtPct(r.confidence)}</span>
          </div>
        </td>
        <td><span class="badge ${GRADE_CLASS[r.grade] || "g-NO"}">${r.grade}</span></td>
        <td class="neutral">${REGIMES[r.regime] || r.regime || "—"}</td>
        <td class="num">${fmtPct(r.coverage_ratio)} <small class="neutral">(${r.active_engines ?? 0})</small></td>
        <td class="num">${fmtNum(r.spot)}</td>
        <td>${status}</td>
      </tr>`;
  }).join("");

  document.querySelectorAll("#tbody tr[data-symbol]").forEach((tr) =>
    tr.addEventListener("click", () => selectSymbol(tr.dataset.symbol))
  );
}

/* ----------------------------------------------------------------- detail */

async function selectSymbol(symbol) {
  State.selected = symbol;
  renderTable();
  $("aiPanel").innerHTML = "";
  $("aiBtn").textContent = "Generate AI read";
  State.chatHistory = [];
  $("aiChatLog").innerHTML = "";
  const cached = State.rows.find((r) => r.symbol === symbol);
  if (cached) renderDetail(cached); // instant paint from the swing snapshot
  await loadTimeframeAnalysis(symbol, State.timeframe);
  await loadChart(symbol);
  await loadIndicators(symbol);
}

function contributionBars(contributions, container) {
  const sorted = contributions.slice().sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  const max = Math.max(0.4, ...sorted.map((c) => Math.abs(c.contribution)));
  container.innerHTML = sorted.map((c) => {
    if (c.skipped_reason) {
      return `<div class="contrib-skipped">— ${ENGINE_LABELS[c.engine] || c.engine}: ${escapeHtml(c.skipped_reason)}</div>`;
    }
    const pct = (Math.abs(c.contribution) / max) * 50;
    const positive = c.contribution >= 0;
    const side = positive ? `left:50%;width:${pct}%` : `right:50%;width:${pct}%`;
    const colour = positive ? "var(--bull)" : "var(--bear)";
    return `
      <div class="contrib-row">
        <span>${ENGINE_LABELS[c.engine] || c.engine}</span>
        <div class="contrib-bar"><span class="zero"></span><span style="${side};background:${colour}"></span></div>
        <span class="num ${positive ? "bull" : "bear"}">${c.contribution >= 0 ? "+" : ""}${c.contribution.toFixed(2)}</span>
      </div>`;
  }).join("");
}

function renderDetail(row) {
  $("detailPanel").hidden = false;
  $("detailTitle").textContent = `${row.name} (${row.symbol})`;

  $("tfButtons").innerHTML = ["1m", "5m", "15m", "1h", "4h", "1d"].map(
    (tf) => `<button class="tf-btn ${tf === State.timeframe ? "active" : ""}" data-tf="${tf}">${
      { "1m": "1M", "5m": "5M", "15m": "15M", "1h": "1H", "4h": "4H", "1d": "1D" }[tf]
    }</button>`
  ).join("");
  document.querySelectorAll(".tf-btn").forEach((b) =>
    b.addEventListener("click", () => {
      State.timeframe = b.dataset.tf;
      renderDetail(row); // instant highlight of the active button
      loadTimeframeAnalysis(State.selected, State.timeframe);
      loadChart(State.selected);
      loadIndicators(State.selected);
    })
  );

  renderTradePlan(row);
  renderVerdict(row);
  renderConfluenceTable(row);
  renderSchools(row);
  contributionBars(row.contributions || [], $("contributions"));

  document.querySelectorAll(".ind-btn").forEach((b) => {
    b.classList.toggle("active", State.indicatorsOn[b.dataset.ind]);
    b.onclick = () => {
      State.indicatorsOn[b.dataset.ind] = !State.indicatorsOn[b.dataset.ind];
      b.classList.toggle("active", State.indicatorsOn[b.dataset.ind]);
      applyIndicators();
    };
  });
}

function renderVerdict(row) {
  const el = $("verdictBanner");
  const dir = DIRECTION[row.direction] || DIRECTION[0];
  if (row.actionable) {
    el.className = "verdict-banner go";
    el.innerHTML = `<div class="headline">✅ TRADE — ${dir.label}, ${fmtPct(row.confidence)} confidence</div>
      <div>Every hard gate passed and the timeframes agree.</div>`;
    return;
  }
  el.className = "verdict-banner wait";
  const reason = (row.blocking_failures && row.blocking_failures[0])
    ? row.blocking_failures[0].detail || row.blocking_failures[0].label
    : "confidence is too low relative to the setup";
  el.innerHTML = `<div class="headline">⏳ WAIT — ${dir.label} lean, but not tradeable (${fmtPct(row.confidence)} confidence)</div>
    <div>${escapeHtml(reason)}</div>`;
}

function renderTradePlan(row) {
  const el = $("tradePlan");
  const r = row.risk;
  if (!r) {
    el.className = "trade-plan empty";
    el.innerHTML = `<div class="cell">No trade plan yet — this setup is not tradeable (see WAIT reason below).</div>`;
    return;
  }
  el.className = "trade-plan";
  const cells = [
    ["entry", "Entry", fmtNum(r.entry)],
    ["stop", "Stop loss", fmtNum(r.stop_loss)],
    ["tp", "Take profit 1", fmtNum(r.take_profit_1)],
  ];
  if (r.take_profit_2 !== null && r.take_profit_2 !== undefined) {
    cells.push(["tp", "Take profit 2", fmtNum(r.take_profit_2)]);
  }
  cells.push(["", "Risk : reward", r.risk_reward ? `1 : ${r.risk_reward.toFixed(2)}` : "—"]);
  el.innerHTML = cells.map(([cls, label, value]) =>
    `<div class="cell ${cls}"><div class="label">${label}</div><div class="value">${value}</div></div>`
  ).join("");
}

function renderConfluenceTable(row) {
  const el = $("confluenceTable");
  const engines = row.engines || [];
  if (!engines.length) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = engines.map((e) => {
    const name = ENGINE_LABELS[e.engine] || e.engine;
    if (e.skipped_reason) {
      return `<div class="confluence-row">
        <span class="system">${name}</span>
        <span class="confluence-result"><span class="confluence-dot skip"></span>—</span>
      </div>`;
    }
    const dir = { 1: ["bull", "Buy"], 0: ["neutral", "Neutral"], "-1": ["bear", "Sell"] }[
      String(e.direction)
    ] || ["neutral", "Neutral"];
    return `<div class="confluence-row">
      <span class="system">${name}</span>
      <span class="confluence-result"><span class="confluence-dot ${dir[0]}"></span>${dir[1]}</span>
    </div>`;
  }).join("");
}

function renderSchools(row) {
  const el = $("schools");
  const engines = row.engines || [];
  if (!engines.length) {
    el.innerHTML = `<div class="notice">No engine detail available for this analysis.</div>`;
    return;
  }
  el.innerHTML = engines.map((e) => {
    const name = ENGINE_LABELS[e.engine] || e.engine;
    if (e.skipped_reason) {
      return `<div class="school-card skipped">
        <div class="school-name">${name}</div>
        <div class="school-read">— Stood aside</div>
        <div class="school-detail">${escapeHtml(e.skipped_reason)}</div>
      </div>`;
    }
    const dir = DIRECTION[String(e.direction)] || DIRECTION[0];
    const top = (e.evidence || []).slice().sort(
      (a, b) => Math.abs(b.contribution) - Math.abs(a.contribution)
    )[0];
    const detail = top
      ? (top.detail ? `${top.label} — ${top.detail}` : top.label)
      : (e.notes && e.notes[0]) || "";
    return `<div class="school-card ${dir.cls}">
      <div class="school-name">${name}</div>
      <div class="school-read">${dir.icon} ${dir.label}</div>
      <div class="school-detail">${escapeHtml(detail)}</div>
    </div>`;
  }).join("");
}

/* -------------------------------------------------------------- AI analyst */

async function loadAI(symbol) {
  const panel = $("aiPanel");
  const btn = $("aiBtn");
  btn.disabled = true;
  btn.textContent = "Thinking…";
  panel.innerHTML = "";
  try {
    const r = await api(`/api/analysis/${symbol}/ai`);
    renderAI(r);
  } catch {
    panel.innerHTML = `<div class="notice">Could not reach the AI analyst.</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Regenerate";
  }
}

function renderAI(r) {
  const panel = $("aiPanel");
  if (r.status === "not_configured") {
    panel.innerHTML = `<div class="notice">AI analyst is not turned on for this deployment
      (no <code>ANTHROPIC_API_KEY</code> set) — everything else on the dashboard works without it.</div>`;
    return;
  }
  if (r.status === "no_analysis") {
    panel.innerHTML = `<div class="notice">No stored analysis to interpret yet.</div>`;
    return;
  }
  if (r.status === "error") {
    panel.innerHTML = `<div class="notice">AI request failed: ${escapeHtml(r.message || "unknown error")}</div>`;
    return;
  }

  const list = (items) => (items || []).length
    ? `<ul>${items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`
    : `<p>—</p>`;

  panel.innerHTML = `
    <div class="ai-block">
      <div class="ai-bias">🤖 ${escapeHtml(r.market_bias || "—")}</div>

      <div class="ai-section">
        <div class="ai-label">Main reason</div>
        <p>${escapeHtml(r.main_reason || "—")}</p>
      </div>

      <div class="ai-section">
        <div class="ai-label">Supporting evidence</div>
        ${list(r.supporting_evidence)}
      </div>

      <div class="ai-scenarios">
        <div class="ai-scenario bull">
          <div class="ai-label">Bullish scenario</div>
          <p>${escapeHtml(r.bullish_scenario || "—")}</p>
        </div>
        <div class="ai-scenario bear">
          <div class="ai-label">Bearish scenario</div>
          <p>${escapeHtml(r.bearish_scenario || "—")}</p>
        </div>
      </div>

      <div class="ai-section">
        <div class="ai-label">Key levels</div>
        ${list(r.key_levels)}
      </div>

      <div class="ai-section">
        <div class="ai-label">Invalidation</div>
        <p>${escapeHtml(r.invalidation_condition || "—")}</p>
      </div>

      <div class="ai-section">
        <div class="ai-label">Risk warnings</div>
        ${list(r.risk_warnings)}
      </div>

      <div class="ai-section">
        <div class="ai-label">Conflicting evidence</div>
        ${list(r.conflicting_evidence)}
      </div>

      <div class="ai-section">
        <div class="ai-label">Summary</div>
        <p>${escapeHtml(r.final_summary || "—")}</p>
      </div>

      <div class="ai-disclaimer">
        AI-generated interpretation of the analysis above — not financial advice, and not a
        guarantee of any outcome. Cross-check against "Read by school" before acting on it.
      </div>
    </div>`;
}

function appendChatMessage(role, text, pending) {
  const log = $("aiChatLog");
  const el = document.createElement("div");
  el.className = `ai-chat-msg ${role}${pending ? " pending" : ""}`;
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

async function sendChatMessage(text) {
  const symbol = State.selected;
  if (!symbol || !text.trim()) return;

  appendChatMessage("user", text);
  const pending = appendChatMessage("assistant", "Thinking…", true);

  try {
    const r = await postJson(`/api/analysis/${symbol}/ai/chat`, {
      message: text, history: State.chatHistory,
    });
    if (r.status === "ok") {
      pending.textContent = r.reply;
      pending.classList.remove("pending");
      State.chatHistory.push({ role: "user", content: text });
      State.chatHistory.push({ role: "assistant", content: r.reply });
    } else if (r.status === "not_configured") {
      pending.textContent = "AI analyst is not turned on for this deployment (no ANTHROPIC_API_KEY set).";
      pending.classList.remove("pending");
    } else {
      pending.textContent = r.message || "Could not get a reply — try again.";
      pending.classList.remove("pending");
    }
  } catch {
    pending.textContent = "Could not reach the AI analyst.";
    pending.classList.remove("pending");
  }
}

function renderGates(gates, container) {
  container.innerHTML = gates.map((g) => {
    const icon = g.status === "passed" ? "✅" : g.status === "failed" ? "❌" : "—";
    const tag = g.blocking ? "" : " <small class='neutral'>(advisory)</small>";
    return `<li>${icon} ${escapeHtml(g.label)}${tag}<span class="detail">${escapeHtml(g.detail || "")}</span></li>`;
  }).join("");
}

async function loadTimeframeAnalysis(symbol, tf) {
  try {
    const result = await api(`/api/analysis/${symbol}/timeframe/${tf}`);
    State.currentAnalysis = result;
    renderDetail(result);
    $("report").textContent = result.report || "—";
    renderGates(result.gates || [], $("gates"));
    drawLevels(symbol);
  } catch (err) {
    $("verdictBanner").className = "verdict-banner wait";
    $("verdictBanner").innerHTML = `<div class="headline">⏳ Could not analyse ${escapeHtml(tf)}</div>
      <div>${escapeHtml(err.message || "Not enough data for this timeframe yet — try another one.")}</div>`;
  }
}

async function loadChart(symbol) {
  const el = $("chart");
  if (typeof LightweightCharts === "undefined") {
    el.style.height = "auto";
    el.innerHTML = `<div class="notice">Chart library did not load (it comes from a CDN and needs
      internet access). Everything else on the dashboard works.</div>`;
    return;
  }
  if (!State.chart) {
    State.chart = LightweightCharts.createChart(el, {
      layout: { background: { color: "#151b23" }, textColor: "#8b97a8", fontFamily: "Inter" },
      grid: { vertLines: { color: "#1c242e" }, horzLines: { color: "#1c242e" } },
      rightPriceScale: { borderColor: "#253040" },
      timeScale: { borderColor: "#253040", timeVisible: true },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      height: 380,
    });
    State.candleSeries = State.chart.addCandlestickSeries({
      upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
      wickUpColor: "#26a69a", wickDownColor: "#ef5350",
    });
    new ResizeObserver(() => State.chart.applyOptions({ width: el.clientWidth })).observe(el);
  }
  try {
    const candles = await api(`/api/candles/${symbol}?timeframe=${State.timeframe}&bars=400`);
    State.candleSeries.setData(candles);
    State.chart.applyOptions({ width: el.clientWidth });
    State.chart.timeScale().fitContent();
    drawLevels(symbol);
  } catch { /* the table remains authoritative */ }
}

function drawLevels(symbol) {
  if (!State.candleSeries) return;
  const analysis = State.currentAnalysis;
  State.priceLines.forEach((l) => State.candleSeries.removePriceLine(l));
  State.priceLines = [];
  if (!analysis || analysis.symbol !== symbol || !analysis.risk) return;
  const risk = analysis.risk;
  [
    { price: risk.entry, color: "#4a9eff", title: "Entry" },
    { price: risk.stop_loss, color: "#ef5350", title: "Stop" },
    { price: risk.take_profit_1, color: "#26a69a", title: "TP1" },
    { price: risk.take_profit_2, color: "#26a69a", title: "TP2" },
  ].forEach((l) => {
    if (l.price === null || l.price === undefined) return;
    State.priceLines.push(
      State.candleSeries.createPriceLine({ ...l, lineWidth: 1, lineStyle: 2, axisLabelVisible: true })
    );
  });
}

/* -------------------------------------------------------------- indicators */

async function loadIndicators(symbol) {
  if (!State.chart) return;
  try {
    State.indicatorsData = await api(`/api/indicators/${symbol}?timeframe=${State.timeframe}&bars=400`);
  } catch {
    State.indicatorsData = null;
  }
  applyIndicators();
}

function ensureLineSeries(name, options) {
  if (!State.indicatorSeries[name]) {
    State.indicatorSeries[name] = State.chart.addLineSeries({ lineWidth: 1, ...options });
  }
  return State.indicatorSeries[name];
}

function removeLineSeries(name) {
  if (State.indicatorSeries[name]) {
    State.chart.removeSeries(State.indicatorSeries[name]);
    delete State.indicatorSeries[name];
  }
}

function applyIndicators() {
  if (!State.chart) return;
  const data = State.indicatorsData;

  const maSpecs = [
    ["sma20", { color: "#f5a623", title: "SMA 20" }],
    ["sma50", { color: "#4a9eff", title: "SMA 50" }],
    ["ema20", { color: "#c084fc", title: "EMA 20" }],
  ];
  maSpecs.forEach(([key, options]) => {
    if (State.indicatorsOn.ma && data && data[key] && data[key].length) {
      ensureLineSeries(key, options).setData(data[key]);
    } else {
      removeLineSeries(key);
    }
  });

  const ichimokuSpecs = [
    ["tenkan", { color: "#26a69a", title: "Tenkan-sen" }],
    ["kijun", { color: "#ef5350", title: "Kijun-sen" }],
    ["span_a", { color: "rgba(38,166,154,.6)", title: "Senkou A" }],
    ["span_b", { color: "rgba(239,83,80,.6)", title: "Senkou B" }],
  ];
  ichimokuSpecs.forEach(([key, options]) => {
    const series = data && data.ichimoku && data.ichimoku[key];
    if (State.indicatorsOn.ichimoku && series && series.length) {
      ensureLineSeries(`ichimoku_${key}`, options).setData(series);
    } else {
      removeLineSeries(`ichimoku_${key}`);
    }
  });

  const stSpecs = [
    ["st_up", { color: "#26a69a", title: "SuperTrend" }, "up"],
    ["st_down", { color: "#ef5350", title: "SuperTrend" }, "down"],
  ];
  stSpecs.forEach(([key, options, side]) => {
    const series = data && data.supertrend && data.supertrend[side];
    if (State.indicatorsOn.supertrend && series && series.length) {
      ensureLineSeries(key, { ...options, lineWidth: 2 }).setData(series);
    } else {
      removeLineSeries(key);
    }
  });

  applyMacd(data && data.macd);
}

function ensureMacdChart() {
  if (State.macdChart) return State.macdChart;
  const el = $("macdChart");
  State.macdChart = LightweightCharts.createChart(el, {
    layout: { background: { color: "#151b23" }, textColor: "#8b97a8", fontFamily: "Inter" },
    grid: { vertLines: { color: "#1c242e" }, horzLines: { color: "#1c242e" } },
    rightPriceScale: { borderColor: "#253040" },
    timeScale: { borderColor: "#253040", timeVisible: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    height: 120,
  });
  State.macdSeries.hist = State.macdChart.addHistogramSeries({ color: "#4a9eff" });
  State.macdSeries.macd = State.macdChart.addLineSeries({ color: "#f5a623", lineWidth: 1 });
  State.macdSeries.signal = State.macdChart.addLineSeries({ color: "#c084fc", lineWidth: 1 });
  new ResizeObserver(() => State.macdChart.applyOptions({ width: el.clientWidth })).observe(el);

  // Keep the two time scales in lock-step, guarding against the feedback loop
  // a naive two-way subscription would create.
  let syncing = false;
  const sync = (from, to) => (range) => {
    if (syncing || !range) return;
    syncing = true;
    to.timeScale().setVisibleLogicalRange(range);
    syncing = false;
  };
  State.chart.timeScale().subscribeVisibleLogicalRangeChange(sync(State.chart, State.macdChart));
  State.macdChart.timeScale().subscribeVisibleLogicalRangeChange(sync(State.macdChart, State.chart));

  return State.macdChart;
}

function applyMacd(macd) {
  const el = $("macdChart");
  if (!State.indicatorsOn.macd || !macd || !macd.macd || !macd.macd.length) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const chart = ensureMacdChart();
  chart.applyOptions({ width: el.clientWidth });
  State.macdSeries.macd.setData(macd.macd);
  State.macdSeries.signal.setData(macd.signal);
  State.macdSeries.hist.setData(
    (macd.hist || []).map((p) => ({ ...p, color: p.value >= 0 ? "#26a69a" : "#ef5350" }))
  );
  chart.timeScale().fitContent();
}

/* ------------------------------------------------------------------ stats */

async function renderStats() {
  let s;
  try { s = await api("/api/stats"); } catch { return; }
  const cells = [
    ["Resolved trades", s.sample],
    ["Currently open", s.open_count],
    ["Win rate", s.win_rate === null ? "sample too small" : fmtPct(s.win_rate)],
    ["Expectancy", s.expectancy_r === null ? "—" : `${s.expectancy_r >= 0 ? "+" : ""}${s.expectancy_r.toFixed(2)}R`],
    ["Profit factor", s.profit_factor ?? "—"],
    ["Average MFE", s.avg_mfe_r === null ? "—" : `${s.avg_mfe_r}R`],
  ];
  $("stats").innerHTML =
    `<div class="stat" style="grid-column:1/-1"><div class="label">Summary</div>
       <div style="font-size:14px">${escapeHtml(s.headline)}</div></div>` +
    cells.map(([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
}

/* ================================================================ COMPANIES */

async function loadCompanies() {
  try { State.companies = await api("/api/companies"); } catch { return; }
  const rows = State.companies;
  if (!rows.length) {
    $("companyBody").innerHTML =
      `<tr><td colspan="9" class="notice">No companies yet. Use “Add company” to register one.</td></tr>`;
    return;
  }
  $("companyBody").innerHTML = rows.map((c) => {
    const yieldPct = c.price && c.dividend_per_share ? c.dividend_per_share / c.price : null;
    const payout = c.eps && c.dividend_per_share && c.eps > 0 ? c.dividend_per_share / c.eps : null;
    return `
      <tr data-company="${escapeHtml(c.symbol)}" class="${State.selectedCompany === c.symbol ? "selected" : ""}">
        <td class="sym">${escapeHtml(c.symbol)}</td>
        <td>${escapeHtml(c.name)}</td>
        <td class="neutral">${escapeHtml(c.sector || "—")}</td>
        <td class="num">${fmtNum(c.price, 4)}</td>
        <td class="num">${yieldPct === null ? "—" : (yieldPct * 100).toFixed(2) + "%"}</td>
        <td class="num">${payout === null ? "—" : Math.round(payout * 100) + "%"}</td>
        <td class="num">${c.news_count}</td>
        <td class="neutral">${c.updated_at.slice(0, 10)}</td>
        <td>
          <button class="icon-btn" data-edit="${escapeHtml(c.symbol)}" title="Edit">✎</button>
          <button class="icon-btn" data-del="${escapeHtml(c.symbol)}" title="Delete">✕</button>
        </td>
      </tr>`;
  }).join("");

  document.querySelectorAll("#companyBody tr[data-company]").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.dataset.edit) return editCompany(e.target.dataset.edit);
      if (e.target.dataset.del) return removeCompany(e.target.dataset.del);
      selectCompany(tr.dataset.company);
    })
  );
}

function showCompanyForm(company) {
  const form = $("cForm");
  form.reset();
  $("companyFormTitle").textContent = company ? `Edit ${company.symbol}` : "Add company";
  if (company) {
    Object.entries(company).forEach(([k, v]) => {
      if (form.elements[k] && v !== null && v !== undefined) form.elements[k].value = v;
    });
    form.elements.symbol.readOnly = true;
  } else {
    form.elements.symbol.readOnly = false;
    form.elements.currency.value = "OMR";
  }
  $("companyForm").hidden = false;
  $("companyForm").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

const editCompany = (symbol) =>
  showCompanyForm(State.companies.find((c) => c.symbol === symbol));

async function removeCompany(symbol) {
  if (!confirm(`Remove ${symbol} and all its announcements?`)) return;
  await api(`/api/companies/${symbol}`, { method: "DELETE" });
  if (State.selectedCompany === symbol) {
    State.selectedCompany = null;
    $("companyDetail").hidden = true;
  }
  await loadCompanies();
}

async function selectCompany(symbol) {
  State.selectedCompany = symbol;
  await loadCompanies();
  $("companyDetail").hidden = false;

  const [assessment, news] = await Promise.all([
    api(`/api/companies/${symbol}/assessment`),
    api(`/api/companies/${symbol}/news`),
  ]);

  $("companyTitle").textContent = `${assessment.name} (${assessment.symbol})`;
  const badge = GRADE_CLASS[assessment.grade] || "g-NO";
  $("companyVerdict").innerHTML =
    `<span class="badge ${badge}">${assessment.grade}</span> ` +
    `${DIRECTION[String(assessment.direction)].label} · ${fmtPct(assessment.confidence)}`;

  const dir = DIRECTION[String(assessment.direction)] || DIRECTION[0];
  const banner = $("companySummary");
  banner.className = `verdict-banner ${assessment.direction === 0 ? "wait" : "go"}`;
  banner.innerHTML = `<div class="headline">${dir.icon} ${dir.label} read — ${fmtPct(assessment.confidence)} confidence</div>
    <div>${escapeHtml(assessment.summary || "Nothing has been entered for this company yet.")}</div>`;

  contributionBars(
    assessment.engines.map((e) => ({
      engine: e.engine,
      contribution: e.direction * e.strength * e.quality,
      skipped_reason: e.skipped_reason,
    })),
    $("companyEngines")
  );
  renderGates(assessment.gates, $("companyGates"));
  $("companyReport").textContent = assessment.report;
  renderNews(news);
}

function renderNews(items) {
  if (!items.length) {
    $("newsList").innerHTML = `<li class="notice">No announcements recorded yet.</li>`;
    return;
  }
  $("newsList").innerHTML = items.map((n) => {
    const cls = n.sentiment > 0 ? "bull" : n.sentiment < 0 ? "bear" : "neutral";
    const terms = [...(n.matched_terms.positive || []), ...(n.matched_terms.negative || [])];
    const rtl = ARABIC.test(n.headline) ? " rtl" : "";
    const meta = [
      n.published_at.slice(0, 10),
      n.source,
      n.manual_sentiment !== null ? "manually set" : terms.length ? `matched: ${terms.slice(0, 4).join(", ")}` : "no terms matched",
    ].filter(Boolean).join(" · ");
    return `
      <li>
        <span class="news-score ${cls}">${n.sentiment >= 0 ? "+" : ""}${n.sentiment.toFixed(2)}</span>
        <div>
          <div class="news-body${rtl}">${escapeHtml(n.headline)}</div>
          <div class="news-meta">${escapeHtml(meta)}</div>
        </div>
        <button class="icon-btn" data-news="${n.id}" title="Delete">✕</button>
      </li>`;
  }).join("");

  document.querySelectorAll("#newsList button[data-news]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/news/${b.dataset.news}`, { method: "DELETE" });
      await selectCompany(State.selectedCompany);
    })
  );
}

/* ----------------------------------------------------------------- events */

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("tab-markets").hidden = tab.dataset.tab !== "markets";
    $("tab-companies").hidden = tab.dataset.tab !== "companies";
  })
);

$("refreshBtn").addEventListener("click", refresh);
$("aiBtn").addEventListener("click", () => { if (State.selected) loadAI(State.selected); });
$("aiChatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("aiChatInput");
  const text = input.value;
  input.value = "";
  sendChatMessage(text);
});
$("search").addEventListener("input", renderTable);
$("onlyActionable").addEventListener("change", renderTable);
document.querySelectorAll("thead th[data-sort]").forEach((th) =>
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    State.sortDir = State.sortKey === key ? -State.sortDir : -1;
    State.sortKey = key;
    renderTable();
  })
);
$("runBtn").addEventListener("click", async () => {
  const btn = $("runBtn");
  btn.disabled = true;
  btn.textContent = "Analysing…";
  try { await postJson("/api/run", {}); await refresh(); }
  finally { btn.disabled = false; btn.textContent = "Run analysis"; }
});

$("newCompanyBtn").addEventListener("click", () => showCompanyForm(null));
$("cancelCompany").addEventListener("click", () => { $("companyForm").hidden = true; });

$("cForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target).entries());
  // Empty strings must become null, not 0: "not supplied" and "zero" are
  // different statements, and the engines treat them differently.
  const numeric = ["price", "dividend_per_share", "previous_dividend_per_share", "eps",
                   "previous_eps", "book_value_per_share", "debt_to_equity",
                   "profit_margin", "revenue_growth", "dividend_years_paid", "dividend_years_cut"];
  numeric.forEach((k) => { data[k] = data[k] === "" ? null : Number(data[k]); });
  try {
    await postJson("/api/companies", data);
    $("companyForm").hidden = true;
    await loadCompanies();
    await selectCompany(data.symbol.toUpperCase());
  } catch (err) {
    alert(err.message);
  }
});

$("newsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!State.selectedCompany) return;
  const data = Object.fromEntries(new FormData(e.target).entries());
  const payload = { headline: data.headline, source: data.source };
  if (data.published_at) payload.published_at = `${data.published_at}T12:00:00+00:00`;
  try {
    await postJson(`/api/companies/${State.selectedCompany}/news`, payload);
    e.target.reset();
    $("sentimentPreview").innerHTML = "";
    await selectCompany(State.selectedCompany);
  } catch (err) {
    alert(err.message);
  }
});

let previewTimer = null;
$("newsForm").elements.headline.addEventListener("input", (e) => {
  clearTimeout(previewTimer);
  const text = e.target.value.trim();
  e.target.classList.toggle("rtl", ARABIC.test(text));
  if (!text) { $("sentimentPreview").innerHTML = ""; return; }
  previewTimer = setTimeout(async () => {
    try {
      const r = await postJson("/api/sentiment/preview", { headline: text });
      const cls = r.sentiment > 0 ? "pos" : r.sentiment < 0 ? "neg" : "";
      const terms = [...(r.matched_terms.positive || []), ...(r.matched_terms.negative || [])];
      $("sentimentPreview").innerHTML =
        `Sentiment <b class="${cls}">${r.sentiment >= 0 ? "+" : ""}${r.sentiment.toFixed(2)}</b>` +
        (terms.length ? ` — matched: ${escapeHtml(terms.join(", "))}` : " — no scoring terms matched");
    } catch { /* preview is best-effort */ }
  }, 300);
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

boot();
