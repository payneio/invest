"""Apply the symbol-mapping YAML to parsed positions.

The map is the classification + price-source truth. This module loads it and
joins it onto positions, filling an ``asset_class`` and ``price_source`` for
every row (falling back to the configured default and flagging anything the user
hasn't classified yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from . import config


@dataclass(frozen=True)
class SymbolMap:
    """Parsed symbol_map.yaml."""

    symbols: dict[str, dict] = field(default_factory=dict)
    default: dict = field(default_factory=lambda: {
        "asset_class": "unclassified",
        "price_source": "snapshot",
    })
    benchmarks: list[str] = field(default_factory=list)

    def entry(self, symbol: str) -> dict:
        """Return the map entry for a symbol, falling back to the default."""
        return self.symbols.get(symbol, self.default)

    def yfinance_symbols(self) -> list[str]:
        """Symbols whose canonical price comes from Yahoo (their ``yf_symbol``)."""
        out = []
        for entry in self.symbols.values():
            if entry.get("price_source") == "yfinance":
                out.append(entry.get("yf_symbol"))
        return sorted({s for s in out if s})

    def history_symbols(self) -> list[str]:
        """Yahoo tickers usable for HISTORY: real yf_symbols plus any yf_proxy."""
        out = list(self.yfinance_symbols())
        for entry in self.symbols.values():
            proxy = entry.get("yf_proxy")
            if proxy:
                out.append(proxy)
        return sorted(set(out) | set(self.benchmarks))


def load_symbol_map(path: str | Path | None = None) -> SymbolMap:
    """Load ``config/symbol_map.yaml`` (or a given path)."""
    path = Path(path) if path else config.SYMBOL_MAP_PATH
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    # YAML keys for CUSIPs are quoted strings; normalize all keys to str.
    symbols = {str(k): (v or {}) for k, v in (raw.get("symbols") or {}).items()}
    default = raw.get("default") or {}
    return SymbolMap(
        symbols=symbols,
        default={
            "asset_class": default.get("asset_class", "unclassified"),
            "price_source": default.get("price_source", "snapshot"),
        },
        benchmarks=list(raw.get("benchmarks") or []),
    )


def _raw_accounts(path: str | Path | None = None) -> dict[str, dict | str]:
    """The raw ``account_number -> (name | mapping)`` dict from accounts.yaml."""
    path = Path(path) if path else config.ACCOUNTS_MAP_PATH
    if not Path(path).exists():
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("accounts", raw) or {}


def load_accounts(path: str | Path | None = None) -> dict[str, str]:
    """Load ``config/accounts.yaml`` — an ``account_number -> human name`` map.

    Returns an empty dict if the file is absent, so account naming is optional;
    callers fall back to the raw account number. Keys are normalized to str so
    numeric-looking account numbers stay strings (e.g. ``"12345678"``). Entry values
    may be a bare name string or a mapping with a ``name`` key (see accounts.yaml).
    """
    out: dict[str, str] = {}
    for num, val in _raw_accounts(path).items():
        name = val.get("name") if isinstance(val, dict) else val
        out[str(num)] = str(name if name is not None else num)
    return out


def normalize_account(name: object) -> str:
    """Lowercase alphanumeric key for matching account labels across forms —
    e.g. ``"Rollover IRA"``, ``"RolloverIRA"`` and ``"rollover_ira"`` all collapse
    to ``"rolloveria"``. Lets us join tax metadata onto the ledger's CamelCase
    account names regardless of how the name was originally written."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


# Default tax treatment per account ``type`` — the inference used when an entry
# declares a type but no explicit ``tax`` (and the keyword fallback below).
_TAX_BY_TYPE: dict[str, str] = {
    "individual": "taxable", "cash_management": "taxable", "crypto": "taxable",
    "ira_traditional": "tax_deferred", "401k": "tax_deferred", "keogh": "tax_deferred",
    "ira_roth": "tax_free", "hsa": "tax_free",
}


def _infer_type_tax(name: str) -> tuple[str, str]:
    """Guess ``(type, tax)`` from an account name's keywords (last-resort default)."""
    n = name.lower()
    if "roth" in n:
        return "ira_roth", "tax_free"
    if "hsa" in n:
        return "hsa", "tax_free"
    if "401" in n or "403" in n:
        return "401k", "tax_deferred"
    if "keogh" in n or "sep" in n or "profit sharing" in n:
        return "keogh", "tax_deferred"
    if "ira" in n:
        return "ira_traditional", "tax_deferred"
    if "crypto" in n:
        return "crypto", "taxable"
    return "individual", "taxable"


def load_account_meta(path: str | Path | None = None) -> dict[str, dict]:
    """Account metadata keyed by :func:`normalize_account` of the display name.

    Each value is ``{account_number, name, type, tax}``. Explicit ``type``/``tax``
    in accounts.yaml win; otherwise they're inferred from the type's default tax,
    else from the name's keywords. Keyed by normalized name (not account number) so
    it joins onto the ledger-derived ``account_name`` the rest of the pipeline uses.
    """
    out: dict[str, dict] = {}
    for num, val in _raw_accounts(path).items():
        if isinstance(val, dict):
            name = str(val.get("name", num))
            atype = val.get("type")
            tax = val.get("tax")
        else:
            name, atype, tax = str(val), None, None
        if not atype or not tax:
            inferred_type, inferred_tax = _infer_type_tax(name)
            atype = atype or inferred_type
            tax = tax or _TAX_BY_TYPE.get(atype, inferred_tax)
        out[normalize_account(name)] = {
            "account_number": str(num), "name": name, "type": atype, "tax": tax,
        }
    return out


def classify_positions(positions: pd.DataFrame, smap: SymbolMap) -> pd.DataFrame:
    """Add ``asset_class``, ``price_source``, ``yf_symbol`` columns to positions.

    Adds an ``unclassified`` boolean so you can spot new holdings the map doesn't
    cover yet.
    """
    df = positions.copy()

    def _field(symbol: str, key: str, default=None):
        return smap.entry(symbol).get(key, default)

    df["asset_class"] = df["symbol"].map(
        lambda s: _field(s, "asset_class", smap.default["asset_class"])
    )
    df["price_source"] = df["symbol"].map(
        lambda s: _field(s, "price_source", smap.default["price_source"])
    )
    df["yf_symbol"] = df["symbol"].map(lambda s: _field(s, "yf_symbol"))
    df["yf_proxy"] = df["symbol"].map(lambda s: _field(s, "yf_proxy"))
    df["manual_price"] = df["symbol"].map(lambda s: _field(s, "manual_price"))
    df["expense_ratio"] = df["symbol"].map(lambda s: _field(s, "expense_ratio"))
    df["unclassified"] = df["symbol"].map(lambda s: s not in smap.symbols)
    return df
