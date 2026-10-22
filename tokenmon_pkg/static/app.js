"use strict";

function initialParams() {
  const params = new URLSearchParams(window.location.search);
  const period = params.get("period");
  const span = parseInt(params.get("span") || "", 10);
  return {
    period: ["day", "week", "month"].indexOf(period) >= 0 ? period : "day",
    span: Number.isFinite(span) && span > 0 ? Math.min(span, 90) : 1,
    tz: params.get("tz") || "",
    tools: (params.get("tools") || "").split(",").filter(Boolean),
    models: (params.get("model") || "").split(",").filter(Boolean),
  };
}

const urlParams = initialParams();

const state = {
  period: urlParams.period,
  span: urlParams.span,
  tz: urlParams.tz,
  tools: urlParams.tools,
  models: urlParams.models,
  knownTools: [],
  knownModels: [],
  last: null,
};

const TOOL_COLORS = {
  codex: "#5b8cff",
  claudecode: "#f0a742",
  zcode: "#37c2a8",
  opencode: "#a06bff",
  "deepseek-harness": "#4d9fff",
  antigravity: "#ef5f6b",
  qwencode: "#8bd45a",
  cursor: "#e879b9",
};

function qs(sel) { return document.querySelector(sel); }
function qsa(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

function fmt(n) {
  if (n === null || n === undefined) return "-";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
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
  const p = new URLSearchParams();
  p.set("period", state.period);
  p.set("span", String(state.span));
  if (state.tz) p.set("tz", state.tz);
  if (state.tools.length) p.set("tools", state.tools.join(","));
  if (state.models.length) p.set("model", state.models.join(","));
  return p.toString();
}

async function getJSON(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(path + " -> " + res.status);
  return res.json();
}

// ---------------------------------------------------------------- filters

function syncControls() {
  qsa("#period button").forEach(function (b) {
    b.classList.toggle("active", b.dataset.value === state.period);
  });
  qs("#span").value = String(state.span);
  qs("#tz").value = state.tz;
}

function initPeriod() {
  syncControls();
  qsa("#period button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      qsa("#period button").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      state.period = btn.dataset.value;
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
  qs("#refresh").addEventListener("click", load);
  qs("#collect").addEventListener("click", collect);
}

function buildChips(container, values, selected, onChange) {
  container.innerHTML = "";
  values.forEach(function (value) {
    const chip = document.createElement("span");
    chip.className = "chip" + (selected.indexOf(value) >= 0 ? " on" : "");
    chip.textContent = value;
    chip.title = value;
    chip.addEventListener("click", function () {
      const index = selected.indexOf(value);
      if (index >= 0) selected.splice(index, 1);
      else selected.push(value);
      chip.classList.toggle("on");
      onChange();
    });
    container.appendChild(chip);
  });
}

function buildTimezoneOptions() {
  const zones = ["", "UTC", "Asia/Shanghai", "Asia/Singapore", "Asia/Tokyo",
                 "Europe/London", "Europe/Berlin", "America/New_York",
                 "America/Los_Angeles", "Australia/Sydney"];
  const sel = qs("#tz");
  zones.forEach(function (z) {
    const opt = document.createElement("option");
    opt.value = z;
    opt.textContent = z || "本地时区 (" + Intl.DateTimeFormat().resolvedOptions().timeZone + ")";
    sel.appendChild(opt);
  });
}

// ---------------------------------------------------------------- render

function renderKpis(data) {
  const t = data.totals;
  const cards = [
    { label: "总 Token", value: fmt(t.total_tokens), extra: fmtFull(t.total_tokens) + " tokens" },
    { label: "输入 Token", value: fmt(t.input_tokens), extra: "不含缓存部分" },
    { label: "输出 Token", value: fmt(t.output_tokens), extra: "推理 " + fmt(t.reasoning_tokens) },
    { label: "缓存命中", value: fmt(t.cache_read_tokens), extra: "命中率 " + pct(t.cache_hit_ratio), accent: true },
    { label: "缓存写入", value: fmt(t.cache_write_tokens), extra: "cache creation" },
    { label: "交互次数", value: fmtFull(t.interactions), extra: "用户提问数" },
    { label: "LLM 调用", value: fmtFull(t.llm_calls), extra: "模型请求数" },
    { label: "工具调用", value: fmtFull(t.tool_calls), extra: "tool calls" },
    { label: "使用工具", value: String(t.tool_count), extra: t.tools.join(", ") },
    { label: "使用模型", value: String(t.model_count), extra: t.models.slice(0, 3).join(", ") },
  ];
  qs("#kpis").innerHTML = cards.map(function (c) {
    return "<div class=\"kpi" + (c.accent ? " accent" : "") + "\">"
      + "<div class=\"label\">" + esc(c.label) + "</div>"
      + "<div class=\"value\">" + esc(c.value) + "</div>"
      + "<div class=\"extra\">" + esc(c.extra) + "</div></div>";
  }).join("");
}

function renderChart(buckets) {
  const chart = qs("#chart");
  if (!buckets.length) {
    chart.innerHTML = "<div class=\"empty\">当前周期暂无数据，请先点击「重新采集」</div>";
    qs("#seriesLegend").innerHTML = "";
    qs("#chartHint").textContent = "";
    return;
  }
  const max = buckets.reduce(function (m, b) {
    return Math.max(m, b.input_tokens + b.output_tokens + b.reasoning_tokens
      + b.cache_read_tokens + b.cache_write_tokens);
  }, 0);
  chart.innerHTML = buckets.map(function (b) {
    const total = b.input_tokens + b.output_tokens + b.reasoning_tokens
      + b.cache_read_tokens + b.cache_write_tokens;
    const seg = function (cls, value) {
      const width = max > 0 ? (value / max) * 100 : 0;
      return width > 0 ? "<div class=\"bar-seg " + cls + "\" style=\"width:"
        + width.toFixed(2) + "%\" title=\"" + fmtFull(value) + "\"></div>" : "";
    };
    return "<div class=\"bar-row\"><div class=\"name\">" + esc(b.label) + "</div>"
      + "<div><div class=\"bar-track\">"
      + seg("input", b.input_tokens)
      + seg("cache", b.cache_read_tokens)
      + seg("output", b.output_tokens)
      + seg("reasoning", b.reasoning_tokens)
      + "</div><div class=\"bar-note\">" + fmt(total) + " tokens · 交互 "
      + fmtFull(b.interactions) + " · 命中 " + pct(b.cache_hit_ratio) + "</div></div></div>";
  }).join("");
  qs("#seriesLegend").innerHTML =
    "<span><i style=\"background:#5b8cff\"></i>输入</span>"
    + "<span><i style=\"background:#37c2a8\"></i>缓存命中</span>"
    + "<span><i style=\"background:#f0a742\"></i>输出</span>"
    + "<span><i style=\"background:#a06bff\"></i>推理</span>";
  qs("#chartHint").textContent = "周期：" + state.period + " · 共 "
    + buckets.length + " 个时间桶";
}

function tableRows(rows, cells) {
  if (!rows.length) return "<tr><td colspan=\"9\" class=\"empty\">暂无数据</td></tr>";
  return rows.map(function (row) {
    return "<tr>" + cells(row).map(function (c) { return c; }).join("") + "</tr>";
  }).join("");
}

function num(v) { return "<td class=\"num\">" + fmtFull(v) + "</td>"; }
function txt(v) { return "<td>" + esc(v) + "</td>"; }

function renderToolTable(rows) {
  qs("#toolTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(r.tool), num(r.total_tokens), num(r.input_tokens),
      num(r.output_tokens), num(r.reasoning_tokens), num(r.cache_read_tokens),
      num(r.cache_write_tokens), num(r.interactions),
      "<td class=\"num\">" + pct(r.cache_hit_ratio) + "</td>"];
  });
}

