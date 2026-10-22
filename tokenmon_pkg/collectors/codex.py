"""Collector for the OpenAI Codex CLI.

Data layout
-----------
Codex writes one JSONL rollout file per session:

    <codex_home>/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
    <codex_home>/archived_sessions/rollout-<ts>-<uuid>.jsonl
    <codex_home>/<extra_home>/sessions/...     # alternate accounts

Token accounting
----------------
``token_usage_record`` is the authoritative source: one row per model response
with payload.usage holding the tokens of that single response.  The
``event_msg``/``token_count`` records are deliberately ignored - they carry no
turn_id, so they cannot be attributed to a model, and their
``total_token_usage`` field is a running thread total that would multiply
count the same turn.

Two passes per file
-------------------
Codex appends the ``turn_context`` (which names the model) *after* the first
usage records of a turn, so a single streaming pass would label the first
requests of every turn as unknown.  The collector therefore parses the file
once to collect session context (model per turn, interaction and tool counts),
and a second time to emit the usage records with full attribution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .. import paths as P
from ..model import UsageRecord
from .base import (
    BaseCollector,
    CollectContext,
    first_number,
    iter_jsonl,
    project_from_cwd,
)

INPUT_KEYS = ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
OUTPUT_KEYS = ("output_tokens", "outputTokens", "completion_tokens", "completionTokens")
REASONING_KEYS = ("reasoning_tokens", "reasoningTokens", "reasoning_output_tokens")
CACHE_READ_KEYS = ("cached_input_tokens", "cachedInputTokens", "cache_read_input_tokens",
                   "cacheReadTokens", "cache_read_tokens", "cached_tokens")
CACHE_WRITE_KEYS = ("cache_write_input_tokens", "cacheWriteInputTokens",
                    "cache_creation_input_tokens", "cacheWriteTokens")
TOTAL_KEYS = ("total_tokens", "totalTokens")


def _usage_numbers(usage):
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": first_number(usage, INPUT_KEYS),
        "output_tokens": first_number(usage, OUTPUT_KEYS),
        "reasoning_tokens": first_number(usage, REASONING_KEYS),
        "cache_read_tokens": first_number(usage, CACHE_READ_KEYS),
        "cache_write_tokens": first_number(usage, CACHE_WRITE_KEYS),
        "total_tokens": first_number(usage, TOTAL_KEYS),
    }


class _Session:
    """Everything known about one rollout file after the first pass."""

    def __init__(self):
        self.session_id = ""
        self.cwd = ""
        self.provider = ""
        self.turn_model = {}
        self.interactions = {}
        self.tool_calls = {}
        self.turn_first_ts = {}


class CodexCollector(BaseCollector):
    tool = "codex"
    description = "OpenAI Codex CLI rollout logs"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.codex_paths().roots)
            roots.extend(P.codex_extra_homes())
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
            if candidate.is_file() and candidate.name.startswith("rollout-"):
                yield candidate

    def _parse_file(self, path, ctx):
        session = _Session()
        rows = []
        for obj in iter_jsonl(path, ctx.report):
            rtype = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            ts = obj.get("timestamp")

            if rtype == "session_meta":
                session.session_id = str(payload.get("session_id") or payload.get("id") or "")
                session.cwd = str(payload.get("cwd") or "")
                session.provider = str(payload.get("model_provider") or "")
                continue

            if rtype == "turn_context":
                turn_id = str(payload.get("turn_id") or "")
                if turn_id and payload.get("model"):
                    session.turn_model[turn_id] = str(payload.get("model"))
                continue

            if rtype == "event_msg":
                sub = payload.get("type")
                if sub == "task_started":
                    # Every task_started marks one turn that the user kicked
                    # off.  turn_trigger is unreliable (None for most builds),
                    # so presence of the event is the signal.
                    turn_id = str(payload.get("turn_id") or "")
                    if turn_id:
                        session.interactions[turn_id] = 1
                    continue
                if sub == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") in ("CommandExecution", "command_execution", "McpToolCall", "DynamicToolCall", "CollabAgentToolCall"):
                        turn_id = str(payload.get("turn_id") or "")
                        if turn_id:
                            session.tool_calls[turn_id] = session.tool_calls.get(turn_id, 0) + 1
                    continue
                # token_count events carry no turn_id and cannot be attributed.
                continue

            if rtype == "token_usage_record":
                usage = payload.get("usage")
                if not isinstance(usage, dict):
                    continue
                turn_id = str(payload.get("turn_id") or "")
                if turn_id:
                    session.turn_first_ts.setdefault(turn_id, ts)
                rows.append((turn_id, ts, _usage_numbers(usage)))

        for turn_id, ts, numbers in rows:
            if not numbers or not any(numbers.values()):
                continue
            model = session.turn_model.get(turn_id) or "unknown"
            first_ts = session.turn_first_ts.get(turn_id) or ts
            key = "|".join([session.session_id, turn_id, str(ts),
                            str(numbers.get("input_tokens")),
                            str(numbers.get("output_tokens")),
                            str(numbers.get("cache_read_tokens"))])
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=key,
                ts=first_ts,
                model=model,
                provider=session.provider,
                session_id=session.session_id,
                project=project_from_cwd(session.cwd),
                input_tokens=numbers.get("input_tokens", 0),
                output_tokens=numbers.get("output_tokens", 0),
                reasoning_tokens=numbers.get("reasoning_tokens", 0),
                cache_read_tokens=numbers.get("cache_read_tokens", 0),
                cache_write_tokens=numbers.get("cache_write_tokens", 0),
                total_tokens=numbers.get("total_tokens", 0),
                llm_calls=1,
                tool_calls=session.tool_calls.get(turn_id, 0),
                is_interaction=1 if session.interactions.get(turn_id) else 0,
                source_path=str(path),
                metadata={"turn_id": turn_id, "source": "token_usage_record"},
            )
