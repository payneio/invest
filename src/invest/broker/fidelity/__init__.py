"""Fidelity broker: positions + transactions parsers, raw paths, and its ``Broker``.

Fidelity has no Python fetch — the Chrome extension in ``fidelity-export-extension/``
exports the Activity JSON by hand; these are the locations the pipeline reads.
"""

from pathlib import Path

from invest import config
from invest.broker.base import Broker, latest_json

DIR = config.RAW_DIR / "fidelity"
HISTORY_DIR = DIR / "history"  # canonical drop spot for the extension's JSON

from invest.broker.fidelity import positions, transactions  # noqa: E402  (after paths)

# Money-market sweeps modeled as USD cash. (Also classifiable via symbol_map.yaml
# asset_class: cash; this is Fidelity's built-in default set.)
CASH_SYMBOLS = frozenset({"SPAXX", "FDRXX", "FCASH", "SPRXX", "FZFXX", "FZDXX", "FGXX"})


def _tx_locator(subdir: Path) -> Path | None:
    """Newest JSON in ``<subdir>/history/`` (canonical), else one in the subdir root."""
    hist = subdir / "history"
    if hist.exists() and (p := latest_json(hist)):
        return p
    direct = subdir / "fidelity_history.json"
    if direct.exists():
        return direct
    return latest_json(subdir)


BROKER = Broker(
    name="fidelity",
    subdir="fidelity",
    positions_loader=positions.load_positions,
    transactions_locator=_tx_locator,
    transactions_loader=transactions.load_transactions,
    cash_symbols=CASH_SYMBOLS,
    # Fidelity defaults: positions CSV carries account numbers and a cash row.
)
