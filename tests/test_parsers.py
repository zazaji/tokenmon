"""Parser tests: one per collector, using synthetic fixtures."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenmon_pkg.collectors.base import CollectContext
from tokenmon_pkg.collectors import REGISTRY
from tokenmon_pkg.store import SourceReport, Store


def collect(tool, source_dirs):
    """Run one collector against explicit source directories."""
    report = SourceReport(tool=tool)
    ctx = CollectContext(roots=[], report=report, source_dirs=source_dirs)
    records = list(REGISTRY[tool].collect(ctx))
    return records, report


def totals(records):
    return {
        "input": sum(r.input_tokens for r in records),
        "output": sum(r.output_tokens for r in records),
        "reasoning": sum(r.reasoning_tokens for r in records),
        "cache_read": sum(r.cache_read_tokens for r in records),
        "cache_write": sum(r.cache_write_tokens for r in records),
        "total": sum(r.total_tokens for r in records),
        "interactions": sum(r.is_interaction for r in records),
        "tool_calls": sum(r.tool_calls for r in records),
    }


# ------------------------------------------------------------------ codex

def test_codex_tokens_and_models(codex_home):
    records, report = collect("codex", [codex_home])
    t = totals(records)
    # 1000+400+50+100+20 + 1200+900+0+50+0 + 500+0+0+60+0
    assert t["input"] == 2700
    assert t["cache_read"] == 1300
    assert t["cache_write"] == 50
    assert t["output"] == 210
    assert t["reasoning"] == 20
    assert t["total"] == 2980
    # The cumulative token_count record must be ignored.
    assert 99999 not in (r.input_tokens for r in records)


def test_codex_model_attribution_and_interactions(codex_home):
    records, _ = collect("codex", [codex_home])
    models = {r.model for r in records}
    assert models == {"gpt-6.1-sol", "gpt-5.6-sol"}
    flagged = [r for r in records if r.is_interaction]
    assert len(flagged) == 2
    assert {r.session_id for r in flagged} == {"sess-a"}
    projects = {r.project for r in records}
    assert projects == {"alpha", "beta"}


def test_codex_scans_extra_account_homes(codex_home):
    extra = codex_home / "account_go" / "sessions" / "2026" / "10" / "06"
    extra.mkdir(parents=True)
    (extra / "rollout-c.jsonl").write_text(
        '{"type":"session_meta","timestamp":"2026-10-06T12:00:00Z",'

        ' "payload":{"session_id":"sess-c","cwd":"/work/gamma"}}\n'

        '{"type":"turn_context","timestamp":"2026-10-06T12:00:01Z",'

        ' "payload":{"turn_id":"t","model":"gpt-6.1-sol"}}\n'

        '{"type":"token_usage_record","timestamp":"2026-10-06T12:00:02Z",'

        ' "payload":{"turn_id":"t","session_id":"sess-c",'

        ' "usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}\n',
        encoding="utf-8")
    from tokenmon_pkg import paths as P

    import os

    os.environ["TOKENMON_CODEX_HOME"] = str(codex_home)
    try:
        records, _ = collect("codex", [])
        assert any(r.session_id == "sess-c" for r in records)
    finally:
        del os.environ["TOKENMON_CODEX_HOME"]


# -------------------------------------------------------------- claudecode

def test_claude_tokens_and_cache_split(claude_home):
    records, report = collect("claudecode", [claude_home])
    t = totals(records)
    assert t["input"] == 2000
    assert t["output"] == 300
    assert t["reasoning"] == 40
    assert t["cache_read"] == 1500
    assert t["cache_write"] == 100
    assert t["total"] == 3940


def test_claude_dedupes_and_skips_synthetic(claude_home):
    records, _ = collect("claudecode", [claude_home])
    assert len(records) == 1
    assert records[0].model == "claude-sonnet-4"
    assert records[0].project == "alpha"
    assert 999 not in (r.input_tokens for r in records)


def test_claude_interaction_excludes_tool_results(claude_home):
    records, _ = collect("claudecode", [claude_home])
    # One real prompt (the tool_result line does not count).
    assert sum(r.is_interaction for r in records) == 1


# ------------------------------------------------------------------- zcode

def test_zcode_reads_database(zcode_home):
    records, report = collect("zcode", [zcode_home])
    t = totals(records)
    assert t["input"] == 4000
    assert t["output"] == 500
    assert t["reasoning"] == 60
    assert t["cache_read"] == 2000
    assert t["cache_write"] == 10
    assert t["total"] == 6510
    assert records[0].model == "GLM-5.3-Flash"
    assert records[0].project == "alpha"
    assert records[0].tool_calls == 3


def test_zcode_interactions_from_message_table(zcode_home):
    records, _ = collect("zcode", [zcode_home])
    assert sum(r.is_interaction for r in records) == 1


# ---------------------------------------------------------------- opencode

def test_opencode_tokens_and_cache(opencode_home):
    records, report = collect("opencode", [opencode_home])
    t = totals(records)
    assert t["input"] == 800
    assert t["output"] == 200
    assert t["reasoning"] == 50
    assert t["cache_read"] == 1900
    assert t["cache_write"] == 50
    assert t["total"] == 3000
    assert records[0].model == "muse-1"
    assert records[0].project == "beta"


def test_opencode_interactions(opencode_home):
    records, _ = collect("opencode", [opencode_home])
    assert sum(r.is_interaction for r in records) == 1


# -------------------------------------------------------- deepseek harness

def test_dsh_reads_plain_jsonl(dsh_home):
    records, report = collect("deepseek-harness", [dsh_home])
    t = totals(records)
    assert t["input"] == 10200
    assert t["output"] == 600
    assert t["cache_read"] == 6100
    assert t["total"] == 10800
    assert {r.model for r in records} == {"step-5-preview"}
    assert {r.provider for r in records} == {"workbuddy"}
    assert {r.project for r in records} == {"gamma"}
    assert sum(r.tool_calls for r in records) == 2


def test_dsh_counts_interactions(dsh_home):
    records, _ = collect("deepseek-harness", [dsh_home])
    assert sum(r.is_interaction for r in records) == 2


def test_dsh_reads_zstd(dsh_home):
    pytest = __import__("pytest")
    zstd = pytest.importorskip("zstandard")
    plain = dsh_home / "sessions" / "--work-gamma--" / "session-abc" / "session.v4.jsonl"
    compressed = plain.with_suffix(".jsonl.zstd")
    compressed.write_bytes(zstd.ZstdCompressor().compress(plain.read_bytes()))
    plain.unlink()
    records, _ = collect("deepseek-harness", [dsh_home])
    assert len(records) == 2
    assert sum(r.total_tokens for r in records) == 10800


# ------------------------------------------------------- generic collectors

def test_generic_jsonl_and_vscdb(generic_home):
    records, report = collect("cursor", [generic_home])
    t = totals(records)
    # jsonl row 1 + jsonl row 2 + vscdb row
    assert t["input"] == 700 + 300 + 900
    assert t["output"] == 90 + 40 + 120
    assert t["cache_read"] == 250 + 400
    assert len(records) == 3


def test_generic_ignores_context_window(generic_home):
    records, _ = collect("cursor", [generic_home])
    assert all(r.input_tokens != 100000 for r in records)
    assert len(records) == 3


def test_generic_empty_directory_reports_warning(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    records, report = collect("antigravity", [empty])
    assert records == []
    assert report.warnings >= 1


def test_generic_missing_directory_is_silent(tmp_path):
    records, report = collect("qwencode", [tmp_path / "absent"])
    assert records == []
    assert report.errors == 0


# ------------------------------------------------------------- every tool

def test_registry_has_all_tools():
    assert set(REGISTRY) == {
        "codex", "claudecode", "zcode", "opencode", "deepseek-harness",
        "antigravity", "qwencode", "cursor"}


def test_collect_all_tools_without_error(all_homes):
    for tool, home in all_homes.items():
        name = "cursor" if tool == "generic" else tool
        records, report = collect(name, [home])
        assert report.errors == 0
        assert records
