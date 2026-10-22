"""Collector for the ZCode CLI.

Data layout
-----------
    <home>/.zcode/cli/db/db.sqlite
    <home>/.zcode/cli/rollout/model-io-*.jsonl

The SQLite database is the authoritative source: the ``model_usage`` table
already stores one row per model request with the token split, so no
arithmetic or deduplication is needed.  ``turn_usage`` provides the tool call
counts, and ``session`` maps a session id to its project directory.

The rollout JSONL files are a fallback for when the database cannot be opened
(locked, corrupted or a newer schema).  They contain full request/response
bodies, so only ``response.usage`` and the surrounding metadata are read.
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
    iter_jsonl,
    iter_text_lines,
    open_sqlite_readonly,
)

MODEL_USAGE_SQL = """
    SELECT session_id, turn_id, model_id, variant, agent, mode, status,
           started_at, completed_at, duration_ms, finish_reason,
           input_tokens, output_tokens, reasoning_tokens,
           cache_creation_input_tokens, cache_read_input_tokens,
           computed_total_tokens, tool_call_count
      FROM model_usage
"""


class ZCodeCollector(BaseCollector):
    tool = "zcode"
    description = "ZCode CLI usage database"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.zcode_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        handled = set()
        for root in self._roots(ctx):
            if root in handled:
                continue
            handled.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            produced = False
            for db in self._databases(root):
                ctx.report.scanned_files += 1
                count = 0
                for record in self._from_database(db, ctx):
                    count += 1
                    yield record
                if count:
                    produced = True
            if not produced:
                for path in self._rollout_files(root):
                    ctx.report.scanned_files += 1
                    for record in self._from_rollout(path, ctx):
                        yield record

    def _databases(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("db.sqlite"):
            if candidate.is_file():
                yield candidate
        # db.sqlite is matched above; this catches a renamed or copied db.

    def _rollout_files(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("model-io-*.jsonl"):
            if candidate.is_file():
                yield candidate

    def _session_projects(self, conn):
        projects = {}
        try:
            rows = conn.execute("SELECT id, directory FROM session")
            for row in rows:
                projects[str(row["id"])] = project_from_cwd(row["directory"])
        except Exception:
            pass
        return projects

    def _session_interactions(self, conn):
        """User prompts per session (role == user rows in the message table)."""
        counts = {}
        try:
            import json

            for row in conn.execute("SELECT session_id, data FROM message"):
                try:
                    data = json.loads(row["data"])
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict) and data.get("role") == "user":
                    key = str(row["session_id"] or "")
                    counts[key] = counts.get(key, 0) + 1
        except Exception:
            pass
        return counts

    def _from_database(self, db_path, ctx):
        conn = open_sqlite_readonly(db_path)
        if conn is None:
            ctx.report.warn("cannot open " + str(db_path) + " read-only")
            return
        try:
            projects = self._session_projects(conn)
            interactions = self._session_interactions(conn)
            rows = conn.execute(MODEL_USAGE_SQL)
            for row in rows:
                numbers = {
                    "input_tokens": row["input_tokens"] or 0,
                    "output_tokens": row["output_tokens"] or 0,
                    "reasoning_tokens": row["reasoning_tokens"] or 0,
                    "cache_read_tokens": row["cache_read_input_tokens"] or 0,
                    "cache_write_tokens": row["cache_creation_input_tokens"] or 0,
                    "total_tokens": row["computed_total_tokens"] or 0,
                }
                if not any(numbers.values()):
                    continue
                session_id = str(row["session_id"] or "")
                model = str(row["model_id"] or "unknown")
                variant = str(row["variant"] or "")
                turn_interactions = interactions.get(session_id, 0)
                started = row["started_at"]
                key = "db|" + session_id + "|" + model + "|" + str(started)
                ctx.report.parsed_records += 1
                yield UsageRecord(
                    tool=self.tool,
                    record_key=key,
                    ts=started,
                    model=model,
                    provider="",
                    session_id=session_id,
                    project=projects.get(session_id, ""),
                    input_tokens=numbers["input_tokens"],
                    output_tokens=numbers["output_tokens"],
                    reasoning_tokens=numbers["reasoning_tokens"],
                    cache_read_tokens=numbers["cache_read_tokens"],
                    cache_write_tokens=numbers["cache_write_tokens"],
                    total_tokens=numbers["total_tokens"],
                    llm_calls=1,
                    tool_calls=row["tool_call_count"] or 0,
                    is_interaction=1 if turn_interactions else 0,
                    status=str(row["status"] or "ok"),
                    source_path=str(db_path),
                    metadata={
                        "variant": variant,
                        "agent": row["agent"],
                        "mode": row["mode"],
                        "finish_reason": row["finish_reason"],
                        "duration_ms": row["duration_ms"],
                        "turn_id": row["turn_id"],
                        "session_interactions": turn_interactions,
                    },
                )
        except Exception as exc:
            ctx.report.warn("model_usage query failed on " + str(db_path) + ": " + str(exc))
        finally:
            conn.close()

    def _from_rollout(self, path, ctx):
        for obj in iter_jsonl(path, ctx.report):
            response = obj.get("response")
            if not isinstance(response, dict):
                continue
            usage = response.get("usage")
            if not isinstance(usage, dict):
                continue
            model = obj.get("model")
            if isinstance(model, dict):
                model = model.get("modelId")
            model = str(model or response.get("modelId") or "unknown")
            started = obj.get("startedAt") or obj.get("completedAt")
            session_id = str(obj.get("sessionId") or "")
            key = "io|" + str(obj.get("requestId") or "") + "|" + str(started)
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=key,
                ts=started,
                model=model,
                session_id=session_id,
                input_tokens=usage.get("inputTokens") or 0,
                output_tokens=usage.get("outputTokens") or 0,
                reasoning_tokens=usage.get("reasoningTokens") or 0,
                cache_read_tokens=usage.get("cacheReadTokens") or 0,
                cache_write_tokens=usage.get("cacheWriteTokens") or 0,
                total_tokens=usage.get("totalTokens") or 0,
                llm_calls=1,
                is_interaction=0,
                source_path=str(path),
                metadata={"querySource": obj.get("querySource"),
                          "finishReason": response.get("finishReason")},
            )
