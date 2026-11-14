"use strict";

/* Millennium Mutual: money ledger (frontend)
   Talks to the Python API in backend/app.py. No build step, no framework. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  meta: null,
  filters: {},
  periodFrom: null,
  periodTo: null,
  periodToIsLatest: true,
  assumptions: { expense_ratio: 0.25, broker_commission: 0.12 },
  dim: "city_product",
  heatRows: "city",
  heatCols: "policy_type",
  sort: null,
  data: null,
  charts: {},
  request: 0,
  manualKind: "policy",
  lastCheck: null,
  checkLens: "where",
};

const FILTER_GROUPS = [
  { title: "Where", open: true, fields: [["city", "City"]] },
  { title: "What", open: true, fields: [["policy_type", "Product"], ["risk_band", "Risk band"], ["deductible", "Deductible"]] },
  { title: "Who sold it", open: true, fields: [["channel", "Channel"], ["source", "Broker or direct"]] },
  { title: "Customer", open: false, fields: [["age_band", "Age"], ["renewal_status", "Renewal status"]] },
  {
    title: "Claims only", open: false, note: "These narrow the money going out, not the premium.",
    fields: [["claim_type", "Claim type"], ["adjuster_team", "Adjuster team"], ["status", "Claim status"]],
  },
];
const FIELD_LABEL = Object.fromEntries(FILTER_GROUPS.flatMap((g) => g.fields));
const NATURAL_ORDER = new Set(["risk_band", "age_band", "deductible", "renewal_status"]);

/* ---------------------------------------------------------------- format */
function money(x) {
  if (x == null || Number.isNaN(x)) return "–";
  const a = Math.abs(x);
  let s;
  if (a >= 1e6) s = "$" + (a / 1e6).toFixed(2) + "M";
  else if (a >= 1e5) s = "$" + Math.round(a / 1e3) + "K";
  else if (a >= 1e4) s = "$" + (a / 1e3).toFixed(1) + "K";
  else s = "$" + Math.round(a).toLocaleString("en-CA");
  return x < 0 ? "(" + s + ")" : s;
}
const moneyFull = (x) => (x == null ? "–" : (x < 0 ? "(" : "") + "$" + Math.round(Math.abs(x)).toLocaleString("en-CA") + (x < 0 ? ")" : ""));
const pct = (x, d = 0) => (x == null || Number.isNaN(x) ? "–" : (x * 100).toFixed(d) + "%");
const cents = (x) => (x == null ? "–" : Math.abs(x) >= 1 ? "$" + Math.abs(x).toFixed(2) : Math.round(Math.abs(x) * 100) + "¢");
const int = (x) => (x == null ? "–" : Math.round(x).toLocaleString("en-CA"));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function dateLabel(iso) {
  if (!iso) return "–";
  return new Date(iso + "T00:00:00").toLocaleDateString("en-CA", { month: "short", day: "numeric", year: "numeric" });
}
function monthLabel(ym) {
  const [y, m] = ym.split("-");
  return new Date(+y, +m - 1, 1).toLocaleDateString("en-CA", { month: "short" }) + " ’" + y.slice(2);
}
const optionLabel = (col, v) => (col === "deductible" ? "$" + Number(v).toLocaleString("en-CA") : String(v));
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const moneyClass = (x) => (x != null && x < 0 ? "num loss" : "num");

/* ------------------------------------------------------------------- api */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error(body && body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `Request failed (${res.status})`);
  return body;
}
const postJSON = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });

let toastTimer;
function toast(message, error = false) {
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", error);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, error ? 7000 : 4000);
}

/* ----------------------------------------------------------------- theme */
function currentTheme() {
  const forced = document.documentElement.dataset.theme;
  if (forced) return forced;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function initTheme() {
  try { const saved = localStorage.getItem("mm-theme"); if (saved) document.documentElement.dataset.theme = saved; } catch (e) { /* ignore */ }
  $("#theme-toggle").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("mm-theme", next); } catch (e) { /* ignore */ }
    applyChartDefaults();
    if (state.data) renderLedger();
    if (state.lastCheck) renderCheckChart();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    applyChartDefaults();
    if (state.data) renderLedger();
  });
}
function applyChartDefaults() {
  Chart.defaults.font.family = cssVar("--font");
  Chart.defaults.font.size = 12;
  Chart.defaults.color = cssVar("--ink-2");
  Chart.defaults.borderColor = cssVar("--rule");
}

/* ----------------------------------------------------------------- views */
function showView(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (location.hash !== "#" + name) history.pushState(null, "", "#" + name);
  if (name === "alerts") loadAlerts();
  if (name === "notes") loadNotes();
  window.scrollTo({ top: 0 });
}
function bindTabs() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
}

/* ------------------------------------------------------------------ meta */
async function loadMeta() {
  const meta = await api("/api/meta");
  state.meta = meta;
  $("#asof").textContent = dateLabel(meta.asof);
  $("#alert-count").textContent = meta.alerts_new ? meta.alerts_new : "";

  const quarters = meta.quarters;
  const last = quarters[quarters.length - 1].value;
  if (!state.periodFrom || !quarters.some((q) => q.value === state.periodFrom)) state.periodFrom = quarters[0].value;
  if (state.periodToIsLatest || !quarters.some((q) => q.value === state.periodTo)) state.periodTo = last;
  for (const id of ["#period-from", "#period-to"]) {
    $(id).innerHTML = quarters.map((q) => `<option value="${q.value}">${esc(q.label.replace(/ \(.*\)$/, ""))}</option>`).join("");
  }
  $("#check-quarter").innerHTML = quarters.map((q) => `<option value="${q.value}">${esc(q.label)}</option>`).join("");
  $("#period-from").value = state.periodFrom;
  $("#period-to").value = state.periodTo;
  $("#check-quarter").value = last;

  const dims = meta.dimensions;
  $("#dim").innerHTML = dims.map((d) => `<option value="${d.value}">${esc(d.label.toLowerCase())}</option>`).join("");
  $("#dim").value = state.dim;
  const policyDims = dims.filter((d) => d.scope === "policy" && !d.value.includes("_product") && d.value !== "product_risk");
  for (const id of ["#heat-rows", "#heat-cols"]) {
    $(id).innerHTML = policyDims.map((d) => `<option value="${d.value}">${esc(d.label.toLowerCase())}</option>`).join("");
  }
  $("#heat-rows").value = state.heatRows;
  $("#heat-cols").value = state.heatCols;

  $("#a-expense").value = Math.round(state.assumptions.expense_ratio * 100);
  $("#a-commission").value = +(state.assumptions.broker_commission * 100).toFixed(1);

  const r = meta.rules;
  $("#r-materiality").value = r.materiality;
  $("#r-confidence").value = String(r.confidence);
  $("#r-min_earned").value = r.min_earned;
  $("#r-min_claims").value = r.min_claims;
  $("#r-large_loss_cap").value = r.large_loss_cap;
  $("#r-worsening_points").value = r.worsening_points;
  $("#r-band_min_share").value = Math.round(r.band_min_share * 100);

  $("#manual-policy [name=city]").innerHTML = meta.options.city.map((c) => `<option>${esc(c)}</option>`).join("");
  $("#manual-claim [name=claim_type]").innerHTML = meta.options.claim_type.map((c) => `<option>${esc(c)}</option>`).join("");
  $("#sample-button").hidden = !meta.sample_available;
  buildFilters();
}

