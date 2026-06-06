"""yfinance enrichment: current prices and historical close series.

Kept deliberately defensive — Yahoo is an unofficial source that can rate-limit,
return partial frames, or go offline. Every fetch degrades gracefully so the
pipeline still produces positions + snapshot prices when the network is down.
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
