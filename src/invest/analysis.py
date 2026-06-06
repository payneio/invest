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
    p = positions.copy()
    p["symbol"] = p["symbol"].fillna("CASH")  # cash has no ticker — label it
    g = (
        p.groupby(["symbol", "description"], dropna=False)["market_value"]
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


# --------------------------------------------------------------------------- #
# Transaction-ledger analytics (operate on transactions.parquet)
# --------------------------------------------------------------------------- #
# These take the tidy ledger and return small frames — nothing is materialized
# into the pipeline, so you can slice the same ledger any way you like.

def income_by_period(
    transactions: pd.DataFrame,
    *,
    freq: str = "M",
    types: tuple[str, ...] = ("dividend", "interest"),
) -> pd.DataFrame:
    """Income (positive cash) by period and type. ``freq`` is a Period alias (M/Q/Y)."""
    df = transactions[transactions["type"].isin(types)].dropna(subset=["date"]).copy()
    df = df[df["amount"] > 0]
    if df.empty:
        return pd.DataFrame()
    g = (
        df.assign(period=df["date"].dt.to_period(freq))
        .groupby(["period", "type"])["amount"]
        .sum()
        .unstack("type")
        .fillna(0.0)
    )
    g["total"] = g.sum(axis=1)
    return g


def cash_flows(transactions: pd.DataFrame, *, freq: str = "M") -> pd.DataFrame:
    """Net external cash flows (transfers + contributions) by period.

    These are the deposits/withdrawals you'd feed a money-weighted-return calc.
    """
    ext = ("transfer_in", "transfer_out", "contribution")
    df = transactions[transactions["type"].isin(ext)].dropna(subset=["date"]).copy()
    if df.empty:
        return pd.DataFrame()
    return (
        df.assign(period=df["date"].dt.to_period(freq))
        .groupby("period")["amount"]
        .sum()
        .to_frame("net_flow")
    )


def fees_paid(transactions: pd.DataFrame, *, freq: str = "Y") -> pd.Series:
    """Total fees by period (rows carrying a non-zero ``fees``)."""
    df = transactions.dropna(subset=["date"]).copy()
    df = df[df["fees"].fillna(0) != 0]
    if df.empty:
        return pd.Series(dtype="float64")
    return df.assign(period=df["date"].dt.to_period(freq)).groupby("period")["fees"].sum()


def contributions_summary(transactions: pd.DataFrame) -> pd.DataFrame:
    """Total contributions by account (useful for the 401(k)/IRA accounts)."""
    df = transactions[transactions["type"] == "contribution"]
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby("account_name")["amount"]
        .agg(total_contributed="sum", events="count")
        .sort_values("total_contributed", ascending=False)
    )