/* --------------------------------------------------------------- filters */
function buildFilters() {
  const opts = state.meta.options;
  const openNow = {};
  $$("#filter-groups details").forEach((d) => { openNow[d.dataset.title] = d.open; });
  $("#filter-groups").innerHTML = FILTER_GROUPS.map((g) => {
    const open = openNow[g.title] ?? g.open;
    const fields = g.fields.map(([col, label]) => {
      const chips = (opts[col] || []).map((v) => {
        const checked = (state.filters[col] || []).map(String).includes(String(v)) ? "checked" : "";
        return `<label class="chip"><input type="checkbox" data-col="${col}" value="${esc(v)}" ${checked}><span>${esc(optionLabel(col, v))}</span></label>`;
      }).join("");
      return `<div class="field"><span class="field-label">${esc(label)}</span><div class="chips">${chips}</div></div>`;
    }).join("");
    const note = g.note ? `<p class="hint" style="margin-top:.35rem">${esc(g.note)}</p>` : "";
    return `<details class="group" data-title="${esc(g.title)}" ${open ? "open" : ""}><summary>${esc(g.title)}<span class="count-on"></span></summary>${note}${fields}</details>`;
  }).join("");
  $$("#filter-groups input[type=checkbox]").forEach((cb) => cb.addEventListener("change", onChipChange));
  updateGroupCounts();
}
function onChipChange(e) {
  const col = e.target.dataset.col;
  const values = $$(`#filter-groups input[data-col="${col}"]:checked`).map((cb) => (col === "deductible" ? Number(cb.value) : cb.value));
  if (values.length) state.filters[col] = values; else delete state.filters[col];
  updateGroupCounts();
  scheduleLedger();
}
function updateGroupCounts() {
  $$("#filter-groups details").forEach((d) => {
    const n = $$("input:checked", d).length;
    $(".count-on", d).textContent = n ? `${n} on` : "";
  });
}
function setFilters(newFilters, { replace = false } = {}) {
  if (replace) state.filters = {};
  for (const [col, values] of Object.entries(newFilters)) {
    if (values && values.length) state.filters[col] = values; else delete state.filters[col];
  }
  buildFilters();
  scheduleLedger(0);
}
function renderActiveFilters() {
  const pills = Object.entries(state.filters).flatMap(([col, values]) =>
    values.map((v) => `<span class="pill">${esc(FIELD_LABEL[col] || col)}: ${esc(optionLabel(col, v))}<button type="button" aria-label="Remove ${esc(optionLabel(col, v))}" data-col="${col}" data-value="${esc(v)}">×</button></span>`));
  $("#active-filters").innerHTML = pills.length
    ? `<span class="hint">Showing only:</span>${pills.join("")}`
    : `<span class="hint">Showing all business. Use the filters, or click any row or cell, to narrow it down.</span>`;
  $$("#active-filters button").forEach((b) => b.addEventListener("click", () => {
    const col = b.dataset.col;
    const keep = (state.filters[col] || []).filter((v) => String(v) !== b.dataset.value);
    setFilters({ [col]: keep });
  }));
}
function describeSelection() {
  const parts = Object.entries(state.filters).map(([col, values]) => {
    const vals = values.map((v) => optionLabel(col, v)).join(" or ");
    if (col === "source") return values.every((v) => /^B\d/.test(v)) ? `Broker ${vals}` : vals;
    return vals;
  });
  return parts.length ? parts.join(", ") : "All business";
}

/* ---------------------------------------------------------------- ledger */
let ledgerTimer;
function scheduleLedger(delay = 150) {
  clearTimeout(ledgerTimer);
  ledgerTimer = setTimeout(loadLedger, delay);
}
async function loadLedger() {
  const id = ++state.request;
  $("#ledger-main").classList.add("loading");
  try {
    const data = await postJSON("/api/ledger", {
      filters: { ...state.filters, period_from: state.periodFrom, period_to: state.periodTo },
      dim: state.dim, heat_rows: state.heatRows, heat_cols: state.heatCols, assumptions: state.assumptions,
    });
    if (id !== state.request) return;
    state.data = data;
    renderLedger();
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (id === state.request) $("#ledger-main").classList.remove("loading");
  }
}
function renderLedger() {
  renderActiveFilters();
  renderDollar();
  renderTotals();
  renderMonthly();
  renderTable();
  renderHeatmap();
  renderHist();
}

