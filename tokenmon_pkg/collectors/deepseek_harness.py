"""Collector for DeepSeek Harness itself.

Data layout
-----------
    <home>/.dsh/sessions/<encoded-cwd>/session-<id>/session.v4.jsonl.zstd

Sessions are append only JSONL, optionally zstd compressed.  The token
accounting lives in two record types:

assistant/message
    data.stream is a list of chunks; a chunk whose chunk.type is "usage"
    carries chunk.usage with inputTokens / outputTokens / totalTokens and,
    on cache aware providers, cacheReadTokens.  data.message.source carries
    the provider and model that produced the message.  This is the primary
    source because it is written once per completed assistant step.
assistant/attempt
    Same chunk shape but written for every retry, so the same step can appear
    several times.  Used as a fallback only when a session has no
    assistant/message records; deduplicated by (turn, step, total).

Other record types used for dimensions:

request/context and model/selection
    provider and model of the current request.
user/message
    One human prompt -> one interaction.
tool/call
    One tool invocation -> tool_calls.

Chunks without any token numbers (failed or cancelled attempts) are skipped so
a broken request does not inflate the totals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import paths as P
from ..model import UsageRecord
from .base import (
    BaseCollector,
    CollectContext,
    iter_text_lines,
    project_from_cwd,
    read_zstd,
)


def _usage_from_stream(stream):
    """Return the usage dict of the last usage chunk in a stream."""
    if not isinstance(stream, list):
        return None
    usage = None
    for chunk in stream:
        if not isinstance(chunk, dict):
            continue
        inner = chunk.get("chunk")
        if not isinstance(inner, dict) or inner.get("type") != "usage":
            continue
        candidate = inner.get("usage")
        if isinstance(candidate, dict):
            usage = candidate
    return usage


def _numbers(usage):
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": usage.get("inputTokens") or 0,
        "output_tokens": usage.get("outputTokens") or 0,
        "reasoning_tokens": usage.get("reasoningTokens") or 0,
        "cache_read_tokens": usage.get("cacheReadTokens") or 0,
        "cache_write_tokens": usage.get("cacheWriteTokens") or 0,
        "total_tokens": usage.get("totalTokens") or 0,
    }


class DeepSeekHarnessCollector(BaseCollector):
    tool = "deepseek-harness"
    description = "DeepSeek Harness session logs"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.deepseek_harness_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        handled = set()
        unsupported_reported = False
        for root in self._roots(ctx):
            if root in handled:
                continue
            handled.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in self._session_files(root):
                ctx.report.scanned_files += 1
                if path.suffix == ".zstd" and read_zstd(path) is None:
                    if not unsupported_reported:
                        ctx.report.warn(
                            "zstd support unavailable: install the zstandard module or the zstd CLI")
                        unsupported_reported = True
                    continue
                produced = 0
                for record in self._parse_file(path, ctx):
                    produced += 1
                    yield record
                if produced == 0:
                    for record in self._parse_attempts(path, ctx):
                        yield record

    def _session_files(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("session*.jsonl.zstd"):
            if candidate.is_file():
                yield candidate
        for candidate in root.rglob("session*.jsonl"):
            if candidate.is_file():
                yield candidate

    def _load(self, path):
        """Parse a session file into a list of records."""
        records = []
        for line in iter_text_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(obj, dict):
                records.append(obj)
        return records

    def _context(self, records):
        """Session level context discovered by a single pass.

        ``tool_calls_by_step`` counts the tool calls of each (turn, step) so a
        usage record can report the calls of its own step.  Repeating a
        session wide total on every record would multiply the real number of
        tool calls by the number of model responses.
        """
        session_id = ""
        cwd = ""
        provider = ""
        model = "unknown"
        interactions = 0
        tool_calls = 0
        tool_calls_by_step: dict = {}
        for obj in records:
            rtype = obj.get("type")
            data = obj.get("data")
            if not isinstance(data, dict):
                data = {}
            if rtype == "session":
                session_id = str(obj.get("id") or "")
                cwd = str(obj.get("cwd") or "")
            elif rtype in ("request/context", "model/selection"):
                provider = str(data.get("provider") or provider)
                model = str(data.get("model") or model)
            elif rtype == "user/message":
                interactions += 1
            elif rtype == "tool/call":
                tool_calls += 1
                slot = (data.get("turn"), data.get("step"))
                tool_calls_by_step[slot] = tool_calls_by_step.get(slot, 0) + 1
        return (session_id, cwd, provider, model, interactions, tool_calls,
                tool_calls_by_step)

    def _parse_file(self, path, ctx):
        records = self._load(path)
        if not records:
            return
        (session_id, cwd, provider, model, interactions, tool_calls,
         tool_calls_by_step) = self._context(records)
        for obj in records:
            if obj.get("type") != "assistant/message":
                continue
            data = obj.get("data")
            if not isinstance(data, dict):
                continue
            numbers = _numbers(_usage_from_stream(data.get("stream")))
            if not numbers or not any(numbers.values()):
                continue
            message = data.get("message")
            source = message.get("source") if isinstance(message, dict) else None
            if isinstance(source, dict):
                provider = str(source.get("provider") or provider)
                model = str(source.get("model") or model)
            turn = data.get("turn")
            step = data.get("step")
            ts = obj.get("time")
            key = "|".join(["msg", session_id, str(turn), str(step), str(ts),
                            str(numbers.get("total_tokens"))])
            ctx.report.parsed_records += 1
            yield self._record(path, key, ts, model, provider, session_id, cwd,
                               numbers, interactions,
                               tool_calls_by_step.get((turn, step), 0),
                               turn, step, interactions, tool_calls)

    def _parse_attempts(self, path, ctx):
        records = self._load(path)
        (session_id, cwd, provider, model, interactions, tool_calls,
         tool_calls_by_step) = self._context(records)
        seen = set()
        for obj in records:
            if obj.get("type") != "assistant/attempt":
                continue
            data = obj.get("data")
            if not isinstance(data, dict):
                continue
            numbers = _numbers(_usage_from_stream(data.get("stream")))
            if not numbers or not any(numbers.values()):
                continue
            turn = data.get("turn")
            step = data.get("step")
            ts = obj.get("time")
            slot = (turn, step, numbers.get("total_tokens"))
            if slot in seen:
                continue
            seen.add(slot)
            key = "|".join(["attempt", session_id, str(turn), str(step), str(ts),
                            str(numbers.get("total_tokens"))])
            ctx.report.parsed_records += 1
            yield self._record(path, key, ts, model, provider, session_id, cwd,
                               numbers, interactions,
                               tool_calls_by_step.get((turn, step), 0),
                               turn, step, interactions, tool_calls)

    def _record(self, path, key, ts, model, provider, session_id, cwd, numbers,
                interactions, step_tool_calls, turn, step,
                session_interactions, session_tool_calls):
        return UsageRecord(
            tool=self.tool,
            record_key=key,
            ts=ts,
            model=model,
            provider=provider,
            session_id=session_id,
            project=project_from_cwd(cwd),
            input_tokens=numbers.get("input_tokens", 0),
            output_tokens=numbers.get("output_tokens", 0),
            reasoning_tokens=numbers.get("reasoning_tokens", 0),
            cache_read_tokens=numbers.get("cache_read_tokens", 0),
            cache_write_tokens=numbers.get("cache_write_tokens", 0),
            total_tokens=numbers.get("total_tokens", 0),
            llm_calls=1,
            tool_calls=step_tool_calls,
            is_interaction=1 if interactions else 0,
            source_path=str(path),
            metadata={"turn": turn, "step": step,
                      "session_interactions": session_interactions,
                      "session_tool_calls": session_tool_calls},
        )
