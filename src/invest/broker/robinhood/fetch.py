"""Download Robinhood transaction history to a local JSON store via the private API.

Robinhood has no transaction-history export, but its web app (the
``/account/history`` page) is just a client over the private REST API at
``api.robinhood.com``. This module replays the same per-type endpoints through
``robin_stocks`` — which handles auth and the ``next``-cursor pagination — into a
local store under ``data/raw/robinhood/history/`` (gitignored).

The store is **idempotent and incremental**:

* Each endpoint is a ``<name>.json`` list of records, the canonical store.
* On every run, fetched records are merged into the store **keyed by record id**,
  so re-running never duplicates and only surfaces what's genuinely new/changed.
* For the order endpoints (which support an ``updated_at[gte]`` filter) only
  records updated since the latest one already stored are pulled — the bulk of
  history is not refetched. The small append-only ledgers (dividends, interest,
  transfers, …) are pulled in full and merged; that's cheap and still idempotent.
  Pass ``full=True`` to force a complete rebuild.

Two ways to authenticate; pick whichever you like:

1. **Bearer token** (no MFA): grab the ``authorization: Bearer …`` header from any
   ``api.robinhood.com`` request in Chrome DevTools (Network tab). Pass it, set
   ``RH_TOKEN`` in the environment, or drop it in ``data/raw/robinhood/.rh_token``.
   Tokens are good for weeks.
2. **Interactive login** (caches a session pickle under ``~/.tokens/`` for reuse)::

       authenticate(username="me@example.com", password="…")  # prompts for MFA

Typical use (also exposed as the ``invest-robinhood-fetch`` CLI)::

    from invest.broker.robinhood.fetch import authenticate, fetch_all
    authenticate()                       # token from RH_TOKEN / .rh_token
    summary = fetch_all(account_number="171074198")
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from . import DIR, HISTORY_DIR, TOKEN_PATH

# robin_stocks is imported lazily inside functions so importing this module never
# requires the dependency unless you actually pull.

_TOKEN_FILE = TOKEN_PATH

# Record fields that carry a record's timestamp, newest-meaningful first. Used
# both to compute the incremental high-water mark and to sort the store stably.
_DATE_FIELDS = (
    "updated_at",
    "created_at",
    "created",
    "paid_at",
    "payable_date",
    "effective_date",
    "date",
)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _resolve_token(token: str | None) -> str | None:
    """Token precedence: explicit arg → ``RH_TOKEN`` env → ``.rh_token`` file."""
    if token:
        return token.strip()
    env = os.environ.get("RH_TOKEN")
    if env:
        return env.strip()
    if _TOKEN_FILE.exists():
        text = _TOKEN_FILE.read_text().strip()
        if text:
            return text
    return None


def authenticate(
    token: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    mfa_code: str | None = None,
    store_session: bool = True,
) -> str:
    """Authenticate ``robin_stocks``. Returns the mode used: ``"token"`` or ``"login"``.

    If a token is available (arg, ``RH_TOKEN``, or ``.rh_token`` file) it is
    injected into the session and no password is needed. Otherwise falls back to
    an interactive ``robin_stocks`` login (prompts for missing credentials/MFA).
    """
    import robin_stocks.robinhood as rh
    import robin_stocks.robinhood.helper as helper

    resolved = _resolve_token(token)
    if resolved:
        # helper binds SESSION/LOGGED_IN by value at import time, so the auth flag
        # must be set on `helper` itself; SESSION is a shared object, so mutating
        # its headers is enough.
        helper.SESSION.headers["Authorization"] = f"Bearer {resolved}"
        helper.LOGGED_IN = True
        return "token"

    rh.login(
        username=username,
        password=password,
        mfa_code=mfa_code,
        store_session=store_session,
    )
    return "login"


# --------------------------------------------------------------------------- #
# Endpoint registry
# --------------------------------------------------------------------------- #
def _registry(account_number: str | None) -> list[dict[str, Any]]:
    """Each endpoint: ``name``, an ``incr`` flag (supports ``start_date``), and a
    ``fetch(start_date)`` closure. Account-scoped endpoints close over
    ``account_number`` (``None`` = all accounts).

    Extend the store over time by adding rows here.
    """
    import robin_stocks.robinhood as rh

    acct = account_number
    return [
        {"name": "stock_orders", "incr": True,
         "fetch": lambda sd: rh.get_all_stock_orders(account_number=acct, start_date=sd)},
        {"name": "option_orders", "incr": True,
         "fetch": lambda sd: rh.get_all_option_orders(account_number=acct, start_date=sd)},
        # Non-incremental endpoints ignore start_date (`_sd`); they're small and
        # pulled in full, then merged by id.
        {"name": "crypto_orders", "incr": False,
         "fetch": lambda _sd: rh.get_all_crypto_orders()},
        {"name": "dividends", "incr": False,
         "fetch": lambda _sd: rh.get_dividends()},
        {"name": "interest_payments", "incr": False,
         "fetch": lambda _sd: rh.get_interest_payments()},
        {"name": "margin_interest", "incr": False,
         "fetch": lambda _sd: rh.get_margin_interest()},
        {"name": "bank_transfers", "incr": False,
         "fetch": lambda _sd: rh.get_bank_transfers()},
        # NOTE: rh.get_wire_transfers() hits a broken URL (404) in robin_stocks
        # 3.4.0 — omitted. Add back if upstream fixes it.
        {"name": "card_transactions", "incr": False,
         "fetch": lambda _sd: rh.get_card_transactions()},
        {"name": "documents", "incr": False,
         "fetch": lambda _sd: rh.get_documents()},
    ]


# --------------------------------------------------------------------------- #
# Local store: load / merge / sort
# --------------------------------------------------------------------------- #
def _record_key(rec: dict) -> str:
    """Stable identity for a record — its ``id`` if present, else its JSON."""
    rid = rec.get("id")
    return rid if rid else json.dumps(rec, sort_keys=True)


def _record_date(rec: dict) -> str:
    """The record's timestamp (first present of ``_DATE_FIELDS``), else ``""``."""
    for field in _DATE_FIELDS:
        val = rec.get(field)
        if val:
            return str(val)
    return ""