function renderDollar() {
  const d = state.data, t = d.totals, pd = d.per_dollar;
  const win = `${dateLabel(d.window.from)} to ${dateLabel(d.window.to)}`;
  $("#dollar-sub").textContent = `${describeSelection()}. ${win}.`;
  const bar = $("#dollar-bar"), scale = $("#dollar-scale");
  if (!t.earned) {
    bar.innerHTML = ""; scale.innerHTML = ""; $("#dollar-legend").innerHTML = "";
    $("#dollar-summary").textContent = "No premium was earned for this selection. Widen the period or remove a filter.";
    return;
  }
  const parts = [
    ["paid", pd.paid, "Claims paid", t.paid],
    ["open", pd.reserve, "Open claims", t.reserve],
    ["commission", pd.commission, "Broker commission", t.commission],
    ["costs", pd.expenses, "Operating costs", t.expenses],
  ];
  const out = parts.reduce((s, p) => s + p[1], 0);
  const total = Math.max(1, out);
  const w = (v) => (v / total) * 100;
  let html = parts.map(([cls, v, label]) => {
    const tight = w(v) < 9 ? " tight" : "";
    return `<div class="seg ${cls}${tight}" style="flex-basis:${w(v)}%"><b>${cents(v)}</b><span>${label}</span></div>`;
  }).join("");
  if (pd.result >= 0) {
    const tight = w(pd.result) < 9 ? " tight" : "";
    html += `<div class="seg kept${tight}" style="flex-basis:${w(pd.result)}%"><b>${cents(pd.result)}</b><span>Left over</span></div>`;
  } else {
    html += `<div class="one-mark" style="left:${w(1)}%"></div>`;
    html += `<div class="seg lost" style="position:absolute;left:${w(1)}%;right:0;top:0;bottom:0;opacity:.55"></div>`;
  }
  bar.innerHTML = html;
  const tickEvery = total <= 1.3 ? 0.1 : total <= 3 ? 0.5 : total <= 6 ? 1 : 2;
  bar.style.setProperty("--tick", `${w(tickEvery)}%`);
  bar.style.backgroundImage = `repeating-linear-gradient(90deg, transparent 0 calc(${w(tickEvery)}% - 1px), ${cssVar("--rule")} calc(${w(tickEvery)}% - 1px) ${w(tickEvery)}%)`;
  bar.setAttribute("aria-label", `Of each premium dollar: ${parts.map((p) => `${p[2]} ${cents(p[1])}`).join(", ")}; ${pd.result >= 0 ? "left over " + cents(pd.result) : "lost " + cents(-pd.result)}.`);
  const ticks = [];
  const narrow = bar.clientWidth < 640;  // phones: fewer, shorter labels so they don't collide
  const labelEvery = total <= 1.3 ? (narrow ? 0.5 : 0.25) : (narrow ? tickEvery * 2 : tickEvery);
  for (let v = 0; v <= total + 1e-9; v += labelEvery) ticks.push(+v.toFixed(2));
  if (!ticks.includes(1)) ticks.push(1);
  scale.innerHTML = ticks.map((v) => {
    const pos = Math.min(w(v), 100);
    const shift = pos <= 2 ? "0" : pos >= 92 ? "-100%" : "-50%";
    if (v !== 1 && v !== 0 && Math.abs(w(v) - w(1)) < (narrow ? 22 : 9)) return "";  // keep clear of the $1.00 label
    return `<span class="${v === 1 ? "one" : ""}" style="left:${pos}%;transform:translateX(${shift})">${v === 1 ? (narrow ? "$1.00" : "$1.00 of premium") : v === 0 ? "$0" : cents(v)}</span>`;
  }).join("");

  $("#dollar-legend").innerHTML = parts.map(([cls, v, label, amount]) =>
    `<span><i style="background:var(--${cls === "open" ? "open" : cls === "paid" ? "paid" : cls})"></i>${label} <b>${cents(v)}</b> (${money(amount)})</span>`).join("")
    + (pd.result >= 0
      ? `<span><i style="background:var(--in)"></i>Left over <b>${cents(pd.result)}</b> (${money(t.result)})</span>`
      : `<span><i style="background:var(--red)"></i>Lost <b class="loss">${cents(-pd.result)}</b> per dollar (${money(-t.result)} in total)</span>`);

  const claims = pd.paid + pd.reserve;
  const costs = pd.commission + pd.expenses;
  let sentence;
  if (pd.result >= 0) {
    sentence = `For every dollar of premium earned, ${cents(claims)} went to claims and ${cents(costs)} to commission and operating costs. <strong>${cents(pd.result)}</strong> was left: ${money(t.result)} in total.`;
  } else if (claims >= 1) {
    sentence = `For every dollar of premium earned, ${cents(claims)} went to claims: more than the dollar itself. After commission and costs, <span class="loss">each dollar lost ${cents(-pd.result)}</span>: ${money(-t.result)} in total.`;
  } else {
    sentence = `For every dollar of premium earned, ${cents(claims)} went to claims and ${cents(costs)} to commission and operating costs. <span class="loss">Each dollar lost ${cents(-pd.result)}</span>: ${money(-t.result)} in total.`;
  }
  $("#dollar-summary").innerHTML = sentence;
}

function renderTotals() {
  const d = state.data, t = d.totals;
  const be = d.breakeven_loss_ratio;
  const cells = [
    ["Premium written", money(t.written), `${int(t.policies_written)} policies sold`, ""],
    ["Premium earned", money(t.earned), `${int(t.policies_in_force)} policies covered`, ""],
    ["Claims paid", money(t.paid), `${int(t.claims)} claims` + (d.settle_median_days != null ? `, median ${Math.round(d.settle_median_days)} days to settle` : ""), ""],
    ["Open claims", money(t.reserve), "set aside, not yet paid", ""],
    ["Loss ratio", pct(t.loss_ratio), `break-even at ${pct(be)}`, t.loss_ratio > be ? "loss" : ""],
    ["Result after costs", money(t.result), `combined ratio ${pct(t.combined_ratio)}`, t.result < 0 ? "loss" : ""],
  ];
  $("#totals").innerHTML = cells.map(([label, value, note, cls]) =>
    `<div class="total"><div class="total-label">${label}</div><div class="total-value ${cls}">${value}</div><div class="total-note">${esc(note)}</div></div>`).join("");
}

function makeChart(key, canvas, config) {
  if (state.charts[key]) state.charts[key].destroy();
  state.charts[key] = new Chart(canvas, config);
  return state.charts[key];
}

function renderMonthly() {
  const m = state.data.monthly;
  const be = state.data.breakeven_loss_ratio;
  const lr3 = m.map((_, i) => {
    const w = m.slice(Math.max(0, i - 2), i + 1);
    const e = w.reduce((s, x) => s + x.earned, 0);
    const c = w.reduce((s, x) => s + x.paid + x.reserve, 0);
    return { e, lr: e > 0 ? c / e : null };
  }).map((x, _, all) => {
    const maxE = Math.max(...all.map((y) => y.e));
    return x.e >= 0.2 * maxE ? x.lr : null;  // too little premium earned yet to be meaningful
  });
  const maxLr = Math.max(1.2, ...lr3.filter((x) => x != null).map((x) => Math.min(x, 6)));
  makeChart("monthly", $("#chart-monthly"), {
    data: {
      labels: m.map((x) => monthLabel(x.month)),
      datasets: [
        { type: "bar", label: "Premium earned", data: m.map((x) => x.earned), backgroundColor: cssVar("--in"), stack: "in", order: 3, barPercentage: 0.9, categoryPercentage: 0.75 },
        { type: "bar", label: "Claims paid", data: m.map((x) => x.paid), backgroundColor: cssVar("--paid"), stack: "out", order: 3, barPercentage: 0.9, categoryPercentage: 0.75 },
        { type: "bar", label: "Open claims", data: m.map((x) => x.reserve), backgroundColor: cssVar("--open"), stack: "out", order: 3, barPercentage: 0.9, categoryPercentage: 0.75 },
        { type: "line", label: "Loss ratio, 3-month average", data: lr3, yAxisID: "y2", borderColor: cssVar("--ink"), backgroundColor: cssVar("--ink"), borderWidth: 2, pointRadius: 2, tension: 0.25, order: 1, spanGaps: true },
        { type: "line", label: "Break-even loss ratio", data: m.map(() => be), yAxisID: "y2", borderColor: cssVar("--red"), borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, order: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 12, padding: 14, sort: (a, b) => a.datasetIndex - b.datasetIndex } },
        tooltip: { itemSort: (a, b) => a.datasetIndex - b.datasetIndex, callbacks: { label: (c) => `${c.dataset.label}: ${c.dataset.yAxisID === "y2" ? pct(c.raw) : money(c.raw)}` } },
      },
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, ticks: { callback: (v) => money(v) }, title: { display: true, text: "Dollars" } },
        y2: { position: "right", min: 0, max: Math.ceil(maxLr * 10) / 10, grid: { display: false }, ticks: { callback: (v) => pct(v) }, title: { display: true, text: "Loss ratio" } },
      },
    },
  });
}

