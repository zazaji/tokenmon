"""Tests for balance, quota and cost collection."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenmon_pkg.analysis import build_account_report
from tokenmon_pkg.collectors.balance import (
    BALANCE_COLLECTORS,
    CodexQuotaCollector,
    HarnessBalanceCollector,
)
from tokenmon_pkg.model import HARNESS_UNIT_SCALE, AccountSnapshot
from tokenmon_pkg.store import Store

from conftest import write_jsonl


def collect(collector, source_dirs):
    from tokenmon_pkg.collectors.base import CollectContext
    from tokenmon_pkg.store import SourceReport

    report = SourceReport(tool=collector.tool)
    ctx = CollectContext(roots=[], report=report, source_dirs=source_dirs)
    return list(collector.collect(ctx)), report


def make_codex_home_with_limits(root):
    """A codex home whose session carries rate limit snapshots."""
    root.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": "2026-10-05T09:00:05.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 10}},
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 42.0, "window_minutes": 300,
                            "resets_at": 1792000000},
                "secondary": {"used_percent": 12.0, "window_minutes": 10080,
                              "resets_at": 1792000000},
                "credits": {"has_credits": True, "unlimited": False,
                            "balance": "5.25"},
                "plan_type": "plus",
            },
        },
    }
    write_jsonl(root / "sessions" / "rollout-a.jsonl", [row])
    return root


def make_harness_home_with_book(root):
    """A harness home with a credit book."""
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": "2026-10-05",
        "lastBalance": 20,
        "todayUsage": 0,
        "accounting": {
            "version": 1,
            "active": "book-1-CNY",
            "books": {
                "book-1-CNY": {
                    "currency": "CNY",
                    "days": {
                        "2026-10-04": {"day": "2026-10-04", "firstAt": 1790000000000,
                                       "lastAt": 1790086400000,
                                       "openingUnits": 25 * HARNESS_UNIT_SCALE,
                                       "lastUnits": 22 * HARNESS_UNIT_SCALE,
                                       "debitUnits": 3 * HARNESS_UNIT_SCALE,
                                       "creditUnits": 0, "revision": 0},
                        "2026-10-05": {"day": "2026-10-05", "firstAt": 1790086400000,
                                       "lastAt": 1790172800000,
                                       "openingUnits": 22 * HARNESS_UNIT_SCALE,
                                       "lastUnits": 20 * HARNESS_UNIT_SCALE,
                                       "debitUnits": 2 * HARNESS_UNIT_SCALE,
                                       "creditUnits": 0, "revision": 0},
                    },
                    "lastAt": 1790172800000,
                }
            },
            "migratedAt": 1790000000000,
        },
        "dayStart": 20,
    }
    (root / ".dshw-usage.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


# ------------------------------------------------------------- codex quota

def test_codex_quota_windows(tmp_path):
    home = make_codex_home_with_limits(tmp_path / "codex")
    snapshots, report = collect(CodexQuotaCollector(), [home])
    quotas = [s for s in snapshots if s.kind == "quota"]
    assert len(quotas) == 2
    labels = {s.label for s in quotas}
    assert labels == {"primary", "secondary"}
    primary = [s for s in quotas if s.label == "primary"][0]
    assert primary.used_percent == 42.0
    assert primary.remaining_percent == 58.0
    assert primary.window_minutes == 300
    assert primary.plan == "plus"
    assert primary.resets_at is not None


def test_codex_credit_balance(tmp_path):
    home = make_codex_home_with_limits(tmp_path / "codex")
    snapshots, _ = collect(CodexQuotaCollector(), [home])
    balances = [s for s in snapshots if s.kind == "balance"]
    assert len(balances) == 1
    assert balances[0].amount == 5.25
    assert balances[0].currency == "USD"


def test_codex_quota_dedupes_repeated_snapshots(tmp_path):
    home = make_codex_home_with_limits(tmp_path / "codex")
    # Same limits repeated at another timestamp produce a new observation.
    row = {
        "timestamp": "2026-10-05T09:05:05.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 42.0, "window_minutes": 300,
                            "resets_at": 1792000000},
                "credits": {"has_credits": False, "unlimited": True,
                            "balance": "0"},
                "plan_type": "plus",
            },
        },
    }
    path = home / "sessions" / "rollout-a.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    snapshots, _ = collect(CodexQuotaCollector(), [home])
    # The unlimited credits row must not add a second balance snapshot; the
    # first row (has_credits=True, balance 5.25) is still reported.
    balances = [s for s in snapshots if s.kind == "balance"]
    assert len(balances) == 1
    assert balances[0].metadata["has_credits"] is True
    primary = [s for s in snapshots if s.label == "primary"]
    assert len(primary) == 2


# -------------------------------------------------------- harness balance

def test_harness_balance_scales_units(tmp_path):
    home = make_harness_home_with_book(tmp_path / "dsh")
    snapshots, report = collect(HarnessBalanceCollector(), [home])
    assert len(snapshots) == 2
    by_day = {s.metadata["day"]: s for s in snapshots}
    assert by_day["2026-10-05"].amount == 20.0
    assert by_day["2026-10-05"].opening_amount == 22.0
    assert by_day["2026-10-04"].amount == 22.0
    assert by_day["2026-10-04"].opening_amount == 25.0
    assert all(s.currency == "CNY" for s in snapshots)
    assert all(s.kind == "balance" for s in snapshots)
    assert by_day["2026-10-05"].metadata["active"] is True


def test_harness_balance_missing_file(tmp_path):
    snapshots, report = collect(HarnessBalanceCollector(), [tmp_path / "empty"])
    assert snapshots == []
    assert report.errors == 0


# ----------------------------------------------------------- store + report

def test_insert_accounts_is_idempotent(tmp_path):
    store = Store(tmp_path / "a.db")
    snapshot = AccountSnapshot(tool="codex", record_key="", kind="quota",
                               ts="2026-10-05T09:00:00Z", used_percent=42.0,
                               window_minutes=300)
    assert store.insert_accounts([snapshot]) == (1, 0)
    assert store.insert_accounts([snapshot]) == (0, 1)
    store.close()


def test_latest_account_keeps_newest(tmp_path):
    store = Store(tmp_path / "b.db")
    store.insert_accounts([
        AccountSnapshot(tool="codex", record_key="k1", kind="quota",
                        ts="2026-10-05T09:00:00Z", label="primary",
                        used_percent=10.0, window_minutes=300),
        AccountSnapshot(tool="codex", record_key="k2", kind="quota",
                        ts="2026-10-05T10:00:00Z", label="primary",
                        used_percent=55.0, window_minutes=300),
        AccountSnapshot(tool="codex", record_key="k3", kind="quota",
                        ts="2026-10-05T10:00:00Z", label="secondary",
                        used_percent=5.0, window_minutes=10080),
    ])
    latest = store.latest_accounts()
    by_label = {s.label: s for s in latest}
    assert by_label["primary"].used_percent == 55.0
    assert by_label["secondary"].used_percent == 5.0
    store.close()


def test_iter_accounts_filters(tmp_path):
    store = Store(tmp_path / "c.db")
    store.insert_accounts([
        AccountSnapshot(tool="codex", record_key="a1", kind="quota",
                        ts="2026-10-05T09:00:00Z", used_percent=1.0),
        AccountSnapshot(tool="deepseek-harness", record_key="a2",
                        kind="balance", ts="2026-10-05T09:00:00Z",
                        currency="CNY", amount=20.0),
    ])
    assert len(list(store.iter_accounts(tools=["codex"]))) == 1
    assert len(list(store.iter_accounts(kinds=["balance"]))) == 1
    store.close()


def test_account_report_latest_and_cost(tmp_path):
    store = Store(tmp_path / "d.db")
    store.insert_accounts([
        AccountSnapshot(tool="deepseek-harness", record_key="b1",
                        kind="balance", ts="2026-10-05T09:00:00Z",
                        currency="CNY", amount=20.0, opening_amount=22.0),
        AccountSnapshot(tool="deepseek-harness", record_key="b2",
                        kind="balance", ts="2026-10-04T09:00:00Z",
                        currency="CNY", amount=22.0, opening_amount=25.0),
        AccountSnapshot(tool="claudecode", record_key="c1", kind="cost",
                        ts="2026-10-05T09:00:00Z", currency="USD",
                        amount=1.5, account="s1"),
        AccountSnapshot(tool="claudecode", record_key="c2", kind="cost",
                        ts="2026-10-05T10:00:00Z", currency="USD",
                        amount=2.5, account="s1"),
    ])
    data = build_account_report(store, tz_name="UTC")
    # Only the newest balance per account survives.
    balances = [r for r in data["latest"] if r["kind"] == "balance"]
    assert len(balances) == 1
    assert balances[0]["amount"] == 20.0
    assert balances[0]["opening_amount"] == 22.0
    # Costs are additive.
    costs = {r["tool"]: r for r in data["cost_summary"]}
    assert costs["claudecode"]["amount"] == 4.0
    assert costs["claudecode"]["sessions"] == 1
    assert costs["claudecode"]["records"] == 2
    assert data["total_cost"] == 4.0
    assert [r["day"] for r in data["cost_series"]] == ["2026-10-05"]
    # currencies lists the currencies of balances and quotas only;
    # costs are USD and are reported separately.
    assert data["currencies"] == ["CNY"]
    store.close()


def test_account_report_quota_series(tmp_path):
    store = Store(tmp_path / "e.db")
    for index, percent in enumerate((10.0, 30.0, 55.0)):
        store.insert_accounts([AccountSnapshot(
            tool="codex", record_key="q" + str(index), kind="quota",
            ts="2026-10-05T09:0" + str(index) + ":00Z", label="primary",
            used_percent=percent, window_minutes=300)])
    data = build_account_report(store, tz_name="UTC")
    series = data["quota_series"]["codex||primary"]
    assert [p["used_percent"] for p in series] == [10.0, 30.0, 55.0]
    assert series[-1]["remaining_percent"] == 45.0
    store.close()


def test_balance_collectors_registered():
    tools = {c.tool for c in BALANCE_COLLECTORS}
    assert tools == {"codex", "deepseek-harness", "claudecode", "opencode"}


def test_snapshot_to_row_roundtrip():
    snapshot = AccountSnapshot(tool="codex", record_key="k", kind="quota",
                               ts="2026-10-05T09:00:00Z", used_percent=42.0,
                               window_minutes=300, resets_at=1792000000,
                               plan="plus")
    row = snapshot.to_row()
    assert row[0] == "codex"
    assert row[3] == "quota"
    assert row[9] == 42.0
    assert row[10] == 58.0
    assert row[11] == 300
    assert row[11] == 300 and row[12] is not None
