"""Broker registry — collect each broker's self-describing ``Broker`` and load all.

Each ``invest.broker.<name>`` package declares a ``BROKER`` (see :mod:`invest.broker.base`)
holding its loaders, locators, and quirk flags. **To add a broker: write its package and
append its ``BROKER`` to ``BROKERS`` below — no other generic code changes.**
"""

from __future__ import annotations

import pandas as pd

from invest import config, schema
from invest.broker import fidelity, robinhood
from invest.broker.base import Broker, latest_csv, latest_json  # noqa: F401  (re-exported)

# The registry. Appending a broker's descriptor here is the only edit adding one needs.
BROKERS: list[Broker] = [fidelity.BROKER, robinhood.BROKER]
BY_NAME: dict[str, Broker] = {b.name: b for b in BROKERS}

# Back-compat private aliases (default positions locator + any external callers).
_latest_csv = latest_csv
_latest_json = latest_json


def load_all_positions(verbose: bool = True) -> pd.DataFrame:
    """Load the newest positions export from every broker and concatenate.

    Brokers with no export present are skipped (so a Fidelity-only setup works
    before Robinhood files exist).
    """
    frames: list[pd.DataFrame] = []
    for src in BROKERS:
        if src.positions_loader is None:
            continue
        directory = config.RAW_DIR / src.subdir
        locator = src.positions_locator or _latest_csv
        export = locator(directory) if directory.exists() else None
        if export is None:
            if verbose:
                print(f"[broker] no positions export for {src.name} in {directory}; skipping.")
            continue
        if verbose:
            print(f"[broker] {src.name} positions: {export.name}")
        frames.append(src.positions_loader(export))

    if not frames:
        raise FileNotFoundError(
            "No broker exports found under data/raw/. Drop a CSV in e.g. "
            "data/raw/fidelity/."
        )
    return pd.concat(frames, ignore_index=True)


def load_all_transactions(verbose: bool = True) -> pd.DataFrame:
    """Load and unify the transaction ledger from every broker that has one.

    Returns the canonical ``TRANSACTIONS_SCHEMA`` frame (empty-but-typed if no
    broker has a store). Brokers with no store are skipped. Globally deduped by
    ``(broker, id)`` and sorted deterministically so re-ingest is idempotent.
    """
    frames: list[pd.DataFrame] = [schema.empty_transactions()]
    for src in BROKERS:
        if src.transactions_loader is None or src.transactions_locator is None:
            continue
        directory = config.RAW_DIR / src.subdir
        store = src.transactions_locator(directory) if directory.exists() else None
        if store is None:
            if verbose:
                print(f"[broker] no transaction store for {src.name}; skipping.")
            continue
        if verbose:
            print(f"[broker] {src.name} transactions: {store.name}")
        frames.append(src.transactions_loader(store))

    tx = pd.concat(frames, ignore_index=True)
    if not tx.empty:
        tx = tx.drop_duplicates(subset=["broker", "id"], keep="first")
        tx = tx.sort_values(["broker", "date", "id"], na_position="last").reset_index(drop=True)
    return tx
