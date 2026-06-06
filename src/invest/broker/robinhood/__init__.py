"""Robinhood broker: positions + transactions parsers, the API fetch, raw paths,
and its ``Broker`` descriptor.
"""

from pathlib import Path

from invest import config
from invest.broker.base import Broker, latest_csv

# Paths first — submodules below import some of these (e.g. transactions imports
# INSTRUMENTS_PATH), so they must exist before those imports run.
DIR = config.RAW_DIR / "robinhood"
HISTORY_DIR = DIR / "history"                     # incremental transaction store
INSTRUMENTS_PATH = HISTORY_DIR / "instruments.json"  # instrument_id -> ticker cache
POSITIONS_PATH = DIR / "positions.json"           # fetched current-holdings snapshot
TOKEN_PATH = DIR / ".rh_token"                    # bearer token (gitignored)

from invest.broker.robinhood import positions, transactions  # noqa: E402  (after paths)


def _tx_locator(subdir: Path) -> Path | None:
    """The Robinhood history store directory itself (its loader reads the dir)."""
    hist = subdir / "history"
    return hist if hist.exists() else None


def _pos_locator(subdir: Path) -> Path | None:
    """Fetched ``positions.json`` (preferred), else a legacy manual ``*.csv`` list."""
    fetched = subdir / "positions.json"
    return fetched if fetched.exists() else latest_csv(subdir)


BROKER = Broker(
    name="robinhood",
    subdir="robinhood",
    positions_loader=positions.load_positions,
    positions_locator=_pos_locator,
    transactions_locator=_tx_locator,
    transactions_loader=transactions.load_transactions,
    # build_holdings() is account-agnostic (one brokerage) and stocks-only:
    snapshot_has_accounts=False,
    has_cash_snapshot=False,
)
