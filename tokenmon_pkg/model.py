"""Unified usage record model shared by every collector.

All eight tools expose their token accounting with slightly different field
names and slightly different semantics (some providers report a cached-token
count, others only a prompt/completion split).  This module normalises them
into a single :class:"UsageRecord" so the analysis and web layers never need
to know which tool a row came from.

Token semantics
---------------
input_tokens
    Non-cached prompt tokens.
output_tokens
    Completion tokens (excluding reasoning when the provider splits it).
reasoning_tokens
    Reasoning / thinking tokens.
cache_read_tokens
    Tokens served from cache - i.e. **cache hits**.
cache_write_tokens
    Tokens written into the cache (cache creation).
total_tokens
    Provider reported total when available, otherwise
    input + output + reasoning + cache_read + cache_write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

TOOLS = (
    "codex",
    "claudecode",
    "zcode",
    "opencode",
    "deepseek-harness",
    "antigravity",
    "qwencode",
    "cursor",
)

#: metric keys that the aggregation layer knows about
METRIC_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "llm_calls",
    "tool_calls",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: Any) -> datetime | None:
    """Coerce a timestamp of unknown provenance into an aware UTC datetime.

    Accepts ISO-8601 strings (with or without Z / offset), epoch seconds,
    epoch milliseconds and naive datetimes (assumed UTC).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return to_utc(float(raw))
            except (TypeError, ValueError):
                return None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e14:  # microseconds
            number /= 1_000_000
        elif number > 1e11:  # milliseconds
            number /= 1_000
        try:
            dt = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


#: Model aliases that differ only in casing or separators.  Tools record the
#: same model with different conventions (GLM-5.3-Flash vs glm-5.3-flash),
#: which would otherwise split one model across several rows.
MODEL_ALIASES = {
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3": "GLM-5.3",
    "gpt-6.1-sol": "gpt-6.1-sol",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-6-luna": "gpt-6-luna",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "dsv4": "deepseek-v4.1-flash",
    "c_glm": "GLM-5.3-Flash",
}


def canonical_model(name: Any) -> str:
    """Normalise a model name for grouping.

    Applies the alias table and, as a fallback, collapses the name to lower
    case when a differently cased variant of it is already known.  Unknown
    names are returned trimmed but otherwise untouched so new models show up
    immediately instead of being hidden.
    """
    if not name:
        return "unknown"
    text = str(name).strip()
    if not text:
        return "unknown"
    return MODEL_ALIASES.get(text.lower(), text)