function tableColumns(scope) {
  if (scope === "claim") {
    return [
      ["label", "Segment", (r) => `<span class="seg-name">${esc(r.label)}</span>`],
      ["claims", "Claims", (r) => int(r.claims)],
      ["paid", "Claims paid", (r) => money(r.paid)],
      ["reserve", "Open claims", (r) => money(r.reserve)],
      ["incurred", "Total claims", (r) => money(r.incurred)],
      ["incurred_share", "Share of claim cost", (r) => pct(r.incurred_share)],
      ["loss_ratio_points", "Loss ratio points", (r) => (r.loss_ratio_points == null ? "–" : (r.loss_ratio_points * 100).toFixed(1))],
      ["settle_days", "Median days to settle", (r) => (r.settle_days == null ? "–" : Math.round(r.settle_days))],
    ];
  }
  return [
    ["label", "Segment", (r) => `<span class="seg-name">${esc(r.label)}</span>`],
    ["policies_written", "Policies sold", (r) => int(r.policies_written)],
    ["earned", "Premium earned", (r) => money(r.earned)],
    ["paid", "Claims paid", (r) => money(r.paid)],
    ["reserve", "Open claims", (r) => money(r.reserve)],
    ["costs", "Commission and costs", (r) => money(r.commission + r.expenses)],
    ["result", "Result after costs", (r) => money(r.result)],
    ["loss_ratio", "Loss ratio", (r) => pct(r.loss_ratio)],
    ["shares", "Share of premium (green) and claims (blue)", null],
  ];
}

