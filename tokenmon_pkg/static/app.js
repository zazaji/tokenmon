"use strict";

// ------------------------------------------------------------------ state

// How many periods the window covers when the user has not chosen one.
// A single day or week is almost always empty, so the defaults are wide
// enough to show a meaningful curve immediately.
var DEFAULT_SPAN = { day: 14, week: 8, month: 6 };

function initialParams() {
  var params = new URLSearchParams(window.location.search);
  var period = params.get("period");
  var span = parseInt(params.get("span") || "", 10);
  return {
    period: ["day", "week", "month"].indexOf(period) >= 0 ? period : "day",
    span: (Number.isFinite(span) && span > 0) ? Math.min(span, 90) : null,
    spanFromUrl: Number.isFinite(span) && span > 0,
    tz: params.get("tz") || "",
    tools: (params.get("tools") || "").split(",").filter(Boolean),
    models: (params.get("model") || "").split(",").filter(Boolean),
    anchor: params.get("anchor") || ""
  };
}

var urlParams = initialParams();

var state = {
  period: urlParams.period,
  span: urlParams.spanFromUrl ? urlParams.span : DEFAULT_SPAN[urlParams.period],
  tz: urlParams.tz,
  tools: urlParams.tools,
  models: urlParams.models,
  anchor: urlParams.anchor,
  knownTools: [],
  knownModels: [],
  last: null,
  series: [],
  metric: "total_tokens"
};

// Metrics offered by the line chart toolbar.
var METRICS = [
  { key: "total_tokens", label: "总 Token", color: "#5b8cff" },
  { key: "input_tokens", label: "输入", color: "#5b8cff" },
  { key: "output_tokens", label: "输出", color: "#f0a742" },
  { key: "reasoning_tokens", label: "推理", color: "#a06bff" },
  { key: "cache_read_tokens", label: "缓存命中", color: "#37c2a8" },
  { key: "cache_write_tokens", label: "缓存写入", color: "#e879b9" },
  { key: "interactions", label: "交互次数", color: "#8bd45a" },
  { key: "llm_calls", label: "LLM 调用", color: "#4d9fff" },
  { key: "tool_calls", label: "工具调用", color: "#ef5f6b" }
];

function metricDef(key) {
  for (var i = 0; i < METRICS.length; i++) {
    if (METRICS[i].key === key) return METRICS[i];
  }
  return METRICS[0];
}

var TOOL_COLORS = {
  codex: "#5b8cff",
  claudecode: "#f0a742",
  zcode: "#37c2a8",
  opencode: "#a06bff",
  "deepseek-harness": "#4d9fff",
  antigravity: "#ef5f6b",
  qwencode: "#8bd45a",
  cursor: "#e879b9"
};

function qs(sel) { return document.querySelector(sel); }
function qsa(sel) {
  return Array.prototype.slice.call(document.querySelectorAll(sel));
}

// Compact token formatting: B / M / k with at most 3 significant digits.
//   1_884_229_336 -> 1.88B   348_583_129 -> 349M   14_200 -> 14.2k
function fmt(n) {
  if (n === null || n === undefined) return "-";
  var value = Number(n);
  if (!isFinite(value)) return "-";
  var sign = value < 0 ? "-" : "";
  var abs = Math.abs(value);
  if (abs >= 1e9) return sign + sig3(abs / 1e9) + "B";
  if (abs >= 1e6) return sign + sig3(abs / 1e6) + "M";
  if (abs >= 1e3) return sign + sig3(abs / 1e3) + "k";
  return sign + String(Math.round(abs));
}

// Round to 3 significant digits and drop trailing zeros so the label stays
// short: 1.005 -> "1.01", 1.0 -> "1", 349.0 -> "349".
function sig3(x) {
  if (x >= 100) return String(Math.round(x));
  if (x >= 10) return String(Math.round(x * 10) / 10);
  return String(Math.round(x * 100) / 100);
}

function fmtFull(n) {
  if (n === null || n === undefined) return "-";
  return Number(n).toLocaleString("en-US");
}

