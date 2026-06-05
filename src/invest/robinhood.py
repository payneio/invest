"""Parse a manual Robinhood holdings list into the normalized positions schema.

Robinhood has no clean positions-CSV export, so this reads a simple hand-kept
file of ``SYMBOL QUANTITY`` lines (whitespace- or comma-separated), e.g.::

    GOOG 276
    TSM 100
    NVDA 550

There is no price or cost-basis data here — prices come from yfinance during
enrichment, so every symbol must be classified ``price_source: yfinance`` in
config/symbol_map.yaml. Cost basis is left NA (unknown), so unrealized gain/loss
is intentionally not reported for these holdings.

Output matches ``fidelity.load_positions`` column-for-column so both brokers flow
through the same pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

# Columns the rest of the pipeline expects, in order.
_SCHEMA = [
    "broker",
    "account_number",
    "account_name",
    "symbol",
    "symbol_raw",
    "description",
    "quantity",
    "last_price",
    "current_value",
    "cost_basis",
    "avg_cost_basis",
    "total_gain_loss",
    "total_gain_loss_pct",
    "is_cash",
]

# "GOOG 276", "GOOG,276", "GOOG  276.5" — symbol then quantity.
_LINE = re.compile(r"^\s*([A-Za-z.\-]+)[\s,]+([0-9][0-9_.]*)\s*$")


def load_positions(csv_path: str | Path) -> pd.DataFrame:
    """Load a manual Robinhood ``SYMBOL QUANTITY`` holdings file.

    Blank lines and lines starting with ``#`` are ignored. Lines that don't match
    the ``SYMBOL QUANTITY`` shape raise, so a typo surfaces instead of silently
    dropping a holding.
    """
    csv_path = Path(csv_path)
    rows = []
    for lineno, raw in enumerate(csv_path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            raise ValueError(
                f"{csv_path.name}:{lineno}: expected 'SYMBOL QUANTITY', got {raw!r}"
            )
        symbol = m.group(1).upper()
        quantity = float(m.group(2).replace("_", ""))
        rows.append((symbol, quantity))

    df = pd.DataFrame(rows, columns=["symbol", "quantity"])
    df["broker"] = "robinhood"
    df["account_number"] = pd.NA
    df["account_name"] = "Robinhood"
    df["symbol_raw"] = df["symbol"]
    df["description"] = pd.NA
    # No price / cost data in a manual list — filled by yfinance, or left unknown.
    for col in [
        "last_price",
        "current_value",
        "cost_basis",
        "avg_cost_basis",
        "total_gain_loss",
        "total_gain_loss_pct",
    ]:
        df[col] = np.nan
    df["quantity"] = df["quantity"].astype("float64")
    df["is_cash"] = False

    return df[_SCHEMA].reset_index(drop=True)
