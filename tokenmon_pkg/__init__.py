"""tokenmon - AI coding tool token usage analytics.

Collects normalised token usage records from eight AI coding tools, stores them
in a local SQLite database and serves day / week / month analytics over HTTP.
"""

__version__ = "1.0.0"

from . import paths, store
from .model import TOOLS, UsageRecord
from .store import RunReport, SourceReport, Store

__all__ = [
    "TOOLS",
    "UsageRecord",
    "Store",
    "RunReport",
    "SourceReport",
    "paths",
    "store",
]
