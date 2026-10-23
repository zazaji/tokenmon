"""Shared fixtures: synthetic data directories for every collector."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def make_codex_home(root):
    """A codex home with two sessions across two accounts."""
    root.mkdir(parents=True, exist_ok=True)
    session_a = {
        "timestamp": "2026-10-05T09:00:00.000Z",
        "type": "session_meta",
        "payload": {"session_id": "sess-a", "cwd": "/work/alpha",
                    "model_provider": "openai"},
    }
    turn_ctx = {
        "timestamp": "2026-10-05T09:00:01.000Z",
        "type": "turn_context",
        "payload": {"turn_id": "turn-a1", "model": "gpt-6.1-sol"},
    }
    started = {
        "timestamp": "2026-10-05T09:00:02.000Z",
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "turn-a1"},
    }
    usage = {
        "timestamp": "2026-10-05T09:00:03.000Z",
        "type": "token_usage_record",
        "payload": {"turn_id": "turn-a1", "session_id": "sess-a",
                    "usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                              "cache_write_input_tokens": 50,
                              "output_tokens": 100, "reasoning_output_tokens": 20,
                              "total_tokens": 1170}},
    }
    # A second response in the same turn.
    usage2 = {
        "timestamp": "2026-10-05T09:00:04.000Z",
        "type": "token_usage_record",
        "payload": {"turn_id": "turn-a1", "session_id": "sess-a",
                    "usage": {"input_tokens": 1200, "cached_input_tokens": 900,
                              "output_tokens": 50, "total_tokens": 1250}},
    }
    # Cumulative token_count must be ignored.
    cumulative = {
        "timestamp": "2026-10-05T09:00:05.000Z",
        "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 99999},
                             "last_token_usage": {"input_tokens": 99999}}},
    }
    write_jsonl(root / "sessions" / "2026" / "10" / "05" / "rollout-a.jsonl",
                [session_a, turn_ctx, started, usage, usage2, cumulative])

    session_b = {
        "timestamp": "2026-10-06T10:00:00.000Z",
        "type": "session_meta",
        "payload": {"session_id": "sess-b", "cwd": "/work/beta"},
    }
    turn_ctx_b = {
        "timestamp": "2026-10-06T10:00:01.000Z",
        "type": "turn_context",
        "payload": {"turn_id": "turn-b1", "model": "gpt-5.6-sol"},
    }
    usage_b = {
        "timestamp": "2026-10-06T10:00:02.000Z",
        "type": "token_usage_record",
        "payload": {"turn_id": "turn-b1", "session_id": "sess-b",
                    "usage": {"input_tokens": 500, "output_tokens": 60,
                              "total_tokens": 560}},
    }
    write_jsonl(root / "archived_sessions" / "rollout-b.jsonl",
                [session_b, turn_ctx_b, usage_b])
    return root


def make_claude_home(root):
    """A claude home with one session containing real and synthetic rows."""
    root.mkdir(parents=True, exist_ok=True)
    user = {"type": "user", "timestamp": "2026-10-05T08:00:00.000Z",
            "sessionId": "claude-s1",
            "message": {"role": "user", "content": "hello"}}
    tool_result = {"type": "user", "timestamp": "2026-10-05T08:00:01.000Z",
                   "sessionId": "claude-s1",
                   "message": {"role": "user",
                               "content": [{"type": "tool_result",
                                            "tool_use_id": "t1"}]}}
    assistant = {"type": "assistant", "timestamp": "2026-10-05T08:00:02.000Z",
                 "sessionId": "claude-s1", "uuid": "u1",
                 "message": {"id": "m1", "model": "claude-sonnet-4",
                             "usage": {"input_tokens": 2000,
                                       "output_tokens": 300,
                                       "cache_creation_input_tokens": 100,
                                       "cache_read_input_tokens": 1500,
                                       "output_tokens_details": {"thinking_tokens": 40}}}}
    # Duplicate of the same assistant message (claude rewrites on retry).
    duplicate = dict(assistant)
    synthetic = {"type": "assistant", "timestamp": "2026-10-05T08:00:03.000Z",
                 "sessionId": "claude-s1", "uuid": "u2",
                 "message": {"id": "m2", "model": "<synthetic>",
                             "usage": {"input_tokens": 999, "output_tokens": 9}}}
    write_jsonl(root / "projects" / "-work-alpha" / "claude-s1.jsonl",
                [user, tool_result, assistant, duplicate, synthetic])
    return root


def make_zcode_home(root):
    """A zcode home with the usage database."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
                              data TEXT);
        CREATE TABLE model_usage (
            session_id TEXT, turn_id TEXT, model_id TEXT, variant TEXT,
            agent TEXT, mode TEXT, status TEXT, started_at INTEGER,
            completed_at INTEGER, duration_ms INTEGER, finish_reason TEXT,
            input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, cache_creation_input_tokens INTEGER,
            cache_read_input_tokens INTEGER, computed_total_tokens INTEGER,
            tool_call_count INTEGER);
    """)
    conn.execute("INSERT INTO session VALUES (?, ?)", ("sess_z1", "/work/alpha"))
    conn.execute("INSERT INTO message VALUES (?, ?, ?)",
                 ("m1", "sess_z1", json.dumps({"role": "user"})))
    conn.execute("INSERT INTO message VALUES (?, ?, ?)",
                 ("m2", "sess_z1", json.dumps({"role": "user"})))
    conn.execute("INSERT INTO message VALUES (?, ?, ?)",
                 ("m3", "sess_z1", json.dumps({"role": "assistant"})))
    conn.execute(
        "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sess_z1", "turn_z1", "glm-5.3-flash", "max", "zcode-agent", "build",
         "completed", 1780000000000, 1780000001000, 1000, "stop",
         4000, 500, 60, 10, 2000, 6510, 3))
    conn.commit()
    conn.close()
    return root


def make_opencode_home(root):
    """An opencode home with its session database."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
                              time_created INTEGER, data TEXT);
    """)
    conn.execute("INSERT INTO session VALUES (?, ?)", ("os1", "/work/beta"))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("msg_u1", "os1", 1780000000000,
                  json.dumps({"role": "user", "path": {"cwd": "/work/beta"}})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?)",
                 ("msg_a1", "os1", 1780000001000,
                  json.dumps({"role": "assistant", "modelID": "muse-1",
                              "providerID": "opencode",
                              "path": {"cwd": "/work/beta"},
                              "tokens": {"total": 3000, "input": 800,
                                         "output": 200, "reasoning": 50,
                                         "cache": {"read": 1900, "write": 50}},
                              "finish": "stop"})))
    conn.commit()
    conn.close()
    return root


def make_dsh_home(root):
    """A harness home with one plain (uncompressed) session file.

    Two tool calls belong to step 1 and one to step 2, so a collector that
    repeats the session-wide tool call total on every record cannot pass.
    """
    root.mkdir(parents=True, exist_ok=True)
    session_dir = root / "sessions" / "--work-gamma--" / "session-abc"
    session_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session", "id": "session-abc", "cwd": "/work/gamma"},
        {"type": "request/context", "time": 1780000000000,
         "data": {"provider": "workbuddy", "model": "step-5-preview"}},
        {"type": "user/message", "time": 1780000000100, "data": {}},
        {"type": "tool/call", "time": 1780000000200,
         "data": {"turn": 1, "step": 1, "name": "bash"}},
        {"type": "tool/call", "time": 1780000000250,
         "data": {"turn": 1, "step": 1, "name": "bash"}},
        {"type": "assistant/message", "time": 1780000000300,
         "data": {"turn": 1, "step": 1,
                  "message": {"role": "assistant",
                              "source": {"provider": "workbuddy",
                                         "model": "step-5-preview"}},
                  "stream": [{"type": "chunk",
                             "chunk": {"type": "usage",
                                       "usage": {"inputTokens": 5000,
                                                 "outputTokens": 400,
                                                 "cacheReadTokens": 3000,
                                                 "totalTokens": 5400}}}]}},
        {"type": "assistant/message", "time": 1780000000400,
         "data": {"turn": 1, "step": 2,
                  "message": {"role": "assistant",
                              "source": {"provider": "workbuddy",
                                         "model": "step-5-preview"}},
                  "stream": [{"type": "chunk",
                             "chunk": {"type": "usage",
                                       "usage": {"inputTokens": 5200,
                                                 "outputTokens": 200,
                                                 "cacheReadTokens": 3100,
                                                 "totalTokens": 5400}}}]}},
        {"type": "tool/call", "time": 1780000000450,
         "data": {"turn": 1, "step": 2, "name": "bash"}},
    ]
    write_jsonl(session_dir / "session.v4.jsonl", rows)
    return root


def make_workbuddy_home(root):
    """A WorkBuddy home with per-request usage logs and a context database.

    The real ``~/.workbuddy`` layout holds two different kinds of fact:

    ``projects/<encoded-cwd>/<session-id>.jsonl``
        Every provider turn carries ``providerData.usage`` with the real
        per-request input / output / cached / reasoning token counts.
    ``workbuddy.db`` -> ``session_usage``
        How full the context window of a session is.  That is context
        occupancy, not billed usage, so it must never be summed into the
        token totals.
    """
    root.mkdir(parents=True, exist_ok=True)

    def turn(session_id, ts, step_seq, req, model, inp, out, cached,
             reasoning, kind="function_call", cwd="/work/gamma"):
        return {
            "id": "%s-%s" % (session_id, step_seq),
            "timestamp": ts,
            "type": kind,
            "sessionId": session_id,
            "cwd": cwd,
            "providerData": {
                "model": model,
                "requestModelId": model,
                "stepSeq": step_seq,
                "conversationRequestId": req,
                "messageId": "%s-%s" % (req, step_seq),
                "usage": {
                    "requests": 1,
                    "inputTokens": inp,
                    "outputTokens": out,
                    "totalTokens": inp + out,
                    "inputTokensDetails": [{"cached_tokens": cached}],
                    "outputTokensDetails": [{"reasoning_tokens": reasoning}],
                },
            },
        }

    # Two steps of one request plus a final answer, then a second request.
    write_jsonl(root / "projects" / "-work-gamma-" / "wb-s1.jsonl", [
        turn("wb-s1", 1780000000000, 1, "req-a1", "hy3", 1000, 100, 400, 10),
        turn("wb-s1", 1780000001000, 2, "req-a1", "hy3", 1100, 50, 500, 5),
        turn("wb-s1", 1780000002000, 3, "req-a1", "hy3", 1200, 70, 600, 0,
             kind="message"),
        {"type": "message", "timestamp": 1780000000000, "role": "user",
         "sessionId": "wb-s1",
         "content": [{"type": "input_text", "text": "hello"}]},
    ])
    write_jsonl(root / "projects" / "-work-delta-" / "wb-s2.jsonl", [
        turn("wb-s2", 1780000100000, 1, "req-b1", "deepseek-v4.1-flash",
             300, 30, 100, 0, cwd="/work/delta"),
    ])
    # Noise: a session log without any usage must be ignored entirely.
    write_jsonl(root / "projects" / "-work-delta-" / "wb-s3.jsonl",
                [{"type": "session-meta", "timestamp": 1780000200000,
                  "sessionId": "wb-s3", "meta": {}}])

    db = root / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, cwd TEXT NOT NULL, model TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE session_usage (
            session_id TEXT PRIMARY KEY, used INTEGER NOT NULL,
            size INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            credit_json TEXT);
    """)
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                 ("wb-s1", "/work/gamma", "hy3", 1780000000000, 1780000002000))
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                 ("wb-s2", "/work/delta", "deepseek-v4.1-flash",
                  1780000100000, 1780000100000))
    conn.execute("INSERT INTO session_usage VALUES (?,?,?,?,?)",
                 ("wb-s1", 5000, 192000, 1780000002000, None))
    conn.execute("INSERT INTO session_usage VALUES (?,?,?,?,?)",
                 ("wb-s2", 800, 192000, 1780000100000, None))
    conn.commit()
    conn.close()
    return root