function renderTable() {
  const d = state.data, b = d.breakdown;
  const cols = tableColumns(b.scope);
  let rows = b.rows.map((r) => ({ ...r, costs: (r.commission || 0) + (r.expenses || 0) }));
  const sort = state.sort && state.sort.dim === b.dim ? state.sort : null;
  if (sort) {
    rows.sort((a, c) => {
      const x = a[sort.col], y = c[sort.col];
      if (sort.col === "label") return sort.dir * String(x).localeCompare(String(y));
      return sort.dir * ((x ?? -Infinity) - (y ?? -Infinity));
    });
  } else if (b.scope === "claim") {
    rows.sort((a, c) => (c.incurred || 0) - (a.incurred || 0));
  } else if (!NATURAL_ORDER.has(b.dim)) {
    rows.sort((a, c) => (a.result ?? 0) - (c.result ?? 0));
  }
  const t = d.totals;
  const totEarned = t.earned || 1, totInc = t.incurred || 1;
  const maxShare = Math.max(...rows.map((r) => Math.max((r.earned || 0) / totEarned, (r.incurred || 0) / totInc)), 0.0001);
  const be = d.breakeven_loss_ratio;

  const head = cols.map(([key, label, fmt]) => {
    const ariaSort = sort && sort.col === key ? (sort.dir === 1 ? "ascending" : "descending") : "none";
    return `<th scope="col" data-key="${key}" ${fmt ? `aria-sort="${ariaSort}" tabindex="0"` : ""}>${esc(label)}</th>`;
  }).join("");
  const body = rows.map((r, i) => {
    const selected = Object.entries(r.key).every(([k, v]) => (state.filters[k] || []).length === 1 && String(state.filters[k][0]) === String(v));
    const cells = cols.map(([key, , fmt]) => {
      if (key === "shares") {
        const si = ((r.earned || 0) / totEarned / maxShare) * 100, so = ((r.incurred || 0) / totInc / maxShare) * 100;
        return `<td><div class="shares" title="${pct((r.earned || 0) / totEarned, 1)} of premium, ${pct((r.incurred || 0) / totInc, 1)} of claims"><i class="s-in" style="width:${si}%"></i><i class="s-out" style="width:${so}%"></i></div></td>`;
      }
      let cls = key === "label" ? "" : "num";
      if (key === "result" && r.result < 0) cls += " loss";
      if (key === "loss_ratio" && r.loss_ratio > be) cls += " loss";
      return `<td class="${cls}">${fmt(r)}</td>`;
    }).join("");
    return `<tr class="clickable${selected ? " selected" : ""}" data-i="${i}" tabindex="0">${cells}</tr>`;
  }).join("");
  let foot = "";
  if (b.scope === "policy") {
    foot = `<tfoot><tr><td>Total</td><td class="num">${int(t.policies_written)}</td><td class="num">${money(t.earned)}</td><td class="num">${money(t.paid)}</td><td class="num">${money(t.reserve)}</td><td class="num">${money(t.commission + t.expenses)}</td><td class="${moneyClass(t.result)}">${money(t.result)}</td><td class="num${t.loss_ratio > be ? " loss" : ""}">${pct(t.loss_ratio)}</td><td></td></tr></tfoot>`;
  } else {
    const sum = (k) => rows.reduce((s, r) => s + (r[k] || 0), 0);
    foot = `<tfoot><tr><td>Total</td><td class="num">${int(sum("claims"))}</td><td class="num">${money(sum("paid"))}</td><td class="num">${money(sum("reserve"))}</td><td class="num">${money(sum("incurred"))}</td><td class="num">100%</td><td class="num">${t.earned ? (sum("incurred") / t.earned * 100).toFixed(1) : "–"}</td><td></td></tr></tfoot>`;
  }
  const table = $("#pl-table");
  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}`;

  $$("thead th[aria-sort]", table).forEach((th) => {
    const go = () => {
      const col = th.dataset.key;
      const dir = sort && sort.col === col ? -sort.dir : (col === "label" ? 1 : -1);
      state.sort = { dim: b.dim, col, dir };
      renderTable();
    };
    th.addEventListener("click", go);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
  $$("tbody tr", table).forEach((tr) => {
    const go = () => {
      const r = rows[+tr.dataset.i];
      const already = Object.entries(r.key).every(([k, v]) => (state.filters[k] || []).length === 1 && String(state.filters[k][0]) === String(v));
      const update = {};
      for (const [k, v] of Object.entries(r.key)) update[k] = already ? [] : [k === "deductible" ? Number(v) : v];
      setFilters(update);
    };
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
}

function heatColor(lr, be, thin = false) {
  if (lr == null) return "transparent";
  const k = thin ? 0.45 : 1;
  if (lr <= be) {
    const p = Math.round(Math.min(1, (be - lr) / be) * 55 * k);
    return `color-mix(in srgb, var(--in) ${p}%, var(--sheet))`;
  }
  const p = Math.round(Math.min(1, (lr - be) / be) * 60 * k);
  return `color-mix(in srgb, var(--red) ${p}%, var(--sheet))`;
}
function renderHeatmap() {
  const h = state.data.heatmap, be = state.data.breakeven_loss_ratio;
  if (!h.rows.length) { $("#heatmap").innerHTML = `<p class="hint">No data for this selection.</p>`; return; }
  const head = `<tr><th></th>${h.cols.map((c) => `<th scope="col">${esc(c)}</th>`).join("")}</tr>`;
  const body = h.rows.map((rowLabel, i) => `<tr><th scope="row" class="row">${esc(rowLabel)}</th>${h.cells[i].map((cell, j) => {
    if (!cell) return `<td class="empty">–</td>`;
    const title = `${rowLabel}, ${h.cols[j]}: loss ratio ${pct(cell.loss_ratio)}. Premium earned ${money(cell.earned)}, claims ${money(cell.incurred)} (${cell.claims}), result after costs ${money(cell.result)}.${cell.thin ? " Small: under $15K of premium, read with care." : ""}`;
    return `<td class="${cell.thin ? "thin" : ""}" style="background-color:${heatColor(cell.loss_ratio, be, cell.thin)}" title="${esc(title)}" data-key='${esc(JSON.stringify(cell.key))}' tabindex="0">${pct(cell.loss_ratio)}</td>`;
  }).join("")}</tr>`).join("");
  $("#heat-title").textContent = `Loss ratio by ${h.rows_label.toLowerCase()} and ${h.cols_label.toLowerCase()}`;
  $("#heatmap").innerHTML = `<table class="heat"><thead>${head}</thead><tbody>${body}</tbody></table>
    <div class="heat-key"><span class="ramp"></span><span>Green: below break-even (${pct(be)}). Red: above it. Hatched: under $15K of premium.</span></div>`;
  $$("#heatmap td[data-key]").forEach((td) => {
    const go = () => {
      const key = JSON.parse(td.dataset.key);
      const update = {};
      for (const [k, v] of Object.entries(key)) update[k] = [k === "deductible" ? Number(v) : v];
      setFilters(update);
    };
    td.addEventListener("click", go);
    td.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
}

const reviewLinePlugin = {
  id: "reviewLine",
  afterDatasetsDraw(chart) {
    const x = chart.scales.x, area = chart.chartArea;
    if (!x || x.ticks.length < 11) return;
    const pos = (x.getPixelForTick(9) + x.getPixelForTick(10)) / 2;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = cssVar("--ink");
    ctx.setLineDash([5, 4]);
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(pos, area.top - 4); ctx.lineTo(pos, area.bottom); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = cssVar("--ink");
    ctx.font = `600 12px ${cssVar("--font")}`;
    ctx.textAlign = "left";
    ctx.fillText("Senior review from $10,000", pos + 6, area.top + 10);
    ctx.restore();
  },
};
function renderHist() {
  const h = state.data.histogram;
  const suspicious = h.band_count >= 4 && h.book_band_share != null && h.band_share > Math.max(0.12, 2 * h.book_band_share);
  const colors = h.bins.map((b) => (b.lo === 9000 ? (suspicious ? cssVar("--red") : cssVar("--ink")) : cssVar("--paid")));
  makeChart("hist", $("#chart-hist"), {
    type: "bar",
    data: {
      labels: h.bins.map((b) => (b.hi == null ? "$20K+" : `$${b.lo / 1000}–${b.hi / 1000}K`)),
      datasets: [{ label: "Claims", data: h.bins.map((b) => b.count), backgroundColor: colors, barPercentage: 0.95, categoryPercentage: 0.95 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      layout: { padding: { top: 18 } },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${c.raw} claims` } } },
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 60, minRotation: 45, autoSkip: false, font: { size: 10 } } },
        y: { ticks: { precision: 0 }, title: { display: true, text: "Claims" } },
      },
    },
    plugins: [reviewLinePlugin],
  });
  if (!h.n_sized) { $("#hist-note").textContent = "No claims with an amount in this selection."; return; }
  let note = `${h.band_count} of ${h.n_sized} claims (${pct(h.band_share)}) were priced $9,000 to $9,999, against ${pct(h.above_share)} at $10,000 to $10,999 just over the line. ${money(h.band_paid)} was paid on them.`;
  if (Object.keys(state.filters).length && h.book_band_share != null) note += ` Across the whole book in this period the share is ${pct(h.book_band_share)}.`;
  if (suspicious) note = `<strong class="loss">Claims bunch up just under the senior-review line.</strong> ` + note;
  $("#hist-note").innerHTML = note;
}

function bindLedger() {
  $("#period-from").addEventListener("change", (e) => {
    state.periodFrom = e.target.value;
    if (state.periodFrom > state.periodTo) { state.periodTo = state.periodFrom; $("#period-to").value = state.periodTo; }
    scheduleLedger(0);
  });
  $("#period-to").addEventListener("change", (e) => {
    state.periodTo = e.target.value;
    const quarters = state.meta.quarters;
    state.periodToIsLatest = state.periodTo === quarters[quarters.length - 1].value;
    if (state.periodTo < state.periodFrom) { state.periodFrom = state.periodTo; $("#period-from").value = state.periodFrom; }
    scheduleLedger(0);
  });
  $("#clear-filters").addEventListener("click", () => {
    state.periodFrom = state.meta.quarters[0].value;
    state.periodTo = state.meta.quarters[state.meta.quarters.length - 1].value;
    state.periodToIsLatest = true;
    $("#period-from").value = state.periodFrom;
    $("#period-to").value = state.periodTo;
    setFilters({}, { replace: true });
  });
  $("#dim").addEventListener("change", (e) => { state.dim = e.target.value; state.sort = null; scheduleLedger(0); });
  $("#heat-rows").addEventListener("change", (e) => { state.heatRows = e.target.value; scheduleLedger(0); });
  $("#heat-cols").addEventListener("change", (e) => { state.heatCols = e.target.value; scheduleLedger(0); });
  const readAssumptions = () => {
    const e = Number($("#a-expense").value), c = Number($("#a-commission").value);
    if (Number.isFinite(e) && Number.isFinite(c)) {
      state.assumptions = { expense_ratio: Math.min(Math.max(e, 0), 60) / 100, broker_commission: Math.min(Math.max(c, 0), 40) / 100 };
      scheduleLedger(250);
    }
  };
  $("#a-expense").addEventListener("input", readAssumptions);
  $("#a-commission").addEventListener("input", readAssumptions);
}