function pct(n) { return (n * 100).toFixed(1) + "%"; }

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function params() {
  var p = new URLSearchParams();
  p.set("period", state.period);
  p.set("span", String(state.span));
  if (state.tz) p.set("tz", state.tz);
  if (state.anchor) p.set("anchor", state.anchor);
  if (state.tools.length) p.set("tools", state.tools.join(","));
  if (state.models.length) p.set("model", state.models.join(","));
  return p.toString();
}

async function getJSON(path) {
  var res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(path + " -> " + res.status);
  return res.json();
}

// ----------------------------------------------------------------- filters

function initControls() {
  qsa("#period button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      qsa("#period button").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      state.period = btn.dataset.value;
      state.anchor = "";
      state.span = DEFAULT_SPAN[state.period];
      var spanSel = qs("#span");
  var wanted = String(state.span);
  var hasOption = Array.prototype.some.call(spanSel.options, function (o) {
    return o.value === wanted;
  });
  if (!hasOption) {
    // Keep the model and the control in sync for non standard spans without
    // appending a duplicate option on every period change.
    var stale = spanSel.querySelector("option[data-dynamic=\"1\"]");
    if (stale) stale.remove();
    var opt = document.createElement("option");
    opt.value = wanted;
    opt.textContent = wanted + " 个周期";
    opt.dataset.dynamic = "1";
    spanSel.appendChild(opt);
  }
  spanSel.value = wanted;
      load();
    });
  });

  qs("#span").addEventListener("change", function (e) {
    state.span = parseInt(e.target.value, 10) || 1;
    load();
  });

  qs("#tz").addEventListener("change", function (e) {
    state.tz = e.target.value;
    load();
  });

  qs("#prevPeriod").addEventListener("click", function () { shift(-1); });
  qs("#nextPeriod").addEventListener("click", function () { shift(1); });
  qs("#refresh").addEventListener("click", load);
  qs("#collect").addEventListener("click", collect);
}

function shift(direction) {
  // The anchor always travels with the request so the server resolves the
  // window from the same period the user is currently looking at.
  var query = params();
  qs("#status").textContent = "加载中…";
  getJSON("/api/nav?" + query).then(function (nav) {
    state.anchor = direction < 0 ? nav.previous : nav.next;
    return load();
  }).catch(function (err) {
    qs("#status").textContent = "切换周期失败: " + err.message;
  });
}

function syncControls() {
  qsa("#period button").forEach(function (b) {
    b.classList.toggle("active", b.dataset.value === state.period);
  });
  qs("#span").value = String(state.span);
  qs("#tz").value = state.tz;
}

// Chip row helper.  When single is true the row behaves like a radio group:
// selecting a chip replaces the previous selection, clicking the active chip
// clears it.
function buildChips(container, values, selected, onChange, single) {
  container.innerHTML = "";
  values.forEach(function (value) {
    var on = selected.indexOf(value) >= 0;
    var chip = document.createElement("span");
    chip.className = "chip" + (on ? " on" : "");
    chip.textContent = value;
    chip.title = single ? "单选：" + value : value;
    chip.dataset.value = value;
    chip.addEventListener("click", function () {
      if (single) {
        var wasOn = selected.indexOf(value) >= 0;
        selected.length = 0;
        if (!wasOn) selected.push(value);
      } else {
        var index = selected.indexOf(value);
        if (index >= 0) selected.splice(index, 1);
        else selected.push(value);
      }
      // Reflect the new state on every chip of this row.
      Array.prototype.forEach.call(container.children, function (c) {
        c.classList.toggle("on", selected.indexOf(c.dataset.value) >= 0);
      });
      onChange();
    });
    container.appendChild(chip);
  });
}

function buildTimezoneOptions() {
  var zones = ["", "UTC", "Asia/Shanghai", "Asia/Singapore", "Asia/Tokyo",
               "Europe/London", "Europe/Berlin", "America/New_York",
               "America/Los_Angeles", "Australia/Sydney"];
  var sel = qs("#tz");
  zones.forEach(function (z) {
    var opt = document.createElement("option");
    opt.value = z;
    opt.textContent = z || ("本地时区 (" + Intl.DateTimeFormat().resolvedOptions().timeZone + ")");
    sel.appendChild(opt);
  });
}