def dividend_calendar(transactions: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol dividend totals and payment counts (positive dividend rows only)."""
    df = transactions[(transactions["type"] == "dividend") & (transactions["amount"] > 0)]
    if df.empty:
        return pd.DataFrame()
    return (
        df.groupby("symbol")["amount"]
        .agg(total_dividends="sum", payments="count")
        .sort_values("total_dividends", ascending=False)
    )


# --------------------------------------------------------------------------- #
# Returns & price panels — the bridge from holdings to a returns matrix
# --------------------------------------------------------------------------- #
# Most "coveted" analytics (correlation, beta, risk contribution, Monte Carlo)
# need a returns matrix aligned to your *holdings*. These helpers build it by
# resolving each holding to its best available price series — its own ticker
# (``yf_symbol``) where one exists, else a proxy index (``yf_proxy``) for
# proprietary funds — so target-date / index funds aren't silently dropped.

def _price_ticker(row: pd.Series, history: pd.DataFrame) -> str | None:
    """The history column to use for a holding: its own ticker, else a proxy."""
    for col in ("yf_symbol", "yf_proxy"):
        t = row.get(col)
        if isinstance(t, str) and t in history.columns:
            return t
    return None


def holding_price_panel(positions: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Wide ``date × symbol`` price panel for non-cash holdings.

    One column per portfolio ``symbol`` (deduped across accounts), valued by the
    holding's own ticker where available, else its proxy index. Holdings with no
    usable price series are omitted (their value is constant anyway).
    """
    cols: dict[str, pd.Series] = {}
    for _, row in positions[~positions["is_cash"]].iterrows():
        sym = row["symbol"]
        if sym in cols:
            continue
        t = _price_ticker(row, history)
        if t is not None:
            cols[sym] = history[t]
    return pd.DataFrame(cols)


def holding_returns(positions: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Daily simple returns for each held symbol (via :func:`holding_price_panel`)."""
    panel = holding_price_panel(positions, history)
    return panel.pct_change().dropna(how="all") if not panel.empty else pd.DataFrame()


def portfolio_weights(positions: pd.DataFrame, *, include_cash: bool = False) -> pd.Series:
    """Current market-value weights per symbol (aggregated across accounts)."""
    p = positions if include_cash else positions[~positions["is_cash"]]
    w = p.groupby("symbol")["market_value"].sum()
    total = w.sum()
    return (w / total).sort_values(ascending=False) if total else w


def portfolio_return_series(positions: pd.DataFrame, history: pd.DataFrame) -> pd.Series:
    """Daily return of TODAY's holdings held flat over the window.

    Built from :func:`portfolio_value_history` so it includes proxied funds; a
    current-composition lens (it does not reflect when you actually traded).
    """
    pvh = portfolio_value_history(positions, history)
    if pvh.empty:
        return pd.Series(dtype="float64")
    return pvh.sum(axis=1).pct_change().dropna()


def growth_of_dollar(history: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Each ticker's cumulative growth of $1 over the (common) window, base = 1.0."""
    cols = [t for t in tickers if t in history.columns]
    sub = history[cols].dropna(how="all").ffill().dropna()
    return sub / sub.iloc[0] if not sub.empty else pd.DataFrame()


# --------------------------------------------------------------------------- #
# Rolling risk
# --------------------------------------------------------------------------- #
def rolling_volatility(returns, *, window: int = 63) -> pd.Series | pd.DataFrame:
    """Annualized rolling volatility (default ~one quarter window)."""
    return returns.rolling(window).std() * np.sqrt(TRADING_DAYS)


def rolling_beta(asset_ret: pd.Series, market_ret: pd.Series, *, window: int = 63) -> pd.Series:
    """Rolling beta of ``asset_ret`` to ``market_ret`` (cov / market var)."""
    df = pd.concat({"a": asset_ret, "m": market_ret}, axis=1).dropna()
    cov = df["a"].rolling(window).cov(df["m"])
    var = df["m"].rolling(window).var()
    return (cov / var).dropna()


def drawdown(value: pd.Series) -> pd.Series:
    """Drawdown series: value / running-peak − 1 (≤ 0). Feed an equity/value curve."""
    return value / value.cummax() - 1.0


# --------------------------------------------------------------------------- #
# Concentration & diversification (operate on a weights Series / returns matrix)
# --------------------------------------------------------------------------- #
def concentration_metrics(weights: pd.Series) -> dict[str, float]:
    """HHI, effective holdings, and top-1/top-5 weight from an arbitrary weight
    vector — the reusable core behind :func:`concentration`, also handy for a
    point-in-time concentration *time series*."""
    w = weights[weights > 0]
    total = w.sum()
    if not total:
        return {"hhi": np.nan, "effective_holdings": np.nan,
                "top1_weight": np.nan, "top5_weight": np.nan}
    w = (w / total).sort_values(ascending=False)
    hhi = float((w**2).sum())
    return {
        "hhi": hhi,
        "effective_holdings": float(1.0 / hhi) if hhi else np.nan,
        "top1_weight": float(w.iloc[0]),
        "top5_weight": float(w.head(5).sum()),
    }


def risk_contributions(weights: pd.Series, returns: pd.DataFrame) -> pd.DataFrame:
    """Each holding's share of total portfolio *variance* vs its capital weight.

    The coveted "where does my risk actually come from" view: a small high-vol,
    high-correlation position can contribute far more risk than its weight. Uses
    the annualized sample covariance of the supplied returns.
    """
    syms = [s for s in weights.index if s in returns.columns]
    w = weights[syms]
    w = w / w.sum()
    cov = returns[syms].cov() * TRADING_DAYS
    port_var = float(w.values @ cov.values @ w.values)
    comp = w.values * (cov.values @ w.values)  # component contribution to variance
    out = pd.DataFrame({
        "weight": w,
        "vol": np.sqrt(np.diag(cov.values)),
        "risk_contribution": comp / port_var if port_var else np.nan,
    }, index=syms)
    return out.sort_values("risk_contribution", ascending=False)


def diversification_ratio(weights: pd.Series, returns: pd.DataFrame) -> float:
    """Weighted-average holding vol ÷ portfolio vol. 1.0 = no diversification
    benefit; higher means correlations are working for you."""
    syms = [s for s in weights.index if s in returns.columns]
    w = weights[syms]
    w = w / w.sum()
    cov = returns[syms].cov() * TRADING_DAYS
    vols = np.sqrt(np.diag(cov.values))
    port_vol = float(np.sqrt(w.values @ cov.values @ w.values))
    return float((w.values @ vols) / port_vol) if port_vol else np.nan


def correlation_ordered(returns: pd.DataFrame) -> pd.DataFrame:
    """Correlation matrix with rows/cols greedily ordered so similar holdings sit
    together (a cheap seriation, no SciPy) — makes the heatmap's blocks pop."""
    corr = returns.corr()
    if corr.empty:
        return corr
    remaining = list(corr.columns)
    order = [remaining.pop(0)]
    while remaining:
        last = order[-1]
        nxt = max(remaining, key=lambda c: corr.loc[last, c])
        order.append(nxt)
        remaining.remove(nxt)
    return corr.loc[order, order]


# --------------------------------------------------------------------------- #
# Monte Carlo projection
# --------------------------------------------------------------------------- #
def monte_carlo_paths(
    daily_returns: pd.Series,
    *,
    value0: float,
    horizon_days: int = TRADING_DAYS * 5,
    n_sims: int = 1000,
    method: str = "bootstrap",
    seed: int | None = 42,
) -> pd.DataFrame:
    """Simulate forward portfolio value. ``bootstrap`` resamples your own daily
    returns (keeps fat tails); ``normal`` draws from a fitted Gaussian. Returns a
    ``(horizon_days+1) × n_sims`` frame of value paths, all starting at ``value0``."""
    r = np.asarray(daily_returns.dropna(), dtype="float64")
    rng = np.random.default_rng(seed)
    if method == "normal":
        draws = rng.normal(r.mean(), r.std(), size=(horizon_days, n_sims))
    else:
        draws = rng.choice(r, size=(horizon_days, n_sims), replace=True)
    paths = value0 * np.cumprod(1.0 + draws, axis=0)
    paths = np.vstack([np.full((1, n_sims), value0), paths])
    return pd.DataFrame(paths)


def projection_bands(paths: pd.DataFrame, quantiles=(0.05, 0.25, 0.5, 0.75, 0.95)) -> pd.DataFrame:
    """Percentile fan (one column per quantile) across simulated paths by step."""
    band = paths.quantile(list(quantiles), axis=1).T
    band.columns = [f"p{int(q * 100)}" for q in quantiles]
    return band


# --------------------------------------------------------------------------- #
# Dividend / income shaping for heatmaps
# --------------------------------------------------------------------------- #
def dividend_month_year(transactions: pd.DataFrame) -> pd.DataFrame:
    """Dividend income pivoted ``year × month`` (1–12) — the calendar heatmap source."""
    df = transactions[(transactions["type"] == "dividend") & (transactions["amount"] > 0)]
    df = df.dropna(subset=["date"])
    if df.empty:
        return pd.DataFrame()
    df = df.assign(year=df["date"].dt.year, month=df["date"].dt.month)
    piv = df.pivot_table(index="year", columns="month", values="amount",
                         aggfunc="sum", fill_value=0.0)
    return piv.reindex(columns=range(1, 13), fill_value=0.0).sort_index()


def trailing_yield(
    transactions: pd.DataFrame, positions: pd.DataFrame, *, months: int = 12
) -> pd.DataFrame:
    """Trailing-``months`` dividends per symbol vs current market value → an
    estimated forward yield. Income holdings only (rows that actually paid)."""
    div = transactions[(transactions["type"] == "dividend") & (transactions["amount"] > 0)]
    div = div.dropna(subset=["date"])
    if div.empty:
        return pd.DataFrame()
    cutoff = div["date"].max() - pd.DateOffset(months=months)
    recent = div[div["date"] >= cutoff].groupby("symbol")["amount"].sum()
    mv = positions[~positions["is_cash"]].groupby("symbol")["market_value"].sum()
    out = pd.DataFrame({"ttm_income": recent, "market_value": mv}).dropna(subset=["ttm_income"])
    out["yield_pct"] = np.where(out["market_value"] > 0,
                                out["ttm_income"] / out["market_value"] * 100, np.nan)
    return out.sort_values("ttm_income", ascending=False)
