"""Flask application serving the tokenmon analytics UI and JSON API.

Routes
------
GET /                      single page dashboard
GET /api/summary           KPI cards for the selected period
GET /api/series            bucket series for the selected period
GET /api/breakdown         breakdown by tool / model / project / tool+model
GET /api/tools             detected data directories
GET /api/records           raw records (paged)
POST /api/collect          run a collection in the background
GET /api/runs              recent collection runs
GET /api/export.csv        CSV export of the current period

Every endpoint accepts the same query parameters:

    period = day | week | month
    tools  = comma separated tool keys
    model  = comma separated models
    tz     = IANA time zone name
    span   = how many periods back to include
"""

from __future__ import annotations

import csv
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from . import paths as P
from .analysis import (
    PERIODS,
    build_account_report,
    build_report,
    get_zone,
    resolve_range,
    to_utc,
)
from .collectors import DISPLAY_NAMES, SHORT_NAMES, TOOL_ORDER, known
from .model import UsageRecord
from .store import Store


def _filters() -> dict:
    period = request.args.get("period", "day")
    if period not in PERIODS:
        period = "day"
    tools = known((request.args.get("tools") or "").split(","))
    models = [m.strip() for m in (request.args.get("model") or "").split(",") if m.strip()]
    tz_name = request.args.get("tz") or None
    try:
        span = max(1, min(60, int(request.args.get("span", 1))))
    except (TypeError, ValueError):
        span = 1
    anchor = request.args.get("anchor") or None
    return {"period": period, "tools": tools or None, "models": models or None,
            "tz": tz_name, "span": span, "anchor": anchor}


