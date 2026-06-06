"""Pandas bridge over the beancount ledger — the source of truth.

The ledger (``ledger/main.beancount``) is curated and authoritative; this module
loads it and flattens it into the DataFrames the rest of the project speaks:

* :func:`transactions_dataframe` — the ledger as a tidy event table.
* :func:`positions_dataframe` — holdings realized **as of any date** (default today),
  with cost basis. Point-in-time is just an earlier ``date``.
* :func:`enrich_positions` — add classification (``symbol_map.yaml``) + a current
  price/market value (yfinance, with the broker snapshot as fallback for holdings
  with no public quote).
* :func:`reconcile` — derived vs snapshot diff.
* :func:`filter_entries` — drop/keep entries for counterfactuals, then re-derive.

Nothing here mutates the ledger; it only reads and projects it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from beancount import loader
from beancount.core import data, realization

from invest import config, mapping


def load(path: str | Path | None = None):
    """``loader.load_file`` the ledger. Returns ``(entries, errors, options_map)``."""
    return loader.load_file(str(path or config.LEDGER_MAIN))


def commodity_maps(entries) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(commodity -> original_symbol, commodity -> yf_symbol)`` from the
    ``commodity`` directives' metadata (so we can recover real tickers/CUSIPs)."""
    original, yf = {}, {}
    for e in entries:
        if isinstance(e, data.Commodity):
            original[e.currency] = (e.meta or {}).get("original", e.currency)
            if (e.meta or {}).get("yf_symbol"):
                yf[e.currency] = e.meta["yf_symbol"]
    return original, yf


def _broker_account(account: str) -> tuple[str, str]:
    """``Assets:Fidelity:RolloverIRA`` -> ``("fidelity", "RolloverIRA")``."""
    parts = account.split(":")
    broker = parts[1].lower() if len(parts) > 1 else ""
    name = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else account)
    return broker, name


def filter_entries(entries, predicate):
    """Entries for which ``predicate(entry)`` is true — for counterfactuals.

    Non-transaction directives (open/commodity/balance) are always kept so the
    result still realizes; only ``Transaction`` entries are filtered.
    """
    out = []
    for e in entries:
        if isinstance(e, data.Transaction) and not predicate(e):
            continue
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Positions (point-in-time)
# --------------------------------------------------------------------------- #
def positions_dataframe(entries, *, date: object = None) -> pd.DataFrame:
    """Holdings realized as of ``date`` (default: today), one row per
    ``(account, commodity)`` with quantity and cost basis. USD balances become
    ``is_cash`` rows. Pass an earlier ``date`` for point-in-time."""
    if date is not None:
        cutoff = pd.Timestamp(date).date()
        entries = [e for e in entries if e.date <= cutoff]

    original, yf = commodity_maps(entries)
    root = realization.realize(entries)

    rows: list[dict] = []
    for ra in realization.iter_children(root):
        # Brokerage holdings only — skip the synthetic External (transfer counter).
        if not ra.account.startswith("Assets:") or ra.account.startswith("Assets:External"):
            continue
        broker, name = _broker_account(ra.account)
        # Aggregate lots per commodity: total qty and total cost.
        agg: dict[str, list[float]] = {}
        for pos in ra.balance:
            u = pos.units
            qty = float(u.number)
            cost = float(pos.cost.number) * qty if pos.cost else np.nan
            tot = agg.setdefault(u.currency, [0.0, 0.0])
            tot[0] += qty
            tot[1] = (tot[1] + cost) if not np.isnan(cost) else tot[1]
        for currency, (qty, cost_total) in agg.items():
            if abs(qty) < 1e-9:
                continue
            is_cash = currency == "USD"
            rows.append({
                "broker": broker,
                "account_number": name,
                "account_name": name,
                "description": None,
                "commodity": currency,
                "symbol": original.get(currency, currency) if not is_cash else "CASH",
                "yf_symbol_ledger": yf.get(currency),
                "quantity": np.nan if is_cash else qty,
                "cash_value": qty if is_cash else np.nan,
                "cost_basis": np.nan if is_cash else cost_total,
                "avg_cost_basis": np.nan if (is_cash or qty == 0) else cost_total / qty,
                "is_cash": is_cash,
            })
    return pd.DataFrame(rows)


