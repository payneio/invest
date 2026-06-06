# invest

Personal portfolio analysis built on a single source of truth: a **curated
[beancount](https://beancount.github.io/) ledger** (`ledger/`). Fetched broker data
*generates* the machine part of the ledger; **one-time hand fixups** (opening lots,
splits, mergers) complete it until **derived positions reconcile to the broker
snapshot**. Positions are then *derived* from the ledger — which also unlocks
**point-in-time** holdings and **counterfactual** ("what if I'd never sold X")
analysis. A pandas bridge flattens the ledger to Parquet so analysis stays
data-science-native; **yfinance** prices it; a **symbol-map YAML** classifies it.

```
FETCH (network/auth, occasional)        EMIT + CURATE                 DERIVE + ANALYZE
  invest-robinhood-fetch → robinhood JSON ┐                                ┌ positions.parquet
  extension         → fidelity JSON  ├→ invest-ingest → ledger/*.beancount ┤ transactions.parquet
  broker positions CSV / manual list ┘   (auto) + fixups (you)  →  load  └ prices.parquet → notebook
                                                    ▲ reconcile to snapshot ─┘
```

- The ledger is **truth**; the broker snapshot becomes a **reconciliation target**.
- `invest-ingest` regenerates the auto ledger, loads it, derives + prices positions
  (cash from the snapshot), and flattens transactions — all to `data/processed/*.parquet`.
- `ledger/` is gitignored (holds your holdings); curate `ledger/fixups.beancount` by hand.

## Setup

Uses [uv](https://docs.astral.sh/uv/). The repo is a uv package.

```bash
uv sync --extra notebook        # create .venv and install everything
uv run nbstripout --install     # strip notebook outputs from commits (once per clone)
```

The second command wires a git *clean filter* (via the committed `.gitattributes`) so
notebook cell outputs — which contain holdings and balances — are stripped from what
git stores, while your local working copy keeps them rendered. It's a no-op on already
clean checkouts; re-run it after a fresh `git clone`.

## Use

Notebooks drive everything — you rarely touch the CLIs directly. Broker-specific
fetching is isolated in one notebook per broker; the rest is generic:

- **`notebooks/broker-robinhood.ipynb`** — Robinhood token-expiry check + fetch
  (`invest-robinhood-fetch`: incremental history **and** `positions.json` in one call).
- **`notebooks/broker-fidelity.ipynb`** — Fidelity's manual export steps (no API) and a
  cell that confirms which files the pipeline will pick up.
- **`notebooks/refresh.ipynb`** — the **generic operations console**: freshness
  dashboard, the rebuild cell, a reconciliation report, and a curation helper that
  drafts the `fixups.beancount` entries you need. No broker-specific code lives here.
- **`notebooks/portfolio.ipynb`** — the **analysis overview** (allocation, income, realized
  gains, point-in-time, counterfactual), reading the derived Parquet.
- **Themed deep-dives** (interactive Plotly — hover, zoom, drill-in):
  - **`performance-and-risk.ipynb`** — growth-of-$1 vs benchmarks, value-by-class area,
    underwater drawdowns, rolling volatility & beta, a risk/reward bubble scatter.
  - **`allocation-and-concentration.ipynb`** — broker→account→class→holding sunburst &
    treemap, concentration gauges (HHI / effective-N), and concentration *drift* over time.
  - **`correlations-and-diversification.ipynb`** — clustered correlation heatmap,
    diversification ratio, risk-contribution-vs-weight, rolling correlation to the market.
  - **`income-dividends-projections.ipynb`** — dividend calendar heatmap, income by year,
    forward yield, and a 5-year Monte-Carlo fan of portfolio outcomes.
- **Opportunity finders** (`opportunities-*.ipynb`) — candidate generators (not advice),
  each tied to a dollar figure, built on the `invest.opportunities` library:
  - **`opportunities-tax-loss-harvesting.ipynb`** — taxable lots underwater, loss sized
    against YTD realized gains, with wash-sale flags and a worklist of lots.
  - **`opportunities-asset-location.ipynb`** — asset-class × tax-treatment map,
    tax-inefficient holdings in taxable accounts, and an idle-vs-earning cash audit.
  - **`opportunities-concentration-rebalancing.ipynb`** — drift from a target you set, the
    trades to close it, and a risk-reduction-per-dollar trim guide.
  - **`opportunities-cost-performance.ipynb`** — fund expense drag and an active-vs-index
    return gap (swap candidates).
  - **`opportunities-lot-sale-planner.ipynb`** — need to raise cash? Picks the specific
    tax lots to sell at **minimum tax** (spec-ID worklist) and shows the saving vs. the
    FIFO default.

  `refresh.ipynb` includes an **Opportunities dashboard** cell — a one-glance digest
  (harvestable losses, cash, asset location, concentration, fees) you see every refresh.

  Tax analyses read each account's `tax` treatment from `config/accounts.yaml` (taxable /
  tax_deferred / tax_free) — the scaffolded values there were inferred from account names;
  **confirm them** before trusting the tax output.

```bash
uv run jupyter lab notebooks/   # then: fetch each broker → refresh → portfolio + deep-dives
```

**The loop:** ① fetch each broker (the `broker-*.ipynb` notebooks) → ② run
`refresh.ipynb` top to bottom (rebuild + verify) → ③ analyze in `portfolio.ipynb`.

The broker notebooks cover the few manual things:

1. **Robinhood token** (`broker-robinhood.ipynb`) — grab a fresh
   `authorization: Bearer …` header from any `api.robinhood.com` request in Chrome
   DevTools into `data/raw/robinhood/.rh_token` (the JWT lasts ~weeks; the token cell
   shows days left), then run the fetch cell.
2. **Fidelity exports** (`broker-fidelity.ipynb`, no API) — datestamp both into
   `data/raw/fidelity/`:
   - Positions CSV → `20260805_fidelity_positions.csv`
   - Activity JSON via the unpacked Chrome extension in `fidelity-export-extension/`
     (`chrome://extensions` → Developer mode → Load unpacked; it runs in the page and
     loops 90-day windows past the ~93-day API cap) → `20260805_fidelity_history.json`.
     See [`fidelity-export-extension/README.md`](fidelity-export-extension/README.md).
3. **Curate** `ledger/fixups.beancount` for any holding that doesn't reconcile (opening
   lot, split, merger). `refresh.ipynb` drafts the directive; paste it, refine, re-derive.