def make_generic_home(root):
    """A generic (cursor style) home with a usage JSONL and a vscdb."""
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": "2026-10-05T11:00:00Z", "model": "cursor-fast",
         "sessionId": "cur-1", "cwd": "/work/delta",
         "inputTokens": 700, "outputTokens": 90, "totalTokens": 790},
        {"timestamp": "2026-10-05T11:01:00Z", "model": "cursor-fast",
         "sessionId": "cur-1", "cwd": "/work/delta",
         "promptTokens": 300, "completionTokens": 40,
         "cachedTokens": 250, "totalTokens": 340},
        {"timestamp": "2026-10-05T11:02:00Z", "model": "cursor-fast",
         "sessionId": "cur-1", "context_window": 100000},
    ]
    write_jsonl(root / "chat-sessions" / "s1.jsonl", rows)

    db = root / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)",
                 ("chat.1", json.dumps({"modelId": "cursor-slow",
                                        "inputTokens": 900,
                                        "outputTokens": 120,
                                        "cachedTokens": 400})))
    conn.commit()
    conn.close()
    return root


@pytest.fixture()
def codex_home(tmp_path):
    return make_codex_home(tmp_path / "codex")


@pytest.fixture()
def claude_home(tmp_path):
    return make_claude_home(tmp_path / "claude")


