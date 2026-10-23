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
    # Tool calls belong to the step that made them: 2 in step 1, 1 in step 2.
    assert [r.tool_calls for r in records] == [2, 1]


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


# ---------------------------------------------------------------- workbuddy

def test_workbuddy_reads_per_request_usage(workbuddy_home):
    records, report = collect("workbuddy", [workbuddy_home])
    t = totals(records)
    # 1000+1100+1200 (session 1) + 300 (session 2)
    assert t["input"] == 3600
    assert t["output"] == 250
    assert t["cache_read"] == 1600
    assert t["reasoning"] == 15
    billed = [r for r in records if r.status != "context-snapshot"]
    assert len(billed) == 4
    assert {r.model for r in records} == {"hy3", "deepseek-v4.1-flash"}
    assert {r.project for r in billed} == {"gamma", "delta"}
    assert {r.provider for r in billed} == {"workbuddy"}
    assert report.errors == 0


def test_workbuddy_context_occupancy_is_not_billed_usage(workbuddy_home):
    """Context occupancy must be reported, but never counted as tokens."""
    records, _ = collect("workbuddy", [workbuddy_home])
    billed = [r for r in records if r.status != "context-snapshot"]
    assert sum(r.total_tokens for r in billed) == 3850
    snapshots = [r for r in records if r.status == "context-snapshot"]
    assert len(snapshots) == 2
    # The occupancy figures stay available in metadata for the UI.
    assert {r.metadata["context_used"] for r in snapshots} == {5000, 800}
    # ... but they contribute nothing to the token columns.
    assert all(r.input_tokens == 0 for r in snapshots)
    assert all(r.output_tokens == 0 for r in snapshots)
    assert all(r.total_tokens == 0 for r in snapshots)
    assert all(r.metadata["metric"] == "context_occupancy" for r in snapshots)


def test_workbuddy_usage_does_not_double_count(workbuddy_home):
    """Re-collecting the same home must not create new rows."""
    from tokenmon_pkg.store import Store

    db = workbuddy_home / "tokenmon.db"
    keys = set()
    for _ in range(2):
        store = Store(str(db))
        report = SourceReport(tool="workbuddy")
        ctx = CollectContext(roots=[], report=report,
                             source_dirs=[workbuddy_home])
        records = list(REGISTRY["workbuddy"].collect(ctx))
        store.insert_many(records)
        keys |= {r.record_key for r in records}
        store.close()
    assert len(keys) == 6
    assert sum(1 for k in keys if "ctx" in k) == 2


# --------------------------------------------------------------------- pi

def test_pi_reads_assistant_usage(pi_home):
    records, report = collect("pi", [pi_home])
    t = totals(records)
    assert t["input"] == 900 + 1188
    assert t["output"] == 270
    assert t["total"] == 900 + 1458
    assert len(records) == 2
    # c_glm is canonicalised to GLM-5.3-Flash so one model stays one row.
    assert {r.model for r in records} == {"GLM-5.3-Flash"}
    assert {r.provider for r in records} == {"apigate"}
    assert {r.project for r in records} == {"AINEWS"}
    assert {r.session_id for r in records} == {
        "01a0c3d5-68fc-72e0-b577-8a316362ba68"}
    assert report.errors == 0


def test_pi_counts_tool_calls_and_interactions(pi_home):
    records, _ = collect("pi", [pi_home])
    assert [r.tool_calls for r in records] == [1, 0]
    assert sum(r.is_interaction for r in records) == 2


def test_pi_ignores_non_assistant_messages(pi_home):
    """user / system / toolResult rows carry no usage."""
    records, _ = collect("pi", [pi_home])
    assert len(records) == 2


# ------------------------------------------------------- generic collectors

def test_generic_jsonl_and_vscdb(qwen_home):
    records, report = collect("qwencode", [qwen_home])
    t = totals(records)
    # jsonl row 1 + jsonl row 2 + vscdb row
    assert t["input"] == 3000
    assert t["output"] == 300
    assert t["cache_read"] == 1500
    assert t["total"] == 4820
    assert len(records) == 2


def test_generic_ignores_context_window(qwen_home):
    records, _ = collect("qwencode", [qwen_home])
    assert all(r.input_tokens != 100000 for r in records)
    assert len(records) == 2


def test_generic_empty_directory_reports_warning(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    records, report = collect("qwencode", [empty])
    assert records == []
    assert report.errors == 0


def test_generic_missing_directory_is_silent(tmp_path):
    records, report = collect("qwencode", [tmp_path / "absent"])
    assert records == []
    assert report.errors == 0


# ------------------------------------------------------------- every tool

def test_registry_has_all_tools():
    assert set(REGISTRY) == {
        "codex", "claudecode", "zcode", "opencode", "deepseek-harness",
        "qwencode", "workbuddy", "pi"}


def test_collect_all_tools_without_error(all_homes):
    for tool, home in all_homes.items():
        name = "cursor" if tool == "generic" else tool
        records, report = collect(name, [home])
        assert report.errors == 0
        assert records