/* ----------------------------------------------------------- new quarter */
function bindQuarter() {
  for (const [dropId, inputId] of [["#drop-policies", "#file-policies"], ["#drop-claims", "#file-claims"]]) {
    const drop = $(dropId), input = $(inputId);
    const show = () => {
      const f = input.files[0];
      $(".file-name", drop).textContent = f ? `${f.name} (${(f.size / 1024).toFixed(0)} KB)` : "Choose or drop a file";
      drop.classList.toggle("has-file", !!f);
    };
    input.addEventListener("change", show);
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault(); drop.classList.remove("over");
      if (e.dataTransfer.files.length) {
        const dt = new DataTransfer(); dt.items.add(e.dataTransfer.files[0]); input.files = dt.files; show();
      }
    });
  }
  $("#upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const p = $("#file-policies").files[0], c = $("#file-claims").files[0];
    if (!p && !c) { toast("Choose a policies file, a claims file, or both.", true); return; }
    const fd = new FormData();
    if (p) fd.append("policies", p);
    if (c) fd.append("claims", c);
    fd.append("as_of", $("#as-of").value || "");
    await runBusy($("#upload-button"), "Checking…", async () => {
      const payload = await api("/api/upload", { method: "POST", body: fd });
      await afterDataChange(payload);
    });
  });
  $("#sample-button").addEventListener("click", async () => {
    await runBusy($("#sample-button"), "Loading the sample…", async () => {
      const payload = await postJSON("/api/upload-sample");
      await afterDataChange(payload);
    });
  });

  $$(".seg-toggle button").forEach((b) => b.addEventListener("click", () => {
    state.manualKind = b.dataset.kind;
    $$(".seg-toggle button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("#manual-policy").hidden = state.manualKind !== "policy";
    $("#manual-claim").hidden = state.manualKind !== "claim";
  }));
  const channel = $("#manual-policy [name=channel]"), broker = $("#manual-policy [name=broker_code]");
  channel.addEventListener("change", () => { broker.disabled = channel.value !== "Broker"; if (broker.disabled) broker.value = ""; });
  for (const kind of ["policy", "claim"]) {
    $(`#manual-${kind}`).addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      if (!form.reportValidity()) return;
      const row = Object.fromEntries(new FormData(form).entries());
      if (kind === "policy") { row.renewal_status = "In Force"; row.broker_code = broker.disabled ? "" : row.broker_code; }
      await runBusy($("button[type=submit]", form), "Adding…", async () => {
        const payload = await postJSON("/api/manual", { kind, row, assumptions: state.assumptions });
        if (!payload.entry.accepted) {
          toast(`Not added: ${payload.entry.rejected.map((r) => r.reason).join("; ")}.`, true);
          return;
        }
        toast(`${kind === "policy" ? "Policy" : "Claim"} ${payload.entry.id} added. Check re-run for ${payload.check ? payload.check.label : "its quarter"}.`);
        form.reset();
        broker.disabled = channel.value !== "Broker";
        await afterDataChange(payload);
      });
    });
  }

  const readRules = () => ({
    materiality: Number($("#r-materiality").value),
    confidence: Number($("#r-confidence").value),
    min_earned: Number($("#r-min_earned").value),
    min_claims: Number($("#r-min_claims").value),
    large_loss_cap: Number($("#r-large_loss_cap").value),
    worsening_points: Number($("#r-worsening_points").value),
    band_min_share: Number($("#r-band_min_share").value) / 100,
  });
  for (const [id, log] of [["#preview-button", false], ["#run-button", true]]) {
    $(id).addEventListener("click", async () => {
      await runBusy($(id), log ? "Checking…" : "Previewing…", async () => {
        const payload = await postJSON("/api/check", { quarter: $("#check-quarter").value, rules: readRules(), assumptions: state.assumptions, log });
        renderCheckResult(payload);
        if (log) await refreshBadge();
      });
    });
  }
}

async function runBusy(button, label, fn) {
  const old = button.textContent;
  button.disabled = true; button.textContent = label;
  try { await fn(); } catch (err) { toast(err.message, true); } finally { button.disabled = false; button.textContent = old; }
}
async function afterDataChange(payload) {
  await loadMeta();
  renderCheckResult(payload);
  scheduleLedger(0);
}
async function refreshBadge() {
  const meta = await api("/api/meta");
  $("#alert-count").textContent = meta.alerts_new ? meta.alerts_new : "";
}

const GROUPS = [
  ["new", "New this quarter", "Losing money now, but not in the previous four quarters (or no history yet)."],
  ["ongoing", "Still losing money from before", "Also losing money over the previous four quarters."],
  ["watch", "Getting worse", "Not losing money beyond doubt yet, but rising faster than its own history."],
];
const STATUS_LABEL = { critical: "Critical", high: "High", watch: "Watch" };

