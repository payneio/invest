"""yfinance enrichment: current prices and historical close series.

Kept deliberately defensive — Yahoo is an unofficial source that can rate-limit,
return partial frames, or go offline. Every fetch degrades gracefully so the
pipeline still produces positions + Fidelity prices when the network is down.
"""

from __future__ import annotations

import pandas as pd


def _empty_close_frame() -> pd.DataFrame:
    return pd.DataFrame()


def fetch_history(
    symbols: list[str],
    period: str = "3y",
    *,
    progress: bool = False,
) -> pd.DataFrame:
    """Download adjusted close history for ``symbols`` as a wide DataFrame.

    Index is dates; columns are tickers. Returns an empty frame on any failure
    so callers can fall back. ``auto_adjust=True`` folds in splits/dividends.
    """
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return _empty_close_frame()

    try:
        import yfinance as yf

        data = yf.download(
            symbols,
            period=period,
            auto_adjust=True,
            progress=progress,
            group_by="column",
        )
    except Exception as exc:  # network, rate-limit, parse errors, etc.
        print(f"[prices] history fetch failed ({exc!r}); continuing without it.")
        return _empty_close_frame()

    if data is None or len(data) == 0:
        return _empty_close_frame()

    # Single-ticker downloads come back with simple columns; multi-ticker come
    # back MultiIndex (field, ticker). Normalize to a wide Close frame.
    if isinstance(data.columns, pd.MultiIndex):
        if "Close" not in data.columns.get_level_values(0):
            return _empty_close_frame()
        close = data["Close"]
    else:
        if "Close" not in data.columns:
            return _empty_close_frame()
        close = data[["Close"]].rename(columns={"Close": symbols[0]})

    return close.dropna(how="all")


def latest_from_history(history: pd.DataFrame) -> pd.Series:
    """Last available (forward-filled) price per column of a history frame."""
    if history is None or history.empty:
        return pd.Series(dtype="float64")
    return history.ffill().iloc[-1]


def resolve_prices(
    classified: pd.DataFrame,
    yf_latest: pd.Series,
) -> pd.DataFrame:
    """Resolve a single ``price`` per position from its declared price_source.

    Priority by ``price_source``:
      manual       -> ``manual_price`` from the map
      yfinance     -> Yahoo latest for ``yf_symbol`` (fallback: Fidelity last_price)
      fidelity_csv -> Fidelity ``last_price``

    Cash rows (no quantity) keep their Fidelity ``current_value`` directly and get
    a sentinel price of 1.0. Adds ``price`` and ``price_used`` (actual source).
    """
    df = classified.copy()

    def _resolve(row) -> tuple[float | None, str]:
        if row["is_cash"]:
            return 1.0, "cash_value"

        source = row.get("price_source", "fidelity_csv")
        if source == "manual":
            mp = row.get("manual_price")
            if pd.notna(mp):
                return float(mp), "manual"
        elif source == "yfinance":
            yfs = row.get("yf_symbol")
            if yfs and yfs in yf_latest.index and pd.notna(yf_latest[yfs]):
                return float(yf_latest[yfs]), "yfinance"
            # Fall back to Fidelity's quote if Yahoo had nothing.
            if pd.notna(row.get("last_price")):
                return float(row["last_price"]), "fidelity_csv_fallback"

        # Default / fidelity_csv.
        if pd.notna(row.get("last_price")):
            return float(row["last_price"]), "fidelity_csv"
        return None, "unavailable"

    resolved = df.apply(_resolve, axis=1, result_type="expand")
    df["price"] = resolved[0]
    df["price_used"] = resolved[1]

    # Market value: cash uses its Fidelity current_value; everything else qty*price.
    df["market_value"] = df.apply(
        lambda r: r["current_value"]
        if r["is_cash"]
        else (r["quantity"] * r["price"] if pd.notna(r["price"]) else r["current_value"]),
        axis=1,
    )
    return df
