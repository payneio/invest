Yes — yfinance is a good default for a Jupyter-based personal portfolio notebook. Use it for prices, dividends, splits, basic history, not as your canonical source of positions or tax lots.

yfinance is an open-source Python package that fetches Yahoo Finance market data into pandas, including historical prices via download() and per-ticker data via Ticker. Its docs expose Ticker, Tickers, download, search, calendar, and market helpers. 

Important caveat: it is not affiliated with Yahoo, uses Yahoo’s publicly available endpoints, and is intended for personal/research use; the project itself tells users to consult Yahoo’s terms for data rights. 

How I’d use it with your Fidelity CSV

Use Fidelity as the truth for:

positions quantities accounts cost basis current value cash / sweep positions 

Use yfinance for:

latest prices historical prices dividends splits benchmark returns volatility / drawdown analysis 

Minimal pattern

import pandas as pd import yfinance as yf positions = pd.read_csv("../data/raw/fidelity/Portfolio_Positions_Jun-04-2026.csv") # Clean Fidelity symbols positions = positions[positions["Symbol"].notna()].copy() positions["Symbol"] = ( positions["Symbol"] .astype(str) .str.replace("*", "", regex=False) .str.strip() ) # Only request public tickers exclude = {"SPAXX", "FDRXX", "FCASH", "09261F614", "31617E471", "59515R401"} tickers = sorted(set(positions["Symbol"]) - exclude) prices = yf.download( tickers, period="1y", auto_adjust=True, progress=False, )["Close"] prices.tail() 

Latest price map

latest_prices = prices.ffill().iloc[-1] positions["Market Price yf"] = positions["Symbol"].map(latest_prices) positions[["Account Name", "Symbol", "Quantity", "Last Price", "Market Price yf"]] 

Portfolio history approximation

This assumes today’s share counts held over the whole period, so it is not true performance. It is still useful for current-risk analysis.

qty = positions.set_index("Symbol")["Quantity"] public_qty = qty[qty.index.isin(tickers)] portfolio_value_history = prices[public_qty.index].mul(public_qty, axis=1).sum(axis=1) portfolio_value_history.plot(title="Current Holdings Backtest") 

Better: benchmark and concentration analysis

bench = yf.download( ["SPY", "QQQ", "VTI", "BND"], period="3y", auto_adjust=True, progress=False, )["Close"] returns = bench.pct_change().dropna() summary = pd.DataFrame({ "Annual Return": returns.mean() * 252, "Annual Vol": returns.std() * 252**0.5, "Max Drawdown": (bench / bench.cummax() - 1).min() }) summary 

Biggest limitations

yfinance will not handle your Fidelity export perfectly because:

money market funds like SPAXX and FDRXX may need special treatment

proprietary 401k funds may not have public Yahoo symbols

ticker metadata can be stale or incomplete

intraday/realtime data is not something I would trust for trading decisions

no tax-lot or transaction truth

no guaranteed API stability

My recommendation

Use this stack:

Fidelity CSV → positions truth manual mapping YAML → classification truth yfinance → price/history enrichment Jupyter/pandas → analysis Parquet → cleaned local data store 

For your case, yfinance is useful, but the highest-leverage thing is a symbol mapping file:

SPAXX: asset_class: cash price_source: manual manual_price: 1.00 FDRXX: asset_class: cash price_source: manual manual_price: 1.00 09261F614: asset_class: target_date price_source: fidelity_csv MSFT: asset_class: employer_stock price_source: yfinance yf_symbol: MSFT 

That mapping file will make the whole notebook robust.
