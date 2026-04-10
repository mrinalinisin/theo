# Tech Notes

## Where are the images saved?

Images are saved to disk in the `instance/images/` directory (under Flask's `instance_path`). The `image_store.py` module handles all image persistence — it accepts images in three forms (remote URLs, base64 data URIs, or already-saved filenames), downloads/decodes them as needed, and writes them as files named `product_{product_id}_{index}.{ext}` (e.g. `product_42_0.jpg`). For edits that add new images, a timestamp-based naming scheme (`product_{id}_{timestamp}_{i}.{ext}`) is used to avoid filename collisions with existing images.

## How many simultaneous client requests can the server handle?

The server is run using Flask's built-in development server (`app.run(debug=True)`), which uses a single process with a single thread by default. This means it can handle **only one request at a time** — while one request is being processed, any other incoming requests are queued and must wait. This is fine for personal or development use but wouldn't scale for production traffic. To handle concurrent requests, you'd typically deploy behind a production WSGI server like Gunicorn or Waitress (neither of which is configured in this project). That said, the SQLAlchemy connection pool in `config.py` is configured for up to 30 connections (pool_size=10, max_overflow=20), which suggests it was set up with the possibility of future concurrent access in mind.

## How many simultaneous connections from server can the database handle?

The database is **SQLite** (`sqlite:///gummi.db`), which operates as an embedded file-based database rather than a client-server one — so there's no separate database process accepting connections. SQLite allows **unlimited simultaneous readers** but only **one writer at a time**. When a write is in progress, other writers block (by default for 5 seconds in Python's `sqlite3` module before raising a "database is locked" error). On the application side, SQLAlchemy's connection pool is configured with `pool_size=10` and `max_overflow=20`, meaning up to **30 connections** can be open simultaneously from the app. However, since the Flask server is single-threaded, only one connection is actually used at a time in practice. If the app were moved to a multi-worker setup, those 30 pooled connections could all read concurrently, but writes would still serialize at the SQLite level.
