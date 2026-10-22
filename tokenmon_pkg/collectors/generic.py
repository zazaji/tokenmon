"""Best-effort collector for tools without a documented usage schema.

Antigravity, QwenCode and Cursor are all derived from (or adjacent to) VS Code
and keep their agent state in editor databases and JSONL session files.  None
of them publishes a token accounting schema, so this collector scans the
candidate directories for anything that looks like a usage record and extracts
the numbers with a very permissive field matcher.

What is scanned
--------------
* ``*.jsonl`` / ``*.json`` / ``*.ndjson`` files under the tool data dir
  (session logs, request logs, chat exports).
* SQLite databases (``*.vscdb``, ``*.db``, ``*.sqlite``): the ``ItemTable``
  and ``workspaceStorage`` tables plus any table with a ``data``/``value``
  text column that parses as JSON.

What counts as a usage record
-----------------------------
A JSON object is accepted when it contains at least one input-side token field
(inputTokens, input_tokens, promptTokens, prompt_tokens) and at least one
output-side field (outputTokens, output_tokens, completionTokens,
completion_tokens).  Cache fields are optional and read from the common
aliases (cachedTokens, cacheReadTokens, cache_read_tokens, cached_input_tokens,
cacheWriteTokens, cache_creation_input_tokens).

Because these layouts are undocumented, the collector is deliberately
conservative: it prefers to report zero records (with a warning naming the
directories it checked) over inventing numbers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..model import UsageRecord
from .base import (
    project_from_cwd,
    BaseCollector,
    CollectContext,
    first_number,
    first_text,
    iter_jsonl,
    open_sqlite_readonly,
)

INPUT_KEYS = ("inputTokens", "input_tokens", "promptTokens", "prompt_tokens",
              "prompt_token_count", "inputTokenCount")
OUTPUT_KEYS = ("outputTokens", "output_tokens", "completionTokens",
               "completion_tokens", "completion_token_count", "outputTokenCount")
REASONING_KEYS = ("reasoningTokens", "reasoning_tokens", "thinkingTokens",
                  "thinking_tokens")
CACHE_READ_KEYS = ("cachedTokens", "cached_tokens", "cacheReadTokens",
                   "cache_read_tokens", "cache_read_input_tokens",
                   "cached_input_tokens", "cacheHitTokens")
CACHE_WRITE_KEYS = ("cacheWriteTokens", "cache_write_tokens",
                    "cache_creation_input_tokens", "cacheCreationTokens")
TOTAL_KEYS = ("totalTokens", "total_tokens", "totalTokenCount")
MODEL_KEYS = ("model", "modelId", "model_id", "modelName", "slug")
TS_KEYS = ("timestamp", "ts", "time", "createdAt", "created_at", "time_created",
           "date", "startedAt", "started_at")
CWD_KEYS = ("cwd", "workspace", "directory", "projectPath", "root", "path")
SESSION_KEYS = ("sessionId", "session_id", "conversationId", "chatId", "threadId")

#: token-ish fields that must never be mistaken for usage
IGNORED_HINTS = ("context_window", "contextWindow", "maxTokens", "max_tokens",
                 "limit", "quota")


def _numbers(obj):
    if not isinstance(obj, dict):
        return None
    has_input = any(k in obj for k in INPUT_KEYS)
    has_output = any(k in obj for k in OUTPUT_KEYS)
    if not (has_input and has_output):
        return None
    return {
        "input_tokens": first_number(obj, INPUT_KEYS),
        "output_tokens": first_number(obj, OUTPUT_KEYS),
        "reasoning_tokens": first_number(obj, REASONING_KEYS),
        "cache_read_tokens": first_number(obj, CACHE_READ_KEYS),
        "cache_write_tokens": first_number(obj, CACHE_WRITE_KEYS),
        "total_tokens": first_number(obj, TOTAL_KEYS),
    }


def _walk_json(obj, path="", depth=0):
    """Yield (json_path, dict) for every nested object, breadth first."""
    if depth > 6 or not isinstance(obj, (dict, list)):
        return
    if isinstance(obj, list):
        for index, item in enumerate(obj):
            for found in _walk_json(item, path + "[" + str(index) + "]", depth + 1):
                yield found
        return
    yield path, obj
    for key, value in obj.items():
        if key in IGNORED_HINTS:
            continue
        for found in _walk_json(value, path + "." + str(key), depth + 1):
            yield found


class GenericCollector(BaseCollector):
    """Scanner used by antigravity, qwencode and cursor."""

    def __init__(self, tool, description, resolver):
        self.tool = tool
        self.description = description
        self._resolver = resolver

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(self._resolver().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        handled = set()
        found_any = False
        for root in self._roots(ctx):
            if root in handled:
                continue
            handled.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in self.iter_files(root, max_depth=6,
                                        skip_dirs=("node_modules", "Caches", "Cache",
                                                   "GPUCache", "Code Cache",
                                                   "extensions", "CachedExtensionVSIXs",
                                                   "Backups", "logs", "DawnGraphiteCache",
                                                   "DawnWebGPUCache", "DIPS")):
                ctx.report.scanned_files += 1
                for record in self._scan_file(path, ctx):
                    found_any = True
                    yield record
            for db in self.iter_sqlite_files(root, max_depth=4):
                ctx.report.scanned_files += 1
                for record in self._scan_sqlite(db, ctx):
                    found_any = True
                    yield record
        if not found_any and ctx.report is not None:
            ctx.report.warn(
                "no usage records found; checked "
                + ", ".join(str(r) for r in handled or ["<none>"]))

    # -- sources ----------------------------------------------------------
    def _scan_file(self, path, ctx):
        import json

        if path.suffix.lower() == ".jsonl" or path.suffix.lower() == ".ndjson":
            for obj in iter_jsonl(path, ctx.report):
                for record in self._records_from(obj, path, ctx):
                    yield record
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        try:
            obj = json.loads(text)
        except (TypeError, ValueError):
            return
        for record in self._records_from(obj, path, ctx):
            yield record

    def _scan_sqlite(self, db_path, ctx):
        import json

        conn = open_sqlite_readonly(db_path)
        if conn is None:
            return
        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                try:
                    columns = [row[1] for row in conn.execute(
                        "PRAGMA table_info(" + table + ")")]
                except Exception:
                    continue
                text_columns = [c for c in columns
                                if c.lower() in ("data", "value", "json", "payload",
                                                 "content", "body")]
                if not text_columns:
                    continue
                select = ",".join(text_columns)
                try:
                    rows = conn.execute(
                        "SELECT " + select + " FROM " + table + " LIMIT 20000")
                except Exception:
                    continue
                for row in rows:
                    for column in text_columns:
                        raw = row[column]
                        if not isinstance(raw, str) or len(raw) < 8:
                            continue
                        try:
                            obj = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        for record in self._records_from(obj, db_path, ctx):
                            yield record
        except Exception as exc:
            ctx.report.warn("cannot scan " + str(db_path) + ": " + str(exc))
        finally:
            conn.close()

    # -- extraction -------------------------------------------------------
    def _records_from(self, obj, path, ctx):
        import json

        for json_path, node in _walk_json(obj):
            numbers = _numbers(node)
            if numbers is None or not any(numbers.values()):
                continue
            model = first_text(node, MODEL_KEYS)
            ts = node.get("timestamp") or node.get("ts") or node.get("time")
            if ts is None:
                ts = first_text(node, TS_KEYS) or None
            cwd = first_text(node, CWD_KEYS)
            session = first_text(node, SESSION_KEYS)
            key = "|".join([self.tool, str(path), json_path, str(ts),
                            str(numbers.get("input_tokens")),
                            str(numbers.get("output_tokens")),
                            str(numbers.get("cache_read_tokens"))])
            ctx.report.parsed_records += 1
            yield UsageRecord(
                tool=self.tool,
                record_key=key,
                ts=ts,
                model=model or "unknown",
                session_id=session,
                project=project_from_cwd(cwd),
                input_tokens=numbers.get("input_tokens", 0),
                output_tokens=numbers.get("output_tokens", 0),
                reasoning_tokens=numbers.get("reasoning_tokens", 0),
                cache_read_tokens=numbers.get("cache_read_tokens", 0),
                cache_write_tokens=numbers.get("cache_write_tokens", 0),
                total_tokens=numbers.get("total_tokens", 0),
                llm_calls=1,
                is_interaction=0,
                source_path=str(path),
                metadata={"json_path": json_path},
            )