// ------------------------------------------------------------- line chart

// ------------------------------------------------------------- line chart

// Catmull-Rom spline converted to cubic Bezier segments.  This is what makes
// the curve smooth: straight L segments between sparse daily buckets read as
// a jagged polyline, while the spline keeps the data points exact and bends
// the line between them.
function splinePath(points) {
  if (!points.length) return "";
  if (points.length === 1) {
    return "M" + points[0][0].toFixed(1) + " " + points[0][1].toFixed(1);
  }
  var d = "M" + points[0][0].toFixed(1) + " " + points[0][1].toFixed(1);
  for (var i = 0; i < points.length - 1; i++) {
    var p0 = points[i - 1] || points[i];
    var p1 = points[i];
    var p2 = points[i + 1];
    var p3 = points[i + 2] || p2;
    var k = 1 / 6;
    var c1x = p1[0] + (p2[0] - p0[0]) * k * 2;
    var c1y = p1[1] + (p2[1] - p0[1]) * k * 2;
    var c2x = p2[0] - (p3[0] - p1[0]) * k * 2;
    var c2y = p2[1] - (p3[1] - p1[1]) * k * 2;
    d += "C" + c1x.toFixed(1) + " " + c1y.toFixed(1) + " "
      + c2x.toFixed(1) + " " + c2y.toFixed(1) + " "
      + p2[0].toFixed(1) + " " + p2[1].toFixed(1) + " ";
  }
  return d;
}

