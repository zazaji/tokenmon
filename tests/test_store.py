"""Store and analysis tests: idempotency, filtering and period bucketing."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenmon_pkg.analysis import (
    PERIODS,
    _bucket_key,
    build_report,
    resolve_range,
)
from tokenmon_pkg.model import UsageRecord, canonical_model, to_utc
from tokenmon_pkg.store import Store


def make_record(**kwargs):
    base = {
        "tool": "codex",
        "record_key": "",
        "ts": datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc),
        "model": "gpt-6.1-sol",
        "session_id": "s1",
        "project": "alpha",
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 50,
        "total_tokens": 160,
        "is_interaction": 1,
    }
    base.update(kwargs)
    return UsageRecord(**base)


# ------------------------------------------------------------------- store

def test_insert_is_idempotent(tmp_path):
    store = Store(tmp_path / "a.db")
    record = make_record()
    first = store.insert_many([record])
    second = store.insert_many([record])
    assert first == (1, 0)
    assert second == (0, 1)
    assert store.count() == 1
    store.close()


def test_insert_different_keys_are_kept(tmp_path):
    store = Store(tmp_path / "b.db")
    a = make_record(ts=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc))
    b = make_record(ts=datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc))
    inserted, skipped = store.insert_many([a, b])
    assert (inserted, skipped) == (2, 0)
    assert store.count() == 2
    store.close()


def test_empty_batch_is_noop(tmp_path):
    store = Store(tmp_path / "c.db")
    assert store.insert_many([]) == (0, 0)
    store.close()


def test_iter_records_filters(tmp_path):
    store = Store(tmp_path / "d.db")
    store.insert_many([
        make_record(tool="codex", model="gpt-6.1-sol", project="alpha"),
        make_record(tool="zcode", model="glm-5.3-flash", project="beta",
                    ts=datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)),
        make_record(tool="zcode", model="glm-5.3-flash", project="beta",
                    ts=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)),
    ])
    assert len(list(store.iter_records(tools=["codex"]))) == 1
    assert len(list(store.iter_records(tools=["zcode"]))) == 2
    assert len(list(store.iter_records(models=["GLM-5.3-Flash"]))) == 2
    assert len(list(store.iter_records(projects=["alpha"]))) == 1
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert len(list(store.iter_records(since=since))) == 2
    store.close()


def test_distinct_and_runs(tmp_path):
    store = Store(tmp_path / "e.db")
    run_id = store.start_run(["codex"])
    store.insert_many([make_record()])
    store.save_source(run_id, _source_report())
    store.finish_run(run_id, 1, 0, 0, 0, 12)
    assert store.distinct("model") == ["gpt-6.1-sol"]
    assert store.distinct("tool") == ["codex"]
    assert store.distinct("project") == ["alpha"]
    runs = store.last_runs()
    assert runs[0]["inserted"] == 1
    assert store.last_run_sources(run_id)[0]["tool"] == "codex"
    store.close()


def _source_report():
    from tokenmon_pkg.store import SourceReport

    report = SourceReport(tool="codex")
    report.scanned_files = 1
    report.parsed_records = 1
    return report


# ------------------------------------------------------------------ model

def test_total_tokens_computed_when_missing():
    record = make_record(total_tokens=0, input_tokens=100, output_tokens=10,
                         cache_read_tokens=50, cache_write_tokens=5)
    assert record.total_tokens == 165


def test_provider_total_wins():
    record = make_record(total_tokens=999, input_tokens=1, output_tokens=1)
    assert record.total_tokens == 999


def test_cache_hit_ratio():
    record = make_record(input_tokens=100, cache_read_tokens=50,
                         cache_write_tokens=50)
    assert record.cache_hit_ratio == 0.25


def test_canonical_model_aliases():
    assert canonical_model("glm-5.3-flash") == "GLM-5.3-Flash"
    assert canonical_model("c_glm") == "GLM-5.3-Flash"
    assert canonical_model("") == "unknown"
    assert canonical_model(None) == "unknown"
    assert canonical_model("some-new-model") == "some-new-model"


def test_to_utc_accepts_many_formats():
    assert to_utc("2026-10-15T12:00:00Z").year == 2026
    assert to_utc("2026-10-15T12:00:00+00:00").hour == 12
    assert to_utc(1780000000).year == 2026
    assert to_utc(1780000000000).year == 2026
    assert to_utc(datetime(2026, 10, 15, 12, 0)).tzinfo is not None
    assert to_utc(None) is None
    assert to_utc("garbage") is None


# ---------------------------------------------------------------- analysis

def test_bucket_keys():
    moment = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert _bucket_key(moment, "day") == "2026-10-15"
    assert _bucket_key(moment, "week") == "2026-W42"
    assert _bucket_key(moment, "month") == "2026-10"


def test_resolve_range_day():
    anchor = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    start, end = resolve_range("day", anchor=anchor, tz=timezone.utc, span=3)
    assert (end - start).days == 3


def test_resolve_range_week():
    anchor = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    start, end = resolve_range("week", anchor=anchor, tz=timezone.utc, span=2)
    assert (end - start).days == 14
    assert start.weekday() == 0


def test_resolve_range_month():
    anchor = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    start, end = resolve_range("month", anchor=anchor, tz=timezone.utc, span=1)
    assert start.day == 1
    assert end.day == 1
    assert end.month == 11


def test_build_report_dimensions(tmp_path):
    store = Store(tmp_path / "f.db")
    store.insert_many([
        make_record(tool="codex", model="gpt-6.1-sol", project="alpha"),
        make_record(tool="codex", model="gpt-5.6-sol", project="beta",
                    input_tokens=200, cache_read_tokens=100, total_tokens=310),
        make_record(tool="zcode", model="glm-5.3-flash", project="alpha",
                    input_tokens=300, cache_read_tokens=0, total_tokens=360),
    ])
    data = build_report(store, period="month", span=3, tz_name="UTC")
    assert data["period"] == "month"
    assert data["totals"]["input_tokens"] == 600
    assert data["totals"]["cache_read_tokens"] == 150
    assert len(data["by_tool"]) == 2
    assert len(data["by_model"]) == 3
    assert len(data["by_project"]) == 2
    assert {r["tool"] for r in data["by_tool"]} == {"codex", "zcode"}
    assert data["by_tool"][0]["tool"] == "codex"
    row = [r for r in data["by_tool_model"] if r["tool"] == "codex"]
    assert len(row) == 2
    assert row[0]["model"]
    store.close()


def test_build_report_counts_distinct_interactions(tmp_path):
    store = Store(tmp_path / "g.db")
    # Three usage records that all belong to the same turn.  They are
    # deduplicated by the record key, so use distinct keys but the same
    # interaction unit (same session and turn).
    for index in range(3):
        store.insert_many([make_record(
            record_key="k" + str(index),
            session_id="s1",
            metadata={"turn_id": "t1"},
            is_interaction=1)])
    data = build_report(store, period="month", span=1, tz_name="UTC")
    assert data["totals"]["records"] == 3
    assert data["totals"]["interactions"] == 1
    store.close()


def test_build_report_series_has_bucket_per_period(tmp_path):
    store = Store(tmp_path / "h.db")
    store.insert_many([make_record()])
    data = build_report(store, period="day", span=7, tz_name="UTC")
    assert len(data["buckets"]) == 7
    assert all(b["total_tokens"] >= 0 for b in data["buckets"])
    store.close()


def test_build_report_invalid_period_falls_back(tmp_path):
    store = Store(tmp_path / "i.db")
    store.insert_many([make_record()])
    data = build_report(store, period="year")
    assert data["period"] == "day"
    store.close()


def test_periods_exposed():
    assert PERIODS == ("day", "week", "month")