def _as_int(value: Any) -> int:
    """Best effort non negative integer conversion."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


@dataclass
class UsageRecord:
    """One normalised usage observation."""

    tool: str
    record_key: str
    ts: datetime
    model: str = "unknown"
    provider: str = ""
    session_id: str = ""
    project: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 1
    tool_calls: int = 0
    is_interaction: int = 0
    status: str = "ok"
    source_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ts = to_utc(self.ts) or utcnow()
        self.input_tokens = _as_int(self.input_tokens)
        self.output_tokens = _as_int(self.output_tokens)
        self.reasoning_tokens = _as_int(self.reasoning_tokens)
        self.cache_read_tokens = _as_int(self.cache_read_tokens)
        self.cache_write_tokens = _as_int(self.cache_write_tokens)
        self.tool_calls = _as_int(self.tool_calls)
        self.llm_calls = _as_int(self.llm_calls) or 1
        self.is_interaction = 1 if self.is_interaction else 0
        computed = (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
        self.total_tokens = _as_int(self.total_tokens)
        if self.total_tokens <= 0:
            self.total_tokens = computed
        self.model = canonical_model(self.model)
        if not self.record_key:
            self.record_key = self.build_key()

    # -- derived helpers ---------------------------------------------------
    @property
    def billed_tokens(self) -> int:
        """Tokens that are not cache hits (cache reads are usually discounted)."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_write_tokens
        )

    @property
    def cache_hit_ratio(self) -> float:
        """Share of input-side tokens that were served from cache."""
        input_side = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        if input_side <= 0:
            return 0.0
        return self.cache_read_tokens / input_side

    def build_key(self) -> str:
        """Stable identity used for idempotent imports."""
        payload = "|".join(
            [
                self.tool,
                self.session_id,
                self.model,
                self.provider,
                self.ts.isoformat(),
                str(self.input_tokens),
                str(self.output_tokens),
                str(self.cache_read_tokens),
                str(self.cache_write_tokens),
                self.source_path,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]

    def to_row(self) -> tuple[Any, ...]:
        """Tuple matching the usage_records column order."""
        return (
            self.tool,
            self.record_key,
            self.ts.isoformat(),
            self.model,
            self.provider,
            self.session_id,
            self.project,
            self.input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.total_tokens,
            self.llm_calls,
            self.tool_calls,
            self.is_interaction,
            self.status,
            self.source_path,
            json.dumps(self.metadata, ensure_ascii=False, sort_keys=True, default=str),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ts"] = self.ts.isoformat()
        return data


#: Unit scale used by DeepSeek Harness accounting books.
HARNESS_UNIT_SCALE = 100_000_000


@dataclass
class AccountSnapshot:
    """One observation of an account balance or quota window.

    Different tools expose very different shapes: a credit balance in a
    currency, a percentage of a rolling rate-limit window, or a paid cost
    accumulated over a session.  All of them are normalised here so the web
    layer can render one table.

    ``kind`` selects the rendering and the unit:

    ``balance``
        Absolute amount left, in ``currency``.
    ``quota``
        Percentage of a rate-limit window that is used.
    ``cost``
        Money already spent in the selected period.
    """

    tool: str
    kind: str
    ts: datetime
    account: str = ""
    currency: str = ""
    #: absolute amount (balance / cost)
    amount: float = 0.0
    #: original provider amount before scaling, for display
    raw_amount: float = 0.0
    #: percentage of a window that is used (quota)
    used_percent: float | None = None
    #: window length in minutes and reset time (quota)
    window_minutes: int | None = None
    resets_at: datetime | None = None
    #: opening amount for the day, when the provider tracks it
    opening_amount: float | None = None
    plan: str = ""
    label: str = ""
    source_path: str = ""
    record_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ts = to_utc(self.ts) or utcnow()
        if self.resets_at is not None:
            self.resets_at = to_utc(self.resets_at)
        if not self.record_key:
            self.record_key = self.build_key()

    def build_key(self) -> str:
        payload = "|".join([
            self.tool,
            self.kind,
            self.account,
            self.label,
            self.ts.isoformat(),
            str(self.amount),
            str(self.used_percent),
            str(self.window_minutes),
        ])
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]

    @property
    def remaining_percent(self) -> float | None:
        """Unused share of a quota window (None for balances and costs)."""
        if self.used_percent is None:
            return None
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.tool,
            self.record_key,
            self.ts.isoformat(),
            self.kind,
            self.account,
            self.label,
            self.currency,
            float(self.amount),
            float(self.raw_amount),
            float(self.used_percent) if self.used_percent is not None else None,
            float(self.remaining_percent) if self.remaining_percent is not None else None,
            int(self.window_minutes) if self.window_minutes is not None else None,
            self.resets_at.isoformat() if self.resets_at else None,
            float(self.opening_amount) if self.opening_amount is not None else None,
            self.plan,
            self.source_path,
            json.dumps(self.metadata, ensure_ascii=False, sort_keys=True, default=str),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ts"] = self.ts.isoformat()
        data["resets_at"] = self.resets_at.isoformat() if self.resets_at else None
        data["remaining_percent"] = self.remaining_percent
        return data


def sum_records(records: Iterable[UsageRecord]) -> dict[str, int]:
    """Aggregate the numeric fields of records into a flat dict."""
    totals = {key: 0 for key in METRIC_KEYS}
    for rec in records:
        for key in METRIC_KEYS:
            totals[key] += int(getattr(rec, key) or 0)
    return totals


def dedupe(records: Iterable[UsageRecord]) -> list[UsageRecord]:
    """Drop records that share a record_key (first wins)."""
    seen: set[str] = set()
    result: list[UsageRecord] = []
    for rec in records:
        if rec.record_key in seen:
            continue
        seen.add(rec.record_key)
        result.append(rec)
    return result
