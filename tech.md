# Tech Notes

## Where are the images saved?

Images are saved to disk in the `instance/images/` directory (under Flask's `instance_path`). The `image_store.py` module handles all image persistence — it accepts images in three forms (remote URLs, base64 data URIs, or already-saved filenames), downloads/decodes them as needed, and writes them as files named `product_{product_id}_{index}.{ext}` (e.g. `product_42_0.jpg`). For edits that add new images, a timestamp-based naming scheme (`product_{id}_{timestamp}_{i}.{ext}`) is used to avoid filename collisions with existing images.

## How many simultaneous client requests can the server handle?

The server is run using Flask's built-in development server (`app.run(debug=True)`), which uses a single process with a single thread by default. This means it can handle **only one request at a time** — while one request is being processed, any other incoming requests are queued and must wait. This is fine for personal or development use but wouldn't scale for production traffic. To handle concurrent requests, you'd typically deploy behind a production WSGI server like Gunicorn or Waitress (neither of which is configured in this project).

## How many simultaneous connections from server can the database handle?

The database is **SQLite** (`sqlite:///theo.db`), which operates as an embedded file-based database rather than a client-server one — so there's no separate database process accepting connections. SQLite allows **unlimited simultaneous readers** but only **one writer at a time**. When a write is in progress, other writers block (by default for 5 seconds in Python's `sqlite3` module before raising a "database is locked" error). On the application side, SQLAlchemy uses its default pool strategy for SQLite (with `pool_pre_ping` enabled for connection health checks). Since the Flask server is single-threaded, only one connection is actually used at a time in practice. If the app were moved to a multi-worker setup, reads could happen concurrently, but writes would still serialize at the SQLite level.

## How does product ingestion work?

Theo doesn't scrape — that responsibility belongs to the **Roger** Safari extension. Roger reads the page the user is browsing, lets them pick the best images and tags, and POSTs a JSON payload to `/products/new_from_browser`. The endpoint:

1. Canonicalizes the URL via `urls.sanitize_url` (strips tracking params, normalizes whitespace).
2. Resolves the currency code → `Currency` row, defaulting to INR.
3. Resolves tag names → `Tag` rows, **creating new tags on the fly** if the user typed an unknown name.
4. Saves images to disk via `image_store.save_images_for_product` (which also computes perceptual hashes for duplicate detection).
5. Returns `{"ok": true, "product_id": ..., "duplicate_name": ...}` so Roger can show feedback.

Price tracking, if needed, is delegated to external tools (e.g. OpenClaw) — they can read product URLs from Theo's DB or API and watch them independently.
