"""Parse the Fidelity Activity export (from the Chrome extension) into the ledger.

Input is ``fidelity_history.json`` — shape ``{meta, data: {historys: [...], ...}}`` —
produced by the ``fidelity-export-extension/`` exporter. We normalize ``data["historys"]`` (the
transaction ledger) into the unified ``TRANSACTIONS_SCHEMA``.

Event type is decided from the ``(txnCatCode, txnTypeCode, txnSubCatCode)`` tuple,
falling back to ``txnCatDesc`` for the 401(k) rows (which carry no ``txnCatCode``),
then ``other``. ``amount`` strings already carry the sign (``+$.../-$...``). Records
have no stable id, so ``id`` is ``sourceSystemRefId`` when present else a content hash
that excludes the extension's window tags (so the same event harvested in two
overlapping windows collapses on dedupe).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from invest import mapping
from invest.broker.fidelity.positions import clean_symbol
from invest.schema import TRANSACTIONS_SCHEMA

# (txnCatCode, txnTypeCode, txnSubCatCode) -> normalized type.
# "transfer_by_sign" is resolved to transfer_in/out using the parsed amount sign.
FIDELITY_TYPE_MAP: dict[tuple, str] = {
    ("DV", "IT", "VP"): "dividend",
    ("DV", "ST", "RN"): "dividend",          # reinvested dividend (negative cash)
    ("DV", "IT", "IT"): "interest",
    ("IA", "ST", "SL"): "sell",
    ("IA", "ST", "BY"): "buy",
    # "Other Cash" is mixed: HSA contributions (+) but also outflows (-). By sign.
    ("IA", "CT", "OC"): "contribution_by_sign",
    ("IA", "IT", "IT"): "interest",
    ("IA", "IT", "VP"): "dividend",
    ("IA", "CA", "DS"): "other",             # corporate action: distribution
    ("IA", "CA", "RD"): "other",             # corporate action: redemption
    ("IA", "CA", "RS"): "other",             # corporate action: reverse split
    ("ZZ", "ST", "BY"): "conversion",        # shares deposited / converted
    ("ZZ", "CT", "OC"): "other",
    ("ZZ", "CT", "TX"): "tax",
    ("X3", "CT", "WD"): "transfer_out",      # "Transfers To Your Bank"
    ("X1", "CT", "OC"): "transfer_by_sign",  # "Transfers Btw Fidelity Accts"
    ("AT", "CT", "DC"): "card",              # debit card
    ("BP", "CT", "BP"): "bill_pay",
}

# txnCatDesc -> type, for the 401(k) (acct 12345678) rows that have no txnCatCode.
FIDELITY_DESC_MAP: dict[str, str] = {
    "contribution": "contribution",
    "dividend": "dividend",
    "interest": "interest",
    "exchangeIn": "exchange_in",
    "exchangeOut": "exchange_out",
    "realizedGainLoss": "realized_gain_loss",
}

# Extension-added harvest tags; excluded from the content hash and from `raw`.
_TAG_KEYS = ("_window_from", "_window_to", "_variant", "_kind")


def _money(value: object) -> float:
    """Parse a Fidelity money/share string (``-$12,342.46``, ``+55.000``) to float.

    Returns NaN for blanks and the ``--`` placeholder. Sign is preserved.
    """
    if value is None:
        return float("nan")
    s = str(value).replace("$", "").replace(",", "").replace("+", "").strip()
    if s in ("", "--", "n/a", "N/A"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _parse_fidelity_date(value: object) -> pd.Timestamp:
    """Parse ``"Aug-29-2025"`` to a Timestamp; NaT on failure."""
    return pd.to_datetime(value, format="%b-%d-%Y", errors="coerce")


def _detail(items: list | None, key: str) -> str | None:
    """Pull a value out of a record's ``detailItems`` list by its ``key``."""
    for it in items or []:
        if it.get("key") == key:
            return it.get("value")
    return None


def _classify(rec: dict, amount: float) -> str:
    """Map a record to a normalized type (see module docstring)."""
    tup = (rec.get("txnCatCode"), rec.get("txnTypeCode"), rec.get("txnSubCatCode"))
    if rec.get("txnCatCode") is not None and tup in FIDELITY_TYPE_MAP:
        t = FIDELITY_TYPE_MAP[tup]
        if t == "transfer_by_sign":
            return "transfer_in" if (amount or 0) >= 0 else "transfer_out"
        if t == "contribution_by_sign":
            return "contribution" if (amount or 0) >= 0 else "other"
        return t
    desc = rec.get("txnCatDesc")
    if desc in FIDELITY_DESC_MAP:
        return FIDELITY_DESC_MAP[desc]
    return "other"


def _clean_record(rec: dict) -> dict:
    """Strip the extension's harvest tags so `raw`/hash see only Fidelity data."""
    return {k: v for k, v in rec.items() if k not in _TAG_KEYS}


def _row_id(rec: dict) -> str:
    """Stable id: ``sourceSystemRefId`` if present, else a content hash.

    The hash excludes the window tags and ``cashBalance`` (which can vary between
    overlapping windows) so the same economic event collapses on dedupe.
    """
    ref = rec.get("sourceSystemRefId")
    if ref:
        return str(ref)
    core = {k: v for k, v in _clean_record(rec).items() if k != "cashBalance"}
    blob = json.dumps(core, sort_keys=True, default=str)
    return "fh_" + hashlib.sha1(blob.encode()).hexdigest()[:16]


def load_transactions(
    json_path: str | Path, accounts: dict[str, str] | None = None
) -> pd.DataFrame:
    """Load ``fidelity_history.json`` into the unified transactions schema.

    ``accounts`` maps account_number -> name; loaded from config/accounts.yaml when
    omitted, falling back to the raw number.
    """
    if accounts is None:
        accounts = mapping.load_accounts()

    envelope = json.loads(Path(json_path).read_text())
    historys = (envelope.get("data") or {}).get("historys") or []

    rows: list[dict] = []
    for rec in historys:
        amount = _money(rec.get("amount"))
        acct = rec.get("acctNum")
        items = rec.get("detailItems")
        cat = rec.get("txnCatCode") or rec.get("txnCatDesc")
        rows.append(
            {
                "broker": "fidelity",
                "account_number": acct,
                "account_name": accounts.get(str(acct), acct),
                "date": _parse_fidelity_date(rec.get("date")),
                "type": _classify(rec, amount),
                "symbol": clean_symbol(rec.get("symbol")),
                "description": rec.get("description"),
                "quantity": _money(_detail(items, "Shares")),
                "price": _money(_detail(items, "Price")),
                "amount": amount,
                "fees": _money(_detail(items, "Fees")),
                "currency": "USD",
                "source": cat,
                "id": _row_id(rec),
                "raw": json.dumps(_clean_record(rec), sort_keys=True, default=str),
            }
        )

    df = pd.DataFrame(rows, columns=TRANSACTIONS_SCHEMA)
    if not df.empty:
        df["date"] = df["date"].astype("datetime64[ns]")
        df = df.drop_duplicates(subset=["broker", "id"], keep="first").reset_index(drop=True)
    return df