function renderLineChart(buckets) {
  var host = qs("#chart");
  var metric = metricDef(state.metric);
  renderMetricToolbar(metric);

  if (!buckets.length) {
    host.innerHTML = "<div class=\"empty\">当前周期暂无数据</div>";
    qs("#chartHint").textContent = "";
    return;
  }

  var width = Math.max(560, host.clientWidth || 860);
  var height = 280;
  var pad = { top: 18, right: 18, bottom: 28, left: 60 };
  var plotW = width - pad.left - pad.right;
  var plotH = height - pad.top - pad.bottom;

  var values = buckets.map(function (b) { return Number(b[metric.key]) || 0; });
  var max = Math.max.apply(null, values.concat([0]));
  // Anchor the y axis at zero so the area fill is meaningful, then leave
  // head room for the peak label.
  var top = max === 0 ? 1 : max * 1.14;
  var span = top;
  var stepX = buckets.length > 1 ? plotW / (buckets.length - 1) : 0;

  function xAt(i) { return pad.left + stepX * i; }
  function yAt(v) { return pad.top + plotH - (v / span) * plotH; }

  var points = values.map(function (v, i) { return [xAt(i), yAt(v)]; });
  var linePath = splinePath(points);
  var areaPath = points.length > 1
    ? linePath + "L" + xAt(points.length - 1).toFixed(1)
      + " " + (pad.top + plotH).toFixed(1)
      + " L" + xAt(0).toFixed(1) + " " + (pad.top + plotH).toFixed(1) + " Z"
    : "";

  var gradId = "grad-" + metric.key;
  var parts = [];
  parts.push("<svg viewBox=\"0 0 " + width + " " + height + "\" role=\"img\">");
  parts.push("<defs><linearGradient id=\"" + gradId + "\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">",
             "<stop offset=\"0%\" stop-color=\"" + metric.color + "\" stop-opacity=\"0.40\"/>",
             "<stop offset=\"100%\" stop-color=\"" + metric.color + "\" stop-opacity=\"0.02\"/>",
             "</linearGradient></defs>");

  var ticks = 4;
  for (var t = 0; t <= ticks; t++) {
    var v = (span * t) / ticks;
    var y = yAt(v);
    parts.push("<line class=\"grid-line\" x1=\"" + pad.left + "\" y1=\"" + y.toFixed(1),
               + "\" x2=\"" + (width - pad.right) + "\" y2=\"" + y.toFixed(1) + "\"/>");
    parts.push("<text class=\"axis-label\" x=\"" + (pad.left - 9) + "\" y=\"",
               + (y + 3.5).toFixed(1) + "\" text-anchor=\"end\">" + esc(fmt(v)) + "</text>");
  }

  var every = Math.max(1, Math.ceil(buckets.length / 14));
  buckets.forEach(function (b, i) {
    if (i % every !== 0 && i !== buckets.length - 1) return;
    parts.push("<text class=\"axis-label\" x=\"" + xAt(i).toFixed(1) + "\" y=\"",
               + (height - 9) + "\" text-anchor=\"middle\">" + esc(shortLabel(b.label)) + "</text>");
  });

  if (areaPath) {
    parts.push("<path class=\"series-area\" d=\"" + areaPath + "\" fill=\"url(#" + gradId + ")\" stroke=\"none\"/>");
  }
  parts.push("<path class=\"series-path\" d=\"" + linePath + "\" stroke=\"" + metric.color + "\"/>");

  // Mark only the peak so the curve stays clean when there are many buckets.
  var peakIndex = 0;
  values.forEach(function (v, i) { if (v > values[peakIndex]) peakIndex = i; });
  if (values[peakIndex] > 0) {
    parts.push("<circle class=\"dot peak\" cx=\"" + xAt(peakIndex).toFixed(1)
               + "\" cy=\"" + yAt(values[peakIndex]).toFixed(1) + "\" r=\"4\" fill=\""
               + metric.color + "\"/>");
    parts.push("<text class=\"peak-label\" x=\"" + xAt(peakIndex).toFixed(1)
               + "\" y=\"" + (yAt(values[peakIndex]) - 10).toFixed(1)
               + "\" text-anchor=\"middle\">" + esc(fmt(values[peakIndex])) + "</text>");
  }

  parts.push("<line class=\"hover-line\" id=\"hoverLine\" x1=\"0\" y1=\"" + pad.top
             + "\" x2=\"0\" y2=\"" + (pad.top + plotH) + "\" style=\"display:none\"/>");
  parts.push("</svg>");
  parts.push("<div class=\"linechart-tip\" id=\"chartTip\"></div>");
  host.innerHTML = parts.join("");

  qs("#chartHint").textContent = "指标 " + metric.label + " · 共 "
    + buckets.length + " 个时间桶 · 峰值 " + fmt(max);

  attachChartHover(host, buckets, metric, xAt, width);
}
function shortLabel(label) {
  // 2026-10-22 -> 10-22 ; 2026-W42 -> W42 ; 2026-10 -> 10月
  var s = String(label);
  if (s.indexOf("-W") > 0) return s.split("-")[1];
  if (s.length === 7) return s.slice(5) + "月";
  return s.slice(5);
}

function attachChartHover(host, buckets, metric, xAt, width) {
  var tip = qs("#chartTip");
  var hoverLine = qs("#hoverLine");
  if (!tip) return;
  host.addEventListener("mousemove", function (event) {
    var rect = host.getBoundingClientRect();
    var relX = (event.clientX - rect.left) * (width / rect.width);
    var best = 0;
    var bestDist = Infinity;
    buckets.forEach(function (b, i) {
      var d = Math.abs(xAt(i) - relX);
      if (d < bestDist) { bestDist = d; best = i; }
    });
    var bucket = buckets[best];
    if (!bucket) return;
    if (hoverLine) {
      hoverLine.setAttribute("x1", String(xAt(best)));
      hoverLine.setAttribute("x2", String(xAt(best)));
      hoverLine.style.display = "block";
    }
    tip.innerHTML = "<div class=\"t-title\">" + esc(bucket.label) + "</div>"
      + "<div class=\"t-row\"><span><i style=\"background:" + metric.color
        + "\"></i>" + esc(metric.label) + "</span><span>"
        + fmtFull(bucket[metric.key]) + "</span></div>"
      + "<div class=\"t-row\"><span>缓存命中</span><span>"
        + fmtFull(bucket.cache_read_tokens) + "</span></div>"
      + "<div class=\"t-row\"><span>交互次数</span><span>"
        + fmtFull(bucket.interactions) + "</span></div>"
      + "<div class=\"t-row\"><span>命中率</span><span>"
        + pct(bucket.cache_hit_ratio) + "</span></div>";
    tip.classList.add("on");
    var leftPct = (xAt(best) / width) * 100;
    tip.style.left = Math.min(72, Math.max(0, leftPct)) + "%";
    tip.style.top = "4px";
  });
  host.addEventListener("mouseleave", function () {
    tip.classList.remove("on");
    if (hoverLine) hoverLine.style.display = "none";
  });
}

