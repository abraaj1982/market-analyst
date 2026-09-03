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
  priceLines: [],
  tz: "Asia/Muscat",
  companies: [],
  selectedCompany: null,
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
    const row = State.rows.find((r) => r.symbol === State.selected);
    if (row) renderDetail(row);
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
  const row = State.rows.find((r) => r.symbol === symbol);
  if (row) renderDetail(row);
  await loadReport(symbol);
  await loadChart(symbol);
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

  $("tfButtons").innerHTML = ["1h", "4h", "1d"].map(
    (tf) => `<button class="tf-btn ${tf === State.timeframe ? "active" : ""}" data-tf="${tf}">${
      { "1h": "1H", "4h": "4H", "1d": "1D" }[tf]
    }</button>`
  ).join("");
  document.querySelectorAll(".tf-btn").forEach((b) =>
    b.addEventListener("click", () => { State.timeframe = b.dataset.tf; renderDetail(row); loadChart(row.symbol); })
  );

  contributionBars(row.contributions || [], $("contributions"));
}

function renderGates(gates, container) {
  container.innerHTML = gates.map((g) => {
    const icon = g.status === "passed" ? "✅" : g.status === "failed" ? "❌" : "—";
    const tag = g.blocking ? "" : " <small class='neutral'>(advisory)</small>";
    return `<li>${icon} ${escapeHtml(g.label)}${tag}<span class="detail">${escapeHtml(g.detail || "")}</span></li>`;
  }).join("");
}

async function loadReport(symbol) {
  try {
    const data = await api(`/api/analysis/${symbol}`);
    $("report").textContent = data.report || "—";
    renderGates((data.payload && data.payload.gates) || [], $("gates"));
  } catch {
    $("report").textContent = "Could not load the report.";
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
  const row = State.rows.find((r) => r.symbol === symbol);
  State.priceLines.forEach((l) => State.candleSeries.removePriceLine(l));
  State.priceLines = [];
  if (!row || !row.risk) return;
  [
    { price: row.risk.entry, color: "#4a9eff", title: "Entry" },
    { price: row.risk.stop_loss, color: "#ef5350", title: "Stop" },
    { price: row.risk.take_profit_1, color: "#26a69a", title: "TP1" },
    { price: row.risk.take_profit_2, color: "#26a69a", title: "TP2" },
  ].forEach((l) => {
    if (l.price === null || l.price === undefined) return;
    State.priceLines.push(
      State.candleSeries.createPriceLine({ ...l, lineWidth: 1, lineStyle: 2, axisLabelVisible: true })
    );
  });
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

boot();
