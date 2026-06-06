// This function is serialized and injected into the Fidelity page's MAIN world,
// where it runs with your live session. It reads the captured getTransactions
// templates (one per distinct query variant), replays each across date windows,
// harvests every array field returned, and downloads one JSON file.
// Must be fully self-contained (no references to popup scope).
function fidReplay(years, windowDays) {
  const tplMap = window.__fidExportTemplates || {};
  const templates = Object.values(tplMap);
  if (!templates.length) {
    return { error: "No getTransactions requests captured yet. Reload the Activity page, let it fully load, then try again." };
  }

  const ENDPOINT = "https://digital.fidelity.com/ftgw/digital/webactivity/api/graphql?ref_at=activity";
  // Top-level getTransactions fields that aren't transaction rows.
  const SKIP = new Set(["__typename", "sysMsgs", "backendStatus", "footNote", "footNoteTransfer"]);

  const now = Math.floor(Date.now() / 1000);
  const start = now - Math.round(years * 365.25 * 24 * 3600);
  const win = Math.round(windowDays * 24 * 3600);
  const windows = [];
  for (let from = start; from < now; from += win) windows.push([from, Math.min(from + win, now)]);

  const data = {};            // field name -> array of rows
  const errors = [];
  const day = (s) => new Date(s * 1000).toISOString().slice(0, 10);

  function harvest(json, f, t, variant) {
    const g = json && json.data && json.data.getTransactions;
    if (!g) return 0;
    let added = 0;
    for (const key of Object.keys(g)) {
      if (SKIP.has(key)) continue;
      if (Array.isArray(g[key])) {
        if (!data[key]) data[key] = [];
        for (const row of g[key]) {
          data[key].push(Object.assign({ _kind: key, _variant: variant, _window_from: f, _window_to: t }, row));
          added++;
        }
      }
    }
    return added;
  }

  async function run() {
    for (let i = 0; i < windows.length; i++) {
      const [f, t] = windows[i];
      let windowAdded = 0;

      for (let v = 0; v < templates.length; v++) {
        let base;
        try { base = JSON.parse(templates[v]); } catch (e) { errors.push("variant " + v + " unparseable: " + e.message); continue; }
        base.variables = base.variables || {};
        base.variables.searchCriteriaDetail = Object.assign(
          {}, base.variables.searchCriteriaDetail, { txnFromDate: String(f), txnToDate: String(t) }
        );

        let resp;
        try {
          resp = await fetch(ENDPOINT, {
            method: "POST",
            credentials: "include",
            headers: {
              "content-type": "application/json",
              "accept": "*/*",
              "apollographql-client-name": "activity",
              "apollographql-client-version": "0.0.1",
            },
            body: JSON.stringify(base),
          });
        } catch (e) {
          errors.push("window " + (i + 1) + " variant " + v + ": network " + e.message);
          continue;
        }
        if (!resp.ok) {
          errors.push("window " + (i + 1) + " (" + day(f) + ") variant " + v + ": HTTP " + resp.status);
          continue;
        }
        let json;
        try { json = await resp.json(); } catch (e) { errors.push("window " + (i + 1) + " variant " + v + ": bad JSON"); continue; }
        windowAdded += harvest(json, f, t, v);

        await new Promise((r) => setTimeout(r, 250)); // be gentle
      }

      console.log("[FidExport] window " + (i + 1) + "/" + windows.length + " " + day(f) + ".." + day(t) + " → +" + windowAdded);
    }

    const counts = {};
    for (const k of Object.keys(data)) counts[k] = data[k].length;
    const total = Object.values(counts).reduce((a, b) => a + b, 0);

    const out = {
      meta: {
        fetched_at: new Date().toISOString(),
        years, windowDays, windows: windows.length,
        variants: templates.length,
        counts, total,
        errors: errors.slice(0, 50),
      },
      data,
    };

    const blob = new Blob([JSON.stringify(out, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "fidelity_history.json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 15000);

    return { ok: true, counts, total, windows: windows.length, variants: templates.length, errorCount: errors.length };
  }

  return run();
}

const el = (id) => document.getElementById(id);

el("go").addEventListener("click", async () => {
  const years = parseFloat(el("years").value) || 5;
  const windowDays = Math.min(parseInt(el("win").value, 10) || 90, 93);
  el("status").textContent = "Working… keep this Activity tab open.";

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https:\/\/digital\.fidelity\.com\//.test(tab.url || "")) {
    el("status").textContent = "Open your Fidelity Activity page (digital.fidelity.com) in the active tab first.";
    return;
  }

  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: fidReplay,
      args: [years, windowDays],
    });
    const r = res && res.result;
    if (!r) { el("status").textContent = "No result — the page may have blocked injection."; return; }
    if (r.error) { el("status").textContent = "⚠ " + r.error; return; }
    el("status").textContent =
      "✅ Downloaded fidelity_history.json\n" +
      r.total + " records · " + r.windows + " windows · " + r.variants + " query variants" +
      (r.errorCount ? "\n⚠ " + r.errorCount + " request(s) failed (see meta.errors in the file)" : "") +
      "\n" + Object.entries(r.counts).map(([k, v]) => "  " + k + ": " + v).join("\n");
  } catch (e) {
    el("status").textContent = "Error: " + e.message;
  }
});
