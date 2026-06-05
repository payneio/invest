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
        "price_source": "fidelity_csv",
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
            "price_source": default.get("price_source", "fidelity_csv"),
        },
        benchmarks=list(raw.get("benchmarks") or []),
    )


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
    df["unclassified"] = df["symbol"].map(lambda s: s not in smap.symbols)
    return df
