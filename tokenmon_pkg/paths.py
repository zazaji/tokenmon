"""Cross-platform discovery of AI coding tool data directories.

Every tool stores its usage logs in a different place on every OS.  This module
centralises those lookup rules so the rest of the package never hardcodes a
user name or an OS specific path.

Lookup rules
------------
For each tool we build an ordered list of *candidate directories*.  The first
existing directory wins; if none exists the tool is reported as "not found"
instead of raising.  Every candidate can be overridden with an environment
variable so the tool also works for non default installs, portable builds and
CI sandboxes.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _expand(path: str | os.PathLike[str]) -> Path:
    """Expand \"~\" and environment variables, returning an absolute path."""
    return Path(os.path.abspath(os.path.expanduser(os.path.expandvars(str(path)))))


def home() -> Path:
    """User home directory (honours HOME on POSIX and USERPROFILE on Windows)."""
    return _expand("~")


def _appdata(*parts: str) -> Path:
    base = os.environ.get("APPDATA") or str(home() / "AppData" / "Roaming")
    return _expand(os.path.join(base, *parts))


def _localappdata(*parts: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(home() / "AppData" / "Local")
    return _expand(os.path.join(base, *parts))


def macos_app_support(*parts: str) -> Path:
    return _expand(str(home() / "Library" / "Application Support" / Path(*parts)))


def xdg_config(*parts: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(home() / ".config")
    return _expand(os.path.join(base, *parts))


def xdg_data(*parts: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(home() / ".local" / "share")
    return _expand(os.path.join(base, *parts))


@dataclass
class ToolPaths:
    """Resolved data locations for a single tool.

    \"\"\"roots\"\"\" are directories that were confirmed to exist.  \"\"\"checked\"\"\" keeps
    every candidate that was inspected (existing or not) which is useful for
    diagnostics in the CLI output.
    """

    tool: str
    roots: list[Path] = field(default_factory=list)
    checked: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.roots)


def _first_existing(candidates: Iterable[Path], tool: str, notes: list[str]) -> ToolPaths:
    checked: list[Path] = []
    roots: list[Path] = []
    for cand in candidates:
        cand = _expand(cand)
        if cand in checked:
            continue
        checked.append(cand)
        try:
            if cand.exists():
                roots.append(cand)
        except OSError:
            continue
    return ToolPaths(tool=tool, roots=roots, checked=checked, notes=notes)


def _env_dir(env_name: str) -> Path | None:
    raw = os.environ.get(env_name)
    return _expand(raw) if raw else None


# ---------------------------------------------------------------------------
# Per tool resolution
# ---------------------------------------------------------------------------


def codex_paths() -> ToolPaths:
    """OpenAI Codex CLI.

    Layout (all platforms, default \"~/.codex\")::

        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
        ~/.codex/archived_sessions/rollout-*.jsonl
        ~/.codex/<extra_home>/sessions/...      # alternate accounts / homes

    Alternate homes are any direct sub directory that itself contains a
    \"\"\"sessions\"\"\" directory, which covers \"\"\"account_go\"\"\"-style homes.
    """
    notes: list[str] = []
    override = _env_dir("TOKENMON_CODEX_HOME")
    if override:
        return _first_existing([override], "codex", notes)
    base = home() / ".codex"
    notes.append("default codex home is ~/.codex on all platforms")
    return _first_existing([base], "codex", notes)


def codex_extra_homes() -> list[Path]:
    """Sub directories of the codex home that look like additional session homes."""
    result: list[Path] = []
    try:
        base = codex_paths().roots[0]
    except IndexError:
        return result
    try:
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if (child / "sessions").is_dir():
                result.append(child)
    except OSError:
        return []
    return result


def claudecode_paths() -> ToolPaths:
    """Claude Code.

    Layout (all platforms, default \"~/.claude\")::

        ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
        ~/.claude/history.jsonl
    """
    notes: list[str] = []
    override = _env_dir("TOKENMON_CLAUDE_HOME")
    if override:
        return _first_existing([override], "claudecode", notes)
    base = home() / ".claude"
    notes.append("default claude home is ~/.claude on all platforms")
    return _first_existing([base], "claudecode", notes)


def zcode_paths() -> ToolPaths:
    """ZCode CLI.

    Layout: \"~/.zcode/cli/db/db.sqlite\" (SQLite) plus
    \"~/.zcode/cli/rollout/model-io-*.jsonl\" (raw model IO, used as a fallback
    when the database is unavailable).
    """
    notes: list[str] = []
    override = _env_dir("TOKENMON_ZCODE_HOME")
    if override:
        return _first_existing([override], "zcode", notes)
    base = home() / ".zcode"
    notes.append("default zcode home is ~/.zcode on all platforms")
    return _first_existing([base], "zcode", notes)


def opencode_paths() -> ToolPaths:
    """opencode.

    Data dir is \"~/.local/share/opencode\" on Linux and macOS (this is where
    \"\"\"opencode.db\"\"\" lives) and \"\"%APPDATA%\\opencode\" on Windows.  The config
    dir \"~/.config/opencode\" is only a secondary source for log files.
    """
    notes: list[str] = []
    override = _env_dir("TOKENMON_OPENCODE_HOME")
    if override:
        return _first_existing([override], "opencode", notes)
    candidates: list[Path]
    if IS_WINDOWS:
        candidates = [_appdata("opencode"), _localappdata("opencode")]
    else:
        candidates = [xdg_data("opencode"), xdg_config("opencode")]
    notes.append("opencode.db lives in the XDG data dir (or %APPDATA% on Windows)")
    return _first_existing(candidates, "opencode", notes)


def deepseek_harness_paths() -> ToolPaths:
    """DeepSeek Harness (this tool).

    Layout: \"~/.dsh/sessions/<encoded-cwd>/session-<id>/session.v4.jsonl.zstd\".
    Plain \"\"\".jsonl\"\"\" files are also accepted for older/uncompressed sessions.
    """
    notes: list[str] = []
    override = _env_dir("TOKENMON_DSH_HOME")
    if override:
        return _first_existing([override], "deepseek-harness", notes)
    base = home() / ".dsh"
    notes.append("default harness home is ~/.dsh on all platforms")
    return _first_existing([base], "deepseek-harness", notes)


def qwencode_paths() -> ToolPaths:
    """QwenCode CLI (Alibaba Qwen coding assistant).

    The CLI keeps its own accounting under ~/.qwen:

        ~/.qwen/usage/token-usage-YYYY-MM.jsonl   per-request tokens
        ~/.qwen/usage_record.jsonl                per-session rollup

    Earlier layouts (a .qwencode dot dir, or an app-support folder) are still
    probed so upgrades and forks keep working.
    """
    notes = ["reads ~/.qwen/usage/token-usage-*.jsonl plus usage_record.jsonl"]
    override = _env_dir("TOKENMON_QWENCODE_HOME")
    if override:
        return _first_existing([override], "qwencode", notes)
    if IS_WINDOWS:
        candidates = [
            home() / ".qwen",
            _appdata("QwenCode"),
            _localappdata("QwenCode"),
            home() / ".qwencode",
        ]
    elif IS_MACOS:
        candidates = [
            home() / ".qwen",
            macos_app_support("QwenCode"),
            home() / ".qwencode",
        ]
    else:
        candidates = [
            home() / ".qwen",
            xdg_config("qwen-code"),
            xdg_data("qwen-code"),
            home() / ".qwencode",
        ]
    return _first_existing(candidates, "qwencode", notes)

def workbuddy_paths() -> ToolPaths:
    """WorkBuddy (Tencent desktop AI assistant).

    Layout (macOS): `~/.workbuddy` holding `workbuddy.db`, whose
    `session_usage` table records context-window usage per session.
    Windows/Linux fall back to the app-support / XDG locations.
    """
    notes = ["reads ~/.workbuddy/workbuddy.db (session context usage)"]
    override = _env_dir("TOKENMON_WORKBUDDY_HOME")
    if override:
        return _first_existing([override], "workbuddy", notes)
    if IS_WINDOWS:
        candidates = [_appdata("WorkBuddy"), home() / ".workbuddy"]
    elif IS_MACOS:
        candidates = [home() / ".workbuddy", macos_app_support("WorkBuddy")]
    else:
        candidates = [home() / ".workbuddy", xdg_config("WorkBuddy"),
                      xdg_data("WorkBuddy")]
    return _first_existing(candidates, "workbuddy", notes)


RESOLVERS = {
    "codex": codex_paths,
    "claudecode": claudecode_paths,
    "zcode": zcode_paths,
    "opencode": opencode_paths,
    "deepseek-harness": deepseek_harness_paths,
    "qwencode": qwencode_paths,
    "workbuddy": workbuddy_paths,
}

#: Canonical tool order used by the CLI, the web UI and the collectors.
TOOL_ORDER = [
    "codex",
    "claudecode",
    "zcode",
    "opencode",
    "deepseek-harness",
    "qwencode",
    "workbuddy",
]


def resolve(tool: str) -> ToolPaths:
    """Resolve the data locations for """ + '"""' + """tool""" + '"""' + """."""
    try:
        resolver = RESOLVERS[tool]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError("unknown tool: " + tool) from exc
    return resolver()


def resolve_all(tools: Iterable[str] | None = None) -> dict[str, ToolPaths]:
    """Resolve several tools at once (all known tools by default)."""
    names = list(tools) if tools else TOOL_ORDER
    return {name: resolve(name) for name in names}


def default_db_path() -> Path:
    """Platform default location of the tokenmon SQLite database."""
    override = os.environ.get("TOKENMON_DB")
    if override:
        return _expand(override)
    if IS_WINDOWS:
        return _appdata("tokenmon", "tokenmon.db")
    if IS_MACOS:
        return macos_app_support("tokenmon", "tokenmon.db")
    return xdg_data("tokenmon", "tokenmon.db")


def ensure_parent(path: Path) -> Path:
    """Create the parent directory of """ + '"""' + """path""" + '"""' + """ when needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path