@pytest.fixture()
def zcode_home(tmp_path):
    return make_zcode_home(tmp_path / "zcode")


@pytest.fixture()
def opencode_home(tmp_path):
    return make_opencode_home(tmp_path / "opencode")


@pytest.fixture()
def dsh_home(tmp_path):
    return make_dsh_home(tmp_path / "dsh")


@pytest.fixture()
def workbuddy_home(tmp_path):
    return make_workbuddy_home(tmp_path / "workbuddy")


def make_pi_home(root):
    """A Pi agent home with session logs carrying usage."""
    root.mkdir(parents=True, exist_ok=True)
    session = root / "sessions" / "--home-zazaji-projects-AINEWS--"
    session.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session", "version": 3,
         "id": "01a0c3d5-68fc-72e0-b577-8a316362ba68",
         "timestamp": "2026-10-20T09:00:00.000Z",
         "cwd": "/home/zazaji/projects/AINEWS"},
        {"type": "model_change", "id": "235feb87", "parentId": None,
         "timestamp": "2026-10-20T09:00:00.100Z", "provider": "apigate",
         "modelId": "c_glm"},
        {"type": "message", "id": "8a2c7bae", "parentId": "235feb87",
         "timestamp": "2026-10-20T09:00:00.200Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "help"}],
                     "timestamp": 1790000000000}},
        {"type": "message", "id": "338da9fd", "parentId": "8a2c7bae",
         "timestamp": "2026-10-20T09:00:05.774Z",
         "message": {"role": "assistant",
                     "content": [{"type": "toolCall",
                                  "id": "chatcmpl-tool-b11", "name": "bash",
                                  "arguments": {"command": "ls"}}],
                     "api": "anthropic-messages", "provider": "apigate",
                     "model": "c_glm",
                     "usage": {"input": 900, "output": 0, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 900},
                     "stopReason": "toolUse",
                     "timestamp": 1790000000000}},
        {"type": "message", "id": "c0c1e50b", "parentId": "338da9fd",
         "timestamp": "2026-10-20T09:00:12.271Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "done"}],
                     "api": "anthropic-messages", "provider": "apigate",
                     "model": "c_glm",
                     "usage": {"input": 1188, "output": 270, "cacheRead": 0,
                               "cacheWrite": 0, "totalTokens": 1458},
                     "stopReason": "stop",
                     "timestamp": 1790000000000}},
        # A tool result carries no usage and must be ignored.
        {"type": "message", "id": "f5a5beeb", "parentId": "338da9fd",
         "timestamp": "2026-10-20T09:00:05.828Z",
         "message": {"role": "toolResult", "toolCallId": "chatcmpl-tool-b11",
                     "content": [{"type": "text", "text": "listing"}],
                     "isError": False, "timestamp": 1790000000000}},
    ]
    write_jsonl(session / "2026-10-20T09-00-00-000Z_01a0c3d5-68fc-72e0-b577-8a316362ba68.jsonl",
                rows)
    return root


