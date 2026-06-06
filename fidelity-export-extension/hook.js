// Runs in the PAGE's main world at document_start. Its job: watch the requests
// the Activity page makes and stash the body of EVERY distinct `getTransactions`
// GraphQL call. The page fires several variants under the same operation name —
// one selecting orders/historys, another transfers/billpays, etc. — so we key by
// the query string to keep one of each. The popup later replays all of them across
// date windows. We never hardcode account numbers; the captured bodies carry your
// real accounts and the real queries.
(() => {
  const TARGET = "/webactivity/api/graphql";
  window.__fidExportTemplates = window.__fidExportTemplates || {};

  function capture(bodyStr) {
    try {
      if (typeof bodyStr !== "string" || !bodyStr.includes("getTransactions")) return;
      const parsed = JSON.parse(bodyStr);
      const q = parsed && parsed.query;
      if (q) {
        window.__fidExportTemplates[q] = bodyStr; // dedupe by selection set
        window.__fidExportCapturedAt = Date.now();
      }
    } catch (_) {}
  }

  // Hook fetch (Apollo uses fetch by default).
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input && input.url;
      if (url && url.includes(TARGET) && init && init.body) capture(init.body);
    } catch (_) {}
    return origFetch.apply(this, arguments);
  };

  // Hook XHR too, just in case.
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__fidUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (this.__fidUrl && String(this.__fidUrl).includes(TARGET)) capture(body);
    } catch (_) {}
    return origSend.apply(this, arguments);
  };
})();
