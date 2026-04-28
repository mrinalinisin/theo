# Roger

A Safari Web Extension that sends selected page content to your local Theo app
(`http://localhost:5111/products/new_from_browser`).

## Files

- `manifest.json` — MV3 manifest, declares popup + background worker + host permissions for `localhost:5111`.
- `popup.html` / `popup.css` / `popup.js` — the toolbar UI.
- `content.js` — injected on demand to read the page's selection, URL, and image candidates.
- `background.js` — service worker that POSTs the payload to Theo (extension origin avoids mixed-content/CORS issues).

## Load it in Safari (development)

1. Safari → Settings → Advanced → enable **Show features for web developers**.
2. Develop menu → **Allow Unsigned Extensions** (you'll need to repeat this each Safari session).
3. To package as a real Safari app extension:
   ```
   xcrun safari-web-extension-converter /Users/sindhus/Desktop/ss_life/Theo/roger-extension
   ```
   Open the generated Xcode project, run it once, then enable the extension in Safari → Settings → Extensions.

For Chrome (handy for quick iteration): `chrome://extensions` → Developer mode → **Load unpacked** → pick this folder.

## Use

1. Make sure Theo is running (`sv start theo`, listening on `:5111`).
2. Select some text on any product page (optional — you can also send without a selection).
3. Click the Roger toolbar icon. The popup pre-fills name/store/notes/images from the page.
4. Adjust the form, click **Send to Theo**.

## Icons

Drop `icon-16.png`, `icon-32.png`, `icon-48.png`, `icon-128.png` into `icons/`.
The extension still loads without them; Safari just shows a default placeholder.
