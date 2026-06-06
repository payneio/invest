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
PROCESSED_DIR = DATA_DIR / "processed"
# Broker-specific paths under RAW_DIR live in invest.broker.<broker> (e.g.
# invest.broker.robinhood.HISTORY_DIR), not here.

SYMBOL_MAP_PATH = CONFIG_DIR / "symbol_map.yaml"
# Optional account_number -> human name map (gitignored; holds account numbers).
ACCOUNTS_MAP_PATH = CONFIG_DIR / "accounts.yaml"

# Beancount ledger — the curated source of truth (gitignored; holds holdings).
LEDGER_DIR = ROOT / "ledger"
LEDGER_MAIN = LEDGER_DIR / "main.beancount"
LEDGER_FIXUPS = LEDGER_DIR / "fixups.beancount"

# Pipeline outputs.
POSITIONS_PARQUET = PROCESSED_DIR / "positions.parquet"
PRICES_PARQUET = PROCESSED_DIR / "prices.parquet"
TRANSACTIONS_PARQUET = PROCESSED_DIR / "transactions.parquet"

# Per-source raw exports are discovered by invest.broker (newest CSV per dir).
