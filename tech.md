# Tech Notes

## Where are the images saved?

Images are saved to disk in the `instance/images/` directory (under Flask's `instance_path`). The `image_store.py` module handles all image persistence — it accepts images in three forms (remote URLs, base64 data URIs, or already-saved filenames), downloads/decodes them as needed, and writes them as files named `product_{product_id}_{index}.{ext}` (e.g. `product_42_0.jpg`). For edits that add new images, a timestamp-based naming scheme (`product_{id}_{timestamp}_{i}.{ext}`) is used to avoid filename collisions with existing images.

## How many simultaneous client requests can the server handle?

The server is run using Flask's built-in development server (`app.run(debug=True)`), which uses a single process with a single thread by default. This means it can handle **only one request at a time** — while one request is being processed, any other incoming requests are queued and must wait. This is fine for personal or development use but wouldn't scale for production traffic. To handle concurrent requests, you'd typically deploy behind a production WSGI server like Gunicorn or Waitress (neither of which is configured in this project).

## How many simultaneous connections from server can the database handle?

The database is **SQLite** (`sqlite:///money_penny.db`), which operates as an embedded file-based database rather than a client-server one — so there's no separate database process accepting connections. SQLite allows **unlimited simultaneous readers** but only **one writer at a time**. When a write is in progress, other writers block (by default for 5 seconds in Python's `sqlite3` module before raising a "database is locked" error). On the application side, SQLAlchemy uses its default pool strategy for SQLite (with `pool_pre_ping` enabled for connection health checks). Since the Flask server is single-threaded, only one connection is actually used at a time in practice. If the app were moved to a multi-worker setup, reads could happen concurrently, but writes would still serialize at the SQLite level.

## How does the scraper work? How does it know what to get from a page?

The scraper (`scraper.py`) uses a layered fallback strategy — it tries the most structured data source first, then progressively falls back to less reliable methods.

### Fetching the HTML

There are two backends for getting the page HTML:

1. **`requests`** (default) — fast, plain HTTP, no JavaScript execution
2. **Playwright** (headless Chromium) — renders JavaScript, handles SPAs and bot-protected pages

The choice is made in `_fetch_html`:
- A **per-domain override** (`DomainStrategy` table) takes highest priority — you can force specific domains to always use Playwright or always use requests via the Settings page.
- Otherwise, the global **"use browser rendering"** toggle from Settings applies.
- If requests fails with a 403 or connection error, it **auto-falls back** to Playwright.

There's also a **block detection** step — if the fetched page title contains phrases like "access denied" or "just a moment" (Cloudflare), it retries with Playwright even if requests technically succeeded.

### Extracting product data

This is a cascade of four extraction strategies:

1. **JSON-LD (most reliable):** `_extract_jsonld` looks for `<script type="application/ld+json">` tags containing structured `Product` data. This is a standard that e-commerce sites embed for search engines (Google Shopping, etc.). When present, it gives you name, price, images, and even variants in a clean, machine-readable format. The scraper handles both flat objects and `@graph` arrays.

2. **Open Graph meta tags:** If JSON-LD didn't yield a name or image, it checks `og:title` and `og:image` meta tags. These are the same tags that generate link previews when you share a URL on social media — most product pages have them.

3. **HTML `<title>` and `<h1>`:** Next fallback for the product name. The `<title>` gets split on `|` and `-` to strip store-name suffixes like "Product Name | Amazon.in".

4. **CSS selector heuristics for price:** `_extract_price_from_html` tries a prioritized list of CSS selectors that match common e-commerce patterns — classes containing `price` + `current`/`sale`/`final`, `[itemprop="price"]`, `[data-price]`, `.price`, `#price`. As a last resort, regex over the entire page text looking for currency patterns like `₹2,490` or `Rs. 1,299`.

### Images and variants

- `_extract_images` has Amazon-specific selectors (`#landingImage`, `#altImages`) plus generic gallery/carousel/slider patterns. A junk filter (`_is_junk_image`) removes SVGs, icons, tracking pixels, and placeholders.
- `_extract_variants_from_html` looks for size/colour swatches scoped to product forms, checking `[class*="size"] button`, `[data-option-name*="ize"]`, etc. A text filter rejects generic button labels like "Add to cart" or "Select".
