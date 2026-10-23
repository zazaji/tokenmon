"""The shared collect entry point used by the web UI.

``POST /api/collect`` runs :func:`tokenmon_pkg.cli.collect_once`, which is why
a defect there only ever shows up in the browser and never in the CLI.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenmon_pkg import cli  # noqa: E402
from tokenmon_pkg.store import Store  # noqa: E402


HOMES = {
    "deepseek-harness": "dsh_home",
    "workbuddy": "workbuddy_home",
}


def run(tmp_path, tool, home):
    """Collect one tool against its synthetic home only."""
    db = str(tmp_path / ("db-" + tool + ".sqlite"))
    return db, cli.collect_once(db, [tool], source_dirs=[home])


def test_collect_once_returns_a_summary(tmp_path, dsh_home, workbuddy_home):
    _, summary = run(tmp_path, "deepseek-harness", dsh_home)
    assert summary["inserted"] >= 1
    assert summary["errors"] == 0
    assert summary["sources"]["deepseek-harness"]["parsed_records"] == 2
    _, summary = run(tmp_path, "workbuddy", workbuddy_home)
    assert summary["sources"]["workbuddy"]["parsed_records"] == 6


def test_collect_once_closes_the_run(tmp_path, dsh_home):
    """The run row must be finished, otherwise /api/runs shows open runs."""
    db, _ = run(tmp_path, "deepseek-harness", dsh_home)
    store = Store(db)
    runs = store.last_runs(1)
    store.close()
    assert runs
    assert runs[0]["finished_at"] is not None
    assert runs[0]["duration_ms"] is not None


def test_collect_once_is_idempotent(tmp_path, workbuddy_home):
    db, first = run(tmp_path, "workbuddy", workbuddy_home)
    second = cli.collect_once(db, ["workbuddy"], source_dirs=[workbuddy_home])
    assert second["inserted"] == 0
    assert second["skipped"] == 6
    assert second["errors"] == 0
    assert first["inserted"] == 6


@pytest.mark.parametrize("tool,fixture", sorted(HOMES.items()))
def test_collect_once_never_reports_context_occupancy_as_tokens(
        tmp_path, request, tool, fixture):
    """A WorkBuddy context snapshot must not inflate the token totals."""
    db, summary = run(tmp_path, tool, request.getfixturevalue(fixture))
    assert summary["errors"] == 0
    store = Store(db)
    rows = list(store.iter_records(tools=[tool]))
    store.close()
    snapshots = [r for r in rows if r.status == "context-snapshot"]
    for record in snapshots:
        assert record.input_tokens == 0
        assert record.output_tokens == 0
        assert record.total_tokens == 0
        assert record.llm_calls == 0
        assert record.metadata["metric"] == "context_occupancy"


def test_collect_once_survives_a_broken_source(tmp_path, dsh_home):
    """One failing collector must not abort the run."""
    db = str(tmp_path / "db.sqlite")
    summary = cli.collect_once(db, ["deepseek-harness"],
                               source_dirs=[dsh_home, tmp_path / "absent"])
    assert summary["errors"] == 0
    assert summary["inserted"] >= 2
