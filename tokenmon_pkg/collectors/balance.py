"""Balance, credit and quota collectors.

Token usage answers "how much did I spend"; these collectors answer "what is
left".  Every tool exposes that differently, so each source is handled
explicitly and anything unrecognised is skipped rather than guessed.

Sources
-------
codex
    event_msg / token_count records carry rate_limits: a primary (short
    window) and secondary (weekly) usage percentage, the plan type and a
    credits block with the remaining balance.  Each distinct observation is
    stored as a quota snapshot so the UI can show the trend and the latest
    remaining share.
deepseek-harness
    ~/.dsh/.dshw-usage.json holds the credit book: the active account, its
    currency, per-day opening / closing units and the current balance.  Units
    are integers scaled by HARNESS_UNIT_SCALE (1e8), so 2000000000 units is
    20 CNY.
claudecode
    cost-state records inside the session logs carry the running cost of a
    session in USD plus a per-model breakdown.  These are costs already
    incurred, not a balance, and are stored with kind="cost".
opencode
    Assistant messages carry an optional cost field.  Only non-zero costs are
    stored so the cost table is not flooded with free-model rows.

No credentials, tokens or account identifiers beyond an opaque label are read
or stored.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import paths as P
from ..model import HARNESS_UNIT_SCALE, AccountSnapshot
from .base import (
    BaseCollector,
    CollectContext,
    iter_jsonl,
    load_json,
    open_sqlite_readonly,
)


#: Human labels for the two Codex rate-limit windows.  "Primary" is the
#: short rolling window and "secondary" the weekly one; showing the length
#: makes the differing percentages obvious instead of looking like two
#: accounts with inconsistent balances.
def _window_label(name, minutes):
    try:
        mins = int(minutes)
    except (TypeError, ValueError):
        return name
    if mins >= 10080:
        return "7天窗口"
    if mins >= 1440:
        return str(mins // 1440) + "天窗口"
    if mins >= 60:
        return str(mins // 60) + "小时窗口"
    return str(mins) + "分钟窗口"


def _money(value):
    """Parse a provider amount that may be a string or number."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class CodexQuotaCollector(BaseCollector):
    """Codex rate-limit windows and credit balance."""

    tool = "codex"
    description = "Codex rate limits and credits"

    def collect(self, ctx):
        from .codex import CodexCollector

        for root in self._roots(ctx):
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in CodexCollector()._session_files(root):
                ctx.report.scanned_files += 1
                for snapshot in self._parse(path, ctx):
                    yield snapshot

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.codex_paths().roots)
            roots.extend(P.codex_extra_homes())
        return [r for r in roots if r.exists()]

    def _parse(self, path, ctx):
        seen = set()
        for obj in iter_jsonl(path, ctx.report):
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            limits = payload.get("rate_limits")
            if not isinstance(limits, dict):
                continue
            ts = obj.get("timestamp")
            plan = str(limits.get("plan_type") or "")
            credits = limits.get("credits")
            balance = None
            unlimited = False
            if isinstance(credits, dict):
                unlimited = bool(credits.get("unlimited"))
                if credits.get("balance") is not None:
                    balance = _money(credits.get("balance"))

            for window_name in ("primary", "secondary"):
                window = limits.get(window_name)
                if not isinstance(window, dict):
                    continue
                used = window.get("used_percent")
                if used is None:
                    continue
                minutes = window.get("window_minutes")
                resets_at = window.get("resets_at")
                key = (str(ts), window_name, str(used), str(minutes), str(resets_at))
                if key in seen:
                    continue
                seen.add(key)
                ctx.report.parsed_records += 1
                yield AccountSnapshot(
                    tool=self.tool,
                    kind="quota",
                    ts=ts,
                    account=str(limits.get("limit_id") or "codex"),
                    label=_window_label(window_name, minutes),
                    used_percent=float(used),
                    window_minutes=int(minutes) if minutes else None,
                    resets_at=resets_at,
                    source_path=str(path),
                    metadata={"unlimited": unlimited},
                )

            if balance is not None and not unlimited:
                key = (str(ts), "credits", balance, plan)
                if key not in seen:
                    seen.add(key)
                    ctx.report.parsed_records += 1
                    yield AccountSnapshot(
                        tool=self.tool,
                        kind="balance",
                        ts=ts,
                        account="codex-credits",
                        label="credits",
                        currency="USD",
                        amount=balance,
                        raw_amount=balance,
                        source_path=str(path),
                        metadata={"has_credits": bool(
                            credits.get("has_credits")) if isinstance(credits, dict) else False},
                    )


