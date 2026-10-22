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
  metric: "token_split"
};

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

// Model filter is a dropdown: one model at a time keeps the query simple and
// the list short even with dozens of models across five tools.
function buildModelSelect(models) {
  state.knownModels = models || [];
  var sel = qs("#modelSelect");
  if (!sel) return;
  sel.innerHTML = "";
  var all = document.createElement("option");
  all.value = "";
  all.textContent = "全部模型 (" + state.knownModels.length + ")";
  sel.appendChild(all);
  state.knownModels.forEach(function (name) {
    var opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  // Reflect the URL selection, if any.
  state.models = state.models.filter(function (m) {
    return state.knownModels.indexOf(m) >= 0;
  }).slice(0, 1);
  sel.value = state.models[0] || "";
  if (sel.dataset.bound !== "1") {
    sel.dataset.bound = "1";
    sel.addEventListener("change", function (e) {
      state.models = e.target.value ? [e.target.value] : [];
      load();
    });
  }
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

// Series definitions.  The default "token split" view draws one line per
// token component (input / output / cache) so the composition of the usage
// is visible at a glance; the other metrics are single line views.
var SERIES_GROUPS = [
  {
    key: "token_split",
    label: "Token 构成",
    lines: [
      { key: "input_tokens", label: "输入", color: "#5b8cff" },
      { key: "cache_read_tokens", label: "缓存命中", color: "#37c2a8" },
      { key: "output_tokens", label: "输出", color: "#f0a742" },
      { key: "reasoning_tokens", label: "推理", color: "#a06bff" }
    ]
  },
  {
    key: "total_tokens",
    label: "总 Token",
    lines: [
      { key: "total_tokens", label: "总 Token", color: "#5b8cff" }
    ]
  },
  {
    key: "cache_read_tokens",
    label: "缓存命中",
    lines: [
      { key: "cache_read_tokens", label: "缓存命中", color: "#37c2a8" }
    ]
  },
  {
    key: "interactions",
    label: "交互次数",
    lines: [
      { key: "interactions", label: "交互次数", color: "#8bd45a" }
    ]
  },
  {
    key: "llm_calls",
    label: "LLM 调用",
    lines: [
      { key: "llm_calls", label: "LLM 调用", color: "#4d9fff" }
    ]
  },
  {
    key: "tool_calls",
    label: "工具调用",
    lines: [
      { key: "tool_calls", label: "工具调用", color: "#ef5f6b" }
    ]
  }
];

function groupDef(key) {
  for (var i = 0; i < SERIES_GROUPS.length; i++) {
    if (SERIES_GROUPS[i].key === key) return SERIES_GROUPS[i];
  }
  return SERIES_GROUPS[0];
}

// Catmull-Rom spline converted to cubic Bezier segments.  Straight L
// segments between sparse daily buckets read as a jagged polyline, while
// the spline keeps the data points exact and bends the line between them.
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
  var group = groupDef(state.metric);
  renderMetricToolbar(group);

  if (!buckets.length) {
    host.innerHTML = "<div class=\"empty\">当前周期暂无数据</div>";
    qs("#chartHint").textContent = "";
    return;
  }

  var width = Math.max(560, host.clientWidth || 860);
  var height = 320;
  var pad = { top: 22, right: 18, bottom: 32, left: 62 };
  var plotW = width - pad.left - pad.right;
  var plotH = height - pad.top - pad.bottom;

  // One shared scale across every line of the group so the stacked bars
  // are comparable.  For the composition view the stack total is what
  // the y axis measures, so the axis is derived from the sums.
  var stacked = group.lines.length > 1;
  var totals = buckets.map(function (b) {
    if (!stacked) return Number(b[group.lines[0].key]) || 0;
    return group.lines.reduce(function (sum, line) {
      return sum + (Number(b[line.key]) || 0);
    }, 0);
  });
  var max = Math.max.apply(null, totals.concat([0]));
  var top = max === 0 ? 1 : max * 1.16;

  function xAt(i) {
    var slot = plotW / buckets.length;
    return pad.left + slot * i + slot / 2;
  }
  function slotW() { return plotW / buckets.length; }
  function yAt(v) { return pad.top + plotH - (v / top) * plotH; }

  // Bars get thin when there are many buckets; keep a readable gap.
  var barW = Math.max(2, Math.min(34, slotW() * 0.66));
  var radius = Math.min(3, barW / 3);

  var parts = [];
  parts.push("<svg viewBox=\"0 0 " + width + " " + height + "\" role=\"img\">");

  // horizontal grid + y labels
  var ticks = 4;
  for (var t = 0; t <= ticks; t++) {
    var v = (top * t) / ticks;
    var y = yAt(v);
    parts.push("<line class=\"grid-line\" x1=\"" + pad.left + "\" y1=\"" + y.toFixed(1)
               + "\" x2=\"" + (width - pad.right) + "\" y2=\"" + y.toFixed(1) + "\"/>");
    parts.push("<text class=\"axis-label\" x=\"" + (pad.left - 9) + "\" y=\""
               + (y + 3.5).toFixed(1) + "\" text-anchor=\"end\">" + esc(fmt(v)) + "</text>");
  }

  // x labels, thinned so they never overlap
  var every = Math.max(1, Math.ceil(buckets.length / 14));
  buckets.forEach(function (b, i) {
    if (i % every !== 0 && i !== buckets.length - 1) return;
    parts.push("<text class=\"axis-label\" x=\"" + xAt(i).toFixed(1) + "\" y=\""
               + (height - 11) + "\" text-anchor=\"middle\">" + esc(shortLabel(b.label)) + "</text>");
  });

  // Stacked bars.  Each bar is a rounded top segment per series so the
  // composition reads from the bottom up, matching the legend order.
  var peakIndex = 0;
  totals.forEach(function (v, i) { if (v > totals[peakIndex]) peakIndex = i; });

  buckets.forEach(function (bucket, i) {
    var cx = xAt(i);
    var x0 = cx - barW / 2;
    if (stacked) {
      var acc = 0;
      group.lines.forEach(function (line, li) {
        var v = Number(bucket[line.key]) || 0;
        if (v <= 0) return;
        var yTop = yAt(acc + v);
        var yBottom = yAt(acc);
        var h = Math.max(0.6, yBottom - yTop);
        var isTop = li === group.lines.length - 1;
        // Only the topmost visible segment gets rounded corners.
        var r = isTop ? Math.min(radius, h / 2) : 0;
        var d = barPath(x0, yTop, barW, h, r);
        parts.push("<path class=\"bar-seg\" d=\"" + d + "\" fill=\"" + line.color
                   + "\" data-line=\"" + esc(line.key) + "\"/>");
        acc += v;
      });
    } else {
      var single = group.lines[0];
      var sv = Number(bucket[single.key]) || 0;
      var sy = yAt(sv);
      var sh = Math.max(0.6, yAt(0) - sy);
      var sr = Math.min(radius, sh / 2);
      parts.push("<path class=\"bar-seg\" d=\"" + barPath(x0, sy, barW, sh, sr)
                 + "\" fill=\"" + single.color + "\"/>");
    }
  });

  // Peak value label above the tallest bar.
  if (totals[peakIndex] > 0) {
    parts.push("<text class=\"peak-label\" x=\"" + xAt(peakIndex).toFixed(1)
               + "\" y=\"" + (yAt(totals[peakIndex]) - 8).toFixed(1)
               + "\" text-anchor=\"middle\">" + esc(fmt(totals[peakIndex])) + "</text>");
  }

  parts.push("<line class=\"hover-line\" id=\"hoverLine\" x1=\"0\" y1=\"" + pad.top
             + "\" x2=\"0\" y2=\"" + (pad.top + plotH) + "\" style=\"display:none\"/>");
  parts.push("</svg>");
  parts.push("<div class=\"linechart-tip\" id=\"chartTip\"></div>");
  host.innerHTML = parts.join("");

  qs("#chartHint").textContent = group.label + " · 共 " + buckets.length
    + " 个时间桶 · 峰值 " + fmt(max);

  attachChartHover(host, buckets, group, xAt, width);
}

// Rounded-top rectangle as an SVG path so stacked segments can round only
// the segment that closes the stack.
function barPath(x, y, w, h, r) {
  if (h <= 0) return "";
  var rr = Math.min(r, w / 2, h);
  if (rr <= 0.5) {
    return "M" + x.toFixed(2) + " " + y.toFixed(2)
      + "h" + w.toFixed(2) + "v" + h.toFixed(2) + "h" + (-w).toFixed(2) + "Z";
  }
  return "M" + (x + rr).toFixed(2) + " " + y.toFixed(2)
    + "h" + (w - rr * 2).toFixed(2)
    + "a" + rr.toFixed(2) + " " + rr.toFixed(2) + " 0 0 1 " + rr.toFixed(2) + " " + rr.toFixed(2)
    + "v" + (h - rr).toFixed(2)
    + "h" + (-w).toFixed(2)
    + "v" + (-(h - rr)).toFixed(2)
    + "a" + rr.toFixed(2) + " " + rr.toFixed(2) + " 0 0 1 " + rr.toFixed(2) + " " + (-rr).toFixed(2)
    + "Z";
}
// Metric toolbar: builds once, then just reflects the active group.
function renderMetricToolbar(group) {
  var host = qs("#seriesLegend");
  if (!host) return;
  if (host.dataset.built !== "1") {
    host.dataset.built = "1";
    var wrap = document.createElement("div");
    wrap.className = "chart-toolbar";
    SERIES_GROUPS.forEach(function (g) {
      var chip = document.createElement("span");
      chip.className = "chip" + (g.key === state.metric ? " on" : "");
      chip.textContent = g.label;
      chip.dataset.metric = g.key;
      chip.addEventListener("click", function () {
        state.metric = g.key;
        renderMetricToolbar(groupDef(state.metric));
        renderLineChart(state.series);
      });
      wrap.appendChild(chip);
    });
    host.innerHTML = "";
    host.appendChild(wrap);
  }
  Array.prototype.forEach.call(host.querySelectorAll(".chip"), function (chip) {
    var on = chip.dataset.metric === state.metric;
    chip.classList.toggle("on", on);
    if (on) {
      chip.style.borderColor = group.lines[0].color;
      chip.style.color = group.lines[0].color;
    } else {
      chip.style.borderColor = "";
      chip.style.color = "";
    }
  });
}

// Tooltip lists every line of the active group for the hovered bucket.
function attachChartHover(host, buckets, group, xAt, width) {
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
    // Highlight the hovered column.
    Array.prototype.forEach.call(host.querySelectorAll(".bar-seg"), function (seg) {
      seg.style.opacity = "";
    });
    var bucket = buckets[best];
    if (!bucket) return;
    Array.prototype.forEach.call(host.querySelectorAll(".bar-seg"), function (seg, index) {
      var owner = Math.floor(index / Math.max(1, group.lines.length));
      seg.style.opacity = owner === best ? "1" : "0.42";
    });
    if (hoverLine) {
      hoverLine.setAttribute("x1", String(xAt(best)));
      hoverLine.setAttribute("x2", String(xAt(best)));
      hoverLine.style.display = "block";
    }
    var rows = group.lines.map(function (line) {
      return "<div class=\"t-row\"><span><i style=\"background:" + line.color
        + "\"></i>" + esc(line.label) + "</span><span>"
        + fmtFull(bucket[line.key]) + "</span></div>";
    }).join("");
    tip.innerHTML = "<div class=\"t-title\">" + esc(bucket.label) + "</div>" + rows
      + "<div class=\"t-row\"><span>\u4ea4互次数</span><span>"
      + fmtFull(bucket.interactions) + "</span></div>"
      + "<div class=\"t-row\"><span>\u547d中率</span><span>"
      + pct(bucket.cache_hit_ratio) + "</span></div>";
    tip.classList.add("on");
    var leftPct = (xAt(best) / width) * 100;
    tip.style.left = Math.min(70, Math.max(0, leftPct)) + "%";
    tip.style.top = "6px";
  });
  host.addEventListener("mouseleave", function () {
    tip.classList.remove("on");
    if (hoverLine) hoverLine.style.display = "none";
  });
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
    { label: "交互次数", value: fmtFull(t.interactions), extra: "用户提问轮次" },
    { label: "LLM 调用", value: fmtFull(t.llm_calls), extra: "模型请求数" },
    { label: "工具调用", value: fmtFull(t.tool_calls), extra: "tool calls" },
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
    buildModelSelect(info.models);
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
