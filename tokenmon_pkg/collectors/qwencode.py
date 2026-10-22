"""Collector for QwenCode (Alibaba Qwen coding assistant).

Data layout
-----------
    ~/.qwen/usage/token-usage-YYYY-MM.jsonl   per-request token accounting
    ~/.qwen/usage_record.jsonl                per-session rollup

The per-request file is the primary source.  One line per model call:

    inputTokens / outputTokens / cachedTokens / thoughtsTokens
    totalTokens, model, sessionId, timestamp, source (main vs background
    extractor), apiDurationMs

The session rollup adds the project directory, tool call counts and skill
counts, which the per-request rows do not carry, so both files are read
and joined on sessionId.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths as P
from ..model import UsageRecord
from .base import BaseCollector, CollectContext, iter_jsonl, project_from_cwd

class QwenCodeCollector(BaseCollector):
    tool = "qwencode"
    description = "QwenCode token usage logs"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.qwencode_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        seen = set()
        for root in self._roots(ctx):
            if root in seen:
                continue
            seen.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            sessions = self._session_meta(root)
            for path in self._usage_files(root):
                ctx.report.scanned_files += 1
                for record in self._parse(path, sessions, ctx):
                    yield record
        # Remote ~/.qwen over ssh, when configured.
        for record in self.collect_remote(ctx):
            yield record

    def _usage_files(self, root):
        """Per-request usage logs, oldest month first."""
        root = Path(root)
        if root.is_file():
            yield root
            return
        usage_dir = root / "usage"
        base = usage_dir if usage_dir.is_dir() else root
        for candidate in sorted(base.rglob("token-usage-*.jsonl")):
            if candidate.is_file():
                yield candidate

    def _session_meta(self, root):
        """Session rollup keyed by sessionId (project, tools, skills)."""
        meta = {}
        root = Path(root)
        candidates = [root / "usage_record.jsonl"]
        if root.is_file():
            candidates = [root]
        for path in candidates:
            if not path.is_file():
                continue
            for obj in iter_jsonl(path):
                session_id = str(obj.get("sessionId") or "")
                if not session_id:
                    continue
                tools = obj.get("tools")
                skills = obj.get("skills")
                meta[session_id] = {
                    "project": project_from_cwd(str(obj.get("project") or "")),
                    "tool_calls": int(tools.get("totalCalls") or 0) if isinstance(tools, dict) else 0,
                    "skill_calls": int(skills.get("totalCalls") or 0) if isinstance(skills, dict) else 0,
                    "duration_ms": int(obj.get("durationMs") or 0),
                }
        return meta

    def _parse(self, path, sessions, ctx):
        for obj in iter_jsonl(path, ctx.report):
            total = int(obj.get("totalTokens") or 0)
            input_tokens = int(obj.get("inputTokens") or 0)
            output_tokens = int(obj.get("outputTokens") or 0)
            cached = int(obj.get("cachedTokens") or 0)
            thoughts = int(obj.get("thoughtsTokens") or 0)
            if not total and not (input_tokens or output_tokens):
                continue
            session_id = str(obj.get("sessionId") or "")
            meta = sessions.get(session_id, {})
            model = str(obj.get("model") or "unknown")
            record_id = str(obj.get("id") or "")
            ts = obj.get("timestamp")
            # The id is a stable UUID per request, so re-running the
            # collector cannot double count.
            key = record_id or "|".join([
                session_id, str(ts), model,
                str(input_tokens), str(output_tokens), str(cached)])
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=key,
                ts=ts,
                model=model,
                provider=str(obj.get("authType") or ""),
                session_id=session_id,
                project=meta.get("project", ""),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=thoughts,
                cache_read_tokens=cached,
                cache_write_tokens=0,
                total_tokens=total,
                llm_calls=1,
                tool_calls=meta.get("tool_calls", 0),
                is_interaction=0,
                source_path=str(path),
                metadata={
                    "source": obj.get("source"),
                    "api_duration_ms": obj.get("apiDurationMs"),
                    "local_date": obj.get("localDate"),
                    "skill_calls": meta.get("skill_calls", 0),
                },
            )

    def _remote_target(self):
        """Optional user@host whose ~/.qwen should also be read.

        QwenCode often runs on a remote development box while the dashboard
        lives on the laptop, so an ssh target can be configured with
        TOKENMON_QWEN_SSH.  Only the two accounting files are read, never a
        whole directory listing, and nothing is written back.
        """
        import os

        return (os.environ.get("TOKENMON_QWEN_SSH") or "").strip()

    def collect_remote(self, ctx):
        """Yield records from a remote ~/.qwen over ssh (optional)."""
        target = self._remote_target()
        if not target:
            return
        import json
        import shutil
        import subprocess

        if not shutil.which("ssh"):
            ctx.report.warn("TOKENMON_QWEN_SSH set but no ssh client available")
            return
        remote = (
            "for f in ~/.qwen/usage/token-usage-*.jsonl; do "
            "[ -f \"$f\" ] && echo \"#FILE $f\" && cat \"$f\"; done; "
            "if [ -f ~/.qwen/usage_record.jsonl ]; then "
            "echo \"#FILE ~/.qwen/usage_record.jsonl\" && cat ~/.qwen/usage_record.jsonl; fi"
        )
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 target, remote],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            ctx.report.warn("ssh to " + target + " failed: " + str(exc))
            return
        if proc.returncode != 0:
            ctx.report.warn("ssh to " + target + " returned " + str(proc.returncode))
            return
        sessions = {}
        for line in proc.stdout.splitlines():
            if line.startswith("#FILE "):
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            session_id = str(obj.get("sessionId") or "")
            if "models" in obj and session_id:
                tools = obj.get("tools")
                sessions[session_id] = {
                    "project": project_from_cwd(str(obj.get("project") or "")),
                    "tool_calls": int(tools.get("totalCalls") or 0)
                    if isinstance(tools, dict) else 0,
                    "skill_calls": 0,
                }
                continue
            total = int(obj.get("totalTokens") or 0)
            if not total and not (obj.get("inputTokens") or obj.get("outputTokens")):
                continue
            meta = sessions.get(session_id, {})
            model = str(obj.get("model") or "unknown")
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=str(obj.get("id") or (session_id + str(obj.get("timestamp")))),
                ts=obj.get("timestamp"),
                model=model,
                provider=str(obj.get("authType") or ""),
                session_id=session_id,
                project=meta.get("project", ""),
                input_tokens=int(obj.get("inputTokens") or 0),
                output_tokens=int(obj.get("outputTokens") or 0),
                reasoning_tokens=int(obj.get("thoughtsTokens") or 0),
                cache_read_tokens=int(obj.get("cachedTokens") or 0),
                total_tokens=total,
                llm_calls=1,
                tool_calls=meta.get("tool_calls", 0),
                is_interaction=0,
                source_path="ssh://" + target + "/.qwen",
                metadata={"source": obj.get("source"), "remote": target},
            )