class HarnessBalanceCollector(BaseCollector):
    """DeepSeek Harness credit book."""

    tool = "deepseek-harness"
    description = "DeepSeek Harness credit balance"

    def _roots(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(P.deepseek_harness_paths().roots)
        return [r for r in roots if r.exists()]

    def collect(self, ctx):
        handled = set()
        for root in self._roots(ctx):
            if root in handled:
                continue
            handled.add(root)
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in root.rglob(".dshw-usage.json"):
                if not path.is_file():
                    continue
                ctx.report.scanned_files += 1
                for snapshot in self._parse(path, ctx):
                    yield snapshot

    def _parse(self, path, ctx):
        data = load_json(path, ctx.report)
        if not isinstance(data, dict):
            return
        accounting = data.get("accounting")
        if not isinstance(accounting, dict):
            return
        books = accounting.get("books")
        active = str(accounting.get("active") or "")
        if not isinstance(books, dict):
            return
        for book_id, book in books.items():
            if not isinstance(book, dict):
                continue
            currency = str(book.get("currency") or "")
            days = book.get("days")
            if not isinstance(days, dict):
                continue
            for day, info in sorted(days.items()):
                if not isinstance(info, dict):
                    continue
                last_units = info.get("lastUnits")
                opening_units = info.get("openingUnits")
                if last_units is None:
                    continue
                ts = info.get("lastAt") or day
                amount = float(last_units) / HARNESS_UNIT_SCALE
                opening = (float(opening_units) / HARNESS_UNIT_SCALE
                           if opening_units is not None else None)
                ctx.report.parsed_records += 1
                yield AccountSnapshot(
                    tool=self.tool,
                    kind="balance",
                    ts=ts,
                    account=str(book_id),
                    label="balance",
                    currency=currency,
                    amount=amount,
                    raw_amount=float(last_units),
                    opening_amount=opening,
                    source_path=str(path),
                    metadata={
                        "day": str(day),
                        "active": book_id == active,
                        "debit_units": info.get("debitUnits"),
                        "credit_units": info.get("creditUnits"),
                    },
                )


class CostCollector(BaseCollector):
    """Session costs reported by claude code and opencode.

    These are money already spent rather than a remaining balance, so they are
    stored with kind="cost" and summed over the selected period.
    """

    description = "Session cost records"

    def __init__(self, tool, description_text):
        self.tool = tool
        self.description = description_text

    def collect(self, ctx):
        roots = [Path(p) for p in ctx.source_dirs]
        if not roots:
            roots.extend(self._resolve().roots)
        for root in roots:
            if not root.exists():
                continue
            if ctx.report is not None:
                ctx.report.root_paths.append(str(root))
            for path in self._files(root):
                ctx.report.scanned_files += 1
                for snapshot in self._parse(path, ctx):
                    yield snapshot

    def _resolve(self):
        raise NotImplementedError

    def _files(self, root):
        raise NotImplementedError


class ClaudeCostCollector(CostCollector):
    """Claude Code cost-state records."""

    def _resolve(self):
        return P.claudecode_paths()

    def _files(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("*.jsonl"):
            if candidate.is_file() and "projects" in candidate.parts:
                yield candidate

    def _parse(self, path, ctx):
        for obj in iter_jsonl(path, ctx.report):
            if obj.get("type") != "cost-state":
                continue
            cost = _money(obj.get("totalCostUSD"))
            if cost <= 0:
                continue
            session_id = str(obj.get("sessionId") or "")
            models = obj.get("modelUsage")
            model_breakdown = {}
            if isinstance(models, dict):
                for name, info in models.items():
                    if isinstance(info, dict):
                        model_breakdown[str(name)] = _money(info.get("costUSD"))
            ctx.report.parsed_records += 1
            yield AccountSnapshot(
                tool=self.tool,
                kind="cost",
                ts=obj.get("startTime"),
                account=session_id,
                label="session",
                currency="USD",
                amount=cost,
                raw_amount=cost,
                source_path=str(path),
                metadata={"model_breakdown": model_breakdown,
                          "has_unknown_model_cost": bool(
                              obj.get("hasUnknownModelCost"))},
            )


class OpenCodeCostCollector(CostCollector):
    """opencode per-message costs."""

    def _resolve(self):
        return P.opencode_paths()

    def _files(self, root):
        root = Path(root)
        if root.is_file():
            yield root
            return
        for candidate in root.rglob("opencode.db"):
            if candidate.is_file():
                yield candidate

    def _parse(self, path, ctx):
        conn = open_sqlite_readonly(path)
        if conn is None:
            ctx.report.warn("cannot open " + str(path) + " read-only")
            return
        try:
            for row in conn.execute("SELECT session_id, data FROM message"):
                try:
                    data = json.loads(row["data"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                cost = _money(data.get("cost"))
                if cost <= 0:
                    continue
                time_info = data.get("time")
                created = (time_info.get("created")
                           if isinstance(time_info, dict) else None)
                ctx.report.parsed_records += 1
                yield AccountSnapshot(
                    tool=self.tool,
                    kind="cost",
                    ts=created,
                    account=str(row["session_id"] or ""),
                    label="message",
                    currency="USD",
                    amount=cost,
                    raw_amount=cost,
                    source_path=str(path),
                    metadata={"model": data.get("modelID")},
                )
        except Exception as exc:
            ctx.report.warn("cost query failed on " + str(path) + ": " + str(exc))
        finally:
            conn.close()


BALANCE_COLLECTORS = [
    CodexQuotaCollector(),
    HarnessBalanceCollector(),
    ClaudeCostCollector("claudecode", "Claude Code session costs"),
    OpenCodeCostCollector("opencode", "opencode message costs"),
]
