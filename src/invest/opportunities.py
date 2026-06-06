"""Opportunity finders — decision-support signals computed from the derived data.

These are **candidate generators**, not advice: they surface lots, accounts, tilts and
costs worth a human look, each tied to a dollar figure. Four families:

* **Tax-loss harvesting** — taxable lots underwater, with holding term + wash-sale flags.
* **Asset location** — tax-inefficient holdings sitting in taxable accounts; the
  asset-class × tax-treatment map; idle vs. earning cash.
* **Concentration / rebalancing** — drift from a target you set, and a risk-reduction
  ranking (which trims cut the most portfolio variance per dollar).
* **Cost / performance leakage** — fund expense drag and an active-vs-index gap.

Everything takes the tidy frames the pipeline already produces (positions, lots,
transactions, price history) plus the account metadata from ``mapping.load_account_meta``.
Pure functions, frame-in / frame-out — no I/O, no plotting.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from invest import analysis, broker, mapping

# Asset classes whose distributions are taxed as ordinary income — inefficient to
# hold in a taxable account (better sheltered in an IRA/401k/HSA).
TAX_INEFFICIENT: frozenset[str] = frozenset({"bdc", "bond", "reit", "high_yield", "preferred"})

LONG_TERM_DAYS = 365  # holding period beyond which US gains are long-term

# Default marginal tax rates for the lot-sale planner (override per your bracket).
# Long-term ≈ 15–20% cap-gains + 3.8% NIIT; short-term ≈ your ordinary rate.
DEFAULT_LT_RATE = 0.238
DEFAULT_ST_RATE = 0.37

# A sensible default benchmark per asset class for the active-vs-index gap.
ASSET_BENCHMARK: dict[str, str] = {
    "us_equity": "SPY",
    "employer_stock": "SPY",
    "us_equity_fund": "QQQ",       # growth-tilted active funds vs the Nasdaq-100
    "us_equity_index": "VOO",      # should track ~exactly (sanity check)
    "target_date": "VTI",          # broad-market yardstick for a glide-path fund
    "bdc": "SPY",                  # imperfect — no clean BDC index in the panel
}


def _cash_symbols() -> set[str]:
    """Money-market / sweep symbols across brokers, plus the derived cash label."""
    return {s for b in broker.BROKERS for s in b.cash_symbols} | {"CASH"}


def attach_account_tax(df: pd.DataFrame, account_meta: dict[str, dict]) -> pd.DataFrame:
    """Add ``tax_treatment`` and ``account_type`` columns, joined onto ``account_name``
    via :func:`mapping.normalize_account` (so CamelCase ledger names match the YAML)."""
    norm = df["account_name"].map(mapping.normalize_account)
    out = df.copy()
    out["tax_treatment"] = norm.map(lambda k: account_meta.get(k, {}).get("tax", "unknown"))
    out["account_type"] = norm.map(lambda k: account_meta.get(k, {}).get("type", "unknown"))
    return out


# --------------------------------------------------------------------------- #
# 1. Tax-loss harvesting
# --------------------------------------------------------------------------- #
def price_lots(lots: pd.DataFrame, positions: pd.DataFrame) -> pd.DataFrame:
    """Add current price, market value and unrealized P/L to a per-lot frame, using
    the per-symbol current price from the enriched positions."""
    px = positions.dropna(subset=["price"]).groupby("symbol")["price"].first()
    df = lots.copy()
    df["price"] = df["symbol"].map(px)
    df["market_value"] = df["quantity"] * df["price"]
    df["unrealized"] = df["market_value"] - df["cost_basis"]
    df["unrealized_pct"] = np.where(df["cost_basis"] > 0, df["unrealized"] / df["cost_basis"], np.nan)
    return df


def harvest_candidates(
    lots_priced: pd.DataFrame, account_meta: dict[str, dict], *,
    today: object = None, min_loss: float = 0.0,
) -> pd.DataFrame:
    """Taxable lots sitting at an unrealized loss — tax-loss-harvest candidates.

    Each row carries the loss, the holding ``term`` (short/long) and ``days_held``;
    a realized loss offsets gains of the same term first, so term matters.
    """
    today = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    df = attach_account_tax(lots_priced, account_meta)
    df = df[(df["tax_treatment"] == "taxable") & (df["unrealized"] < -abs(min_loss))].copy()
    if df.empty:
        return df
    df["days_held"] = (today - df["acquired"]).dt.days
    df["term"] = np.where(df["days_held"] > LONG_TERM_DAYS, "long", "short")
    return df.sort_values("unrealized")


def realized_gains_ytd(
    realized: pd.DataFrame, account_meta: dict[str, dict], *, year: int,
) -> pd.DataFrame:
    """Realized P/L booked in taxable accounts during ``year`` — the pool a harvested
    loss would offset. (Term not split: beancount's realized postings don't retain the
    closed lot's acquisition date.)"""
    df = attach_account_tax(realized, account_meta)
    return df[(df["tax_treatment"] == "taxable") & (df["date"].dt.year == year)]


def wash_sale_risk(
    transactions: pd.DataFrame, symbols, *, today: object = None, window: int = 30,
) -> pd.DataFrame:
    """Recent purchases (within ``window`` days) of each candidate symbol — a sale at a
    loss is disallowed if you bought the same security within ±30 days. We can only see
    the *before* side today; flag those so you don't harvest into a wash sale."""
    today = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    buys = transactions[transactions["type"].isin(["buy", "crypto_buy"])].dropna(subset=["date"])
    recent = buys[(buys["date"] >= today - pd.Timedelta(days=window)) & (buys["symbol"].isin(set(symbols)))]
    if recent.empty:
        return pd.DataFrame(columns=["last_buy", "buys_in_window"])
    return recent.groupby("symbol")["date"].agg(last_buy="max", buys_in_window="count")


# --------------------------------------------------------------------------- #
# 2. Asset location & cash
# --------------------------------------------------------------------------- #
def location_matrix(positions: pd.DataFrame, account_meta: dict[str, dict]) -> pd.DataFrame:
    """Market value pivot: ``asset_class`` (rows) × ``tax_treatment`` (columns)."""
    df = attach_account_tax(positions, account_meta)
    return df.pivot_table(index="asset_class", columns="tax_treatment",
                          values="market_value", aggfunc="sum", fill_value=0.0)


def location_flags(
    positions: pd.DataFrame, account_meta: dict[str, dict],
    transactions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Tax-inefficient holdings (BDC/bond/REIT/…) held in *taxable* accounts — the
    misplacements. With ``transactions`` it estimates the annual taxable income each
    throws off (trailing 12-month distributions)."""
    df = attach_account_tax(positions[~positions["is_cash"]], account_meta)
    flagged = df[(df["tax_treatment"] == "taxable") & (df["asset_class"].isin(TAX_INEFFICIENT))].copy()
    if transactions is not None and not flagged.empty:
        ty = analysis.trailing_yield(transactions, positions)
        flagged["ttm_income"] = flagged["symbol"].map(ty["ttm_income"]) if not ty.empty else np.nan
    return flagged.sort_values("market_value", ascending=False)


def cash_audit(
    positions: pd.DataFrame, transactions: pd.DataFrame, account_meta: dict[str, dict],
    *, months: int = 12,
) -> pd.DataFrame:
    """Cash by account with its trailing income and *implied yield* — the lever for
    spotting idle balances. Cash earning ~0% on a large balance is the opportunity;
    money-market sweeps already yielding ~4–5% are fine."""
    cash = attach_account_tax(positions[positions["is_cash"]], account_meta)
    bal = cash.groupby(["account_name", "tax_treatment"])["market_value"].sum().reset_index()
    tx = transactions.dropna(subset=["date"])
    cutoff = tx["date"].max() - pd.DateOffset(months=months)
    cash_syms = _cash_symbols()
    inc = tx[(tx["date"] >= cutoff) & (
        (tx["type"] == "interest") | ((tx["type"] == "dividend") & tx["symbol"].isin(cash_syms)))]
    by_acct = inc.groupby("account_name")["amount"].sum()
    bal["ttm_income"] = bal["account_name"].map(by_acct).fillna(0.0)
    bal["implied_yield"] = np.where(bal["market_value"] > 0, bal["ttm_income"] / bal["market_value"], np.nan)
    return bal.sort_values("market_value", ascending=False)


# --------------------------------------------------------------------------- #
# 3. Concentration / rebalancing
# --------------------------------------------------------------------------- #
def rebalance_plan(weights: pd.Series, targets: dict | pd.Series, *, total: float) -> pd.DataFrame:
    """Drift from a target weighting and the dollar trade to close it.

    ``weights``/``targets`` are fractions over the same labels (symbols or asset
    classes). ``trade`` is signed dollars: positive = buy, negative = sell/trim.
    """
    tgt = pd.Series(targets, dtype="float64")
    idx = weights.index.union(tgt.index)
    cur = weights.reindex(idx).fillna(0.0)
    tgt = tgt.reindex(idx).fillna(0.0)
    out = pd.DataFrame({"current": cur, "target": tgt})
    out["drift"] = out["current"] - out["target"]
    out["trade"] = -out["drift"] * total
    return out.sort_values("drift")


def risk_reduction_ranking(weights: pd.Series, returns: pd.DataFrame) -> pd.DataFrame:
    """Per holding: capital weight, share of portfolio variance, and the *excess* of
    risk over weight — the trim guide. Names where risk ≫ weight reduce portfolio
    volatility the most per dollar sold."""
    rc = analysis.risk_contributions(weights, returns)
    rc["risk_vs_weight"] = rc["risk_contribution"] - rc["weight"]
    rc["risk_per_weight"] = rc["risk_contribution"] / rc["weight"].replace(0, np.nan)
    return rc.sort_values("risk_vs_weight", ascending=False)


# --------------------------------------------------------------------------- #
# 4. Cost / performance leakage
# --------------------------------------------------------------------------- #
def expense_drag(positions: pd.DataFrame) -> pd.DataFrame:
    """Annual fund-fee dollars per holding (market value × expense ratio). Individual
    stocks carry no fee and drop out. Source the ratios from symbol_map.yaml."""
    df = positions[~positions["is_cash"]].copy()
    df["expense_ratio"] = pd.to_numeric(df["expense_ratio"], errors="coerce").fillna(0.0)
    g = df.groupby(["symbol", "description"]).agg(
        market_value=("market_value", "sum"), expense_ratio=("expense_ratio", "first"))
    g["annual_cost"] = g["market_value"] * g["expense_ratio"]
    return g[g["annual_cost"] > 0].sort_values("annual_cost", ascending=False)


# --------------------------------------------------------------------------- #
# 5. Specific-lot sell optimization — raise cash at minimum tax
# --------------------------------------------------------------------------- #
def plan_lot_sale(
    lots_priced: pd.DataFrame, account_meta: dict[str, dict], *,
    raise_amount: float, symbol: str | None = None, taxable_only: bool = True,
    lt_rate: float = DEFAULT_LT_RATE, st_rate: float = DEFAULT_ST_RATE,
    today: object = None, strategy: str = "min_tax",
) -> pd.DataFrame:
    """Choose which tax lots to sell to raise ``raise_amount`` of cash, ordered by a
    ``strategy``. With divisible (fractional-share) lots the greedy ``min_tax`` order
    — least tax per dollar of proceeds, i.e. losses first — is **optimal**.

    Strategies: ``min_tax`` (tax-efficient), ``hifo`` (highest cost first),
    ``fifo`` (oldest first), ``max_gain`` (worst case, for comparison). Returns the
    selected lots with ``sell_qty`` / ``proceeds`` / ``sell_gain`` / ``sell_tax`` and a
    running ``cum_proceeds``; the final lot is partially filled to hit the target.
    """
    today = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    df = attach_account_tax(lots_priced, account_meta).copy()
    df = df.dropna(subset=["market_value", "cost_basis"])
    if taxable_only:
        df = df[df["tax_treatment"] == "taxable"]
    if symbol is not None:
        df = df[df["symbol"] == symbol]
    df = df[df["market_value"] > 0].copy()
    if df.empty:
        return df

    df["days_held"] = (today - df["acquired"]).dt.days
    df["term"] = np.where(df["days_held"] > LONG_TERM_DAYS, "long", "short")
    df["gain"] = df["market_value"] - df["cost_basis"]
    df["rate"] = np.where(df["term"] == "long", lt_rate, st_rate)
    df["tax"] = df["gain"] * df["rate"]
    df["tax_per_dollar"] = df["tax"] / df["market_value"]
    df["basis_estimated"] = df["unit_cost"].fillna(0) <= 0  # placeholder/opening basis

    order = {
        "min_tax": ("tax_per_dollar", True),
        "max_gain": ("tax_per_dollar", False),
        "hifo": ("unit_cost", False),
        "fifo": ("acquired", True),
    }.get(strategy, ("tax_per_dollar", True))
    df = df.sort_values(order[0], ascending=order[1])

    chosen, raised = [], 0.0
    for _, r in df.iterrows():
        if raised >= raise_amount - 1e-6:
            break
        frac = min(1.0, (raise_amount - raised) / r["market_value"])
        row = r.copy()
        row["sell_qty"] = r["quantity"] * frac
        row["proceeds"] = r["market_value"] * frac
        row["sell_gain"] = r["gain"] * frac
        row["sell_tax"] = r["tax"] * frac
        raised += row["proceeds"]
        row["cum_proceeds"] = raised
        chosen.append(row)
    return pd.DataFrame(chosen)


def sale_plan_totals(plan: pd.DataFrame, raise_amount: float) -> dict[str, float]:
    """Headline totals for a sale plan: proceeds, realized gain, tax, effective rate,
    and any ``shortfall`` if the (filtered) lots can't cover the target."""
    if plan.empty:
        return {"proceeds": 0.0, "gain": 0.0, "tax": 0.0,
                "effective_rate": np.nan, "shortfall": float(raise_amount)}
    proceeds = plan["proceeds"].sum()
    tax = plan["sell_tax"].sum()
    shortfall = raise_amount - proceeds
    return {
        "proceeds": float(proceeds), "gain": float(plan["sell_gain"].sum()),
        "tax": float(tax), "effective_rate": float(tax / proceeds) if proceeds else np.nan,
        "shortfall": float(shortfall) if shortfall > 1.0 else 0.0,  # ignore sub-$1 float residue
    }


def compare_sale_strategies(
    lots_priced: pd.DataFrame, account_meta: dict[str, dict], *, raise_amount: float, **kwargs,
) -> pd.DataFrame:
    """Totals for each ordering strategy, so the tax saved vs. naive selling is explicit."""
    rows = {}
    for s in ("min_tax", "hifo", "fifo", "max_gain"):
        plan = plan_lot_sale(lots_priced, account_meta, raise_amount=raise_amount, strategy=s, **kwargs)
        rows[s] = sale_plan_totals(plan, raise_amount)
    out = pd.DataFrame(rows).T
    out["tax_vs_min"] = out["tax"] - out.loc["min_tax", "tax"]
    return out


# --------------------------------------------------------------------------- #
# Headline summary — the one-glance dashboard digest
# --------------------------------------------------------------------------- #
def summary(
    positions: pd.DataFrame, history: pd.DataFrame, transactions: pd.DataFrame,
    entries, account_meta: dict[str, dict], *, today: object = None,
) -> dict:
    """A compact dict of headline numbers across all four finders — the source for the
    refresh notebook's opportunities dashboard."""
    from invest import ledger  # lazy: avoid coupling the library to the ledger layer

    today = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.today().normalize()
    total = float(positions["market_value"].sum())

    lots = price_lots(ledger.lots_dataframe(entries), positions)
    cand = harvest_candidates(lots, account_meta, today=today)
    realized = realized_gains_ytd(ledger.realized_gains_dataframe(entries), account_meta, year=today.year)
    ws = wash_sale_risk(transactions, cand["symbol"].unique() if not cand.empty else [], today=today)

    ca = cash_audit(positions, transactions, account_meta)
    idle = ca[(ca["implied_yield"] < 0.01) & (ca["market_value"] > 5_000)]
    cash_total = float(ca["market_value"].sum())

    ret = analysis.holding_returns(positions, history)
    rr = risk_reduction_ranking(analysis.portfolio_weights(positions), ret)
    sym_w = analysis.portfolio_weights(positions, include_cash=True)

    ed = expense_drag(positions)
    bg = benchmark_gap(positions, history)
    flags = location_flags(positions, account_meta)

    return {
        "harvest_loss": float(cand["unrealized"].sum()) if not cand.empty else 0.0,
        "harvest_lots": int(len(cand)),
        "realized_ytd": float(realized["realized"].sum()) if not realized.empty else 0.0,
        "wash_blocked": list(ws.index) if not ws.empty else [],
        "cash_total": cash_total, "cash_pct": cash_total / total if total else np.nan,
        "idle_cash": float(idle["market_value"].sum()),
        "misplaced_value": float(flags["market_value"].sum()) if not flags.empty else 0.0,
        "top_risk_name": rr.index[0] if not rr.empty else None,
        "top_risk_share": float(rr.iloc[0]["risk_contribution"]) if not rr.empty else np.nan,
        "top_risk_weight": float(rr.iloc[0]["weight"]) if not rr.empty else np.nan,
        "max_name": sym_w.index[0] if not sym_w.empty else None,
        "max_name_weight": float(sym_w.iloc[0]) if not sym_w.empty else np.nan,
        "expense_annual": float(ed["annual_cost"].sum()) if not ed.empty else 0.0,
        "laggards": list(bg[bg["excess"] < 0]["symbol"]) if not bg.empty else [],
    }


def benchmark_gap(
    positions: pd.DataFrame, history: pd.DataFrame, benchmark_map: dict | None = None,
) -> pd.DataFrame:
    """Each priced holding vs its asset-class benchmark over the common window:
    annualized return, tracking error and **excess return** (negative = lagging =
    swap candidate). Proxied funds reflect their proxy's path."""
    benchmark_map = benchmark_map or ASSET_BENCHMARK
    panel = analysis.holding_price_panel(positions, history)
    acls = positions.groupby("symbol")["asset_class"].first()

    def _cagr(r: pd.Series) -> float:
        return (1 + r).prod() ** (analysis.TRADING_DAYS / len(r)) - 1 if len(r) else np.nan

    rows = []
    for sym in panel.columns:
        bench = benchmark_map.get(acls.get(sym))
        if not bench or bench not in history.columns:
            continue
        pair = pd.concat([panel[sym], history[bench]], axis=1).dropna()
        if len(pair) < 30:
            continue
        hr, br = pair.iloc[:, 0].pct_change().dropna(), pair.iloc[:, 1].pct_change().dropna()
        rows.append({
            "symbol": sym, "asset_class": acls.get(sym), "benchmark": bench,
            "holding_cagr": _cagr(hr), "benchmark_cagr": _cagr(br),
            "excess": _cagr(hr) - _cagr(br), "tracking_error": (hr - br).std() * np.sqrt(analysis.TRADING_DAYS),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out["info_ratio"] = out["excess"] / out["tracking_error"]
        out = out.sort_values("excess")
    return out
