# Theo

A personal shopping price tracker that monitors product prices, sends alerts on changes, and helps you stay within budget.

Add items by pasting a product URL. Theo scrapes the product name, price, images, and variants automatically. A background scheduler re-checks prices on a configurable interval and notifies you via WhatsApp when prices drop or rise.

## Features

- **Product scraping** — paste a URL to extract name, price, images, sizes/colours
- **Price tracking** — configurable check intervals per item (default every 4 hours)
- **Analytics dashboard** — spending by tag (pie chart), monthly budget tracking
- **Purchase history** — filterable by time period, grouped by tag or month
- **Tag management** — organise items with coloured tags
- **WhatsApp integration** — add items, check budget, get price alerts via Twilio Sandbox
- **Playwright support** — headless browser fallback for JS-heavy sites that block regular scraping
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

### 3. Install Playwright browser (optional, for JS-heavy sites)

```bash
playwright install chromium
```

This downloads a headless Chromium binary (~90 MB). Skip this if you don't need to scrape sites that block regular HTTP requests — the app works fine without it.

### 4. Configure environment

Copy `.env` and fill in your values:

```bash
cp .env .env.local  # or edit .env directly
```

The only required change is `SECRET_KEY`. Twilio credentials are optional — the app works fully without WhatsApp.

### 5. Run

```bash
source venv/bin/activate
python app.py
```

The server starts on `http://localhost:5000`. The background price checker starts automatically.

## WhatsApp Integration (Twilio Sandbox)

Theo can send you price alerts and accept commands over WhatsApp using Twilio's free sandbox. This is entirely optional — all features work without it.

### Step 1: Create a Twilio account

1. Sign up at [twilio.com/try-twilio](https://www.twilio.com/try-twilio) (free tier works)
2. Go to **Console > Messaging > Try it out > Send a WhatsApp message**
3. You'll see a sandbox number and a join code (e.g. `join <two-words>`)

### Step 2: Join the sandbox from your phone

Open WhatsApp on your phone and send the join code message to the sandbox number shown in the Twilio console. You'll get a confirmation reply.

### Step 3: Get your credentials

From the Twilio Console dashboard, copy:
- **Account SID** (starts with `AC`)
- **Auth Token**

### Step 4: Configure `.env`

```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_WHATSAPP_TO=whatsapp:+91XXXXXXXXXX
```

- `TWILIO_WHATSAPP_FROM` — the sandbox number from Step 1 (default is usually `+14155238886`)
- `TWILIO_WHATSAPP_TO` — your phone number in E.164 format with `whatsapp:` prefix

### Step 5: Set up the webhook (for incoming messages)

For Theo to receive WhatsApp messages (so you can send URLs and commands), Twilio needs to reach your local server.

**Option A: ngrok (recommended for development)**

```bash
ngrok http 5000
```

Copy the HTTPS forwarding URL (e.g. `https://abc123.ngrok.io`) and set it in Twilio:

1. Go to **Console > Messaging > Try it out > WhatsApp Sandbox Settings**
2. Set **"When a message comes in"** to: `https://abc123.ngrok.io/webhook/whatsapp`
3. Method: **POST**
4. Save

**Option B: Local network (if your phone and Mac are on the same Wi-Fi)**

Find your Mac's local IP (`ifconfig | grep inet`) and use:
```
http://192.168.x.x:5000/webhook/whatsapp
```

### Step 6: Enable notifications in Settings

In the Theo web UI, go to **Settings** and toggle on:
- **Notify on price drop** — get alerted when a tracked item's price decreases
- **Notify on price rise** — get alerted when a price increases
- **Budget warning** — get alerted when spending passes 80% of your budget

### WhatsApp commands

Once set up, you can send these messages to the Twilio sandbox number:

| Message | What it does |
|---------|-------------|
| A product URL | Scrapes the product and adds it to your shopping list |
| `budget` | Shows current month's spending vs budget |
| `list` | Lists all items you're watching with current prices |
| `drops` | Shows price drops from the last 7 days |
| `help` | Lists available commands |

Items added via WhatsApp are tagged as "Uncategorised" — use the web UI to assign proper tags.

### Troubleshooting

- **Messages not arriving?** The Twilio Sandbox session expires after 72 hours of inactivity. Re-send the join code to reconnect.
- **Webhook not working?** Make sure ngrok is running and the URL in Twilio console matches your current ngrok URL (it changes each restart unless you have a paid plan).
- **Notifications not sending?** Check that the toggles are enabled in Settings and that all four `.env` values are filled in. Check the terminal for `[WhatsApp]` log messages.

## Browser rendering

Some sites (e.g. Urban Outfitters) return 403 errors or render product data only via JavaScript. Theo handles this in two ways:

1. **Auto-fallback** (default) — tries a fast HTTP request first. If the site returns 403 or a connection error, automatically retries with Playwright's headless Chromium.
2. **Always use browser** — toggle "Use browser rendering" in Settings to force Playwright for all scrapes. Slower but more reliable for JS-heavy sites.

Requires `playwright install chromium` to have been run during setup.

## Tech stack

- Python 3 + Flask (server-rendered templates)
- SQLite + SQLAlchemy
- APScheduler (background price checking)
- BeautifulSoup + lxml (HTML scraping)
- Playwright (headless browser fallback)
- Twilio SDK (WhatsApp messaging)
- Chart.js (analytics charts)

## Concepts
* Crawler - Scraper
* Scheduler - Price tracker
* Inventory - DB connectable to Metabase 
* Dashboards - UI templates