"""Broker registry — discover raw exports and parse them to one tidy schema.

Each broker has a loader that returns the normalized positions schema (see
``fidelity.load_positions``). To add a broker (e.g. Robinhood):

  1. Write ``src/invest/robinhood.py`` with a ``load_positions(csv_path)`` that
     returns the same columns, including ``broker="robinhood"``.
  2. Register it in ``BROKERS`` below with the subdirectory its exports live in.

The pipeline then picks it up automatically and concatenates everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from . import config, fidelity, robinhood


@dataclass(frozen=True)
class Broker:
    name: str
    subdir: str  # under data/raw/
    loader: Callable[[Path], pd.DataFrame]


# Register brokers here. Each broker's newest CSV under data/raw/<subdir>/ is used.
BROKERS: list[Broker] = [
    Broker(name="fidelity", subdir="fidelity", loader=fidelity.load_positions),
    Broker(name="robinhood", subdir="robinhood", loader=robinhood.load_positions),
]


def _latest_csv(directory: Path) -> Path | None:
    csvs = sorted(directory.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def load_all_positions(verbose: bool = True) -> pd.DataFrame:
    """Load the newest export from every registered broker and concatenate.

    Brokers with no export present are skipped (so a Fidelity-only setup works
    before Robinhood files exist).
    """
    frames: list[pd.DataFrame] = []
    for broker in BROKERS:
        directory = config.RAW_DIR / broker.subdir
        csv = _latest_csv(directory) if directory.exists() else None
        if csv is None:
            if verbose:
                print(f"[brokers] no export for {broker.name} in {directory}; skipping.")
            continue
        if verbose:
            print(f"[brokers] {broker.name}: {csv.name}")
        frames.append(broker.loader(csv))

    if not frames:
        raise FileNotFoundError(
            "No broker exports found under data/raw/. Drop a CSV in e.g. "
            "data/raw/fidelity/."
        )
    return pd.concat(frames, ignore_index=True)
