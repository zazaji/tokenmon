"""Aggregation and bucketing for day / week / month analytics.

All bucketing is done in the browser time zone supplied by the caller so a
user in Asia/Singapore sees their local day boundaries rather than UTC ones.
The CLI defaults to the local time zone of the machine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

from .model import METRIC_KEYS, UsageRecord, to_utc

PERIODS = ("day", "week", "month")


def get_zone(name: str | None):
    """Return a tzinfo for ``name`` (falls back to the local zone)."""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    local = datetime.now(timezone.utc).astimezone()
    return local.tzinfo or timezone.utc


def _zero() -> dict:
    values = {key: 0 for key in METRIC_KEYS}
    values["interactions"] = 0
    values["records"] = 0
    values["tools"] = set()
    values["models"] = set()
    values["interaction_units"] = set()
    return values


def _accumulate(target: dict, record: UsageRecord) -> None:
    for key in METRIC_KEYS:
        target[key] += int(getattr(record, key) or 0)
    target["records"] += 1
    target["tools"].add(record.tool)
    target["models"].add(record.model)
    if record.is_interaction:
        # An interaction is a user turn; several usage records can belong to
        # the same turn, so count distinct (tool, session, turn) units.
        unit = (record.tool, record.session_id,
                (record.metadata or {}).get("turn_id")
                or (record.metadata or {}).get("turn")
                or record.session_id + "|" + record.ts.isoformat())
        if unit not in target["interaction_units"]:
            target["interaction_units"].add(unit)
            target["interactions"] += 1


def _finalise(target: dict) -> dict:
    out = {k: v for k, v in target.items()
           if k not in ("tools", "models", "interaction_units")}
    out["tools"] = sorted(target["tools"])
    out["models"] = sorted(target["models"])
    out["tool_count"] = len(out["tools"])
    out["model_count"] = len(out["models"])
    out["cache_hit_ratio"] = _ratio(out["cache_read_tokens"],
                                    out["input_tokens"] + out["cache_read_tokens"]
                                    + out["cache_write_tokens"])
    out["billed_tokens"] = (out["input_tokens"] + out["output_tokens"]
                            + out["reasoning_tokens"] + out["cache_write_tokens"])
    return out


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def _bucket_key(moment: datetime, period: str) -> str:
    if period == "day":
        return moment.strftime("%Y-%m-%d")
    if period == "week":
        iso = moment.isocalendar()
        return str(iso[0]) + "-W" + str(iso[1]).zfill(2)
    return moment.strftime("%Y-%m")


def _bucket_label(key: str, period: str) -> str:
    if period == "day":
        return key
    if period == "week":
        year, week = key.split("-W")
        return str(year) + "-W" + str(week)
    return key


def _bucket_range(key: str, period: str, tz):
    """Return (start, end) datetimes for a bucket key."""
    if period == "day":
        start = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=tz)
        return start, start + timedelta(days=1)
    if period == "week":
        year, week = key.split("-W")
        monday = datetime.fromisocalendar(int(year), int(week), 1).replace(tzinfo=tz)
        return monday, monday + timedelta(days=7)
    year, month = key.split("-")
    start = datetime(int(year), int(month), 1, tzinfo=tz)
    if int(month) == 12:
        end = datetime(int(year) + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(int(year), int(month) + 1, 1, tzinfo=tz)
    return start, end


def _iter_buckets(start: datetime, end: datetime, period: str, tz) -> Iterable[str]:
    """Every bucket key between two datetimes, including empty ones."""
    cursor = start.astimezone(tz)
    if period == "day":
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor < end:
            yield cursor.strftime("%Y-%m-%d")
            cursor += timedelta(days=1)
        return
    if period == "week":
        cursor = cursor - timedelta(days=cursor.weekday())
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor < end:
            yield _bucket_key(cursor, "week")
            cursor += timedelta(days=7)
        return
    cursor = cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        yield cursor.strftime("%Y-%m")
        year, month = cursor.year, cursor.month
        cursor = (datetime(year + 1, 1, 1, tzinfo=tz) if month == 12
                  else datetime(year, month + 1, 1, tzinfo=tz))


def _next_month(moment: datetime) -> datetime:
    """First day of the month after ``moment`` (tz preserved)."""
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1)
    return moment.replace(month=moment.month + 1)


def _prev_month(moment: datetime) -> datetime:
    """First day of the month before ``moment`` (tz preserved)."""
    if moment.month == 1:
        return moment.replace(year=moment.year - 1, month=12)
    return moment.replace(month=moment.month - 1)


def shift_period(period: str, anchor, tz=None, steps: int = -1):
    """Move an anchor back (steps<0) or forward (steps>0) by whole periods.

    Used by the previous / next period buttons: the UI keeps an anchor date
    and asks the server for the window that starts one period earlier or
    later, so the user can walk the history without changing span.
    """
    tz = tz or timezone.utc
    anchor_dt = (to_utc(anchor) or datetime.now(timezone.utc)).astimezone(tz)
    if period == "day":
        return anchor_dt + timedelta(days=steps)
    if period == "week":
        return anchor_dt + timedelta(days=7 * steps)
    year, month = anchor_dt.year, anchor_dt.month
    total = year * 12 + (month - 1) + steps
    new_year, new_month = divmod(total, 12)
    return anchor_dt.replace(year=new_year, month=new_month + 1)


def resolve_range(period: str, anchor: datetime | None = None, tz=None,
                  span: int = 1) -> tuple:
    """Return the (start, end) window for the requested period.

    ``anchor`` defaults to now.  ``span`` is how many periods back the window
    reaches (1 = current period only).
    """
    tz = tz or timezone.utc
    anchor = (anchor or datetime.now(timezone.utc)).astimezone(tz)
    if period == "day":
        end = anchor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=span)
    elif period == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = monday + timedelta(days=7)
        start = end - timedelta(days=7 * span)
    else:
        # The window covers exactly span months ending with the current one,
        # so span=1 is "this month only" and span=14 is the last 14 months.
        end = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = _next_month(end)
        start = end
        for _ in range(span):
            start = _prev_month(start)
    return start, end


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------


def _group(records: Iterable[UsageRecord], keyfunc) -> list:
    """Group records and keep the grouping key in each row.

    The key is stored under "key" so callers can render it; single field
    groupings also expose it under a friendly name.
    """
    groups: dict = {}
    for record in records:
        key = keyfunc(record)
        if key not in groups:
            groups[key] = _zero()
        _accumulate(groups[key], record)
    rows = []
    for key, value in groups.items():
        row = _finalise(value)
        row["key"] = key
        rows.append(row)
    return rows


def _with_key(rows: list, name: str) -> list:
    """Promote the grouping key to a named field and sort by total tokens."""
    for row in rows:
        row[name] = row.pop("key")
    return sorted(rows, key=lambda row: row["total_tokens"], reverse=True)


# ---------------------------------------------------------------------------
# Balance, quota and cost
# ---------------------------------------------------------------------------

KIND_LABELS = {
    "balance": "余额",
    "quota": "额度",
    "cost": "已花费",
}


def build_account_report(store, tz_name=None, since=None, until=None,
                         tools=None) -> dict:
    """Latest balance per account plus the cost accumulated in the period.

    ``latest`` keeps one row per (tool, kind, account, label) with the most
    recent observation, so a credit balance is never summed across days.
    ``cost`` sums the cost snapshots of the period, grouped by tool and
    currency, because a cost is additive while a balance is not.
    """
    tz = get_zone(tz_name)
    snapshots = list(store.iter_accounts(tools=tools, since=since, until=until))
    latest_rows = []
    cost_rows = []
    for snapshot in snapshots:
        local_ts = snapshot.ts.astimezone(tz)
        row = snapshot.to_dict()
        row["ts"] = local_ts.isoformat()
        row["kind_label"] = KIND_LABELS.get(snapshot.kind, snapshot.kind)
        if snapshot.kind == "cost":
            cost_rows.append(row)
        else:
            latest_rows.append(row)

    # Newest observation per identity wins for balances and quotas.
    merged = {}
    for row in latest_rows:
        key = (row["tool"], row["kind"], row["account"], row["label"])
        current = merged.get(key)
        if current is None or row["ts"] >= current["ts"]:
            merged[key] = row
    latest = sorted(merged.values(),
                    key=lambda row: (row["tool"], row["kind"], row["label"]))

    cost_by_tool = {}
    for row in cost_rows:
        entry = cost_by_tool.setdefault(
            row["tool"], {"tool": row["tool"], "currency": row["currency"],
                          "amount": 0.0, "sessions": set(), "records": 0})
        entry["amount"] += float(row["amount"] or 0)
        entry["records"] += 1
        if row["account"]:
            entry["sessions"].add(row["account"])
    cost_summary = []
    for entry in cost_by_tool.values():
        entry["sessions"] = len(entry["sessions"])
        entry["amount"] = round(entry["amount"], 6)
        cost_summary.append(entry)
    cost_summary.sort(key=lambda row: row["amount"], reverse=True)

    # Daily cost series for the chart.
    daily = {}
    for row in cost_rows:
        day = str(row["ts"])[:10]
        daily.setdefault(day, {"day": day, "amount": 0.0, "currency": row["currency"]})
        daily[day]["amount"] += float(row["amount"] or 0)
    cost_series = [daily[key] for key in sorted(daily)]

    quota_series = {}
    for row in latest_rows:
        if row["kind"] != "quota":
            continue
        key = (row["tool"], row["account"], row["label"])
        quota_series.setdefault(key, []).append(row)
    for rows in quota_series.values():
        rows.sort(key=lambda row: row["ts"])

    return {
        "latest": latest,
        "cost_summary": cost_summary,
        "cost_series": cost_series,
        "quota_series": {
            "|".join(key): [
                {"ts": row["ts"],
                 "used_percent": row["used_percent"],
                 "remaining_percent": row["remaining_percent"],
                 "label": row["label"],
                 "plan": row["plan"],
                 "window_minutes": row["window_minutes"],
                 "resets_at": row["resets_at"]}
                for row in rows[-200:]
            ]
            for key, rows in quota_series.items()
        },
        "total_cost": round(sum(row["amount"] for row in cost_summary), 6),
        "currencies": sorted({row["currency"] for row in latest if row["currency"]}),
    }


def build_report(store, period: str = "day", tools: Sequence[str] | None = None,
                 tz_name: str | None = None, span: int = 1, anchor=None,
                 models: Sequence[str] | None = None) -> dict:
    """Build the full analytics payload for the web UI and the CLI.

    The report contains a bucket series plus breakdowns by tool, model,
    project and tool+model, so the front end can switch dimension without a
    second request.

    anchor selects which period the window ends on (default now).  Passing an
    earlier date moves the whole window back so the UI can offer previous /
    next period navigation; span is how many periods the window covers.

    ``models`` restricts every dimension of the report to the named models, so
    the KPI cards, the curve and all breakdown tables agree on one filter.
    """
    if period not in PERIODS:
        period = "day"
    tz = get_zone(tz_name)
    anchor_dt = to_utc(anchor) if anchor is not None else None
    start, end = resolve_range(period, anchor=anchor_dt, tz=tz, span=span)

    records = list(store.iter_records(tools=tools, models=models,
                                     since=start, until=end))
    for record in records:
        record.ts = record.ts.astimezone(tz)

    buckets = {key: _zero() for key in _iter_buckets(start, end, period, tz)}
    for record in records:
        key = _bucket_key(record.ts, period)
        if key not in buckets:
            buckets[key] = _zero()
        _accumulate(buckets[key], record)

    series = [dict(_finalise(value), label=_bucket_label(key, period), key=key)
              for key, value in sorted(buckets.items())]

    by_tool = _with_key(
        _group(records, lambda r: r.tool), "tool")
    by_model = _with_key(
        _group(records, lambda r: r.model), "model")
    by_project = _with_key(
        _group(records, lambda r: r.project or "(unknown)"), "project")
    by_tool_model = _group(records, lambda r: (r.tool, r.model))
    by_tool_model = sorted(by_tool_model,
                           key=lambda row: row["total_tokens"], reverse=True)
    for row in by_tool_model:
        key = row.pop("key", None)
        if isinstance(key, (tuple, list)) and len(key) == 2:
            row["tool"], row["model"] = key[0], key[1]

    totals = _zero()
    for record in records:
        _accumulate(totals, record)
    totals = _finalise(totals)

    accounts = build_account_report(store, tz_name=tz_name, since=start,
                                   until=end, tools=tools)

    return {
        "period": period,
        "timezone": str(tz),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "accounts": accounts,
        "totals": totals,
        "buckets": series,
        "by_tool": by_tool,
        "by_model": by_model,
        "by_project": by_project,
        "by_tool_model": by_tool_model,
        "record_count": len(records),
    }
