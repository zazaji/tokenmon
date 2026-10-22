"""Shared helpers for every collector.

Collectors are deliberately simple: they walk a set of candidate directories,
yield UsageRecord objects and report problems through the SourceReport
instead of raising.  A single malformed file must never abort a whole
collection run.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from ..model import UsageRecord


@dataclass
class CollectContext:
    """Runtime context handed to every collector."""

    roots: list = field(default_factory=list)
    report: object = None
    source_dirs: list = field(default_factory=list)

    def note(self, text: str) -> None:
        if self.report is not None:
            self.report.details.append(text)


class BaseCollector:
    """Base class implementing the collector protocol."""

    tool = ""
    description = ""

    def collect(self, ctx):
        raise NotImplementedError

    def iter_files(self, root, patterns=("*.jsonl", "*.json", "*.ndjson"),
                   max_depth=8,
                   skip_dirs=("node_modules", "Caches", "Cache", "GPUCache", "Code Cache")):
        """Yield files under root matching patterns.

        The walk is depth limited and skips cache heavy directories so that a
        tool with a large install directory does not turn the scan into a full
        disk traversal.
        """
        root = Path(root)
        if not root.exists():
            return
        if root.is_file():
            yield root
            return
        base_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            depth = len(here.parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if d not in skip_dirs and not d.startswith(".")]
            for name in filenames:
                if any(fnmatch.fnmatch(name, pat) for pat in patterns):
                    yield here / name

    def iter_sqlite_files(self, root, max_depth=4):
        """Yield SQLite databases under root."""
        yield from self.iter_files(
            root, patterns=("*.db", "*.sqlite", "*.sqlite3", "*.vscdb"),
            max_depth=max_depth)


def iter_jsonl(path, report=None, max_bad_lines=50):
    """Stream a JSONL file, skipping and reporting malformed lines."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    if report is not None and max_bad_lines > 0:
                        report.error(path.name + ":" + str(lineno)
                                     + " invalid JSON (" + str(exc) + ")")
                        max_bad_lines -= 1
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        if report is not None:
            report.error("cannot read " + path.name + ": " + str(exc))


def load_json(path, report=None):
    """Load a JSON document, returning None on failure."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        if report is not None:
            report.error("cannot read " + str(path) + ": " + str(exc))
        return None


def open_sqlite_readonly(path):
    """Open a SQLite database read-only without creating it.

    Uses the immutable=1 hint so a database that is currently being written by
    another process can still be read without locking issues.
    """
    path = Path(path)
    if not path.exists():
        return None
    uri = "file:" + str(path).replace("?", "%3f").replace("#", "%23") + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def zstd_available():
    try:
        import zstandard  # noqa: F401

        return True
    except ImportError:
        return False


def read_zstd(path):
    """Decompress a .zstd file to bytes, or return None when unsupported.

    Tries the zstandard module first and falls back to the zstd CLI so the
    collector still works when only the system binary is installed.
    """
    path = Path(path)
    if zstd_available():
        import zstandard

        try:
            with path.open("rb") as handle:
                return zstandard.ZstdDecompressor().stream_reader(handle).read()
        except Exception:
            return None
    binary = shutil.which("zstd") or shutil.which("unzstd")
    if binary:
        try:
            proc = subprocess.run([binary, "-dc", str(path)], capture_output=True,
                                  timeout=120, check=False)
            if proc.returncode == 0:
                return proc.stdout
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def iter_text_lines(path):
    """Yield decoded lines from a plain or zstd compressed JSONL file."""
    path = Path(path)
    if path.suffix == ".zstd":
        data = read_zstd(path)
        if data is None:
            return
        for line in data.decode("utf-8", "replace").splitlines():
            yield line
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line
    except OSError:
        return


def safe_int(value, default=0):
    try:
        if value is None or isinstance(value, bool):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def first_number(mapping, keys, default=0):
    """Return the first present, non negative numeric value among keys."""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            value = safe_int(mapping[key], -1)
            if value >= 0:
                return value
    return default


def first_text(mapping, keys, default=""):
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return default


def project_from_cwd(cwd):
    """Human friendly project name from a working directory path."""
    if not cwd:
        return ""
    parts = [p for p in str(cwd).replace("\\", "/").split("/") if p]
    return parts[-1] if parts else ""
