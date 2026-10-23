"""Collector for WorkBuddy (Tencent desktop AI assistant).

Two different facts live under ``~/.workbuddy`` and they must not be mixed.

Per-request usage
    ``projects/<encoded-cwd>/<session-id>.jsonl`` holds the provider turns.
    A turn carrying ``providerData.usage`` reports the tokens that were really
    billed for one request::

        {"requests": 1, "inputTokens": 1000, "outputTokens": 100,
         "totalTokens": 1100,
         "inputTokensDetails": [{"cached_tokens": 400}],
         "outputTokensDetails": [{"reasoning_tokens": 12}]}

    These become ordinary usage records with ``llm_calls=1``.

Context occupancy
    ``workbuddy.db`` has a ``session_usage`` table with, per session, how full
    the context window currently is::

        used        context tokens currently consumed
        size        the model context window
        updated_at  epoch milliseconds

    That is not billed usage: it is one number that grows and shrinks as the
    conversation is compacted, and summing it across sessions or days would be
    meaningless.  It is kept as a snapshot with zero token columns and
    ``status="context-snapshot"`` so the UI can show context pressure without
    polluting the totals.

Note: WorkBuddy is also the model provider behind DeepSeek Harness sessions
(provider == "workbuddy"), so the harness side of its usage shows up under the
`deepseek-harness` tool.
"""

from __future__ import annotations

from pathlib import Path

from ..model import UsageRecord
from .. import paths as P
from .base import (
    BaseCollector,
    CollectContext,
    iter_jsonl,
    open_sqlite_readonly,
    project_from_cwd,
)

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


def _cached_tokens(usage):
    """Cached input tokens from ``inputTokensDetails``."""
    details = usage.get("inputTokensDetails")
    if not isinstance(details, list):
        return 0
    total = 0
    for item in details:
        if isinstance(item, dict):
            try:
                total += int(item.get("cached_tokens") or 0)
            except (TypeError, ValueError):
                continue
    return total


def _reasoning_tokens(usage):
    """Reasoning tokens from ``outputTokensDetails``."""
    details = usage.get("outputTokensDetails")
    if not isinstance(details, list):
        return 0
    total = 0
    for item in details:
        if isinstance(item, dict):
            try:
                total += int(item.get("reasoning_tokens") or 0)
            except (TypeError, ValueError):
                continue
    return total


class WorkBuddyCollector(BaseCollector):
    tool = "workbuddy"
    description = "WorkBuddy session usage and context occupancy"

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
            for record in self._from_project_logs(root, ctx):
                yield record
            for db in self._databases(root):
                if ctx.report is not None:
                    ctx.report.scanned_files += 1
                for record in self._from_database(db, ctx):
                    yield record

    # -- per-request usage ------------------------------------------------
    def _from_project_logs(self, root, ctx):
        for path in self._project_logs(root):
            if ctx.report is not None:
                ctx.report.scanned_files += 1
            for record in self._parse_log(path, ctx):
                yield record

    def _project_logs(self, root):
        root = Path(root)
        projects = root / "projects"
        if not projects.is_dir():
            return
        for path in sorted(projects.rglob("*.jsonl")):
            if path.is_file():
                yield path

    def _parse_log(self, path, ctx):
        for obj in iter_jsonl(path, ctx.report):
            provider = obj.get("providerData")
            if not isinstance(provider, dict):
                continue
            usage = provider.get("usage")
            if not isinstance(usage, dict):
                continue
            try:
                input_tokens = int(usage.get("inputTokens") or 0)
                output_tokens = int(usage.get("outputTokens") or 0)
            except (TypeError, ValueError):
                continue
            if input_tokens <= 0 and output_tokens <= 0:
                continue
            session_id = str(obj.get("sessionId") or path.stem)
            request_id = str(provider.get("conversationRequestId") or "")
            step = provider.get("stepSeq")
            ts = obj.get("timestamp")
            model = str(provider.get("model") or "unknown")
            # The identity of one request must not depend on numbers that
            # change when the same step is retried, so no token counts here.
            key = "|".join(["wb", session_id, request_id, str(step), str(ts)])
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=key,
                ts=ts,
                model=model,
                provider="workbuddy",
                session_id=session_id,
                project=project_from_cwd(str(obj.get("cwd") or "")),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=_reasoning_tokens(usage),
                cache_read_tokens=_cached_tokens(usage),
                total_tokens=int(usage.get("totalTokens") or 0),
                llm_calls=1,
                is_interaction=0,
                status="ok",
                source_path=str(path),
                metadata={"step_seq": step, "request_id": request_id},
            )

    # -- context occupancy -------------------------------------------------
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
                # Keyed on the session only: `used` and `updated_at` change
                # every time the session is touched, and including them would
                # keep inserting a new row per snapshot while the old ones
                # stay in the table, so one session would be counted many
                # times over.
                yield UsageRecord(
                    tool=self.tool,
                    record_key="wb|ctx|" + session_id,
                    ts=ts,
                    model=str(row["model"] or "unknown"),
                    provider="workbuddy",
                    session_id=session_id,
                    project=project_from_cwd(str(row["cwd"] or "")),
                    # `used` is context occupancy, not a billed input count,
                    # so it must not reach the token columns.
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
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