function renderModelTable(rows, toolOf) {
  qs("#modelTable tbody").innerHTML = tableRows(rows, function (r) {
    const tools = (toolOf[r.model] || []).join(", ");
    return [txt(r.model), "<td>" + esc(tools) + "</td>", num(r.total_tokens),
      num(r.cache_read_tokens), num(r.llm_calls)];
  });
}

function renderProjectTable(rows) {
  qs("#projectTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(r.project), num(r.total_tokens), num(r.cache_read_tokens),
      num(r.interactions)];
  });
}

function renderRecords(rows) {
  qs("#recordTable tbody").innerHTML = tableRows(rows, function (r) {
    return [txt(r.ts.replace("T", " ").slice(0, 19)), txt(r.tool), txt(r.model),
      txt(r.project), num(r.input_tokens), num(r.output_tokens),
      num(r.reasoning_tokens), num(r.cache_read_tokens),
      num(r.cache_write_tokens), num(r.total_tokens)];
  });
}

// ------------------------------------------------------------- accounts

function fmtMoney(amount, currency) {
  if (amount === null || amount === undefined) return "-";
  const value = Number(amount);
  if (!isFinite(value)) return "-";
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 1 ? 2 : 4;
  return value.toFixed(digits) + (currency ? " " + currency : "");
}

function fmtTs(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").slice(0, 16);
}