function renderCheckResult(payload) {
  const { check, actions, batch, entry } = payload;
  const box = $("#q-result");
  if (!check) { box.innerHTML = ""; return; }
  state.lastCheck = check;
  const book = check.book || {}, base = check.book_baseline || {};
  let html = `<div class="headline">${esc(check.headline)}</div>
    <p class="headline-sub">Whole book in ${esc(check.label)}: loss ratio ${pct(book.loss_ratio)}${base.loss_ratio != null ? ` (previous four quarters: ${pct(base.loss_ratio)})` : ""}, result after costs ${money(book.result)}. Checked ${dateLabel(check.window.from)} to ${dateLabel(check.window.to)}.</p>`;

  if (batch && batch.report) html += reportHTML(batch);
  if (entry) html += `<p class="hint">Added ${esc(entry.id)} by hand.</p>`;
  if (actions) {
    const people = [...new Set(actions.messages.map((m) => m.to))];
    html += `<div class="actions-taken">
      <span><b>${actions.alerts_created}</b>new alert${actions.alerts_created === 1 ? "" : "s"} logged${actions.alerts_updated ? `, ${actions.alerts_updated} updated` : ""}</span>
      <span><b>${actions.claims_routed}</b>claim${actions.claims_routed === 1 ? "" : "s"} sent to senior review</span>
      <span><b>${people.length}</b>${people.length ? `messaged: ${esc(people.join(", "))}` : "people messaged (nothing new to send)"}</span>
      <button class="link-button" type="button" id="see-alerts">See alerts</button></div>`;
  } else {
    html += `<p class="hint" style="margin:1rem 0">Preview only: nothing was logged or sent.</p>`;
  }
  if (check.set_aside && check.set_aside.length) {
    html += `<p class="hint">${esc(check.set_aside.join(", "))} ${check.set_aside.length === 1 ? "is" : "are"} set aside from the city and pricing views, so ${check.set_aside.length === 1 ? "its" : "their"} claims aren’t flagged three times.</p>`;
  }
  if (!check.flags.length) {
    html += `<div class="empty-state">Nothing crossed the alert lines in ${esc(check.label)}. Lower the lines under “Check any quarter” to see what’s closest.</div>`;
  }
  for (const [group, title, hint] of GROUPS) {
    const flags = check.flags.filter((f) => f.group === group);
    if (!flags.length) continue;
    html += `<section class="flag-group"><h2>${title}<span class="hint">${hint}</span></h2>${flags.map(flagHTML).join("")}</section>`;
  }
  html += `<section class="panel"><div class="panel-head"><h2>Loss ratio this quarter against the previous four, by
      <select id="check-lens">${check.lenses.map((l) => `<option value="${l.key}">${esc(l.detail.toLowerCase())}</option>`).join("")}</select></h2>
      <p class="hint">Red: flagged. Lighter bar: previous four quarters. Single claims capped at $25K.</p></div>
      <div class="chart-box" id="check-chart-box"><canvas id="chart-check"></canvas></div></section>`;
  box.innerHTML = html;

  $("#check-lens").value = state.checkLens;
  $("#check-lens").addEventListener("change", (e) => { state.checkLens = e.target.value; renderCheckChart(); });
  const see = $("#see-alerts");
  if (see) see.addEventListener("click", () => showView("alerts"));
  $$("[data-show]", box).forEach((b) => b.addEventListener("click", () => showInLedger(JSON.parse(b.dataset.show), check.quarter)));
  $$("[data-review]", box).forEach((b) => b.addEventListener("click", () => {
    const f = check.flags.find((x) => x.id === b.dataset.review);
    downloadCSV(f.review, `senior-review_${f.segment.replace(/[^A-Za-z0-9]+/g, "-")}_${check.quarter}.csv`);
  }));
  renderCheckChart();
  box.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function reportHTML(batch) {
  const rep = batch.report;
  const line = (label, r) => (r ? `<li>${label}: ${int(r.received)} rows received, ${int(r.added)} added, ${int(r.updated)} already known (updated, not double counted), ${int(r.rejected)} rejected.</li>` : "");
  const fixes = (rep.log || []).map((e) => `<li>${esc(e.step)}: ${int(e.rows)}${e.impact ? `. ${esc(e.impact)}` : ""}</li>`).join("");
  const rejected = (rep.rejected || []).slice(0, 8).map((r) => `<li>${esc(r.id)} (${esc(r.table)}): ${esc(r.reason)}</li>`).join("");
  return `<div class="report">
    <div><h3>What came in</h3><ul>${line("Policies", rep.policies)}${line("Claims", rep.claims)}
      <li>Extract date: ${dateLabel(batch.as_of)}. Quarter checked: ${esc(String(batch.quarter || "").replace("Q", " Q"))}.</li></ul></div>
    <div><h3>What was fixed or turned away</h3><ul>${fixes || "<li>Nothing needed fixing.</li>"}${rejected}</ul></div>
  </div>`;
}

function flagHTML(f) {
  const m = f.metrics;
  const statusText = { new: "New", no_history: "No reliable history", ongoing: "Also losing money before", worsening: "Rising", pattern: "Claim pattern" }[f.status] || "";
  const review = f.review && f.review.length
    ? `<button class="link-button" type="button" data-review="${esc(f.id)}">Download the ${f.review.length} claims for senior review</button>` : "";
  return `<article class="flag">
    <div><span class="stamp ${f.severity}">${STATUS_LABEL[f.severity]}</span></div>
    <div>
      <div class="flag-title">${esc(f.segment)}</div>
      <div class="flag-lens">${esc(f.lens_label)} (${esc(f.lens_detail.toLowerCase())})${statusText ? `. ${statusText}.` : ""}</div>
      <ul class="flag-reasons">${f.reasons.map((r) => `<li>${esc(r.text)}</li>`).join("")}</ul>
      <ul class="flag-drivers">${f.drivers.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
      <div class="flag-next"><b>Next step.</b> ${esc(f.action)}</div>
      <div class="flag-meta"><span>Sent to ${esc(f.owners.join(", "))}</span>${review}
        <button class="link-button" type="button" data-show='${esc(JSON.stringify(f.key))}'>Show in the ledger</button></div>
    </div>
    <div class="flag-figs">
      <div><span>Premium earned</span><span class="num">${money(m.earned)}</span></div>
      <div><span>Claims (${int(m.claims)})</span><span class="num">${money(m.incurred)}</span></div>
      <div><span>Loss ratio</span><span class="num loss">${pct(m.loss_ratio)}</span></div>
      <div><span>Previous four quarters</span><span class="num">${pct(m.baseline_loss_ratio)}</span></div>
      <div><span>Result after costs</span><span class="${moneyClass(m.result)}">${money(m.result)}</span></div>
    </div>
  </article>`;
}

function renderCheckChart() {
  const check = state.lastCheck;
  if (!check || !$("#chart-check")) return;
  const rows = (check.segments[state.checkLens] || []).filter((r) => r.earned > 0).slice(0, 16);
  $("#check-chart-box").style.height = `${Math.max(12, rows.length * 1.9 + 4)}rem`;
  const red = cssVar("--red"), ink = cssVar("--ink-2");
  makeChart("check", $("#chart-check"), {
    type: "bar",
    data: {
      labels: rows.map((r) => r.label),
      datasets: [
        { label: "This quarter", data: rows.map((r) => r.loss_ratio_capped), backgroundColor: rows.map((r) => (r.flagged ? red : ink)), barPercentage: 0.8, categoryPercentage: 0.8 },
        { label: "Previous four quarters", data: rows.map((r) => r.baseline_loss_ratio_capped), backgroundColor: cssVar("--rule-strong"), barPercentage: 0.8, categoryPercentage: 0.8 },
      ],
    },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } }, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${pct(c.raw)}` } } },
      scales: { x: { ticks: { callback: (v) => pct(v) }, suggestedMax: 1.5 }, y: { grid: { display: false } } },
    },
  });
}

function showInLedger(key, quarter) {
  const update = {};
  for (const [k, v] of Object.entries(key)) update[k] = [k === "deductible" ? Number(v) : v];
  state.periodFrom = quarter; state.periodTo = quarter;
  state.periodToIsLatest = quarter === state.meta.quarters[state.meta.quarters.length - 1].value;
  $("#period-from").value = quarter; $("#period-to").value = quarter;
  setFilters(update, { replace: true });
  showView("ledger");
}

function downloadCSV(rows, filename) {
  if (!rows || !rows.length) return;
  const cols = Object.keys(rows[0]);
  const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => {
    const v = r[c] == null ? "" : String(r[c]);
    return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  }).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* ---------------------------------------------------------------- alerts */
async function loadAlerts() {
  try {
    const data = await api("/api/alerts");
    renderAlerts(data);
  } catch (err) { toast(err.message, true); }
}
function renderAlerts(data) {
  const filter = $("#alert-filter").value;
  const items = data.alerts.filter((a) => !filter || a.workflow === filter);
  if (!data.alerts.length) {
    $("#alerts-body").innerHTML = `<div class="empty-state">No alerts yet. They appear here when a check runs: upload a new quarter, add a record by hand, or press “Check and send alerts”.
      <div><button class="button primary" type="button" id="go-quarter">Go to New quarter</button></div></div>`;
    $("#go-quarter").addEventListener("click", () => showView("quarter"));
  } else if (!items.length) {
    $("#alerts-body").innerHTML = `<div class="empty-state">No alerts marked ${esc(filter)}.</div>`;
  } else {
    const rows = items.map((a) => `<tr>
      <td>${esc(a.quarter_label)}<div class="hint">${esc(a.created_at.replace("T", " ").slice(0, 16))}</div></td>
      <td><span class="mini-stamp ${a.severity}">${STATUS_LABEL[a.severity]}</span></td>
      <td><span class="seg-name">${esc(a.segment)}</span><div class="hint">${esc(a.lens_label)}</div>${a.flagged_before && a.flagged_before.length ? `<div class="hint">Also flagged in ${esc(a.flagged_before.join(", "))}</div>` : ""}</td>
      <td class="what">${esc(a.reasons[0] ? a.reasons[0].text : "")}<div class="hint" style="margin-top:.3rem">Next step: ${esc(a.action)}</div></td>
      <td>${esc(a.owners.join(", "))}</td>
      <td><select data-alert="${a.id}" aria-label="Status of ${esc(a.segment)}">${["New", "Acknowledged", "Resolved"].map((w) => `<option ${w === a.workflow ? "selected" : ""}>${w}</option>`).join("")}</select></td>
      <td>${a.review && a.review.length ? `<a href="/api/alerts/${a.id}/review.csv">${a.review.length} claims (CSV)</a>` : `<span class="hint">–</span>`}</td>
    </tr>`).join("");
    $("#alerts-body").innerHTML = `<div class="table-wrap"><table class="ledger alerts-table"><thead><tr>
      <th>Quarter</th><th>Severity</th><th>Segment</th><th>What the check found</th><th>Sent to</th><th>Status</th><th>Review list</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    $$("#alerts-body select[data-alert]").forEach((s) => s.addEventListener("change", async () => {
      try {
        await postJSON(`/api/alerts/${s.dataset.alert}`, { workflow: s.value });
        toast(`Marked ${s.value.toLowerCase()}.`);
        await refreshBadge();
      } catch (err) { toast(err.message, true); }
    }));
  }
  $("#outbox-body").innerHTML = data.outbox.length
    ? data.outbox.map((m) => `<details class="message"><summary><span>${esc(m.sent_at.replace("T", " ").slice(0, 16))}</span><span><strong>${esc(m.to)}</strong></span><span>${esc(m.subject)}</span></summary><pre>${esc(m.body)}</pre><p class="hint">Delivered to: ${esc(m.delivered)}</p></details>`).join("")
    : `<p class="empty-state">No messages yet.</p>`;
}
function bindAlerts() {
  $("#alert-filter").addEventListener("change", loadAlerts);
}

