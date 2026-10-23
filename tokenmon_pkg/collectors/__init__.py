"""Collector registry.

REGISTRY maps a tool key to a collector instance; TOOL_ORDER gives the display
order used by the CLI and the web UI.  Keys are stable identifiers that must
not change once data has been collected, because they are stored in the
database.
"""

from __future__ import annotations

from typing import Iterable

from .. import paths as P
from .balance import BALANCE_COLLECTORS
from .base import BaseCollector
from .claudecode import ClaudeCodeCollector
from .codex import CodexCollector
from .deepseek_harness import DeepSeekHarnessCollector
from .opencode import OpenCodeCollector
from .pi import PiCollector
from .qwencode import QwenCodeCollector
from .workbuddy import WorkBuddyCollector
from .zcode import ZCodeCollector

REGISTRY = {
    "codex": CodexCollector(),
    "claudecode": ClaudeCodeCollector(),
    "zcode": ZCodeCollector(),
    "opencode": OpenCodeCollector(),
    "deepseek-harness": DeepSeekHarnessCollector(),
    "qwencode": QwenCodeCollector(),
    "workbuddy": WorkBuddyCollector(),
    "pi": PiCollector(),
}

TOOL_ORDER = list(P.TOOL_ORDER)

DISPLAY_NAMES = {
    "codex": "Codex",
    "claudecode": "Claude Code",
    "zcode": "ZCode",
    "opencode": "opencode",
    "deepseek-harness": "DeepSeek Harness",
    "qwencode": "QwenCode",
    "workbuddy": "WorkBuddy",
    "pi": "Pi",
}


#: Short labels used where space is tight (filter chips, table cells).
SHORT_NAMES = {
    "codex": "Codex",
    "claudecode": "CC",
    "zcode": "ZCode",
    "opencode": "OC",
    "deepseek-harness": "DSH",
    "qwencode": "Qwen",
    "workbuddy": "WB",
    "pi": "Pi",
}


def get(tool: str) -> BaseCollector:
    """Return the collector for ``tool`` or raise KeyError."""
    return REGISTRY[tool]


def select(tools: Iterable[str] | None = None) -> list:
    """Collectors for the requested tools, in canonical order."""
    names = [t for t in (tools or TOOL_ORDER) if t in REGISTRY]
    return [REGISTRY[name] for name in TOOL_ORDER if name in names]


def balance_collectors(tools: Iterable[str] | None = None) -> list:
    """Collectors that report balance / quota / cost snapshots."""
    names = set(tools) if tools else None
    return [c for c in BALANCE_COLLECTORS
            if names is None or c.tool in names]


def known(tools: Iterable[str]) -> list:
    """Filter an iterable of tool names down to the ones we support."""
    return [t for t in tools if t in REGISTRY]
