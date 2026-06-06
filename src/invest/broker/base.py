"""The ``Broker`` descriptor + file-discovery helpers.

Lives in its own module so each ``invest.broker.<name>`` package can import the
descriptor type without triggering the registry (``invest.broker.__init__``), which
imports the packages — that would be circular.

A broker package declares ONE ``Broker`` describing everything broker-specific:
its loaders/locators and a few **quirk flags** the generic ledger/pipeline consult
instead of branching on the broker's name. Add a broker by writing its package and
appending its ``BROKER`` to ``invest.broker.BROKERS`` — no generic code changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd


@dataclass(frozen=True)
class Broker:
    name: str
    subdir: str  # under data/raw/
    # Discovery + parsing (None where a broker doesn't provide that entity).
    positions_loader: Callable[[Path], pd.DataFrame] | None = None
    positions_locator: Callable[[Path], Path | None] | None = None
    transactions_loader: Callable[[Path], pd.DataFrame] | None = None
    transactions_locator: Callable[[Path], Path | None] | None = None
    # Quirks the generic ledger/pipeline consult instead of branching on the name:
    snapshot_has_accounts: bool = True   # do positions carry an account_number?
    has_cash_snapshot: bool = True       # does the positions snapshot include cash?
    cash_symbols: frozenset[str] = field(default_factory=frozenset)  # MMF tickers → USD


def latest(paths) -> Path | None:
    """Most recent export: a ``YYYYMMDD``-prefixed name wins by name (deterministic,
    copy-safe); otherwise newest mtime."""
    paths = list(paths)
    if not paths:
        return None
    dated = [p for p in paths if re.match(r"\d{8}", p.name)]
    if dated:
        return max(dated, key=lambda p: p.name)
    return max(paths, key=lambda p: p.stat().st_mtime)


def latest_csv(directory: Path) -> Path | None:
    return latest(directory.glob("*.csv"))


def latest_json(directory: Path) -> Path | None:
    return latest(directory.glob("*.json"))