function renderAccounts(accounts) {
  const rows = (accounts && accounts.latest) || [];
  qs("#accountTable tbody").innerHTML = rows.length
    ? rows.map(function (r) {
        let valueCell;
        let remainingCell;
        if (r.kind === "quota") {
          valueCell = "<td class=\"num\">" + (r.used_percent === null ? "-"
            : r.used_percent.toFixed(1) + "%") + "</td>";
          remainingCell = "<td class=\"num\">"
            + (r.remaining_percent === null ? "-"
               : r.remaining_percent.toFixed(1) + "%") + "</td>";
        } else {
          valueCell = "<td class=\"num\">"
            + fmtMoney(r.amount, r.currency) + "</td>";
          remainingCell = "<td class=\"num\">-</td>";
        }
        const reset = r.resets_at
          ? "<td>" + fmtTs(r.resets_at)
            + (r.window_minutes ? " <span class=\"tag\">" + r.window_minutes + "min</span>" : "")
            + "</td>"
          : "<td>-</td>";
        return "<tr><td>" + esc(r.tool) + "</td>"
          + "<td>" + esc(r.kind_label || r.kind) + "</td>"
          + "<td>" + esc(r.label || r.account || "-") + "</td>"
          + valueCell + remainingCell + reset
          + "<td>" + esc(r.plan || "-") + "</td></tr>";
      }).join("")
    : "<tr><td colspan=\"7\" class=\"empty\">暂无余额数据</td></tr>";

  const costRows = (accounts && accounts.cost_summary) || [];
  qs("#costTable tbody").innerHTML = costRows.length
    ? costRows.map(function (r) {
        return "<tr><td>" + esc(r.tool) + "</td>"
          + "<td class=\"num\">" + fmtMoney(r.amount, r.currency) + "</td>"
          + "<td class=\"num\">" + fmtFull(r.sessions) + "</td>"
          + "<td class=\"num\">" + fmtFull(r.records) + "</td></tr>";
      }).join("")
    : "<tr><td colspan=\"4\" class=\"empty\">暂无花费数据</td></tr>";

  qs("#accountHint").textContent = rows.length
    ? "共 " + rows.length + " 项，数据来自各工具本地账本"
    : "尚未采集到余额 / 额度数据";
  qs("#costHint").textContent = costRows.length
    ? "周期内合计 " + fmtMoney(accounts.total_cost, costRows[0].currency)
    : "";
}

// ---------------------------------------------------------------- loading

async function load() {
  const status = qs("#status");
  status.textContent = "加载中…";
  try {
    const query = params();
    const results = await Promise.all([
      getJSON("/api/summary?" + query),
      getJSON("/api/series?" + query),
      getJSON("/api/breakdown?" + query),
      getJSON("/api/records?" + query + "&limit=40"),
    ]);
    const summary = results[0];
    const series = results[1];
    const breakdown = results[2];
    const records = results[3];
    state.last = { summary: summary, series: series, breakdown: breakdown };

    renderKpis(summary);
    renderAccounts(summary.accounts);
    renderChart(series.buckets);
    renderToolTable(breakdown.by_tool);
    renderModelTable(breakdown.by_model, toolByModel(breakdown.by_tool_model));
    renderProjectTable(breakdown.by_project);
    renderRecords(records.records);

    qs("#recordHint").textContent = "最近 " + records.records.length
      + " 条 / 全库 " + records.total + " 条";
    qs("#export").href = "/api/export.csv?" + query;
    if (summary.last_run) {
      status.textContent = "上次采集 " + String(summary.last_run.started_at).replace("T", " ").slice(0, 19)
        + " · 新增 " + summary.last_run.inserted + " 条";
    } else {
      status.textContent = "尚未采集";
    }
  } catch (err) {
    status.textContent = "加载失败: " + err.message;
  }
}

function toolByModel(rows) {
  const map = {};
  rows.forEach(function (r) {
    if (!map[r.model]) map[r.model] = [];
    if (map[r.model].indexOf(r.tool) < 0) map[r.model].push(r.tool);
  });
  return map;
}

async function collect() {
  const btn = qs("#collect");
  btn.disabled = true;
  btn.textContent = "采集中…";
  try {
    const res = await fetch("/api/collect", { method: "POST" });
    if (res.status === 409) {
      qs("#status").textContent = "已有采集任务在进行";
      return;
    }
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise(function (r) { setTimeout(r, 1500); });
      const st = await getJSON("/api/collect/status");
      if (!st.running) {
        if (st.last && st.last.error) {
          qs("#status").textContent = "采集失败: " + st.last.error;
        } else if (st.last) {
          qs("#status").textContent = "采集完成，新增 "
            + (st.last.inserted || 0) + " 条";
        }
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

// ---------------------------------------------------------------- boot

async function boot() {
  buildTimezoneOptions();
  initPeriod();
  try {
    const info = await getJSON("/api/tools");
    state.knownTools = info.tools.map(function (t) { return t.key; });
    state.knownModels = info.models;
    qs("#dbPath").textContent = "数据库: " + info.db_path;
    buildChips(qs("#toolChips"), state.knownTools, state.tools, function () { load(); });
    buildChips(qs("#modelChips"), state.knownModels, state.models, function () { load(); });
  } catch (err) {
    qs("#status").textContent = "初始化失败: " + err.message;
  }
  load();
}

document.addEventListener("DOMContentLoaded", boot);