def lots_dataframe(entries, *, date: object = None) -> pd.DataFrame:
    """One row per **tax lot** held as of ``date`` (default today): account, symbol,
    quantity, per-share ``unit_cost``, ``cost_basis`` (qty×cost) and the lot
    ``acquired`` date. The granular view tax-loss harvesting needs — beancount's
    FIFO booking keeps lots distinct, each with its own cost and acquisition date.

    Cash (USD) balances are skipped. Lots with no cost (e.g. opening lots booked at
    ``{}``) carry NaN cost.
    """
    if date is not None:
        cutoff = pd.Timestamp(date).date()
        entries = [e for e in entries if e.date <= cutoff]

    original, _ = commodity_maps(entries)
    root = realization.realize(entries)

    rows: list[dict] = []
    for ra in realization.iter_children(root):
        if not ra.account.startswith("Assets:") or ra.account.startswith("Assets:External"):
            continue
        broker, name = _broker_account(ra.account)
        for pos in ra.balance:
            u = pos.units
            if u.currency == "USD" or abs(float(u.number)) < 1e-9:
                continue
            qty = float(u.number)
            unit_cost = float(pos.cost.number) if pos.cost else np.nan
            rows.append({
                "broker": broker,
                "account_name": name,
                "symbol": original.get(u.currency, u.currency),
                "commodity": u.currency,
                "quantity": qty,
                "unit_cost": unit_cost,
                "cost_basis": unit_cost * qty if not np.isnan(unit_cost) else np.nan,
                "acquired": pd.Timestamp(pos.cost.date) if pos.cost and pos.cost.date else pd.NaT,
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Transactions (the ledger flattened)
# --------------------------------------------------------------------------- #
def transactions_dataframe(entries) -> pd.DataFrame:
    """Flatten the ledger's transactions to a tidy event table (incl. opening lots
    and fixups), recovering ``type/id/source/symbol`` from metadata and the signed
    cash ``amount`` + share ``quantity`` from the brokerage-account postings."""
    rows: list[dict] = []
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        meta = e.meta or {}
        amount = quantity = fees = 0.0
        acct = None
        for p in e.postings:
            if p.account.startswith("Expenses:Fees"):
                fees += float(p.units.number)
                continue
            if not p.account.startswith("Assets:") or p.account.startswith("Assets:External"):
                continue
            acct = acct or p.account
            if p.units.currency == "USD":
                amount += float(p.units.number)
            else:
                quantity += float(p.units.number)
        broker, name = _broker_account(acct) if acct else ("", "")
        rows.append({
            "broker": broker,
            "account_number": name,
            "account_name": name,
            "date": pd.Timestamp(e.date),
            "type": meta.get("txn_type"),
            "symbol": meta.get("symbol"),
            "description": e.narration,
            "quantity": quantity if abs(quantity) > 1e-12 else np.nan,
            "price": np.nan,
            "amount": amount,
            "fees": fees,
            "currency": "USD",
            "source": meta.get("source"),
            "id": meta.get("id"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["broker", "date", "id"], na_position="last").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# Enrichment (classification + current price/value) and reconciliation
# --------------------------------------------------------------------------- #
def enrich_positions(
    positions: pd.DataFrame,
    *,
    smap: mapping.SymbolMap | None = None,
    snapshot: pd.DataFrame | None = None,
    yf_latest: pd.Series | None = None,
) -> pd.DataFrame:
    """Add ``asset_class``/price-source classification and a current ``price`` +
    ``market_value`` to derived positions.

    Pricing is per symbol: yfinance latest where available, else the broker
    snapshot's ``last_price`` (covers proprietary funds with no public quote), else
    a manual price; cash is valued at its USD balance.
    """
    smap = smap or mapping.load_symbol_map()
    df = mapping.classify_positions(positions, smap)
    # Cash carries the "CASH" label, not a real ticker — bucket it and mark it known
    # so the classifier's defaults don't flag it.
    df.loc[df["is_cash"], "asset_class"] = "cash"
    df.loc[df["is_cash"], "unclassified"] = False

    # symbol -> current price and human description, from the snapshot.
    snap_price: dict[str, float] = {}
    snap_desc: dict[str, str] = {}
    if snapshot is not None:
        for r in snapshot.to_dict("records"):
            s, lp, d = r.get("symbol"), r.get("last_price"), r.get("description")
            if s and lp is not None and not pd.isna(lp):
                snap_price[str(s)] = float(lp)
            if s and d is not None and not pd.isna(d):
                snap_desc[str(s)] = str(d)

    def price_for(row) -> tuple[float, str]:
        if row["is_cash"]:
            return 1.0, "cash"
        if row.get("price_source") == "manual" and not pd.isna(row.get("manual_price")):
            return float(row["manual_price"]), "manual"
        yfs = row.get("yf_symbol")
        if yf_latest is not None and yfs and yfs in yf_latest.index and not pd.isna(yf_latest[yfs]):
            return float(yf_latest[yfs]), "yfinance"
        if row["symbol"] in snap_price:
            return snap_price[row["symbol"]], "snapshot"
        return np.nan, "unavailable"

    prices_used = df.apply(price_for, axis=1, result_type="expand")
    df["price"] = prices_used[0]
    df["price_used"] = prices_used[1]
    df["market_value"] = np.where(
        df["is_cash"], df["cash_value"], df["quantity"] * df["price"]
    )
    # Human descriptions, so tables don't show NaN: prefer the symbol map's friendly
    # ``name`` (e.g. for CUSIP-only funds), then the snapshot's terse description.
    smap_name = {s: e["name"] for s, e in smap.symbols.items() if e.get("name")}
    df["description"] = df["symbol"].map(smap_name).fillna(df["symbol"].map(snap_desc))
    df.loc[df["is_cash"], "description"] = "Cash & money market"
    # Cash's "cost" is its face value, so account return comes out at 0 (not NaN).
    df.loc[df["is_cash"], "cost_basis"] = df.loc[df["is_cash"], "cash_value"]
    return df


def realized_gains_dataframe(entries) -> pd.DataFrame:
    """Realized P&L per sale, straight from beancount's ``Income:RealizedPnL``
    postings — which already did the FIFO lot matching and cost basis (including
    opening lots). One row per sale: ``realized`` (+gain / −loss)."""
    original, _ = commodity_maps(entries)
    rows: list[dict] = []
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        pnl = sum(float(p.units.number) for p in e.postings
                  if p.account.startswith("Income:RealizedPnL"))
        if abs(pnl) < 1e-9:
            continue
        sym = acct = None
        for p in e.postings:
            if p.account.startswith("Assets:") and p.units.currency != "USD" \
                    and float(p.units.number) < 0:
                sym = original.get(p.units.currency, p.units.currency)
                acct = p.account
        broker, name = _broker_account(acct) if acct else ("", "")
        rows.append({"broker": broker, "account_name": name, "date": pd.Timestamp(e.date),
                     "symbol": sym, "realized": -pnl})
    return pd.DataFrame(rows)


def reconcile(derived: pd.DataFrame, snapshot: pd.DataFrame) -> pd.DataFrame:
    """Per-(broker, symbol) derived vs snapshot share quantities, with a ``match``
    flag — the audit that the curated ledger reproduces the broker snapshot. Keyed
    by broker so the same ticker in two brokers isn't conflated."""
    d = derived[~derived["is_cash"]].groupby(["broker", "symbol"])["quantity"].sum()
    s = snapshot[snapshot["quantity"].notna()].groupby(["broker", "symbol"])["quantity"].sum()
    out = pd.DataFrame({"snapshot": s, "derived": d}).fillna(0.0)
    out["diff"] = out["derived"] - out["snapshot"]
    out["match"] = out["diff"].abs() < 0.01
    return out.sort_values("match")
