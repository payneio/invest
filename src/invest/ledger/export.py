"""Emit the unified transaction rows as a beancount ledger.

The ledger (under ``ledger/``) is the curated source of truth; this module
*generates* the machine part of it from the fetched data:

* ``ledger/<broker>.auto.beancount`` — one transaction per unified row.
* ``ledger/accounts.beancount`` — the ``open`` + ``commodity`` declarations used.
* ``ledger/main.beancount`` — options + ``include`` of everything (+ hand-edited
  ``fixups.beancount`` when present).

Routing is by economic shape, not just ``type``: a positive share quantity is an
acquisition, negative a disposal, and a NaN quantity is a cash event. Buys use
beancount's total-cost ``{{...}}`` lot syntax so they balance exactly despite
per-share price rounding. Disposals reduce lots with ``{}`` (+ the configured FIFO
booking), letting beancount compute realized P&L. Anything ambiguous balances to
``Equity:Adjustments`` so the file always loads.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from invest import broker, config

# Shared (non-brokerage) accounts.
EXTERNAL_BANK = "Assets:External:Bank"
INCOME_DIVIDENDS = "Income:Dividends"
INCOME_INTEREST = "Income:Interest"
INCOME_PNL = "Income:RealizedPnL"
EXPENSES_FEES = "Expenses:Fees"
EXPENSES_TAXES = "Expenses:Taxes"
EXPENSES_MISC = "Expenses:Misc"
EQUITY_CONTRIB = "Equity:Contributions"
EQUITY_OPENING = "Equity:Opening-Balances"
EQUITY_ADJ = "Equity:Adjustments"

_SHARED_ACCOUNTS = [
    EXTERNAL_BANK, INCOME_DIVIDENDS, INCOME_INTEREST, INCOME_PNL,
    EXPENSES_FEES, EXPENSES_TAXES, EXPENSES_MISC,
    EQUITY_CONTRIB, EQUITY_OPENING, EQUITY_ADJ,
]

_OPEN_DATE = "2000-01-01"  # everything opens before the earliest possible activity
_COMMODITY_RE = re.compile(r"^[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]$")


# --------------------------------------------------------------------------- #
# Name sanitization
# --------------------------------------------------------------------------- #
def sanitize_commodity(symbol: object) -> str | None:
    """Map a ticker/CUSIP to a valid beancount commodity, or None for cash-only.

    Beancount commodities must match ``[A-Z][A-Z0-9'._-]*`` (start with a letter).
    CUSIPs like ``09261F614`` get a ``C`` prefix; the original is kept in the
    commodity's metadata by :func:`generate`.
    """
    if symbol is None or (isinstance(symbol, float) and pd.isna(symbol)):
        return None
    s = str(symbol).strip().upper()
    if not s:
        return None
    s = re.sub(r"[^A-Z0-9'._-]", "", s)
    if not s:
        return None
    if not s[0].isalpha():
        s = "C" + s
    if not s[-1].isalnum():
        s = s + "X"
    return s if _COMMODITY_RE.match(s) else None


def cash_symbols(symbol_map=None) -> set[str]:
    """Symbols to treat as USD cash: each broker's declared money-market tickers
    plus anything the map marks ``asset_class: cash``."""
    syms = {s for b in broker.BROKERS for s in b.cash_symbols}
    if symbol_map is not None:
        for orig, entry in symbol_map.symbols.items():
            if entry.get("asset_class") == "cash":
                syms.add(str(orig).strip().upper())
    return syms


def _camel(name) -> str:
    """Turn an account name into a valid beancount account component.

    ``"Rollover IRA"`` -> ``"RolloverIRA"``, ``"Microsoft 401k"`` -> ``"Microsoft401k"``.
    Components must start with an uppercase letter; numeric ids get an ``A`` prefix.
    """
    parts = re.split(r"[^A-Za-z0-9]+", str(name).strip())
    camel = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not camel:
        camel = "Unknown"
    if not camel[0].isalpha():
        camel = "A" + camel
    return camel


def account_for(broker, name) -> str:
    """``Assets:<Broker>:<CamelAccountName>``."""
    return f"Assets:{_camel(broker)}:{_camel(name)}"


# --------------------------------------------------------------------------- #
# Number formatting
# --------------------------------------------------------------------------- #
def _q(x: float) -> str:
    """Format a share quantity (up to 8 dp, trailing zeros trimmed)."""
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"


def _m(x: float) -> str:
    """Format a money amount to cents."""
    return f"{x:.2f}"


def _f(x) -> float:
    return float(x) if x is not None and not pd.isna(x) else 0.0


# --------------------------------------------------------------------------- #
# One transaction -> directive text
# --------------------------------------------------------------------------- #
def _directive(
    row: dict, used_accounts: set[str], used_commodities: dict[str, dict],
    cash_syms: set[str],
) -> str | None:
    broker = row["broker"]
    name = row.get("account_name") or row.get("account_number") or "Unknown"
    acct = account_for(broker, name)
    used_accounts.add(acct)

    t = row.get("type") or "other"
    amt = _f(row.get("amount"))
    qty = row.get("quantity")
    qty = float(qty) if qty is not None and not pd.isna(qty) else None
    price = row.get("price")
    price = float(price) if price is not None and not pd.isna(price) else None
    fees = _f(row.get("fees"))
    raw_sym = row.get("symbol")
    sym = sanitize_commodity(raw_sym)

    # Money-market funds are USD cash: a share sweep (buy/sell at $1 NAV) nets to zero
    # — skip it; a cash yield falls through and is booked by type.
    if raw_sym and str(raw_sym).strip().upper() in cash_syms:
        if qty is not None and qty != 0:
            return None
        sym = None

    if sym:
        used_commodities.setdefault(sym, {"original": str(raw_sym)})

    date = row["date"]
    if pd.isna(date):
        return None
    date = pd.Timestamp(date).date().isoformat()

    narration = f"{broker}: {t}" + (f" {raw_sym}" if raw_sym else "")
    meta = [f'  id: "{row.get("id")}"', f'  txn_type: "{t}"']
    if row.get("source"):
        meta.append(f'  source: "{row["source"]}"')
    if raw_sym:
        meta.append(f'  symbol: "{raw_sym}"')
    head = f'{date} * "{narration}"\n' + "\n".join(meta) + "\n"

    def post(account: str, amount_str: str) -> str:
        used_accounts.add(account)
        return f"  {account}  {amount_str}\n"

    # --- share movements ---
    if sym and qty is not None and qty != 0:
        if qty > 0:  # acquisition
            if amt < 0:  # bought with cash
                cost_total = -amt - fees
                body = post(acct, f"{_q(qty)} {sym} {{{{{_m(cost_total)} USD}}}}")
                if fees:
                    body += post(EXPENSES_FEES, f"{_m(fees)} USD")
                body += post(acct, f"{_m(amt)} USD")
            else:  # in-kind (conversion / exchange-in): value = amt, no cash leg
                cost_total = amt if amt > 0 else 0.0
                body = post(acct, f"{_q(qty)} {sym} {{{{{_m(cost_total)} USD}}}}")
                body += post(EQUITY_OPENING, f"{_m(-cost_total)} USD")
            return head + body
        else:  # disposal (qty < 0): reduce lot, let FIFO + RealizedPnL settle
            sale_price = abs(price if price else (amt / -qty if qty else 0.0))
            body = post(acct, f"{_q(qty)} {sym} {{}} @ {_m(sale_price)} USD")
            if amt:  # proceeds to cash
                body += post(acct, f"{_m(amt)} USD")
            else:  # in-kind out (exchange-out)
                body += post(EQUITY_OPENING, f"{_m(-(-qty) * sale_price)} USD")
            body += f"  {INCOME_PNL}\n"
            used_accounts.add(INCOME_PNL)
            return head + body

    # --- cash events ---
    if t == "dividend":
        body = post(acct, f"{_m(amt)} USD") + post(INCOME_DIVIDENDS, f"{_m(-amt)} USD")
    elif t == "interest":
        body = post(acct, f"{_m(amt)} USD") + post(INCOME_INTEREST, f"{_m(-amt)} USD")
    elif t == "realized_gain_loss":
        body = post(acct, f"{_m(amt)} USD") + post(INCOME_PNL, f"{_m(-amt)} USD")
    elif t in ("transfer_in", "transfer_out"):
        body = post(acct, f"{_m(amt)} USD") + post(EXTERNAL_BANK, f"{_m(-amt)} USD")
    elif t == "contribution":
        body = post(acct, f"{_m(amt)} USD") + post(EQUITY_CONTRIB, f"{_m(-amt)} USD")
    elif t in ("fee",):
        body = post(EXPENSES_FEES, f"{_m(-amt)} USD") + post(acct, f"{_m(amt)} USD")
    elif t == "tax":
        body = post(EXPENSES_TAXES, f"{_m(-amt)} USD") + post(acct, f"{_m(amt)} USD")
    elif t in ("card", "bill_pay"):
        body = post(EXPENSES_MISC, f"{_m(-amt)} USD") + post(acct, f"{_m(amt)} USD")
    else:  # unknown / ambiguous — balance to Adjustments so it always loads
        body = post(acct, f"{_m(amt)} USD") + post(EQUITY_ADJ, f"{_m(-amt)} USD")

    return head + body


# --------------------------------------------------------------------------- #
# Generate the ledger tree
# --------------------------------------------------------------------------- #
def _snapshot_account(row: dict, accounts_map: dict, primary_by_broker: dict) -> str:
    """The beancount account a snapshot holding maps to.

    Brokers whose snapshot carries account numbers match by account_number (→
    accounts.yaml name, same as the ledger). Brokers whose snapshot is
    account-agnostic (``snapshot_has_accounts=False``) map to that broker's busiest
    ledger account.
    """
    bk = row["broker"]
    spec = broker.BY_NAME.get(bk)
    if spec is not None and not spec.snapshot_has_accounts:
        return primary_by_broker.get(bk, f"Assets:{_camel(bk)}:Main")
    name = accounts_map.get(str(row.get("account_number"))) or row.get("account_name") \
        or row.get("account_number")
    return account_for(bk, name)


def _broker_of_account(account: str) -> str:
    """``Assets:Fidelity:Individual`` -> ``"fidelity"`` (the broker registry key)."""
    parts = account.split(":")
    return parts[1].lower() if len(parts) > 1 else ""


def generate_openings(
    transactions: pd.DataFrame,
    snapshot: pd.DataFrame,
    *,
    accounts_map: dict | None = None,
    symbol_map=None,
    out_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Write ``opening.auto.beancount`` so derived positions reconcile to the snapshot.

    For each held ``(account, commodity)``: ``opening = snapshot_qty − net_in_window``.
    Positive openings become a dated-2000 lot (cost = Fidelity ``avg_cost_basis``, or
    ``last_price``/0 + ``estimated:`` flag for Robinhood). This both reconciles current
    positions by construction and supplies lots for in-window sells to book against.
    Negative openings (usually un-adjusted splits) are reported, not emitted.
    Returns a frame of every proposal for review.
    """
    accounts_map = accounts_map if accounts_map is not None else {}
    out = Path(out_dir) if out_dir else config.LEDGER_DIR
    out.mkdir(parents=True, exist_ok=True)
    cash_syms = cash_symbols(symbol_map)

    def _is_cash(raw) -> bool:
        return raw is not None and not pd.isna(raw) and str(raw).strip().upper() in cash_syms

    # One pass over the ledger: net signed qty, disposal price, and the time-ordered
    # flow per (account, commodity) (for the intra-window running-balance dip).
    net: dict[tuple, float] = defaultdict(float)
    sell_qty: dict[tuple, float] = defaultdict(float)
    sell_amt: dict[tuple, float] = defaultdict(float)
    flows: dict[tuple, list] = defaultdict(list)
    acct_counts: dict[str, Counter] = defaultdict(Counter)  # broker -> account → count
    for row in transactions.to_dict("records"):
        q = row.get("quantity")
        if q is None or pd.isna(q) or q == 0 or _is_cash(row.get("symbol")):
            continue
        sym = sanitize_commodity(row.get("symbol"))
        if not sym:
            continue
        q = float(q)
        acct = account_for(row["broker"], row.get("account_name") or row.get("account_number"))
        net[(acct, sym)] += q
        flows[(acct, sym)].append((pd.Timestamp(row["date"]), q))
        if q < 0:
            sell_qty[(acct, sym)] += -q
            sell_amt[(acct, sym)] += _f(row.get("amount"))
        acct_counts[row["broker"]][acct] += 1
    # Busiest ledger account per broker — where account-agnostic snapshots map.
    primary_by_broker = {bk: c.most_common(1)[0][0] for bk, c in acct_counts.items()}

    # Snapshot holdings keyed by the same beancount account.
    snap_qty: dict[tuple, float] = {}
    snap_cost: dict[tuple, float] = {}
    for row in snapshot.to_dict("records"):
        if row.get("is_cash"):
            continue
        q = row.get("quantity")
        sym = sanitize_commodity(row.get("symbol"))
        if q is None or pd.isna(q) or not sym or _is_cash(row.get("symbol")):
            continue
        acct = _snapshot_account(row, accounts_map, primary_by_broker)
        snap_qty[(acct, sym)] = float(q)
        cb = row.get("avg_cost_basis")
        if cb is not None and not pd.isna(cb):
            snap_cost[(acct, sym)] = float(cb)

    lines: list[str] = []
    proposals: list[dict] = []
    for key in sorted(set(net) | set(snap_qty)):
        acct, sym = key
        held = snap_qty.get(key, 0.0)
        base = held - net.get(key, 0.0)  # opening that reconciles the END balance

        # Deepest intra-window dip if we started from `base`; bump opening to cover it
        # so in-window sells always have a lot to book against.
        run = mn = base
        for _, q in sorted(flows.get(key, [])):
            run += q
            mn = min(mn, run)
        deficit = max(0.0, -mn)
        opening = base + deficit

        if abs(opening) < 1e-6:
            status = "reconciled"
        elif deficit > 1e-6:
            status = "split-review"  # booking needed extra shares — likely a split/vest
        elif base < 0:
            status = "negative-review"  # un-recorded disposal (merger/transfer-out)
        else:
            status = "ok"
        proposals.append({"account": acct, "symbol": sym, "opening_qty": round(opening, 6),
                          "held": round(held, 6), "deficit": round(deficit, 6), "status": status})
        if status in ("reconciled", "negative-review"):
            continue

        # Cost basis: snapshot avg cost if held; else the in-window disposal price
        # (so realized gain on a sold-off lot is ~0 rather than full proceeds).
        if key in snap_cost:
            cost, estimated = snap_cost[key], False
        elif sell_qty.get(key):
            cost, estimated = abs(sell_amt[key] / sell_qty[key]), True
        else:
            cost, estimated = 0.0, True
        meta = "  estimated: TRUE\n" if estimated else ""
        lines.append(
            f'{_OPEN_DATE} * "opening: {sym}"\n'
            f'  txn_type: "opening"\n{meta}'
            f'  {acct}  {_q(opening)} {sym} {{{{{_m(opening * cost)} USD}}}}\n'
            f'  {EQUITY_OPENING}  {_m(-opening * cost)} USD\n'
        )

    (out / "opening.auto.beancount").write_text("\n".join(lines))
    return pd.DataFrame(proposals)


def generate_cash_openings(
    entries,
    snapshot: pd.DataFrame,
    *,
    accounts_map: dict | None = None,
    out_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Write ``cash_opening.auto.beancount`` so derived USD cash reconciles to the
    snapshot — the cash analogue of the share opening lots.

    ``opening = snapshot_cash − net_USD_flow``, where the net flow is the USD
    balance of the *already-loaded* ledger (which has no cash opening yet). Run as
    the second pass of :func:`build_ledger`. Returns the per-account proposals.
    """
    from beancount.core import realization

    accounts_map = accounts_map if accounts_map is not None else {}
    out = Path(out_dir) if out_dir else config.LEDGER_DIR

    # Net USD flow per account = the current ledger USD balance (zero opening).
    root = realization.realize(entries)
    net_usd: dict[str, float] = {}
    for ra in realization.iter_children(root):
        if not ra.account.startswith("Assets:"):
            continue
        for p in ra.balance:
            if p.units.currency == "USD":
                net_usd[ra.account] = net_usd.get(ra.account, 0.0) + float(p.units.number)

    # Snapshot cash target per account (MMFs included — they're modeled as USD).
    snap_cash: dict[str, float] = {}
    for row in snapshot.to_dict("records"):
        if not row.get("is_cash"):
            continue
        cv = pd.to_numeric(pd.Series([row.get("current_value")]), errors="coerce").iloc[0]
        if pd.isna(cv):
            continue
        acct = _snapshot_account(row, accounts_map, {})  # cash rows carry accounts
        snap_cash[acct] = snap_cash.get(acct, 0.0) + float(cv)

    lines: list[str] = []
    proposals: list[dict] = []
    accounts = {a for a in net_usd if not a.startswith("Assets:External")} | set(snap_cash)
    for acct in sorted(accounts):
        # Brokers with a cash snapshot are authoritative (0 if it lists no cash for an
        # account → zeroes phantom cash where contributions bought funds modeled via
        # opening lots). Brokers without one keep the (full-history) ledger net flow.
        spec = broker.BY_NAME.get(_broker_of_account(acct))
        if spec is not None and not spec.has_cash_snapshot and acct not in snap_cash:
            continue
        target = snap_cash.get(acct, 0.0)
        opening = target - net_usd.get(acct, 0.0)
        proposals.append({"account": acct, "opening_usd": round(opening, 2),
                          "target": round(target, 2)})
        if abs(opening) < 0.005:
            continue
        lines.append(
            f'{_OPEN_DATE} * "opening cash"\n  txn_type: "opening"\n'
            f'  {acct}  {_m(opening)} USD\n'
            f'  {EQUITY_OPENING}  {_m(-opening)} USD\n'
        )
    (out / "cash_opening.auto.beancount").write_text("\n".join(lines))
    return pd.DataFrame(proposals)


def build_ledger(
    transactions: pd.DataFrame,
    snapshot: pd.DataFrame,
    *,
    accounts_map: dict | None = None,
    symbol_map=None,
    out_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Generate the full auto ledger in two passes (cash openings need the loaded
    ledger's net USD flow). Returns the cash-opening proposals.
    """
    from beancount import loader

    out = Path(out_dir) if out_dir else config.LEDGER_DIR
    generate_openings(transactions, snapshot, accounts_map=accounts_map,
                      symbol_map=symbol_map, out_dir=out_dir)
    (out / "cash_opening.auto.beancount").write_text("")  # reset for pass 1
    generate(transactions, snapshot=snapshot, symbol_map=symbol_map, out_dir=out_dir)
    entries, _, _ = loader.load_file(str(out / "main.beancount"))
    return generate_cash_openings(entries, snapshot, accounts_map=accounts_map, out_dir=out_dir)


def generate(
    transactions: pd.DataFrame,
    *,
    snapshot: pd.DataFrame | None = None,
    out_dir: str | Path | None = None,
    symbol_map=None,
) -> dict[str, int]:
    """Write the auto-generated ledger files. Returns counts.

    Produces per-broker ``<broker>.auto.beancount`` plus ``accounts.beancount`` and
    ``main.beancount``. Leaves a hand-edited ``fixups.beancount`` untouched (creates
    a stub only if absent).
    """
    out = Path(out_dir) if out_dir else config.LEDGER_DIR
    out.mkdir(parents=True, exist_ok=True)

    used_accounts: set[str] = set(_SHARED_ACCOUNTS)
    used_commodities: dict[str, dict] = {}
    by_broker: dict[str, list[str]] = {}
    cash_syms = cash_symbols(symbol_map)

    rows = transactions.sort_values(["broker", "date", "id"], na_position="last")
    for row in rows.to_dict("records"):
        text = _directive(row, used_accounts, used_commodities, cash_syms)
        if text:
            by_broker.setdefault(row["broker"], []).append(text)

    # Also declare commodities held only via opening lots (e.g. proprietary 401k
    # pools with no per-share transactions), so their original symbol is recoverable.
    if snapshot is not None:
        for row in snapshot.to_dict("records"):
            raw = row.get("symbol")
            if raw is None or pd.isna(raw) or str(raw).strip().upper() in cash_syms:
                continue
            s = sanitize_commodity(raw)
            if s:
                used_commodities.setdefault(s, {"original": str(raw)})

    # yf_symbol metadata from the symbol map, if available.
    yf = {}
    if symbol_map is not None:
        for orig, entry in symbol_map.symbols.items():
            s = sanitize_commodity(orig)
            if s and entry.get("yf_symbol"):
                yf[s] = entry["yf_symbol"]

    # accounts.beancount — open + commodity directives only (options live in main,
    # the top-level file, so booking_method actually applies).
    acc_lines = []
    for acct in sorted(used_accounts):
        acc_lines.append(f"{_OPEN_DATE} open {acct}")
    acc_lines.append("")
    for sym in sorted(used_commodities):
        acc_lines.append(f"{_OPEN_DATE} commodity {sym}")
        orig = used_commodities[sym]["original"]
        if orig and orig != sym:
            acc_lines.append(f'  original: "{orig}"')
        if sym in yf:
            acc_lines.append(f'  yf_symbol: "{yf[sym]}"')
    (out / "accounts.beancount").write_text("\n".join(acc_lines) + "\n")

    # per-broker auto files
    broker_files = []
    for broker, directives in sorted(by_broker.items()):
        fname = f"{broker}.auto.beancount"
        (out / fname).write_text("\n".join(directives))
        broker_files.append(fname)

    # main.beancount — top-level: options MUST live here to take effect, then includes.
    includes = [
        'option "operating_currency" "USD"',
        'option "booking_method" "FIFO"',
        "",
        'include "accounts.beancount"',
    ]
    includes += [f'include "{f}"' for f in broker_files]
    if (out / "opening.auto.beancount").exists():
        includes.append('include "opening.auto.beancount"')
    if (out / "cash_opening.auto.beancount").exists():
        includes.append('include "cash_opening.auto.beancount"')
    if (out / "fixups.beancount").exists():
        includes.append('include "fixups.beancount"')
    if (out / "balances.beancount").exists():
        includes.append('include "balances.beancount"')
    (out / "main.beancount").write_text("\n".join(includes) + "\n")

    # fixups stub (never clobber an existing one)
    fixups = out / "fixups.beancount"
    if not fixups.exists():
        fixups.write_text(
            "; Hand-edited corrections: opening lots, splits, transfers, reclassifications.\n"
            "; Generated files (*.auto, accounts, balances) are overwritten; this one is not.\n"
        )

    return {
        "transactions": int(sum(len(v) for v in by_broker.values())),
        "accounts": len(used_accounts),
        "commodities": len(used_commodities),
    }