/* ----------------------------------------------------------------- notes */
async function loadNotes() {
  try {
    const data = await api("/api/data-notes");
    $("#notes-table").innerHTML = `<thead><tr><th>File</th><th>What we found</th><th>Rows</th><th>What we did</th><th>Why it matters</th></tr></thead><tbody>${
      data.log.map((e) => `<tr><td>${esc(e.table)}</td><td><span class="seg-name">${esc(e.step)}</span></td><td>${int(e.rows)}</td><td>${esc(e.action)}</td><td>${esc(e.why)}${e.impact ? ` <strong>${esc(e.impact)}</strong>` : ""}</td></tr>`).join("")
    }</tbody>`;
    $("#batches-body").innerHTML = data.batches.length
      ? data.batches.map((b) => `<div class="side-box"><h3>${esc(b.name)}</h3><p>Added ${esc(b.uploaded_at.replace("T", " ").slice(0, 16))}. Extract date ${dateLabel(b.as_of)}.</p>${b.report ? reportHTML(b) : ""}</div>`).join("")
      : `<p class="empty-state">No extracts added yet. The ledger shows the original extract of ${dateLabel(state.meta.extraction_date)}.</p>`;
  } catch (err) { toast(err.message, true); }
}
function bindNotes() {
  $("#reset-button").addEventListener("click", async () => {
    if (!confirm("Remove every uploaded or hand-entered record, and clear all alerts and messages? The original extract stays.")) return;
    try {
      await postJSON("/api/reset");
      state.lastCheck = null;
      $("#q-result").innerHTML = "";
      state.periodToIsLatest = true;
      await loadMeta();
      await loadLedger();
      await loadNotes();
      toast("Back to the original extract.");
    } catch (err) { toast(err.message, true); }
  });
}

/* ------------------------------------------------------------------ init */
async function init() {
  initTheme();
  applyChartDefaults();
  bindTabs();
  bindLedger();
  bindQuarter();
  bindAlerts();
  bindNotes();
  try {
    await loadMeta();
    await loadLedger();
  } catch (err) {
    toast("Can't reach the server. Is `python run.py` running? " + err.message, true);
  }
  const v = location.hash.replace("#", "");
  if (["quarter", "alerts", "notes"].includes(v)) showView(v);
  const fromUrl = () => {  // back/forward buttons and typed links switch tabs
    const name = location.hash.replace("#", "") || "ledger";
    if (["ledger", "quarter", "alerts", "notes"].includes(name)) showView(name);
  };
  window.addEventListener("hashchange", fromUrl);
  window.addEventListener("popstate", fromUrl);
}
document.addEventListener("DOMContentLoaded", init);
