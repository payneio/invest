"""Parse a Fidelity "Portfolio Positions" CSV export into a clean DataFrame.

The export is messy in predictable ways:
  * Money-market / cash rows carry trailing ``*`` / ``**`` on the symbol and have
    blank Quantity / Last Price (only Current Value is meaningful).
  * Money columns are strings like ``$1,234.56``, ``-$3,308.04``, ``+$169.50``.
  * Percent columns look like ``-3.17%``.
  * The file ends with blank lines and quoted legal disclaimer paragraphs that
    are not positions.

We keep this parser tolerant: anything without a usable Symbol is dropped, so the
trailing disclaimer rows fall away on their own.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from invest.schema import POSITIONS_SCHEMA

# Raw header -> tidy snake_case name. Only columns we care about are renamed;
# the rest are passed through untouched.
_COLUMN_RENAMES = {
    "Account Number": "account_number",
    "Account Name": "account_name",
    "Symbol": "symbol_raw",
    "Description": "description",
    "Quantity": "quantity",
    "Last Price": "last_price",
    "Current Value": "current_value",
    "Total Gain/Loss Dollar": "total_gain_loss",
    "Total Gain/Loss Percent": "total_gain_loss_pct",
    "Cost Basis Total": "cost_basis",
    "Average Cost Basis": "avg_cost_basis",
    "Type": "type",
}

_MONEY_COLUMNS = [
    "last_price",
    "current_value",
    "total_gain_loss",
    "cost_basis",
    "avg_cost_basis",
]
_PERCENT_COLUMNS = ["total_gain_loss_pct"]


def _parse_money(series: pd.Series) -> pd.Series:
    """Turn ``$1,234.56`` / ``-$1,234.56`` / ``+$169.50`` / ``""`` into floats."""
    cleaned = (
        series.astype("string")
        .str.replace(r"[\$,+]", "", regex=True)
        .str.strip()
        .replace({"": pd.NA, "--": pd.NA, "n/a": pd.NA})
    )
    # Plain numpy float64 (not nullable Float64) so downstream numpy/matplotlib
    # alignment doesn't choke on masked arrays.
    return pd.to_numeric(cleaned, errors="coerce").astype("float64")


def _parse_percent(series: pd.Series) -> pd.Series:
    """Turn ``-3.17%`` into the float ``-3.17`` (percent units, not fraction)."""
    cleaned = (
        series.astype("string")
        .str.replace(r"[%+,]", "", regex=True)
        .str.strip()
        .replace({"": pd.NA})
    )
    return pd.to_numeric(cleaned, errors="coerce").astype("float64")


def clean_symbol(raw: object) -> str | None:
    """Strip Fidelity's trailing ``*`` markers and surrounding whitespace.

    ``"FCASH**"`` -> ``"FCASH"``, ``"MSFT"`` -> ``"MSFT"``. Returns ``None`` for
    blanks so non-position rows can be filtered out.
    """
    # Catches None, float NaN, and pandas' pd.NA (the "string" dtype missing
    # value) — the latter would otherwise stringify to the literal "<NA>".
    if not isinstance(raw, str):
        if raw is None or pd.isna(raw):
            return None
        raw = str(raw)
    s = raw.strip().rstrip("*").strip()
    return s or None


def load_positions(csv_path: str | Path) -> pd.DataFrame:
    """Load and clean a Fidelity positions export.

    Returns one row per holding with tidy column names, numeric money/percent
    columns, a cleaned ``symbol``, and an ``is_cash`` flag. Disclaimer/footer
    rows are dropped.
    """
    csv_path = Path(csv_path)
    # Fidelity data rows carry a trailing comma -> one more field than the header.
    # Without index_col=False, pandas silently promotes the first column to the
    # index and shifts every column left by one. Pin it off.
    df = pd.read_csv(
        csv_path, dtype="string", skip_blank_lines=True, index_col=False
    )
    df = df.rename(columns=_COLUMN_RENAMES)

    # Drop the phantom trailing column and any other fully-unnamed columns.
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]

    # Clean symbols; rows without a real symbol are footers/disclaimers.
    df["symbol"] = df["symbol_raw"].map(clean_symbol)
    df = df[df["symbol"].notna()].copy()

    # Numeric coercions.
    for col in _MONEY_COLUMNS:
        if col in df.columns:
            df[col] = _parse_money(df[col])
    for col in _PERCENT_COLUMNS:
        if col in df.columns:
            df[col] = _parse_percent(df[col])
    if "quantity" in df.columns:
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").astype("float64")

    # Cash / money-market rows have no share quantity.
    df["is_cash"] = df["quantity"].isna()

    # Broker tag — keeps the schema uniform once other brokers (e.g. Robinhood)
    # feed into the same pipeline.
    df["broker"] = "fidelity"

    df = df[[c for c in POSITIONS_SCHEMA if c in df.columns]].reset_index(drop=True)
    return df
