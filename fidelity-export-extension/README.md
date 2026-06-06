# Fidelity Activity Exporter (unpacked Chrome extension)

Exports your Fidelity transaction history — **all accounts, multiple years** — to a
single `fidelity_history.json`, by calling Fidelity's own `getTransactions` GraphQL
API from inside the Activity page (where your session and Akamai bot-protection are
valid). No cookies are copied, no credentials are stored, and your account numbers
are never written to disk — the extension captures the page's own request and just
replays it across date windows.

## Load it (one time, ~30 seconds)

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked** and select this `fidelity-export-extension/` folder.
4. The "Fidelity Activity Exporter" icon appears in your toolbar (pin it if you like).

No store, no packaging, no signing — "Load unpacked" runs the folder as-is. After
editing any file here, click the **↻ reload** icon on the extension card to pick up
changes.

## Use it

1. Go to your Fidelity **Activity** page
   (`https://digital.fidelity.com/ftgw/digital/portfolio/activity`) and let it load
   once — that's what lets the extension learn your accounts.
2. Click the extension icon. Set **Years back** (default 5) and **Window** (default
   90 days — Fidelity caps a single query near 93).
3. Click **Export to JSON**. It walks the windows (progress prints to the page's
   DevTools console) and downloads `fidelity_history.json` to your Downloads folder.

Then move it into the repo for ingest:

```bash
mv ~/Downloads/fidelity_history.json data/raw/fidelity/history/
```

(`data/raw/**/*.json` is gitignored, so it won't be committed.)

## How it works

- The Activity page fires **several** `getTransactions` calls under one operation
  name, each selecting a different slice (one `orders`/`historys`, another
  `transfers`/`billpays`, …). `hook.js` runs in the page's main world at
  document-start and records the body of **every distinct** variant (keyed by its
  query), carrying your real `acctIdList`/`acctDetailList`.
- `popup.js` injects a function that, for each 90-day window, replays **all**
  captured variants, overriding only `txnFromDate`/`txnToDate`, and harvests **every
  array field** the response returns. Output is
  `{ meta, data: { orders: [...], historys: [...], transfers: [...], billpays: [...] } }`,
  each row tagged with `_kind` / `_variant` / `_window_from` / `_window_to`. The
  ingest parser dedupes overlaps later.

## Notes / limits

- The session must be live — if a window returns a non-200, reload the Activity page
  and re-run (it'll tell you which window failed).
- A single window is limited to ~93 days; that's why we loop. Transfers (`historyToa`)
  can be capped per query at very high counts — not a concern for 90-day windows.
- Records carry `_window_from`/`_window_to`/`_kind` tags; overlapping/duplicate rows
  across windows are de-duplicated later by the ingest parser.
