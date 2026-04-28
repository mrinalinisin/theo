// Service worker: owns the network call to Theo.
// Popup posts a message; we POST to /products/new_from_browser and reply.

const THEO_BASE = "http://localhost:5000";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type !== "save") return false;

  (async () => {
    try {
      const resp = await fetch(`${THEO_BASE}/products/new_from_browser`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(msg.payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        sendResponse({ ok: false, error: data.error || `HTTP ${resp.status}` });
        return;
      }
      sendResponse({ ok: true, product_id: data.product_id, warning: data.warning });

      // Best-effort notification (works in Safari Web Extensions).
      try {
        chrome.notifications?.create({
          type: "basic",
          iconUrl: "icons/icon-128.png",
          title: "Saved to Theo",
          message: data.warning || (msg.payload.name || "Item saved"),
        });
      } catch { /* notifications may be unavailable */ }
    } catch (err) {
      sendResponse({ ok: false, error: String(err.message || err) });
    }
  })();

  return true; // keep the message channel open for async sendResponse
});
