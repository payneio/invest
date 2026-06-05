"""Project paths and a couple of shared constants.

Everything is resolved relative to the repo root so the package works the same
from a notebook, a script, or the ``invest-ingest`` entry point.
"""

from __future__ import annotations

from pathlib import Path

# src/invest/config.py -> repo root is three parents up.
ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
FIDELITY_DIR = RAW_DIR / "fidelity"
PROCESSED_DIR = DATA_DIR / "processed"

SYMBOL_MAP_PATH = CONFIG_DIR / "symbol_map.yaml"

# Pipeline outputs.
POSITIONS_PARQUET = PROCESSED_DIR / "positions.parquet"
PRICES_PARQUET = PROCESSED_DIR / "prices.parquet"

# Per-broker raw exports are discovered by invest.brokers (newest CSV per dir).