def _load_store(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return loaded if isinstance(loaded, list) else []


def _merge(existing: list[dict], fetched: list[dict]) -> tuple[list[dict], int, int]:
    """Union ``existing`` and ``fetched`` by record key. Newest data wins on
    conflict. Returns ``(merged_sorted, added, updated)``.

    Sorted newest-first by timestamp then key, so the file is deterministic (a
    no-op run rewrites byte-identical content).
    """
    by_key = {_record_key(r): r for r in existing}
    added = updated = 0
    for rec in fetched:
        key = _record_key(rec)
        if key not in by_key:
            added += 1
        elif by_key[key] != rec:
            updated += 1
        by_key[key] = rec
    merged = sorted(
        by_key.values(),
        key=lambda r: (_record_date(r), _record_key(r)),
        reverse=True,
    )
    return merged, added, updated


# --------------------------------------------------------------------------- #
# Instrument resolution (instrument_id -> ticker), so ingest stays offline.
# --------------------------------------------------------------------------- #
def _resolve_instruments(
    store: dict[str, list[dict]], out_dir: Path, *, quiet: bool = False
) -> dict[str, dict]:
    """Resolve the ticker for every instrument referenced by orders/dividends.

    Builds/updates ``instruments.json`` (``instrument_id -> {symbol, name, url}``)
    in the store. Only ids not already cached cost a network call, so re-runs are
    cheap. This is the one network touch in ingest's upstream — kept here in the
    fetch step so the transactions parser is a pure offline lookup.
    """
    import re

    import robin_stocks.robinhood as rh

    need: dict[str, str] = {}
    for rec in store.get("stock_orders", []):
        iid, url = rec.get("instrument_id"), rec.get("instrument")
        if iid and url:
            need[iid] = url
    for rec in store.get("dividends", []):
        url, iid = rec.get("instrument"), rec.get("active_instrument_id")
        if not iid and url:
            m = re.search(r"/instruments/([^/]+)/", url)
            iid = m.group(1) if m else None
        if iid and url:
            need.setdefault(iid, url)

    path = out_dir / "instruments.json"
    cache: dict[str, dict] = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text())
        except json.JSONDecodeError:
            cache = {}

    added = 0
    for iid, url in need.items():
        if iid in cache and cache[iid].get("symbol"):
            continue
        try:
            info = rh.get_instrument_by_url(url)
        except Exception:  # noqa: BLE001 — a bad instrument shouldn't abort the run
            info = None
        if info and info.get("symbol"):
            cache[iid] = {
                "symbol": info.get("symbol"),
                "name": info.get("simple_name") or info.get("name"),
                "url": url,
            }
            added += 1

    if added or not path.exists():
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    if not quiet:
        print(f"  · instruments: {len(cache)} cached (+{added} resolved)")
    return cache


