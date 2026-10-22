"""Collector for Claude Code.

Data layout
-----------
    <home>/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

Each JSONL line is one message.  Only assistant lines carry usage:

    message.usage.input_tokens
    message.usage.output_tokens
    message.usage.cache_creation_input_tokens   -> cache writes
    message.usage.cache_read_input_tokens        -> cache hits
    message.usage.output_tokens_details.thinking_tokens -> reasoning
    message.model

Deduplication
-------------
The same assistant message can appear more than once in a session file (Claude
rewrites the tail when a request is retried or resumed).  Records are keyed on
the assistant message id when present, otherwise on the line uuid plus the
usage numbers, so a duplicate never counts twice.

Interaction counting
--------------------
A user line counts as one interaction when its content is a plain string or a
list whose first block is text.  Tool result lines (list of tool_result blocks)
are not interactions - they are the model consuming its own tool output.
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
    first_number,
    first_text,
    iter_jsonl,
)

USAGE_KEYS = {
    "input": ("input_tokens", "inputTokens", "prompt_tokens"),
    "output": ("output_tokens", "outputTokens", "completion_tokens"),
    "cache_read": ("cache_read_input_tokens", "cacheReadInputTokens",
                   "cache_read_tokens", "cached_tokens"),
    "cache_write": ("cache_creation_input_tokens", "cache_creation_tokens",
                    "cacheWriteInputTokens", "cache_write_tokens"),
}


def _usage_numbers(usage):
    if not isinstance(usage, dict):
        return {}
    thinking = usage.get("output_tokens_details")
    reasoning = first_number(thinking, ("thinking_tokens", "reasoning_tokens")) if isinstance(thinking, dict) else 0
    numbers = {
        "input_tokens": first_number(usage, USAGE_KEYS["input"]),
        "output_tokens": first_number(usage, USAGE_KEYS["output"]),
        "reasoning_tokens": reasoning,
        "cache_read_tokens": first_number(usage, USAGE_KEYS["cache_read"]),
        "cache_write_tokens": first_number(usage, USAGE_KEYS["cache_write"]),
        "total_tokens": first_number(usage, ("total_tokens", "totalTokens")),
    }
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        # Newer Claude builds split cache creation into two buckets.
        split = (first_number(cache_creation, ("ephemeral_1h_input_tokens",))
                 + first_number(cache_creation, ("ephemeral_5m_input_tokens",)))
        if split > numbers["cache_write_tokens"]:
            numbers["cache_write_tokens"] = split
    return numbers


def _is_interaction(obj):
    """True when a user line is a real prompt rather than a tool result."""
    if obj.get("type") != "user":
        return False
    message = obj.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            return first.get("type") in ("text", "input_text")
        return isinstance(first, str)
    return False


class ClaudeCodeCollector(BaseCollector):
    tool = "claudecode"
    description = "Claude Code session logs"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.claudecode_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        seen = set()
        for root in self._roots(ctx):
            if root in seen:
                continue
            seen.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in self._session_files(root):
                ctx.report.scanned_files += 1
                for record in self._parse_file(path, ctx):
                    yield record

    def _session_files(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("*.jsonl"):
            if candidate.is_file() and "projects" in candidate.parts:
                yield candidate

    def _parse_file(self, path, ctx):
        """Emit assistant usage records for one session file.

        Interactions are counted per session in a first pass so every record
        can carry the session interaction total without needing a join.
        """
        seen_keys = set()
        interactions = _count_interactions(path)
        for obj in iter_jsonl(path, ctx.report):
            if obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            numbers = _usage_numbers(message.get("usage"))
            if not numbers or not any(numbers.values()):
                continue
            model = str(message.get("model") or "")
            if model in ("<synthetic>", "synthetic"):
                continue
            session_id = str(obj.get("sessionId") or obj.get("session_id") or "")
            msg_id = str(message.get("id") or obj.get("uuid") or "")
            ts = obj.get("timestamp")
            key = "|".join([session_id, msg_id, ts or "",
                            str(numbers.get("input_tokens")),
                            str(numbers.get("output_tokens")),
                            str(numbers.get("cache_read_tokens"))])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=key,
                ts=ts,
                model=model or "unknown",
                provider="anthropic",
                session_id=session_id,
                project=self._project_from_path(path),
                input_tokens=numbers.get("input_tokens", 0),
                output_tokens=numbers.get("output_tokens", 0),
                reasoning_tokens=numbers.get("reasoning_tokens", 0),
                cache_read_tokens=numbers.get("cache_read_tokens", 0),
                cache_write_tokens=numbers.get("cache_write_tokens", 0),
                total_tokens=numbers.get("total_tokens", 0),
                llm_calls=1,
                is_interaction=1 if interactions else 0,
                source_path=str(path),
                metadata={"message_id": msg_id, "stop_reason": message.get("stop_reason"),
                          "session_interactions": interactions},
            )

    @staticmethod
    def _project_from_path(path):
        """projects/-Users-ou-project-foo -> project-foo."""
        parts = Path(path).parts
        for index, part in enumerate(parts):
            if part == "projects" and index + 1 < len(parts):
                encoded = parts[index + 1]
                tail = encoded.rsplit("-", 1)[-1]
                return tail
        return ""


def _count_interactions(path):
    """Number of real user prompts in a Claude session file."""
    total = 0
    for obj in iter_jsonl(Path(path)):
        if _is_interaction(obj):
            total += 1
    return total
