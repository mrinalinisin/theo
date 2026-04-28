// Injected into the active tab via chrome.scripting.executeScript.
// Must be a single self-contained function returning a serializable object.
// Order of preference: JSON-LD Product → OpenGraph → microdata → DOM heuristics.
function scrapePage() {
  const out = {
    url: location.href,
    name: "",
    store: location.hostname.replace(/^www\./, ""),
    price: "",
    currency: "",
    image_url: "",
    images: [],
    selected_text: (window.getSelection?.().toString() || "").trim(),
  };

  const symbolToCode = { "₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY" };

  // --- 1. JSON-LD ----------------------------------------------------------
  const ldNodes = document.querySelectorAll('script[type="application/ld+json"]');
  for (const node of ldNodes) {
    let data;
    try { data = JSON.parse(node.textContent); } catch { continue; }
    const items = Array.isArray(data) ? data : [data];
    for (const raw of items) {
      const list = raw["@graph"] ? raw["@graph"] : [raw];
      for (const item of list) {
        const type = item["@type"];
        const isProduct = type === "Product" ||
          (Array.isArray(type) && type.includes("Product"));
        if (!isProduct) continue;
        if (!out.name && item.name) out.name = String(item.name);
        if (!out.image_url) {
          const img = Array.isArray(item.image) ? item.image[0] : item.image;
          if (img) out.image_url = typeof img === "string" ? img : (img.url || "");
        }
        const offers = Array.isArray(item.offers) ? item.offers[0] : item.offers;
        if (offers) {
          if (!out.price && offers.price) out.price = String(offers.price);
          if (!out.currency && offers.priceCurrency) out.currency = String(offers.priceCurrency);
        }
        if (!out.store && item.brand) {
          out.store = typeof item.brand === "string" ? item.brand : (item.brand.name || out.store);
        }
      }
    }
  }

  // --- 2. OpenGraph / meta tags --------------------------------------------
  const meta = (sel) => document.querySelector(sel)?.content?.trim() || "";
  if (!out.name) out.name = meta('meta[property="og:title"]') || meta('meta[name="twitter:title"]');
  if (!out.image_url) out.image_url = meta('meta[property="og:image"]') || meta('meta[name="twitter:image"]');
  if (!out.store) out.store = meta('meta[property="og:site_name"]') || out.store;
  if (!out.price) out.price = meta('meta[property="product:price:amount"]') || meta('meta[itemprop="price"]');
  if (!out.currency) out.currency = meta('meta[property="product:price:currency"]');

  // --- 3. Microdata --------------------------------------------------------
  if (!out.name) out.name = document.querySelector('[itemprop="name"]')?.textContent?.trim() || "";
  if (!out.price) {
    const el = document.querySelector('[itemprop="price"]');
    out.price = el?.getAttribute("content") || el?.textContent?.trim() || "";
  }

  // --- 4. DOM fallbacks ----------------------------------------------------
  if (!out.name) out.name = document.querySelector("h1")?.textContent?.trim() || document.title;
  if (!out.name) out.name = document.title;

  // Price regex fallback — first currency-symbol+number on the page.
  if (!out.price) {
    const text = document.body?.innerText || "";
    const m = text.match(/([₹$€£¥])\s?([\d,]+(?:\.\d+)?)/);
    if (m) {
      out.price = m[2].replace(/,/g, "");
      if (!out.currency) out.currency = symbolToCode[m[1]] || "";
    }
  }

  // Normalise price to a plain number string
  if (out.price) {
    const cleaned = String(out.price).replace(/[^\d.]/g, "");
    out.price = cleaned;
  }
  if (!out.currency) out.currency = "INR";

  // --- 5. Image gallery ----------------------------------------------------
  // Collect images that are actually rendered on screen at a reasonable size.
  const seen = new Set();
  const candidates = [];
  if (out.image_url) {
    candidates.push(out.image_url);
    seen.add(out.image_url);
  }
  for (const img of document.querySelectorAll("img")) {
    const src = img.currentSrc || img.src;
    if (!src || seen.has(src)) continue;
    if (src.startsWith("data:")) continue;
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    if (w < 200 || h < 200) continue;
    seen.add(src);
    candidates.push(src);
    if (candidates.length >= 24) break;
  }
  out.images = candidates;
  if (!out.image_url && candidates.length) out.image_url = candidates[0];

  return out;
}

// Last expression is what chrome.scripting.executeScript returns.
scrapePage();
