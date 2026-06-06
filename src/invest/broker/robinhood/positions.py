"""Parse Robinhood holdings into the normalized positions schema.

Two inputs, dispatched by file type:

* ``positions.json`` — fetched by ``invest-robinhood-fetch`` via ``robin_stocks``'
  ``build_holdings()``. Carries quantity **and** average cost basis + current price,
  so it's the preferred Robinhood snapshot (and the reconciliation target for the
  derived ledger).
* a manual ``SYMBOL QUANTITY`` ``*.csv`` list — the legacy fallback if no
  ``positions.json`` is present. No price/cost (prices come from yfinance).

Output matches ``fidelity.load_positions`` column-for-column so both brokers flow
through the same pipeline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from invest.schema import POSITIONS_SCHEMA

# "GOOG 276", "GOOG,276", "GOOG  276.5" — symbol then quantity.
_LINE = re.compile(r"^\s*([A-Za-z.\-]+)[\s,]+([0-9][0-9_.]*)\s*$")


def load_positions(path: str | Path) -> pd.DataFrame:
    """Load Robinhood holdings — fetched ``positions.json`` or a manual list."""
    path = Path(path)
    if path.suffix == ".json":
        return _load_json(path)
    return _load_manual(path)


def _num(x: object) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _load_json(path: str | Path) -> pd.DataFrame:
    """Parse the ``build_holdings()`` dict: ``{symbol: {quantity, average_buy_price,
    price, equity, name, ...}}`` into the positions schema (with cost basis)."""
    holdings = json.loads(Path(path).read_text())
    rows = []
    for symbol, h in holdings.items():
        qty = _num(h.get("quantity"))
        avg = _num(h.get("average_buy_price"))
        rows.append({
            "broker": "robinhood",
            "account_number": pd.NA,
            "account_name": "Robinhood",
            "symbol": str(symbol).upper(),
            "symbol_raw": str(symbol).upper(),
            "description": h.get("name"),
            "quantity": qty,
            "last_price": _num(h.get("price")),
            "current_value": _num(h.get("equity")),
            "cost_basis": avg * qty if avg == avg and qty == qty else np.nan,
            "avg_cost_basis": avg,
            "total_gain_loss": np.nan,
            "total_gain_loss_pct": np.nan,
            "is_cash": False,
        })
    df = pd.DataFrame(rows)
    return df[POSITIONS_SCHEMA].reset_index(drop=True) if not df.empty else df


def _load_manual(csv_path: str | Path) -> pd.DataFrame:
    """Load a manual ``SYMBOL QUANTITY`` holdings file (legacy fallback).

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
        rows.append((m.group(1).upper(), float(m.group(2).replace("_", ""))))

    df = pd.DataFrame(rows, columns=["symbol", "quantity"])
    df["broker"] = "robinhood"
    df["account_number"] = pd.NA
    df["account_name"] = "Robinhood"
    df["symbol_raw"] = df["symbol"]
    df["description"] = pd.NA
    for col in ["last_price", "current_value", "cost_basis", "avg_cost_basis",
                "total_gain_loss", "total_gain_loss_pct"]:
        df[col] = np.nan
    df["quantity"] = df["quantity"].astype("float64")
    df["is_cash"] = False
    return df[POSITIONS_SCHEMA].reset_index(drop=True)
