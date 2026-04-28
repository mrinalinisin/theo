# Theo Clipper — Safari Web Extension

Clips a product from the current page into Theo via `POST /products/new_from_browser`.

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest. Permissions: `activeTab`, `scripting`, `storage`, `notifications`, host `http://localhost:5000/*`. |
| `scraper.js` | Injected into the active tab. Reads JSON-LD → OpenGraph → microdata → DOM heuristics. Returns `{url, name, store, price, currency, image_url, images[], selected_text}`. |
| `popup.html` / `popup.css` / `popup.js` | The form UI shown when the toolbar button is clicked. Pre-fills from the scraper, lets the user pick which images to save (first selected = primary). |
| `background.js` | Service worker. Receives `{type: "save", payload}` from the popup and POSTs to Theo. |
| `icons/` | Toolbar / store icons. **You need to drop in 16/48/128 px PNGs** before shipping; Safari will use a default icon if missing during dev. |

## Building for Safari

Theo runs at `http://localhost:5000` (per `app.py:1466` and the `sv start theo` workflow), and the route is already CORS-allowlisted at `app.py:469`.

```sh
# from the repo root
xcrun safari-web-extension-converter ./theo-extension \
  --project-location ./theo-extension/SafariApp \
  --bundle-identifier com.theo.clipper \
  --no-open
```

Then:

1. Open the generated Xcode project in `theo-extension/SafariApp/`.
2. Build & run the wrapper app (⌘R).
3. In Safari → **Settings → Advanced** → enable **Show Develop menu**.
4. **Develop → Allow Unsigned Extensions** (re-enable each Safari restart, or sign with your Apple ID).
5. **Settings → Extensions** → enable **Theo Clipper**.

Make sure Theo is running (`sv start theo`) before you click the toolbar button.

## Save flow

1. Click the toolbar icon on any product page.
2. Popup injects `scraper.js`, pre-fills the form, shows image thumbnails (≥200×200 px, up to 24).
3. Click thumbnails to select; the first-selected becomes the primary image (`image_url`), the rest go into `images[]`.
4. Edit fields → **Save** → background POSTs to `/products/new_from_browser`.
5. On success, the popup shows ✓ and auto-closes; a duplicate-image warning from the backend is surfaced inline.

## Known gaps (deferred)

- No icons committed yet — add `icons/icon-{16,48,128}.png`.
- No options page — `THEO_BASE` is hardcoded in `background.js`. Move to `chrome.storage` when we want a non-localhost setup.
- No context-menu entrypoints (right-click on image / selection) — backend already supports the minimal payload, so this is a small follow-up.
- No Chrome manifest tweaks — Safari-only for now.