function renderMetricToolbar(metric) {
  var host = qs("#seriesLegend");
  if (!host) return;
  if (host.dataset.built !== "1") {
    host.dataset.built = "1";
    var wrap = document.createElement("div");
    wrap.className = "chart-toolbar";
    METRICS.forEach(function (m) {
      var chip = document.createElement("span");
      chip.className = "chip" + (m.key === state.metric ? " on" : "");
      chip.textContent = m.label;
      chip.dataset.metric = m.key;
      chip.addEventListener("click", function () {
        state.metric = m.key;
        renderMetricToolbar(metricDef(state.metric));
        renderLineChart(state.series);
      });
      wrap.appendChild(chip);
    });
    host.innerHTML = "";
    host.appendChild(wrap);
  }
  // Reflect the active metric and its colour in the toolbar.
  Array.prototype.forEach.call(host.querySelectorAll(".chip"), function (chip) {
    var on = chip.dataset.metric === state.metric;
    chip.classList.toggle("on", on);
    if (on && metric) {
      chip.style.borderColor = metric.color;
      chip.style.color = metric.color;
    } else {
      chip.style.borderColor = "";
      chip.style.color = "";
    }
  });
  var title = document.createElement("span");
  title.className = "chart-metric-title";
  title.textContent = metric ? metric.label : "";
  var existing = host.querySelector(".chart-metric-title");
  if (existing) existing.replaceWith(title);
  else host.appendChild(title);
}

// Legacy stacked bars are replaced by the line chart above; kept as a helper
// for the empty state message only.
function renderChartPlaceholder() {
  var host = qs("#chart");
  if (!host) return;
}

// --------------------------------------------------------------------- KPIs

function renderKpis(data) {
  var t = data.totals;
  var cards = [
    { label: "总 Token", value: fmt(t.total_tokens), extra: fmtFull(t.total_tokens) + " tokens" },
    { label: "输入 Token", value: fmt(t.input_tokens), extra: "不含缓存部分" },
    { label: "输出 Token", value: fmt(t.output_tokens), extra: "推理 " + fmt(t.reasoning_tokens) },
    { label: "缓存命中", value: fmt(t.cache_read_tokens),
      extra: "命中率 " + pct(t.cache_hit_ratio), accent: true },
    { label: "缓存写入", value: fmt(t.cache_write_tokens), extra: "cache creation" },
    { label: "交互次数", value: fmtFull(t.interactions), extra: "用户提问轮次" },
    { label: "LLM 调用", value: fmtFull(t.llm_calls), extra: "模型请求数" },
    { label: "工具调用", value: fmtFull(t.tool_calls), extra: "tool calls" },
    { label: "使用工具", value: String(t.tool_count), extra: t.tools.join(", ") },
    { label: "使用模型", value: String(t.model_count),
      extra: t.models.slice(0, 3).join(", ") }
  ];
  qs("#kpis").innerHTML = cards.map(function (c) {
    return "<div class=\"kpi" + (c.accent ? " accent" : "") + "\">"
      + "<div class=\"label\">" + esc(c.label) + "</div>"
      + "<div class=\"value\">" + esc(c.value) + "</div>"
      + "<div class=\"extra\">" + esc(c.extra) + "</div></div>";
  }).join("");
}

// ------------------------------------------------------------------ tables

