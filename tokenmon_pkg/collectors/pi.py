"""Collector for the Pi coding agent.

Data layout
-----------
    ~/.pi/agent/sessions/<encoded-cwd>/<iso-timestamp>_<session-id>.jsonl

Each line is one event.  The only token bearing ones are the assistant
messages::

    {"type": "message", "id": "338da9fd", "timestamp": "2026-09-21T11:59:05.774Z",
     "message": {"role": "assistant", "content": [{"type": "toolCall", ...}],
                 "api": "anthropic-messages", "provider": "apigate",
                 "model": "c_glm",
                 "usage": {"input": 900, "output": 0, "cacheRead": 0,
                           "cacheWrite": 0, "totalTokens": 900}}}

Other roles (user, system, toolResult) carry no usage and are only used to
count interactions and tool calls.  The session record carries the cwd, which
is the project name; ``model_change`` records give the provider / model when an
assistant message omits them.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths as P
from ..model import UsageRecord
from .base import BaseCollector, CollectContext, project_from_cwd


class PiCollector(BaseCollector):
    tool = "pi"
    description = "Pi coding agent session logs"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.pi_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        handled = set()
        for root in self._roots(ctx):
            if root in handled:
                continue
            handled.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in self._session_files(root):
                ctx.report.scanned_files += 1
                for record in self._from_file(path, ctx):
                    yield record

    def _session_files(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in sorted(root.rglob("*.jsonl")):
            if candidate.is_file():
                yield candidate

    def _from_file(self, path, ctx):
        from .base import iter_jsonl

        cwd = ""
        provider = ""
        model = "unknown"
        users = 0
        for obj in iter_jsonl(path, ctx.report):
            rtype = obj.get("type")
            if rtype == "session":
                cwd = str(obj.get("cwd") or cwd)
            elif rtype == "model_change":
                provider = str(obj.get("provider") or provider)
                model = str(obj.get("modelId") or model)
            elif rtype == "message":
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                if message.get("role") == "user":
                    users += 1
                    continue
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                input_tokens = _as_int(usage.get("input"))
                output_tokens = _as_int(usage.get("output"))
                cache_read = _as_int(usage.get("cacheRead"))
                cache_write = _as_int(usage.get("cacheWrite"))
                total_tokens = _as_int(usage.get("totalTokens"))
                if not any((input_tokens, output_tokens, cache_read,
                            cache_write, total_tokens)):
                    continue
                session_id = str(obj.get("sessionId") or _session_id(path))
                provider = str(message.get("provider") or provider)
                model = str(message.get("model") or model)
                tool_calls = 0
                content = message.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "toolCall":
                            tool_calls += 1
                ts = message.get("timestamp") or obj.get("timestamp")
                key = "|".join(["pi", session_id, str(obj.get("id") or ""),
                                str(ts)])
                ctx.report.parsed_records += 1
                yield UsageRecord(
                    tool=self.tool,
                    record_key=key,
                    ts=ts,
                    model=model,
                    provider=provider,
                    session_id=session_id,
                    project=project_from_cwd(cwd),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    total_tokens=total_tokens,
                    llm_calls=1,
                    tool_calls=tool_calls,
                    is_interaction=1 if users else 0,
                    status=str(message.get("stopReason") or "ok"),
                    source_path=str(path),
                    metadata={
                        "api": message.get("api"),
                        "response_model": message.get("responseModel"),
                        "session_interactions": users,
                    },
                )


def _session_id(path: Path) -> str:
    """Session id from ``<iso-timestamp>_<session-id>.jsonl``."""
    stem = Path(path).stem
    if "_" in stem:
        return stem.split("_", 1)[1]
    return stem


def _as_int(value) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
