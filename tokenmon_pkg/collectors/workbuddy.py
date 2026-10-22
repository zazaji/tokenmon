"""Collector for WorkBuddy (Tencent desktop AI assistant).

What exists locally
-------------------
WorkBuddy keeps no per-request token ledger.  `~/.workbuddy/workbuddy.db` has
a `session_usage` table with, per session:

    used        context tokens currently consumed
    size        the model context window
    updated_at  epoch milliseconds

and a `sessions` table with the working directory, model name and timestamps.
The skill usage file `usage-log.json` only stores last-used dates.

So this collector reports what the data actually contains: one record per
session snapshot carrying the context tokens in use, with `llm_calls=0` and
zero output tokens, tagged so the UI can explain that these are context
occupancy figures rather than billed usage.  Inventing input/output splits to
look like the other tools would be wrong.

Note: WorkBuddy is also the model provider behind DeepSeek Harness sessions
(provider == "workbuddy"), so its real billed usage shows up under the
`deepseek-harness` tool.
"""

from __future__ import annotations

from pathlib import Path

from ..model import UsageRecord
from .. import paths as P
from .base import BaseCollector, CollectContext, open_sqlite_readonly, project_from_cwd

SESSION_SQL = """
    SELECT s.id           AS session_id,
           s.cwd          AS cwd,
           s.model        AS model,
           s.updated_at   AS updated_at,
           u.used         AS used,
           u.size         AS size,
           u.updated_at   AS usage_updated_at
      FROM session_usage u
      JOIN sessions s ON s.id = u.session_id
"""


class WorkBuddyCollector(BaseCollector):
    tool = "workbuddy"
    description = "WorkBuddy session context usage"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.workbuddy_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        seen = set()
        for root in self._roots(ctx):
            if root in seen:
                continue
            seen.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for db in self._databases(root):
                if ctx.report is not None:
                    ctx.report.scanned_files += 1
                for record in self._from_database(db, ctx):
                    yield record

    def _databases(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        candidate = root / "workbuddy.db"
        if candidate.is_file():
            yield candidate

    def _from_database(self, db_path, ctx):
        conn = open_sqlite_readonly(db_path)
        if conn is None:
            ctx.report.warn("cannot open " + str(db_path) + " read-only")
            return
        try:
            for row in conn.execute(SESSION_SQL):
                used = int(row["used"] or 0)
                size = int(row["size"] or 0)
                if used <= 0:
                    continue
                session_id = str(row["session_id"] or "")
                ts = row["usage_updated_at"] or row["updated_at"]
                ctx.report.parsed_records += 1
                yield UsageRecord(
                    tool=self.tool,
                    record_key="wb|" + session_id + "|" + str(ts) + "|" + str(used),
                    ts=ts,
                    model=str(row["model"] or "unknown"),
                    provider="workbuddy",
                    session_id=session_id,
                    project=project_from_cwd(str(row["cwd"] or "")),
                    # `used` is context occupancy, not a billed input count.
                    input_tokens=used,
                    output_tokens=0,
                    total_tokens=used,
                    llm_calls=0,
                    is_interaction=0,
                    status="context-snapshot",
                    source_path=str(db_path),
                    metadata={
                        "context_used": used,
                        "context_size": size,
                        "metric": "context_occupancy",
                    },
                )
        except Exception as exc:
            ctx.report.error("query failed on " + str(db_path) + ": " + str(exc))
        finally:
            conn.close()