def create_app(db_path: str | None = None) -> Flask:
    """Application factory.

    A fresh :class:\"\"\"Store\"\"\" is opened per request so the database can be replaced
    while the server runs and so concurrent readers never share a connection.
    """
    resolved = Path(db_path) if db_path else P.default_db_path()
    app = Flask(__name__,
                template_folder=str(Path(__file__).resolve().parent / "templates"),
                static_folder=str(Path(__file__).resolve().parent / "static"))
    app.config["TOKENMON_DB"] = str(resolved)
    app.config["JSON_SORT_KEYS"] = False

    def open_store() -> Store:
        return Store(app.config["TOKENMON_DB"])

    # -- pages -----------------------------------------------------------
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            tools=TOOL_ORDER,
            display=DISPLAY_NAMES,
            db_path=app.config["TOKENMON_DB"],
        )

    # -- api -------------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        f = _filters()
        with open_store() as store:
            data = build_report(store, period=f["period"], tools=f["tools"],
                                tz_name=f["tz"], span=f["span"],
                                anchor=f["anchor"], models=f["models"])
            record_count = store.count()
            last_run = store.last_runs(1)
        return jsonify({
            "period": f["period"],
            "timezone": data["timezone"],
            "range": data["range"],
            "anchor": f["anchor"],
            "span": f["span"],
            "period": f["period"],
            "totals": data["totals"],
            "accounts": data["accounts"],
            "by_tool": data["by_tool"],
            "record_count": record_count,
            "db_path": app.config["TOKENMON_DB"],
            "last_run": last_run[0] if last_run else None,
        })

    @app.get("/api/series")
    def api_series():
        f = _filters()
        with open_store() as store:
            data = build_report(store, period=f["period"], tools=f["tools"],
                                tz_name=f["tz"], span=f["span"],
                                anchor=f["anchor"], models=f["models"])
        return jsonify({
            "period": f["period"],
            "timezone": data["timezone"],
            "buckets": data["buckets"],
        })

    @app.get("/api/breakdown")
    def api_breakdown():
        f = _filters()
        with open_store() as store:
            data = build_report(store, period=f["period"], tools=f["tools"],
                                tz_name=f["tz"], span=f["span"],
                                anchor=f["anchor"], models=f["models"])
            if f["models"]:
                filtered = [r for r in store.iter_records(tools=f["tools"],
                                                          models=f["models"])]
            else:
                filtered = []
        return jsonify({
            "period": f["period"],
            "dimension": request.args.get("dimension", "tool"),
            "by_tool": data["by_tool"],
            "by_model": data["by_model"],
            "by_project": data["by_project"],
            "by_tool_model": data["by_tool_model"],
            "filtered_records": len(filtered),
        })

    @app.get("/api/accounts")
    def api_accounts():
        """Latest balances, quota windows and cost totals."""
        f = _filters()
        with open_store() as store:
            from .analysis import resolve_range

            start, end = resolve_range(
                f["period"],
                anchor=to_utc(f["anchor"]) if f["anchor"] else None,
                tz=get_zone(f["tz"]), span=f["span"])
            data = build_account_report(store, tz_name=f["tz"], since=start,
                                        until=end, tools=f["tools"])
        return jsonify(data)

    @app.get("/api/nav")
    def api_nav():
        """Previous / next period anchors for the current selection."""
        f = _filters()
        from .analysis import shift_period

        tz = get_zone(f["tz"])
        current = to_utc(f["anchor"]) if f["anchor"] else datetime.now(tz)
        previous = shift_period(f["period"], current, tz, steps=-(f["span"] or 1))
        nxt = shift_period(f["period"], current, tz, steps=(f["span"] or 1))
        now = datetime.now(tz)
        return jsonify({
            "period": f["period"],
            "span": f["span"],
            "current": current.isoformat(),
            "previous": previous.isoformat(),
            "next": nxt.isoformat(),
            "next_is_future": nxt > now,
        })

    @app.get("/api/tool-models")
    def api_tool_models():
        """Models used by one tool, for the second-level model filter."""
        tool = request.args.get("tools") or ""
        with open_store() as store:
            rows = store._conn.execute(
                "SELECT DISTINCT model FROM usage_records WHERE tool = ?"
                " AND model != '' AND model != 'unknown' ORDER BY model",
                (tool,),
            ).fetchall()
        return jsonify({"tool": tool, "models": [row["model"] for row in rows]})

    @app.get("/api/tools")
    def api_tools():
        resolved = P.resolve_all()
        with open_store() as store:
            models = store.distinct("model")
            projects = store.distinct("project")
        # Why a tool has no data, in one line the UI can show verbatim.
        reasons = {
            "cursor": "未安装：未找到 Cursor 数据目录",
            "qwencode": "未安装：未找到 QwenCode 数据目录",
            "workbuddy": "仅有上下文占用快照：会话日志里没有 providerData.usage 记录",
        }
        return jsonify({
            "tools": [
                {
                    "key": tool,
                    "name": DISPLAY_NAMES.get(tool, tool),
                    "short": SHORT_NAMES.get(tool, tool),
                    "found": resolved[tool].found,
                    "roots": [str(r) for r in resolved[tool].roots],
                    "checked": [str(c) for c in resolved[tool].checked],
                    "notes": resolved[tool].notes,
                    "reason": reasons.get(tool, ""),
                }
                for tool in TOOL_ORDER
            ],
            "models": models,
            "projects": projects,
            "db_path": app.config["TOKENMON_DB"],
        })

    @app.get("/api/records")
    def api_records():
        f = _filters()
        try:
            limit = max(1, min(500, int(request.args.get("limit", 100))))
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            limit, offset = 100, 0
        with open_store() as store:
            rows = []
            for index, record in enumerate(store.iter_records(
                    tools=f["tools"], models=f["models"])):
                if index < offset:
                    continue
                if len(rows) >= limit:
                    break
                rows.append(_record_json(record))
            total = store.count()
        return jsonify({"total": total, "limit": limit, "offset": offset,
                        "records": rows})

    @app.get("/api/runs")
    def api_runs():
        with open_store() as store:
            runs = store.last_runs(20)
            detail = []
            for run in runs[:5]:
                detail.append({"run": run, "sources": store.last_run_sources(run["id"])})
        return jsonify({"runs": runs, "detail": detail})

    @app.get("/api/export-accounts.csv")
    def api_export_accounts():
        """Export balance / quota / cost snapshots as CSV."""
        with open_store() as store:
            rows = list(store.iter_accounts())
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["ts", "tool", "kind", "account", "label", "currency",
                         "amount", "used_percent", "remaining_percent",
                         "window_minutes", "resets_at", "plan"])
        for row in rows:
            writer.writerow([
                row.ts.isoformat(), row.tool, row.kind, row.account, row.label,
                row.currency, row.amount, row.used_percent,
                row.remaining_percent, row.window_minutes,
                row.resets_at.isoformat() if row.resets_at else "", row.plan])
        return Response(buffer.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=tokenmon-accounts.csv"})

    @app.get("/api/export.csv")
    def api_export():
        f = _filters()
        with open_store() as store:
            data = build_report(store, period=f["period"], tools=f["tools"],
                                tz_name=f["tz"], span=f["span"],
                                anchor=f["anchor"], models=f["models"])
            rows = list(store.iter_records(tools=f["tools"], models=f["models"]))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["ts", "tool", "model", "project", "session_id",
                         "input_tokens", "output_tokens", "reasoning_tokens",
                         "cache_read_tokens", "cache_write_tokens", "total_tokens",
                         "llm_calls", "tool_calls", "interactions"])
        for record in rows:
            writer.writerow([
                record.ts.isoformat(), record.tool, record.model, record.project,
                record.session_id, record.input_tokens, record.output_tokens,
                record.reasoning_tokens, record.cache_read_tokens,
                record.cache_write_tokens, record.total_tokens, record.llm_calls,
                record.tool_calls, record.is_interaction])
        name = "tokenmon-" + f["period"] + "-" + datetime.now(timezone.utc).strftime("%Y%m%d") + ".csv"
        return Response(buffer.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=" + name})

    # -- collection ------------------------------------------------------
    state = {"running": False, "last": None}
    lock = threading.Lock()

    @app.post("/api/collect")
    def api_collect():
        with lock:
            if state["running"]:
                return jsonify({"status": "already-running"}), 409
            state["running"] = True

        def run():
            try:
                from .cli import collect_once

                summary = collect_once(app.config["TOKENMON_DB"])
                with lock:
                    state["last"] = summary
            except Exception as exc:
                with lock:
                    state["last"] = {"error": str(exc)}
            finally:
                with lock:
                    state["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return jsonify({"status": "started"})

    @app.get("/api/collect/status")
    def api_collect_status():
        with lock:
            return jsonify({"running": state["running"], "last": state["last"]})

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def server_error(exc):  # pragma: no cover - defensive
        return jsonify({"error": str(exc)}), 500

    return app


def _record_json(record: UsageRecord) -> dict:
    return {
        "ts": record.ts.isoformat(),
        "tool": record.tool,
        "model": record.model,
        "provider": record.provider,
        "project": record.project,
        "session_id": record.session_id,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "reasoning_tokens": record.reasoning_tokens,
        "cache_read_tokens": record.cache_read_tokens,
        "cache_write_tokens": record.cache_write_tokens,
        "total_tokens": record.total_tokens,
        "llm_calls": record.llm_calls,
        "tool_calls": record.tool_calls,
        "interactions": record.is_interaction,
        "source_path": record.source_path,
    }
