/* ==========================================================================
   محلل الأسواق — منطق اللوحة
   تقرأ من /api فقط، ولا تحسب أي شيء بنفسها: مصدر الحقيقة واحد وهو الخادم.
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
  tz: "Asia/Muscat",
};

const $ = (id) => document.getElementById(id);
const fmtPct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const fmtNum = (v, d = 5) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d });

const DIRECTION = {
  1: { label: "صاعد", cls: "bull", icon: "▲" },
  0: { label: "محايد", cls: "neutral", icon: "—" },
  "-1": { label: "هابط", cls: "bear", icon: "▼" },
};

const GRADE_CLASS = { "A+": "g-Aplus", A: "g-A", B: "g-B", C: "g-C", NO_TRADE: "g-NO" };

const ENGINE_LABELS = {
  trend: "الاتجاه متعدد الفريمات",
  ict_smc: "ICT / السيولة والبنية",
  classic_ta: "التحليل الكلاسيكي",
  indicators: "المؤشرات الفنية",
  macro: "الكلي والترابط",
  cot: "تموضع المضاربين",
  volume_seasonality: "الحجم والموسمية",
  fundamentals: "التحليل الأساسي",
  news: "الأخبار والتقويم",
};

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  try {
    const health = await api("/api/health");
    State.tz = health.timezone || State.tz;
    $("profileLine").textContent =
      `الملف التعريفي: ${health.profile} · إصدار الإعدادات ${health.config_version}` +
      (health.offline ? " · وضع بيانات تركيبية" : "");
    $("version").textContent = health.version;
  } catch {
    $("profileLine").textContent = "تعذّر الاتصال بالخادم";
  }
  tickClock();
  setInterval(tickClock, 1000);
  await refresh();
  setInterval(refresh, 60000);
}

function tickClock() {
  $("clock").textContent = new Intl.DateTimeFormat("ar", {
    timeZone: State.tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date());
}

async function refresh() {
  try {
    State.rows = await api("/api/analyses");
  } catch {
    $("tbody").innerHTML = `<tr><td colspan="8" class="notice">تعذّر جلب التحليلات.</td></tr>`;
    return;
  }
  renderKpis();
  renderTable();
  renderStats();
  if (State.selected) {
    const row = State.rows.find((r) => r.symbol === State.selected);
    if (row) renderDetail(row);
  }
}

/* ------------------------------------------------------------------- KPIs */