function num(v) { return "<td class=\"num\">" + fmtFull(v) + "</td>"; }
function txt(v) { return "<td>" + esc(v) + "</td>"; }

function tableRows(rows, cells, colspan) {
  if (!rows.length) {
    return "<tr><td colspan=\"" + (colspan || 9)
      + "\" class=\"empty\">暂无数据</td></tr>";
  }
  return rows.map(function (row) {
    return "<tr>" + cells(row).join("") + "</tr>";
  }).join("");
}

function renderToolTable(rows) {
  qs("#toolTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(r.tool), num(r.total_tokens), num(r.input_tokens),
      num(r.output_tokens), num(r.reasoning_tokens), num(r.cache_read_tokens),
      num(r.cache_write_tokens), num(r.interactions),
      "<td class=\"num\">" + pct(r.cache_hit_ratio) + "</td>"];
  }, 9);
}

function toolByModel(rows) {
  var map = {};
  rows.forEach(function (r) {
    if (!map[r.model]) map[r.model] = [];
    if (map[r.model].indexOf(r.tool) < 0) map[r.model].push(r.tool);
  });
  return map;
}

function renderModelTable(rows, toolOf) {
  qs("#modelTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(r.model), "<td>" + esc((toolOf[r.model] || []).join(", ")) + "</td>",
      num(r.total_tokens), num(r.cache_read_tokens), num(r.llm_calls)];
  }, 5);
}

function renderProjectTable(rows) {
  qs("#projectTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(r.project), num(r.total_tokens), num(r.cache_read_tokens),
      num(r.interactions)];
  }, 4);
}

function renderRecords(rows) {
  qs("#recordTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(String(r.ts).replace("T", " ").slice(0, 16)), txt(r.tool),
      txt(r.model), num(r.input_tokens), num(r.output_tokens),
      num(r.reasoning_tokens), num(r.cache_read_tokens), num(r.total_tokens)];
  }, 8);
}

// ---------------------------------------------------------------- accounts

function fmtMoney(amount, currency) {
  if (amount === null || amount === undefined) return "-";
  var value = Number(amount);
  if (!isFinite(value)) return "-";
  var digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 1 ? 2 : 4;
  return value.toFixed(digits) + (currency ? " " + currency : "");
}

function renderAccounts(accounts) {
  var rows = (accounts && accounts.latest) || [];
  qs("#accountTable tbody").innerHTML = tableRows(rows, function (r) {
    var valueCell;
    var remainingCell;
    if (r.kind === "quota") {
      valueCell = "<td class=\"num\">" + (r.used_percent === null ? "-"
        : r.used_percent.toFixed(1) + "%") + "</td>";
      remainingCell = "<td class=\"num\">" + (r.remaining_percent === null ? "-"
        : r.remaining_percent.toFixed(1) + "%") + "</td>";
    } else {
      valueCell = "<td class=\"num\">" + fmtMoney(r.amount, r.currency) + "</td>";
      remainingCell = "<td class=\"num\">-</td>";
    }
    var reset = r.resets_at
      ? "<td>" + String(r.resets_at).replace("T", " ").slice(0, 16)
        + (r.window_minutes
           ? " <span class=\"tag\">" + r.window_minutes + "min</span>" : "")
        + "</td>"
      : "<td>-</td>";
    return [txt(r.tool), txt(r.kind_label || r.kind),
      txt(r.label || r.account || "-"), valueCell, remainingCell, reset,
      txt(r.plan || "-")];
  }, 7);

  var costRows = (accounts && accounts.cost_summary) || [];
  qs("#costTable tbody").innerHTML = tableRows(costRows, function (r) {
    return [txt(r.tool), "<td class=\"num\">" + fmtMoney(r.amount, r.currency) + "</td>",
      num(r.sessions), num(r.records)];
  }, 4);

  qs("#accountHint").textContent = rows.length
    ? "共 " + rows.length + " 项，来自各工具本地账本"
    : "尚未采集到余额 / 额度数据";
  qs("#costHint").textContent = costRows.length
    ? "周期内合计 " + fmtMoney(accounts.total_cost, costRows[0].currency)
    : "";
}

