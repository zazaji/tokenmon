"""Collector for opencode.

Data layout
-----------
    <data>/opencode/opencode.db     (Linux and macOS: ~/.local/share/opencode)
    <config>/opencode/logs/         (secondary source for plain log files)

opencode keeps its history in SQLite.  The ``message`` table stores a JSON
document per row in the ``data`` column.  Only assistant messages carry usage:

    data.tokens.input
    data.tokens.output
    data.tokens.reasoning
    data.tokens.cache.read          -> cache hits
    data.tokens.cache.write         -> cache writes
    data.tokens.total
    data.modelID / data.providerID
    data.time.created               -> epoch milliseconds
    data.path.cwd                   -> project

Interactions are the user messages of the same session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .. import paths as P
from ..model import UsageRecord
from .base import (
    project_from_cwd,
    BaseCollector,
    CollectContext,
    open_sqlite_readonly,
)

MESSAGE_SQL = "SELECT id, session_id, time_created, data FROM message ORDER BY time_created ASC"


class OpenCodeCollector(BaseCollector):
    tool = "opencode"
    description = "opencode session database"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.opencode_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        handled = set()
        for root in self._roots(ctx):
            if root in handled:
                continue
            handled.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for db in self._databases(root):
                ctx.report.scanned_files += 1
                for record in self._from_database(db, ctx):
                    yield record

    def _databases(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("opencode.db"):
            if candidate.is_file():
                yield candidate

    def _from_database(self, db_path, ctx):
        conn = open_sqlite_readonly(db_path)
        if conn is None:
            ctx.report.warn("cannot open " + str(db_path) + " read-only")
            return
        try:
            import json

            user_counts = {}
            try:
                for row in conn.execute(MESSAGE_SQL):
                    data = _loads(row["data"])
                    if not isinstance(data, dict):
                        continue
                    if data.get("role") != "user":
                        continue
                    session_id = str(row["session_id"] or "")
                    user_counts[session_id] = user_counts.get(session_id, 0) + 1
            except Exception as exc:
                ctx.report.warn("message scan failed: " + str(exc))

            for row in conn.execute(MESSAGE_SQL):
                data = _loads(row["data"])
                if not isinstance(data, dict):
                    continue
                if data.get("role") != "assistant":
                    continue
                tokens = data.get("tokens")
                if not isinstance(tokens, dict):
                    continue
                cache = tokens.get("cache")
                if not isinstance(cache, dict):
                    cache = {}
                session_id = str(row["session_id"] or "")
                model = str(data.get("modelID") or "unknown")
                provider = str(data.get("providerID") or "")
                path_info = data.get("path")
                cwd = path_info.get("cwd", "") if isinstance(path_info, dict) else ""
                time_info = data.get("time")
                created = None
                if isinstance(time_info, dict):
                    created = time_info.get("created")
                if created is None:
                    created = row["time_created"]
                numbers = (tokens.get("input") or 0, tokens.get("output") or 0,
                           tokens.get("reasoning") or 0, cache.get("read") or 0,
                           cache.get("write") or 0, tokens.get("total") or 0)
                if not any(numbers):
                    continue
                interactions = user_counts.get(session_id, 0)
                key = "|".join(["opencode", session_id, str(row["id"]), str(created)])
                ctx.report.parsed_records += 1
                yield UsageRecord(
                    tool=self.tool,
                    record_key=key,
                    ts=created,
                    model=model,
                    provider=provider,
                    session_id=session_id,
                    project=project_from_cwd(cwd),
                    input_tokens=numbers[0],
                    output_tokens=numbers[1],
                    reasoning_tokens=numbers[2],
                    cache_read_tokens=numbers[3],
                    cache_write_tokens=numbers[4],
                    total_tokens=numbers[5],
                    llm_calls=1,
                    is_interaction=1 if interactions else 0,
                    status=str(data.get("finish") or "ok"),
                    source_path=str(db_path),
                    metadata={
                        "message_id": row["id"],
                        "agent": data.get("agent"),
                        "mode": data.get("mode"),
                        "variant": data.get("variant"),
                        "session_interactions": interactions,
                    },
                )
        except Exception as exc:
            ctx.report.warn("query failed on " + str(db_path) + ": " + str(exc))
        finally:
            conn.close()



def _loads(raw):
    import json

    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
