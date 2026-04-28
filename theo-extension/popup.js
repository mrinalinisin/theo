const form = document.getElementById("form");
const statusEl = document.getElementById("status");
const imagesEl = document.getElementById("images");
const imgCountEl = document.getElementById("img-count");
const saveBtn = document.getElementById("save");
const cancelBtn = document.getElementById("cancel");

// Selection state for images: ordered array of URLs.
// Index 0 is the "primary" image (image_url); rest go into images[].
let selected = [];

function setStatus(msg, kind = "") {
  statusEl.textContent = msg;
  statusEl.className = kind;
}

function renderImages(urls) {
  imagesEl.innerHTML = "";
  imgCountEl.textContent = urls.length ? `(${selected.length}/${urls.length} selected)` : "(none found)";
  for (const url of urls) {
    const div = document.createElement("div");
    div.className = "thumb";
    if (selected.includes(url)) div.classList.add("selected");
    if (selected[0] === url) div.classList.add("primary");

    const img = document.createElement("img");
    img.src = url;
    img.loading = "lazy";
    img.onerror = () => div.remove();

    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = selected.indexOf(url) >= 0 ? String(selected.indexOf(url) + 1) : "";

    div.append(img, badge);
    div.addEventListener("click", () => {
      const i = selected.indexOf(url);
      if (i >= 0) selected.splice(i, 1);
      else selected.push(url);
      renderImages(urls);
    });
    imagesEl.append(div);
  }
}

function fillForm(scraped) {
  form.name.value = scraped.name || "";
  form.store.value = scraped.store || "";
  form.price.value = scraped.price || "";
  if (scraped.currency) {
    const opt = [...form.currency.options].find(o => o.value === scraped.currency.toUpperCase());
    if (opt) form.currency.value = opt.value;
  }
  form.notes.value = scraped.selected_text || "";

  // Default: primary image first, then everything else.
  const all = scraped.images || [];
  selected = scraped.image_url
    ? [scraped.image_url, ...all.filter(u => u !== scraped.image_url)].slice(0, 1)
    : (all[0] ? [all[0]] : []);
  renderImages(all);
}

async function scrapeActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No active tab");
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["scraper.js"],
  });
  return results[0]?.result;
}

(async () => {
  setStatus("Reading page…");
  try {
    const scraped = await scrapeActiveTab();
    if (!scraped) throw new Error("No data");
    fillForm(scraped);
    setStatus("");
  } catch (err) {
    setStatus(err.message || "Could not read page", "error");
  }
})();

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  saveBtn.disabled = true;
  setStatus("Saving…");

  const fd = new FormData(form);
  const tag_names = (fd.get("tag_names") || "")
    .split(",")
    .map(s => s.trim())
    .filter(Boolean);

  const payload = {
    url: (await chrome.tabs.query({ active: true, currentWindow: true }))[0]?.url || "",
    name: fd.get("name"),
    store: fd.get("store"),
    price: fd.get("price"),
    currency: fd.get("currency"),
    notes: fd.get("notes"),
    track_price: form.track_price.checked,
    tag_names,
    image_url: selected[0] || "",
    images: selected.slice(1),
  };

  const resp = await chrome.runtime.sendMessage({ type: "save", payload });
  if (resp?.ok) {
    setStatus(resp.warning ? `Saved — ${resp.warning}` : "Saved ✓", "ok");
    setTimeout(() => window.close(), 900);
  } else {
    saveBtn.disabled = false;
    setStatus(resp?.error || "Save failed", "error");
  }
});

cancelBtn.addEventListener("click", () => window.close());
