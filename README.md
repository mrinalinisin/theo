# Theo

A personal **inventory app** for things you buy. Theo stores the products you care about — what you're watching, what you've ordered, what's arrived — with images, tags, prices, and notes. It's deliberately not a scraper or a price tracker.

Products are clipped into Theo from the browser using the **[Roger](../roger-extension)** Safari extension. If you want price alerts, point an external tool like **OpenClaw** at your stored URLs.

## Features

- **Inventory by status** — watching, awaiting delivery, purchased
- **Tags** — organise items with coloured tags
- **Cart and checkout** — bulk-mark a set of items as purchased in one go
- **Purchase history** — filterable by time period, grouped by tag or month
- **Stats** — spending by tag (pie chart), monthly budget tracking
- **PWA** — installable on iOS (Add to Home Screen) and macOS (Add to Dock)
- **Responsive** — works on desktop, tablet, and mobile

## Setup

### 1. Clone and create virtual environment

```bash
cd Theo
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

Copy `.env.example` to `.env` and set `SECRET_KEY` to any random string.

### 4. Run

```bash
python app.py
```

Theo runs at `http://localhost:5000` (or whatever `PORT` is set to in `.env`).

## Adding products

Theo's only ingestion path is the JSON endpoint `POST /products/new_from_browser`, used by the Roger Safari extension. The extension reads the page you're browsing, lets you pick the right images and tags, and POSTs the data to Theo.

If you need to add an item without Roger (e.g. an offline purchase), you can call the endpoint directly with curl:

```bash
curl -X POST http://localhost:5000/products/new_from_browser \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/item","name":"Thing","price":1500,"currency":"INR"}'
```

## Tech

- **Flask** + Flask-SQLAlchemy on **SQLite**
- **Pillow** for image processing and perceptual hashing
- **requests** for fetching images from Roger payloads
- No scrapers, no schedulers, no third-party messaging
