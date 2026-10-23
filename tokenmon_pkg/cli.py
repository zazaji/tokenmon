"""Programmatic entry points used by the web layer.

Kept separate from tokenmon.py so the Flask app can trigger a collection
without shelling out to a subprocess; tokenmon.py's ``collect`` command is a
thin printer around :func:`collect_once` so the two paths cannot drift apart.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Iterable

from .collectors import (
    DISPLAY_NAMES,
    TOOL_ORDER,
    balance_collectors,
    known,
    select,
)
from .collectors.base import CollectContext
from .store import RunReport, SourceReport, Store


def collect_once(db_path: str, tools: Iterable[str] | None = None,
                 source_dirs: Iterable[str] | None = None) -> dict:
    """Run one full collection and return a JSON serialisable summary.

    Usage records as well as balance / quota / cost snapshots are collected for
    the same set of tools.  A failing collector becomes one erroring source
    instead of aborting the run, and the run row is always closed so the UI
    never shows a collection that looks stuck.
    """
    names = known(tools) if tools else list(TOOL_ORDER)
    dirs = [str(d) for d in (source_dirs or [])]
    started_ms = int(time.time() * 1000)
    store = Store(db_path)
    run_id = store.start_run(names)
    report = RunReport(run_id=run_id, started_at=datetime.now(timezone.utc),
                       tools=names, db_path=str(db_path))

    for collector in select(names):
        source = SourceReport(tool=collector.tool)
        report.sources[collector.tool] = source
        _drain(store, collector, source, dirs)
        store.save_source(run_id, source)

    for collector in balance_collectors(names):
        source = SourceReport(tool=collector.tool)
        report.sources[collector.tool + "/balance"] = source
        _drain(store, collector, source, dirs)
        store.save_source(run_id, source)

    report.inserted = sum(s.inserted for s in report.sources.values())
    report.skipped = sum(s.skipped for s in report.sources.values())
    report.errors = sum(s.errors for s in report.sources.values())
    report.warnings = sum(s.warnings for s in report.sources.values())
    store.finish_run(run_id, report.inserted, report.skipped, report.errors,
                     report.warnings, int(time.time() * 1000) - started_ms)
    store.close()

    summary = report.summary()
    summary["names"] = {k: DISPLAY_NAMES.get(k, k) for k in names}
    return summary


def _drain(store, collector, source, source_dirs):
    """Collect one source, inserting in batches and reporting failures."""
    try:
        ctx = CollectContext(roots=[], report=source, source_dirs=source_dirs)
        batch = []
        for item in collector.collect(ctx):
            batch.append(item)
            if len(batch) >= 2000:
                inserted, skipped = _insert(store, batch)
                source.inserted += inserted
                source.skipped += skipped
                batch = []
        if batch:
            inserted, skipped = _insert(store, batch)
            source.inserted += inserted
            source.skipped += skipped
    except Exception as exc:
        source.status = "error"
        source.error(str(exc))
    if source.errors:
        source.status = "error"


def _insert(store, batch):
    """Insert a batch into the table matching the item type.

    Usage records carry token columns; account snapshots carry ``kind`` and go
    to the balance table.  Batches never mix the two, so the first item
    decides.
    """
    if not batch:
        return 0, 0
    insert = store.insert_accounts if hasattr(batch[0], "kind") else store.insert_many
    return insert(batch)
