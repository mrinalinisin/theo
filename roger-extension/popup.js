const $ = (id) => document.getElementById(id);
const status = $("status");

const setStatus = (msg, kind = "") => {
  status.textContent = msg;
  status.className = "status" + (kind ? " " + kind : "");
};

// Selected tag names (lowercased keys → original-case display names).
const selected = new Map();

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function collect() {
  const tab = await getActiveTab();
  if (!tab || !tab.id) throw new Error("No active tab");
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["content.js"],
  });
  return results && results[0] && results[0].result;
}

function fillImageOptions(images) {
  const sel = $("image_url");
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = images.length ? "— none —" : "— no images found —";
  sel.appendChild(none);
  images.forEach((src) => {
    const o = document.createElement("option");
    o.value = src;
    o.textContent = src.length > 60 ? src.slice(0, 57) + "…" : src;
    sel.appendChild(o);
  });
  if (images.length) sel.selectedIndex = 1;
}

function makeChip(name, { isNew = false } = {}) {
  const chip = document.createElement("span");
  chip.className = "chip" + (isNew ? " new" : "");
  chip.textContent = name;
  chip.dataset.name = name;
  if (selected.has(name.toLowerCase())) chip.classList.add("selected");
  chip.addEventListener("click", () => {
    const key = name.toLowerCase();
    if (selected.has(key)) {
      selected.delete(key);
      chip.classList.remove("selected");
    } else {
      selected.set(key, name);
      chip.classList.add("selected");
    }
  });
  return chip;
}

async function loadTags() {
  const hint = $("tags-hint");
  const wrap = $("tag-chips");
  const endpoints = [
    "http://localhost:5111/tags?format=json",
    "http://127.0.0.1:5111/tags?format=json",
  ];
  for (const url of endpoints) {
    try {
      const resp = await fetch(url, { headers: { Accept: "application/json" } });
      if (!resp.ok) continue;
      const data = await resp.json();
      const tags = (data && data.tags) || [];
      wrap.innerHTML = "";
      tags.forEach((t) => wrap.appendChild(makeChip(t.name)));
      hint.textContent = tags.length ? `(${tags.length} available — click to toggle)` : "";
      return;
    } catch (_) {}
  }
  hint.textContent = "couldn't reach Theo";
  hint.style.color = "#b3261e";
}

$("new-tag").addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  ev.preventDefault();
  const name = ev.target.value.trim();
  if (!name) return;
  const key = name.toLowerCase();
  const wrap = $("tag-chips");
  // If a chip with that name already exists, just select it.
  const existing = Array.from(wrap.children).find(
    (c) => c.dataset.name && c.dataset.name.toLowerCase() === key,
  );
  if (existing) {
    if (!existing.classList.contains("selected")) existing.click();
  } else {
    const chip = makeChip(name, { isNew: true });
    selected.set(key, name);
    chip.classList.add("selected");
    wrap.appendChild(chip);
  }
  ev.target.value = "";
});

async function init() {
  loadTags();
  try {
    const data = await collect();
    if (!data) {
      setStatus("Couldn't read the page. Try reloading the tab.", "err");
      return;
    }
    $("url").value = data.url || "";
    $("name").value = (data.selected_text || data.title || "").slice(0, 256);
    $("store").value = data.store || "";
    $("notes").value = data.selected_text || "";
    fillImageOptions(data.images || []);
  } catch (e) {
    setStatus("Error: " + e.message, "err");
  }
}

$("roger-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("submit-btn");
  btn.disabled = true;
  setStatus("Sending…");

  const payload = {
    url: $("url").value,
    name: $("name").value.trim(),
    store: $("store").value.trim(),
    price: parseFloat($("price").value) || 0,
    currency: $("currency").value.trim().toUpperCase() || "INR",
    image_url: $("image_url").value || "",
    images: Array.from($("image_url").options).map((o) => o.value).filter(Boolean),
    notes: $("notes").value,
    tag_names: Array.from(selected.values()),
  };

  try {
    const resp = await chrome.runtime.sendMessage({ type: "roger:send", payload });
    if (resp && resp.ok) {
      setStatus("Saved to Theo ✓", "ok");
      setTimeout(() => window.close(), 700);
    } else {
      setStatus("Failed: " + (resp && resp.error ? resp.error : "unknown error"), "err");
      btn.disabled = false;
    }
  } catch (e) {
    setStatus("Failed: " + e.message, "err");
    btn.disabled = false;
  }
});

init();
