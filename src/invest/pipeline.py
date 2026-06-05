"""Ingest pipeline: broker exports + symbol map + yfinance -> Parquet.

Run as ``uv run invest-ingest`` (see flags in ``main``). Produces:
  data/processed/positions.parquet  — one row per holding, classified & priced
  data/processed/prices.parquet     — wide adjusted-close history (yahoo tickers)
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import analysis, brokers, config, mapping, prices


def run(
    *,
    history_period: str = "3y",
    use_network: bool = True,
    verbose: bool = True,
) -> dict[str, pd.DataFrame]:
    """Execute the full pipeline and write Parquet outputs. Returns the frames."""
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    # 1. Positions truth (all brokers).
    positions = brokers.load_all_positions(verbose=verbose)
    say(f"[pipeline] {len(positions)} positions loaded.")

    # 2. Classification truth.
    smap = mapping.load_symbol_map()
    classified = mapping.classify_positions(positions, smap)
    n_unclassified = int(classified["unclassified"].sum())
    if n_unclassified:
        unknown = sorted(classified.loc[classified["unclassified"], "symbol"].unique())
        say(f"[pipeline] WARNING: {n_unclassified} unclassified holding(s): {unknown}")
        say("           Add them to config/symbol_map.yaml for correct pricing.")

    # 3. Price/history enrichment (yfinance), with graceful offline fallback.
    if use_network:
        hist_symbols = smap.history_symbols()
        say(f"[pipeline] fetching {history_period} history for {len(hist_symbols)} tickers…")
        history = prices.fetch_history(hist_symbols, period=history_period)
        yf_latest = prices.latest_from_history(history)
    else:
        say("[pipeline] --no-network: skipping yfinance, using Fidelity prices.")
        history = pd.DataFrame()
        yf_latest = pd.Series(dtype="float64")

    # 4. Resolve a single price + market value per position.
    resolved = prices.resolve_prices(classified, yf_latest)

    # 5. Persist.
    resolved.to_parquet(config.POSITIONS_PARQUET, index=False)
    if not history.empty:
        history.to_parquet(config.PRICES_PARQUET)
    say(f"[pipeline] wrote {config.POSITIONS_PARQUET.relative_to(config.ROOT)}")
    if not history.empty:
        say(f"[pipeline] wrote {config.PRICES_PARQUET.relative_to(config.ROOT)}")

    # 6. Quick console summary so a bare ingest is still informative.
    if verbose:
        total = resolved["market_value"].sum()
        say(f"\n[pipeline] total portfolio value: ${total:,.2f}")
        alloc = analysis.allocation_by(resolved, "asset_class")
        with pd.option_context("display.float_format", lambda v: f"{v:,.2f}"):
            say("\nAllocation by asset class:")
            say(alloc.to_string())

    return {"positions": resolved, "history": history}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invest-ingest",
        description="Parse broker exports, enrich with yfinance, write Parquet.",
    )
    parser.add_argument(
        "--history-period",
        default="3y",
        help="yfinance history window (e.g. 1y, 3y, 5y, max). Default: 3y.",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip yfinance; price everything from the Fidelity export.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    args = parser.parse_args(argv)

    try:
        run(
            history_period=args.history_period,
            use_network=not args.no_network,
            verbose=not args.quiet,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
