"""Parse the Robinhood history JSON store into the unified transactions ledger.

Input is the per-endpoint store under ``data/raw/robinhood/history/`` produced by
``broker.robinhood.fetch.fetch_all`` (``stock_orders.json``, ``dividends.json``, …) plus the
``instruments.json`` cache (instrument_id -> ticker) written by the fetch step. Each
endpoint has its own normalizer mapping records into ``TRANSACTIONS_SCHEMA``.

Symbol resolution is a **pure lookup** against ``instruments.json`` — no network here
(that happens in the fetch step). Orders that reference an instrument missing from the
cache get ``symbol = NA``. Only settled/filled rows become transactions; pending,
cancelled, and rejected rows are dropped so ``amount`` reflects realized cash.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from invest import mapping
from . import INSTRUMENTS_PATH
from invest.schema import TRANSACTIONS_SCHEMA

_ACCT_RE = re.compile(r"/accounts/([^/]+)/")


def _f(value: object) -> float:
    """Coerce a Robinhood numeric (str / number / ``{amount: ...}``) to float; NaN otherwise."""
    if value is None:
        return float("nan")
    if isinstance(value, dict):  # e.g. {"amount": "7.13", "currency_code": "USD"}
        value = value.get("amount")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _date(value: object) -> pd.Timestamp:
    """Parse an ISO timestamp (tz-aware) to a tz-naive UTC date; NaT on failure."""
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if ts is pd.NaT or pd.isna(ts):
        return pd.NaT
    return ts.tz_localize(None).normalize()


def _acct_from_url(url: object) -> str | None:
    m = _ACCT_RE.search(str(url or ""))
    return m.group(1) if m else None


def _instrument_id_from_url(url: object) -> str | None:
    m = re.search(r"/instruments/([^/]+)/", str(url or ""))
    return m.group(1) if m else None


def _sum_fees(*values: object) -> float:
    """Sum fee components, treating missing/blank as 0; NaN only if all missing."""
    total, seen = 0.0, False
    for v in values:
        f = _f(v)
        if f == f:  # not NaN
            total += f
            seen = True
    return total if seen else float("nan")


def load_instruments(path: str | Path | None = None) -> dict[str, str]:
    """Load the instrument_id -> ticker cache; empty dict if absent."""
    path = Path(path) if path else INSTRUMENTS_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    out: dict[str, str] = {}
    for iid, rec in data.items():
        sym = rec.get("symbol") if isinstance(rec, dict) else rec
        if sym:
            out[iid] = sym
    return out


# --------------------------------------------------------------------------- #
# Per-endpoint normalizers — each yields partial row dicts (schema-shaped).
# --------------------------------------------------------------------------- #
def _row(**kw) -> dict:
    """Build a schema-shaped row, defaulting missing fields."""
    base = {
        "broker": "robinhood", "account_number": None, "account_name": None,
        "date": pd.NaT, "type": "other", "symbol": None, "description": None,
        "quantity": float("nan"), "price": float("nan"), "amount": float("nan"),
        "fees": float("nan"), "currency": "USD", "source": None, "id": None, "raw": None,
    }
    base.update(kw)
    return base


def _norm_stock_orders(recs, instruments, accounts):
    for r in recs:
        if r.get("state") != "filled":
            continue
        side = r.get("side")
        qty = _f(r.get("cumulative_quantity"))
        notional = _f(r.get("executed_notional"))
        if notional != notional:  # NaN -> fall back
            notional = _f(r.get("total_notional"))
        acct = _acct_from_url(r.get("account"))
        sym = instruments.get(r.get("instrument_id"))
        yield _row(
            account_number=acct, account_name=accounts.get(str(acct), acct),
            date=_date(r.get("last_transaction_at") or r.get("created_at")),
            type="buy" if side == "buy" else "sell",
            symbol=sym, description=f"{side} {qty:g} {sym or '?'}",
            quantity=qty if side == "buy" else -qty,
            price=_f(r.get("average_price")) or _f(r.get("price")),
            amount=-notional if side == "buy" else notional,
            fees=_sum_fees(r.get("fees"), r.get("sec_fees"), r.get("taf_fees")),
            source="stock_orders", id=r.get("id"),
            raw=json.dumps(r, sort_keys=True, default=str),
        )


def _norm_option_orders(recs, instruments, accounts):
    for r in recs:
        if r.get("state") != "filled":
            continue
        direction = r.get("direction")
        acct = r.get("account_number")
        net = _f(r.get("net_amount"))
        qty = _f(r.get("quantity"))
        sym = r.get("chain_symbol")
        is_buy = direction == "debit"
        yield _row(
            account_number=acct, account_name=accounts.get(str(acct), acct),
            date=_date(r.get("created_at")),
            type="option_buy" if is_buy else "option_sell",
            symbol=sym, description=f"option {direction} {qty:g} {sym or '?'}",
            quantity=qty if is_buy else -qty, price=_f(r.get("price")),
            amount=-net if is_buy else net,
            fees=_sum_fees(r.get("regulatory_fees"), r.get("contract_fees"), r.get("sales_taxes")),
            source="option_orders", id=r.get("id"),
            raw=json.dumps(r, sort_keys=True, default=str),
        )


def _norm_crypto_orders(recs, instruments, accounts):
    for r in recs:
        if r.get("state") != "filled":
            continue
        side = r.get("side")
        qty = _f(r.get("cumulative_quantity"))
        notional = _f(r.get("total_executed_notional"))
        sym = r.get("currency_code")
        fees = r.get("fees")
        fee_total = sum(_f(f) for f in fees) if isinstance(fees, list) else _f(fees)
        acct = r.get("account_id")
        yield _row(
            account_number=acct, account_name=accounts.get(str(acct), acct),
            date=_date(r.get("last_transaction_at") or r.get("created_at")),
            type="crypto_buy" if side == "buy" else "crypto_sell",
            symbol=sym, description=f"{side} {qty:g} {sym or '?'}",
            quantity=qty if side == "buy" else -qty, price=_f(r.get("average_price")),
            amount=-notional if side == "buy" else notional,
            fees=fee_total, currency="USD",
            source="crypto_orders", id=r.get("id"),
            raw=json.dumps(r, sort_keys=True, default=str),
        )


def _norm_dividends(recs, instruments, accounts):
    for r in recs:
        if r.get("state") not in ("paid", "reinvested"):
            continue
        acct = _acct_from_url(r.get("account"))
        iid = r.get("active_instrument_id") or _instrument_id_from_url(r.get("instrument"))
        sym = instruments.get(iid)
        yield _row(
            account_number=acct, account_name=accounts.get(str(acct), acct),
            date=_date(r.get("paid_at") or r.get("payable_date")),
            type="dividend", symbol=sym, description=f"dividend {sym or '?'}",
            amount=_f(r.get("amount")), fees=_f(r.get("fee")),
            source="dividends", id=r.get("id"),
            raw=json.dumps(r, sort_keys=True, default=str),
        )


def _norm_interest(recs, instruments, accounts):
    for r in recs:
        acct = r.get("account_number")
        amt = _f(r.get("amount"))
        signed = amt if r.get("direction") == "credit" else -amt
        yield _row(
            account_number=acct, account_name=accounts.get(str(acct), acct),
            date=_date(r.get("pay_date")), type="interest",
            description=r.get("reason") or "interest", amount=signed,
            source="interest_payments", id=r.get("id"),
            raw=json.dumps(r, sort_keys=True, default=str),
        )


def _norm_bank_transfers(recs, instruments, accounts):
    for r in recs:
        if r.get("state") != "completed":
            continue
        direction = r.get("direction")
        amt = _f(r.get("amount"))
        acct = _acct_from_url(r.get("account"))
        is_in = direction == "deposit"
        yield _row(
            account_number=acct, account_name=accounts.get(str(acct), acct),
            date=_date(r.get("created_at")),
            type="transfer_in" if is_in else "transfer_out",
            description=f"bank {direction}", amount=amt if is_in else -amt,
            fees=_f(r.get("fees")), source="bank_transfers", id=r.get("id"),
            raw=json.dumps(r, sort_keys=True, default=str),
        )


# endpoint file -> normalizer
_NORMALIZERS = {
    "stock_orders": _norm_stock_orders,
    "option_orders": _norm_option_orders,
    "crypto_orders": _norm_crypto_orders,
    "dividends": _norm_dividends,
    "interest_payments": _norm_interest,
    "bank_transfers": _norm_bank_transfers,
}


def load_transactions(
    history_dir: str | Path,
    accounts: dict[str, str] | None = None,
    instruments: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Load the Robinhood JSON store into the unified transactions schema.

    ``accounts`` (number -> name) and ``instruments`` (id -> ticker) default to
    ``config/accounts.yaml`` and the store's ``instruments.json``.
    """
    history_dir = Path(history_dir)
    if accounts is None:
        accounts = mapping.load_accounts()
    if instruments is None:
        instruments = load_instruments(history_dir / "instruments.json")

    rows: list[dict] = []
    for name, normalizer in _NORMALIZERS.items():
        path = history_dir / f"{name}.json"
        if not path.exists():
            continue
        recs = json.loads(path.read_text())
        rows.extend(normalizer(recs, instruments, accounts))

    df = pd.DataFrame(rows, columns=TRANSACTIONS_SCHEMA)
    if not df.empty:
        df["date"] = df["date"].astype("datetime64[ns]")
        df = df.drop_duplicates(subset=["broker", "id"], keep="first").reset_index(drop=True)
    return df
