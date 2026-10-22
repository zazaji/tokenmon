"""Programmatic entry points used by the web layer.

Kept separate from tokenmon.py so the Flask app can trigger a collection
without shelling out to a subprocess.
"""

from __future__ import annotations

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


def collect_once(db_path: str, tools: Iterable[str] | None = None) -> dict:
    """Run a full collection and return a JSON-serialisable summary."""
    names = known(tools) if tools else list(TOOL_ORDER)
    store = Store(db_path)
    run_id = store.start_run(names)
    report = RunReport(run_id=run_id, tools=names, db_path=str(db_path))

    for collector in select(names):
        source = SourceReport(tool=collector.tool)
        report.sources[collector.tool] = source
        try:
            ctx = CollectContext(roots=[], report=source, source_dirs=[])
            batch = []
            for record in collector.collect(ctx):
                batch.append(record)
                if len(batch) >= 2000:
                    inserted, skipped = store.insert_many(batch)
                    source.inserted += inserted
                    source.skipped += skipped
                    batch = []
            if batch:
                inserted, skipped = store.insert_many(batch)
                source.inserted += inserted
                source.skipped += skipped
        except Exception as exc:
            source.status = "error"
            source.error(str(exc))
        store.save_source(run_id, source)

    for collector in balance_collectors(names):
        source = SourceReport(tool=collector.tool + "/balance")
        report.sources[source.tool] = source
        try:
            ctx = CollectContext(roots=[], report=source, source_dirs=[])
            batch = []
            for snapshot in collector.collect(ctx):
                batch.append(snapshot)
                if len(batch) >= 2000:
                    inserted, skipped = store.insert_accounts(batch)
                    source.inserted += inserted
                    source.skipped += skipped
                    batch = []
            if batch:
                inserted, skipped = store.insert_accounts(batch)
                source.inserted += inserted
                source.skipped += skipped
        except Exception as exc:
            source.status = "error"
            source.error(str(exc))
        store.save_source(run_id, source)

    store.close()
    summary = report.summary()
    summary["names"] = {k: DISPLAY_NAMES.get(k, k) for k in names}
    return summary
