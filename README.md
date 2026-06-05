# invest

Personal portfolio analysis. **Fidelity is the source of truth for positions**
(quantities, accounts, cost basis, cash); **yfinance enriches** with market data
(prices, history, dividends, benchmarks). A hand-maintained **symbol-mapping
YAML** classifies every holding and declares where its price comes from — the
piece that makes the whole thing robust against money-market funds and
proprietary 401k pools that have no public quote.

```
broker exports    → positions truth      (data/raw/<broker>/*.csv)
symbol_map.yaml   → classification truth (config/symbol_map.yaml)
yfinance          → price/history        (pulled on ingest)
pandas            → analysis             (notebooks/portfolio.ipynb)
Parquet           → cleaned local store  (data/processed/*.parquet)
```

## Setup

Uses [uv](https://docs.astral.sh/uv/). The repo is a uv package.

```bash
uv sync --extra notebook        # create .venv and install everything
```

## Use

1. Drop your broker exports under `data/raw/<broker>/` (newest `*.csv` per broker
   is used; all gitignored):
   - **Fidelity** — the standard "Portfolio Positions" CSV → `data/raw/fidelity/`.
   - **Robinhood** — a manual `SYMBOL QUANTITY` list (Robinhood has no clean
     positions export) → `data/raw/robinhood/`, e.g.:

     ```
     GOOG 276
     NVDA 550
     ```

     These are priced entirely from yfinance, so each symbol must be classified
     `price_source: yfinance` in the map; there's no cost basis, so gain/loss is
     not reported for them.
2. Run the ingest pipeline — parses positions, pulls prices, writes Parquet:

   ```bash
   uv run invest-ingest
   ```

   Useful flags: `--no-network` (skip yfinance, use Fidelity prices only),
   `--history-period 3y`, `--quiet`.
3. Open the analysis notebook:

   ```bash
   uv run jupyter lab notebooks/portfolio.ipynb
   ```

## Layout

| Path | What |
|------|------|
| `config/symbol_map.yaml` | classification + price-source per symbol |
| `src/invest/fidelity.py` | parse + clean the Fidelity export |
| `src/invest/robinhood.py`| parse the manual Robinhood holdings list |
| `src/invest/brokers.py`  | broker registry → one normalized positions schema |
| `src/invest/mapping.py`  | load the YAML, classify positions, resolve prices |
| `src/invest/prices.py`   | yfinance price + history enrichment |
| `src/invest/pipeline.py` | orchestrate → Parquet (`invest-ingest` CLI) |
| `src/invest/analysis.py` | allocation, concentration, risk helpers |
| `notebooks/portfolio.ipynb` | the analysis surface |
| `data/processed/*.parquet`  | regenerated outputs (gitignored) |

## Caveats (inherited from yfinance / the data)

- Money-market funds (SPAXX/FDRXX/FCASH) are pinned to $1.00 NAV via the map.
- Proprietary 401k pools (CUSIP symbols) have no public quote — priced from the
  Fidelity export; a `yf_proxy` can stand in for *history* approximation only.
- No tax-lot or transaction truth here — positions only.
- yfinance is unofficial; treat prices as research-grade, not trading-grade.
