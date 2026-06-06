"""Canonical normalized schemas, in one place.

Two cross-broker entities flow through the pipeline:

* **positions** — a point-in-time snapshot (what you hold now). Built from broker
  positions exports by ``fidelity.load_positions`` / ``robinhood.load_positions``.
* **transactions** — the historical ledger (one row per economic event). Built from
  the fetched transaction stores by ``fidelity_transactions`` / ``robinhood_transactions``.

Both broker loaders return ``df[POSITIONS_SCHEMA]`` / ``df[TRANSACTIONS_SCHEMA]`` so
the per-broker frames concatenate cleanly.
"""

from __future__ import annotations

import pandas as pd

# --------------------------------------------------------------------------- #
# Positions (snapshot)
# --------------------------------------------------------------------------- #
POSITIONS_SCHEMA: list[str] = [
    "broker",
    "account_number",
    "account_name",
    "symbol",
    "symbol_raw",
    "description",
    "quantity",
    "last_price",
    "current_value",
    "cost_basis",
    "avg_cost_basis",
    "total_gain_loss",
    "total_gain_loss_pct",
    "is_cash",
]

# --------------------------------------------------------------------------- #
# Transactions (ledger) — one row per economic event
# --------------------------------------------------------------------------- #
# Sign convention for `amount`: cash flow INTO the account.
#   amount > 0  -> cash in  (sell proceeds, dividend, interest, deposit, contribution)
#   amount < 0  -> cash out (buy, fee, withdrawal, bill, transfer-out)
TRANSACTIONS_SCHEMA: list[str] = [
    "broker",          # "fidelity" / "robinhood"
    "account_number",  # raw id; never numeric (preserve e.g. "Z00000000")
    "account_name",    # from config/accounts.yaml, else equals account_number
    "date",            # datetime64[ns], event date
    "type",            # normalized enum (see TRANSACTION_TYPES)
    "symbol",          # ticker / fund id; NA for cash-only events
    "description",     # original human description
    "quantity",        # signed shares (+buy / -sell) where available, else NaN
    "price",           # per-share price where available, else NaN
    "amount",          # signed cash flow into the account (the load-bearing number)
    "fees",            # total fees; 0.0 when none, NaN if unknown
    "currency",        # "USD" default; "BTC" etc. only as a quote note
    "source",          # provenance: RH endpoint name, or Fidelity txnCatCode/Desc
    "id",              # stable per-event id (RH record id; Fidelity ref or hash)
    "raw",             # json.dumps(original_record, sort_keys=True)
]

# Closed set of normalized event types.
TRANSACTION_TYPES: frozenset[str] = frozenset({
    "buy", "sell", "dividend", "interest", "transfer_in", "transfer_out",
    "fee", "tax", "contribution", "exchange_in", "exchange_out",
    "realized_gain_loss", "conversion", "bill_pay", "card",
    "crypto_buy", "crypto_sell", "option_buy", "option_sell", "other",
})

# pandas dtypes for the transactions frame (used to build a correctly-typed
# empty frame so an all-empty ingest still has the right columns).
_TX_DTYPES: dict[str, str] = {
    "broker": "string",
    "account_number": "string",
    "account_name": "string",
    "date": "datetime64[ns]",
    "type": "string",
    "symbol": "string",
    "description": "string",
    "quantity": "float64",
    "price": "float64",
    "amount": "float64",
    "fees": "float64",
    "currency": "string",
    "source": "string",
    "id": "string",
    "raw": "string",
}


def empty_transactions() -> pd.DataFrame:
    """A zero-row transactions frame with the canonical columns and dtypes.

    Used as the concat seed in ``broker.load_all_transactions`` so the result
    always has the right schema even when no source produced any rows.
    """
    return pd.DataFrame({col: pd.Series(dtype=dt) for col, dt in _TX_DTYPES.items()})[
        TRANSACTIONS_SCHEMA
    ]
