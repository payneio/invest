"""Portfolio analytics over the resolved positions + price history.

All functions take the tidy frames produced by the pipeline and return small,
plot-ready DataFrames/Series. No I/O here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Allocation & concentration
# --------------------------------------------------------------------------- #
def allocation_by(positions: pd.DataFrame, column: str) -> pd.DataFrame:
    """Total market value and weight grouped by ``column`` (e.g. asset_class)."""
    g = (
        positions.groupby(column)["market_value"]
        .sum()
        .sort_values(ascending=False)
        .to_frame("market_value")
    )
    total = g["market_value"].sum()
    g["weight"] = g["market_value"] / total if total else np.nan
    return g


def top_holdings(positions: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Largest positions by market value, aggregated across accounts per symbol."""
    g = (
        positions.groupby(["symbol", "description"], dropna=False)["market_value"]
        .sum()
        .sort_values(ascending=False)
        .to_frame("market_value")
    )
    total = g["market_value"].sum()
    g["weight"] = g["market_value"] / total if total else np.nan
    return g.head(n)


def concentration(positions: pd.DataFrame) -> dict[str, float]:
    """Concentration metrics: HHI plus top-1/top-5 weight share.

    HHI is the sum of squared symbol weights (1.0 = a single holding; lower is
    more diversified). Computed per-symbol across all accounts.
    """
    by_symbol = positions.groupby("symbol")["market_value"].sum()
    total = by_symbol.sum()
    if not total:
        return {"hhi": np.nan, "top1_weight": np.nan, "top5_weight": np.nan}
    weights = (by_symbol / total).sort_values(ascending=False)
    return {
        "hhi": float((weights**2).sum()),
        "top1_weight": float(weights.iloc[0]),
        "top5_weight": float(weights.head(5).sum()),
        "effective_holdings": float(1.0 / (weights**2).sum()),
    }


# --------------------------------------------------------------------------- #
# Performance approximation & risk
# --------------------------------------------------------------------------- #
def _has(history: pd.DataFrame, ticker: object) -> bool:
    return (
        isinstance(ticker, str)
        and history is not None
        and not history.empty
        and ticker in history.columns
    )


def _holding_value_series(row: pd.Series, history: pd.DataFrame) -> pd.Series:
    """Per-holding value over the history window, by best available source.

    live  : quantity * actual price history (yf_symbol)
    proxy : current market value reshaped by a proxy index's path (yf_proxy)
    flat  : held constant at current market value (no public history)

    Series are forward/back-filled across the window so a holding that started
    trading mid-window doesn't punch an artificial hole in the total.
    """
    idx = history.index
    mv = row.get("market_value")

    if _has(history, row.get("yf_symbol")):
        s = history[row["yf_symbol"]] * row["quantity"]
    elif _has(history, row.get("yf_proxy")):
        proxy = history[row["yf_proxy"]]
        latest = proxy.ffill().iloc[-1]
        s = proxy / latest * mv if latest else pd.Series(mv, index=idx)
    else:
        s = pd.Series(mv, index=idx)

    return s.reindex(idx).ffill().bfill()


def holding_value_treatment(positions: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """How each holding's history is sourced: 'live', 'proxy', or 'flat'.

    Lets the caller show exactly how much of the portfolio's value path is real
    vs. approximated vs. held constant.
    """
    def _t(row) -> str:
        if _has(history, row.get("yf_symbol")):
            return "live"
        if _has(history, row.get("yf_proxy")):
            return "proxy"
        return "flat"

    out = positions[["symbol", "asset_class", "market_value"]].copy()
    out["treatment"] = positions.apply(_t, axis=1)
    return out


def portfolio_value_history(
    positions: pd.DataFrame, history: pd.DataFrame, by: str = "asset_class"
) -> pd.DataFrame:
    """Value of TODAY's holdings over the history window, grouped by ``by``.

    Includes EVERY holding (cash and proprietary funds held flat; index-tracking
    holdings proxied). NOT true performance — it holds current share counts fixed
    and ignores past trades. A current-risk / composition lens. Returns a wide
    DataFrame (date x group); ``.sum(axis=1)`` is total portfolio value.
    """
    if history is None or history.empty:
        return pd.DataFrame()

    groups: dict[str, pd.Series] = {}
    for _, row in positions.iterrows():
        key = row.get(by, "unclassified")
        s = _holding_value_series(row, history)
        groups[key] = groups.get(key, pd.Series(0.0, index=history.index)).add(
            s, fill_value=0.0
        )
    return pd.DataFrame(groups)


def current_holdings_value_history(
    positions: pd.DataFrame, history: pd.DataFrame
) -> pd.Series:
    """Total value of today's holdings over the window (all assets included).

    Thin wrapper over :func:`portfolio_value_history` returning just the total.
    """
    by_group = portfolio_value_history(positions, history)
    if by_group.empty:
        return pd.Series(dtype="float64")
    return by_group.sum(axis=1)


def risk_summary(history: pd.DataFrame) -> pd.DataFrame:
    """Annualized return, volatility, and max drawdown per column of ``history``."""
    if history is None or history.empty:
        return pd.DataFrame()
    returns = history.pct_change().dropna(how="all")
    summary = pd.DataFrame(
        {
            "annual_return": returns.mean() * TRADING_DAYS,
            "annual_vol": returns.std() * np.sqrt(TRADING_DAYS),
            "max_drawdown": (history / history.cummax() - 1).min(),
        }
    )
    summary["sharpe_naive"] = summary["annual_return"] / summary["annual_vol"]
    return summary


def account_summary(positions: pd.DataFrame) -> pd.DataFrame:
    """Market value, cost basis, and gain per account."""
    g = positions.groupby("account_name").agg(
        market_value=("market_value", "sum"),
        # min_count=1 so an account with NO cost basis (e.g. a manual Robinhood
        # holdings list) stays NaN instead of summing to a misleading 0.
        cost_basis=("cost_basis", lambda s: s.sum(min_count=1)),
    )
    g["unrealized_gain"] = g["market_value"] - g["cost_basis"]
    g["return_pct"] = np.where(
        g["cost_basis"] > 0, g["unrealized_gain"] / g["cost_basis"] * 100, np.nan
    )
    return g.sort_values("market_value", ascending=False)