def fetch_positions(*, out_dir: str | Path | None = None, quiet: bool = False) -> dict:
    """Fetch current holdings via ``robin_stocks.build_holdings`` → ``positions.json``.

    This is the Robinhood positions *snapshot* (quantity + average cost + price),
    overwritten each run. It replaces the old hand-kept ``robinhood.csv`` and is the
    reconciliation target for the derived ledger.
    """
    import robin_stocks.robinhood as rh

    out = Path(out_dir) if out_dir else DIR
    out.mkdir(parents=True, exist_ok=True)
    holdings = rh.build_holdings() or {}
    (out / "positions.json").write_text(json.dumps(holdings, indent=2))
    if not quiet:
        print(f"  · positions: {len(holdings)} holdings → positions.json")
    return holdings


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_all(
    account_number: str | None = None,
    *,
    out_dir: str | Path | None = None,
    full: bool = False,
    quiet: bool = False,
) -> dict[str, dict[str, int]]:
    """Pull every history endpoint into the local store, incrementally.

    Call :func:`authenticate` first. Each endpoint is fetched independently — a
    failure on one (e.g. crypto on a no-crypto account) is logged and skipped, not
    fatal. For incremental endpoints, only records updated since the latest stored
    one are pulled unless ``full=True``.

    Writes ``<name>.json`` per endpoint plus a combined ``history.json`` envelope
    (account, timestamp, per-endpoint counts). Returns a per-endpoint summary
    ``{name: {"total", "added", "updated"}}``.
    """
    out = Path(out_dir) if out_dir else HISTORY_DIR
    out.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict[str, int]] = {}
    store: dict[str, list[dict]] = {}

    for ep in _registry(account_number):
        name, path = ep["name"], out / f"{ep['name']}.json"
        existing = _load_store(path)

        # Incremental high-water mark: re-pull from the newest stored timestamp
        # (>= boundary is re-included, then deduped on merge).
        start_date = None
        if ep["incr"] and existing and not full:
            start_date = max((_record_date(r) for r in existing), default="") or None

        try:
            fetched = list(ep["fetch"](start_date) or [])
        except Exception as exc:  # noqa: BLE001 — one bad endpoint shouldn't abort
            if not quiet:
                print(f"  ! {name}: skipped ({type(exc).__name__}: {exc})")
            if existing:
                store[name] = existing
                summary[name] = {"total": len(existing), "added": 0, "updated": 0}
            continue

        merged, added, updated = _merge(existing, fetched)
        store[name] = merged
        summary[name] = {"total": len(merged), "added": added, "updated": updated}

        # Only rewrite when something changed, so a no-op run leaves files untouched.
        if added or updated or not path.exists():
            path.write_text(json.dumps(merged, indent=2))

        if not quiet:
            scope = "incr" if start_date else "full"
            print(f"  · {name}: {len(merged)} total (+{added} new, ~{updated} updated, {scope})")

    # Resolve instrument_id -> ticker (network; only new ids) so ingest is offline.
    try:
        instruments = _resolve_instruments(store, out, quiet=quiet)
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        if not quiet:
            print(f"  ! instruments: skipped ({type(exc).__name__}: {exc})")
        instruments = {}

    # Current holdings snapshot (quantity + cost basis), the RH reconciliation target.
    try:
        fetch_positions(quiet=quiet)
    except Exception as exc:  # noqa: BLE001 — best-effort
        if not quiet:
            print(f"  ! positions: skipped ({type(exc).__name__}: {exc})")

    counts = {name: len(recs) for name, recs in store.items()}
    counts["instruments"] = len(instruments)
    envelope = {
        "account_number": account_number,
        "fetched_at": _dt.datetime.now().astimezone().isoformat(),
        "counts": counts,
        "data": store,
    }
    (out / "history.json").write_text(json.dumps(envelope, indent=2))

    if not quiet:
        tot = sum(s["total"] for s in summary.values())
        new = sum(s["added"] for s in summary.values())
        print(f"  → store at {out}: {tot} records ({new} new this run)")

    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="invest-robinhood-fetch",
        description="Download Robinhood transaction history to a local JSON store.",
    )
    parser.add_argument(
        "--account", default=None,
        help="restrict account-scoped endpoints to this account number "
             "(default: all accounts)",
    )
    parser.add_argument(
        "--token", default=None,
        help="bearer token; otherwise RH_TOKEN env or data/raw/robinhood/.rh_token, "
             "otherwise interactive login",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="force a complete re-pull instead of incremental",
    )
    parser.add_argument("--out-dir", default=None, help="output dir for the JSON store")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    mode = authenticate(token=args.token)
    if not args.quiet:
        print(f"authenticated via {mode}")
    fetch_all(
        account_number=args.account,
        out_dir=args.out_dir,
        full=args.full,
        quiet=args.quiet,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
