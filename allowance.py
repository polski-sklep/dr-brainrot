"""The label-aware pooled allowance. Phase 1 stub; Agent D owns and implements it.

Signatures are fixed by CONTRACTS.md Part II §7. Until Agent D lands, the hook
evidence.finish_import calls is a deliberate no-op so imports still work.
"""
from __future__ import annotations

import sqlite3

from clock import Clock
from config import Settings


def on_measurements_changed(conn: sqlite3.Connection, clk: Clock, settings: Settings) -> None:
    """Recompute stored debt for every closed week whose minutes may have changed."""
    return None
