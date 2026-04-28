// Roger background service worker.
// Forwards the popup's payload to Theo's JSON API. Doing the fetch from
// the background (extension origin) avoids any page-origin / mixed-content
// issues you'd hit if the popup or content script POSTed directly.

const THEO_ENDPOINTS = [
  "http://localhost:5111/products/new_from_browser",
  "http://127.0.0.1:5111/products/new_from_browser",
];

async function sendToTheo(payload) {
  let lastError;
  for (const url of THEO_ENDPOINTS) {
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const text = await resp.text();
      let data;
      try { data = JSON.parse(text); } catch { data = { ok: resp.ok, raw: text }; }
      if (!resp.ok) {
        return { ok: false, error: data.error || `HTTP ${resp.status}` };
      }
      return data.ok === false
        ? { ok: false, error: data.error || "Server rejected the request" }
        : { ok: true, data };
    } catch (e) {
      lastError = e;
    }
  }
  return { ok: false, error: (lastError && lastError.message) || "Theo is not reachable on :5111" };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "roger:send") {
    sendToTheo(msg.payload).then(sendResponse);
    return true; // keep the message channel open for async response
  }
});
