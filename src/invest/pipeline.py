"""Ingest pipeline: raw exports → curated beancount ledger → derived Parquet.

Run as ``uv run invest-ingest`` (see flags in ``main``). The beancount ledger under
``ledger/`` is the source of truth; this pipeline (re)generates its machine part from
the fetched data, loads it, and **derives** the outputs:

  data/processed/positions.parquet     — holdings derived from the ledger (shares) +
                                          snapshot cash, classified & priced
  data/processed/prices.parquet        — wide adjusted-close history (yahoo tickers)
  data/processed/transactions.parquet  — the ledger flattened to a tidy event table

Use ``--no-emit`` to skip regenerating the ledger and just re-derive from the current
(hand-curated) ledger + fixups.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import analysis, broker, config, ledger, mapping, prices


def run(
    *,
    history_period: str = "3y",
    use_network: bool = True,
    transactions: bool = True,
    emit: bool = True,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """Execute the full pipeline and write Parquet outputs. Returns the frames."""
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    # 1. Raw truth: broker snapshot (cash + reconciliation target) + classification.
    snapshot = broker.load_all_positions(verbose=verbose)
    smap = mapping.load_symbol_map()
    accounts = mapping.load_accounts()

    # 2. (Re)generate the beancount ledger from the fetched data, unless --no-emit.
    #    Two passes: share opening lots, then cash opening balances (which need the
    #    loaded ledger's net USD flow) — both anchored to the snapshot.
    if emit:
        tx_rows = broker.load_all_transactions(verbose=verbose)
        ledger.build_ledger(tx_rows, snapshot, accounts_map=accounts, symbol_map=smap)
        say(f"[pipeline] regenerated ledger from {len(tx_rows)} transactions.")

    # 3. Load the curated ledger — the single source of truth (shares AND cash).
    entries, errors, _ = ledger.load()
    if errors:
        say(f"[pipeline] WARNING: {len(errors)} ledger load error(s); "
            f"add opening/split fixups in {config.LEDGER_FIXUPS.relative_to(config.ROOT)}")

    # 4. Derive ALL positions from the ledger.
    holdings = ledger.positions_dataframe(entries)

    # 5. Price/history enrichment (yfinance), snapshot used only as a price source
    #    for holdings with no public quote (proprietary 401k pools).
    if use_network and not holdings.empty:
        say(f"[pipeline] fetching {history_period} history…")
        history = prices.fetch_history(smap.history_symbols(), period=history_period)
        yf_latest = prices.latest_from_history(history)
    else:
        say("[pipeline] --no-network: pricing from the snapshot only.")
        history = pd.DataFrame()
        yf_latest = pd.Series(dtype="float64")
    enriched = ledger.enrich_positions(holdings, smap=smap, snapshot=snapshot, yf_latest=yf_latest)

    # 6. Verify derived positions against the snapshot (audit only — not a data source).
    rec = ledger.reconcile(holdings, snapshot)
    bad = rec[~rec["match"]]
    say(f"[pipeline] shares reconcile: {int(rec['match'].sum())}/{len(rec)} symbols"
        + (f"; review {list(bad.index)} (add fixups)" if len(bad) else " ✓"))
    d_cash = enriched.loc[enriched["is_cash"], "market_value"].sum()
    s_cash = pd.to_numeric(snapshot.loc[snapshot["is_cash"], "current_value"], errors="coerce").sum()
    no_cash_snapshot = [b.name for b in broker.BROKERS if not b.has_cash_snapshot]
    if abs(d_cash - s_cash) < 1.0:
        cash_note = "✓"
    else:
        cash_note = f"(off ${d_cash - s_cash:,.0f}"
        cash_note += f"; no cash snapshot for: {', '.join(no_cash_snapshot)})" if no_cash_snapshot else ")"
    say(f"[pipeline] cash reconcile: derived ${d_cash:,.0f} vs snapshot ${s_cash:,.0f} {cash_note}")

    # 7. Persist.
    enriched.to_parquet(config.POSITIONS_PARQUET, index=False)
    say(f"[pipeline] wrote {config.POSITIONS_PARQUET.relative_to(config.ROOT)}")
    if not history.empty:
        history.to_parquet(config.PRICES_PARQUET)
        say(f"[pipeline] wrote {config.PRICES_PARQUET.relative_to(config.ROOT)}")
    tx = pd.DataFrame()
    if transactions:
        tx = ledger.transactions_dataframe(entries)
        tx.to_parquet(config.TRANSACTIONS_PARQUET, index=False)
        say(f"[pipeline] wrote {config.TRANSACTIONS_PARQUET.relative_to(config.ROOT)} ({len(tx)} events)")

    # 8. Quick console summary so a bare ingest is still informative.
    if verbose:
        total = enriched["market_value"].sum()
        say(f"\n[pipeline] total portfolio value: ${total:,.2f}")
        alloc = analysis.allocation_by(enriched, "asset_class")
        with pd.option_context("display.float_format", lambda v: f"{v:,.2f}"):
            say("\nAllocation by asset class:")
            say(alloc.to_string())

    return {"positions": enriched, "history": history, "transactions": tx}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invest-ingest",
        description="Regenerate the beancount ledger from fetched data, derive Parquet.",
    )
    parser.add_argument(
        "--history-period", default="3y",
        help="yfinance history window (e.g. 1y, 3y, 5y, max). Default: 3y.",
    )
    parser.add_argument(
        "--no-network", action="store_true",
        help="Skip yfinance; price from the snapshot only.",
    )
    parser.add_argument(
        "--no-emit", action="store_true",
        help="Don't regenerate the ledger; derive from the current (curated) ledger.",
    )
    parser.add_argument(
        "--no-transactions", action="store_true",
        help="Skip writing transactions.parquet.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    args = parser.parse_args(argv)

    try:
        run(
            history_period=args.history_period,
            use_network=not args.no_network,
            transactions=not args.no_transactions,
            emit=not args.no_emit,
            verbose=not args.quiet,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