@pytest.fixture()
def pi_home(tmp_path):
    return make_pi_home(tmp_path / "pi")


def make_qwen_home(root):
    """A QwenCode home in the real ~/.qwen layout."""
    root.mkdir(parents=True, exist_ok=True)
    usage = root / "usage"
    usage.mkdir(parents=True, exist_ok=True)
    rows = [
        {"schemaVersion": 1, "id": "q1", "timestamp": "2026-10-05T09:00:00Z",
         "localDate": "2026-10-05", "sessionId": "qs1", "model": "glm-4.6",
         "authType": "openai", "source": "main", "inputTokens": 1000,
         "outputTokens": 100, "cachedTokens": 500, "thoughtsTokens": 20,
         "totalTokens": 1620, "apiDurationMs": 900},
        {"schemaVersion": 1, "id": "q2", "timestamp": "2026-10-05T09:05:00Z",
         "localDate": "2026-10-05", "sessionId": "qs1", "model": "glm-5.3-flash",
         "authType": "openai", "source": "main", "inputTokens": 2000,
         "outputTokens": 200, "cachedTokens": 1000, "thoughtsTokens": 0,
         "totalTokens": 3200, "apiDurationMs": 1200},
    ]
    write_jsonl(usage / "token-usage-2026-10.jsonl", rows)
    record = {
        "version": 1, "sessionId": "qs1", "timestamp": 1790000000000,
        "project": "/work/epsilon", "tools": {"totalCalls": 7},
        "skills": {"totalCalls": 2},
    }
    (root / "usage_record.jsonl").write_text(
        json.dumps(record) + chr(10), encoding="utf-8")
    return root


@pytest.fixture()
def qwen_home(tmp_path):
    return make_qwen_home(tmp_path / "qwen")


@pytest.fixture()
def generic_home(tmp_path):
    return make_generic_home(tmp_path / "generic")


@pytest.fixture()
def all_homes(tmp_path, codex_home, claude_home, zcode_home, opencode_home,
              dsh_home, qwen_home, workbuddy_home, pi_home):
    return {
        "codex": codex_home,
        "claudecode": claude_home,
        "zcode": zcode_home,
        "opencode": opencode_home,
        "deepseek-harness": dsh_home,
        "qwencode": qwen_home,
        "workbuddy": workbuddy_home,
        "pi": pi_home,
    }
