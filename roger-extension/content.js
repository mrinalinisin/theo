// Runs in the page context when the popup asks for the current selection.
// Returns selected text + URL + best-guess image candidates.

(function collectFromPage() {
  const sel = window.getSelection();
  const selectedText = sel ? sel.toString().trim() : "";

  // Try to find images: og:image, twitter:image, then the largest <img>
  // near the selection (or on the page if no selection).
  const images = [];
  const seen = new Set();

  const push = (u) => {
    if (!u) return;
    try {
      const abs = new URL(u, document.baseURI).href;
      if (!seen.has(abs) && /^https?:/i.test(abs)) {
        seen.add(abs);
        images.push(abs);
      }
    } catch (_) {}
  };

  document.querySelectorAll('meta[property="og:image"], meta[name="og:image"], meta[name="twitter:image"], meta[property="twitter:image"]')
    .forEach((m) => push(m.getAttribute("content")));

  // Image near the selection first
  if (sel && sel.rangeCount) {
    const range = sel.getRangeAt(0);
    let node = range.commonAncestorContainer;
    if (node.nodeType === Node.TEXT_NODE) node = node.parentElement;
    let scope = node;
    for (let i = 0; i < 4 && scope && scope.parentElement; i++) scope = scope.parentElement;
    if (scope) scope.querySelectorAll("img").forEach((img) => push(img.currentSrc || img.src));
  }

  // Fall back to the largest images on the page
  const all = Array.from(document.querySelectorAll("img"))
    .map((img) => ({ src: img.currentSrc || img.src, area: (img.naturalWidth || img.width) * (img.naturalHeight || img.height) }))
    .filter((x) => x.src && x.area > 10000)
    .sort((a, b) => b.area - a.area)
    .slice(0, 8);
  all.forEach((x) => push(x.src));

  // Best-guess store name = hostname without the TLD
  const host = location.hostname.replace(/^www\./, "");
  const store = host.split(".")[0].replace(/^./, (c) => c.toUpperCase());

  return {
    url: location.href,
    title: document.title,
    selected_text: selectedText,
    images: images.slice(0, 12),
    store,
  };
})();