function renderKpis() {
  const rows = State.rows;
  const actionable = rows.filter((r) => r.actionable);
  const blocked = rows.filter((r) => !r.actionable && r.blocking_failures.length);
  const newest = rows.reduce((a, r) => (!a || r.as_of > a ? r.as_of : a), null);

  $("kpis").innerHTML = [
    kpi("فرص مؤهلة الآن", actionable.length, actionable.length ? actionable.map((r) => r.symbol).join("، ") : "الانتظار قرار صحيح"),
    kpi("رموز تحت المتابعة", rows.length, "من ملف watchlist.yaml"),
    kpi("موقوفة ببوابة صلبة", blocked.length, "درجة عالية لا تكفي وحدها"),
    kpi("آخر تحديث", newest ? new Intl.DateTimeFormat("ar", { timeZone: State.tz, hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(newest)) : "—", "يُحدَّث تلقائياً حسب الجدولة"),
  ].join("");
}

const kpi = (label, value, hint) =>
  `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;

/* ------------------------------------------------------------------ table */

function renderTable() {
  const term = $("search").value.trim().toLowerCase();
  const onlyActionable = $("onlyActionable").checked;

  let rows = State.rows.filter((r) => {
    if (onlyActionable && !r.actionable) return false;
    if (!term) return true;
    return r.symbol.toLowerCase().includes(term) || (r.name_ar || "").includes(term);
  });

  rows.sort((a, b) => {
    const x = a[State.sortKey], y = b[State.sortKey];
    if (typeof x === "string") return State.sortDir * x.localeCompare(y, "ar");
    return State.sortDir * ((x ?? 0) - (y ?? 0));
  });

  if (!rows.length) {
    $("tbody").innerHTML = `<tr><td colspan="8" class="notice">لا توجد نتائج مطابقة.</td></tr>`;
    return;
  }

  $("tbody").innerHTML = rows.map((r) => {
    const d = DIRECTION[String(r.direction)] || DIRECTION[0];
    const colour = r.direction > 0 ? "var(--bull)" : r.direction < 0 ? "var(--bear)" : "var(--neutral)";
    const status = r.actionable
      ? `<span class="ok">✅ مؤهلة</span>`
      : r.blocking_failures.length
      ? `<span class="blocked" title="${escapeHtml(r.blocking_failures[0].detail_ar)}">⛔ ${escapeHtml(r.blocking_failures[0].label_ar)}</span>`
      : `<span class="neutral">—</span>`;
    return `
      <tr data-symbol="${r.symbol}" class="${State.selected === r.symbol ? "selected" : ""}">
        <td class="sym">${r.symbol}<small>${escapeHtml(r.name_ar || "")}</small></td>
        <td class="${d.cls}">${d.icon} ${d.label}</td>
        <td>
          <div class="conf">
            <div class="conf-track"><div class="conf-fill" style="width:${Math.round((r.confidence || 0) * 100)}%;background:${colour}"></div></div>
            <span class="num">${fmtPct(r.confidence)}</span>
          </div>
        </td>
        <td><span class="badge ${GRADE_CLASS[r.grade] || "g-NO"}">${r.grade}</span></td>
        <td class="neutral">${escapeHtml(regimeLabel(r.regime))}</td>
        <td class="num">${fmtPct(r.coverage_ratio)} <small class="neutral">(${r.active_engines ?? 0})</small></td>
        <td class="num">${fmtNum(r.spot)}</td>
        <td>${status}</td>
      </tr>`;
  }).join("");

  document.querySelectorAll("#tbody tr[data-symbol]").forEach((tr) =>
    tr.addEventListener("click", () => selectSymbol(tr.dataset.symbol))
  );
}

const REGIMES = { trending: "سوق اتجاهي", ranging: "سوق عرضي", quiet: "سوق هادئ", high_volatility: "تقلب مرتفع" };
const regimeLabel = (r) => REGIMES[r] || r || "—";

/* ----------------------------------------------------------------- detail */

async function selectSymbol(symbol) {
  State.selected = symbol;
  renderTable();
  const row = State.rows.find((r) => r.symbol === symbol);
  if (row) renderDetail(row);
  await loadReport(symbol);
  await loadChart(symbol);
}

function renderDetail(row) {
  $("detailPanel").hidden = false;
  $("detailTitle").textContent = `${row.name_ar} (${row.symbol})`;

  $("tfButtons").innerHTML = ["1h", "4h", "1d"].map(
    (tf) => `<button class="tf-btn ${tf === State.timeframe ? "active" : ""}" data-tf="${tf}">${
      { "1h": "ساعة", "4h": "4 ساعات", "1d": "يومي" }[tf]
    }</button>`
  ).join("");
  document.querySelectorAll(".tf-btn").forEach((b) =>
    b.addEventListener("click", () => { State.timeframe = b.dataset.tf; renderDetail(row); loadChart(row.symbol); })
  );

  const contributions = (row.contributions || []).slice().sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  const max = Math.max(0.4, ...contributions.map((c) => Math.abs(c.contribution)));

  $("contributions").innerHTML = contributions.map((c) => {
    if (c.skipped_reason) {
      return `<div class="contrib-skipped">➖ ${ENGINE_LABELS[c.engine] || c.engine}: ${escapeHtml(c.skipped_reason)}</div>`;
    }
    const pct = (Math.abs(c.contribution) / max) * 50;
    const positive = c.contribution >= 0;
    const colour = positive ? "var(--bull)" : "var(--bear)";
    // Bar grows from the centre: right of centre = bullish (RTL layout).
    const side = positive ? `right:50%;width:${pct}%` : `left:50%;width:${pct}%`;
    return `
      <div class="contrib-row">
        <span>${ENGINE_LABELS[c.engine] || c.engine}</span>
        <div class="contrib-bar"><span class="zero"></span><span style="${side};background:${colour}"></span></div>
        <span class="num ${positive ? "bull" : "bear"}">${c.contribution >= 0 ? "+" : ""}${c.contribution.toFixed(2)}</span>
      </div>`;
  }).join("");
}

async function loadReport(symbol) {
  try {
    const data = await api(`/api/analysis/${symbol}`);
    $("report").textContent = data.report_ar || "—";
    const gates = (data.payload && data.payload.gates) || [];
    $("gates").innerHTML = gates.map((g) => {
      const icon = g.status === "passed" ? "✅" : g.status === "failed" ? "❌" : "➖";
      const tag = g.blocking ? "" : " <small class='neutral'>(تحذيرية)</small>";
      return `<li>${icon} ${escapeHtml(g.label_ar)}${tag}<span class="detail">${escapeHtml(g.detail_ar || "")}</span></li>`;
    }).join("");
  } catch {
    $("report").textContent = "تعذّر جلب التقرير.";
  }
}

async function loadChart(symbol) {
  const el = $("chart");
  if (typeof LightweightCharts === "undefined") {
    // Offline or CDN blocked: collapse the box rather than leaving a tall void.
    el.style.height = "auto";
    el.innerHTML = `<div class="notice">مكتبة الرسم لم تُحمّل (تحتاج اتصالاً بالإنترنت لتحميلها من CDN). بقية اللوحة تعمل بشكل كامل.</div>`;
    return;
  }
  if (!State.chart) {
    State.chart = LightweightCharts.createChart(el, {
      layout: { background: { color: "#151b23" }, textColor: "#8b97a8", fontFamily: "IBM Plex Sans Arabic" },
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
  } catch {
    /* chart stays empty; the table remains authoritative */
  }
}

function drawLevels(symbol) {
  const row = State.rows.find((r) => r.symbol === symbol);
  State.candleSeries.setMarkers([]);
  (State.priceLines || []).forEach((l) => State.candleSeries.removePriceLine(l));
  State.priceLines = [];
  if (!row || !row.risk) return;
  const lines = [
    { price: row.risk.entry, color: "#4a9eff", title: "دخول" },
    { price: row.risk.stop_loss, color: "#ef5350", title: "وقف" },
    { price: row.risk.take_profit_1, color: "#26a69a", title: "هدف 1" },
    { price: row.risk.take_profit_2, color: "#26a69a", title: "هدف 2" },
  ];
  lines.forEach((l) => {
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
    ["صفقات محسومة", s.sample],
    ["مفتوحة الآن", s.open_count],
    ["نسبة الإصابة", s.win_rate === null ? "عينة غير كافية" : fmtPct(s.win_rate)],
    ["التوقّع لكل صفقة", s.expectancy_r === null ? "—" : `${s.expectancy_r >= 0 ? "+" : ""}${s.expectancy_r.toFixed(2)}R`],
    ["عامل الربح", s.profit_factor ?? "—"],
    ["متوسط MFE", s.avg_mfe_r === null ? "—" : `${s.avg_mfe_r}R`],
  ];
  $("stats").innerHTML =
    `<div class="stat" style="grid-column:1/-1"><div class="label">الخلاصة</div><div style="font-size:14px">${escapeHtml(s.headline_ar)}</div></div>` +
    cells.map(([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
}

/* ----------------------------------------------------------------- events */

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

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
  btn.textContent = "جارٍ التحليل…";
  try { await api("/api/run", { method: "POST" }); await refresh(); }
  finally { btn.disabled = false; btn.textContent = "تشغيل تحليل الآن"; }
});

boot();