// ------------------------------------------------------------------ loading

function renderRangeLabel(summary, nav) {
  var label = qs("#rangeLabel");
  if (!label) return;
  var start = (summary.range && summary.range.start) || "";
  var end = (summary.range && summary.range.end) || "";
  var unit = { day: "天", week: "周", month: "月" }[state.period] || "";
  var text;
  if (start && end) {
    text = String(start).slice(0, 10) + " ~ " + String(end).slice(0, 10);
  } else {
    text = "-";
  }
  label.textContent = text;
  label.title = state.span + " 个" + unit;
  var next = qs("#nextPeriod");
  if (next) next.disabled = !!(nav && nav.next_is_future);
}

async function load() {
  var status = qs("#status");
  status.textContent = "加载中…";
  try {
    var query = params();
    var results = await Promise.all([
      getJSON("/api/summary?" + query),
      getJSON("/api/series?" + query),
      getJSON("/api/breakdown?" + query),
      getJSON("/api/records?" + query + "&limit=30"),
      getJSON("/api/nav?" + query)
    ]);
    var summary = results[0];
    var series = results[1];
    var breakdown = results[2];
    var records = results[3];
    var nav = results[4];

    state.series = series.buckets || [];
    state.last = { summary: summary, series: series, breakdown: breakdown };

    renderRangeLabel(summary, nav);
    renderKpis(summary);
    renderLineChart(state.series);
    renderAccounts(summary.accounts);
    renderToolTable(breakdown.by_tool);
    renderModelTable(breakdown.by_model, toolByModel(breakdown.by_tool_model));
    renderProjectTable(breakdown.by_project);
    renderRecords(records.records);

    qs("#recordHint").textContent = "最近 " + records.records.length
      + " 条 / 全库 " + records.total + " 条";
    qs("#export").href = "/api/export.csv?" + query;
    if (summary.last_run) {
      status.textContent = "上次采集 "
        + String(summary.last_run.started_at).replace("T", " ").slice(0, 19)
        + " · 新增 " + summary.last_run.inserted + " 条";
    } else {
      status.textContent = "尚未采集";
    }
  } catch (err) {
    status.textContent = "加载失败: " + err.message;
  }
}

async function collect() {
  var btn = qs("#collect");
  btn.disabled = true;
  btn.textContent = "采集中…";
  try {
    var res = await fetch("/api/collect", { method: "POST" });
    if (res.status === 409) {
      qs("#status").textContent = "已有采集任务在进行";
      return;
    }
    var deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise(function (r) { setTimeout(r, 1500); });
      var st = await getJSON("/api/collect/status");
      if (!st.running) {
        qs("#status").textContent = st.last && st.last.error
          ? "采集失败: " + st.last.error
          : "采集完成，新增 " + ((st.last && st.last.inserted) || 0) + " 条";
        break;
      }
    }
    await load();
  } catch (err) {
    qs("#status").textContent = "采集失败: " + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "重新采集";
  }
}

// --------------------------------------------------------------------- boot

async function boot() {
  buildTimezoneOptions();
  initControls();
  syncControls();
  try {
    var info = await getJSON("/api/tools");
    state.knownTools = info.tools.map(function (t) { return t.key; });
    state.knownModels = info.models;
    qs("#dbPath").textContent = "数据库: " + info.db_path;
    buildChips(qs("#toolChips"), state.knownTools, state.tools,
                function () { load(); }, true);
    buildChips(qs("#modelChips"), state.knownModels, state.models, function () { load(); });
  } catch (err) {
    qs("#status").textContent = "初始化失败: " + err.message;
  }
  load();
}

document.addEventListener("DOMContentLoaded", boot);

// Keyboard shortcuts for period navigation.
document.addEventListener("keydown", function (event) {
  if (event.target && /INPUT|SELECT|TEXTAREA/.test(event.target.tagName)) return;
  if (event.key === "ArrowLeft") { event.preventDefault(); shift(-1); }
  if (event.key === "ArrowRight") { event.preventDefault(); shift(1); }
});
