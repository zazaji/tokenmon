#!/usr/bin/env python3
"""tokenmon command line interface.

Usage
-----
    python tokenmon.py collect [--db PATH] [--tools codex,claudecode]
                               [--source-dir DIR ...] [--full]
    python tokenmon.py report  [--db PATH] [--period day|week|month] [--json]
    python tokenmon.py serve   [--db PATH] [--host H] [--port N] [--debug]
    python tokenmon.py tools

The default database location is platform specific and can be overridden with
--db or the TOKENMON_DB environment variable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tokenmon_pkg import paths as P
from tokenmon_pkg.cli import collect_once
from tokenmon_pkg.collectors import DISPLAY_NAMES, TOOL_ORDER, known
from tokenmon_pkg.store import Store

args_verbose = False


def _resolve_db(explicit):
    if explicit:
        return P.ensure_parent(P._expand(explicit))
    return P.ensure_parent(P.default_db_path())


def _utcnow():
    return datetime.now(timezone.utc)


def _now_ms():
    return int(time.time() * 1000)


def cmd_tools(args):
    resolved = P.resolve_all()
    width = max(len(DISPLAY_NAMES.get(t, t)) for t in TOOL_ORDER)
    for tool in TOOL_ORDER:
        info = resolved[tool]
        name = DISPLAY_NAMES.get(tool, tool)
        state = "found   " if info.found else "missing "
        roots = ", ".join(str(r) for r in info.roots) or "-"
        print(name.ljust(width) + "  " + state + "  " + roots)
        for note in info.notes:
            print(" " * (width + 4) + "(" + note + ")")
    return 0


def cmd_collect(args):
    db_path = _resolve_db(args.db)
    tools = known(args.tools.split(",")) if args.tools else list(TOOL_ORDER)
    source_dirs = [P._expand(d) for d in (args.source_dir or [])]
    source_dirs = [d for d in source_dirs if d.exists()]

    print("database: " + str(db_path))
    print("tools:    " + ", ".join(DISPLAY_NAMES.get(t, t) for t in tools))
    if source_dirs:
        print("sources:  " + ", ".join(str(d) for d in source_dirs))
    print("")

    summary = collect_once(str(db_path), tools, source_dirs)
    if args_verbose:
        for name, source in summary["sources"].items():
            for detail in source.get("details", [])[:10]:
                print("    " + detail)
    _print_summary(summary)
    return 0 if summary["errors"] == 0 else 1


def _print_summary(summary):
    print("collection summary (run " + str(summary["run_id"]) + ")")
    print("-" * 78)
    for tool in list(summary["tools"]) + [t + "/balance" for t in summary["tools"]]:
        source = summary["sources"].get(tool)
        if source is None:
            continue
        name = DISPLAY_NAMES.get(tool.split("/")[0], tool) + (
            " 余额/额度" if tool.endswith("/balance") else "")
        line = (name.ljust(18) + " files " + str(source["scanned_files"]).rjust(5)
                + "  records " + str(source["parsed_records"]).rjust(7)
                + "  new " + str(source["inserted"]).rjust(7)
                + "  dup " + str(source["skipped"]).rjust(6))
        if source["errors"] or source["warnings"]:
            line += "  issues " + str(source["errors"]) + "/" + str(source["warnings"])
        print(line)
    print("-" * 78)
    print("inserted " + str(summary["inserted"]) + "  skipped " + str(summary["skipped"])
          + "  errors " + str(summary["errors"])
          + "  warnings " + str(summary["warnings"]))


def cmd_report(args):
    from tokenmon_pkg.analysis import build_report

    db_path = _resolve_db(args.db)
    if not db_path.exists():
        print("no database at " + str(db_path) + " - run collect first")
        return 1
    store = Store(db_path)
    data = build_report(store, period=args.period,
                        tools=known(args.tools.split(",")) if args.tools else None,
                        span=args.span)
    store.close()
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        _print_console_report(data, args.period)
    return 0


def _print_console_report(data, period):
    print("tokenmon report - period " + period)
    print("=" * 78)
    overall = data["totals"]
    print("total tokens      " + str(overall["total_tokens"]))
    print("  input           " + str(overall["input_tokens"]))
    print("  output          " + str(overall["output_tokens"]))
    print("  reasoning       " + str(overall["reasoning_tokens"]))
    print("  cache hit       " + str(overall["cache_read_tokens"]))
    print("  cache write     " + str(overall["cache_write_tokens"]))
    print("  interactions    " + str(overall["interactions"]))
    print("  llm calls       " + str(overall["llm_calls"]))
    print("  tool calls      " + str(overall["tool_calls"]))
    print("")
    print("by tool")
    for row in data["by_tool"]:
        print("  " + row["tool"].ljust(20)
              + " tokens " + str(row["total_tokens"]).rjust(12)
              + "  cache " + str(row["cache_read_tokens"]).rjust(11)
              + "  turns " + str(row["interactions"]).rjust(7))
    print("")
    print("by model")
    for row in data["by_model"][:20]:
        print("  " + row["model"].ljust(28)
              + " tokens " + str(row["total_tokens"]).rjust(12)
              + "  cache " + str(row["cache_read_tokens"]).rjust(11)
              + "  calls " + str(row["llm_calls"]).rjust(7))
    print("")
    print("buckets (" + period + ")")
    for row in data["buckets"]:
        print("  " + str(row["label"]).ljust(14)
              + " tokens " + str(row["total_tokens"]).rjust(12)
              + "  cache " + str(row["cache_read_tokens"]).rjust(11)
              + "  turns " + str(row["interactions"]).rjust(7))


def cmd_serve(args):
    from tokenmon_pkg.web import create_app

    app = create_app(db_path=str(_resolve_db(args.db)))
    print("tokenmon web UI: http://" + args.host + ":" + str(args.port) + "/")
    print("database:       " + str(_resolve_db(args.db)))
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="tokenmon",
                                     description="AI coding tool token usage analytics")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="scan tool data dirs and import usage")
    collect.add_argument("--db", help="SQLite database path (default: platform specific)")
    collect.add_argument("--tools", help="comma separated tool keys (default: all)")
    collect.add_argument("--source-dir", action="append", default=[],
                         help="scan this directory instead of the tool default")
    collect.add_argument("--full", action="store_true",
                         help="re-scan everything (default is already a full scan)")
    collect.add_argument("-v", "--verbose", action="store_true", help="show warnings")
    collect.set_defaults(func=cmd_collect)

    report = sub.add_parser("report", help="print a text report")
    report.add_argument("--db", help="SQLite database path")
    report.add_argument("--period", choices=("day", "week", "month"), default="day")
    report.add_argument("--tools", help="comma separated tool keys")
    report.add_argument("--json", action="store_true", help="machine readable output")
    report.add_argument("--span", type=int, default=1,
                        help="how many periods back to include (default 1)")
    report.set_defaults(func=cmd_report)

    serve = sub.add_parser("serve", help="start the Flask web UI")
    serve.add_argument("--db", help="SQLite database path")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--debug", action="store_true")
    serve.set_defaults(func=cmd_serve)

    tools = sub.add_parser("tools", help="show detected data directories")
    tools.set_defaults(func=cmd_tools)

    return parser


def main(argv=None):
    global args_verbose
    parser = build_parser()
    args = parser.parse_args(argv)
    args_verbose = bool(getattr(args, "verbose", False))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