Optional, gitignored config: `config/accounts.yaml` (account_number → name) and
`config/symbol_map.yaml` (asset-class + price source per symbol).

### Under the hood (the CLIs the notebook drives)

- `invest-robinhood-fetch` — pull Robinhood history + positions (token-authed, incremental).
- `invest-ingest` — regenerate the ledger from raw, derive + price positions, flatten
  transactions, reconcile, write `data/processed/*.parquet`. Flags: `--no-network`
  (snapshot prices only), `--no-emit` (derive from the current hand-curated ledger),
  `--no-transactions`, `--quiet`.

**Why re-running is safe (idempotent):** Robinhood is one incremental store (merge by id;
`positions.json` overwritten — a no-op when nothing changed; don't datestamp it). Fidelity
exports are full snapshots — datestamp them, newest-by-name wins. The ledger regenerates
deterministically from the current raw files each `invest-ingest` (same inputs →
byte-identical ledger), while your `ledger/fixups.beancount` is never overwritten.

## Caveats

- Money-market funds (SPAXX/FDRXX/FCASH) are modeled as USD cash; cash balances come
  from the broker snapshot (the ledger window can't reproduce them).
- Proprietary 401k pools (CUSIP symbols) have no public quote — priced from the
  Fidelity snapshot; a `yf_proxy` can stand in for *history* approximation only.
- yfinance is unofficial; treat prices as research-grade, not trading-grade.
- Derivation is only as good as the curation: positions opened before the fetched
  window need an opening-lot fixup (auto-proposed), and splits/mergers need a hand
  fixup. Realized gains come from beancount's own lot tracking, so they're accurate
  once those lots are curated.
